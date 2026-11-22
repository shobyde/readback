# Changelog

All notable changes to Readback. The format follows Keep a Changelog and the
project uses semantic versioning.

## [0.2.0] — 2026-11-22

The rewards and catalog release: the loop that keeps a reader coming back, and
the Accelerated Reader lookup that makes a shelf easy to fill.

### Added

- **AR catalog lookup.** Search Accelerated Reader BookFind from `/parent/ar` or
  `readback ar-search`, and copy a result onto the shelf with `readback ar-add`.
  Copies the AR quiz number, ATOS level, interest level, point value, and
  catalog summary. No AR quiz content is copied; questions are still generated
  locally and reviewed. An unreadable page raises `ArBookFindError` instead of
  silently returning nothing.
- **Stars.** 0–3 per attempt (3 at 90%, 2 at 70%, 1 for a pass), shown on the
  result card and the finished-book list.
- **Reading goals.** A per-reader points target with 25-point milestones, a
  progress bar on the shelf, a nudge after each quiz, and a Goal getter badge.
- **Badges.** First finish, Bookworm, Shelf hunter, Century club, Week warrior,
  Perfect page, and Goal getter. Derived from history on every render, never
  stored, so a rule change cannot leave a stale trophy behind.
- **Book stickers.** Pin an emoji to a book from the books page; unassigned books
  fall back to a deterministic emoji.
- **Reports.** `/parent/reports` with five weeks of points per reader, average
  quiz score, badge progress, and recent reading events.
- **Books without a quiz.** A reader can log a finished book from their shelf.
  It writes one ledger row (points once per book), counts as a book finished,
  and exports with an empty `quiz_id`.
- **Book removal** from the books page, behind an explicit confirmation
  checkbox, cascading to quizzes, questions, attempts, and ledger rows.
- New columns on `books` (`quiz_no`, `book_level`, `interest_level`, `summary`,
  `source`), new `goals` and `book_stickers` tables, and `attempts.kind` plus a
  nullable `attempts.quiz_id` so a reading event can exist without a quiz.

### Changed

- `recent_attempts` and the CSV export join books through the attempt, so a
  manual read appears beside quiz attempts.
- The demo seeder now ships three goals, three stickers, and a manual read.

### Migration

- `init_db` upgrades a 0.1.x database in place: it adds the catalog columns and
  rewards tables, then rebuilds `attempts` (foreign keys off for the copy) so
  answers and ledger rows survive. `tests/test_migration.py` proves it against a
  real 0.1.0 schema, including `PRAGMA foreign_key_check`.

## [0.1.0] — 2026-10-19

First release: CSV book import, model-generated draft quizzes, a parent review
gate with structural validation, a points ledger awarded once per reader and
book, CSV export, a server-rendered reader shelf and parent area, Docker image,
and 185 tests.
