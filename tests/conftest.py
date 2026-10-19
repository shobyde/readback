"""Shared pytest fixtures.

Fixture names are part of the cross-ticket contract: ``conn``, ``book``,
``reader`` and ``settings``.  Rows for ``book`` / ``reader`` are inserted with
plain SQL on purpose, so this file never depends on sibling modules.
"""

from __future__ import annotations

import pytest

from readback.config import Settings
from readback.db import connect, init_db, transaction
from readback.models import Book, Reader, utc_now


@pytest.fixture()
def conn(tmp_path):
    """Fresh initialised SQLite database."""
    connection = connect(tmp_path / "readback-test.db")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def book(conn) -> Book:
    """A saved book row."""
    created_at = utc_now()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO books (title, author, isbn, points, grade_level, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("The Hobbit", "J.R.R. Tolkien", "9780547928227", 5.0, "6-8", "", created_at),
        )
    return Book(
        id=cursor.lastrowid,
        title="The Hobbit",
        author="J.R.R. Tolkien",
        isbn="9780547928227",
        points=5.0,
        grade_level="6-8",
        notes="",
        created_at=created_at,
    )


@pytest.fixture()
def reader(conn) -> Reader:
    """A saved reader row."""
    created_at = utc_now()
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO readers (name, created_at) VALUES (?, ?)",
            ("Ada", created_at),
        )
    return Reader(id=cursor.lastrowid, name="Ada", created_at=created_at)


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Local-only settings; the database has the schema already."""
    db_path = tmp_path / "settings.db"
    connection = connect(db_path)
    try:
        init_db(connection)
    finally:
        connection.close()
    return Settings(
        db_path=db_path,
        host="127.0.0.1",
        port=5057,
        llm_base_url="http://localhost:9/v1",
        llm_api_key="test-key",
        llm_model="test-model",
        pass_threshold=0.8,
        parent_pin="",
        secret_key="test-secret-key",
    )
