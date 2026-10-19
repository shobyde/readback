"""End-to-end test for the whole MVP path, with no network access.

One test drives the Flask test client from an empty database through every
gate: create a reader, import the example CSV, generate a draft quiz through a
monkeypatched generator, approve it as a parent, take it as the reader, and
check that points land exactly once and show up in the CSV export.
"""

from __future__ import annotations

import csv
import html
import io
from pathlib import Path

from readback.config import Settings
from readback.db import connect, init_db
from readback.models import GeneratedQuestion, GeneratedQuiz
from readback.web.app import create_app

EXAMPLES_CSV = Path(__file__).resolve().parents[1] / "examples" / "books.example.csv"
CHOICES = ["A farm", "A city", "A school", "A spaceship"]


def build_settings(tmp_path: Path) -> Settings:
    db_path = tmp_path / "e2e.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    return Settings(
        db_path=db_path,
        host="127.0.0.1",
        port=5057,
        llm_base_url="http://localhost:9/v1",
        llm_api_key="",
        llm_model="e2e-model",
        pass_threshold=0.8,
        parent_pin="",
        secret_key="e2e-secret-key",
    )


def fake_generated_quiz(num_questions: int = 4) -> GeneratedQuiz:
    return GeneratedQuiz(
        questions=[
            GeneratedQuestion(
                prompt=f"End to end question {index}?",
                choices=list(CHOICES),
                answer_index=2,
                explanation="Chosen for the test.",
            )
            for index in range(1, num_questions + 1)
        ]
    )


def fetch_one(conn, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchone()


def test_full_reading_cycle_from_empty_database(tmp_path, monkeypatch):
    settings = build_settings(tmp_path)
    app = create_app(settings)
    app.config.update(TESTING=True)
    client = app.test_client()

    # The empty database really is empty.
    conn = connect(settings.db_path)
    try:
        for table in ("readers", "books", "quizzes", "attempts", "points_ledger"):
            assert fetch_one(conn, f"SELECT COUNT(*) AS n FROM {table}")["n"] == 0
    finally:
        conn.close()

    # 1. A parent creates a reader.
    response = client.post(
        "/parent/reader", data={"name": "Ruth"}, follow_redirects=True
    )
    assert response.status_code == 200
    assert "Added reader Ruth" in response.get_data(as_text=True)

    conn = connect(settings.db_path)
    try:
        reader_id = fetch_one(conn, "SELECT id FROM readers WHERE name = 'Ruth'")["id"]
    finally:
        conn.close()

    # 2. The parent imports the example CSV through the upload form.
    csv_bytes = EXAMPLES_CSV.read_bytes()
    response = client.post(
        "/parent/books/import",
        data={"csv": (io.BytesIO(csv_bytes), "books.example.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Imported 12 new, updated 0, skipped 0." in text

    conn = connect(settings.db_path)
    try:
        book = fetch_one(
            conn,
            "SELECT id, title, points FROM books WHERE title = ?",
            ("Charlotte's Web",),
        )
        assert book is not None
        book_id = int(book["id"])
        assert float(book["points"]) == 5.0
    finally:
        conn.close()

    # 3. Generating a quiz never leaves the process: the HTTP call is patched.
    monkeypatch.setattr(
        "readback.web.app.generate_quiz",
        lambda book, settings, *, num_questions=5: fake_generated_quiz(
            num_questions
        ),
    )
    response = client.post(
        f"/parent/books/{book_id}/generate",
        data={"num_questions": "4"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Draft quiz" in response.get_data(as_text=True)

    conn = connect(settings.db_path)
    try:
        quiz = fetch_one(
            conn,
            "SELECT id, status FROM quizzes WHERE book_id = ? ORDER BY id DESC",
            (book_id,),
        )
        assert quiz["status"] == "draft"
        quiz_id = int(quiz["id"])
        questions = conn.execute(
            "SELECT id, answer_index FROM questions WHERE quiz_id = ? "
            "ORDER BY ordinal",
            (quiz_id,),
        ).fetchall()
        assert len(questions) == 4
    finally:
        conn.close()

    # A draft is invisible to the reader until it is approved.
    assert (
        client.get(f"/reader/{reader_id}/quiz/{quiz_id}").status_code == 404
    )
    review = client.get(f"/parent/quiz/{quiz_id}")
    assert review.status_code == 200
    assert "End to end question 1?" in review.get_data(as_text=True)

    # 4. The parent approves it.
    response = client.post(
        f"/parent/quiz/{quiz_id}/approve", follow_redirects=True
    )
    assert "Quiz approved" in response.get_data(as_text=True)

    conn = connect(settings.db_path)
    try:
        approved = fetch_one(
            conn, "SELECT status, approved_at FROM quizzes WHERE id = ?", (quiz_id,)
        )
        assert approved["status"] == "approved"
        assert approved["approved_at"]
    finally:
        conn.close()

    # 5. The reader sees the quiz and submits a passing set of answers.
    home = client.get(f"/reader/{reader_id}").get_data(as_text=True)
    assert "Charlotte's Web" in html.unescape(home)
    form = client.get(f"/reader/{reader_id}/quiz/{quiz_id}")
    assert form.status_code == 200
    form_text = form.get_data(as_text=True)
    assert "End to end question 1?" in form_text

    answers = {f"q_{int(row['id'])}": str(int(row["answer_index"])) for row in questions}
    assert len(answers) == 4
    submit = client.post(f"/reader/{reader_id}/quiz/{quiz_id}", data=answers)
    assert submit.status_code == 302
    result_url = submit.headers["Location"]
    assert result_url.startswith("/attempt/")

    result_text = client.get(result_url).get_data(as_text=True)
    assert "Passed" in result_text
    assert "100.0%" in result_text
    assert "4 of 4 questions correct" in result_text
    assert "5.00" in result_text

    conn = connect(settings.db_path)
    try:
        ledger = conn.execute(
            "SELECT reader_id, book_id, attempt_id, points FROM points_ledger"
        ).fetchall()
        assert len(ledger) == 1
        assert ledger[0]["reader_id"] == reader_id
        assert ledger[0]["book_id"] == book_id
        assert float(ledger[0]["points"]) == 5.0
        first_attempt_id = int(ledger[0]["attempt_id"])
        attempt = fetch_one(
            conn, "SELECT score, passed FROM attempts WHERE id = ?",
            (first_attempt_id,),
        )
        assert attempt["score"] == 1.0
        assert attempt["passed"] == 1
    finally:
        conn.close()

    # 6. A second passing attempt scores but does not award points again.
    second = client.post(f"/reader/{reader_id}/quiz/{quiz_id}", data=answers)
    second_text = client.get(second.headers["Location"]).get_data(as_text=True)
    assert "Points earned" in second_text
    assert "0.00" in second_text

    conn = connect(settings.db_path)
    try:
        assert (
            fetch_one(conn, "SELECT COUNT(*) AS n FROM points_ledger")["n"] == 1
        )
        assert fetch_one(conn, "SELECT COUNT(*) AS n FROM attempts")["n"] == 2
        total = fetch_one(
            conn,
            "SELECT COALESCE(SUM(points), 0.0) AS total FROM points_ledger "
            "WHERE reader_id = ?",
            (reader_id,),
        )["total"]
        assert float(total) == 5.0
    finally:
        conn.close()

    # 7. The export contains exactly that one passing row.
    export = client.get("/parent/export.csv")
    assert export.status_code == 200
    assert export.mimetype == "text/csv"
    rows = list(csv.DictReader(io.StringIO(export.get_data(as_text=True))))
    assert len(rows) == 2  # one row per submitted attempt
    passing = [row for row in rows if row["points_awarded"] == "5.0"]
    assert len(passing) == 1
    row = passing[0]
    assert row["reader"] == "Ruth"
    assert row["book"] == "Charlotte's Web"
    assert row["author"] == "E.B. White"
    assert row["points"] == "5.0"
    assert row["quiz_id"] == str(quiz_id)
    assert row["score"] == "1.0"
    assert row["passed"] == "1"
    assert row["attempted_at"]

    dashboard = client.get("/").get_data(as_text=True)
    assert "Ruth" in dashboard
    assert "5.00" in dashboard
    assert "Charlotte's Web" in html.unescape(dashboard)
