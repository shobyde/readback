"""Reading-log CSV export.

Columns are fixed by SPEC.md:
``reader,book,author,points,quiz_id,attempted_at,score,passed,points_awarded``.
``points`` is the book's value; ``points_awarded`` is what that attempt actually
earned from the ledger (``0.0`` unless it was the attempt that first passed).
"""

from __future__ import annotations

import csv
import io
import sqlite3

COLUMNS = (
    "reader",
    "book",
    "author",
    "points",
    "quiz_id",
    "attempted_at",
    "score",
    "passed",
    "points_awarded",
)


def export_reading_log_csv(
    conn: sqlite3.Connection, reader_id: int | None = None
) -> str:
    """Return the reading log as CSV text, newest attempt first."""
    sql = (
        "SELECT r.name AS reader, b.title AS book, b.author AS author, "
        "b.points AS points, a.quiz_id AS quiz_id, "
        "a.submitted_at AS attempted_at, a.score AS score, "
        "a.passed AS passed, COALESCE(l.points, 0.0) AS points_awarded "
        "FROM attempts a "
        "JOIN readers r ON r.id = a.reader_id "
        "JOIN quizzes q ON q.id = a.quiz_id "
        "JOIN books b ON b.id = q.book_id "
        "LEFT JOIN points_ledger l ON l.attempt_id = a.id "
        "WHERE a.submitted_at IS NOT NULL"
    )
    params: tuple = ()
    if reader_id is not None:
        sql += " AND a.reader_id = ?"
        params = (reader_id,)
    sql += " ORDER BY a.submitted_at DESC, a.id DESC"

    rows = conn.execute(sql, params).fetchall()

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        score = row["score"]
        writer.writerow(
            [
                row["reader"],
                row["book"],
                row["author"],
                row["points"],
                row["quiz_id"],
                row["attempted_at"],
                "" if score is None else score,
                1 if row["passed"] else 0,
                row["points_awarded"],
            ]
        )
    return buffer.getvalue()


__all__ = ["COLUMNS", "export_reading_log_csv"]
