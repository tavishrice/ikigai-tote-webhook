"""Shared Postgres helper. One place to get a connection from DATABASE_URL.

Uses psycopg 3 (psycopg[binary]). On Render, set DATABASE_URL to the database's
Internal Connection String. Rows are returned as tuples (positional); callers
that want dict rows pass row_factory explicitly when creating a cursor.
"""
import os
import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    # autocommit off by default; callers commit.
    return psycopg.connect(DATABASE_URL)


def execmany_ignore_conflict(cur, sql, rows):
    """Insert many rows; duplicates (ON CONFLICT DO NOTHING) are silently skipped."""
    if not rows:
        return 0
    cur.executemany(sql, rows)
    return cur.rowcount
