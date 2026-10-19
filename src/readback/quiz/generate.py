"""Generate quizzes through an OpenAI-compatible chat completion endpoint.

The flow is deliberately boring: one model call, lenient JSON parsing, strict
structural validation, at most one repair call, and a single retry for network
failures.  Tests inject a fake client, so nothing here touches the network on
its own.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import httpx

from ..config import Settings
from ..db import transaction
from ..models import Book, GeneratedQuestion, GeneratedQuiz, utc_now
from .prompts import JSON_SCHEMA_TEXT, build_prompt
from .validate import validate_quiz

DEFAULT_TEMPERATURE = 0.4
RAW_BODY_LIMIT = 500


class QuizGenerationError(RuntimeError):
    """Raised when the endpoint cannot produce a usable quiz."""

    def __init__(self, message: str, raw_body: str = "") -> None:
        super().__init__(message)
        self.raw_body = raw_body


class _MalformedResponse(Exception):
    """Internal: the model's answer could not be parsed or validated."""


def generate_quiz(
    book: Book,
    settings: Settings,
    *,
    num_questions: int = 5,
    client: Any | None = None,
) -> GeneratedQuiz:
    """Ask the model for ``num_questions`` questions about ``book``."""
    messages = build_prompt(book, num_questions)

    owns_client = client is None
    if client is None:
        client = httpx.Client(timeout=60.0)

    try:
        requester = _Requester(client, settings)

        raw = requester.complete(messages)
        quiz: GeneratedQuiz | None = None
        first_problem = ""
        try:
            quiz = _parse_quiz(raw, num_questions)
        except _MalformedResponse as exc:
            first_problem = str(exc)
        if quiz is not None:
            return quiz

        repair_messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": _repair_instruction(first_problem)},
        ]
        raw = requester.complete(repair_messages)
        try:
            return _parse_quiz(raw, num_questions)
        except _MalformedResponse as final_problem:
            raise QuizGenerationError(
                f"model did not return a usable quiz: {final_problem} "
                f"(raw response: {raw[:RAW_BODY_LIMIT]})",
                raw_body=raw[:RAW_BODY_LIMIT],
            ) from None
    finally:
        if owns_client:
            client.close()


def save_draft(
    conn: sqlite3.Connection,
    book_id: int,
    quiz: GeneratedQuiz,
    model: str,
    source_notes: str = "",
) -> int:
    """Insert ``quiz`` as the book's only draft, archiving any old draft."""
    problems = validate_quiz(quiz)
    if problems:
        raise ValueError("cannot save an invalid quiz: " + "; ".join(problems))

    created_at = utc_now()
    with transaction(conn):
        conn.execute(
            "UPDATE quizzes SET status = 'archived' "
            "WHERE book_id = ? AND status = 'draft'",
            (book_id,),
        )
        cursor = conn.execute(
            "INSERT INTO quizzes (book_id, status, model, source_notes, created_at) "
            "VALUES (?, 'draft', ?, ?, ?)",
            (book_id, model, source_notes, created_at),
        )
        quiz_id = int(cursor.lastrowid or 0)

        for ordinal, question in enumerate(quiz.questions, start=1):
            conn.execute(
                "INSERT INTO questions "
                "(quiz_id, ordinal, prompt, choices_json, answer_index, explanation) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    quiz_id,
                    ordinal,
                    question.prompt,
                    json.dumps(list(question.choices), ensure_ascii=False),
                    int(question.answer_index),
                    question.explanation or "",
                ),
            )

    return quiz_id


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #


class _Requester:
    """One endpoint, one set of headers, remembered JSON-mode support."""

    def __init__(self, client: Any, settings: Settings) -> None:
        self._client = client
        self._url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self._model = settings.llm_model
        self._json_mode = True

        self._headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            self._headers["Authorization"] = f"Bearer {settings.llm_api_key}"

    def complete(self, messages: list[dict[str, str]]) -> str:
        """Return the assistant message text for ``messages``."""
        payload: dict[str, Any] = {
            "model": self._model,
            "temperature": DEFAULT_TEMPERATURE,
            "messages": messages,
        }
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = self._post(payload)
        body = _response_text(response)

        if (
            self._json_mode
            and 400 <= response.status_code < 500
            and "response_format" in body.lower()
        ):
            # Older OpenAI-compatible servers reject JSON mode outright.
            self._json_mode = False
            payload.pop("response_format", None)
            response = self._post(payload)
            body = _response_text(response)

        if not 200 <= response.status_code < 300:
            raise QuizGenerationError(
                f"LLM endpoint returned HTTP {response.status_code}: "
                f"{body[:RAW_BODY_LIMIT]}",
                raw_body=body[:RAW_BODY_LIMIT],
            )

        content = _extract_content(response)
        return content if content is not None else body

    def _post(self, payload: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for _ in range(2):  # one retry for network-level failures
            try:
                return self._client.post(
                    self._url, headers=self._headers, json=dict(payload)
                )
            except (httpx.RequestError, OSError) as exc:
                last_error = exc
        raise QuizGenerationError(
            f"could not reach the LLM endpoint: {last_error}"
        ) from None


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if isinstance(text, str):
        return text
    try:
        return json.dumps(response.json(), ensure_ascii=False)
    except Exception:  # noqa: BLE001 - best-effort body capture
        return ""


def _extract_content(response: Any) -> str | None:
    """Pull ``choices[0].message.content`` out of a chat completion body."""
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - some proxies return bare JSON
        return None
    if not isinstance(data, dict):
        return None

    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [
                        part["text"]
                        for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ]
                    if parts:
                        return "".join(parts)
            text = first.get("text")
            if isinstance(text, str):
                return text

    # A few servers answer with the quiz object itself.
    if isinstance(data.get("questions"), list):
        return json.dumps(data, ensure_ascii=False)
    return None


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n?(.*?)\n?```$", re.DOTALL)


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    if match:
        return match.group(1).strip()
    return stripped


def _extract_json_object(text: str) -> str | None:
    """Return the outermost ``{...}`` block in ``text``, or None."""
    body = _strip_fences(text)
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        return None
    return body[start : end + 1]


def _parse_quiz(raw: str, expected_questions: int) -> GeneratedQuiz:
    if not isinstance(raw, str) or not raw.strip():
        raise _MalformedResponse("the response was empty")

    candidate = _extract_json_object(raw)
    if candidate is None:
        raise _MalformedResponse("the response contained no JSON object")

    try:
        data = json.loads(candidate)
    except (TypeError, ValueError) as exc:
        raise _MalformedResponse(f"the response was not valid JSON ({exc})") from None

    if not isinstance(data, dict):
        raise _MalformedResponse("the response was not a JSON object")

    items = data.get("questions")
    if not isinstance(items, list):
        raise _MalformedResponse("the response had no \"questions\" list")

    questions: list[GeneratedQuestion] = []
    for index, item in enumerate(items, start=1):
        questions.append(_parse_question(index, item))

    quiz = GeneratedQuiz(questions=questions)
    problems = validate_quiz(quiz, expected_questions=expected_questions)
    if problems:
        raise _MalformedResponse("; ".join(problems))
    return quiz


def _parse_question(index: int, item: Any) -> GeneratedQuestion:
    if not isinstance(item, dict):
        raise _MalformedResponse(f"question {index} was not a JSON object")

    prompt = item.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise _MalformedResponse(f"question {index} had no prompt text")

    choices_raw = item.get("choices")
    if not isinstance(choices_raw, list):
        raise _MalformedResponse(f"question {index} had no choices list")
    choices = ["" if choice is None else str(choice).strip() for choice in choices_raw]

    if "answer_index" not in item:
        raise _MalformedResponse(f"question {index} had no answer_index")
    try:
        answer_index = int(item["answer_index"])
    except (TypeError, ValueError):
        raise _MalformedResponse(
            f"question {index} had a non-numeric answer_index"
        ) from None

    explanation = item.get("explanation", "")
    if explanation is None:
        explanation = ""
    elif not isinstance(explanation, str):
        explanation = str(explanation)

    return GeneratedQuestion(
        prompt=prompt.strip(),
        choices=choices,
        answer_index=answer_index,
        explanation=explanation.strip(),
    )


def _repair_instruction(problem: str) -> str:
    return (
        "That reply could not be used: "
        f"{problem}.\n\n"
        "Fix it and reply again. Return ONLY one JSON object in this exact "
        f"shape:\n{JSON_SCHEMA_TEXT}\n"
        "Do not add markdown fences or any other text."
    )


__all__ = ["QuizGenerationError", "generate_quiz", "save_draft"]
