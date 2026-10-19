"""Plain dataclasses shared across Readback.

The SPEC calls these frozen: rows are read out of SQLite and should not be
mutated in place.  ``ImportResult`` is the one exception — ``books.import_books``
accumulates counters while walking the CSV — so it stays mutable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    """UTC ISO-8601 timestamp used for every ``*_at`` column."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Book:
    id: int
    title: str
    author: str = ""
    isbn: str | None = None
    points: float = 0.0
    grade_level: str = ""
    notes: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class Reader:
    id: int
    name: str
    created_at: str = ""


@dataclass(frozen=True)
class Question:
    id: int
    quiz_id: int
    ordinal: int
    prompt: str
    choices: list[str]
    answer_index: int
    explanation: str = ""


@dataclass(frozen=True)
class Quiz:
    id: int
    book_id: int
    status: str
    model: str = ""
    source_notes: str = ""
    created_at: str = ""
    approved_at: str | None = None
    approved_by: str | None = None
    questions: list[Question] = field(default_factory=list)


@dataclass(frozen=True)
class GeneratedQuestion:
    prompt: str
    choices: list[str]
    answer_index: int
    explanation: str = ""


@dataclass(frozen=True)
class GeneratedQuiz:
    questions: list[GeneratedQuestion]


@dataclass
class ImportResult:
    """Mutable counter bag returned by ``books.import_books``."""

    inserted: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AttemptResult:
    attempt_id: int
    score: float
    passed: bool
    correct: int
    total: int
    points_awarded: float = 0.0
    points_total: float = 0.0


@dataclass(frozen=True)
class ReaderSummary:
    reader_id: int
    name: str
    points_total: float = 0.0
    books_passed: int = 0
    attempts: int = 0


__all__ = [
    "AttemptResult",
    "Book",
    "GeneratedQuestion",
    "GeneratedQuiz",
    "ImportResult",
    "Question",
    "Quiz",
    "Reader",
    "ReaderSummary",
    "utc_now",
]
