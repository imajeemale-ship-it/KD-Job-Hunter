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


def set_decision(job_id: str, approved: bool) -> bool:
    conn = _db()
    try:
        cursor = conn.execute(
            """
            UPDATE kd_approvals
            SET status = ?, decided_at = ?
            WHERE job_id = ? AND status = 'waiting' AND submitted_at IS NULL
            """,
            ("approved" if approved else "declined", datetime.now().isoformat(), job_id),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def mark_submitted(job_id: str) -> None:
    conn = _db()
    try:
        conn.execute(
            """
            UPDATE kd_approvals
            SET status = 'submitted', submitted_at = ?, error = ''
            WHERE job_id = ? AND status = 'submitting' AND submitted_at IS NULL
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
            "UPDATE kd_approvals SET status = 'failed', error = ? WHERE job_id = ? AND status = 'submitting' AND submitted_at IS NULL",
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


def claim_submission(job_id: str, max_per_day: int = 5) -> bool:
    """Serialize live work across workers; interrupted claims require manual review."""
    conn = _db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM kd_approvals WHERE status = 'submitting'").fetchone():
            return False
        count = conn.execute("""
            SELECT COUNT(*) FROM applications WHERE id IN (
                SELECT job_id FROM kd_approvals WHERE DATE(submitted_at) = DATE('now')
                UNION SELECT id FROM applications WHERE status = 'applied' AND DATE(applied_at) = DATE('now')
            )
        """).fetchone()[0]
        if count >= min(5, max_per_day):
            return False
        cursor = conn.execute("""
            UPDATE kd_approvals SET status = 'submitting', error = ''
            WHERE job_id = ? AND status = 'approved' AND submitted_at IS NULL
            AND EXISTS (SELECT 1 FROM applications WHERE id = ?
                        AND status IN ('matched', 'failed', 'discovered'))
        """, (job_id, job_id))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def retry_failed(job_id: str) -> bool:
    """Explicit operator action only; ambiguous outcomes require receipt review first."""
    conn = _db()
    try:
        cursor = conn.execute("""
            UPDATE kd_approvals SET status = 'approved', error = ''
            WHERE job_id = ? AND status = 'failed' AND decided_at IS NOT NULL
            AND submitted_at IS NULL AND error NOT LIKE '%unknown%'
            AND error NOT LIKE '%Application adapter returned false%'
        """, (job_id,))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
