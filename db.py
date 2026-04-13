"""
Database module for job deduplication using SQLite.
Stores seen jobs so we never send duplicates to Telegram.

Dual dedup strategy:
1. Hash-based: SHA256(title + company + location) — catches exact matches
2. URL-based: Normalized job link — catches same job with slightly different metadata
"""

import sqlite3
import hashlib
import re
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
            link_hash    TEXT,
            match_score  REAL,
            matched      INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrate old databases: add link_hash column if missing
    cursor = conn.execute("PRAGMA table_info(seen_jobs)")
    columns = [row[1] for row in cursor.fetchall()]
    if "link_hash" not in columns:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN link_hash TEXT")
        conn.commit()

    # Index for fast URL-based lookups
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_link_hash ON seen_jobs (link_hash)
    """)
    conn.commit()
    return conn


def make_hash(title: str, company: str, location: str) -> str:
    """Create a unique hash for a job based on title + company + location."""
    raw = f"{title.strip().lower()}|{company.strip().lower()}|{location.strip().lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _normalize_url(url: str) -> str:
    """
    Normalize a job URL to catch duplicates with different query params.

    Examples:
        https://jp.linkedin.com/jobs/view/devops-engineer-at-bloomtech-4373401636?tracking=abc
        → linkedin.com/jobs/view/4373401636

        https://jp.indeed.com/viewjob?jk=abc123&from=web
        → indeed.com/viewjob?jk=abc123
    """
    if not url:
        return ""

    url = url.strip().lower()

    # Remove protocol and www
    url = re.sub(r'^https?://(www\.)?', '', url)
    # Remove country prefix for linkedin (jp.linkedin.com → linkedin.com)
    url = re.sub(r'^[a-z]{2}\.linkedin\.com', 'linkedin.com', url)
    # Remove country prefix for indeed
    url = re.sub(r'^[a-z]{2}\.indeed\.com', 'indeed.com', url)

    # LinkedIn: extract the job ID from the URL
    li_match = re.search(r'linkedin\.com/jobs/view/[^/]*?(\d{5,})', url)
    if li_match:
        return f"linkedin.com/jobs/view/{li_match.group(1)}"

    # Indeed: keep just the job key
    indeed_match = re.search(r'indeed\.com/viewjob\?jk=([a-f0-9]+)', url)
    if indeed_match:
        return f"indeed.com/viewjob?jk={indeed_match.group(1)}"

    # TokyoDev: strip query params
    if "tokyodev.com" in url:
        return url.split("?")[0].rstrip("/")

    # JapanDev: strip query params
    if "japan-dev.com" in url:
        return url.split("?")[0].rstrip("/")

    # GaijinPot: extract job ID from /en/job/NNNN/details/...
    gp_match = re.search(r'gaijinpot\.com/en/job/(\d+)', url)
    if gp_match:
        return f"gaijinpot.com/job/{gp_match.group(1)}"

    # Generic: strip query params and trailing slashes
    return url.split("?")[0].rstrip("/")


def make_link_hash(url: str) -> str:
    """Create a hash from a normalized URL."""
    normalized = _normalize_url(url)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def is_seen(conn: sqlite3.Connection, job_hash: str) -> bool:
    """Check if a job has been seen by its content hash."""
    row = conn.execute("SELECT 1 FROM seen_jobs WHERE job_hash = ?", (job_hash,)).fetchone()
    return row is not None


def is_seen_url(conn: sqlite3.Connection, url: str) -> bool:
    """Check if a job has been seen by its URL (catches same job with different metadata)."""
    link_hash = make_link_hash(url)
    if not link_hash:
        return False
    row = conn.execute("SELECT 1 FROM seen_jobs WHERE link_hash = ?", (link_hash,)).fetchone()
    return row is not None


def is_duplicate(conn: sqlite3.Connection, job_hash: str, url: str) -> bool:
    """Check both hash and URL — either match means it's a duplicate."""
    return is_seen(conn, job_hash) or is_seen_url(conn, url)


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
    link_hash = make_link_hash(link)
    conn.execute(
        """
        INSERT OR IGNORE INTO seen_jobs (job_hash, title, company, location, link, link_hash, match_score, matched, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_hash, title, company, location, link, link_hash, match_score, int(matched), datetime.utcnow().isoformat()),
    )
    conn.commit()


def get_stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) FROM seen_jobs").fetchone()[0]
    matched = conn.execute("SELECT COUNT(*) FROM seen_jobs WHERE matched = 1").fetchone()[0]
    return {"total_seen": total, "total_matched": matched}


# ═══════════════════════════════════════════════════════════════════════════════
# METADATA TABLE — stores resume hash to detect changes between runs
# ═══════════════════════════════════════════════════════════════════════════════

def _ensure_metadata_table(conn: sqlite3.Connection):
    """Create metadata table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def get_metadata(conn: sqlite3.Connection, key: str) -> str | None:
    """Get a metadata value by key."""
    _ensure_metadata_table(conn)
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_metadata(conn: sqlite3.Connection, key: str, value: str):
    """Set a metadata value (upsert)."""
    _ensure_metadata_table(conn)
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()


def hash_resume(resume_text: str) -> str:
    """Create a hash of the resume content to detect changes."""
    return hashlib.sha256(resume_text.strip().encode()).hexdigest()[:16]


def get_next_job_id(conn: sqlite3.Connection) -> int:
    """Get the next sequential job ID and increment the counter."""
    _ensure_metadata_table(conn)
    row = conn.execute("SELECT value FROM metadata WHERE key = 'next_job_id'").fetchone()
    current_id = int(row[0]) if row else 1
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('next_job_id', ?)",
        (str(current_id + 1),),
    )
    conn.commit()
    return current_id


def get_rescore_candidates(conn: sqlite3.Connection, threshold: int, max_age_days: int = 30) -> list[dict]:
    """
    Get jobs that scored below threshold — candidates for rescoring
    when resume changes. Ignores jobs older than max_age_days since
    those postings are likely removed.
    """
    rows = conn.execute(
        """
        SELECT job_hash, title, company, location, link, match_score, created_at
        FROM seen_jobs
        WHERE matched = 0 AND match_score > 0 AND match_score < ?
          AND created_at >= datetime('now', ?)
        ORDER BY match_score DESC
        """,
        (threshold, f"-{max_age_days} days"),
    ).fetchall()

    return [
        {
            "job_hash": r[0],
            "title": r[1],
            "company": r[2],
            "location": r[3],
            "link": r[4],
            "match_score": r[5],
        }
        for r in rows
    ]


def update_job_score(conn: sqlite3.Connection, job_hash: str, new_score: float, matched: bool):
    """Update a job's score and matched status after rescoring."""
    conn.execute(
        "UPDATE seen_jobs SET match_score = ?, matched = ? WHERE job_hash = ?",
        (new_score, int(matched), job_hash),
    )
    conn.commit()