"""Upgrading a v0.1.0 database in place.

A home server upgrade must not lose a family's reading log: the migration that
relaxes ``attempts.quiz_id`` recreates the table, so this test proves the child
rows (answers and ledger) survive it.
"""

from __future__ import annotations

import sqlite3

from readback.db import connect, init_db

V1_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE readers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE TABLE books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  isbn TEXT,
  points REAL NOT NULL DEFAULT 0.0 CHECK (points >= 0),
  grade_level TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_books_isbn ON books(isbn) WHERE isbn IS NOT NULL AND isbn != '';
CREATE UNIQUE INDEX idx_books_title_author ON books(lower(title), lower(author));
CREATE TABLE quizzes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','approved','rejected','archived')),
  model TEXT NOT NULL DEFAULT '',
  source_notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  approved_at TEXT,
  approved_by TEXT
);
CREATE TABLE questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  choices_json TEXT NOT NULL,
  answer_index INTEGER NOT NULL CHECK (answer_index BETWEEN 0 AND 3),
  explanation TEXT NOT NULL DEFAULT '',
  UNIQUE (quiz_id, ordinal)
);
CREATE TABLE attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  reader_id INTEGER NOT NULL REFERENCES readers(id) ON DELETE CASCADE,
  started_at TEXT NOT NULL,
  submitted_at TEXT,
  score REAL,
  passed INTEGER,
  total_questions INTEGER NOT NULL DEFAULT 0,
  correct_answers INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_attempts_reader ON attempts(reader_id, submitted_at);
CREATE TABLE attempt_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  choice_index INTEGER NOT NULL,
  correct INTEGER NOT NULL,
  UNIQUE (attempt_id, question_id)
);
CREATE TABLE points_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reader_id INTEGER NOT NULL REFERENCES readers(id) ON DELETE CASCADE,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  points REAL NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (reader_id, book_id)
);
"""


def seed_v1(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    conn.execute(
        "INSERT INTO readers (name, created_at) VALUES ('Maya', '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO books (title, author, points, created_at) "
        "VALUES ('The Hobbit', 'Tolkien', 5.0, '2026-01-01T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO quizzes (book_id, status, created_at) "
        "VALUES (1, 'approved', '2026-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO questions (quiz_id, ordinal, prompt, choices_json, answer_index) "
        "VALUES (1, 0, 'Who?', '[\"a\",\"b\",\"c\",\"d\"]', 0)"
    )
    conn.execute(
        "INSERT INTO attempts (quiz_id, reader_id, started_at, submitted_at, score, "
        "passed, total_questions, correct_answers) VALUES (1, 1, "
        "'2026-01-03T00:00:00+00:00', '2026-01-03T00:00:00+00:00', 1.0, 1, 1, 1)"
    )
    conn.execute(
        "INSERT INTO attempt_answers (attempt_id, question_id, choice_index, correct) "
        "VALUES (1, 1, 0, 1)"
    )
    conn.execute(
        "INSERT INTO points_ledger (reader_id, book_id, attempt_id, points, created_at) "
        "VALUES (1, 1, 1, 5.0, '2026-01-03T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_v1_database_upgrades_without_losing_the_log(tmp_path):
    path = tmp_path / "v1.db"
    seed_v1(path)

    conn = connect(path)
    try:
        init_db(conn)
        init_db(conn)  # idempotent

        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        assert {"goals", "book_stickers"} <= tables

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(books)")}
        assert {"quiz_no", "book_level", "interest_level", "summary", "source"} <= columns

        attempt_columns = {
            row["name"]: row for row in conn.execute("PRAGMA table_info(attempts)")
        }
        assert "kind" in attempt_columns
        assert "book_id" in attempt_columns
        assert attempt_columns["quiz_id"]["notnull"] == 0

        attempt = conn.execute("SELECT * FROM attempts").fetchone()
        assert attempt["kind"] == "quiz"
        assert attempt["book_id"] == 1
        assert attempt["correct_answers"] == 1

        assert conn.execute("SELECT COUNT(*) AS n FROM attempt_answers").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM points_ledger").fetchone()["n"] == 1
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_upgraded_database_accepts_a_manual_read(tmp_path):
    path = tmp_path / "v1.db"
    seed_v1(path)
    conn = connect(path)
    try:
        init_db(conn)
        from readback.points import log_manual_read

        conn.execute("INSERT INTO books (title, points, created_at) VALUES ('Free read', 2.0, '2026-02-01')")
        conn.commit()
        result = log_manual_read(conn, 1, 2)
        assert result.points_awarded == 2.0

        from readback.rewards import badges_for, set_goal

        set_goal(conn, 1, 10)
        ids = {badge.id for badge in badges_for(conn, 1)}
        assert "first_finish" in ids
    finally:
        conn.close()


def test_fresh_database_is_already_v2(conn):
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(attempts)")}
    assert columns["quiz_id"]["notnull"] == 0
    assert "kind" in columns
