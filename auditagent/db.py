"""
AuditAgent — Database abstraction
Local development: SQLite, zero setup, same as before.
Production (Render): Postgres, via the DATABASE_URL Render sets
automatically when a Postgres instance is attached.

Render's filesystem is ephemeral — anything written to disk (the old
SQLite file) disappears on every redeploy. This isn't a "nice to have
for scale" migration, it's required for data to survive at all once
this is actually deployed there.

Existing code was written with sqlite3's `?` placeholders throughout;
rather than rewriting every query, `execute()` translates them to
Postgres's `%s` automatically when running against Postgres, so most
call sites needed zero SQL changes — only schema-creation and
introspection code (which genuinely differ between the two) needed
dialect-specific branches.
"""

import os
import sqlite3

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")))

if IS_POSTGRES:
    import psycopg2
    import psycopg2.extras

SQLITE_DB_PATH = os.path.join(os.getcwd(), "auditagent_history.db")

AUTOINCREMENT_PK = "SERIAL PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"


def get_connection():
    if IS_POSTGRES:
        return psycopg2.connect(DATABASE_URL)
    return sqlite3.connect(SQLITE_DB_PATH)


def execute(conn, sql: str, params: tuple = ()):
    if IS_POSTGRES:
        sql = sql.replace("?", "%s")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def table_exists(conn, table_name: str) -> bool:
    if IS_POSTGRES:
        cur = execute(conn, "SELECT tablename FROM pg_tables WHERE tablename = ?", (table_name,))
    else:
        cur = execute(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table_name,)
        )
    return cur.fetchone() is not None


def column_exists(conn, table_name: str, column_name: str) -> bool:
    if IS_POSTGRES:
        cur = execute(
            conn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            (table_name, column_name),
        )
        return cur.fetchone() is not None
    else:
        cur = conn.execute(f"PRAGMA table_info({table_name})")
        return any(row[1] == column_name for row in cur.fetchall())


def dict_rows(cursor) -> list:
    """Portable equivalent of sqlite3.Row — works for both dialects since
    both sqlite3 and psycopg2 cursors expose .description with column
    names, unlike sqlite3.Row which is sqlite-specific."""
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def insert_returning_id(conn, sql: str, params: tuple) -> int:
    """cursor.lastrowid is a sqlite3-specific convenience — psycopg2
    doesn't support it the same way. Postgres needs INSERT ... RETURNING
    id instead, so this branches once here rather than at every call
    site that needs a newly-inserted row's id."""
    if IS_POSTGRES:
        cur = execute(conn, sql + " RETURNING id", params)
        return cur.fetchone()[0]
    else:
        cur = execute(conn, sql, params)
        return cur.lastrowid


def upsert_sql(table: str, key_cols: list, all_cols: list) -> str:
    placeholders = ", ".join(["?"] * len(all_cols))
    update_cols = [c for c in all_cols if c not in key_cols]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO {table} ({', '.join(all_cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT ({', '.join(key_cols)}) DO UPDATE SET {update_clause}"
    )
