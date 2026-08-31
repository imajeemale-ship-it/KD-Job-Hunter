"""Persistent approval state for KD Job Hunter.

Uses a separate table inside applications.db so the upstream MR.Jobs tracker can
remain untouched and future upstream updates are easier to merge.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "applications.db"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS kd_approvals (
            job_id TEXT PRIMARY KEY,
            nonce TEXT UNIQUE NOT NULL,
            telegram_message_id INTEGER,
            status TEXT NOT NULL DEFAULT 'waiting',
            created_at TEXT NOT NULL,
            decided_at TEXT,
            submitted_at TEXT,
            error TEXT DEFAULT ''
        )
        """
    )
    conn.commit()
    return conn


def create_waiting(job_id: str, nonce: str, telegram_message_id: int) -> None:
    conn = _db()
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO kd_approvals
            (job_id, nonce, telegram_message_id, status, created_at)
            VALUES (?, ?, ?, 'waiting', ?)
            """,
            (job_id, nonce, telegram_message_id, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_by_job(job_id: str):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM kd_approvals WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_by_nonce(nonce: str):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM kd_approvals WHERE nonce = ?", (nonce,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_decision(job_id: str, approved: bool) -> None:
    conn = _db()
    try:
        conn.execute(
            """
            UPDATE kd_approvals
            SET status = ?, decided_at = ?
            WHERE job_id = ?
            """,
            ("approved" if approved else "declined", datetime.now().isoformat(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_submitted(job_id: str) -> None:
    conn = _db()
    try:
        conn.execute(
            """
            UPDATE kd_approvals
            SET status = 'submitted', submitted_at = ?, error = ''
            WHERE job_id = ?
            """,
            (datetime.now().isoformat(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_failed(job_id: str, error: str) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE kd_approvals SET status = 'failed', error = ? WHERE job_id = ?",
            (str(error)[:2000], job_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_approved() -> list[dict]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM kd_approvals WHERE status = 'approved' ORDER BY decided_at"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
