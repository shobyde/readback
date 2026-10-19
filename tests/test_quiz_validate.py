"""Structural validation rules for generated quizzes."""

from __future__ import annotations

from readback.models import GeneratedQuestion, GeneratedQuiz, Question, Quiz
from readback.quiz.validate import validate_quiz


def make_question(
    prompt: str = "Who is the main character?",
    choices: list[str] | None = None,
    answer_index: int = 0,
    explanation: str = "The story follows that character.",
) -> GeneratedQuestion:
    return GeneratedQuestion(
        prompt=prompt,
        choices=["Bilbo", "Gandalf", "Smaug", "Thorin"] if choices is None else choices,
        answer_index=answer_index,
        explanation=explanation,
    )


def make_quiz(count: int = 3) -> GeneratedQuiz:
    return GeneratedQuiz(
        questions=[make_question(prompt=f"Question {index}?") for index in range(1, count + 1)]
    )


def test_valid_quiz_has_no_problems():
    assert validate_quiz(make_quiz()) == []


def test_valid_quiz_respects_expected_count():
    assert validate_quiz(make_quiz(5), expected_questions=5) == []


def test_valid_quiz_dataclass_is_accepted():
    quiz = Quiz(
        id=1,
        book_id=1,
        status="draft",
        questions=[
            Question(
                id=1,
                quiz_id=1,
                ordinal=1,
                prompt="Where does the story begin?",
                choices=["The Shire", "Rivendell", "Mordor", "Gondor"],
                answer_index=0,
                explanation="Bilbo lives in the Shire.",
            )
        ],
    )
    assert validate_quiz(quiz) == []


def test_empty_quiz_is_reported():
    problems = validate_quiz(GeneratedQuiz(questions=[]))
    assert problems == ["quiz has no questions"]


def test_expected_count_mismatch_is_reported():
    problems = validate_quiz(make_quiz(3), expected_questions=5)
    assert problems == ["expected 5 questions, got 3"]


def test_wrong_number_of_choices_is_reported():
    too_few = GeneratedQuiz(questions=[make_question(choices=["A", "B", "C"])])
    too_many = GeneratedQuiz(questions=[make_question(choices=["A", "B", "C", "D", "E"])])

    assert validate_quiz(too_few) == [
        "question 1: must have exactly 4 choices, got 3"
    ]
    assert validate_quiz(too_many) == [
        "question 1: must have exactly 4 choices, got 5"
    ]


def test_non_list_choices_is_reported():
    question = make_question()
    object.__setattr__(question, "choices", "Bilbo")
    problems = validate_quiz(GeneratedQuiz(questions=[question]))
    assert "question 1: choices must be a list of 4 strings" in problems


def test_empty_choice_is_reported():
    problems = validate_quiz(
        GeneratedQuiz(questions=[make_question(choices=["A", "  ", "C", "D"])])
    )
    assert problems == ["question 1: choice 2 is empty"]


def test_duplicate_choices_are_reported():
    problems = validate_quiz(
        GeneratedQuiz(questions=[make_question(choices=["A", "B", "A", "D"])])
    )
    assert problems == ["question 1: choices must be distinct"]


def test_answer_index_outside_range_is_reported():
    for bad in (-1, 4):
        problems = validate_quiz(
            GeneratedQuiz(questions=[make_question(answer_index=bad)])
        )
        assert problems == [f"question 1: answer_index {bad} is outside 0-3"]


def test_non_integer_answer_index_is_reported():
    problems = validate_quiz(
        GeneratedQuiz(questions=[make_question(answer_index="1")])
    )
    assert problems == ["question 1: answer_index must be an integer 0-3"]


def test_empty_prompt_is_reported():
    problems = validate_quiz(GeneratedQuiz(questions=[make_question(prompt="   ")]))
    assert problems == ["question 1: prompt is empty"]


def test_multiple_problems_are_all_reported():
    broken = GeneratedQuiz(
        questions=[
            make_question(prompt="", choices=["A", "B", "C"], answer_index=7),
        ]
    )
    assert validate_quiz(broken) == [
        "question 1: prompt is empty",
        "question 1: must have exactly 4 choices, got 3",
        "question 1: answer_index 7 is outside 0-3",
    ]


def test_quiz_without_questions_attribute_is_reported():
    assert validate_quiz(object()) == ["quiz has no questions"]
