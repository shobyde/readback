"""Review gate tests: draft queue, edits, renumbering, approve/reject."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from readback.db import transaction
from readback.models import utc_now
from readback.review import (
    approve_quiz,
    delete_question,
    get_quiz,
    list_approved,
    list_drafts,
    reject_quiz,
    update_question,
)

CHOICES = ["A", "B", "C", "D"]


def make_quiz(
    conn,
    book_id,
    status="draft",
    created_at=None,
    source_notes="",
    model="test-model",
):
    created_at = created_at or utc_now()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO quizzes (book_id, status, model, source_notes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, status, model, source_notes, created_at),
        )
    return cursor.lastrowid


def add_question(
    conn,
    quiz_id,
    ordinal,
    prompt="What happened?",
    choices=None,
    answer_index=0,
    explanation="",
):
    choices = CHOICES if choices is None else choices
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO questions "
            "(quiz_id, ordinal, prompt, choices_json, answer_index, explanation) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (quiz_id, ordinal, prompt, json.dumps(choices), answer_index, explanation),
        )
    return cursor.lastrowid


def quiz_with_questions(conn, book_id, count=3, **kwargs):
    quiz_id = make_quiz(conn, book_id, **kwargs)
    for ordinal in range(1, count + 1):
        add_question(conn, quiz_id, ordinal, prompt=f"Question {ordinal}?")
    return quiz_id


def status_of(conn, quiz_id):
    return conn.execute(
        "SELECT status FROM quizzes WHERE id = ?", (quiz_id,)
    ).fetchone()["status"]


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def test_list_drafts_newest_first_with_questions(conn, book):
    older = make_quiz(conn, book.id, created_at="2026-01-01T00:00:00+00:00")
    add_question(conn, older, 1, prompt="Old question?")
    newer = make_quiz(conn, book.id, created_at="2026-02-01T00:00:00+00:00")
    add_question(conn, newer, 1, prompt="New question?")
    add_question(conn, newer, 2, prompt="Second question?")
    quiz_with_questions(conn, book.id, count=1, status="approved")

    drafts = list_drafts(conn)

    assert [quiz.id for quiz in drafts] == [newer, older]
    assert all(quiz.status == "draft" for quiz in drafts)
    assert [question.prompt for question in drafts[0].questions] == [
        "New question?",
        "Second question?",
    ]
    assert drafts[0].questions[0].choices == CHOICES


def test_get_quiz_none_and_ordered_questions(conn, book):
    assert get_quiz(conn, 9999) is None

    quiz_id = make_quiz(conn, book.id)
    add_question(conn, quiz_id, 3, prompt="Third?")
    add_question(conn, quiz_id, 1, prompt="First?")
    add_question(conn, quiz_id, 2, prompt="Second?")

    quiz = get_quiz(conn, quiz_id)
    assert quiz is not None
    assert quiz.id == quiz_id
    assert [question.ordinal for question in quiz.questions] == [1, 2, 3]
    assert [question.prompt for question in quiz.questions] == [
        "First?",
        "Second?",
        "Third?",
    ]


def test_list_approved_filters_by_book(conn, book):
    other_book_id = None
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO books (title, author, points, created_at) VALUES (?, ?, ?, ?)",
            ("Other Book", "Someone", 3.0, utc_now()),
        )
        other_book_id = cursor.lastrowid

    first = quiz_with_questions(conn, book.id, count=2, status="approved")
    second = quiz_with_questions(conn, other_book_id, count=2, status="approved")
    quiz_with_questions(conn, book.id, count=2, status="draft")

    assert {quiz.id for quiz in list_approved(conn)} == {first, second}
    assert [quiz.id for quiz in list_approved(conn, book_id=book.id)] == [first]
    assert [quiz.id for quiz in list_approved(conn, book_id=other_book_id)] == [second]
    assert all(len(quiz.questions) == 2 for quiz in list_approved(conn))


# --------------------------------------------------------------------------- #
# edits
# --------------------------------------------------------------------------- #


def test_update_question_changes_only_given_fields(conn, book):
    quiz_id = make_quiz(conn, book.id)
    question_id = add_question(
        conn, quiz_id, 1, prompt="Original?", answer_index=2, explanation="Because."
    )

    update_question(conn, question_id, prompt="Rewritten?")

    quiz = get_quiz(conn, quiz_id)
    question = quiz.questions[0]
    assert question.prompt == "Rewritten?"
    assert question.choices == CHOICES
    assert question.answer_index == 2
    assert question.explanation == "Because."

    update_question(
        conn,
        question_id,
        choices=["W", "X", "Y", "Z"],
        answer_index=3,
        explanation="New explanation.",
    )

    question = get_quiz(conn, quiz_id).questions[0]
    assert question.prompt == "Rewritten?"
    assert question.choices == ["W", "X", "Y", "Z"]
    assert question.answer_index == 3
    assert question.explanation == "New explanation."


@pytest.mark.parametrize(
    "kwargs",
    [
        {"prompt": ""},
        {"prompt": "   "},
        {"choices": ["A", "B", "C"]},
        {"choices": ["A", "B", "C", "D", "E"]},
        {"choices": ["A", "B", "C", "D", "E", "F"]},
        {"choices": ["A", "A", "C", "D"]},
        {"choices": ["A", "B", "", "D"]},
        {"choices": ["A", "B", "C"]},
        {"choices": "A,B,C,D"},
        {"answer_index": 4},
        {"answer_index": -1},
        {"answer_index": "nope"},
    ],
)
def test_update_question_rejects_invalid_values(conn, book, kwargs):
    quiz_id = make_quiz(conn, book.id)
    question_id = add_question(conn, quiz_id, 1, prompt="Original?", answer_index=1)

    with pytest.raises(ValueError):
        update_question(conn, question_id, **kwargs)

    question = get_quiz(conn, quiz_id).questions[0]
    assert question.prompt == "Original?"
    assert question.choices == CHOICES
    assert question.answer_index == 1


def test_update_question_missing_raises(conn):
    with pytest.raises(ValueError):
        update_question(conn, 424242, prompt="Anything?")


def test_update_question_accepts_string_answer_index(conn, book):
    quiz_id = make_quiz(conn, book.id)
    question_id = add_question(conn, quiz_id, 1)

    update_question(conn, question_id, answer_index="2")

    assert get_quiz(conn, quiz_id).questions[0].answer_index == 2


def test_delete_question_renumbers_contiguously(conn, book):
    quiz_id = make_quiz(conn, book.id)
    ids = [
        add_question(conn, quiz_id, ordinal, prompt=f"Question {ordinal}?")
        for ordinal in range(1, 5)
    ]

    delete_question(conn, ids[1])

    questions = get_quiz(conn, quiz_id).questions
    assert [question.ordinal for question in questions] == [1, 2, 3]
    assert [question.prompt for question in questions] == [
        "Question 1?",
        "Question 3?",
        "Question 4?",
    ]

    delete_question(conn, ids[3])

    questions = get_quiz(conn, quiz_id).questions
    assert [question.ordinal for question in questions] == [1, 2]
    assert [question.prompt for question in questions] == [
        "Question 1?",
        "Question 3?",
    ]


def test_delete_question_missing_raises(conn):
    with pytest.raises(ValueError):
        delete_question(conn, 424242)


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #


def test_approve_refuses_quiz_with_no_questions(conn, book):
    quiz_id = make_quiz(conn, book.id)

    with pytest.raises(ValueError) as excinfo:
        approve_quiz(conn, quiz_id)

    assert "no questions" in str(excinfo.value).lower()
    assert excinfo.value.problems
    assert status_of(conn, quiz_id) == "draft"


def test_approve_refuses_invalid_question(conn, book):
    quiz_id = make_quiz(conn, book.id)
    add_question(conn, quiz_id, 1)
    add_question(conn, quiz_id, 2, choices=["Same", "Same", "C", "D"])

    with pytest.raises(ValueError) as excinfo:
        approve_quiz(conn, quiz_id)

    assert excinfo.value.problems
    assert str(excinfo.value)
    assert status_of(conn, quiz_id) == "draft"


def test_approve_sets_status_timestamp_and_actor(conn, book):
    quiz_id = quiz_with_questions(conn, book.id, count=3)

    approve_quiz(conn, quiz_id, actor="Grandma")

    row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    assert row["status"] == "approved"
    assert row["approved_by"] == "Grandma"
    datetime.fromisoformat(row["approved_at"])  # parses as ISO-8601
    assert [quiz.id for quiz in list_approved(conn)] == [quiz_id]
    assert list_drafts(conn) == []


def test_approve_missing_quiz_raises(conn):
    with pytest.raises(ValueError):
        approve_quiz(conn, 424242)


def test_reject_sets_status_and_appends_note(conn, book):
    quiz_id = quiz_with_questions(
        conn, book.id, count=2, source_notes="Generated from metadata."
    )

    reject_quiz(conn, quiz_id, actor="parent", note="Too easy.")

    row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    assert row["status"] == "rejected"
    assert "Generated from metadata." in row["source_notes"]
    assert "Too easy." in row["source_notes"]
    assert list_drafts(conn) == []
    assert list_approved(conn) == []


def test_reject_without_note_leaves_notes_alone(conn, book):
    quiz_id = quiz_with_questions(conn, book.id, count=1, source_notes="Keep me.")

    reject_quiz(conn, quiz_id)

    row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    assert row["status"] == "rejected"
    assert row["source_notes"] == "Keep me."


def test_reject_missing_quiz_raises(conn):
    with pytest.raises(ValueError):
        reject_quiz(conn, 424242, note="nope")
