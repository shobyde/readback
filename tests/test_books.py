"""Tests for the book catalog: CSV import, listing, ISBN enrichment."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from readback import books
from readback.books import add_book, enrich_isbn, get_book, import_books, list_books

EXAMPLE_CSV = Path(__file__).resolve().parents[1] / "examples" / "books.example.csv"


def write_csv(tmp_path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    path = tmp_path / "books.csv"
    path.write_text(text, encoding=encoding)
    return path


def book_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) AS n FROM books").fetchone()["n"]


SIMPLE_CSV = """title,author,isbn,points,grade_level,notes
Charlotte's Web,E.B. White,9780064400558,5.0,4-6,farm
Holes,Louis Sachar,9780440414803,7.0,5-7,camp
"""


# --------------------------------------------------------------------------
# import_books
# --------------------------------------------------------------------------


def test_import_books_clean_file_counts(conn, tmp_path):
    result = import_books(conn, write_csv(tmp_path, SIMPLE_CSV))

    assert (result.inserted, result.updated, result.skipped) == (2, 0, 0)
    assert result.errors == []
    assert book_count(conn) == 2

    stored = list_books(conn)
    assert [b.title for b in stored] == ["Charlotte's Web", "Holes"]
    assert stored[0].isbn == "9780064400558"
    assert stored[0].points == 5.0
    assert stored[0].grade_level == "4-6"
    assert stored[0].created_at  # timestamp is populated


def test_import_books_upserts_by_isbn_and_updates_points(conn, tmp_path):
    import_books(conn, write_csv(tmp_path, SIMPLE_CSV))

    reimport = """title,author,isbn,points,grade_level,notes
Charlotte's Web,E.B. White,9780064400558,6.5,4-6,updated note
"""
    result = import_books(conn, write_csv(tmp_path, reimport))

    assert (result.inserted, result.updated, result.skipped) == (0, 1, 0)
    assert book_count(conn) == 2
    charlotte = list_books(conn, "charlotte")[0]
    assert charlotte.points == 6.5
    assert charlotte.notes == "updated note"


def test_import_books_upserts_by_title_and_author_without_isbn(conn, tmp_path):
    import_books(conn, write_csv(tmp_path, SIMPLE_CSV))
    before = book_count(conn)

    # Same book, different capitalisation and no ISBN at all.
    reimport = """title,author,points
holes,LOUIS SACHAR,8.0
"""
    result = import_books(conn, write_csv(tmp_path, reimport))

    assert (result.inserted, result.updated) == (0, 1)
    assert book_count(conn) == before

    holes = list_books(conn, "holes")[0]
    assert holes.points == 8.0
    # A blank CSV ISBN must not wipe the ISBN we already knew.
    assert holes.isbn == "9780440414803"


def test_import_books_blank_points_defaults_to_zero(conn, tmp_path):
    csv_text = """title,author,isbn,points
Frog and Toad Are Friends,Arnold Lobel,9780064440202,
"""
    result = import_books(conn, write_csv(tmp_path, csv_text))

    assert (result.inserted, result.skipped) == (1, 0)
    assert list_books(conn)[0].points == 0.0


def test_import_books_invalid_points_reported_without_aborting(conn, tmp_path):
    csv_text = """title,author,isbn,points
Good Book,Someone,9780000000001,4.0
Bad Points,Someone,9780000000002,loads
Also Good,Someone,9780000000003,2.5
"""
    result = import_books(conn, write_csv(tmp_path, csv_text))

    assert result.inserted == 2
    assert result.skipped == 1
    assert len(result.errors) == 1
    assert "row 3" in result.errors[0]
    assert "points" in result.errors[0]
    assert book_count(conn) == 2
    assert sorted(b.title for b in list_books(conn)) == ["Also Good", "Good Book"]


def test_import_books_handles_utf8_bom_and_messy_headers(conn, tmp_path):
    csv_text = (
        " Title , AUTHOR ,ISBN,Points,Grade Level,Notes\n"
        "The Giver,Lois Lowry,9780544336261,9.0,6-8,dystopia\n"
    )
    # utf-8-sig writes a real BOM before the first header cell.
    path = write_csv(tmp_path, csv_text, encoding="utf-8-sig")

    result = import_books(conn, path)

    assert (result.inserted, result.skipped) == (1, 0)
    stored = list_books(conn)[0]
    assert stored.title == "The Giver"
    assert stored.author == "Lois Lowry"
    assert stored.grade_level == "6-8"


def test_import_books_missing_title_is_an_error_row(conn, tmp_path):
    csv_text = """title,author,isbn,points
,Anonymous,9780000000010,3.0
Real Book,Real Author,9780000000011,3.0
"""
    result = import_books(conn, write_csv(tmp_path, csv_text))

    assert result.inserted == 1
    assert result.skipped == 1
    assert "row 2" in result.errors[0]
    assert "title" in result.errors[0]
    assert [b.title for b in list_books(conn)] == ["Real Book"]


def test_import_books_example_file(conn):
    result = import_books(conn, EXAMPLE_CSV)

    assert result.errors == []
    assert result.inserted == 12
    assert result.skipped == 0
    stored = list_books(conn)
    assert len(stored) == 12
    assert all(0.5 <= b.points <= 20 for b in stored)
    assert all(b.grade_level for b in stored)
    # Importing the same file again updates instead of duplicating.
    again = import_books(conn, EXAMPLE_CSV)
    assert (again.inserted, again.updated) == (0, 12)
    assert book_count(conn) == 12


# --------------------------------------------------------------------------
# add_book / list_books / get_book
# --------------------------------------------------------------------------


def test_add_book_returns_id_and_row_round_trips(conn):
    book_id = add_book(
        conn,
        title="  Wonder  ",
        author="R.J. Palacio",
        isbn="9780375869020",
        points=9.0,
        grade_level="5-7",
        notes="kindness",
    )

    stored = get_book(conn, book_id)
    assert stored is not None
    assert stored.title == "Wonder"
    assert stored.author == "R.J. Palacio"
    assert stored.isbn == "9780375869020"
    assert stored.points == 9.0
    assert stored.grade_level == "5-7"
    assert stored.notes == "kindness"


def test_add_book_empty_title_raises(conn):
    with pytest.raises(ValueError):
        add_book(conn, title="   ")
    assert book_count(conn) == 0


def test_add_book_blank_isbn_is_stored_as_null(conn):
    book_id = add_book(conn, title="No ISBN Here")
    row = conn.execute("SELECT isbn FROM books WHERE id = ?", (book_id,)).fetchone()
    assert row["isbn"] is None


def test_list_books_is_ordered_and_searches_title_and_author(conn):
    add_book(conn, title="Zebra Tales", author="Alice Smith")
    add_book(conn, title="apple Pie", author="Bob Jones")
    add_book(conn, title="Middle Ground", author="Zelda Quill")

    titles = [b.title for b in list_books(conn)]
    assert titles == ["apple Pie", "Middle Ground", "Zebra Tales"]

    # title match, case-insensitive substring
    assert [b.title for b in list_books(conn, "PIE")] == ["apple Pie"]
    # author match
    assert [b.title for b in list_books(conn, "quill")] == ["Middle Ground"]
    # blank query behaves like no query
    assert len(list_books(conn, "   ")) == 3
    assert list_books(conn, "nothing matches") == []


def test_list_books_query_wildcards_are_literal(conn):
    add_book(conn, title="100% Real")
    add_book(conn, title="Something Else")

    assert [b.title for b in list_books(conn, "%")] == ["100% Real"]
    assert [b.title for b in list_books(conn, "_")] == []


def test_get_book_missing_returns_none(conn):
    assert get_book(conn, 999) is None


# --------------------------------------------------------------------------
# enrich_isbn
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload=None, *, bad_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


def test_enrich_isbn_uses_by_statement(conn, monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(
            200,
            {
                "title": "Charlotte's Web",
                "by_statement": "by E. B. White",
                "authors": [{"name": "E. B. White"}],
            },
        )

    monkeypatch.setattr(books.httpx, "get", fake_get)

    assert enrich_isbn("9780064400558") == {
        "title": "Charlotte's Web",
        "author": "by E. B. White",
    }
    url, kwargs = calls[0]
    assert url == "https://openlibrary.org/isbn/9780064400558.json"
    assert kwargs["follow_redirects"] is True
    assert kwargs["timeout"] == 5.0


def test_enrich_isbn_falls_back_to_first_author(monkeypatch):
    monkeypatch.setattr(
        books.httpx,
        "get",
        lambda url, **kw: FakeResponse(
            200, {"title": "Holes", "authors": [{"name": "Louis Sachar"}]}
        ),
    )

    assert enrich_isbn("9780440414803", timeout=2.5) == {
        "title": "Holes",
        "author": "Louis Sachar",
    }


def test_enrich_isbn_without_any_author_returns_empty_string(monkeypatch):
    monkeypatch.setattr(
        books.httpx, "get", lambda url, **kw: FakeResponse(200, {"title": "Mystery"})
    )

    assert enrich_isbn("9780000000000") == {"title": "Mystery", "author": ""}


def test_enrich_isbn_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(
        books.httpx, "get", lambda url, **kw: FakeResponse(404, {"error": "notfound"})
    )

    assert enrich_isbn("9780000000000") is None


def test_enrich_isbn_returns_none_on_network_error(monkeypatch):
    def boom(url, **kwargs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(books.httpx, "get", boom)

    assert enrich_isbn("9780064400558") is None


def test_enrich_isbn_returns_none_on_malformed_body(monkeypatch):
    monkeypatch.setattr(
        books.httpx, "get", lambda url, **kw: FakeResponse(200, bad_json=True)
    )
    assert enrich_isbn("9780064400558") is None

    monkeypatch.setattr(
        books.httpx, "get", lambda url, **kw: FakeResponse(200, {"by_statement": "x"})
    )
    assert enrich_isbn("9780064400558") is None

    monkeypatch.setattr(
        books.httpx, "get", lambda url, **kw: FakeResponse(200, ["not", "a", "dict"])
    )
    assert enrich_isbn("9780064400558") is None


def test_enrich_isbn_blank_input_never_calls_network(monkeypatch):
    def fail(url, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("network call for a blank ISBN")

    monkeypatch.setattr(books.httpx, "get", fail)

    assert enrich_isbn("   ") is None
