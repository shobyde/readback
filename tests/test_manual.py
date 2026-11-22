"""Books finished without a quiz: the manual reading path."""

from __future__ import annotations

import csv
import io

import pytest

from readback.books import add_book
from readback.export import export_reading_log_csv
from readback.points import log_manual_read, reader_summary, recent_attempts


def test_manual_read_awards_points_and_counts_as_a_book(conn, reader, book):
    result = log_manual_read(conn, reader.id, book.id)
    assert result.passed is True
    assert result.points_awarded == 5.0
    assert result.points_total == 5.0

    summary = reader_summary(conn, reader.id)
    assert summary.books_passed == 1
    assert summary.attempts == 1
    assert summary.quiz_attempts == 0
    assert summary.points_total == 5.0

    row = conn.execute("SELECT * FROM attempts").fetchone()
    assert row["kind"] == "manual"
    assert row["quiz_id"] is None
    assert row["book_id"] == book.id
    assert row["submitted_at"] is not None


def test_manual_read_appears_in_recent_activity(conn, reader, book):
    log_manual_read(conn, reader.id, book.id)
    recent = recent_attempts(conn)
    assert len(recent) == 1
    assert recent[0]["kind"] == "manual"
    assert recent[0]["quiz_id"] is None
    assert recent[0]["book_title"] == book.title
    assert recent[0]["points_awarded"] == 5.0


def test_manual_read_appears_in_the_export(conn, reader, book):
    log_manual_read(conn, reader.id, book.id)
    rows = list(csv.DictReader(io.StringIO(export_reading_log_csv(conn))))
    assert len(rows) == 1
    assert rows[0]["reader"] == reader.name
    assert rows[0]["book"] == book.title
    assert rows[0]["quiz_id"] == ""
    assert rows[0]["points_awarded"] == "5.0"


def test_manual_read_awards_once(conn, reader, book):
    log_manual_read(conn, reader.id, book.id)
    with pytest.raises(ValueError, match="already earned"):
        log_manual_read(conn, reader.id, book.id)
    assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM points_ledger").fetchone()["n"] == 1


def test_manual_read_of_a_zero_point_book(conn, reader):
    book_id = add_book(conn, title="A free read", author="", points=0)
    result = log_manual_read(conn, reader.id, book_id)
    assert result.points_awarded == 0.0
    assert reader_summary(conn, reader.id).books_passed == 1


def test_manual_read_validates_ids(conn, reader, book):
    with pytest.raises(ValueError, match="reader"):
        log_manual_read(conn, 999, book.id)
    with pytest.raises(ValueError, match="book"):
        log_manual_read(conn, reader.id, 999)


def test_manual_read_after_a_quiz_pass_is_refused(conn, reader, book):
    """Points are once per reader and book, whichever path got there first."""
    from readback.models import GeneratedQuestion, GeneratedQuiz
    from readback.points import start_attempt, submit_attempt
    from readback.quiz.generate import save_draft
    from readback.review import approve_quiz, get_quiz

    quiz = GeneratedQuiz(
        questions=[
            GeneratedQuestion(
                prompt="Who is Wilbur?",
                choices=["A pig", "A spider", "A girl", "A rat"],
                answer_index=0,
            )
        ]
    )
    quiz_id = save_draft(conn, book.id, quiz, "test")
    approve_quiz(conn, quiz_id)
    stored = get_quiz(conn, quiz_id)
    assert stored is not None
    attempt_id = start_attempt(conn, quiz_id, reader.id)
    submit_attempt(
        conn,
        attempt_id,
        {stored.questions[0].id: 0},
        pass_threshold=0.8,
    )

    with pytest.raises(ValueError, match="already earned"):
        log_manual_read(conn, reader.id, book.id)
