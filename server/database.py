import sqlite3
import json
from pathlib import Path

DB_PATH    = Path(__file__).parent / "nl_qa.db"
SEED_DIR   = Path(__file__).parent


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS qa_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            rec_key     TEXT    UNIQUE NOT NULL,
            question    TEXT    NOT NULL DEFAULT '',
            answer      TEXT    NOT NULL DEFAULT '',
            subject     TEXT    DEFAULT '',
            reg_date    TEXT    DEFAULT '',
            answer_date TEXT    DEFAULT '',
            answer_lib  TEXT    DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")

        conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS qa_fts USING fts5(
            question,
            answer,
            subject,
            content     = qa_items,
            content_rowid = id,
            tokenize    = 'unicode61'
        )""")

        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS qa_ai AFTER INSERT ON qa_items BEGIN
            INSERT INTO qa_fts(rowid, question, answer, subject)
            VALUES (new.id, new.question, new.answer, new.subject);
        END""")

        conn.execute("""
        CREATE TRIGGER IF NOT EXISTS qa_au AFTER UPDATE ON qa_items BEGIN
            INSERT INTO qa_fts(qa_fts, rowid, question, answer, subject)
            VALUES ('delete', old.id, old.question, old.answer, old.subject);
            INSERT INTO qa_fts(rowid, question, answer, subject)
            VALUES (new.id, new.question, new.answer, new.subject);
        END""")

        conn.commit()


def seed_from_json() -> int:
    """DB가 비어있으면 seed JSON 파일들에서 데이터를 일괄 삽입. 삽입 건수 반환."""
    if count_items() > 0:
        return 0
    seed_files = sorted(SEED_DIR.glob("nl_qa_seed_*.json"))
    if not seed_files:
        return 0
    total = 0
    with get_conn() as conn:
        for path in seed_files:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            conn.executemany("""
                INSERT OR IGNORE INTO qa_items
                    (rec_key, question, answer, subject, answer_date, answer_lib)
                VALUES (:rec_key, :question, :answer, :subject, :answer_date, :answer_lib)
            """, data)
            total += len(data)
        conn.commit()
    return total


def upsert_item(rec_key: str, question: str, answer: str, subject: str,
                reg_date: str = "", answer_date: str = "", answer_lib: str = "") -> bool:
    """Insert or update a Q&A item. Returns True if newly inserted."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM qa_items WHERE rec_key = ?", (rec_key,)
        ).fetchone()

        if existing:
            conn.execute("""
            UPDATE qa_items SET question=?, answer=?, subject=?,
                reg_date=?, answer_date=?, answer_lib=?
            WHERE rec_key=?
            """, (question, answer, subject, reg_date, answer_date, answer_lib, rec_key))
            conn.commit()
            return False
        else:
            conn.execute("""
            INSERT INTO qa_items (rec_key, question, answer, subject, reg_date, answer_date, answer_lib)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (rec_key, question, answer, subject, reg_date, answer_date, answer_lib))
            conn.commit()
            return True


def get_known_keys() -> set:
    """Return all rec_keys already in the database."""
    with get_conn() as conn:
        rows = conn.execute("SELECT rec_key FROM qa_items").fetchall()
    return {r["rec_key"] for r in rows}


def count_items() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM qa_items").fetchone()[0]


def get_items_for_index() -> list:
    """Return all items for building the vector index."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, rec_key, question, answer, subject FROM qa_items"
        ).fetchall()
    return [dict(r) for r in rows]
