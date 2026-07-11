"""One-time copy of the local SQLite database into Supabase (Postgres).

Run this ONCE from your own machine when you first set up Supabase, so the
hosted app starts with everything you already have - resources (including
approved verification changes), verification history, share links, edit log.

Usage:
    DATABASE_URL="postgresql://...supabase.../postgres" \
        .venv/bin/python migrate_to_supabase.py

Safe to re-run only against an EMPTY Supabase database - it refuses to copy
into one that already has resources.
"""
import os
import sqlite3
import sys
from pathlib import Path

if not os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
    sys.exit("Set DATABASE_URL to your Supabase connection string first.")

import db  # noqa: E402  (reads DATABASE_URL -> Postgres mode)

assert db.IS_PG, "db.py did not enter Postgres mode - check DATABASE_URL"

SQLITE_PATH = Path(__file__).parent / "resources.db"
if not SQLITE_PATH.exists():
    sys.exit(f"No local database at {SQLITE_PATH} - nothing to migrate.")

TABLES = ("resources", "verification_checks", "shared_lists", "edit_log")


def main():
    db.init_db()  # create tables in Supabase
    src = sqlite3.connect(SQLITE_PATH)
    src.row_factory = sqlite3.Row
    dst = db.get_conn()

    existing = dst.execute("SELECT COUNT(*) FROM resources").fetchone()[0]
    if existing:
        sys.exit(f"Supabase already has {existing} resources - refusing to "
                 f"copy on top. Empty the tables first if you really mean to.")

    for table in TABLES:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"  {table}: 0 rows")
            continue
        cols = rows[0].keys()
        col_list = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        for row in rows:
            dst.execute(f"INSERT INTO {table} ({col_list}) VALUES ({marks})",
                        [row[c] for c in cols])
        # We copied explicit ids - move each identity sequence past them.
        if "id" in cols:
            dst.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"(SELECT MAX(id) FROM {table}))")
        print(f"  {table}: {len(rows)} rows")

    dst.commit()
    dst.close()
    src.close()
    print("Done - Supabase now mirrors your local database.")


if __name__ == "__main__":
    main()
