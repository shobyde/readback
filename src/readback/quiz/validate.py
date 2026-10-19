"""Structural validation for generated quizzes.

``validate_quiz`` returns human-readable problems so both the generator (which
feeds them back to the model as a repair instruction) and the review gate can
use the same rules.  An empty list means the quiz is usable.
"""

from __future__ import annotations

from typing import Any

from ..models import GeneratedQuiz, Quiz

CHOICES_PER_QUESTION = 4


def validate_quiz(
    quiz: GeneratedQuiz | Quiz,
    expected_questions: int | None = None,
) -> list[str]:
    """Return every structural problem with ``quiz`` (empty means valid)."""
    problems: list[str] = []

    questions = getattr(quiz, "questions", None)
    if not isinstance(questions, list):
        return ["quiz has no questions"]

    if not questions:
        problems.append("quiz has no questions")
    elif expected_questions is not None and len(questions) != expected_questions:
        problems.append(
            f"expected {expected_questions} questions, got {len(questions)}"
        )

    for index, question in enumerate(questions, start=1):
        problems.extend(_question_problems(index, question))

    return problems


def _question_problems(index: int, question: Any) -> list[str]:
    problems: list[str] = []

    prompt = getattr(question, "prompt", None)
    if not isinstance(prompt, str) or not prompt.strip():
        problems.append(f"question {index}: prompt is empty")

    choices = getattr(question, "choices", None)
    if not isinstance(choices, (list, tuple)):
        problems.append(f"question {index}: choices must be a list of 4 strings")
        choices = []

    if len(choices) != CHOICES_PER_QUESTION:
        problems.append(
            f"question {index}: must have exactly {CHOICES_PER_QUESTION} choices, "
            f"got {len(choices)}"
        )

    cleaned: list[str] = []
    for position, choice in enumerate(choices, start=1):
        if not isinstance(choice, str) or not choice.strip():
            problems.append(f"question {index}: choice {position} is empty")
            continue
        cleaned.append(choice.strip())

    if len(cleaned) == len(choices) and len(set(cleaned)) != len(cleaned):
        problems.append(f"question {index}: choices must be distinct")

    answer_index = getattr(question, "answer_index", None)
    if isinstance(answer_index, bool) or not isinstance(answer_index, int):
        problems.append(f"question {index}: answer_index must be an integer 0-3")
    elif not 0 <= answer_index < CHOICES_PER_QUESTION:
        problems.append(
            f"question {index}: answer_index {answer_index} is outside 0-3"
        )

    return problems


__all__ = ["CHOICES_PER_QUESTION", "validate_quiz"]
