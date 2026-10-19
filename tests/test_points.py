"""Attempt, scoring, ledger, and summary tests."""

from __future__ import annotations

import json

import pytest

from readback.db import transaction
from readback.models import utc_now
from readback.points import (
    all_summaries,
    reader_summary,
    recent_attempts,
    start_attempt,
    submit_attempt,
)

CHOICES = ["A", "B", "C", "D"]


def make_book(conn, title="Points Book", author="A. Author", points=5.0):
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO books (title, author, isbn, points, grade_level, notes, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, author, None, points, "", "", utc_now()),
        )
    return cursor.lastrowid


def make_reader(conn, name="Bo"):
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO readers (name, created_at) VALUES (?, ?)",
            (name, utc_now()),
        )
    return cursor.lastrowid


def make_quiz(conn, book_id, count=5, status="approved", answer_index=1):
    """Insert a quiz whose questions all share ``answer_index``."""
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO quizzes (book_id, status, model, source_notes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, status, "test-model", "", utc_now()),
        )
        quiz_id = cursor.lastrowid
        question_ids = []
        for ordinal in range(1, count + 1):
            question = conn.execute(
                "INSERT INTO questions "
                "(quiz_id, ordinal, prompt, choices_json, answer_index, explanation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    quiz_id,
                    ordinal,
                    f"Question {ordinal}?",
                    json.dumps(CHOICES),
                    answer_index,
                    "",
                ),
            )
            question_ids.append(question.lastrowid)
    return quiz_id, question_ids


def correct_answers(question_ids, answer_index=1):
    return {question_id: answer_index for question_id in question_ids}


def wrong_answers(question_ids, answer_index=1):
    return {question_id: (answer_index + 1) % 4 for question_id in question_ids}


def ledger_rows(conn):
    return conn.execute("SELECT * FROM points_ledger").fetchall()


# --------------------------------------------------------------------------- #
# start_attempt
# --------------------------------------------------------------------------- #


def test_start_attempt_refuses_draft_and_missing(conn, book, reader):
    draft_id, _ = make_quiz(conn, book.id, status="draft")

    with pytest.raises(ValueError):
        start_attempt(conn, draft_id, reader.id)
    with pytest.raises(ValueError):
        start_attempt(conn, 424242, reader.id)
    with pytest.raises(ValueError):
        start_attempt(conn, draft_id, 424242)


def test_start_attempt_snapshots_total_questions(conn, book, reader):
    quiz_id, _ = make_quiz(conn, book.id, count=4)

    attempt_id = start_attempt(conn, quiz_id, reader.id)

    row = conn.execute(
        "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    assert row["quiz_id"] == quiz_id
    assert row["reader_id"] == reader.id
    assert row["total_questions"] == 4
    assert row["submitted_at"] is None
    assert row["correct_answers"] == 0


# --------------------------------------------------------------------------- #
# scoring and points
# --------------------------------------------------------------------------- #


def test_pass_awards_points_exactly_once(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=5)

    first = submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), correct_answers(question_ids)
    )

    assert first.score == 1.0
    assert first.passed is True
    assert (first.correct, first.total) == (5, 5)
    assert first.points_awarded == 5.0
    assert first.points_total == 5.0
    assert len(ledger_rows(conn)) == 1

    second = submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), correct_answers(question_ids)
    )

    assert second.passed is True
    assert second.points_awarded == 0.0
    assert second.points_total == 5.0
    assert len(ledger_rows(conn)) == 1


def test_failed_attempt_awards_nothing_then_pass_awards_once(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=5)

    failed = submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), wrong_answers(question_ids)
    )

    assert failed.passed is False
    assert failed.score == 0.0
    assert failed.points_awarded == 0.0
    assert failed.points_total == 0.0
    assert ledger_rows(conn) == []

    passed = submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), correct_answers(question_ids)
    )

    assert passed.passed is True
    assert passed.points_awarded == 5.0
    assert passed.points_total == 5.0
    assert len(ledger_rows(conn)) == 1


def test_partial_score_rounds_to_three_decimals(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=3)
    answers = {question_ids[0]: 1, question_ids[1]: 1, question_ids[2]: 0}

    result = submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), answers
    )

    assert result.correct == 2
    assert result.total == 3
    assert result.score == 0.667
    assert result.passed is False
    assert result.points_awarded == 0.0


def test_missing_answer_counts_as_wrong(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=5)
    answers = correct_answers(question_ids)
    answers.pop(question_ids[-1])

    result = submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), answers
    )

    assert result.correct == 4
    assert result.score == 0.8
    assert result.passed is True  # 0.8 >= default threshold

    stored = conn.execute(
        "SELECT question_id, choice_index, correct FROM attempt_answers "
        "WHERE attempt_id = ? ORDER BY question_id",
        (result.attempt_id,),
    ).fetchall()
    assert len(stored) == 5
    missing = [row for row in stored if row["question_id"] == question_ids[-1]][0]
    assert missing["choice_index"] == -1
    assert missing["correct"] == 0


def test_submit_is_idempotent(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=4)
    attempt_id = start_attempt(conn, quiz_id, reader.id)

    first = submit_attempt(conn, attempt_id, correct_answers(question_ids))
    before = conn.execute(
        "SELECT submitted_at, score, passed, correct_answers FROM attempts "
        "WHERE id = ?",
        (attempt_id,),
    ).fetchone()

    second = submit_attempt(conn, attempt_id, wrong_answers(question_ids))

    after = conn.execute(
        "SELECT submitted_at, score, passed, correct_answers FROM attempts "
        "WHERE id = ?",
        (attempt_id,),
    ).fetchone()

    assert second == first
    assert dict(after) == dict(before)
    assert first.points_awarded == 5.0
    assert len(ledger_rows(conn)) == 1
    answers = conn.execute(
        "SELECT * FROM attempt_answers WHERE attempt_id = ?", (attempt_id,)
    ).fetchall()
    assert len(answers) == 4
    assert all(row["correct"] == 1 for row in answers)


def test_threshold_argument_controls_pass(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=5)
    answers = correct_answers(question_ids)
    answers.pop(question_ids[-1])  # 4 / 5

    strict = submit_attempt(
        conn,
        start_attempt(conn, quiz_id, reader.id),
        answers,
        pass_threshold=0.9,
    )
    assert strict.score == 0.8
    assert strict.passed is False
    assert strict.points_awarded == 0.0

    lenient = submit_attempt(
        conn,
        start_attempt(conn, quiz_id, reader.id),
        answers,
        pass_threshold=0.5,
    )
    assert lenient.passed is True
    assert lenient.points_awarded == 5.0


def test_submit_unknown_attempt_raises(conn):
    with pytest.raises(ValueError):
        submit_attempt(conn, 424242, {})


def test_points_are_per_book(conn, book, reader):
    second_book = make_book(conn, title="Second Book", points=3.5)
    first_quiz, first_questions = make_quiz(conn, book.id, count=2)
    second_quiz, second_questions = make_quiz(conn, second_book, count=2)

    one = submit_attempt(
        conn, start_attempt(conn, first_quiz, reader.id), correct_answers(first_questions)
    )
    two = submit_attempt(
        conn,
        start_attempt(conn, second_quiz, reader.id),
        correct_answers(second_questions),
    )

    assert one.points_awarded == 5.0
    assert two.points_awarded == 3.5
    assert two.points_total == 8.5
    assert len(ledger_rows(conn)) == 2


def test_second_quiz_for_same_book_awards_nothing(conn, book, reader):
    first_quiz, first_questions = make_quiz(conn, book.id, count=2)
    second_quiz, second_questions = make_quiz(conn, book.id, count=2)

    first = submit_attempt(
        conn, start_attempt(conn, first_quiz, reader.id), correct_answers(first_questions)
    )
    second = submit_attempt(
        conn,
        start_attempt(conn, second_quiz, reader.id),
        correct_answers(second_questions),
    )

    assert first.points_awarded == 5.0
    assert second.passed is True
    assert second.points_awarded == 0.0
    assert second.points_total == 5.0
    assert len(ledger_rows(conn)) == 1


# --------------------------------------------------------------------------- #
# summaries and recent attempts
# --------------------------------------------------------------------------- #


def test_reader_summary_counts(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=4)
    submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), wrong_answers(question_ids)
    )
    submit_attempt(
        conn, start_attempt(conn, quiz_id, reader.id), correct_answers(question_ids)
    )
    start_attempt(conn, quiz_id, reader.id)  # unsubmitted, must not count

    summary = reader_summary(conn, reader.id)

    assert summary.reader_id == reader.id
    assert summary.name == reader.name
    assert summary.points_total == 5.0
    assert summary.books_passed == 1
    assert summary.attempts == 2


def test_reader_summary_unknown_raises(conn):
    with pytest.raises(ValueError):
        reader_summary(conn, 424242)


def test_all_summaries_ordered_by_name(conn, book, reader):
    bo_id = make_reader(conn, "Bo")
    quiz_id, question_ids = make_quiz(conn, book.id, count=2)

    submit_attempt(
        conn, start_attempt(conn, quiz_id, bo_id), correct_answers(question_ids)
    )

    summaries = all_summaries(conn)

    assert [summary.name for summary in summaries] == ["Ada", "Bo"]
    assert summaries[0].points_total == 0.0
    assert summaries[0].attempts == 0
    assert summaries[1].points_total == 5.0
    assert summaries[1].books_passed == 1


def test_recent_attempts_newest_first_and_excludes_unsubmitted(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=3)

    passed_id = start_attempt(conn, quiz_id, reader.id)
    submit_attempt(conn, passed_id, correct_answers(question_ids))
    start_attempt(conn, quiz_id, reader.id)  # stays unsubmitted
    failed_id = start_attempt(conn, quiz_id, reader.id)
    submit_attempt(conn, failed_id, wrong_answers(question_ids))

    recent = recent_attempts(conn)

    assert [entry["attempt_id"] for entry in recent] == [failed_id, passed_id]
    assert recent[0]["passed"] is False
    assert recent[0]["score"] == 0.0
    assert recent[0]["book_title"] == book.title
    assert recent[0]["reader_name"] == reader.name
    assert recent[0]["points_awarded"] == 0.0
    assert recent[1]["passed"] is True
    assert recent[1]["points_awarded"] == 5.0
    assert recent[1]["correct"] == 3
    assert recent[1]["total"] == 3
    assert recent[1]["submitted_at"]

    assert [entry["attempt_id"] for entry in recent_attempts(conn, limit=1)] == [
        failed_id
    ]
    assert recent_attempts(conn, limit=1)[0]["quiz_id"] == quiz_id
    assert recent_attempts(conn, limit=1)[0]["book_id"] == book.id
