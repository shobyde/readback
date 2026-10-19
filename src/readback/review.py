"""Parent review gate: draft queue, question edits, approve/reject.

Nothing reaches a reader until ``approve_quiz`` has validated every question.
The structural rules live in ``quiz.validate``; this module keeps a local
equivalent as a fallback so review works even while that sibling module is
being built, and re-checks the same rules before writing an edit.
"""

from __future__ import annotations

import json
import sqlite3

from .db import transaction
from .models import Question, Quiz, utc_now
from .quiz.validate import validate_quiz

ANSWER_CHOICES = 4


# --------------------------------------------------------------------------- #
# row helpers
# --------------------------------------------------------------------------- #


def _question_from_row(row: sqlite3.Row) -> Question:
    try:
        choices = json.loads(row["choices_json"])
    except (TypeError, ValueError):
        choices = []
    if not isinstance(choices, list):
        choices = []
    return Question(
        id=row["id"],
        quiz_id=row["quiz_id"],
        ordinal=row["ordinal"],
        prompt=row["prompt"],
        choices=[str(choice) for choice in choices],
        answer_index=row["answer_index"],
        explanation=row["explanation"],
    )


def _questions_for(conn: sqlite3.Connection, quiz_id: int) -> list[Question]:
    rows = conn.execute(
        "SELECT * FROM questions WHERE quiz_id = ? ORDER BY ordinal, id",
        (quiz_id,),
    ).fetchall()
    return [_question_from_row(row) for row in rows]


def _quiz_from_row(row: sqlite3.Row, questions: list[Question]) -> Quiz:
    return Quiz(
        id=row["id"],
        book_id=row["book_id"],
        status=row["status"],
        model=row["model"],
        source_notes=row["source_notes"],
        created_at=row["created_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        questions=questions,
    )


def _fetch_quiz_row(conn: sqlite3.Connection, quiz_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM quizzes WHERE id = ?", (quiz_id,)
    ).fetchone()


# --------------------------------------------------------------------------- #
# structural validation
# --------------------------------------------------------------------------- #


def _validation_problems(quiz: Quiz) -> list[str]:
    """The shared structural rules from ``quiz.validate``."""
    return [str(problem) for problem in validate_quiz(quiz)]


def _problems_error(problems: list[str]) -> ValueError:
    error = ValueError("; ".join(problems))
    error.problems = list(problems)  # type: ignore[attr-defined]
    return error


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def list_drafts(conn: sqlite3.Connection) -> list[Quiz]:
    """Draft quizzes, newest first, with their questions loaded."""
    rows = conn.execute(
        "SELECT * FROM quizzes WHERE status = 'draft' "
        "ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [_quiz_from_row(row, _questions_for(conn, row["id"])) for row in rows]


def get_quiz(conn: sqlite3.Connection, quiz_id: int) -> Quiz | None:
    """One quiz with questions ordered by ordinal, or ``None``."""
    row = _fetch_quiz_row(conn, quiz_id)
    if row is None:
        return None
    return _quiz_from_row(row, _questions_for(conn, quiz_id))


def list_approved(
    conn: sqlite3.Connection, book_id: int | None = None
) -> list[Quiz]:
    """Approved quizzes with questions, newest first, optionally per book."""
    if book_id is None:
        rows = conn.execute(
            "SELECT * FROM quizzes WHERE status = 'approved' "
            "ORDER BY created_at DESC, id DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM quizzes WHERE status = 'approved' AND book_id = ? "
            "ORDER BY created_at DESC, id DESC",
            (book_id,),
        ).fetchall()
    return [_quiz_from_row(row, _questions_for(conn, row["id"])) for row in rows]


# --------------------------------------------------------------------------- #
# edits
# --------------------------------------------------------------------------- #


def _clean_prompt(value: object) -> str:
    prompt = "" if value is None else str(value).strip()
    if not prompt:
        raise ValueError("prompt must not be empty")
    return prompt


def _clean_choices(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or value is None:
        raise ValueError("choices must be a list of four strings")
    try:
        choices = list(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("choices must be a list of four strings") from None
    if len(choices) != ANSWER_CHOICES:
        raise ValueError(f"choices must contain exactly {ANSWER_CHOICES} entries")
    cleaned: list[str] = []
    for choice in choices:
        text = "" if choice is None else str(choice).strip()
        if not text:
            raise ValueError("choices must all be non-empty")
        cleaned.append(text)
    if len(set(cleaned)) != ANSWER_CHOICES:
        raise ValueError("choices must be distinct")
    return cleaned


def _clean_answer_index(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise ValueError("answer_index must be an integer between 0 and 3")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("answer_index must be an integer between 0 and 3")
        index = int(value)
    elif isinstance(value, int):
        index = value
    elif isinstance(value, str):
        try:
            index = int(value.strip())
        except ValueError:
            raise ValueError(
                "answer_index must be an integer between 0 and 3"
            ) from None
    else:
        raise ValueError("answer_index must be an integer between 0 and 3")
    if not 0 <= index < ANSWER_CHOICES:
        raise ValueError("answer_index must be between 0 and 3")
    return index


def update_question(
    conn: sqlite3.Connection,
    question_id: int,
    *,
    prompt: object = None,
    choices: object = None,
    answer_index: object = None,
    explanation: object = None,
) -> None:
    """Edit a question, validating the merged row before writing.

    Only the arguments that are not ``None`` change.  Any invalid value raises
    ``ValueError`` and leaves the stored row untouched.
    """
    row = conn.execute(
        "SELECT * FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"question {question_id} not found")

    current = _question_from_row(row)
    new_prompt = current.prompt if prompt is None else _clean_prompt(prompt)
    new_choices = current.choices if choices is None else _clean_choices(choices)
    new_answer = (
        current.answer_index
        if answer_index is None
        else _clean_answer_index(answer_index)
    )
    new_explanation = (
        current.explanation if explanation is None else str(explanation)
    )

    # Defence in depth: the merged row must still satisfy the structural rules.
    if not new_prompt.strip():
        raise ValueError("prompt must not be empty")
    if len(new_choices) != ANSWER_CHOICES:
        raise ValueError(f"choices must contain exactly {ANSWER_CHOICES} entries")
    if len(set(new_choices)) != ANSWER_CHOICES:
        raise ValueError("choices must be distinct")
    if not 0 <= new_answer < ANSWER_CHOICES:
        raise ValueError("answer_index must be between 0 and 3")

    with transaction(conn):
        conn.execute(
            "UPDATE questions SET prompt = ?, choices_json = ?, "
            "answer_index = ?, explanation = ? WHERE id = ?",
            (
                new_prompt,
                json.dumps(new_choices),
                new_answer,
                new_explanation,
                question_id,
            ),
        )


def delete_question(conn: sqlite3.Connection, question_id: int) -> None:
    """Delete a question and renumber the quiz's questions from 1."""
    row = conn.execute(
        "SELECT quiz_id FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"question {question_id} not found")
    quiz_id = row["quiz_id"]

    with transaction(conn):
        conn.execute("DELETE FROM questions WHERE id = ?", (question_id,))
        remaining = conn.execute(
            "SELECT id FROM questions WHERE quiz_id = ? ORDER BY ordinal, id",
            (quiz_id,),
        ).fetchall()
        # Shift out of the 1..N range first so UNIQUE (quiz_id, ordinal)
        # never sees a collision while renumbering.
        conn.execute(
            "UPDATE questions SET ordinal = ordinal + 1000000 WHERE quiz_id = ?",
            (quiz_id,),
        )
        for new_ordinal, question in enumerate(remaining, start=1):
            conn.execute(
                "UPDATE questions SET ordinal = ? WHERE id = ?",
                (new_ordinal, question["id"]),
            )


# --------------------------------------------------------------------------- #
# approval
# --------------------------------------------------------------------------- #


def approve_quiz(
    conn: sqlite3.Connection, quiz_id: int, actor: str = "parent"
) -> None:
    """Approve a quiz once nobody is looking at it unresolved.

    Raises ``ValueError`` carrying the human-readable problem list when the
    quiz has no questions or any question fails validation.  The problems are
    also available as ``exc.problems``.
    """
    quiz = get_quiz(conn, quiz_id)
    if quiz is None:
        raise ValueError(f"quiz {quiz_id} not found")
    if not quiz.questions:
        raise _problems_error(["quiz has no questions"])

    problems = _validation_problems(quiz)
    if problems:
        raise _problems_error(problems)

    with transaction(conn):
        conn.execute(
            "UPDATE quizzes SET status = 'approved', approved_at = ?, "
            "approved_by = ? WHERE id = ?",
            (utc_now(), actor, quiz_id),
        )


def reject_quiz(
    conn: sqlite3.Connection,
    quiz_id: int,
    actor: str = "parent",
    note: str = "",
) -> None:
    """Mark a quiz rejected, appending the note to ``source_notes``."""
    quiz = get_quiz(conn, quiz_id)
    if quiz is None:
        raise ValueError(f"quiz {quiz_id} not found")

    notes = quiz.source_notes
    text = str(note or "").strip()
    if text:
        notes = f"{notes}\n{text}" if notes else text

    with transaction(conn):
        conn.execute(
            "UPDATE quizzes SET status = 'rejected', source_notes = ? "
            "WHERE id = ?",
            (notes, quiz_id),
        )


__all__ = [
    "approve_quiz",
    "delete_question",
    "get_quiz",
    "list_approved",
    "list_drafts",
    "reject_quiz",
    "update_question",
]
