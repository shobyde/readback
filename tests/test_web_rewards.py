"""Web layer for rewards: goals, stickers, reports, AR import, manual reads."""

from __future__ import annotations

import json

import pytest

from readback.arbookfind import ArBook
from readback.config import Settings
from readback.db import connect, init_db
from readback.models import utc_now
from readback.web import app as app_module
from readback.web.app import create_app

CHOICES = ["One", "Two", "Three", "Four"]


def build_settings(tmp_path) -> Settings:
    db_path = tmp_path / "web-rewards.db"
    conn = connect(db_path)
    init_db(conn)
    conn.close()
    return Settings(
        db_path=db_path,
        host="127.0.0.1",
        port=5057,
        llm_base_url="http://localhost:9/v1",
        llm_api_key="",
        llm_model="test-model",
        pass_threshold=0.8,
        parent_pin="",
        secret_key="web-rewards-secret",
    )


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


def seed_reader(web_settings, name: str = "Maya") -> int:
    conn = connect(web_settings.db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO readers (name, created_at) VALUES (?, ?)",
            (name, utc_now()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def seed_book(web_settings, title: str = "Charlotte's Web", points: float = 5.0) -> int:
    conn = connect(web_settings.db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO books (title, author, points, created_at) VALUES (?, ?, ?, ?)",
            (title, "E.B. White", points, utc_now()),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def seed_approved_quiz(web_settings, book_id: int, questions: int = 2) -> int:
    conn = connect(web_settings.db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO quizzes (book_id, status, model, created_at, approved_at) "
            "VALUES (?, 'approved', 'test', ?, ?)",
            (book_id, utc_now(), utc_now()),
        )
        quiz_id = int(cursor.lastrowid)
        for ordinal in range(questions):
            conn.execute(
                "INSERT INTO questions (quiz_id, ordinal, prompt, choices_json, "
                "answer_index) VALUES (?, ?, ?, ?, 0)",
                (quiz_id, ordinal, f"Question {ordinal + 1}?", json.dumps(CHOICES)),
            )
        conn.commit()
        return quiz_id
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# goals
# --------------------------------------------------------------------------- #


def test_goals_page_lists_readers(client, web_settings):
    seed_reader(web_settings)
    body = client.get("/parent/goals").data.decode()
    assert "Reading goals" in body
    assert "Maya" in body


def test_setting_a_goal_shows_progress(client, web_settings):
    reader_id = seed_reader(web_settings)
    response = client.post(
        "/parent/goals",
        data={"reader_id": str(reader_id), "target": "100"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Goal set to 100 points." in response.data.decode()

    conn = connect(web_settings.db_path)
    try:
        assert conn.execute("SELECT target FROM goals").fetchone()["target"] == 100
    finally:
        conn.close()


def test_clearing_a_goal(client, web_settings):
    reader_id = seed_reader(web_settings)
    client.post("/parent/goals", data={"reader_id": str(reader_id), "target": "50"})
    response = client.post(
        "/parent/goals",
        data={"reader_id": str(reader_id), "target": ""},
        follow_redirects=True,
    )
    assert "Goal cleared." in response.data.decode()


def test_invalid_goal_flashes_an_error(client, web_settings):
    reader_id = seed_reader(web_settings)
    response = client.post(
        "/parent/goals",
        data={"reader_id": str(reader_id), "target": "-5"},
        follow_redirects=True,
    )
    assert "greater than zero" in response.data.decode()


# --------------------------------------------------------------------------- #
# stickers
# --------------------------------------------------------------------------- #


def test_assigning_a_sticker_from_the_books_page(client, web_settings):
    book_id = seed_book(web_settings)
    response = client.post(
        f"/parent/books/{book_id}/sticker",
        data={"sticker": "🦊", "label": "fox"},
        follow_redirects=True,
    )
    assert "Sticker updated." in response.data.decode()

    conn = connect(web_settings.db_path)
    try:
        row = conn.execute(
            "SELECT emoji, label FROM book_stickers WHERE book_id = ?", (book_id,)
        ).fetchone()
        assert (row["emoji"], row["label"]) == ("🦊", "fox")
    finally:
        conn.close()


def test_clearing_a_sticker_from_the_books_page(client, web_settings):
    book_id = seed_book(web_settings)
    client.post(f"/parent/books/{book_id}/sticker", data={"sticker": "🦊"})
    response = client.post(
        f"/parent/books/{book_id}/sticker",
        data={"sticker": ""},
        follow_redirects=True,
    )
    assert "Sticker cleared." in response.data.decode()


def test_reader_shelf_shows_the_sticker(client, web_settings):
    reader_id = seed_reader(web_settings)
    book_id = seed_book(web_settings)
    quiz_id = seed_approved_quiz(web_settings, book_id)
    conn = connect(web_settings.db_path)
    try:
        attempt = conn.execute(
            "INSERT INTO attempts (quiz_id, book_id, reader_id, kind, started_at, "
            "submitted_at, score, passed, total_questions, correct_answers) "
            "VALUES (?, ?, ?, 'quiz', ?, ?, 1.0, 1, 2, 2)",
            (quiz_id, book_id, reader_id, utc_now(), utc_now()),
        )
        conn.execute(
            "INSERT INTO points_ledger (reader_id, book_id, attempt_id, points, created_at) "
            "VALUES (?, ?, ?, 5.0, ?)",
            (reader_id, book_id, attempt.lastrowid, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()
    client.post(f"/parent/books/{book_id}/sticker", data={"sticker": "🐢"})
    assert "🐢" in client.get(f"/reader/{reader_id}").data.decode()


# --------------------------------------------------------------------------- #
# reports
# --------------------------------------------------------------------------- #


def test_reports_page_renders_weekly_bars_and_badges(client, web_settings):
    reader_id = seed_reader(web_settings)
    book_id = seed_book(web_settings)
    conn = connect(web_settings.db_path)
    try:
        attempt = conn.execute(
            "INSERT INTO attempts (quiz_id, book_id, reader_id, kind, started_at, "
            "submitted_at, score, passed, total_questions, correct_answers) "
            "VALUES (NULL, ?, ?, 'manual', ?, ?, NULL, 1, 0, 0)",
            (book_id, reader_id, utc_now(), utc_now()),
        )
        conn.execute(
            "INSERT INTO points_ledger (reader_id, book_id, attempt_id, points, created_at) "
            "VALUES (?, ?, ?, 5.0, ?)",
            (reader_id, book_id, attempt.lastrowid, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()

    body = client.get("/parent/reports").data.decode()
    assert "Points by week" in body
    assert "First finish" in body
    assert "Maya" in body


# --------------------------------------------------------------------------- #
# AR catalog
# --------------------------------------------------------------------------- #


def test_ar_search_renders_results(client, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "search_ar",
        lambda query: [
            ArBook(
                ar_id=2541,
                title="The Hunger Games",
                author="Suzanne Collins",
                quiz_no=123740,
                book_level=5.3,
                interest_level="MG+",
                points=15.0,
                summary="Katniss volunteers.",
            )
        ],
    )
    body = client.get("/parent/ar?q=hunger").data.decode()
    assert "The Hunger Games" in body
    assert "Quiz 123740" in body
    assert "Add to shelf" in body


def test_ar_search_surfaces_a_site_failure(client, monkeypatch):
    from readback.arbookfind import ArBookFindError

    def boom(query):
        raise ArBookFindError("AR BookFind is unreachable")

    monkeypatch.setattr(app_module, "search_ar", boom)
    response = client.get("/parent/ar?q=hunger")
    assert response.status_code == 200
    assert "AR BookFind is unreachable" in response.data.decode()


def test_adding_an_ar_book(client, web_settings, monkeypatch):
    monkeypatch.setattr(
        app_module,
        "fetch_ar",
        lambda ar_id: ArBook(
            ar_id=ar_id,
            title="The Hunger Games",
            author="Suzanne Collins",
            quiz_no=123740,
            book_level=5.3,
            interest_level="MG+",
            points=15.0,
        ),
    )
    response = client.post(
        "/parent/ar/add", data={"ar_id": "2541"}, follow_redirects=True
    )
    assert "Added The Hunger Games" in response.data.decode()

    conn = connect(web_settings.db_path)
    try:
        row = conn.execute("SELECT * FROM books").fetchone()
        assert row["quiz_no"] == 123740
        assert row["points"] == 15.0
        assert row["source"] == "arbookfind"
    finally:
        conn.close()

    # Adding a second time updates the same row instead of duplicating it.
    client.post("/parent/ar/add", data={"ar_id": "2541"})
    conn = connect(web_settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"] == 1
    finally:
        conn.close()


def test_adding_an_ar_book_without_a_selection(client):
    response = client.post("/parent/ar/add", data={"ar_id": ""}, follow_redirects=True)
    assert "Pick a book" in response.data.decode()


# --------------------------------------------------------------------------- #
# manual reads from the shelf
# --------------------------------------------------------------------------- #


def test_reader_logs_a_book_without_a_quiz(client, web_settings):
    reader_id = seed_reader(web_settings)
    book_id = seed_book(web_settings, points=7.5)

    response = client.post(
        f"/reader/{reader_id}/manual",
        data={"book_id": str(book_id)},
        follow_redirects=True,
    )
    assert "+7.5 points" in response.data.decode()

    summary = client.get(f"/reader/{reader_id}").data.decode()
    assert "Charlotte" in summary


def test_logging_the_same_book_twice_is_refused(client, web_settings):
    reader_id = seed_reader(web_settings)
    book_id = seed_book(web_settings)
    client.post(f"/reader/{reader_id}/manual", data={"book_id": str(book_id)})
    response = client.post(
        f"/reader/{reader_id}/manual",
        data={"book_id": str(book_id)},
        follow_redirects=True,
    )
    assert "already earned" in response.data.decode()


def test_manual_log_without_a_book_flashes(client, web_settings):
    reader_id = seed_reader(web_settings)
    response = client.post(
        f"/reader/{reader_id}/manual", data={}, follow_redirects=True
    )
    assert "Pick a book" in response.data.decode()


def test_dashboard_labels_a_manual_read_as_logged(client, web_settings):
    reader_id = seed_reader(web_settings)
    book_id = seed_book(web_settings, points=4.0)
    client.post(f"/reader/{reader_id}/manual", data={"book_id": str(book_id)})

    dashboard = client.get("/").data.decode()
    assert "logged without a quiz" in dashboard
    assert "Logged" in dashboard

    shelf = client.get(f"/reader/{reader_id}").data.decode()
    assert "0</strong> quizzes taken" in shelf


# --------------------------------------------------------------------------- #
# result page + removal
# --------------------------------------------------------------------------- #


def test_result_page_shows_stars_for_a_perfect_attempt(client, web_settings):
    reader_id = seed_reader(web_settings)
    book_id = seed_book(web_settings)
    quiz_id = seed_approved_quiz(web_settings, book_id, questions=2)

    answers = {}
    conn = connect(web_settings.db_path)
    try:
        from readback.points import start_attempt, submit_attempt

        attempt_id = start_attempt(conn, quiz_id, reader_id)
        for row in conn.execute(
            "SELECT id FROM questions WHERE quiz_id = ?", (quiz_id,)
        ).fetchall():
            answers[row["id"]] = 0
        submit_attempt(conn, attempt_id, answers, pass_threshold=0.8)
    finally:
        conn.close()

    body = client.get(f"/attempt/{attempt_id}").data.decode()
    assert "★★★" in body


def test_removing_a_book_requires_confirmation(client, web_settings):
    book_id = seed_book(web_settings)
    client.post(f"/parent/books/{book_id}/delete", data={})
    conn = connect(web_settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"] == 1
    finally:
        conn.close()


def test_removing_a_book_cascades_its_history(client, web_settings):
    reader_id = seed_reader(web_settings)
    book_id = seed_book(web_settings)
    quiz_id = seed_approved_quiz(web_settings, book_id)
    conn = connect(web_settings.db_path)
    try:
        attempt = conn.execute(
            "INSERT INTO attempts (quiz_id, book_id, reader_id, kind, started_at, "
            "submitted_at, score, passed, total_questions, correct_answers) "
            "VALUES (?, ?, ?, 'quiz', ?, ?, 1.0, 1, 2, 2)",
            (quiz_id, book_id, reader_id, utc_now(), utc_now()),
        )
        conn.execute(
            "INSERT INTO points_ledger (reader_id, book_id, attempt_id, points, created_at) "
            "VALUES (?, ?, ?, 5.0, ?)",
            (reader_id, book_id, attempt.lastrowid, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()

    response = client.post(
        f"/parent/books/{book_id}/delete",
        data={"confirm": "yes"},
        follow_redirects=True,
    )
    assert "Removed" in response.data.decode()

    conn = connect(web_settings.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM quizzes").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM points_ledger").fetchone()["n"] == 0
    finally:
        conn.close()
