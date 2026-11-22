"""Rewards: stars, goals, badges, stickers, weekly buckets, reports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from readback import rewards
from readback.books import add_book
from readback.points import reader_summary

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def award(conn, reader, book, value, *, days_ago=0.0, score=1.0) -> None:
    """Write one finished reading event of ``book`` for ``reader``."""
    when = (NOW - timedelta(days=days_ago)).isoformat()
    cursor = conn.execute(
        "INSERT INTO attempts (quiz_id, book_id, reader_id, kind, started_at, "
        "submitted_at, score, passed, total_questions, correct_answers) "
        "VALUES (NULL, ?, ?, 'manual', ?, ?, ?, 1, 0, 0)",
        (book.id, reader.id, when, when, score),
    )
    conn.execute(
        "INSERT INTO points_ledger (reader_id, book_id, attempt_id, points, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (reader.id, book.id, int(cursor.lastrowid), float(value), when),
    )
    conn.commit()


def fresh_book(conn, title: str, points: float = 5.0):
    from readback.models import Book

    book_id = add_book(conn, title=title, author="A. Writer", points=points)
    return Book(id=book_id, title=title, author="A. Writer", points=points)


# --------------------------------------------------------------------------- #
# stars
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("correct", "total", "expected"),
    [(10, 10, 3), (9, 10, 3), (7, 10, 2), (6, 10, 0), (5, 10, 0), (0, 10, 0), (0, 0, 0)],
)
def test_stars_follow_the_score_bands(correct, total, expected):
    assert rewards.stars(correct, total) == expected


def test_stars_give_one_for_a_pass_at_a_lower_threshold():
    assert rewards.stars(6, 10, pass_threshold=0.6) == 1


def test_stars_respect_a_stricter_pass_threshold():
    assert rewards.stars(6, 10, pass_threshold=0.9) == 0
    assert rewards.stars(7, 10, pass_threshold=0.9) == 2


# --------------------------------------------------------------------------- #
# goals
# --------------------------------------------------------------------------- #


def test_milestones_default_ladder():
    assert rewards.milestones(None) == list(rewards.DEFAULT_MILESTONES)


def test_milestones_for_a_goal():
    assert rewards.milestones(100) == [25, 50, 75, 100]
    assert rewards.milestones(30) == [25, 30]
    assert rewards.milestones(10) == [10]


def test_goal_progress_without_a_goal():
    progress = rewards.goal_progress(40, None)
    assert progress["status"] == "no_goal"
    assert progress["percent"] == 0.0


def test_goal_progress_on_track_and_complete():
    on_track = rewards.goal_progress(40, 100)
    assert on_track["status"] == "on_track"
    assert on_track["percent"] == 40.0
    assert on_track["current"] == 25
    assert on_track["next"] == 50
    assert on_track["remaining"] == 60.0

    complete = rewards.goal_progress(120, 100)
    assert complete["status"] == "complete"
    assert complete["percent"] == 100.0
    assert complete["next"] is None
    assert complete["remaining"] == 0.0


def test_set_get_and_clear_goal(conn, reader):
    goal = rewards.set_goal(conn, reader.id, 100)
    assert goal.target == 100.0
    assert rewards.get_goal(conn, reader.id) == goal

    rewards.set_goal(conn, reader.id, 50.5)
    assert rewards.get_goal(conn, reader.id).target == 50.5

    rewards.clear_goal(conn, reader.id)
    assert rewards.get_goal(conn, reader.id) is None
    rewards.clear_goal(conn, reader.id)  # idempotent


def test_set_goal_validates(conn, reader):
    with pytest.raises(ValueError):
        rewards.set_goal(conn, reader.id, 0)
    with pytest.raises(ValueError):
        rewards.set_goal(conn, reader.id, "many")
    with pytest.raises(ValueError):
        rewards.set_goal(conn, 9999, 10)


# --------------------------------------------------------------------------- #
# stickers
# --------------------------------------------------------------------------- #


def test_stickers_fall_back_to_a_deterministic_emoji(conn, book):
    stickers = rewards.stickers_for(conn, [book.id])
    first = stickers[book.id]
    assert first["custom"] is False
    assert first["emoji"]
    assert rewards.stickers_for(conn, [book.id])[book.id] == first


def test_assigning_and_clearing_a_sticker(conn, book):
    rewards.assign_sticker(conn, book.id, "🦊", "fox")
    pinned = rewards.stickers_for(conn, [book.id])[book.id]
    assert pinned == {"emoji": "🦊", "label": "fox", "custom": True}

    rewards.assign_sticker(conn, book.id, "")
    assert rewards.stickers_for(conn, [book.id])[book.id]["custom"] is False


def test_assign_sticker_validates(conn, book):
    with pytest.raises(ValueError):
        rewards.assign_sticker(conn, book.id, "🦊" * 5)
    with pytest.raises(ValueError):
        rewards.assign_sticker(conn, 9999, "🦊")


# --------------------------------------------------------------------------- #
# badges
# --------------------------------------------------------------------------- #


def test_first_finish_and_bookworm_progress(conn, reader, book):
    award(conn, reader, book, 5)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["first_finish"].earned is True
    assert badges["bookworm"].earned is False
    assert badges["bookworm"].progress == 1
    assert badges["bookworm"].target == 3


def test_bookworm_and_century_unlock(conn, reader):
    for index in range(3):
        award(conn, reader, fresh_book(conn, f"Book {index}", 40), 40)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["bookworm"].earned is True
    assert badges["century"].earned is True


def test_week_warrior_only_counts_the_last_seven_days(conn, reader):
    award(conn, reader, fresh_book(conn, "Old read"), 5, days_ago=10)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["week_warrior"].earned is False

    award(conn, reader, fresh_book(conn, "Fresh read"), 5, days_ago=2)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["week_warrior"].earned is True


def test_perfect_page_needs_a_full_score(conn, reader, book):
    award(conn, reader, book, 5, score=0.8)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["perfect_page"].earned is False

    award(conn, reader, fresh_book(conn, "Flawless"), 5, score=1.0)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["perfect_page"].earned is True


def test_goal_getter_appears_only_with_a_goal(conn, reader, book):
    award(conn, reader, book, 5)
    ids = {badge.id for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert "goal_getter" not in ids

    rewards.set_goal(conn, reader.id, 5)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["goal_getter"].earned is True

    rewards.set_goal(conn, reader.id, 100)
    badges = {badge.id: badge for badge in rewards.badges_for(conn, reader.id, now=NOW)}
    assert badges["goal_getter"].earned is False
    assert badges["goal_getter"].target == 100


def test_badges_are_sorted_earned_first(conn, reader, book):
    award(conn, reader, book, 5)
    earned = [badge.earned for badge in rewards.badges_for(conn, reader.id, now=NOW)]
    assert earned == sorted(earned, reverse=True)


# --------------------------------------------------------------------------- #
# weekly buckets + reports
# --------------------------------------------------------------------------- #


def test_weekly_points_buckets_by_seven_day_window(conn, reader):
    award(conn, reader, fresh_book(conn, "This week"), 5, days_ago=1)
    award(conn, reader, fresh_book(conn, "Last week"), 10, days_ago=8)
    award(conn, reader, fresh_book(conn, "Three weeks"), 15, days_ago=20)
    award(conn, reader, fresh_book(conn, "Ancient"), 20, days_ago=40)

    buckets = rewards.weekly_points(conn, reader.id, weeks=5, now=NOW)
    assert [bucket["points"] for bucket in buckets] == [0, 0, 15, 10, 5]
    assert all(bucket["label"] for bucket in buckets)


def test_reader_counters(conn, reader, book):
    award(conn, reader, book, 12, score=1.0)
    award(conn, reader, fresh_book(conn, "Second"), 8, score=0.6)
    counters = rewards.reader_counters(conn, reader.id, now=NOW)
    assert counters["points"] == 20
    assert counters["books_passed"] == 2
    assert counters["attempts"] == 2
    assert counters["quiz_attempts"] == 0
    assert counters["perfect_scores"] == 1
    assert counters["avg_score"] == 80.0


def test_report_stats_assembles_every_reader(conn, reader):
    award(conn, reader, fresh_book(conn, "One"), 10, days_ago=1)
    rewards.set_goal(conn, reader.id, 100)

    report = rewards.report_stats(conn, now=NOW)[0]
    assert report["name"] == reader.name
    assert report["points"] == 10
    assert report["goal"].target == 100
    assert report["goal_progress"]["percent"] == 10.0
    assert len(report["weekly"]) == 5
    assert report["weekly_max"] == 10
    assert report["recent"][0]["title"] == "One"
    assert any(badge.earned for badge in report["badges"])


def test_summary_still_counts_manual_events(conn, reader, book):
    award(conn, reader, book, 7)
    summary = reader_summary(conn, reader.id)
    assert summary.points_total == 7
    assert summary.books_passed == 1
