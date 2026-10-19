import sqlite3

import pytest

from readback.db import SCHEMA, TABLES, connect, init_db, transaction


def _table_names(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows} - {"sqlite_sequence"}


def _seed_book(conn, title="Dune", author="Frank Herbert"):
    cursor = conn.execute(
        "INSERT INTO books (title, author, created_at) VALUES (?, ?, ?)",
        (title, author, "2026-01-01T00:00:00+00:00"),
    )
    return cursor.lastrowid


def _seed_quiz(conn, book_id, status="draft"):
    cursor = conn.execute(
        "INSERT INTO quizzes (book_id, status, created_at) VALUES (?, ?, ?)",
        (book_id, status, "2026-01-01T00:00:00+00:00"),
    )
    return cursor.lastrowid


def _seed_question(conn, quiz_id, ordinal=0, answer_index=0):
    cursor = conn.execute(
        "INSERT INTO questions (quiz_id, ordinal, prompt, choices_json, answer_index) "
        "VALUES (?, ?, ?, ?, ?)",
        (quiz_id, ordinal, "Who wrote it?", '["a", "b", "c", "d"]', answer_index),
    )
    return cursor.lastrowid


def _seed_reader(conn, name="Ada"):
    cursor = conn.execute(
        "INSERT INTO readers (name, created_at) VALUES (?, ?)",
        (name, "2026-01-01T00:00:00+00:00"),
    )
    return cursor.lastrowid


def _seed_attempt(conn, quiz_id, reader_id, *, submitted=True):
    cursor = conn.execute(
        "INSERT INTO attempts (quiz_id, reader_id, started_at, submitted_at, "
        "score, passed, total_questions, correct_answers) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            quiz_id,
            reader_id,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:10:00+00:00" if submitted else None,
            1.0 if submitted else None,
            1 if submitted else None,
            1,
            1,
        ),
    )
    return cursor.lastrowid


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _graph(conn):
    """Book + quiz + question + reader + attempt + answer + ledger row."""
    book_id = _seed_book(conn)
    quiz_id = _seed_quiz(conn, book_id, "approved")
    question_id = _seed_question(conn, quiz_id)
    reader_id = _seed_reader(conn)
    attempt_id = _seed_attempt(conn, quiz_id, reader_id)
    conn.execute(
        "INSERT INTO attempt_answers (attempt_id, question_id, choice_index, correct) "
        "VALUES (?, ?, ?, ?)",
        (attempt_id, question_id, 0, 1),
    )
    conn.execute(
        "INSERT INTO points_ledger (reader_id, book_id, attempt_id, points, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (reader_id, book_id, attempt_id, 5.0, "2026-01-01T00:10:00+00:00"),
    )
    return {
        "book_id": book_id,
        "quiz_id": quiz_id,
        "question_id": question_id,
        "reader_id": reader_id,
        "attempt_id": attempt_id,
    }


def test_init_db_creates_every_table(conn):
    names = _table_names(conn)
    assert set(TABLES) <= names


def test_schema_string_has_no_placeholder_text():
    assert "CREATE TABLE IF NOT EXISTS" in SCHEMA
    assert "PRAGMA foreign_keys = ON" in SCHEMA


def test_init_db_is_idempotent(conn):
    init_db(conn)
    init_db(conn)
    assert set(TABLES) <= _table_names(conn)


def test_init_db_keeps_existing_rows(conn):
    with transaction(conn):
        _seed_reader(conn)
    init_db(conn)
    assert _count(conn, "readers") == 1


def test_connect_sets_row_factory(tmp_path):
    connection = connect(tmp_path / "row.db")
    try:
        assert connection.row_factory is sqlite3.Row
        init_db(connection)
        row = connection.execute("SELECT 1 AS one").fetchone()
        assert row["one"] == 1
    finally:
        connection.close()


def test_connect_enables_foreign_keys(tmp_path):
    connection = connect(tmp_path / "fk.db")
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_foreign_key_violation_is_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO quizzes (book_id, created_at) VALUES (?, ?)",
            (9999, "2026-01-01T00:00:00+00:00"),
        )


def test_transaction_commits_on_success(tmp_path):
    path = tmp_path / "commit.db"
    connection = connect(path)
    init_db(connection)
    other = connect(path)
    try:
        with transaction(connection):
            _seed_reader(connection)
        assert other.execute("SELECT COUNT(*) AS n FROM readers").fetchone()["n"] == 1
    finally:
        other.close()
        connection.close()


def test_transaction_rolls_back_on_exception(tmp_path):
    path = tmp_path / "rollback.db"
    connection = connect(path)
    init_db(connection)
    other = connect(path)
    try:
        with pytest.raises(RuntimeError):
            with transaction(connection):
                _seed_reader(connection)
                raise RuntimeError("boom")
        assert other.execute("SELECT COUNT(*) AS n FROM readers").fetchone()["n"] == 0
    finally:
        other.close()
        connection.close()


def test_transaction_yields_the_connection(conn):
    with transaction(conn) as active:
        assert active is conn


def test_deleting_book_cascades(conn):
    graph = _graph(conn)
    assert _count(conn, "quizzes") == 1

    with transaction(conn):
        conn.execute("DELETE FROM books WHERE id = ?", (graph["book_id"],))

    assert _count(conn, "books") == 0
    assert _count(conn, "quizzes") == 0
    assert _count(conn, "questions") == 0
    assert _count(conn, "attempts") == 0
    assert _count(conn, "attempt_answers") == 0
    assert _count(conn, "points_ledger") == 0
    assert _count(conn, "readers") == 1


def test_deleting_reader_cascades(conn):
    graph = _graph(conn)

    with transaction(conn):
        conn.execute("DELETE FROM readers WHERE id = ?", (graph["reader_id"],))

    assert _count(conn, "readers") == 0
    assert _count(conn, "attempts") == 0
    assert _count(conn, "attempt_answers") == 0
    assert _count(conn, "points_ledger") == 0
    assert _count(conn, "books") == 1
    assert _count(conn, "quizzes") == 1


def test_answer_index_check_rejects_four(conn):
    book_id = _seed_book(conn)
    quiz_id = _seed_quiz(conn, book_id)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_question(conn, quiz_id, answer_index=4)


def test_answer_index_check_rejects_negative(conn):
    book_id = _seed_book(conn)
    quiz_id = _seed_quiz(conn, book_id)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_question(conn, quiz_id, answer_index=-1)


def test_answer_index_accepts_zero_through_three(conn):
    book_id = _seed_book(conn)
    quiz_id = _seed_quiz(conn, book_id)
    for ordinal, answer_index in enumerate(range(4)):
        _seed_question(conn, quiz_id, ordinal=ordinal, answer_index=answer_index)
    assert _count(conn, "questions") == 4


def test_negative_points_are_rejected(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO books (title, points, created_at) VALUES (?, ?, ?)",
            ("Bad", -1.0, "2026-01-01T00:00:00+00:00"),
        )


def test_quiz_status_check_rejects_unknown_status(conn):
    book_id = _seed_book(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_quiz(conn, book_id, status="published")


def test_duplicate_ordinal_in_quiz_is_rejected(conn):
    book_id = _seed_book(conn)
    quiz_id = _seed_quiz(conn, book_id)
    _seed_question(conn, quiz_id, ordinal=0)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_question(conn, quiz_id, ordinal=0)


def test_reader_names_are_unique(conn):
    _seed_reader(conn, "Ada")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_reader(conn, "Ada")


def test_book_title_and_author_unique_case_insensitively(conn):
    _seed_book(conn, "Dune", "Frank Herbert")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_book(conn, "dune", "frank herbert")


def test_empty_isbn_allows_multiple_rows(conn):
    _seed_book(conn, "One", "Author")
    _seed_book(conn, "Two", "Author")
    # NULL and empty ISBNs must not collide on the partial unique index.
    conn.execute(
        "INSERT INTO books (title, author, isbn, created_at) VALUES (?, ?, ?, ?)",
        ("Three", "Author", "", "2026-01-01T00:00:00+00:00"),
    )
    assert _count(conn, "books") == 3


def test_duplicate_isbn_is_rejected(conn):
    conn.execute(
        "INSERT INTO books (title, author, isbn, created_at) VALUES (?, ?, ?, ?)",
        ("One", "Author", "9780547928227", "2026-01-01T00:00:00+00:00"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO books (title, author, isbn, created_at) VALUES (?, ?, ?, ?)",
            ("Two", "Author", "9780547928227", "2026-01-01T00:00:00+00:00"),
        )


def test_points_ledger_is_unique_per_reader_and_book(conn):
    graph = _graph(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO points_ledger (reader_id, book_id, attempt_id, points, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                graph["reader_id"],
                graph["book_id"],
                graph["attempt_id"],
                5.0,
                "2026-01-02T00:00:00+00:00",
            ),
        )
