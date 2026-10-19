#!/usr/bin/env python3
"""Populate a Readback database with curated demo data.

Useful for local evaluation, screenshots, and trying the UI without pointing a
real model at a real book list. All content here is hand-written demo data.

    python scripts/seed_demo.py --db demo.db --force
    READBACK_DB=demo.db .venv/bin/readback serve
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from readback import books, db, points, review  # noqa: E402
from readback.models import GeneratedQuestion, GeneratedQuiz  # noqa: E402
from readback.quiz.generate import save_draft  # noqa: E402

READERS = ["Maya", "Jonah", "Elena", "Sam"]

# title -> list of (prompt, choices, answer_index, explanation)
QUIZZES: dict[str, list[tuple[str, list[str], int, str]]] = {
    "Charlotte's Web": [
        (
            "Who first tries to save Wilbur from being killed?",
            ["Fern Arable", "Avery Arable", "Mr. Zuckerman", "Templeton"],
            0,
            "Fern pleads with her father and promises to care for the runt pig herself.",
        ),
        (
            "What is the first word Charlotte writes in her web?",
            ["Some Pig", "Terrific", "Radiant", "Humble"],
            0,
            "Charlotte starts with 'Some Pig' to convince the Zuckermans that Wilbur is special.",
        ),
        (
            "What does Templeton do for Charlotte?",
            ["Finds words for the web", "Guards the barn", "Builds a nest", "Feeds Wilbur"],
            0,
            "Templeton collects scraps and words from the dump for Charlotte's messages.",
        ),
        (
            "What happens to Charlotte at the end of the story?",
            ["She dies after laying her eggs", "She moves to another farm", "She is sold", "She becomes a pet"],
            0,
            "Charlotte lays her egg sac and dies, leaving Wilbur to protect her children.",
        ),
        (
            "Which idea is central to the story?",
            ["Loyalty and friendship", "Winning a contest", "Leaving the farm", "Learning to read"],
            0,
            "Wilbur and Charlotte's friendship is the emotional core of the book.",
        ),
    ],
    "The Lion, the Witch and the Wardrobe": [
        (
            "Which Pevensie child enters Narnia first?",
            ["Lucy", "Susan", "Peter", "Edmund"],
            0,
            "Lucy discovers Narnia through the wardrobe while playing hide and seek.",
        ),
        (
            "Who betrays the others for Turkish delight?",
            ["Edmund", "Peter", "Mr. Tumnus", "Mr. Beaver"],
            0,
            "Edmund is tempted by the White Witch and tells her about his siblings.",
        ),
        (
            "Who rules Narnia at the start of the book?",
            ["The White Witch", "Aslan", "The Beavers", "Father Christmas"],
            0,
            "The White Witch has kept Narnia in winter for a hundred years.",
        ),
        (
            "Why does Aslan give himself up?",
            ["To save Edmund", "To become king", "To find the wardrobe", "To stop the winter"],
            0,
            "Aslan offers his life in Edmund's place under the Deep Magic.",
        ),
        (
            "How do the children return home at the end?",
            ["Through the wardrobe", "On Aslan's back", "Through a portal in the forest", "By train"],
            0,
            "They chase a white stag and stumble back through the wardrobe door.",
        ),
    ],
    "James and the Giant Peach": [
        (
            "What causes the peach to grow enormous?",
            ["Magic crystals from an old man", "A strange fertilizer", "A spell from Aunt Sponge", "An earthquake"],
            0,
            "A stranger gives James a bag of magic crystals that spill at the peach tree.",
        ),
        (
            "Who are James's guardians?",
            ["Aunt Sponge and Aunt Spiker", "Mr. and Mrs. Trotter", "The Centipede and the Ladybug", "His grandparents"],
            0,
            "The two aunts are cruel and make James do all the work.",
        ),
        (
            "Where does the peach travel for most of the story?",
            ["Across the ocean", "Through a desert", "Under the ground", "Into space"],
            0,
            "The peach rolls into the sea and floats toward a new life.",
        ),
        (
            "Where does the peach finally land?",
            ["New York City", "London", "Paris", "Sydney"],
            0,
            "The peach comes down on the Empire State Building.",
        ),
        (
            "What happens to the insects at the end?",
            ["They become successful in the city", "They return to the peach tree", "They turn back into people", "They stay in the ocean"],
            0,
            "Each insect finds a new trade in New York, and James finds a home.",
        ),
    ],
    "Matilda": [
        (
            "What is Matilda's family name?",
            ["Wormwood", "Honey", "Trunchbull", "Phelps"],
            0,
            "Matilda Wormwood is the daughter of Mr. and Mrs. Wormwood.",
        ),
        (
            "Who is the headmistress of Matilda's school?",
            ["Miss Trunchbull", "Miss Honey", "Mrs. Phelps", "Miss Bell"],
            0,
            "Miss Trunchbull runs Crunchem Hall with fear and punishment.",
        ),
        (
            "Who is the kind teacher who believes in Matilda?",
            ["Miss Honey", "Miss Trunchbull", "Mrs. Phelps", "Matilda's mother"],
            0,
            "Miss Honey recognizes Matilda's intelligence and becomes her ally.",
        ),
        (
            "What unusual ability does Matilda discover?",
            ["Telekinesis", "Reading minds", "Invisibility", "Time travel"],
            0,
            "Matilda learns she can move objects with her mind.",
        ),
        (
            "How does Matilda help Miss Honey?",
            ["She scares Miss Trunchbull into leaving", "She cleans the school", "She writes a letter", "She calls the police"],
            0,
            "Matilda uses her power to drive Miss Trunchbull away and recover Miss Honey's home.",
        ),
    ],
    "Holes": [
        (
            "Where is Stanley Yelnats sent?",
            ["Camp Green Lake", "Camp Half-Blood", "Camp Warden", "Camp Sunshine"],
            0,
            "Stanley is sent to a juvenile detention camp in the Texas desert.",
        ),
        (
            "What are the boys forced to do every day?",
            ["Dig holes", "Build fences", "Wash dishes", "Plant trees"],
            0,
            "Each boy must dig one hole five feet deep and five feet wide every day.",
        ),
        (
            "Who is Zero?",
            ["Hector Zeroni", "Stanley's cousin", "The warden's son", "A counselor"],
            0,
            "Zero is Hector Zeroni, the boy who becomes Stanley's closest friend.",
        ),
        (
            "What is the warden searching for?",
            ["Buried treasure", "A lost child", "Water", "A map"],
            0,
            "The warden is looking for the treasure Kate Barlow buried years earlier.",
        ),
        (
            "What does Stanley carry out of the desert?",
            ["Zero", "A suitcase of gold", "A shovel", "A letter"],
            0,
            "Stanley carries Zero up the mountain, and together they survive and return.",
        ),
    ],
}

# (reader, title, correct_answers, days_ago)
ATTEMPTS = [
    ("Maya", "Charlotte's Web", 5, 18),
    ("Maya", "The Lion, the Witch and the Wardrobe", 4, 12),
    ("Maya", "James and the Giant Peach", 3, 8),
    ("Maya", "Matilda", 5, 3),
    ("Jonah", "Charlotte's Web", 4, 16),
    ("Jonah", "James and the Giant Peach", 4, 9),
    ("Jonah", "Matilda", 4, 2),
    ("Elena", "Charlotte's Web", 5, 14),
    ("Elena", "The Lion, the Witch and the Wardrobe", 3, 6),
]


def now_minus(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def build_quiz(title: str) -> GeneratedQuiz:
    questions = [
        GeneratedQuestion(
            prompt=prompt,
            choices=list(choices),
            answer_index=index,
            explanation=explanation,
        )
        for prompt, choices, index, explanation in QUIZZES[title]
    ]
    return GeneratedQuiz(questions=questions)


def seed(db_path: Path, force: bool) -> None:
    if db_path.exists():
        if not force:
            raise SystemExit(f"{db_path} already exists; pass --force to replace it")
        db_path.unlink()

    conn = db.connect(db_path)
    db.init_db(conn)

    books.import_books(conn, ROOT / "examples" / "books.example.csv")

    reader_ids: dict[str, int] = {}
    for name in READERS:
        cur = conn.execute(
            "INSERT INTO readers (name, created_at) VALUES (?, ?)",
            (name, now_minus(30)),
        )
        reader_ids[name] = int(cur.lastrowid)

    by_title = {b.title: b for b in books.list_books(conn)}
    quiz_ids: dict[str, int] = {}
    for title in QUIZZES:
        book = by_title[title]
        quiz_id = save_draft(conn, book.id, build_quiz(title), "demo-seed")
        assert quiz_id is not None
        quiz_ids[title] = quiz_id

    # Everything except Holes is approved; Holes stays in the review queue.
    for title, quiz_id in quiz_ids.items():
        if title != "Holes":
            review.approve_quiz(conn, quiz_id, actor="demo-parent")
            conn.execute(
                "UPDATE quizzes SET approved_at = ? WHERE id = ?",
                (now_minus(20), quiz_id),
            )
    conn.commit()

    for reader, title, correct, days in ATTEMPTS:
        quiz = review.get_quiz(conn, quiz_ids[title])
        assert quiz is not None
        attempt_id = points.start_attempt(conn, quiz.id, reader_ids[reader])
        answers = {
            question.id: (
                question.answer_index
                if i < correct
                else (question.answer_index + 1) % 4
            )
            for i, question in enumerate(quiz.questions)
        }
        points.submit_attempt(conn, attempt_id, answers)
        conn.execute(
            "UPDATE attempts SET started_at = ?, submitted_at = ? WHERE id = ?",
            (now_minus(days), now_minus(days), attempt_id),
        )
        conn.execute(
            "UPDATE points_ledger SET created_at = ? "
            "WHERE reader_id = ? AND book_id = ("
            "SELECT book_id FROM quizzes WHERE id = ?)",
            (now_minus(days), reader_ids[reader], quiz.id),
        )
    conn.commit()

    print(f"Seeded {db_path}")
    print(f"  readers: {len(READERS)}")
    print(f"  quizzes: {len(quiz_ids)} approved, 1 draft")
    print(f"  attempts: {len(ATTEMPTS)}")
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="demo.db", help="database path (default: demo.db)")
    parser.add_argument("--force", action="store_true", help="replace an existing database")
    args = parser.parse_args()
    seed(Path(args.db), args.force)


if __name__ == "__main__":
    main()
