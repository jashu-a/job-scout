"""
Database module for job deduplication using SQLite.
Stores seen jobs so we never send duplicates to Telegram.
"""

import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "jobs.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_hash     TEXT PRIMARY KEY,
            title        TEXT,
            company      TEXT,
            location     TEXT,
            link         TEXT,
            match_score  REAL,
            matched      INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    return conn


def make_hash(title: str, company: str, location: str) -> str:
    """Create a unique hash for a job based on title + company + location."""
    raw = f"{title.strip().lower()}|{company.strip().lower()}|{location.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def is_seen(conn: sqlite3.Connection, job_hash: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_jobs WHERE job_hash = ?", (job_hash,)).fetchone()
    return row is not None


def mark_seen(
    conn: sqlite3.Connection,
    job_hash: str,
    title: str,
    company: str,
    location: str,
    link: str,
    match_score: float,
    matched: bool,
):
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_jobs (job_hash, title, company, location, link, match_score, matched, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_hash, title, company, location, link, match_score, int(matched), datetime.utcnow().isoformat()),
    )
    conn.commit()


def get_stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
    matched = conn.execute("SELECT COUNT(*) FROM seen_jobs WHERE matched = 1").fetchone()[0]
    return {"total_seen": total, "total_matched": matched}
