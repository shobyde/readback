"""Flask application for Readback.

Everything is server rendered.  Each request opens its own SQLite connection
(connections are not shared between Flask threads) and closes it on teardown.
The quiz generator is reached through a thin module level wrapper so tests can
patch either ``readback.web.app.generate_quiz`` or the underlying
``readback.quiz.generate.generate_quiz``.
"""

from __future__ import annotations

import csv
import json
import secrets
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .. import books as books_module
from .. import points as points_module
from .. import review as review_module
from ..config import Settings, load_settings
from ..db import connect, init_db, transaction
from ..export import export_reading_log_csv
from ..models import utc_now
from ..quiz import generate as quiz_generate
from ..quiz.generate import QuizGenerationError
from ..quiz.validate import validate_quiz

DEFAULT_QUESTIONS = 5
MAX_QUESTIONS = 20
PARENT_UNLOCK_PATH = "/parent/unlock"
EXPORT_FILENAME = "reading-log.csv"


# --------------------------------------------------------------------------- #
# Test seams
# --------------------------------------------------------------------------- #


def generate_quiz(book, settings, *, num_questions: int = DEFAULT_QUESTIONS):
    """Generate a quiz.  Tests patch this name instead of the HTTP layer."""
    return quiz_generate.generate_quiz(
        book, settings, num_questions=num_questions
    )


def save_draft(conn, book_id, quiz, model, source_notes=""):
    """Persist a generated quiz as the book's draft."""
    return quiz_generate.save_draft(conn, book_id, quiz, model, source_notes)


# --------------------------------------------------------------------------- #
# Application factory
# --------------------------------------------------------------------------- #


def create_app(settings: Settings | None = None) -> Flask:
    """Build the Flask app, make sure the schema exists, register routes."""
    if settings is None:
        settings = load_settings()

    db_path = Path(settings.db_path)
    if db_path.parent and str(db_path.parent) not in ("", "."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    bootstrap = connect(db_path)
    try:
        init_db(bootstrap)
    finally:
        bootstrap.close()

    app = Flask(__name__)
    app.config["READBACK_SETTINGS"] = settings
    app.secret_key = settings.secret_key or secrets.token_hex(32)

    @app.template_filter("short_date")
    def short_date(value: object) -> str:
        """Render a stored ISO timestamp as a short, human date."""
        if not value:
            return ""
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return str(value)[:10]
        return f"{parsed.strftime('%b')} {parsed.day}, {parsed.year}"

    @app.template_filter("points")
    def points_filter(value: object) -> str:
        """Show whole points as whole numbers, decimals only when needed."""
        try:
            amount = float(str(value or 0))
        except (TypeError, ValueError):
            return "0"
        if abs(amount - round(amount)) < 0.001:
            return f"{amount:.0f}"
        return f"{amount:.1f}".rstrip("0").rstrip(".")

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = connect(settings.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(exc: BaseException | None) -> None:  # noqa: ARG001 - Flask API
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    # ------------------------------------------------------------------ #
    # Parent PIN gate
    # ------------------------------------------------------------------ #

    @app.before_request
    def require_parent_pin():
        path = request.path.rstrip("/") or "/"
        if not path.startswith("/parent"):
            return None
        if not settings.parent_pin:
            return None
        if path == PARENT_UNLOCK_PATH:
            return None
        if session.get("parent_unlocked"):
            return None
        return redirect(url_for("parent_unlock"))

    @app.get(PARENT_UNLOCK_PATH)
    def parent_unlock():
        if not settings.parent_pin or session.get("parent_unlocked"):
            return redirect(url_for("parent_index"))
        return render_template("unlock.html")

    @app.post(PARENT_UNLOCK_PATH)
    def parent_unlock_submit():
        if not settings.parent_pin:
            return redirect(url_for("parent_index"))
        pin = (request.form.get("pin") or "").strip()
        if pin and secrets.compare_digest(pin, settings.parent_pin):
            session["parent_unlocked"] = True
            return redirect(url_for("parent_index"))
        flash("That PIN is not correct.", "error")
        return render_template("unlock.html"), 401

    # ------------------------------------------------------------------ #
    # Dashboard
    # ------------------------------------------------------------------ #

    @app.get("/")
    def dashboard():
        db = get_db()
        summaries = points_module.all_summaries(db)
        attempts = points_module.recent_attempts(db, limit=10)
        max_points = max((s.points_total for s in summaries), default=0.0) or 1.0
        return render_template(
            "dashboard.html",
            summaries=summaries,
            attempts=attempts,
            max_points=max_points,
        )

    # ------------------------------------------------------------------ #
    # Reader flow
    # ------------------------------------------------------------------ #

    def require_reader(db: sqlite3.Connection, reader_id: int):
        try:
            return points_module.reader_summary(db, reader_id)
        except ValueError:
            abort(404)

    def passed_books(db: sqlite3.Connection, reader_id: int) -> list[dict]:
        rows = db.execute(
            "SELECT b.id AS book_id, b.title, b.author, l.points, "
            "l.created_at FROM points_ledger l "
            "JOIN books b ON b.id = l.book_id "
            "WHERE l.reader_id = ? ORDER BY l.created_at DESC, l.id DESC",
            (reader_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/reader/<int:reader_id>")
    def reader_home(reader_id: int):
        db = get_db()
        summary = require_reader(db, reader_id)
        quizzes = review_module.list_approved(db)
        books = {book.id: book for book in books_module.list_books(db)}
        passed = passed_books(db, reader_id)
        passed_ids = {item["book_id"] for item in passed}
        quiz_by_book = {quiz.book_id: quiz for quiz in quizzes}
        for item in passed:
            quiz = quiz_by_book.get(item["book_id"])
            item["quiz_id"] = quiz.id if quiz is not None else None
        next_quizzes = [quiz for quiz in quizzes if quiz.book_id not in passed_ids]
        return render_template(
            "reader.html",
            summary=summary,
            next_quizzes=next_quizzes,
            books=books,
            passed=passed,
            passed_ids=passed_ids,
        )

    def approved_quiz(db: sqlite3.Connection, quiz_id: int):
        quiz = review_module.get_quiz(db, quiz_id)
        if quiz is None or quiz.status != "approved":
            abort(404)
        return quiz

    @app.get("/reader/<int:reader_id>/quiz/<int:quiz_id>")
    def quiz_form(reader_id: int, quiz_id: int):
        db = get_db()
        summary = require_reader(db, reader_id)
        quiz = approved_quiz(db, quiz_id)
        book = books_module.get_book(db, quiz.book_id)
        return render_template(
            "quiz.html", summary=summary, quiz=quiz, book=book
        )

    @app.post("/reader/<int:reader_id>/quiz/<int:quiz_id>")
    def quiz_submit(reader_id: int, quiz_id: int):
        db = get_db()
        summary = require_reader(db, reader_id)
        quiz = approved_quiz(db, quiz_id)

        answers: dict[int, int] = {}
        for question in quiz.questions:
            raw = None
            for field in (
                f"q_{question.id}",
                f"question_{question.id}",
                f"answer_{question.id}",
                f"q{question.id}",
                str(question.id),
            ):
                value = request.form.get(field)
                if value is not None:
                    raw = value
                    break
            if raw is None or not str(raw).strip():
                continue
            try:
                answers[question.id] = int(str(raw).strip())
            except ValueError:
                continue

        try:
            attempt_id = points_module.start_attempt(db, quiz_id, reader_id)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("reader_home", reader_id=reader_id))

        points_module.submit_attempt(
            db, attempt_id, answers, pass_threshold=settings.pass_threshold
        )
        return redirect(url_for("attempt_result", attempt_id=attempt_id))

    @app.get("/attempt/<int:attempt_id>")
    def attempt_result(attempt_id: int):
        db = get_db()
        attempt = db.execute(
            "SELECT a.*, r.name AS reader_name, q.book_id AS quiz_book_id, "
            "b.title AS book_title, b.author AS book_author "
            "FROM attempts a "
            "JOIN readers r ON r.id = a.reader_id "
            "JOIN quizzes q ON q.id = a.quiz_id "
            "JOIN books b ON b.id = q.book_id "
            "WHERE a.id = ?",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            abort(404)

        rows = db.execute(
            "SELECT q.id AS question_id, q.ordinal, q.prompt, q.choices_json, "
            "q.answer_index, q.explanation, aa.choice_index AS chosen, "
            "aa.correct AS is_correct "
            "FROM questions q "
            "LEFT JOIN attempt_answers aa "
            "ON aa.question_id = q.id AND aa.attempt_id = ? "
            "WHERE q.quiz_id = ? ORDER BY q.ordinal, q.id",
            (attempt_id, attempt["quiz_id"]),
        ).fetchall()

        questions = []
        for row in rows:
            try:
                choices = json.loads(row["choices_json"])
            except (TypeError, ValueError):
                choices = []
            if not isinstance(choices, list):
                choices = []
            chosen = row["chosen"]
            questions.append(
                {
                    "prompt": row["prompt"],
                    "choices": [str(choice) for choice in choices],
                    "answer_index": row["answer_index"],
                    "chosen": chosen,
                    "chosen_text": (
                        str(choices[chosen])
                        if isinstance(chosen, int)
                        and 0 <= chosen < len(choices)
                        else ""
                    ),
                    "correct_text": (
                        str(choices[row["answer_index"]])
                        if 0 <= row["answer_index"] < len(choices)
                        else ""
                    ),
                    "is_correct": bool(row["is_correct"]),
                    "explanation": row["explanation"],
                }
            )

        summary = points_module.reader_summary(db, attempt["reader_id"])
        ledger = db.execute(
            "SELECT points FROM points_ledger WHERE attempt_id = ?",
            (attempt_id,),
        ).fetchone()
        points_awarded = float(ledger["points"]) if ledger is not None else 0.0
        return render_template(
            "result.html",
            attempt=attempt,
            questions=questions,
            summary=summary,
            points_awarded=points_awarded,
        )

    # ------------------------------------------------------------------ #
    # Parent area
    # ------------------------------------------------------------------ #

    @app.get("/parent")
    def parent_index():
        db = get_db()
        drafts = review_module.list_drafts(db)
        books = {book.id: book for book in books_module.list_books(db)}
        counts = db.execute(
            "SELECT (SELECT COUNT(*) FROM readers) AS readers, "
            "(SELECT COUNT(*) FROM books) AS books, "
            "(SELECT COUNT(*) FROM quizzes WHERE status = 'draft') AS drafts, "
            "(SELECT COUNT(*) FROM quizzes WHERE status = 'approved') AS approved, "
            "(SELECT COUNT(*) FROM attempts WHERE submitted_at IS NOT NULL) "
            "AS attempts"
        ).fetchone()
        summaries = points_module.all_summaries(db)
        max_points = max((s.points_total for s in summaries), default=0.0) or 1.0
        return render_template(
            "parent.html",
            drafts=drafts,
            books=books,
            counts=counts,
            summaries=summaries,
            max_points=max_points,
        )

    @app.post("/parent/reader")
    def parent_create_reader():
        db = get_db()
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("A reader name is required.", "error")
            return redirect(url_for("parent_index"))
        try:
            with transaction(db):
                db.execute(
                    "INSERT INTO readers (name, created_at) VALUES (?, ?)",
                    (name, utc_now()),
                )
        except sqlite3.IntegrityError:
            flash(f"A reader named {name} already exists.", "error")
        else:
            flash(f"Added reader {name}.", "success")
        return redirect(url_for("parent_index"))

    def quiz_for_review(db: sqlite3.Connection, quiz_id: int):
        quiz = review_module.get_quiz(db, quiz_id)
        if quiz is None:
            abort(404)
        return quiz

    @app.get("/parent/quiz/<int:quiz_id>")
    def parent_quiz(quiz_id: int):
        db = get_db()
        quiz = quiz_for_review(db, quiz_id)
        book = books_module.get_book(db, quiz.book_id)
        problems = validate_quiz(quiz) if quiz.status == "draft" else []
        return render_template(
            "review.html",
            quiz=quiz,
            book=book,
            problems=problems,
            choice_indexes=range(4),
        )

    @app.post("/parent/quiz/<int:quiz_id>/approve")
    def parent_quiz_approve(quiz_id: int):
        db = get_db()
        quiz_for_review(db, quiz_id)
        try:
            review_module.approve_quiz(db, quiz_id)
        except ValueError as exc:
            problems = getattr(exc, "problems", None) or [str(exc)]
            for problem in problems:
                flash(problem, "error")
            return redirect(url_for("parent_quiz", quiz_id=quiz_id))
        flash("Quiz approved. Readers can take it now.", "success")
        return redirect(url_for("parent_index"))

    @app.post("/parent/quiz/<int:quiz_id>/reject")
    def parent_quiz_reject(quiz_id: int):
        db = get_db()
        quiz_for_review(db, quiz_id)
        review_module.reject_quiz(
            db, quiz_id, note=request.form.get("note", "")
        )
        flash("Quiz rejected.", "success")
        return redirect(url_for("parent_index"))

    def question_quiz_id(db: sqlite3.Connection, question_id: int) -> int:
        row = db.execute(
            "SELECT quiz_id FROM questions WHERE id = ?", (question_id,)
        ).fetchone()
        if row is None:
            abort(404)
        return int(row["quiz_id"])

    @app.post("/parent/question/<int:question_id>/edit")
    def parent_question_edit(question_id: int):
        db = get_db()
        quiz_id = question_quiz_id(db, question_id)

        choices = request.form.getlist("choices")
        if len(choices) != 4:
            choices = [
                request.form.get(f"choice_{index}", "") for index in range(4)
            ]

        try:
            review_module.update_question(
                db,
                question_id,
                prompt=request.form.get("prompt", ""),
                choices=choices,
                answer_index=request.form.get("answer_index", ""),
                explanation=request.form.get("explanation", ""),
            )
        except ValueError as exc:
            flash(str(exc), "error")
        else:
            flash("Question saved.", "success")
        return redirect(url_for("parent_quiz", quiz_id=quiz_id))

    @app.post("/parent/question/<int:question_id>/delete")
    def parent_question_delete(question_id: int):
        db = get_db()
        quiz_id = question_quiz_id(db, question_id)
        review_module.delete_question(db, question_id)
        flash("Question deleted.", "success")
        return redirect(url_for("parent_quiz", quiz_id=quiz_id))

    @app.get("/parent/books")
    def parent_books():
        db = get_db()
        books = books_module.list_books(db)
        rows = db.execute(
            "SELECT q.book_id, q.status FROM quizzes q WHERE q.id = "
            "(SELECT MAX(id) FROM quizzes WHERE book_id = q.book_id)"
        ).fetchall()
        latest = {row["book_id"]: row["status"] for row in rows}
        return render_template(
            "books.html", books=books, latest=latest, default_questions=DEFAULT_QUESTIONS
        )

    @app.post("/parent/books/import")
    def parent_books_import():
        db = get_db()
        upload = None
        for field in ("csv", "file", "books", "upload"):
            candidate = request.files.get(field)
            if candidate is not None and (candidate.filename or "").strip():
                upload = candidate
                break
        if upload is None:
            flash("Choose a CSV file to import.", "error")
            return redirect(url_for("parent_books"))
        if not upload.filename.lower().endswith(".csv"):
            flash("The import file must be a .csv file.", "error")
            return redirect(url_for("parent_books"))

        handle = tempfile.NamedTemporaryFile(
            mode="wb", suffix=".csv", prefix="readback-import-", delete=False
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(upload.read())
            result = books_module.import_books(db, temp_path)
        except (OSError, ValueError, csv.Error) as exc:
            flash(f"The import failed: {exc}", "error")
            return redirect(url_for("parent_books"))
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

        flash(
            f"Imported {result.inserted} new, updated {result.updated}, "
            f"skipped {result.skipped}.",
            "success" if not result.errors else "error",
        )
        for problem in result.errors:
            flash(problem, "error")
        return redirect(url_for("parent_books"))

    @app.post("/parent/books/<int:book_id>/generate")
    def parent_books_generate(book_id: int):
        db = get_db()
        book = books_module.get_book(db, book_id)
        if book is None:
            abort(404)

        raw = (request.form.get("num_questions") or "").strip()
        try:
            num_questions = int(raw) if raw else DEFAULT_QUESTIONS
        except ValueError:
            num_questions = DEFAULT_QUESTIONS
        num_questions = max(1, min(num_questions, MAX_QUESTIONS))

        try:
            quiz = generate_quiz(book, settings, num_questions=num_questions)
            quiz_id = save_draft(
                db, book.id, quiz, settings.llm_model, book.notes
            )
        except QuizGenerationError as exc:
            flash(f"Quiz generation failed: {exc}", "error")
            return redirect(url_for("parent_books"))
        except Exception as exc:  # noqa: BLE001 - a bad endpoint must not 500
            flash(
                f"Quiz generation failed: {type(exc).__name__}: {exc}", "error"
            )
            return redirect(url_for("parent_books"))

        flash(
            f"Draft quiz {quiz_id} created for {book.title} with "
            f"{len(quiz.questions)} questions. Review it before readers can "
            "take it.",
            "success",
        )
        return redirect(url_for("parent_quiz", quiz_id=quiz_id))

    @app.get("/parent/export.csv")
    def parent_export():
        db = get_db()
        reader_id = request.args.get("reader_id", type=int)
        body = export_reading_log_csv(db, reader_id)
        response = Response(body, mimetype="text/csv")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="{EXPORT_FILENAME}"'
        )
        return response

    # ------------------------------------------------------------------ #
    # Health
    # ------------------------------------------------------------------ #

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    return app


__all__ = ["create_app", "generate_quiz", "save_draft"]
