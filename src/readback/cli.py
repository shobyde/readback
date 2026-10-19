"""Command line entry point for Readback.

Sibling modules (``books``, ``quiz.generate``, ``web.app``) are imported inside
each handler, so ``readback init-db`` keeps working while those modules are
still being written.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from . import __version__
from .config import load_settings
from .db import connect, init_db


def _open_db(path) -> sqlite3.Connection:
    """Connect and make sure the schema exists, with a friendly failure."""
    try:
        conn = connect(path)
    except sqlite3.Error as exc:
        raise RuntimeError(f"cannot open database {path}: {exc}") from exc
    try:
        init_db(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise RuntimeError(f"cannot initialize database {path}: {exc}") from exc
    return conn


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = load_settings()
    conn = _open_db(settings.db_path)
    conn.close()
    print(f"Initialized database at {settings.db_path.resolve()}")
    return 0


def cmd_import_books(args: argparse.Namespace) -> int:
    from .books import import_books  # local import: sibling module

    settings = load_settings()
    conn = _open_db(settings.db_path)
    try:
        result = import_books(conn, args.csv_path)
    finally:
        conn.close()

    print(
        f"Imported {result.inserted} new, updated {result.updated}, "
        f"skipped {result.skipped}"
    )
    for problem in result.errors:
        print(f"  ! {problem}", file=sys.stderr)
    if result.errors and not (result.inserted or result.updated):
        return 1
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .web.app import create_app  # local import: sibling module

    settings = load_settings()
    host = args.host or settings.host
    port = args.port if args.port is not None else settings.port

    conn = _open_db(settings.db_path)
    conn.close()

    app = create_app(settings)
    print(f"Serving Readback on http://{host}:{port}")
    app.run(host=host, port=port, debug=False)
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    from .books import get_book  # local import: sibling modules
    from .quiz.generate import generate_quiz, save_draft

    settings = load_settings()
    conn = _open_db(settings.db_path)
    try:
        book = get_book(conn, args.book_id)
        if book is None:
            print(
                f"readback: no book with id {args.book_id}",
                file=sys.stderr,
            )
            return 1
        quiz = generate_quiz(book, settings, num_questions=args.questions)
        quiz_id = save_draft(
            conn, book.id, quiz, settings.llm_model, book.notes
        )
    finally:
        conn.close()

    print(f"Draft quiz {quiz_id} created for {book.title!r} with {len(quiz.questions)} questions")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="readback",
        description="Self-hosted reading comprehension tracking for families.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    init_parser = subparsers.add_parser(
        "init-db", help="create the SQLite schema"
    )
    init_parser.set_defaults(handler=cmd_init_db)

    import_parser = subparsers.add_parser(
        "import-books", help="import books from a CSV file"
    )
    import_parser.add_argument("csv_path", help="path to the CSV file")
    import_parser.set_defaults(handler=cmd_import_books)

    serve_parser = subparsers.add_parser("serve", help="run the web application")
    serve_parser.add_argument("--host", default=None, help="bind address")
    serve_parser.add_argument("--port", type=int, default=None, help="bind port")
    serve_parser.set_defaults(handler=cmd_serve)

    generate_parser = subparsers.add_parser(
        "generate", help="generate a draft quiz for a book"
    )
    generate_parser.add_argument(
        "--book-id", type=int, required=True, help="book id"
    )
    generate_parser.add_argument(
        "--questions", type=int, default=5, help="number of questions (default 5)"
    )
    generate_parser.set_defaults(handler=cmd_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.handler(args) or 0)
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"readback: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary, no tracebacks for users
        print(f"readback: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
