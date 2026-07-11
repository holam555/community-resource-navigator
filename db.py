"""SQLite helpers for the Community Resource Navigator."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "resources.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    type TEXT,
    area TEXT,
    org_name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    location TEXT,
    hours TEXT,
    website TEXT,
    notes TEXT,
    bilingual_support TEXT,
    accessibility TEXT,
    insurance_cost TEXT,
    eligibility_criteria TEXT,
    income_based INTEGER,            -- 1 / 0 / NULL (unknown)
    has_flyer INTEGER,
    has_application_form INTEGER,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'needs_review', 'inactive')),
    seasonal_note TEXT,
    last_verified_date TEXT,         -- ISO date
    verification_note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_resources_category ON resources(category);
CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status);

CREATE TABLE IF NOT EXISTS verification_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id INTEGER NOT NULL REFERENCES resources(id),
    verdict TEXT NOT NULL CHECK (verdict IN
        ('confirmed', 'changed', 'possibly_inactive', 'unreachable',
         'needs_manual_check', 'error')),
    confidence REAL,
    proposed_changes TEXT,           -- JSON: {"field": "new_value", ...}
    evidence TEXT,                   -- quote from the page that supports the verdict
    source_url TEXT,
    http_status INTEGER,
    error TEXT,
    checked_at TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN
        ('pending', 'approved', 'rejected', 'auto_applied', 'superseded')),
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_checks_review_status ON verification_checks(review_status);
CREATE INDEX IF NOT EXISTS idx_checks_resource ON verification_checks(resource_id);

-- Shareable match lists (resource info only - never any family/patient data)
CREATE TABLE IF NOT EXISTS shared_lists (
    token TEXT PRIMARY KEY,
    payload TEXT NOT NULL,          -- JSON array of resource cards
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
