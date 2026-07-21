"""
Ikigai Contribution — Postgres MCP server
=========================================

A small remote MCP server that gives Claude (Cowork / claude.ai) direct query
access to the Ikigai Contribution Postgres database.

WHY THIS EXISTS
---------------
Cloud Cowork sessions run in a sandbox whose outbound firewall allows HTTPS
(443) but BLOCKS raw Postgres (5432). So a direct `psycopg.connect(...)` from a
session always times out even though the credentials are correct. This server
runs *inside Render* (where 5432 is reachable) and exposes the database to
Claude over HTTPS via the Model Context Protocol — which is allowed.

TRANSPORT
---------
Streamable HTTP (the transport claude.ai custom connectors speak). The MCP
endpoint is mounted at "/mcp". A tiny "/" health route is also served.

AUTH
----
Every request must present the shared secret in one of:
  * Authorization: Bearer <MCP_TOKEN>
  * X-API-Key: <MCP_TOKEN>
  * ?token=<MCP_TOKEN>   (query string — for connector UIs that can't set headers)
Set MCP_TOKEN in the Render env. If MCP_TOKEN is unset the server refuses to
start (fail closed).

ENV VARS
--------
  DATABASE_URL   Render-internal Postgres URL (fast; injected by Render if you
                 link the database to this service). Required.
  MCP_TOKEN      Shared secret required on every request. Required.
  PORT           Injected by Render. Defaults to 8000 locally.
  DB_STATEMENT_TIMEOUT_MS   Per-query timeout. Default 30000.

DEPLOY
------
Render web service, Start command:  python mcp_server.py
Requirements:  mcp>=1.2  uvicorn>=0.30  'psycopg[binary]>=3.1'
"""

import json
import os
import re
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg.rows import dict_row

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

DATABASE_URL = os.environ.get("DATABASE_URL")
MCP_TOKEN = os.environ.get("MCP_TOKEN")
STMT_TIMEOUT_MS = int(os.environ.get("DB_STATEMENT_TIMEOUT_MS", "30000"))
PORT = int(os.environ.get("PORT", "8000"))

if not MCP_TOKEN:
    raise SystemExit(
        "MCP_TOKEN is not set. Refusing to start an unauthenticated database "
        "endpoint. Set MCP_TOKEN in the Render environment."
    )
if not DATABASE_URL:
    raise SystemExit(
        "DATABASE_URL is not set. Link the Render Postgres to this service or "
        "set DATABASE_URL in the environment."
    )

# Heuristic: statements that are pure reads. Anything else needs allow_write=True.
_READ_ONLY_RE = re.compile(r"^\s*(?:with\b.*?\bselect\b|select|explain|show|table)\b",
                           re.IGNORECASE | re.DOTALL)


def _json_default(o):
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        # keep integers as ints, otherwise float
        return int(o) if o == o.to_integral_value() else float(o)
    if isinstance(o, (bytes, bytearray, memoryview)):
        return bytes(o).decode("utf-8", "replace")
    return str(o)


def _connect():
    """One short-lived connection per call with a hard statement timeout."""
    conn = psycopg.connect(DATABASE_URL, connect_timeout=15, row_factory=dict_row)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout = %s", (STMT_TIMEOUT_MS,))
    return conn


# --------------------------------------------------------------------------- #
# MCP server + tools
# --------------------------------------------------------------------------- #

mcp = FastMCP("ikigai-contribution-db", stateless_http=True)


@mcp.tool()
def run_sql(sql: str, allow_write: bool = False, max_rows: int = 200) -> str:
    """Run a SQL statement against the Ikigai Contribution Postgres database.

    By default only read statements (SELECT / WITH … SELECT / EXPLAIN / SHOW /
    TABLE) are permitted. To run an INSERT / UPDATE / DELETE / DDL / function
    call that writes, pass allow_write=true — this is a deliberate guardrail so
    writes are never accidental (the event table is append-only; after any
    manual write you must call refresh_day for each affected ET-day).

    Args:
        sql: The SQL to execute. A single statement.
        allow_write: Must be true to run anything that is not a plain read.
        max_rows: Cap on returned rows for reads (hard cap 2000).

    Returns:
        JSON. For reads: {"columns": [...], "rows": [...], "row_count": n,
        "truncated": bool}. For writes: {"status": "...", "rowcount": n}.
    """
    max_rows = max(1, min(int(max_rows), 2000))
    is_read = bool(_READ_ONLY_RE.match(sql or ""))

    if not is_read and not allow_write:
        return json.dumps({
            "error": "write_blocked",
            "detail": ("This statement looks like a write/DDL. Re-call with "
                       "allow_write=true if you really intend to modify data. "
                       "Remember to refresh_day() the affected ET-day(s) after."),
        })

    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is not None:
                    cols = [d.name for d in cur.description]
                    rows = cur.fetchmany(max_rows + 1)
                    truncated = len(rows) > max_rows
                    rows = rows[:max_rows]
                    conn.commit()
                    return json.dumps({
                        "columns": cols,
                        "rows": rows,
                        "row_count": len(rows),
                        "truncated": truncated,
                    }, default=_json_default)
                else:
                    conn.commit()
                    return json.dumps({
                        "status": cur.statusmessage,
                        "rowcount": cur.rowcount,
                    })
    except Exception as e:  # noqa: BLE001 — surface DB errors to the model
        return json.dumps({"error": type(e).__name__, "detail": str(e)})


@mcp.tool()
def list_schema(table: str = "") -> str:
    """List tables and columns in the public schema.

    Args:
        table: Optional table name to restrict the output to one table.

    Returns:
        JSON list of {table, column, type, nullable, default}.
    """
    where = "WHERE c.table_schema = 'public'"
    params: list = []
    if table:
        where += " AND c.table_name = %s"
        params.append(table)
    q = f"""
        SELECT c.table_name  AS "table",
               c.column_name  AS "column",
               c.data_type    AS "type",
               c.is_nullable  AS "nullable",
               c.column_default AS "default"
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema AND t.table_name = c.table_name
         AND t.table_type = 'BASE TABLE'
        {where}
        ORDER BY c.table_name, c.ordinal_position
    """
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(q, params)
                return json.dumps(cur.fetchall(), default=_json_default)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": type(e).__name__, "detail": str(e)})


@mcp.tool()
def event_snapshot() -> str:
    """Live orientation snapshot — the canonical 'what is flowing and how fresh'
    query: counts and latest ts of the append-only event table grouped by
    stage + source. This is the first move for any new session.
    """
    q = ("SELECT stage, source, count(*) AS n, max(ts) AS latest "
         "FROM event GROUP BY 1, 2 ORDER BY n DESC")
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(q)
                return json.dumps(cur.fetchall(), default=_json_default)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": type(e).__name__, "detail": str(e)})


@mcp.tool()
def refresh_day(et_day: str) -> str:
    """Refresh the rollups for one Eastern-Time day after a manual write/delete.
    Calls refresh_stage_contribution_day(d), refresh_contribution_day(d) and
    refresh_shift_day(d) in order. Call this for EVERY ET-day you touched.

    Args:
        et_day: The day as 'YYYY-MM-DD'.
    """
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", et_day or ""):
        return json.dumps({"error": "bad_date", "detail": "Use 'YYYY-MM-DD'."})
    try:
        with _connect() as conn:
            with conn.cursor() as cur:
                for fn in ("refresh_stage_contribution_day",
                           "refresh_contribution_day",
                           "refresh_shift_day"):
                    cur.execute(f"SELECT {fn}(%s::date)", (et_day,))
                conn.commit()
        return json.dumps({"status": "refreshed", "et_day": et_day})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": type(e).__name__, "detail": str(e)})


# --------------------------------------------------------------------------- #
# HTTP app: auth middleware + health route
# --------------------------------------------------------------------------- #

def _authorized(request) -> bool:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == MCP_TOKEN:
        return True
    if request.headers.get("x-api-key") == MCP_TOKEN:
        return True
    if request.query_params.get("token") == MCP_TOKEN:
        return True
    return False


class TokenAuthMiddleware:
    """ASGI middleware. Guards /mcp; leaves the health route open."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/" or path == "/health":
            await self.app(scope, receive, send)
            return
        # Build a lightweight request view for header/query access
        from starlette.requests import Request
        request = Request(scope, receive=receive)
        if not _authorized(request):
            resp = JSONResponse({"error": "unauthorized"}, status_code=401)
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _health(request):
    return PlainTextResponse("ikigai-contribution-db MCP: ok")


# FastMCP builds a Starlette app for the streamable-http transport (mounts /mcp).
app = mcp.streamable_http_app()
app.router.routes.append(Route("/", _health, methods=["GET"]))
app.router.routes.append(Route("/health", _health, methods=["GET"]))
app.add_middleware(TokenAuthMiddleware)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
