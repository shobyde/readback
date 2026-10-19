"""Book catalog: CSV import, listing, and Open Library ISBN enrichment."""

from __future__ import annotations

import csv
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import httpx

from .db import transaction
from .models import Book, ImportResult, utc_now

__all__ = [
    "add_book",
    "enrich_isbn",
    "get_book",
    "import_books",
    "list_books",
]

COLUMNS = "id, title, author, isbn, points, grade_level, notes, created_at"

#: CSV columns the importer understands.  Header names are matched
#: case-insensitively after stripping surrounding whitespace.
FIELDS = ("title", "author", "isbn", "points", "grade_level", "notes")

OPEN_LIBRARY_ISBN_URL = "https://openlibrary.org/isbn/{isbn}.json"


def _row_to_book(row: sqlite3.Row) -> Book:
    return Book(
        id=row["id"],
        title=row["title"],
        author=row["author"],
        isbn=row["isbn"],
        points=row["points"],
        grade_level=row["grade_level"],
        notes=row["notes"],
        created_at=row["created_at"],
    )


def _clean(value: object) -> str:
    """Text from a CSV cell: ``None`` becomes ``""`` and whitespace is trimmed."""
    if value is None:
        return ""
    return str(value).strip()


def _parse_points(raw: str) -> float:
    """Blank means ``0.0``; anything else must be a non-negative float."""
    text = raw.strip()
    if not text:
        return 0.0
    try:
        points = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"points {raw!r} is not a number") from None
    if points < 0:
        raise ValueError(f"points {raw!r} cannot be negative")
    return points


def _normalised_headers(fieldnames: Sequence[str | None] | None) -> dict[str, str]:
    """Map normalised header name -> the key ``csv.DictReader`` used for it.

    Headers are lowercased and stripped, and internal whitespace becomes an
    underscore so a spreadsheet header such as ``Grade Level`` still lands in
    the ``grade_level`` field.
    """
    mapping: dict[str, str] = {}
    for name in fieldnames or ():
        if name is None:
            continue
        key = "_".join(name.strip().lower().split())
        if key and key not in mapping:
            mapping[key] = name
    return mapping


def _read_row(row: dict, headers: dict[str, str]) -> dict[str, str]:
    return {
        field: _clean(row.get(headers[field])) if field in headers else ""
        for field in FIELDS
    }


def _find_existing(
    conn: sqlite3.Connection, *, isbn: str, title: str, author: str
) -> int | None:
    """Look a book up by ISBN first, then by case-folded title + author."""
    if isbn:
        found = conn.execute("SELECT id FROM books WHERE isbn = ?", (isbn,)).fetchone()
        if found is not None:
            return int(found["id"])
        # A row with the same title/author but a different ISBN is the same book.
    found = conn.execute(
        "SELECT id FROM books WHERE lower(title) = lower(?) AND lower(author) = lower(?)",
        (title, author),
    ).fetchone()
    return int(found["id"]) if found is not None else None


def _insert_book(conn: sqlite3.Connection, row: dict[str, str], points: float) -> None:
    conn.execute(
        "INSERT INTO books (title, author, isbn, points, grade_level, notes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            row["title"],
            row["author"],
            row["isbn"] or None,
            points,
            row["grade_level"],
            row["notes"],
            utc_now(),
        ),
    )


def _update_book(
    conn: sqlite3.Connection, book_id: int, row: dict[str, str], points: float
) -> None:
    # A blank ISBN in the CSV never wipes an ISBN we already know.
    if row["isbn"]:
        conn.execute(
            "UPDATE books SET title = ?, author = ?, isbn = ?, points = ?, "
            "grade_level = ?, notes = ? WHERE id = ?",
            (
                row["title"],
                row["author"],
                row["isbn"],
                points,
                row["grade_level"],
                row["notes"],
                book_id,
            ),
        )
    else:
        conn.execute(
            "UPDATE books SET title = ?, author = ?, points = ?, "
            "grade_level = ?, notes = ? WHERE id = ?",
            (
                row["title"],
                row["author"],
                points,
                row["grade_level"],
                row["notes"],
                book_id,
            ),
        )


def import_books(conn: sqlite3.Connection, csv_path: str | Path) -> ImportResult:
    """Import books from ``csv_path``, upserting rows that already exist.

    The whole file is one transaction.  A bad row is reported in
    ``ImportResult.errors`` (with its 1-based line number) and skipped; it never
    aborts the import.
    """
    result = ImportResult()

    # utf-8-sig strips a BOM that would otherwise end up inside the first header.
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = _normalised_headers(reader.fieldnames)

        with transaction(conn):
            # Line 1 is the header, so the first data row is line 2.
            for line_number, raw in enumerate(reader, start=2):
                row = _read_row(raw, headers)
                try:
                    if not row["title"]:
                        raise ValueError("title is required")
                    points = _parse_points(row["points"])
                    existing = _find_existing(
                        conn,
                        isbn=row["isbn"],
                        title=row["title"],
                        author=row["author"],
                    )
                    if existing is None:
                        _insert_book(conn, row, points)
                        result.inserted += 1
                    else:
                        _update_book(conn, existing, row, points)
                        result.updated += 1
                except (ValueError, sqlite3.IntegrityError) as exc:
                    result.skipped += 1
                    result.errors.append(f"row {line_number}: {exc}")

    return result


def add_book(
    conn: sqlite3.Connection,
    *,
    title: str,
    author: str = "",
    isbn: str | None = None,
    points: float = 0.0,
    grade_level: str = "",
    notes: str = "",
) -> int:
    """Insert one book and return its id.  An empty title is a ``ValueError``."""
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("title is required")
    clean_isbn = (isbn or "").strip() or None
    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO books (title, author, isbn, points, grade_level, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                clean_title,
                (author or "").strip(),
                clean_isbn,
                float(points),
                (grade_level or "").strip(),
                (notes or "").strip(),
                utc_now(),
            ),
        )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _like_pattern(query: str) -> str:
    """Substring pattern for LIKE, with ``%``/``_`` in the query taken literally."""
    escaped = (
        query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def list_books(conn: sqlite3.Connection, query: str | None = None) -> list[Book]:
    """All books, optionally filtered by a case-insensitive title/author substring."""
    if query is not None and query.strip():
        pattern = _like_pattern(query.strip())
        rows = conn.execute(
            f"SELECT {COLUMNS} FROM books "
            "WHERE lower(title) LIKE lower(?) ESCAPE '\\' "
            "OR lower(author) LIKE lower(?) ESCAPE '\\' "
            "ORDER BY lower(title), lower(author)",
            (pattern, pattern),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {COLUMNS} FROM books ORDER BY lower(title), lower(author)"
        ).fetchall()
    return [_row_to_book(row) for row in rows]


def get_book(conn: sqlite3.Connection, book_id: int) -> Book | None:
    """One book by id, or ``None`` when it does not exist."""
    row = conn.execute(
        f"SELECT {COLUMNS} FROM books WHERE id = ?", (book_id,)
    ).fetchone()
    return _row_to_book(row) if row is not None else None


def _author_from_body(body: dict) -> str:
    by_statement = body.get("by_statement")
    if isinstance(by_statement, str) and by_statement.strip():
        return by_statement.strip()
    authors = body.get("authors")
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, dict):
            name = first.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        elif isinstance(first, str) and first.strip():
            return first.strip()
    return ""


def enrich_isbn(isbn: str, *, timeout: float = 5.0) -> dict | None:
    """Look an ISBN up on Open Library.

    Returns ``{"title": ..., "author": ...}`` or ``None``.  Network errors,
    404s, and malformed bodies all collapse to ``None`` — this never raises.
    """
    clean = (isbn or "").strip()
    if not clean:
        return None
    url = OPEN_LIBRARY_ISBN_URL.format(isbn=clean)
    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        if response.status_code != 200:
            return None
        body = response.json()
        if not isinstance(body, dict):
            return None
        title = body.get("title")
        if not isinstance(title, str) or not title.strip():
            return None
        return {"title": title.strip(), "author": _author_from_body(body)}
    except Exception:  # noqa: BLE001 - enrichment is best-effort, never fatal
        return None
