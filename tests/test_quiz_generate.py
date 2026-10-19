"""Quiz generation against a fake chat-completions client.

Nothing here touches the network: every test injects ``FakeClient``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from readback.models import Book, GeneratedQuestion, GeneratedQuiz
from readback.quiz.generate import (
    QuizGenerationError,
    generate_quiz,
    save_draft,
)
from readback.quiz.prompts import build_prompt


class FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(
        self,
        status_code: int = 200,
        payload=None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("response has no JSON body")
        return self._payload


class FakeClient:
    """Records requests and replays queued responses or exceptions."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []
        self.closed = False

    def post(self, url, headers=None, json=None):
        self.calls.append(
            {"url": url, "headers": dict(headers or {}), "json": json}
        )
        if not self._responses:
            raise AssertionError("the code made more requests than the test queued")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


def quiz_body(count: int = 5) -> dict:
    return {
        "questions": [
            {
                "prompt": f"Question {index}?",
                "choices": ["One", "Two", "Three", "Four"],
                "answer_index": 2,
                "explanation": "The story says so.",
            }
            for index in range(1, count + 1)
        ]
    }


def completion(content, status_code: int = 200) -> FakeResponse:
    if not isinstance(content, str):
        content = json.dumps(content)
    payload = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return FakeResponse(status_code=status_code, payload=payload)


def test_generate_clean_response(book, settings):
    client = FakeClient([completion(quiz_body())])

    quiz = generate_quiz(book, settings, num_questions=5, client=client)

    assert isinstance(quiz, GeneratedQuiz)
    assert len(quiz.questions) == 5
    first = quiz.questions[0]
    assert first.prompt == "Question 1?"
    assert first.choices == ["One", "Two", "Three", "Four"]
    assert first.answer_index == 2
    assert first.explanation == "The story says so."

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"] == "http://localhost:9/v1/chat/completions"
    assert call["headers"]["Content-Type"] == "application/json"
    assert call["headers"]["Authorization"] == "Bearer test-key"

    payload = call["json"]
    assert payload["model"] == "test-model"
    assert payload["temperature"] == 0.4
    assert payload["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert "The Hobbit" in payload["messages"][1]["content"]
    assert client.closed is False


def test_build_prompt_includes_book_facts():
    book = Book(
        id=7,
        title="The Hobbit",
        author="J.R.R. Tolkien",
        points=5.0,
        grade_level="6-8",
        notes="Ada loved the riddles chapter.",
    )

    messages = build_prompt(book, 4)

    assert [message["role"] for message in messages] == ["system", "user"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert "four choices" in system.lower()
    assert "edition" in system.lower()
    assert "page number" in system.lower()
    assert "strict json" in system.lower()

    assert "The Hobbit" in user
    assert "J.R.R. Tolkien" in user
    assert "6-8" in user
    assert "5" in user
    assert "Ada loved the riddles chapter." in user
    assert '"questions"' in user
    assert "exactly 4 question" in user


def test_build_prompt_omits_blank_notes_and_grade():
    book = Book(id=8, title="A Short Book", author="", points=1.0)

    user = build_prompt(book, 3)[1]["content"]

    assert "Author: Unknown" in user
    assert "Parent notes" not in user
    assert "Grade level" not in user


def test_build_prompt_rejects_zero_questions(book):
    with pytest.raises(ValueError):
        build_prompt(book, 0)


def test_fenced_response_is_parsed(book, settings):
    fenced = "```json\n" + json.dumps(quiz_body(3)) + "\n```"
    client = FakeClient([completion(fenced)])

    quiz = generate_quiz(book, settings, num_questions=3, client=client)

    assert len(quiz.questions) == 3


def test_outermost_json_object_is_parsed(book, settings):
    noisy = "Sure, here is the quiz:\n" + json.dumps(quiz_body(2)) + "\nGood luck!"
    client = FakeClient([completion(noisy)])

    quiz = generate_quiz(book, settings, num_questions=2, client=client)

    assert len(quiz.questions) == 2


def test_answer_index_string_is_coerced(book, settings):
    body = quiz_body(1)
    body["questions"][0]["answer_index"] = "1"
    client = FakeClient([completion(body)])

    quiz = generate_quiz(book, settings, num_questions=1, client=client)

    assert quiz.questions[0].answer_index == 1
    assert isinstance(quiz.questions[0].answer_index, int)


def test_bare_quiz_object_is_accepted(book, settings):
    client = FakeClient([FakeResponse(payload=quiz_body(2))])

    quiz = generate_quiz(book, settings, num_questions=2, client=client)

    assert len(quiz.questions) == 2


def test_malformed_then_valid_is_repaired(book, settings):
    client = FakeClient([completion("I am sorry, I cannot help with that."),
                         completion(quiz_body(5))])

    quiz = generate_quiz(book, settings, num_questions=5, client=client)

    assert len(quiz.questions) == 5
    assert len(client.calls) == 2

    repair_messages = client.calls[1]["json"]["messages"]
    assert repair_messages[0]["role"] == "system"
    assistant = [m for m in repair_messages if m["role"] == "assistant"]
    assert assistant[-1]["content"] == "I am sorry, I cannot help with that."
    assert "could not be used" in repair_messages[-1]["content"]


def test_invalid_structure_is_repaired(book, settings):
    broken = quiz_body(1)
    broken["questions"][0]["choices"] = ["One", "Two", "Three"]
    client = FakeClient([completion(broken), completion(quiz_body(1))])

    quiz = generate_quiz(book, settings, num_questions=1, client=client)

    assert len(quiz.questions) == 1
    repair_instruction = client.calls[1]["json"]["messages"][-1]["content"]
    assert "exactly 4 choices" in repair_instruction


def test_always_malformed_raises_with_truncated_body(book, settings):
    garbage = "this is not JSON at all. " * 60
    client = FakeClient([completion(garbage), completion(garbage)])

    with pytest.raises(QuizGenerationError) as excinfo:
        generate_quiz(book, settings, num_questions=5, client=client)

    error = excinfo.value
    assert len(error.raw_body) == 500
    assert error.raw_body in str(error)
    assert len(client.calls) == 2


def test_response_format_rejection_falls_back(book, settings):
    rejection = FakeResponse(
        status_code=400,
        text='{"error": {"message": "response_format is not supported"}}',
    )
    client = FakeClient([rejection, completion(quiz_body(5))])

    quiz = generate_quiz(book, settings, num_questions=5, client=client)

    assert len(quiz.questions) == 5
    assert len(client.calls) == 2
    assert client.calls[0]["json"]["response_format"] == {"type": "json_object"}
    assert "response_format" not in client.calls[1]["json"]


def test_missing_api_key_sends_no_authorization_header(book, settings):
    anonymous = replace(settings, llm_api_key="")
    client = FakeClient([completion(quiz_body(2))])

    generate_quiz(book, anonymous, num_questions=2, client=client)

    headers = client.calls[0]["headers"]
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_network_error_is_retried_once(book, settings):
    client = FakeClient([httpx.ConnectError("connection refused"), completion(quiz_body(2))])

    quiz = generate_quiz(book, settings, num_questions=2, client=client)

    assert len(quiz.questions) == 2
    assert len(client.calls) == 2


def test_repeated_network_error_raises(book, settings):
    client = FakeClient(
        [httpx.ConnectError("connection refused"), httpx.ConnectError("still down")]
    )

    with pytest.raises(QuizGenerationError):
        generate_quiz(book, settings, num_questions=2, client=client)

    assert len(client.calls) == 2


def test_http_error_status_raises(book, settings):
    client = FakeClient([FakeResponse(status_code=500, text="upstream exploded")])

    with pytest.raises(QuizGenerationError) as excinfo:
        generate_quiz(book, settings, num_questions=2, client=client)

    assert "500" in str(excinfo.value)
    assert "upstream exploded" in str(excinfo.value)


def test_generate_does_not_close_injected_client(book, settings):
    client = FakeClient([completion(quiz_body(1))])

    generate_quiz(book, settings, num_questions=1, client=client)

    assert client.closed is False


# --------------------------------------------------------------------------- #
# save_draft
# --------------------------------------------------------------------------- #


def sample_quiz(count: int = 3) -> GeneratedQuiz:
    return GeneratedQuiz(
        questions=[
            GeneratedQuestion(
                prompt=f"Question {index}?",
                choices=["A", "B", "C", "D"],
                answer_index=index % 4,
                explanation=f"Explanation {index}.",
            )
            for index in range(1, count + 1)
        ]
    )


def test_save_draft_creates_quiz_and_ordered_questions(conn, book):
    quiz = sample_quiz(3)

    quiz_id = save_draft(conn, book.id, quiz, "test-model", "parent notes")

    row = conn.execute("SELECT * FROM quizzes WHERE id = ?", (quiz_id,)).fetchone()
    assert row["book_id"] == book.id
    assert row["status"] == "draft"
    assert row["model"] == "test-model"
    assert row["source_notes"] == "parent notes"
    assert row["created_at"]

    questions = conn.execute(
        "SELECT * FROM questions WHERE quiz_id = ? ORDER BY ordinal", (quiz_id,)
    ).fetchall()
    assert [question["ordinal"] for question in questions] == [1, 2, 3]
    assert questions[0]["prompt"] == "Question 1?"
    assert json.loads(questions[0]["choices_json"]) == ["A", "B", "C", "D"]
    assert questions[2]["answer_index"] == 3
    assert questions[2]["explanation"] == "Explanation 3."


def test_save_draft_replaces_the_existing_draft(conn, book):
    first = save_draft(conn, book.id, sample_quiz(3), "model-one", "first")
    second = save_draft(conn, book.id, sample_quiz(2), "model-two", "second")

    assert second != first

    rows = conn.execute(
        "SELECT id, status FROM quizzes WHERE book_id = ? ORDER BY id", (book.id,)
    ).fetchall()
    assert [(row["id"], row["status"]) for row in rows] == [
        (first, "archived"),
        (second, "draft"),
    ]
    drafts = conn.execute(
        "SELECT COUNT(*) AS n FROM quizzes WHERE book_id = ? AND status = 'draft'",
        (book.id,),
    ).fetchone()
    assert drafts["n"] == 1

    ordinals = conn.execute(
        "SELECT ordinal FROM questions WHERE quiz_id = ? ORDER BY ordinal", (second,)
    ).fetchall()
    assert [row["ordinal"] for row in ordinals] == [1, 2]


def test_save_draft_leaves_other_books_alone(conn, book):
    cursor = conn.execute(
        "INSERT INTO books (title, author, created_at) VALUES (?, ?, ?)",
        ("Other Book", "Someone", "2026-01-01T00:00:00+00:00"),
    )
    other_book_id = cursor.lastrowid
    conn.commit()

    other_draft = save_draft(conn, other_book_id, sample_quiz(1), "model", "")
    save_draft(conn, book.id, sample_quiz(1), "model", "")

    row = conn.execute(
        "SELECT status FROM quizzes WHERE id = ?", (other_draft,)
    ).fetchone()
    assert row["status"] == "draft"


def test_save_draft_refuses_invalid_quiz(conn, book):
    broken = GeneratedQuiz(
        questions=[
            GeneratedQuestion(prompt="Prompt?", choices=["A", "B", "C"], answer_index=0)
        ]
    )

    with pytest.raises(ValueError):
        save_draft(conn, book.id, broken, "model")

    count = conn.execute("SELECT COUNT(*) AS n FROM quizzes").fetchone()["n"]
    assert count == 0


def test_save_draft_refuses_empty_quiz(conn, book):
    with pytest.raises(ValueError):
        save_draft(conn, book.id, GeneratedQuiz(questions=[]), "model")
