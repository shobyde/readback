"""Rewards: stars, reading goals, badges, book stickers, and weekly reports.

The point of this module is motivation, not assessment.  Nothing here changes
whether a reader passed a quiz — ``points.submit_attempt`` owns that.  These are
the layers around it: how an attempt is celebrated, how far a reader is from
their goal, which milestones they unlocked, and what the shelf looks like.

Two rules keep it honest:

* Badges are **derived**, never stored.  Adding a badge is a code change to
  ``_evaluate``, and a reader's history is re-read every time, so a rule can be
  corrected without a migration and nobody keeps a badge they did not earn.
* Stickers are decoration with a deterministic fallback, so every book on the
  shelf looks like something even before a parent picks an emoji.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .db import transaction
from .models import Badge, Goal, utc_now

#: The ladder used when a reader has no personal goal.
DEFAULT_MILESTONES: tuple[float, ...] = (25, 50, 100, 150, 200, 300, 400, 500, 750, 1000)

#: Emoji a parent can pin to a book.  Kept small and searchable by eye rather
#: than the 200-item grid the original family app used; a native <select> beats
#: a JavaScript picker when the list is this short.
STICKERS: tuple[tuple[str, str], ...] = (
    ("🦁", "lion"),
    ("🐼", "panda"),
    ("🦊", "fox"),
    ("🐸", "frog"),
    ("🐵", "monkey"),
    ("🐯", "tiger"),
    ("🐨", "koala"),
    ("🦉", "owl"),
    ("🐢", "turtle"),
    ("🦄", "unicorn"),
    ("🐉", "dragon"),
    ("🐙", "octopus"),
    ("🐳", "whale"),
    ("🐝", "bee"),
    ("🦋", "butterfly"),
    ("🐌", "snail"),
    ("🐿️", "squirrel"),
    ("🦔", "hedgehog"),
    ("🌻", "sunflower"),
    ("🌳", "tree"),
    ("🍄", "mushroom"),
    ("🌙", "moon"),
    ("⭐", "star"),
    ("☀️", "sun"),
    ("🌈", "rainbow"),
    ("❄️", "snowflake"),
    ("🔥", "campfire"),
    ("🗺️", "map"),
    ("🧭", "compass"),
    ("🏰", "castle"),
    ("🚀", "rocket"),
    ("⛵", "sailboat"),
    ("🎈", "balloon"),
    ("🎨", "palette"),
    ("🎻", "violin"),
    ("🍎", "apple"),
    ("🍪", "cookie"),
    ("🫖", "teapot"),
    ("🧸", "teddy"),
    ("📚", "books"),
)

_STICKER_FALLBACK = ("🦉", "🦊", "🐢", "🐸", "🐼", "🦄", "🐙", "🐝", "🌙", "⭐")


def _as_aware(value: datetime | None) -> datetime:
    """Treat a naive datetime as UTC so timestamp comparisons stay sane."""
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# stars
# --------------------------------------------------------------------------- #


def stars(correct: int, total: int, *, pass_threshold: float = 0.8) -> int:
    """0–3 stars for an attempt: a kid-facing grade that is not a new score.

    Three stars at 90%, two at 70%, one for any pass, zero for a miss.  A quiz
    that failed to record a question count returns zero rather than dividing.
    """
    if not total or total <= 0:
        return 0
    ratio = max(0.0, min(1.0, correct / total))
    if ratio >= 0.9:
        return 3
    if ratio >= 0.7:
        return 2
    if ratio >= pass_threshold:
        return 1
    return 0


# --------------------------------------------------------------------------- #
# goals
# --------------------------------------------------------------------------- #


def set_goal(
    conn: sqlite3.Connection,
    reader_id: int,
    target: float,
    *,
    period: str = "season",
) -> Goal:
    """Set or replace a reader's points target.  ``target`` must be positive."""
    try:
        amount = float(target)
    except (TypeError, ValueError):
        raise ValueError(f"goal must be a number, got {target!r}") from None
    if amount <= 0:
        raise ValueError("a goal has to be greater than zero")
    reader = conn.execute(
        "SELECT id FROM readers WHERE id = ?", (reader_id,)
    ).fetchone()
    if reader is None:
        raise ValueError(f"reader {reader_id} not found")

    stamp = utc_now()
    clean_period = (period or "season").strip() or "season"
    with transaction(conn):
        conn.execute(
            "INSERT INTO goals (reader_id, target, period, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(reader_id) DO UPDATE SET target = excluded.target, "
            "period = excluded.period, updated_at = excluded.updated_at",
            (reader_id, amount, clean_period, stamp, stamp),
        )
    goal = get_goal(conn, reader_id)
    assert goal is not None
    return goal


def get_goal(conn: sqlite3.Connection, reader_id: int) -> Goal | None:
    """A reader's goal, or ``None`` when they have not set one."""
    row = conn.execute(
        "SELECT reader_id, target, period, created_at, updated_at "
        "FROM goals WHERE reader_id = ?",
        (reader_id,),
    ).fetchone()
    if row is None:
        return None
    return Goal(
        reader_id=int(row["reader_id"]),
        target=float(row["target"]),
        period=row["period"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def clear_goal(conn: sqlite3.Connection, reader_id: int) -> None:
    """Remove a goal.  Idempotent: clearing nothing is not an error."""
    with transaction(conn):
        conn.execute("DELETE FROM goals WHERE reader_id = ?", (reader_id,))


def milestones(goal: float | None) -> list[float]:
    """The celebration points for a goal (or the default ladder)."""
    if not goal or goal <= 0:
        return list(DEFAULT_MILESTONES)
    if goal < 25:
        return [float(goal)]
    steps = {float(value) for value in range(25, int(goal) + 1, 25)}
    steps.add(float(goal))
    return sorted(steps)


def goal_progress(points: float, goal: Goal | float | None) -> dict:
    """Where a reader stands against their goal.

    Returns ``status`` (``no_goal`` | ``on_track`` | ``complete``), the last
    milestone cleared, the next one, a clipped percentage and what is left.
    """
    target = goal.target if isinstance(goal, Goal) else goal
    earned = float(points or 0.0)
    if not target or target <= 0:
        return {
            "status": "no_goal",
            "goal": None,
            "points": earned,
            "percent": 0.0,
            "current": 0.0,
            "next": DEFAULT_MILESTONES[0],
            "remaining": None,
        }

    ladder = milestones(float(target))
    current = max((step for step in ladder if earned >= step), default=0.0)
    following = next((step for step in ladder if step > earned), None)
    complete = earned >= float(target)
    return {
        "status": "complete" if complete else "on_track",
        "goal": float(target),
        "points": earned,
        "percent": round(min(100.0, earned / float(target) * 100.0), 1),
        "current": current,
        "next": None if complete else following,
        "remaining": max(0.0, round(float(target) - earned, 2)),
    }


# --------------------------------------------------------------------------- #
# stickers
# --------------------------------------------------------------------------- #


def _fallback(book_id: int) -> dict:
    emoji = _STICKER_FALLBACK[book_id % len(_STICKER_FALLBACK)]
    return {"emoji": emoji, "label": "reader's pick", "custom": False}


def assign_sticker(
    conn: sqlite3.Connection, book_id: int, emoji: str, label: str = ""
) -> None:
    """Pin an emoji to a book.  Empty emoji clears the assignment."""
    book = conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        raise ValueError(f"book {book_id} not found")
    clean = (emoji or "").strip()
    if not clean:
        clear_sticker(conn, book_id)
        return
    if len(clean) > 4:
        raise ValueError("a sticker is one emoji, not a sentence")
    with transaction(conn):
        conn.execute(
            "INSERT INTO book_stickers (book_id, emoji, label, assigned_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(book_id) DO UPDATE SET "
            "emoji = excluded.emoji, label = excluded.label, "
            "assigned_at = excluded.assigned_at",
            (book_id, clean, (label or "").strip(), utc_now()),
        )


def clear_sticker(conn: sqlite3.Connection, book_id: int) -> None:
    """Drop a book's pinned sticker; the deterministic fallback returns."""
    with transaction(conn):
        conn.execute("DELETE FROM book_stickers WHERE book_id = ?", (book_id,))


def stickers_for(
    conn: sqlite3.Connection, book_ids: Iterable[int]
) -> dict[int, dict]:
    """Sticker (pinned or fallback) for each book id, as plain dicts."""
    ids = [int(book_id) for book_id in book_ids]
    out = {book_id: _fallback(book_id) for book_id in ids}
    if not ids:
        return out
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT book_id, emoji, label FROM book_stickers "
        f"WHERE book_id IN ({placeholders})",
        ids,
    ).fetchall()
    for row in rows:
        out[int(row["book_id"])] = {
            "emoji": row["emoji"],
            "label": row["label"],
            "custom": True,
        }
    return out


# --------------------------------------------------------------------------- #
# badges
# --------------------------------------------------------------------------- #


def _evaluate(
    *,
    books_passed: int,
    points: float,
    books_last_week: int,
    perfect_scores: int,
    goal: Goal | None,
) -> list[Badge]:
    """Turn a reader's counters into the badge catalog.

    Pure on purpose: every rule is testable without a database, and the catalog
    order is stable (earned first, then nearest to completion, then name).
    """
    goal_hit = bool(goal and points >= goal.target)
    catalog = [
        Badge(
            id="first_finish",
            name="First finish",
            description="Finish your first book.",
            icon="🌱",
            earned=books_passed >= 1,
            progress=min(books_passed, 1),
            target=1,
        ),
        Badge(
            id="bookworm",
            name="Bookworm",
            description="Finish 3 books.",
            icon="🐛",
            earned=books_passed >= 3,
            progress=min(books_passed, 3),
            target=3,
        ),
        Badge(
            id="shelf_hunter",
            name="Shelf hunter",
            description="Finish 10 books.",
            icon="📚",
            earned=books_passed >= 10,
            progress=min(books_passed, 10),
            target=10,
        ),
        Badge(
            id="century",
            name="Century club",
            description="Earn 100 points.",
            icon="💯",
            earned=points >= 100,
            progress=min(points, 100),
            target=100,
        ),
        Badge(
            id="week_warrior",
            name="Week warrior",
            description="Finish a book this week.",
            icon="🔥",
            earned=books_last_week >= 1,
            progress=min(books_last_week, 1),
            target=1,
        ),
        Badge(
            id="perfect_page",
            name="Perfect page",
            description="Score 100% on a quiz.",
            icon="⭐",
            earned=perfect_scores >= 1,
            progress=min(perfect_scores, 1),
            target=1,
        ),
    ]
    if goal is not None:
        catalog.append(
            Badge(
                id="goal_getter",
                name="Goal getter",
                description=f"Reach your {goal.target:g} point goal.",
                icon="🏁",
                earned=goal_hit,
                progress=min(points, goal.target),
                target=goal.target,
            )
        )

    # Earned first, then the catalog's own order.  Sorting by progress ratio
    # made two readers' badge strips reorder differently, which reads as a
    # layout bug on a shared page.
    ordered = sorted(
        enumerate(catalog), key=lambda pair: (not pair[1].earned, pair[0])
    )
    return [badge for _, badge in ordered]


def reader_counters(
    conn: sqlite3.Connection, reader_id: int, *, now: datetime | None = None
) -> dict:
    """The counters badges and reports are built from."""
    moment = _as_aware(now)
    week_ago = (moment - timedelta(days=7)).isoformat()

    points = conn.execute(
        "SELECT COALESCE(SUM(points), 0.0) AS total FROM points_ledger "
        "WHERE reader_id = ?",
        (reader_id,),
    ).fetchone()["total"]
    books_passed = conn.execute(
        "SELECT COUNT(*) AS n FROM points_ledger WHERE reader_id = ?",
        (reader_id,),
    ).fetchone()["n"]
    books_last_week = conn.execute(
        "SELECT COUNT(*) AS n FROM points_ledger "
        "WHERE reader_id = ? AND created_at >= ?",
        (reader_id, week_ago),
    ).fetchone()["n"]
    attempts = conn.execute(
        "SELECT COUNT(*) AS n FROM attempts "
        "WHERE reader_id = ? AND submitted_at IS NOT NULL",
        (reader_id,),
    ).fetchone()["n"]
    quiz_attempts = conn.execute(
        "SELECT COUNT(*) AS n FROM attempts "
        "WHERE reader_id = ? AND submitted_at IS NOT NULL AND kind = 'quiz'",
        (reader_id,),
    ).fetchone()["n"]
    perfect = conn.execute(
        "SELECT COUNT(*) AS n FROM attempts WHERE reader_id = ? "
        "AND submitted_at IS NOT NULL AND score >= 1.0",
        (reader_id,),
    ).fetchone()["n"]
    avg = conn.execute(
        "SELECT AVG(score) AS avg FROM attempts WHERE reader_id = ? "
        "AND submitted_at IS NOT NULL AND score IS NOT NULL",
        (reader_id,),
    ).fetchone()["avg"]

    return {
        "points": float(points or 0.0),
        "books_passed": int(books_passed),
        "books_last_week": int(books_last_week),
        "attempts": int(attempts),
        "quiz_attempts": int(quiz_attempts),
        "perfect_scores": int(perfect),
        "avg_score": round(float(avg) * 100, 1) if avg is not None else None,
    }


def badges_for(
    conn: sqlite3.Connection, reader_id: int, *, now: datetime | None = None
) -> list[Badge]:
    """Every badge for a reader, earned ones first."""
    counters = reader_counters(conn, reader_id, now=now)
    return _evaluate(
        books_passed=counters["books_passed"],
        points=counters["points"],
        books_last_week=counters["books_last_week"],
        perfect_scores=counters["perfect_scores"],
        goal=get_goal(conn, reader_id),
    )


# --------------------------------------------------------------------------- #
# weekly + report
# --------------------------------------------------------------------------- #


def weekly_points(
    conn: sqlite3.Connection,
    reader_id: int,
    *,
    weeks: int = 5,
    now: datetime | None = None,
) -> list[dict]:
    """Points earned in each of the last ``weeks`` seven-day windows."""
    moment = _as_aware(now)
    buckets: list[dict] = []
    for index in range(weeks - 1, -1, -1):
        start = moment - timedelta(days=(index + 1) * 7)
        end = moment - timedelta(days=index * 7)
        total = conn.execute(
            "SELECT COALESCE(SUM(points), 0.0) AS total FROM points_ledger "
            "WHERE reader_id = ? AND created_at >= ? AND created_at < ?",
            (reader_id, start.isoformat(), end.isoformat()),
        ).fetchone()["total"]
        buckets.append(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "label": f"{end.strftime('%b')} {end.day}",
                "points": float(total or 0.0),
            }
        )
    return buckets


def report_stats(
    conn: sqlite3.Connection,
    *,
    weeks: int = 5,
    now: datetime | None = None,
) -> list[dict]:
    """Per-reader growth numbers for the parent reports page."""
    moment = _as_aware(now)
    rows = conn.execute("SELECT id, name FROM readers ORDER BY lower(name), id").fetchall()
    reports: list[dict] = []
    for row in rows:
        reader_id = int(row["id"])
        counters = reader_counters(conn, reader_id, now=moment)
        goal = get_goal(conn, reader_id)
        weekly = weekly_points(conn, reader_id, weeks=weeks, now=moment)
        recent = conn.execute(
            "SELECT a.id, a.kind, a.score, a.passed, a.submitted_at, "
            "b.title, COALESCE(l.points, 0.0) AS points_awarded "
            "FROM attempts a "
            "LEFT JOIN quizzes q ON q.id = a.quiz_id "
            "JOIN books b ON b.id = COALESCE(q.book_id, a.book_id) "
            "LEFT JOIN points_ledger l ON l.attempt_id = a.id "
            "WHERE a.reader_id = ? AND a.submitted_at IS NOT NULL "
            "ORDER BY a.submitted_at DESC, a.id DESC LIMIT 6",
            (reader_id,),
        ).fetchall()
        reports.append(
            {
                "reader_id": reader_id,
                "name": row["name"],
                "points": counters["points"],
                "books_passed": counters["books_passed"],
                "attempts": counters["attempts"],
                "quiz_attempts": counters["quiz_attempts"],
                "avg_score": counters["avg_score"],
                "goal": goal,
                "goal_progress": goal_progress(counters["points"], goal),
                "badges": badges_for(conn, reader_id, now=moment),
                "weekly": weekly,
                "weekly_max": max((bucket["points"] for bucket in weekly), default=0.0) or 1.0,
                "recent": [dict(item) for item in recent],
            }
        )
    return reports


__all__ = [
    "DEFAULT_MILESTONES",
    "STICKERS",
    "assign_sticker",
    "badges_for",
    "clear_goal",
    "clear_sticker",
    "get_goal",
    "goal_progress",
    "milestones",
    "reader_counters",
    "report_stats",
    "set_goal",
    "stars",
    "stickers_for",
    "weekly_points",
]
