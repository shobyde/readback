"""Quiz attempts, scoring, the points ledger, and reader summaries.

Points are awarded exactly once per (reader, book): the first submitted attempt
that passes inserts into ``points_ledger``.  ``UNIQUE (reader_id, book_id)`` is
the guard, and ``INSERT OR IGNORE`` makes the award idempotent even if a second
passing attempt raced in.
"""

from __future__ import annotations

import sqlite3

from .db import transaction
from .models import AttemptResult, ReaderSummary, utc_now


def _points_total(conn: sqlite3.Connection, reader_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(points), 0.0) AS total "
        "FROM points_ledger WHERE reader_id = ?",
        (reader_id,),
    ).fetchone()
    return float(row["total"] or 0.0)


def _attempt_points(conn: sqlite3.Connection, attempt_id: int) -> float:
    row = conn.execute(
        "SELECT points FROM points_ledger WHERE attempt_id = ?", (attempt_id,)
    ).fetchone()
    return float(row["points"]) if row is not None else 0.0


# --------------------------------------------------------------------------- #
# attempts
# --------------------------------------------------------------------------- #


def start_attempt(conn: sqlite3.Connection, quiz_id: int, reader_id: int) -> int:
    """Open an attempt against an approved quiz and return its id.

    ``total_questions`` is snapshotted here so a later edit to the quiz cannot
    change an attempt already in flight.  Non-approved quizzes are refused.
    """
    quiz = conn.execute(
        "SELECT id, status, book_id FROM quizzes WHERE id = ?", (quiz_id,)
    ).fetchone()
    if quiz is None:
        raise ValueError(f"quiz {quiz_id} not found")
    if quiz["status"] != "approved":
        raise ValueError(f"quiz {quiz_id} is not approved")

    reader = conn.execute(
        "SELECT id FROM readers WHERE id = ?", (reader_id,)
    ).fetchone()
    if reader is None:
        raise ValueError(f"reader {reader_id} not found")

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM questions WHERE quiz_id = ?", (quiz_id,)
    ).fetchone()["n"]

    with transaction(conn):
        cursor = conn.execute(
            "INSERT INTO attempts "
            "(quiz_id, reader_id, started_at, total_questions, correct_answers) "
            "VALUES (?, ?, ?, ?, 0)",
            (quiz_id, reader_id, utc_now(), int(total)),
        )
    return int(cursor.lastrowid)


def _coerce_choice(value: object) -> int:
    """Turn a submitted choice into an index, or -1 when it is unusable."""
    if isinstance(value, bool) or value is None:
        return -1
    if isinstance(value, int):
        index = value
    elif isinstance(value, float) and value.is_integer():
        index = int(value)
    elif isinstance(value, str):
        try:
            index = int(value.strip())
        except ValueError:
            return -1
    else:
        return -1
    return index if 0 <= index <= 3 else -1


def _normalise_answers(answers: object) -> dict[int, int]:
    normalised: dict[int, int] = {}
    if not isinstance(answers, dict):
        return normalised
    for key, value in answers.items():
        try:
            question_id = int(str(key))
        except (TypeError, ValueError):
            continue
        normalised[question_id] = _coerce_choice(value)
    return normalised


def _existing_result(conn: sqlite3.Connection, attempt: sqlite3.Row) -> AttemptResult:
    """Rebuild the result of an attempt that was already submitted."""
    return AttemptResult(
        attempt_id=attempt["id"],
        score=float(attempt["score"] or 0.0),
        passed=bool(attempt["passed"]),
        correct=int(attempt["correct_answers"] or 0),
        total=int(attempt["total_questions"] or 0),
        points_awarded=_attempt_points(conn, attempt["id"]),
        points_total=_points_total(conn, attempt["reader_id"]),
    )


def submit_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    answers: dict[int, int],
    *,
    pass_threshold: float = 0.8,
) -> AttemptResult:
    """Score an attempt, store its answers, and award points on a first pass.

    ``answers`` maps ``question_id`` to a 0-based choice index.  Missing or
    unparseable answers are recorded as incorrect.  Submitting an attempt that
    was already submitted returns the stored result without rescoring.
    """
    attempt = conn.execute(
        "SELECT * FROM attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    if attempt is None:
        raise ValueError(f"attempt {attempt_id} not found")
    if attempt["submitted_at"] is not None:
        return _existing_result(conn, attempt)

    quiz = conn.execute(
        "SELECT id, book_id FROM quizzes WHERE id = ?", (attempt["quiz_id"],)
    ).fetchone()
    if quiz is None:
        raise ValueError(f"quiz {attempt['quiz_id']} not found")

    questions = conn.execute(
        "SELECT id, answer_index FROM questions WHERE quiz_id = ? "
        "ORDER BY ordinal, id",
        (attempt["quiz_id"],),
    ).fetchall()

    submitted = _normalise_answers(answers)
    rows: list[tuple[int, int, int]] = []
    correct = 0
    for question in questions:
        choice = submitted.get(question["id"], -1)
        is_correct = 1 if choice == question["answer_index"] else 0
        correct += is_correct
        rows.append((question["id"], choice, is_correct))

    total = int(attempt["total_questions"] or 0) or len(questions)
    score = round(correct / total, 3) if total else 0.0
    passed = score >= pass_threshold

    with transaction(conn):
        conn.execute(
            "UPDATE attempts SET submitted_at = ?, score = ?, passed = ?, "
            "correct_answers = ? WHERE id = ?",
            (utc_now(), score, 1 if passed else 0, correct, attempt_id),
        )
        for question_id, choice, is_correct in rows:
            conn.execute(
                "INSERT OR IGNORE INTO attempt_answers "
                "(attempt_id, question_id, choice_index, correct) "
                "VALUES (?, ?, ?, ?)",
                (attempt_id, question_id, choice, is_correct),
            )

        points_awarded = 0.0
        if passed:
            book = conn.execute(
                "SELECT points FROM books WHERE id = ?", (quiz["book_id"],)
            ).fetchone()
            book_points = float(book["points"]) if book is not None else 0.0
            cursor = conn.execute(
                "INSERT OR IGNORE INTO points_ledger "
                "(reader_id, book_id, attempt_id, points, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    attempt["reader_id"],
                    quiz["book_id"],
                    attempt_id,
                    book_points,
                    utc_now(),
                ),
            )
            if cursor.rowcount == 1:
                points_awarded = book_points

        points_total = _points_total(conn, attempt["reader_id"])

    return AttemptResult(
        attempt_id=attempt_id,
        score=score,
        passed=passed,
        correct=correct,
        total=total,
        points_awarded=points_awarded,
        points_total=points_total,
    )


# --------------------------------------------------------------------------- #
# summaries
# --------------------------------------------------------------------------- #


def reader_summary(conn: sqlite3.Connection, reader_id: int) -> ReaderSummary:
    """Point total, books passed, and submitted attempts for one reader."""
    reader = conn.execute(
        "SELECT id, name FROM readers WHERE id = ?", (reader_id,)
    ).fetchone()
    if reader is None:
        raise ValueError(f"reader {reader_id} not found")

    books_passed = conn.execute(
        "SELECT COUNT(*) AS n FROM points_ledger WHERE reader_id = ?",
        (reader_id,),
    ).fetchone()["n"]
    attempts = conn.execute(
        "SELECT COUNT(*) AS n FROM attempts "
        "WHERE reader_id = ? AND submitted_at IS NOT NULL",
        (reader_id,),
    ).fetchone()["n"]

    return ReaderSummary(
        reader_id=reader_id,
        name=reader["name"],
        points_total=_points_total(conn, reader_id),
        books_passed=int(books_passed),
        attempts=int(attempts),
    )


def all_summaries(conn: sqlite3.Connection) -> list[ReaderSummary]:
    """Every reader's summary, ordered by name."""
    rows = conn.execute(
        "SELECT id FROM readers ORDER BY lower(name), id"
    ).fetchall()
    return [reader_summary(conn, row["id"]) for row in rows]


def recent_attempts(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    """Newest submitted attempts as plain dicts.

    Keys: ``attempt_id``, ``reader_id``, ``reader_name``, ``quiz_id``,
    ``book_id``, ``book_title``, ``score``, ``passed``, ``correct``, ``total``,
    ``submitted_at``, ``points_awarded``.
    """
    rows = conn.execute(
        "SELECT a.id AS attempt_id, a.reader_id, r.name AS reader_name, "
        "a.quiz_id, q.book_id, b.title AS book_title, a.score, a.passed, "
        "a.correct_answers, a.total_questions, a.submitted_at, "
        "COALESCE(l.points, 0.0) AS points_awarded "
        "FROM attempts a "
        "JOIN readers r ON r.id = a.reader_id "
        "JOIN quizzes q ON q.id = a.quiz_id "
        "JOIN books b ON b.id = q.book_id "
        "LEFT JOIN points_ledger l ON l.attempt_id = a.id "
        "WHERE a.submitted_at IS NOT NULL "
        "ORDER BY a.submitted_at DESC, a.id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [
        {
            "attempt_id": row["attempt_id"],
            "reader_id": row["reader_id"],
            "reader_name": row["reader_name"],
            "quiz_id": row["quiz_id"],
            "book_id": row["book_id"],
            "book_title": row["book_title"],
            "score": float(row["score"] or 0.0),
            "passed": bool(row["passed"]),
            "correct": int(row["correct_answers"] or 0),
            "total": int(row["total_questions"] or 0),
            "submitted_at": row["submitted_at"],
            "points_awarded": float(row["points_awarded"] or 0.0),
        }
        for row in rows
    ]


__all__ = [
    "all_summaries",
    "reader_summary",
    "recent_attempts",
    "start_attempt",
    "submit_attempt",
]
