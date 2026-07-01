"""SQLite persistence for Provenance Guard.

Implements the data model from planning.md §9. Three tables are defined:

* ``content``   — one row per submission (the decision record)
* ``appeals``   — one row per appeal (Milestone 5)
* ``audit_log`` — append-only structured event log (feature 7)

Milestone 3 writes to ``content`` and ``audit_log``; Milestone 5 adds appeal
records, status transitions, and the ``status`` / ``appeal_reasoning`` audit
columns. Every helper opens its own short-lived connection so the module is
safe to use from Flask's multi-threaded dev server.
"""
import json
import os
import sqlite3

DB_PATH = os.environ.get(
    "PROVENANCE_DB",
    os.path.join(os.path.dirname(__file__), "provenance_guard.db"),
)


def get_connection():
    """Open a new SQLite connection with dict-like row access."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the tables if they do not already exist (idempotent)."""
    conn = get_connection()
    try:
        with conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS content (
                    content_id     TEXT PRIMARY KEY,
                    created_at     TEXT NOT NULL,
                    text           TEXT NOT NULL,
                    word_count     INTEGER NOT NULL,
                    ai_probability REAL,
                    confidence     REAL,
                    label_variant  TEXT,
                    label_text     TEXT,
                    p_llm          REAL,
                    p_stylo        REAL,
                    status         TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS appeals (
                    appeal_id              TEXT PRIMARY KEY,
                    content_id             TEXT NOT NULL,
                    created_at             TEXT NOT NULL,
                    reason                 TEXT,
                    creator_id             TEXT,
                    original_label_variant TEXT,
                    original_confidence    REAL,
                    status                 TEXT NOT NULL,
                    FOREIGN KEY (content_id) REFERENCES content (content_id)
                );

                CREATE TABLE IF NOT EXISTS audit_log (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp      TEXT NOT NULL,
                    event_type     TEXT NOT NULL,
                    content_id     TEXT,
                    appeal_id      TEXT,
                    ai_probability REAL,
                    confidence     REAL,
                    label_variant  TEXT,
                    p_llm          REAL,
                    p_stylo        REAL,
                    signals_json   TEXT,
                    status         TEXT,
                    appeal_reasoning TEXT,
                    detail         TEXT
                );
                """
            )
        # Bring older databases (created before Milestone 5) up to the current
        # schema without dropping data: add any columns introduced in M5.
        _ensure_columns(
            conn,
            "audit_log",
            {"status": "TEXT", "appeal_reasoning": "TEXT"},
        )
    finally:
        conn.close()


def _ensure_columns(conn, table, columns):
    """Idempotently add missing columns to an existing table (simple migration).

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a database
    created by an earlier milestone keeps its old shape. This adds any newly
    introduced columns via ``ALTER TABLE`` only when they are absent.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    with conn:
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def insert_content(record):
    """Persist a content decision record (see planning.md §9 `content`)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO content (
                    content_id, created_at, text, word_count, ai_probability,
                    confidence, label_variant, label_text, p_llm, p_stylo, status
                ) VALUES (
                    :content_id, :created_at, :text, :word_count, :ai_probability,
                    :confidence, :label_variant, :label_text, :p_llm, :p_stylo, :status
                )
                """,
                record,
            )
    finally:
        conn.close()


def insert_audit_log(entry):
    """Append one structured row to the audit log (see planning.md §9).

    ``appeal_id``, ``status`` and ``appeal_reasoning`` are optional; they default
    to ``None`` for plain submission events and are populated for appeal events.
    """
    entry = {"appeal_id": None, "status": None, "appeal_reasoning": None, **entry}
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    timestamp, event_type, content_id, appeal_id, ai_probability,
                    confidence, label_variant, p_llm, p_stylo, signals_json,
                    status, appeal_reasoning, detail
                ) VALUES (
                    :timestamp, :event_type, :content_id, :appeal_id, :ai_probability,
                    :confidence, :label_variant, :p_llm, :p_stylo, :signals_json,
                    :status, :appeal_reasoning, :detail
                )
                """,
                entry,
            )
    finally:
        conn.close()


def get_content(content_id):
    """Return a single content record as a dict, or ``None`` if unknown."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM content WHERE content_id = ?", (content_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def update_content_status(content_id, status):
    """Flip a content record's status (e.g. ``labeled`` -> ``under_review``).

    Returns the number of rows updated (0 if the ``content_id`` is unknown).
    """
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE content SET status = ? WHERE content_id = ?",
                (status, content_id),
            )
            return cur.rowcount
    finally:
        conn.close()


def insert_appeal(record):
    """Persist an appeal record (see planning.md §9 `appeals`)."""
    conn = get_connection()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO appeals (
                    appeal_id, content_id, created_at, reason, creator_id,
                    original_label_variant, original_confidence, status
                ) VALUES (
                    :appeal_id, :content_id, :created_at, :reason, :creator_id,
                    :original_label_variant, :original_confidence, :status
                )
                """,
                record,
            )
    finally:
        conn.close()


def get_review():
    """Return the open-appeal review queue (planning.md §6, `GET /review`).

    Joins each still-open appeal to its content record so a human reviewer sees
    the creator's reasoning next to the original decision and per-signal
    breakdown that is being contested, plus a short excerpt of the text.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT a.appeal_id, a.content_id, a.reason, a.creator_id,
                   a.original_label_variant, a.original_confidence, a.created_at,
                   c.text, c.ai_probability, c.p_llm, c.p_stylo, c.status
            FROM appeals a
            JOIN content c ON c.content_id = a.content_id
            WHERE a.status = 'open'
            ORDER BY a.created_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    queue = []
    for row in rows:
        item = dict(row)
        text = item.pop("text", "") or ""
        excerpt = text[:200] + ("…" if len(text) > 200 else "")
        queue.append(
            {
                "appeal_id": item["appeal_id"],
                "content_id": item["content_id"],
                "creator_reasoning": item["reason"],
                "creator_id": item["creator_id"],
                "original_label_variant": item["original_label_variant"],
                "original_confidence": item["original_confidence"],
                "original_ai_probability": item["ai_probability"],
                "original_signals": {"p_llm": item["p_llm"], "p_stylo": item["p_stylo"]},
                "current_status": item["status"],
                "text_excerpt": excerpt,
                "created_at": item["created_at"],
            }
        )
    return queue


def get_log(limit=50):
    """Return the most recent audit-log entries, newest first.

    The stored ``signals_json`` blob is expanded into a nested ``signals`` object
    so ``GET /log`` returns clean structured JSON.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()

    entries = []
    for row in rows:
        entry = dict(row)
        raw = entry.pop("signals_json", None)
        if raw:
            try:
                entry["signals"] = json.loads(raw)
            except (ValueError, TypeError):
                entry["signals"] = None
        else:
            entry["signals"] = None
        entries.append(entry)
    return entries
