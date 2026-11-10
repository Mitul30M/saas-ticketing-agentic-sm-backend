"""Database connection pool, file-based migrations and trace logging.

Migration mechanism copied from the reference repo (QuickDrop helpdesk agent):
a `schema_migrations` table records applied filenames; every SQL file in
db/migrations/*.sql runs in one transaction, `IF NOT EXISTS` makes DDL idempotent.
All SQL is parameterised; ticket/customer text is ALWAYS passed as parameters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row

BASE_DIR = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = BASE_DIR / "db" / "migrations"

# DB rows are always read as dicts (row_factory=dict_row).
type DictConn = "psycopg.Connection[dict[str, Any]]"


def connect(conninfo: str) -> DictConn:
    """Bound-timeout DB connection: fail explicitly, never hang."""
    if "connect_timeout" not in conninfo:
        sep = "&" if "?" in conninfo else "?"
        conninfo = f"{conninfo}{sep}connect_timeout=5"
    try:
        conn = psycopg.connect(conninfo, row_factory=dict_row, autocommit=True)
        return cast(DictConn, conn)
    except psycopg.OperationalError as exc:
        raise RuntimeError(f"database connect failed: {exc}") from exc


def run_migrations(conn: DictConn, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply numbered .sql files in order; skip already-applied ones. Returns applied."""
    with conn.transaction():
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "migration TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
    applied = {
        row["migration"] for row in conn.execute("SELECT migration FROM schema_migrations").fetchall()
    }
    files = sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())
    applied_now: list[str] = []
    for path in files:
        if path.name in applied:
            continue
        sql = _strip_full_line_comments(path.read_text(encoding="utf-8"))
        with conn.transaction():
            # One execute per file: plpgsql bodies keep their semicolons, the file
            # applies atomically, and psycopg runs multi-statement SQL via the
            # simple query protocol when no parameters are passed.
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (migration) VALUES (%s)", (path.name,)
            )
        applied_now.append(path.name)
    return applied_now


def _strip_full_line_comments(sql: str) -> str:
    """Drop whole-line `--` comments so naive `;`-sensitivity in DDL is safe.

    Only full-line comments are removed (code + trailing comments untouched);
    plpgsql function bodies have no full-line comments in our migrations."""
    return "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )


def trace(
    conn: DictConn,
    ticket_id: str,
    trace_id: str,
    node: str,
    kind: str,
    input_: dict | None = None,
    output=None,
    *,
    status: str = "ok",
    error: str | None = None,
    duration_ms: int | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    cache_hit: bool | None = None,
) -> None:
    """Persist an observability step into trace_events (created by 004_platform.sql).

    Callers trace one row per node/tool-call/LLM-call; JSONB columns always receive
    valid JSON text. Fails open loudly: if the platform tables are not migrated yet,
    the caller (tests/setup) sees the error instead of losing the audit silently.
    """
    conn.execute(
        "INSERT INTO trace_events (trace_id, ticket_id, node, kind, input, output, "
        "status, error, duration_ms, tokens_in, tokens_out, cost_usd, cache_hit) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            trace_id,
            ticket_id,
            node,
            kind,
            json.dumps(input_, default=str) if input_ is not None else None,
            json.dumps(output, default=str) if output is not None else None,
            status,
            error,
            duration_ms,
            tokens_in,
            tokens_out,
            cost_usd,
            cache_hit,
        ),
    )
