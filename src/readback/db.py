"""SQLite helpers: connection setup, schema creation, migrations, transactions.

Schema version history (there is no Alembic; ``init_db`` migrates in place):

* v1 — MVP: readers, books, quizzes, questions, attempts, attempt_answers,
  points_ledger.
* v2 — AR catalog columns on ``books``, rewards (``goals``, ``book_stickers``),
  and attempts that can describe a book read without a quiz (``kind`` plus a
  nullable ``quiz_id``).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """\
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS readers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  isbn TEXT,
  points REAL NOT NULL DEFAULT 0.0 CHECK (points >= 0),
  grade_level TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  quiz_no INTEGER,
  book_level REAL,
  interest_level TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_books_isbn
  ON books(isbn) WHERE isbn IS NOT NULL AND isbn != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_books_title_author
  ON books(lower(title), lower(author));

CREATE TABLE IF NOT EXISTS quizzes (
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
CREATE INDEX IF NOT EXISTS idx_quizzes_book_status ON quizzes(book_id, status);

CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  choices_json TEXT NOT NULL,
  answer_index INTEGER NOT NULL CHECK (answer_index BETWEEN 0 AND 3),
  explanation TEXT NOT NULL DEFAULT '',
  UNIQUE (quiz_id, ordinal)
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,
  book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
  reader_id INTEGER NOT NULL REFERENCES readers(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'quiz' CHECK (kind IN ('quiz','manual')),
  started_at TEXT NOT NULL,
  submitted_at TEXT,
  score REAL,
  passed INTEGER,
  total_questions INTEGER NOT NULL DEFAULT 0,
  correct_answers INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attempts_reader ON attempts(reader_id, submitted_at);

CREATE TABLE IF NOT EXISTS attempt_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  choice_index INTEGER NOT NULL,
  correct INTEGER NOT NULL,
  UNIQUE (attempt_id, question_id)
);

CREATE TABLE IF NOT EXISTS points_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reader_id INTEGER NOT NULL REFERENCES readers(id) ON DELETE CASCADE,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  points REAL NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (reader_id, book_id)
);

CREATE TABLE IF NOT EXISTS goals (
  reader_id INTEGER PRIMARY KEY REFERENCES readers(id) ON DELETE CASCADE,
  target REAL NOT NULL CHECK (target > 0),
  period TEXT NOT NULL DEFAULT 'season',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_stickers (
  book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
  emoji TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  assigned_at TEXT NOT NULL
);
"""

TABLES = (
    "readers",
    "books",
    "quizzes",
    "questions",
    "attempts",
    "attempt_answers",
    "points_ledger",
    "goals",
    "book_stickers",
)

#: Columns added after the MVP, with the ``ALTER TABLE`` type that adds them.
_BOOK_COLUMNS = {
    "quiz_no": "INTEGER",
    "book_level": "REAL",
    "interest_level": "TEXT NOT NULL DEFAULT ''",
    "summary": "TEXT NOT NULL DEFAULT ''",
    "source": "TEXT NOT NULL DEFAULT 'manual'",
}

_ATTEMPTS_V2 = (
    "CREATE TABLE attempts_v2 ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,"
    "  book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,"
    "  reader_id INTEGER NOT NULL REFERENCES readers(id) ON DELETE CASCADE,"
    "  kind TEXT NOT NULL DEFAULT 'quiz' CHECK (kind IN ('quiz','manual')),"
    "  started_at TEXT NOT NULL,"
    "  submitted_at TEXT,"
    "  score REAL,"
    "  passed INTEGER,"
    "  total_questions INTEGER NOT NULL DEFAULT 0,"
    "  correct_answers INTEGER NOT NULL DEFAULT 0"
    ");"
    "INSERT INTO attempts_v2 "
    "  (id, quiz_id, book_id, reader_id, kind, started_at, submitted_at, score,"
    "   passed, total_questions, correct_answers) "
    "SELECT a.id, a.quiz_id,"
    "  (SELECT q.book_id FROM quizzes q WHERE q.id = a.quiz_id),"
    "  a.reader_id, 'quiz', a.started_at, a.submitted_at, a.score, a.passed,"
    "  a.total_questions, a.correct_answers "
    "FROM attempts a;"
    "DROP TABLE attempts;"
    "ALTER TABLE attempts_v2 RENAME TO attempts;"
    "CREATE INDEX IF NOT EXISTS idx_attempts_reader"
    "  ON attempts(reader_id, submitted_at);"
    "CREATE INDEX IF NOT EXISTS idx_attempts_book ON attempts(book_id);"
)


def connect(path: str | Path) -> sqlite3.Connection:
    """Open ``path`` with row dictionaries and foreign keys enabled."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _column_flags(conn: sqlite3.Connection, table: str) -> dict[str, tuple]:
    return {row[1]: tuple(row) for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_books(conn: sqlite3.Connection) -> None:
    """Add the AR catalog columns to a database created before v0.2.0."""
    columns = _column_flags(conn, "books")
    if not columns:
        return
    for name, ddl in _BOOK_COLUMNS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE books ADD COLUMN {name} {ddl}")
    # Built here, not in SCHEMA: on a pre-0.2.0 table the column does not exist
    # yet, and executescript would abort before the ALTER above could run.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_books_quiz_no "
        "ON books(quiz_no) WHERE quiz_no IS NOT NULL"
    )


def _migrate_attempts(conn: sqlite3.Connection) -> None:
    """Rebuild ``attempts`` so a book can be logged without a quiz.

    SQLite cannot relax a ``NOT NULL`` constraint in place, so the table is
    recreated and copied.  Foreign keys are switched off for the copy: with
    them on, dropping the old table would cascade away every ledger row.
    """
    columns = _column_flags(conn, "attempts")
    if not columns:
        return
    quiz_id_not_null = bool(columns.get("quiz_id", (None, None, None, 1))[3])
    if "kind" in columns and "book_id" in columns and not quiz_id_not_null:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_attempts_book ON attempts(book_id)"
        )
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(_ATTEMPTS_V2)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """Create the schema and bring an older database up to date.

    Safe to call on every start: every step is idempotent.
    """
    conn.executescript(SCHEMA)
    _migrate_books(conn)
    _migrate_attempts(conn)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on success, roll back on any exception."""
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


__all__ = ["SCHEMA", "TABLES", "connect", "init_db", "transaction"]
