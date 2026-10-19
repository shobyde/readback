"""Web layer tests: Flask test client against a temporary database.

The model endpoint is never reached.  Draft generation is either patched at the
web seam (``readback.web.app.generate_quiz``) or the draft rows are seeded
directly so the reader flow can be tested without the generator.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from readback import books as books_module
from readback.config import Settings
from readback.db import connect, init_db, transaction
from readback.models import GeneratedQuestion, GeneratedQuiz, ImportResult, utc_now
from readback.quiz.generate import QuizGenerationError
from readback.web.app import create_app

CHOICES = ["One", "Two", "Three", "Four"]


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


def build_settings(tmp_path, **overrides) -> Settings:
    db_path = tmp_path / "web.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    values = dict(
        db_path=db_path,
        host="127.0.0.1",
        port=5057,
        llm_base_url="http://localhost:9/v1",
        llm_api_key="",
        llm_model="test-model",
        pass_threshold=0.8,
        parent_pin="",
        secret_key="web-test-secret",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def web_settings(tmp_path) -> Settings:
    return build_settings(tmp_path)


@pytest.fixture()
def app(web_settings):
    application = create_app(web_settings)
    application.config.update(TESTING=True)
    return application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def db(app):
    connection = connect(app.config["READBACK_SETTINGS"].db_path)
    yield connection
    connection.close()


def add_reader(conn, name: str = "Ada") -> int:
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO readers (name, created_at) VALUES (?, ?)",
            (name, utc_now()),
        )
    return int(cursor.lastrowid)


def add_book(
    conn,
    title: str = "The Hobbit",
    author: str = "J. R. R. Tolkien",
    points: float = 5.0,
) -> int:
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO books (title, author, isbn, points, grade_level, notes, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (title, author, None, points, "6-8", "", utc_now()),
        )
    return int(cursor.lastrowid)


def add_quiz(conn, book_id: int, questions=None, status: str = "approved") -> int:
    if questions is None:
        questions = [
            ("Question 1?", list(CHOICES), 1, "Because one."),
            ("Question 2?", list(CHOICES), 2, "Because two."),
            ("Question 3?", list(CHOICES), 3, "Because three."),
            ("Question 4?", list(CHOICES), 0, "Because four."),
        ]
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO quizzes (book_id, status, model, source_notes, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, status, "test-model", "", utc_now()),
        )
        quiz_id = int(cursor.lastrowid)
        for ordinal, (prompt, choices, answer, explanation) in enumerate(
            questions, start=1
        ):
            conn.execute(
                "INSERT INTO questions (quiz_id, ordinal, prompt, choices_json, "
                "answer_index, explanation) VALUES (?, ?, ?, ?, ?, ?)",
                (quiz_id, ordinal, prompt, json.dumps(choices), answer, explanation),
            )
    return quiz_id


def question_rows(conn, quiz_id: int):
    return conn.execute(
        "SELECT * FROM questions WHERE quiz_id = ? ORDER BY ordinal", (quiz_id,)
    ).fetchall()


def fake_quiz(num_questions: int = 5) -> GeneratedQuiz:
    return GeneratedQuiz(
        questions=[
            GeneratedQuestion(
                prompt=f"Generated question {index}?",
                choices=list(CHOICES),
                answer_index=1,
                explanation="The text says so.",
            )
            for index in range(1, num_questions + 1)
        ]
    )


# --------------------------------------------------------------------------- #
# health and dashboard
# --------------------------------------------------------------------------- #


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_dashboard_renders_empty_state(client):
    response = client.get("/")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Readers" in text
    assert "No readers yet" in text
    assert "Parent area" in text
    assert "style.css" in text


def test_create_reader_then_dashboard_and_parent_show_them(client, db):
    response = client.post(
        "/parent/reader", data={"name": "Ada"}, follow_redirects=True
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Added reader Ada" in text
    assert "Ada" in text

    dashboard = client.get("/").get_data(as_text=True)
    assert "Ada" in dashboard

    reader = db.execute("SELECT name FROM readers").fetchone()
    assert reader["name"] == "Ada"


def test_create_reader_rejects_blank_and_duplicate_names(client, db):
    client.post("/parent/reader", data={"name": "   "}, follow_redirects=True)
    assert db.execute("SELECT COUNT(*) AS n FROM readers").fetchone()["n"] == 0

    client.post("/parent/reader", data={"name": "Ada"}, follow_redirects=True)
    response = client.post(
        "/parent/reader", data={"name": "Ada"}, follow_redirects=True
    )
    assert "already exists" in response.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) AS n FROM readers").fetchone()["n"] == 1


def test_dashboard_lists_points_and_recent_attempts(client, db):
    reader_id = add_reader(db)
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id)
    client.post(
        f"/reader/{reader_id}/quiz/{quiz_id}",
        data={"q_1": "1", "q_2": "2", "q_3": "3", "q_4": "0"},
    )
    text = client.get("/").get_data(as_text=True)
    assert "5 pts" in text
    assert "The Hobbit" in text
    assert "Passed" in text


# --------------------------------------------------------------------------- #
# book import
# --------------------------------------------------------------------------- #


def test_import_csv_upload_flashes_counts(client, db):
    body = (
        "title,author,isbn,points,grade level,notes\n"
        "The Hobbit,J. R. R. Tolkien,9780547928227,5,6-8,\n"
    )
    response = client.post(
        "/parent/books/import",
        data={"csv": (io.BytesIO(body.encode("utf-8")), "books.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Imported 1 new, updated 0, skipped 0." in text
    assert "The Hobbit" in text
    assert db.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"] == 1


def test_import_requires_a_csv_file(client):
    missing = client.post(
        "/parent/books/import",
        data={},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Choose a CSV file" in missing.get_data(as_text=True)

    wrong_type = client.post(
        "/parent/books/import",
        data={"csv": (io.BytesIO(b"title\n"), "books.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "must be a .csv file" in wrong_type.get_data(as_text=True)


def test_import_uses_a_temp_file_and_removes_it(client, monkeypatch):
    seen = {}

    def fake_import(conn, path):
        seen["path"] = str(path)
        seen["existed"] = Path(path).exists()
        return ImportResult(inserted=2)

    monkeypatch.setattr(books_module, "import_books", fake_import)
    response = client.post(
        "/parent/books/import",
        data={"csv": (io.BytesIO(b"title\nHoles\n"), "books.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "Imported 2 new" in response.get_data(as_text=True)
    assert seen["existed"] is True
    assert not Path(seen["path"]).exists()


# --------------------------------------------------------------------------- #
# draft generation
# --------------------------------------------------------------------------- #


def test_generate_draft_with_mocked_generator(client, db, monkeypatch):
    book_id = add_book(db)
    monkeypatch.setattr(
        "readback.web.app.generate_quiz",
        lambda book, settings, *, num_questions=5: fake_quiz(num_questions),
    )
    response = client.post(
        f"/parent/books/{book_id}/generate",
        data={"num_questions": "3"},
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Draft quiz" in text
    assert "Review" in text

    quiz = db.execute("SELECT * FROM quizzes WHERE book_id = ?", (book_id,)).fetchone()
    assert quiz["status"] == "draft"
    assert len(question_rows(db, quiz["id"])) == 3

    queue = client.get("/parent").get_data(as_text=True)
    assert "Drafts to review" in queue or "drafts to review" in queue
    assert "The Hobbit" in queue


def test_generate_flashes_quiz_generation_error(client, db, monkeypatch):
    book_id = add_book(db)

    def boom(book, settings, *, num_questions=5):
        raise QuizGenerationError("the endpoint said no")

    monkeypatch.setattr("readback.web.app.generate_quiz", boom)
    response = client.post(
        f"/parent/books/{book_id}/generate", follow_redirects=True
    )
    assert response.status_code == 200
    assert "the endpoint said no" in response.get_data(as_text=True)
    assert db.execute("SELECT COUNT(*) AS n FROM quizzes").fetchone()["n"] == 0


def test_generate_survives_unexpected_errors(client, db, monkeypatch):
    book_id = add_book(db)

    def boom(book, settings, *, num_questions=5):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr("readback.web.app.generate_quiz", boom)
    response = client.post(
        f"/parent/books/{book_id}/generate", follow_redirects=True
    )
    assert response.status_code == 200
    assert "RuntimeError" in response.get_data(as_text=True)
    assert "socket exploded" in response.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# review gate
# --------------------------------------------------------------------------- #


def test_review_page_shows_editable_question_fields(client, db):
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id, status="draft")
    response = client.get(f"/parent/quiz/{quiz_id}")
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Question 1?" in text
    assert 'name="prompt"' in text
    assert 'name="choice_0"' in text
    assert 'name="choice_3"' in text
    assert 'name="answer_index"' in text
    assert 'name="explanation"' in text
    assert f'action="/parent/quiz/{quiz_id}/approve"' in text
    assert f'action="/parent/quiz/{quiz_id}/reject"' in text


def test_edit_question_updates_the_draft(client, db):
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id, status="draft")
    question_id = question_rows(db, quiz_id)[0]["id"]

    response = client.post(
        f"/parent/question/{question_id}/edit",
        data={
            "prompt": "Who is Bilbo?",
            "choice_0": "A hobbit",
            "choice_1": "A wizard",
            "choice_2": "A dwarf",
            "choice_3": "An elf",
            "answer_index": "0",
            "explanation": "Bilbo is a hobbit.",
        },
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert "Question saved." in text
    assert "Who is Bilbo?" in text

    row = db.execute(
        "SELECT * FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    assert row["prompt"] == "Who is Bilbo?"
    assert json.loads(row["choices_json"]) == [
        "A hobbit",
        "A wizard",
        "A dwarf",
        "An elf",
    ]
    assert row["answer_index"] == 0


def test_edit_question_with_bad_answer_index_is_refused(client, db):
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id, status="draft")
    question_id = question_rows(db, quiz_id)[0]["id"]

    response = client.post(
        f"/parent/question/{question_id}/edit",
        data={
            "prompt": "Still the prompt?",
            "choice_0": "One",
            "choice_1": "Two",
            "choice_2": "Three",
            "choice_3": "Four",
            "answer_index": "9",
            "explanation": "",
        },
        follow_redirects=True,
    )
    assert "between 0 and 3" in response.get_data(as_text=True)
    row = db.execute(
        "SELECT answer_index, prompt FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    assert row["answer_index"] == 1
    assert row["prompt"] == "Question 1?"


def test_delete_question_renumbers_and_unblocks_nothing(client, db):
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id, status="draft")
    rows = question_rows(db, quiz_id)

    response = client.post(
        f"/parent/question/{rows[0]['id']}/delete", follow_redirects=True
    )
    assert "Question deleted." in response.get_data(as_text=True)

    remaining = question_rows(db, quiz_id)
    assert [row["ordinal"] for row in remaining] == [1, 2, 3]
    assert remaining[0]["id"] == rows[1]["id"]


def test_approve_refusal_shows_validation_problems(client, db):
    book_id = add_book(db)
    quiz_id = add_quiz(
        db,
        book_id,
        questions=[("Only question?", list(CHOICES), 0, "")],
        status="draft",
    )
    question_id = question_rows(db, quiz_id)[0]["id"]
    client.post(f"/parent/question/{question_id}/delete")

    review = client.get(f"/parent/quiz/{quiz_id}").get_data(as_text=True)
    assert "Not ready to approve" in review
    assert "quiz has no questions" in review

    response = client.post(
        f"/parent/quiz/{quiz_id}/approve", follow_redirects=True
    )
    text = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "quiz has no questions" in text
    assert (
        db.execute("SELECT status FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()[
            "status"
        ]
        == "draft"
    )


def test_approve_and_reject_forms(client, db):
    book_id = add_book(db)
    approve_id = add_quiz(db, book_id, status="draft")
    reject_id = add_quiz(
        db, book_id, status="draft", questions=[("Another?", list(CHOICES), 0, "")]
    )

    response = client.post(
        f"/parent/quiz/{approve_id}/approve", follow_redirects=True
    )
    assert "Quiz approved" in response.get_data(as_text=True)
    assert (
        db.execute(
            "SELECT status FROM quizzes WHERE id = ?", (approve_id,)
        ).fetchone()["status"]
        == "approved"
    )

    response = client.post(
        f"/parent/quiz/{reject_id}/reject",
        data={"note": "Too easy"},
        follow_redirects=True,
    )
    assert "Quiz rejected" in response.get_data(as_text=True)
    row = db.execute("SELECT * FROM quizzes WHERE id = ?", (reject_id,)).fetchone()
    assert row["status"] == "rejected"
    assert "Too easy" in row["source_notes"]


# --------------------------------------------------------------------------- #
# reader flow
# --------------------------------------------------------------------------- #


def test_reader_home_lists_only_approved_quizzes(client, db):
    reader_id = add_reader(db)
    approved_book = add_book(db, title="The Hobbit")
    draft_book = add_book(db, title="Draft Book", author="Someone")
    approved_id = add_quiz(db, approved_book)
    draft_id = add_quiz(db, draft_book, status="draft")

    text = client.get(f"/reader/{reader_id}").get_data(as_text=True)
    assert "The Hobbit" in text
    assert "Draft Book" not in text

    assert client.get(f"/reader/{reader_id}/quiz/{draft_id}").status_code == 404
    assert (
        client.post(f"/reader/{reader_id}/quiz/{draft_id}", data={}).status_code
        == 404
    )
    assert client.get(f"/reader/{reader_id}/quiz/{approved_id}").status_code == 200


def test_quiz_form_shows_choices_without_answers(client, db):
    reader_id = add_reader(db)
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id)
    text = client.get(
        f"/reader/{reader_id}/quiz/{quiz_id}"
    ).get_data(as_text=True)
    assert "Question 1?" in text
    assert 'name="q_1"' in text
    assert "Because one." not in text


def test_passing_attempt_scores_and_awards_points(client, db):
    reader_id = add_reader(db)
    book_id = add_book(db, points=5.0)
    quiz_id = add_quiz(db, book_id)

    response = client.post(
        f"/reader/{reader_id}/quiz/{quiz_id}",
        data={"q_1": "1", "q_2": "2", "q_3": "3", "q_4": "0"},
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("/attempt/")

    text = client.get(location).get_data(as_text=True)
    assert "Passed" in text
    assert "100.0%" in text
    assert "4 of 4 questions correct" in text
    assert "5.00" in text

    attempt = db.execute("SELECT * FROM attempts").fetchone()
    assert attempt["score"] == 1.0
    assert attempt["passed"] == 1
    ledger = db.execute("SELECT * FROM points_ledger").fetchall()
    assert len(ledger) == 1
    assert ledger[0]["points"] == 5.0


def test_second_pass_does_not_award_points_twice(client, db):
    reader_id = add_reader(db)
    book_id = add_book(db, points=5.0)
    quiz_id = add_quiz(db, book_id)
    answers = {"q_1": "1", "q_2": "2", "q_3": "3", "q_4": "0"}

    first = client.post(
        f"/reader/{reader_id}/quiz/{quiz_id}", data=answers
    )
    second = client.post(
        f"/reader/{reader_id}/quiz/{quiz_id}", data=answers
    )
    text = client.get(second.headers["Location"]).get_data(as_text=True)
    assert "Points earned" in text
    assert "0.00" in text
    assert db.execute("SELECT COUNT(*) AS n FROM points_ledger").fetchone()["n"] == 1
    assert db.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 2
    # The first attempt is still linked to the ledger row.
    assert first.status_code == 302


def test_failing_attempt_awards_nothing(client, db):
    reader_id = add_reader(db)
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id)

    response = client.post(
        f"/reader/{reader_id}/quiz/{quiz_id}",
        data={"q_1": "0", "q_2": "0", "q_3": "0", "q_4": "0"},
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert "Try again" in text
    assert "25.0%" in text
    assert db.execute("SELECT COUNT(*) AS n FROM points_ledger").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 1


def test_missing_answers_count_as_wrong(client, db):
    reader_id = add_reader(db)
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id)

    response = client.post(
        f"/reader/{reader_id}/quiz/{quiz_id}",
        data={"q_1": "1", "q_2": "2", "q_3": "3"},
        follow_redirects=True,
    )
    text = response.get_data(as_text=True)
    assert "75.0%" in text
    assert "Try again" in text


def test_unknown_reader_and_quiz_return_404(client):
    assert client.get("/reader/999").status_code == 404
    assert client.get("/attempt/999").status_code == 404
    assert client.get("/parent/quiz/999").status_code == 404


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #


def test_export_csv_response(client, db):
    reader_id = add_reader(db)
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id)
    client.post(
        f"/reader/{reader_id}/quiz/{quiz_id}",
        data={"q_1": "1", "q_2": "2", "q_3": "3", "q_4": "0"},
    )

    response = client.get("/parent/export.csv")
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert "reading-log.csv" in response.headers["Content-Disposition"]

    text = response.get_data(as_text=True)
    lines = text.splitlines()
    assert lines[0] == (
        "reader,book,author,points,quiz_id,attempted_at,score,passed,points_awarded"
    )
    assert "Ada,The Hobbit" in text
    assert "5.0" in lines[1]
    assert lines[1].endswith(",1.0,1,5.0")


def test_export_can_filter_by_reader(client, db):
    ada = add_reader(db, "Ada")
    grace = add_reader(db, "Grace")
    book_id = add_book(db)
    quiz_id = add_quiz(db, book_id)
    client.post(
        f"/reader/{ada}/quiz/{quiz_id}",
        data={"q_1": "1", "q_2": "2", "q_3": "3", "q_4": "0"},
    )
    text = client.get(f"/parent/export.csv?reader_id={ada}").get_data(as_text=True)
    assert "Ada" in text
    assert "Grace" not in text
    # Only the header when a reader has no attempts.
    empty = client.get(f"/parent/export.csv?reader_id={grace}").get_data(
        as_text=True
    )
    assert len(empty.splitlines()) == 1


# --------------------------------------------------------------------------- #
# parent PIN gate
# --------------------------------------------------------------------------- #


def test_parent_area_is_open_without_a_pin(client):
    assert client.get("/parent").status_code == 200
    assert client.get("/parent/books").status_code == 200
    assert client.get("/parent/export.csv").status_code == 200


def test_pin_gate_redirects_then_unlocks(tmp_path):
    settings = build_settings(tmp_path, parent_pin="2468")
    app = create_app(settings)
    app.config.update(TESTING=True)
    client = app.test_client()

    for path in ("/parent", "/parent/books", "/parent/export.csv"):
        response = client.get(path)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/parent/unlock")

    form = client.get("/parent/unlock")
    assert form.status_code == 200
    assert 'name="pin"' in form.get_data(as_text=True)

    wrong = client.post("/parent/unlock", data={"pin": "1111"})
    assert wrong.status_code == 401
    assert "not correct" in wrong.get_data(as_text=True)

    unlocked = client.post("/parent/unlock", data={"pin": "2468"})
    assert unlocked.status_code == 302
    assert unlocked.headers["Location"].endswith("/parent")

    assert client.get("/parent").status_code == 200
    assert client.get("/parent/books").status_code == 200
    # The readers' side is never gated.
    assert client.get("/").status_code == 200
    assert client.get("/healthz").status_code == 200


def test_parent_posts_are_gated_too(tmp_path):
    settings = build_settings(tmp_path, parent_pin="2468")
    app = create_app(settings)
    app.config.update(TESTING=True)
    client = app.test_client()

    response = client.post("/parent/reader", data={"name": "Ada"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/parent/unlock")


# --------------------------------------------------------------------------- #
# static assets and templates
# --------------------------------------------------------------------------- #


def test_stylesheet_is_served_from_the_package(client):
    response = client.get("/static/style.css")
    assert response.status_code == 200
    text = response.get_data(as_text=True)
    assert "--paper" in text
    assert "system-ui" in text
    assert "linear-gradient" not in text.lower()
    assert "http://" not in text
    assert "https://" not in text
