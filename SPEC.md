# Readback — MVP specification

Readback is a self-hosted reading comprehension tracker for families. A parent
imports books, generates a short quiz, reviews every question, and approves it.
A reader takes approved quizzes, earns points for passing, and the family keeps
a reading log. Nothing leaves the machine unless the operator points the quiz
generator at a remote model endpoint.

This project is not affiliated with Renaissance Learning or Accelerated Reader.
Readback generates original questions. It never copies proprietary quiz content.

## Non-goals for the MVP

- No accounts, email, or password login. Readers are local profiles.
- No cloud sync, telemetry, or hosted service.
- No book text ingestion. Quizzes are generated from metadata plus optional
  parent-written notes, then gated by human review.
- No bundled Accelerated Reader catalog and no AR quiz content. The optional
  catalog lookup reads public BookFind pages **when a parent asks**, and stores
  only catalog metadata (quiz number, level, points); questions are still
  generated locally and reviewed.

## v0.2.0 additions

- Optional AR catalog lookup (`arbookfind.py`, `/parent/ar`, `ar-search`).
- Rewards (`rewards.py`): stars, per-reader goals, derived badges, book stickers,
  weekly reports, and reading events that carry no quiz.

## Stack

- Python 3.10+
- Flask 3 for the web layer, Jinja templates, no JS framework
- sqlite3 from the standard library, no ORM, no Alembic
- httpx for OpenAI-compatible chat completion calls
- pytest for tests
- Package layout: `src/readback`, console script `readback`

Runtime dependencies must stay at Flask + httpx. Anything else needs a reason
in the ticket discussion.

## Repository layout (fixed contract)

```
pyproject.toml
SPEC.md
README.md
LICENSE
Dockerfile
src/readback/
  __init__.py          # __version__
  config.py            # Settings + load_settings
  db.py                # connect, init_db, migrations, SCHEMA
  models.py            # dataclasses
  cli.py               # argparse entry point
  books.py             # CSV import, AR catalog upsert, listing, ISBN enrichment
  arbookfind.py        # optional BookFind lookup (search + detail parsing)
  rewards.py           # stars, goals, badges, stickers, weekly reports
  review.py            # draft queue, edits, approve/reject
  points.py            # attempts, scoring, ledger, manual reads, summaries
  export.py            # CSV reading log
  quiz/
    __init__.py
    prompts.py         # prompt templates + JSON schema text
    generate.py        # LLM call, parsing, retries
    validate.py        # structural validation
  web/
    __init__.py
    app.py             # create_app + routes
    templates/*.html
    static/style.css
tests/
  conftest.py
  test_db.py
  test_books.py
  test_arbookfind.py
  test_quiz_generate.py
  test_quiz_validate.py
  test_review.py
  test_points.py
  test_rewards.py
  test_manual.py
  test_migration.py
  test_export.py
  test_web.py
  test_web_rewards.py
  test_e2e.py
examples/books.example.csv
```

Each ticket owns a disjoint file set. Do not edit files outside your ticket's
ownership list. The tree may contain uncommitted work from sibling tickets.
Never revert, stash, clean, or reformat someone else's files.

## Configuration

Environment variables, all optional except the database path:

| Variable | Default | Meaning |
|---|---|---|
| `READBACK_DB` | `./readback.db` | SQLite file path |
| `READBACK_HOST` | `127.0.0.1` | Web bind address |
| `READBACK_PORT` | `5057` | Web port |
| `READBACK_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible base URL |
| `READBACK_LLM_API_KEY` | empty | Bearer token, empty for local models |
| `READBACK_LLM_MODEL` | `llama3.1:8b` | Model name |
| `READBACK_LLM_TIMEOUT` | `120` | Seconds to wait for a completion, raise it for slow local models |
| `READBACK_PASS_THRESHOLD` | `0.8` | Fraction correct needed to pass |
| `READBACK_PARENT_PIN` | empty | If set, `/parent/*` requires this PIN via session |
| `READBACK_SECRET_KEY` | random per process | Flask session signing key |

`config.Settings` is a frozen dataclass with exactly these fields:
`db_path: Path`, `host: str`, `port: int`, `llm_base_url: str`,
`llm_api_key: str`, `llm_model: str`, `pass_threshold: float`,
`parent_pin: str`, `secret_key: str`, `llm_timeout: float`.

`load_settings(env: Mapping[str, str] | None = None) -> Settings` reads from
`os.environ` when `env` is None, coerces types, and raises `ValueError` for a
threshold outside `(0.0, 1.0]` or a non-integer port.

## Data model

`db.init_db(conn)` creates this schema idempotently. All timestamps are UTC
ISO-8601 strings (`datetime.now(timezone.utc).isoformat()`).

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS readers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  author TEXT NOT NULL DEFAULT '',
  isbn TEXT,
  points REAL NOT NULL DEFAULT 0.0 CHECK (points >= 0),
  grade_level TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  quiz_no INTEGER,
  book_level REAL,
  interest_level TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'manual'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_books_isbn
  ON books(isbn) WHERE isbn IS NOT NULL AND isbn != '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_books_title_author
  ON books(lower(title), lower(author));
CREATE UNIQUE INDEX IF NOT EXISTS idx_books_quiz_no
  ON books(quiz_no) WHERE quiz_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS quizzes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'draft'
    CHECK (status IN ('draft','approved','rejected','archived')),
  model TEXT NOT NULL DEFAULT '',
  source_notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  approved_at TEXT,
  approved_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_quizzes_book_status ON quizzes(book_id, status);

CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE,
  ordinal INTEGER NOT NULL,
  prompt TEXT NOT NULL,
  choices_json TEXT NOT NULL,
  answer_index INTEGER NOT NULL CHECK (answer_index BETWEEN 0 AND 3),
  explanation TEXT NOT NULL DEFAULT '',
  UNIQUE (quiz_id, ordinal)
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  quiz_id INTEGER REFERENCES quizzes(id) ON DELETE CASCADE,
  book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
  reader_id INTEGER NOT NULL REFERENCES readers(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'quiz' CHECK (kind IN ('quiz','manual')),
  started_at TEXT NOT NULL,
  submitted_at TEXT,
  score REAL,
  passed INTEGER,
  total_questions INTEGER NOT NULL DEFAULT 0,
  correct_answers INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_attempts_reader ON attempts(reader_id, submitted_at);
CREATE INDEX IF NOT EXISTS idx_attempts_book ON attempts(book_id);

CREATE TABLE IF NOT EXISTS attempt_answers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  choice_index INTEGER NOT NULL,
  correct INTEGER NOT NULL,
  UNIQUE (attempt_id, question_id)
);

CREATE TABLE IF NOT EXISTS points_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reader_id INTEGER NOT NULL REFERENCES readers(id) ON DELETE CASCADE,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  attempt_id INTEGER NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
  points REAL NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (reader_id, book_id)
);

CREATE TABLE IF NOT EXISTS goals (
  reader_id INTEGER PRIMARY KEY REFERENCES readers(id) ON DELETE CASCADE,
  target REAL NOT NULL CHECK (target > 0),
  period TEXT NOT NULL DEFAULT 'season',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_stickers (
  book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
  emoji TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  assigned_at TEXT NOT NULL
);
```

Rules:

- A book can have any number of quizzes, but at most one `draft` at a time.
- Only `approved` quizzes are visible to readers and can start an attempt.
- Points are awarded once per reader and book, on the first submitted attempt
  that passes. Later attempts never add points. A failed attempt awards nothing.
- `score` is `correct / total` as a float rounded to 3 decimals.
- `passed` is `1` when `score >= pass_threshold`, else `0`.
- Deleting a reader or book cascades to their quizzes, attempts, and ledger rows.
- A reading event with `kind = 'manual'` has no `quiz_id`; it still writes one
  `points_ledger` row, so points are once per reader and book on either path.
- `goals` is at most one row per reader; clearing a goal deletes the row.
- `book_stickers` is at most one row per book; absence means the deterministic
  fallback emoji is used.

## Module contracts

Keep these signatures stable. Sibling tickets code against them.

```python
# readback/db.py
def connect(path: str | Path) -> sqlite3.Connection
def init_db(conn: sqlite3.Connection) -> None
@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]

# readback/models.py (frozen dataclasses unless noted)
Book(id, title, author, isbn, points, grade_level, notes, created_at)
Reader(id, name, created_at)
Question(id, quiz_id, ordinal, prompt, choices: list[str], answer_index, explanation)
Quiz(id, book_id, status, model, source_notes, created_at, approved_at, approved_by, questions: list[Question])
GeneratedQuestion(prompt, choices, answer_index, explanation)
GeneratedQuiz(questions: list[GeneratedQuestion])
ImportResult(inserted: int, updated: int, skipped: int, errors: list[str])
AttemptResult(attempt_id, score, passed, correct, total, points_awarded, points_total)
ReaderSummary(reader_id, name, points_total, books_passed, attempts)

# readback/books.py
def import_books(conn: sqlite3.Connection, csv_path: str | Path) -> ImportResult
def list_books(conn: sqlite3.Connection, query: str | None = None) -> list[Book]
def get_book(conn: sqlite3.Connection, book_id: int) -> Book | None
def add_book(conn, *, title, author="", isbn=None, points=0.0, grade_level="",
             notes="", quiz_no=None, book_level=None, interest_level="",
             summary="", source="manual") -> int
def add_ar_book(conn, entry: ArBook) -> int
    # upsert by quiz_no when present, else title + author
def enrich_isbn(isbn: str, *, timeout: float = 5.0) -> dict | None
    # GET https://openlibrary.org/isbn/<isbn>.json
    # returns {"title": ..., "author": ...} or None. Never raises on network error.

# readback/arbookfind.py — optional, user-initiated
class ArBookFindError(RuntimeError): ...
ArBook(ar_id, title, author="", quiz_no=None, book_level=None,
       interest_level="", points=None, word_count=None, summary="", detail_path="")
def search_ar_books(query: str, *, client: httpx.Client | None = None) -> list[ArBook]
def fetch_ar_book(ar_id: int, *, client: httpx.Client | None = None) -> ArBook
def parse_search_page(page: str) -> list[ArBook]
def parse_detail_page(page: str, ar_id: int) -> ArBook
    # raises ArBookFindError on an unreadable page; blank query returns []

# readback/rewards.py
def stars(correct: int, total: int, *, pass_threshold: float = 0.8) -> int
def set_goal(conn, reader_id: int, target: float, *, period="season") -> Goal
def get_goal(conn, reader_id: int) -> Goal | None
def clear_goal(conn, reader_id: int) -> None
def milestones(goal: float | None) -> list[float]
def goal_progress(points: float, goal: Goal | float | None) -> dict
    # {"status","goal","points","percent","current","next","remaining"}
def assign_sticker(conn, book_id: int, emoji: str, label: str = "") -> None
def clear_sticker(conn, book_id: int) -> None
def stickers_for(conn, book_ids: Iterable[int]) -> dict[int, dict]
def badges_for(conn, reader_id: int, *, now=None) -> list[Badge]
def reader_counters(conn, reader_id: int, *, now=None) -> dict
def weekly_points(conn, reader_id: int, *, weeks: int = 5, now=None) -> list[dict]
def report_stats(conn, *, weeks: int = 5, now=None) -> list[dict]

# readback/quiz/generate.py
class QuizGenerationError(RuntimeError): ...
def generate_quiz(book: Book, settings: Settings, *, num_questions: int = 5,
                  client=None) -> GeneratedQuiz
def save_draft(conn: sqlite3.Connection, book_id: int, quiz: GeneratedQuiz,
               model: str, source_notes: str = "") -> int
    # enforces one draft per book: replaces the previous draft for that book

# readback/quiz/validate.py
def validate_quiz(quiz: GeneratedQuiz | Quiz, *, expected_questions: int | None = None) -> list[str]
    # returns human-readable problems; empty list means valid

# readback/review.py
def list_drafts(conn) -> list[Quiz]
def get_quiz(conn, quiz_id: int) -> Quiz | None
def update_question(conn, question_id: int, *, prompt=None, choices=None,
                    answer_index=None, explanation=None) -> None
def delete_question(conn, question_id: int) -> None
def approve_quiz(conn, quiz_id: int, actor: str = "parent") -> None
def reject_quiz(conn, quiz_id: int, actor: str = "parent", note: str = "") -> None
def list_approved(conn, book_id: int | None = None) -> list[Quiz]

# readback/points.py
def start_attempt(conn, quiz_id: int, reader_id: int) -> int
def submit_attempt(conn, attempt_id: int, answers: dict[int, int],
                   *, pass_threshold: float = 0.8) -> AttemptResult
    # answers maps question_id -> choice_index; a missing answer is incorrect
def log_manual_read(conn, reader_id: int, book_id: int) -> AttemptResult
    # a finished book with no quiz; raises ValueError when points were
    # already earned for that (reader, book)
def reader_summary(conn, reader_id: int) -> ReaderSummary
def all_summaries(conn) -> list[ReaderSummary]
def recent_attempts(conn, limit: int = 10) -> list[dict]

# readback/export.py
def export_reading_log_csv(conn, reader_id: int | None = None) -> str
    # columns: reader,book,author,points,quiz_id,attempted_at,score,passed,points_awarded

# readback/web/app.py
def create_app(settings: Settings | None = None) -> Flask
```

`approve_quiz` must refuse a quiz with zero questions or any invalid question
and raise `ValueError` with the validation problems. `start_attempt` must
refuse non-approved quizzes. `submit_attempt` must be idempotent for an already
submitted attempt: it returns the existing result instead of rescoring.

## LLM contract

`generate_quiz` sends one chat completion request:

- URL: `{llm_base_url.rstrip('/')}/chat/completions`
- Headers: `Content-Type: application/json`, plus `Authorization: Bearer ...`
  only when `llm_api_key` is non-empty
- Body: `{"model": settings.llm_model, "temperature": 0.4, "messages": [...]}`
  and `"response_format": {"type": "json_object"}`. If the endpoint returns a
  4xx that mentions `response_format`, retry once without that field.
- Prompt lives in `quiz/prompts.py`. It must include the book title, author,
  grade level, points, and parent notes, and must demand strict JSON:

```json
{"questions": [{"prompt": "...", "choices": ["...", "...", "...", "..."],
                "answer_index": 0, "explanation": "..."}]}
```

- Parse leniently: strip markdown code fences, take the outermost JSON object.
- On malformed JSON, retry once with the previous response and a repair
  instruction. Then raise `QuizGenerationError` with the truncated raw body.
- Validate the result with `validate_quiz`. On validation failure, retry once.
- Do not retry network errors more than once. Raise `QuizGenerationError` with
  a short message on final failure.
- Tests must inject a fake client. No test may touch the network.

## Web routes

Server-rendered forms only. Core flows must work with JavaScript disabled.

```
GET  /                                  dashboard: readers, points, recent attempts
GET  /reader/<int:reader_id>            reader home: goal, badges, quizzes, finished books
POST /reader/<int:reader_id>/manual     log a finished book with no quiz
GET  /reader/<int:reader_id>/quiz/<int:quiz_id>    quiz form
POST /reader/<int:reader_id>/quiz/<int:quiz_id>    submit answers
GET  /attempt/<int:attempt_id>          result page (stars, goal nudge, sticker)
GET  /parent                            review queue + counts
POST /parent/reader                     create reader (form field: name)
GET  /parent/quiz/<int:quiz_id>         review and edit a draft
POST /parent/quiz/<int:quiz_id>/approve
POST /parent/quiz/<int:quiz_id>/reject
POST /parent/question/<int:question_id>/edit
POST /parent/question/<int:question_id>/delete
GET  /parent/books                      book list + import form + sticker/remove controls
POST /parent/books/import               multipart CSV upload
POST /parent/books/<int:book_id>/generate   generate a draft quiz
POST /parent/books/<int:book_id>/sticker    pin or clear a book's emoji
POST /parent/books/<int:book_id>/delete     requires form field confirm=yes
GET  /parent/ar?q=...                   search the AR catalog
POST /parent/ar/add                     add a catalog entry (form field: ar_id)
GET  /parent/goals                      per-reader goals and progress
POST /parent/goals                      set a goal (reader_id, target); blank target clears
GET  /parent/reports                    weekly points, scores, badges, recent reading
GET  /parent/export.csv                 reading log export
GET  /healthz                           {"status":"ok"}
```

Parent routes require the PIN when `READBACK_PARENT_PIN` is set. Use a session
flag set by a `POST /parent/unlock` form. Redirect unauthenticated requests to
`/parent/unlock`.

The UI is warm, plain, and readable: one stylesheet, no icon fonts, no external
CDN requests, mobile width works at 360px. Do not ship a dark neon theme.

## CLI

```
readback init-db
readback import-books <csv>
readback serve [--host H] [--port P]
readback generate --book-id N [--questions 5]
readback ar-search <query> [--limit 20]
readback ar-add <ar_id>
```

`init-db` creates the schema, upgrades an older one, and prints the database
path. `serve` calls `create_app()` and runs Flask. `generate` prints the draft
quiz id. `ar-search` prints BookFind ids; `ar-add` copies one onto the shelf.

## Testing requirements

- Every module ships pytest coverage for its contract, including failure paths.
- Use `tmp_path` databases. Never write to the real `readback.db`.
- `tests/conftest.py` provides fixtures: `conn` (initialized temp DB), `book`,
  `reader`, and `settings` (local dummy values).
- `pytest -q` must pass from the repo root before any ticket is marked done.
- Keep tests fast (< 5 seconds total) and deterministic.

## Definition of done for the MVP

1. `python -m venv .venv && .venv/bin/pip install -e '.[dev]'` succeeds.
2. `.venv/bin/pytest -q` passes with at least 250 tests.
3. `readback init-db` then `readback import-books examples/books.example.csv`
   works from a clean directory.
4. With a fake LLM endpoint (tests) the full path works: import book, generate
   draft, edit a question, approve, take quiz as a reader, see score and points,
   export CSV.
5. `docker build` succeeds and `docker run` serves `/healthz`.
6. README explains setup, LM Studio/Ollama configuration, the parent approval
   gate, privacy behavior, and the unaffiliated notice.
