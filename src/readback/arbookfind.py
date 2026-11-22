"""Look books up in the Accelerated Reader BookFind catalog.

This is an **optional, read-only** client.  Readback ships no AR data of its
own: the operator asks arbookfind.com for a book, and what comes back is the
public catalog entry a teacher would see (quiz number, ATOS level, interest
level, AR points).  Quiz content is never copied — Readback still generates
its own questions.

The site was rewritten from ASP.NET WebForms to ASP.NET Core Razor Pages, so
the ``*.aspx`` endpoints are gone.  Current flow, verified against the live
site in November 2026:

1. ``GET  /UserType``                  -> antiforgery token in a hidden input
2. ``POST /UserType?handler=Select``   -> ``userType=Teacher`` (Teacher view
   shows the wider data set)
3. ``GET  /Search?q=<keywords>``       -> one ``.result-item`` block per hit
4. ``GET  /BookDetail/<id>``           -> title, author, description,
   and a ``bd-attr-table`` of labeled rows

Everything that can fail (network, a changed page shape, a missing token)
raises :class:`ArBookFindError` with a short, printable message.  Nothing here
touches the database, so it is trivial to test with an ``httpx.MockTransport``.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import httpx

BASE_URL = "https://www.arbookfind.com"
USER_AGENT = "Readback/0.2 (self-hosted family reading tracker)"
DEFAULT_TIMEOUT = 25.0

_TOKEN_RE = re.compile(
    r'name="__RequestVerificationToken"[^>]*value="([^"]+)"'
)
_RESULT_HREF_RE = re.compile(r'href="(/BookDetail/(\d+))"', re.I)
_QUIZ_NO_RE = re.compile(r"AR quiz no\.\s*(\d+)")
_LEVEL_RE = re.compile(r"BL:\s*([\d.]+)")
_POINTS_RE = re.compile(r"AR Pts:\s*([\d.]+)")
_INTEREST_RE = re.compile(r"IL:\s*([A-Z][A-Z+\-]*)")


class ArBookFindError(RuntimeError):
    """AR BookFind could not be read (network, or the page shape changed)."""


@dataclass(frozen=True)
class ArBook:
    """One catalog entry, independent of Readback's own ``Book`` row."""

    ar_id: int
    title: str
    author: str = ""
    quiz_no: int | None = None
    book_level: float | None = None
    interest_level: str = ""
    points: float | None = None
    word_count: int | None = None
    summary: str = ""
    detail_path: str = field(default="", compare=False)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _text(raw: str) -> str:
    """Tags out, entities decoded, whitespace squashed."""
    without_tags = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _float_or_none(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _int_or_none(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_search_page(page: str) -> list[ArBook]:
    """Pull every ``.result-item`` block out of a search response.

    A block opens with a cover anchor whose only child is an ``<img>`` and then
    repeats the link around the title text, so the first non-empty anchor is the
    title — the cover anchor strips down to nothing.
    """
    blocks = page.split('<div class="result-item">')[1:]
    results: list[ArBook] = []
    for block in blocks:
        link = _RESULT_HREF_RE.search(block)
        if link is None:
            continue
        title = ""
        for anchor in re.finditer(
            r'<a[^>]*href="/BookDetail/\d+"[^>]*>(.*?)</a>', block, re.S
        ):
            candidate = _text(anchor.group(1))
            if candidate:
                title = candidate
                break
        if not title:
            continue
        author_match = re.search(
            r'class="result-author-link"[^>]*>(.*?)</a>', block, re.S
        )
        summary_match = re.search(
            r'<div class="result-summary">(.*?)</div>', block, re.S
        )
        results.append(
            ArBook(
                ar_id=int(link.group(2)),
                title=title,
                author=_text(author_match.group(1)) if author_match else "",
                quiz_no=_int_or_none(
                    m.group(1) if (m := _QUIZ_NO_RE.search(block)) else None
                ),
                book_level=_float_or_none(
                    m.group(1) if (m := _LEVEL_RE.search(block)) else None
                ),
                points=_float_or_none(
                    m.group(1) if (m := _POINTS_RE.search(block)) else None
                ),
                interest_level=(
                    m.group(1) if (m := _INTEREST_RE.search(block)) else ""
                ),
                summary=(
                    _text(summary_match.group(1)).rstrip("\u2026").strip()
                    if summary_match
                    else ""
                ),
                detail_path=link.group(1),
            )
        )
    return results


def _attr(page: str, label: str) -> str:
    """Read one labeled row out of the detail page's ``bd-attr-table``."""
    match = re.search(
        r"<th>\s*%s\s*:?\s*</th>\s*<td>(.*?)</td>" % re.escape(label),
        page,
        re.S | re.I,
    )
    return _text(match.group(1)) if match else ""


def parse_detail_page(page: str, ar_id: int) -> ArBook:
    """Turn a ``/BookDetail/<id>`` page into an :class:`ArBook`."""
    title_match = re.search(r'<h1 class="bd-title">(.*?)</h1>', page, re.S)
    title = _text(title_match.group(1)) if title_match else ""
    if not title:
        title_match = re.search(r"<title>(.*?)</title>", page, re.S)
        title = _text(title_match.group(1)).split(" - ")[0] if title_match else ""
    if not title:
        raise ArBookFindError(
            "AR BookFind returned a detail page Readback could not read "
            "(the site may have changed)"
        )

    author_match = re.search(r'<p class="bd-author">(.*?)</p>', page, re.S)
    summary_match = re.search(
        r'<p class="bd-description">(.*?)</p>', page, re.S
    )
    author = _text(author_match.group(1)) if author_match else ""
    if author.lower().startswith("by "):
        author = author[3:].strip()

    quiz_match = _QUIZ_NO_RE.search(page)
    return ArBook(
        ar_id=ar_id,
        title=title,
        author=author,
        quiz_no=_int_or_none(quiz_match.group(1) if quiz_match else None),
        book_level=_float_or_none(_attr(page, "ATOS level") or None),
        interest_level=_attr(page, "Interest level"),
        points=_float_or_none(_attr(page, "AR points") or None),
        word_count=_int_or_none(_attr(page, "Word count") or None),
        summary=_text(summary_match.group(1)) if summary_match else "",
        detail_path=f"/BookDetail/{ar_id}",
    )


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _client(client: httpx.Client | None) -> tuple[httpx.Client, bool]:
    if client is not None:
        return client, False
    return (
        httpx.Client(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ),
        True,
    )


def _open_session(client: httpx.Client) -> str:
    """Establish the Teacher-view cookie and return the session's search page."""
    try:
        page = client.get(f"{BASE_URL}/UserType")
        page.raise_for_status()
    except httpx.HTTPError as exc:
        raise ArBookFindError(
            f"could not reach AR BookFind ({type(exc).__name__})"
        ) from exc

    token = _TOKEN_RE.search(page.text)
    if token is None:
        raise ArBookFindError(
            "AR BookFind did not return a form token (the site may have changed)"
        )

    try:
        selected = client.post(
            f"{BASE_URL}/UserType?handler=Select",
            data={
                "userType": "Teacher",
                "returnUrl": "",
                "__RequestVerificationToken": token.group(1),
            },
        )
        selected.raise_for_status()
    except httpx.HTTPError as exc:
        raise ArBookFindError(
            f"AR BookFind refused the teacher session ({type(exc).__name__})"
        ) from exc
    return selected.text


def search_ar_books(
    query: str, *, client: httpx.Client | None = None
) -> list[ArBook]:
    """Search the catalog.  An empty result set is a normal answer."""
    keywords = (query or "").strip()
    if not keywords:
        return []

    session, owned = _client(client)
    try:
        _open_session(session)
        try:
            page = session.get(f"{BASE_URL}/Search", params={"q": keywords})
            page.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArBookFindError(
                f"AR BookFind search failed ({type(exc).__name__})"
            ) from exc

        results = parse_search_page(page.text)
        if not results and "result-item" not in page.text:
            lowered = page.text.lower()
            if "no results" in lowered or "did not match" in lowered:
                return []
            raise ArBookFindError(
                "AR BookFind returned a page Readback could not read "
                "(the site may have changed)"
            )
        return results
    finally:
        if owned:
            session.close()


def fetch_ar_book(ar_id: int, *, client: httpx.Client | None = None) -> ArBook:
    """Fetch one catalog entry by its BookFind id (the number in the URL)."""
    try:
        identifier = int(ar_id)
    except (TypeError, ValueError):
        raise ValueError(f"ar_id must be an integer, got {ar_id!r}") from None
    if identifier <= 0:
        raise ValueError(f"ar_id must be positive, got {identifier}")

    session, owned = _client(client)
    try:
        _open_session(session)
        try:
            page = session.get(f"{BASE_URL}/BookDetail/{identifier}")
            page.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ArBookFindError(
                f"AR BookFind has no book {identifier} ({exc.response.status_code})"
            ) from exc
        except httpx.HTTPError as exc:
            raise ArBookFindError(
                f"could not load book {identifier} ({type(exc).__name__})"
            ) from exc
        return parse_detail_page(page.text, identifier)
    finally:
        if owned:
            session.close()


__all__ = [
    "ArBook",
    "ArBookFindError",
    "BASE_URL",
    "fetch_ar_book",
    "parse_detail_page",
    "parse_search_page",
    "search_ar_books",
]
