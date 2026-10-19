"""Prompt text for the quiz generator.

The model gets one system message with the writing rules and one user message
with the book facts plus the exact JSON shape.  Keeping the schema in a single
constant lets the repair turn reuse the same wording.
"""

from __future__ import annotations

from ..models import Book

JSON_SCHEMA_TEXT = (
    '{"questions": [{"prompt": "...", '
    '"choices": ["...", "...", "...", "..."], '
    '"answer_index": 0, "explanation": "..."}]}'
)

SYSTEM_PROMPT = """\
You write reading comprehension quizzes for a family reading app.

Rules:
- Every question must be answerable by someone who has read the book itself.
  Use only facts that come from reading the story; do not invent details.
- Questions must be age appropriate for the stated grade level, with
  vocabulary and situations suitable for children.
- Each question has exactly four choices. The four choices must be non-empty,
  distinct, and plausible. Exactly one choice must be unambiguously correct.
- "answer_index" is the zero-based index (0, 1, 2, or 3) of the correct choice.
- "explanation" is one sentence explaining why the answer is correct.
- Never ask about a specific edition, printing, cover, ISBN, page number,
  pagination, page count, chapter numbering, or anything else that changes
  between editions. Ask about plot, characters, setting, and themes instead.
- Write exactly the number of questions requested.
- Reply with strict JSON only, matching the schema in the user message. No
  markdown code fences, no commentary, and no text before or after the JSON."""


def build_prompt(book: Book, num_questions: int) -> list[dict[str, str]]:
    """Build the ``messages`` payload for one quiz request.

    Returns two chat messages (``system`` then ``user``) in the OpenAI shape.
    """
    count = _question_count(num_questions)

    facts = [
        f"Title: {book.title.strip()}",
        f"Author: {(book.author or '').strip() or 'Unknown'}",
    ]
    grade_level = (book.grade_level or "").strip()
    if grade_level:
        facts.append(f"Grade level: {grade_level}")
    facts.append(f"Points: {book.points:g}")

    lines = [
        f"Write {count} multiple-choice reading comprehension questions "
        f"about the book below.",
        "",
        *facts,
    ]

    notes = (book.notes or "").strip()
    if notes:
        lines.extend(["", "Parent notes about this book:", notes])

    lines.extend(
        [
            "",
            "Return a JSON object in exactly this shape:",
            JSON_SCHEMA_TEXT,
            "",
            f"Return exactly {count} question object(s) in the \"questions\" list.",
        ]
    )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def _question_count(num_questions: int) -> int:
    if isinstance(num_questions, bool) or not isinstance(num_questions, int):
        raise ValueError(f"num_questions must be an integer, got {num_questions!r}")
    if num_questions < 1:
        raise ValueError(f"num_questions must be at least 1, got {num_questions}")
    return num_questions


__all__ = ["JSON_SCHEMA_TEXT", "SYSTEM_PROMPT", "build_prompt"]
