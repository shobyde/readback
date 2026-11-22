"""AR BookFind client: parsing, session flow, and catalog import."""

from __future__ import annotations

import httpx
import pytest

from readback.arbookfind import (
    ArBookFindError,
    parse_detail_page,
    parse_search_page,
    search_ar_books,
    fetch_ar_book,
)
from readback.books import add_ar_book, add_book, get_book, list_books

SEARCH_PAGE = """\
<html><body>
<div class="result-item">
  <div class="result-cover">
    <a href="/BookDetail/2541"><img src="cover.jpg" alt="Cover: The Hunger Games"></a>
  </div>
  <a href="/BookDetail/2541" class="result-title-link">The Hunger Games</a>
  <a href="/Search?q=hunger" class="result-author-link">Suzanne Collins</a>
  <div class="result-meta">AR quiz no. 123740 &nbsp; BL: 5.3 &nbsp; AR Pts: 15.0 &nbsp; IL: MG+</div>
  <div class="result-summary">Katniss Everdeen volunteers to take her sister&#x27;s place&#x2026;</div>
</div>
<div class="result-item">
  <div class="result-cover">
    <a href="/BookDetail/8812"><img src="cover2.jpg" alt="Cover: Catching Fire"></a>
  </div>
  <a href="/BookDetail/8812" class="result-title-link">Catching Fire</a>
  <a href="/Search?q=fire" class="result-author-link">Suzanne Collins</a>
  <div class="result-meta">AR quiz no. 132824 BL: 5.3 AR Pts: 16.0 IL: MG+</div>
  <div class="result-summary">Katniss and Peeta are pulled back into the Games&#x2026;</div>
</div>
</body></html>
"""

DETAIL_PAGE = """\
<html><body>
<h1 class="bd-title">The Hunger Games</h1>
<p class="bd-author">by Suzanne Collins</p>
<p class="bd-description">In Panem, Katniss volunteers for the Games&#x2014;and wins.</p>
<p>AR quiz no. 123740</p>
<table class="bd-attr-table">
  <tr><th>Interest level</th><td>MG+</td></tr>
  <tr><th>ATOS level</th><td>5.3</td></tr>
  <tr><th>AR points</th><td>15.0</td></tr>
  <tr><th>Word count</th><td>99750</td></tr>
</table>
</body></html>
"""

NO_RESULTS_PAGE = "<html><body><p>Your search did not match any books.</p></body></html>"


def transport_for(*, search_page: str = SEARCH_PAGE, detail_page: str = DETAIL_PAGE):
    """A fake BookFind that answers the session handshake and both pages."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/UserType":
            if request.method == "POST":
                return httpx.Response(200, text="<html>teacher</html>")
            return httpx.Response(
                200,
                text=(
                    '<input name="__RequestVerificationToken" '
                    'type="hidden" value="token-123">'
                ),
            )
        if path == "/Search":
            return httpx.Response(200, text=search_page)
        if path.startswith("/BookDetail/"):
            return httpx.Response(200, text=detail_page)
        return httpx.Response(404, text="nope")

    return httpx.MockTransport(handler)


def client_for(**kwargs) -> httpx.Client:
    return httpx.Client(
        transport=transport_for(**kwargs), base_url="https://www.arbookfind.com"
    )


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_parse_search_page_extracts_catalog_fields():
    results = parse_search_page(SEARCH_PAGE)
    assert [entry.title for entry in results] == ["The Hunger Games", "Catching Fire"]
    first = results[0]
    assert first.ar_id == 2541
    assert first.author == "Suzanne Collins"
    assert first.quiz_no == 123740
    assert first.book_level == 5.3
    assert first.points == 15.0
    assert first.interest_level == "MG+"
    assert first.summary.startswith("Katniss Everdeen volunteers")
    assert first.detail_path == "/BookDetail/2541"


def test_parse_search_page_ignores_blocks_without_a_link():
    assert parse_search_page("<div class=\"result-item\">no anchor</div>") == []


def test_parse_detail_page_reads_the_attribute_table():
    entry = parse_detail_page(DETAIL_PAGE, 2541)
    assert entry.title == "The Hunger Games"
    assert entry.author == "Suzanne Collins"
    assert entry.quiz_no == 123740
    assert entry.book_level == 5.3
    assert entry.points == 15.0
    assert entry.interest_level == "MG+"
    assert entry.word_count == 99750
    assert "Katniss volunteers" in entry.summary


def test_parse_detail_page_rejects_an_unreadable_page():
    with pytest.raises(ArBookFindError):
        parse_detail_page("<html><body>Service unavailable</body></html>", 1)


# --------------------------------------------------------------------------- #
# session + HTTP
# --------------------------------------------------------------------------- #


def test_search_opens_the_teacher_session_then_queries():
    with client_for() as client:
        results = search_ar_books("hunger", client=client)
    assert len(results) == 2
    assert results[1].quiz_no == 132824


def test_search_with_a_blank_query_makes_no_request():
    def handler(request):  # pragma: no cover - should never run
        raise AssertionError("no request expected")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert search_ar_books("   ", client=client) == []


def test_search_returns_empty_when_the_site_says_no_results():
    with client_for(search_page=NO_RESULTS_PAGE) as client:
        assert search_ar_books("zzzz", client=client) == []


def test_search_raises_when_the_page_shape_changes():
    with client_for(search_page="<html><body><div>new layout</div></body></html>") as client:
        with pytest.raises(ArBookFindError):
            search_ar_books("hunger", client=client)


def test_search_raises_when_the_token_is_missing():
    def handler(request):
        return httpx.Response(200, text="<html>no token here</html>")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArBookFindError, match="form token"):
            search_ar_books("hunger", client=client)


def test_search_wraps_transport_errors():
    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArBookFindError, match="could not reach"):
            search_ar_books("hunger", client=client)


def test_fetch_book_reads_the_detail_page():
    with client_for() as client:
        entry = fetch_ar_book(2541, client=client)
    assert entry.title == "The Hunger Games"
    assert entry.ar_id == 2541


def test_fetch_book_reports_a_missing_book():
    def handler(request):
        if request.url.path == "/UserType":
            if request.method == "POST":
                return httpx.Response(200, text="ok")
            return httpx.Response(
                200,
                text='<input name="__RequestVerificationToken" value="t">',
            )
        return httpx.Response(404, text="missing")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ArBookFindError, match="no book 999"):
            fetch_ar_book(999, client=client)


def test_fetch_book_validates_the_identifier():
    with pytest.raises(ValueError):
        fetch_ar_book(0, client=client_for())


# --------------------------------------------------------------------------- #
# catalog import
# --------------------------------------------------------------------------- #


def test_add_ar_book_inserts_catalog_fields(conn):
    entry = parse_detail_page(DETAIL_PAGE, 2541)
    book_id = add_ar_book(conn, entry)
    book = get_book(conn, book_id)
    assert book is not None
    assert book.title == "The Hunger Games"
    assert book.quiz_no == 123740
    assert book.book_level == 5.3
    assert book.interest_level == "MG+"
    assert book.points == 15.0
    assert book.source == "arbookfind"
    assert book.summary.startswith("In Panem")


def test_add_ar_book_is_idempotent_by_quiz_number(conn):
    entry = parse_detail_page(DETAIL_PAGE, 2541)
    first = add_ar_book(conn, entry)
    again = add_ar_book(conn, entry)
    assert first == again
    assert len(list_books(conn)) == 1


def test_add_ar_book_refreshes_points_without_losing_notes(conn):
    entry = parse_detail_page(DETAIL_PAGE, 2541)
    book_id = add_ar_book(conn, entry)
    conn.execute("UPDATE books SET notes = 'read aloud in March' WHERE id = ?", (book_id,))
    conn.commit()

    from dataclasses import replace

    add_ar_book(conn, replace(entry, points=16.0))
    book = get_book(conn, book_id)
    assert book is not None
    assert book.points == 16.0
    assert book.notes == "read aloud in March"


def test_add_ar_book_matches_a_hand_entered_title(conn):
    existing = add_book(conn, title="The Hunger Games", author="Suzanne Collins", points=5.0)
    entry = parse_detail_page(DETAIL_PAGE, 2541)
    assert add_ar_book(conn, entry) == existing
    book = get_book(conn, existing)
    assert book is not None and book.quiz_no == 123740


def test_add_ar_book_requires_a_title(conn):
    from readback.arbookfind import ArBook

    with pytest.raises(ValueError):
        add_ar_book(conn, ArBook(ar_id=1, title="  "))
