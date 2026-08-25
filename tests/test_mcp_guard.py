"""Guard tests for the contribution MCP.

This server reaches a database holding `employee`, `time_clock`, `time_break` and
`person_alias` — people's hours — plus the append-only `event` table every rollup derives
from. Until now a caller passing allow_write=true could UPDATE or DROP any of it, and reads
ran in a normal read-write transaction.

These lock in the three things that changed, and one thing that deliberately did NOT:
employee data stays readable, because the attendance and performance workflows exist to read
it. Personal data here is protected by who holds the bearer token, not by hiding tables from
the tool whose job is to query them.

Run: python -m tests.test_mcp_guard
"""
import json
import os
import sys

# Deliberately carries NO user:password — the CI secret scan flags credential-SHAPED strings
# and a fake one would train everybody to ignore it. An unresolvable host is all these tests
# need: nothing here is supposed to reach a database.
os.environ["DATABASE_URL"] = "postgresql://localhost.localdomain:5432/nonexistent"
# The server fails CLOSED without a token — refusing to start an unauthenticated database
# endpoint, which is the right instinct. Give it a dummy so the guards can be exercised.
os.environ.setdefault("MCP_TOKEN", "test-token-not-a-real-one")

import mcp_server as M  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "  FAIL  ") + name + (f" — {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def _call(sql, **kw):
    try:
        return json.loads(M.run_sql(sql, **kw))
    except Exception as e:                      # reached the DB = passed the guards
        return {"_reached_db": True, "_err": str(e)}


print("\nwrites need BOTH the caller's flag and the server's unlock")
os.environ.pop("MCP_ALLOW_WRITE", None)
r = _call("UPDATE employee SET name='x'")
check("a write without allow_write is blocked", r.get("error") == "write_blocked", str(r)[:80])

for stmt in ("UPDATE time_clock SET clock_out=now()",
             "DELETE FROM event",
             "DROP TABLE employee",
             "TRUNCATE time_break"):
    r = _call(stmt, allow_write=True)
    check(f"locked server refuses: {stmt.split()[0]}",
          r.get("server_write_locked") is True, str(r)[:80])

os.environ["MCP_ALLOW_WRITE"] = "1"
r = _call("UPDATE employee SET name='x'", allow_write=True)
check("MCP_ALLOW_WRITE=1 lets a deliberate write through the gate",
      r.get("_reached_db") or not r.get("server_write_locked"), str(r)[:80])
os.environ.pop("MCP_ALLOW_WRITE")

print("\nsecrets stay unreadable; people data does not")
for sql in ("SELECT * FROM oauth_token",
            "SELECT * FROM api_token",
            "SELECT * FROM some_secret_store",
            "SELECT * FROM user_password"):
    check(f"refused: {sql.split('FROM ')[1]}",
          _call(sql).get("error") == "sensitive_relation", sql)

for sql in ("SELECT * FROM employee",
            "SELECT * FROM time_clock",
            "SELECT * FROM person_alias",
            "SELECT * FROM contribution_daily",
            "SELECT count(*) FROM event"):
    r = _call(sql)
    check(f"still readable: {sql.split('FROM ')[1]}",
          r.get("error") != "sensitive_relation", str(r)[:60])

print("\nreads run read-only at the database, not just by regex")
src = open(os.path.join(os.path.dirname(__file__), "..", "mcp_server.py")).read()
check("the read path sets conn.read_only", "conn.read_only = True" in src)
check("and says why the regex alone is not enough", "heuristics can be fooled" in src)

print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
