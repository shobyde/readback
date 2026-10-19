"""Reading-log CSV export tests."""

from __future__ import annotations

import csv
import io
import json

from readback.db import transaction
from readback.export import COLUMNS, export_reading_log_csv
from readback.models import utc_now
from readback.points import start_attempt, submit_attempt

CHOICES = ["A", "B", "C", "D"]


def make_book(conn, title="Export Book", author="E. Author", points=4.0):
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO books (title, author, isbn, points, grade_level, notes, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, author, None, points, "", "", utc_now()),
        )
    return cursor.lastrowid


def make_reader(conn, name):
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO readers (name, created_at) VALUES (?, ?)",
            (name, utc_now()),
        )
    return cursor.lastrowid


def make_quiz(conn, book_id, count=2, status="approved"):
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
                    1,
                    "",
                ),
            )
            question_ids.append(question.lastrowid)
    return quiz_id, question_ids


def take_quiz(conn, quiz_id, question_ids, reader_id, *, correct=True):
    answers = {
        question_id: 1 if correct else 0 for question_id in question_ids
    }
    attempt_id = start_attempt(conn, quiz_id, reader_id)
    return submit_attempt(conn, attempt_id, answers)


def parse(text):
    return list(csv.reader(io.StringIO(text)))


def test_empty_log_is_header_only(conn):
    text = export_reading_log_csv(conn)

    assert text == ",".join(COLUMNS) + "\n"
    assert parse(text) == [list(COLUMNS)]


def test_row_content_and_newest_first(conn, book, reader):
    quiz_id, question_ids = make_quiz(conn, book.id, count=2)
    failed = take_quiz(conn, quiz_id, question_ids, reader.id, correct=False)
    passed = take_quiz(conn, quiz_id, question_ids, reader.id, correct=True)

    rows = parse(export_reading_log_csv(conn))

    assert rows[0] == list(COLUMNS)
    assert len(rows) == 3
    assert rows[1][0] == reader.name
    assert rows[1][1] == book.title
    assert rows[1][2] == book.author
    assert float(rows[1][3]) == book.points
    assert int(rows[1][4]) == quiz_id
    assert rows[1][6] == "1.0"
    assert rows[1][7] == "1"
    assert float(rows[1][8]) == book.points
    assert rows[2][6] == "0.0"
    assert rows[2][7] == "0"
    assert float(rows[2][8]) == 0.0

    # Newest attempt first.
    submitted = {
        row["id"]: row["submitted_at"]
        for row in conn.execute("SELECT id, submitted_at FROM attempts").fetchall()
    }
    assert submitted[passed.attempt_id] >= submitted[failed.attempt_id]


def test_reader_filter(conn, book, reader):
    other_reader_id = make_reader(conn, "Bo")
    quiz_id, question_ids = make_quiz(conn, book.id, count=2)
    take_quiz(conn, quiz_id, question_ids, reader.id, correct=True)
    take_quiz(conn, quiz_id, question_ids, other_reader_id, correct=False)

    everyone = parse(export_reading_log_csv(conn))
    ada_only = parse(export_reading_log_csv(conn, reader_id=reader.id))
    bo_only = parse(export_reading_log_csv(conn, reader_id=other_reader_id))

    assert {row[0] for row in everyone[1:]} == {"Ada", "Bo"}
    assert [row[0] for row in ada_only[1:]] == ["Ada"]
    assert [row[0] for row in bo_only[1:]] == ["Bo"]
    assert ada_only[0] == list(COLUMNS)
    assert parse(export_reading_log_csv(conn, reader_id=424242)) == [list(COLUMNS)]


def test_title_with_comma_is_quoted(conn, reader):
    book_id = make_book(conn, title="Hello, World", author="Comma, Author")
    quiz_id, question_ids = make_quiz(conn, book_id, count=2)
    take_quiz(conn, quiz_id, question_ids, reader.id, correct=True)

    text = export_reading_log_csv(conn)

    assert '"Hello, World"' in text
    assert '"Comma, Author"' in text
    rows = parse(text)
    assert rows[1][1] == "Hello, World"
    assert rows[1][2] == "Comma, Author"


def test_unsubmitted_attempts_are_omitted(conn, book, reader):
    quiz_id, _question_ids = make_quiz(conn, book.id, count=2)
    start_attempt(conn, quiz_id, reader.id)

    text = export_reading_log_csv(conn)

    assert parse(text) == [list(COLUMNS)]
