# CLAUDE.md — Ikigai warehouse dashboard / contribution DB

Read this before touching anything.

## What this is

The warehouse-side data layer for Ikigai Cases: ShipHero and Shopify ingests, identity and
fulfilment resolution, per-shift contribution rollups, a dashboard, and an MCP server
(`ikigai-contribution-db`) that Claude reaches all of it through.

It is the **source of record for labour and contribution per shift** — who worked, when, and
what shipped while they did. See `docs/ikigai_app_contract.md` in `ikigai-supply-chain` for
which app owns which concept across the network.

## This repository is PUBLIC

The other Ikigai repos are private; this one is not. Nothing sensitive is committed today —
no credentials in source, and `render.yaml` marks secrets `sync: false` — and **it must stay
that way**. Before every commit: no connection strings, no tokens, no employee names in
fixtures or comments, no customer data in test data. The database this code reaches holds
people's hours; the code that reaches it is world-readable.

## Non-negotiables

1. **`event` is append-only.** Every rollup derives from it. Never UPDATE or DELETE a row —
   correct by appending a compensating event. After any manual write, call `refresh_day()`
   for **every** ET-day you touched, or the rollups quietly disagree with their own source.

2. **The MCP is read-only unless deliberately unlocked.** `run_sql(allow_write=True)` also
   requires `MCP_ALLOW_WRITE=1` in the service environment. Reads additionally run in a
   Postgres read-only transaction, because the read/write classifier is a regex and regexes
   can be fooled — let the database decide.

3. **Employee data is readable through the MCP on purpose.** `employee`, `time_clock`,
   `time_break` and `person_alias` are the point of this database, and the attendance and
   performance workflows read them. They are protected by **who holds the bearer token**, not
   by hiding tables from the tool whose job is to query them. What IS blocked is credential
   material — tokens, secrets, passwords. Do not "harden" this by denying people data; that
   breaks real workflows and protects nothing the token doesn't already.

4. **The MCP fails closed.** No `MCP_TOKEN`/`MCP_TOKENS`, no server. Keep it that way — an
   unauthenticated database endpoint is worse than a broken one.

5. **Times are Eastern.** `et_day` is the grain everything rolls up to. A UTC day boundary
   here silently moves a shift into the wrong day.

## Layout

```
app.py               the Flask app + dashboard
db.py                connection helper
mcp_server.py        the MCP (ikigai-contribution-db) — bearer-gated, read-only by default
*_ingest.py          ShipHero / Shopify pulls
*_resolve.py         identity, fulfilment, engraving resolution
rollup_cron.py       the derived tables
tests/               what CI runs
```

## The MCP

`db_overview()` / `list_schema()` / `event_snapshot()` orient you; `refresh_day(et_day)`
rebuilds a day's rollups; `run_sql()` is the escape hatch.

**This server has no curated tools yet, and it should.** Per the network contract, any query
run more than once earns a named tool with a docstring — the docstring is the manual for
whoever reads nothing else. Good candidates: hours by person over a window, contribution per
shift, unresolved identities, orders shipped by day. Until they exist everyone hand-writes
SQL against a schema they have to re-learn each time.

**Pin the SDK.** A floating `mcp>=` let mcp 2.0.0 remove `mcp.server.fastmcp` in a sibling
repo and the server died at import before answering a call. All Ikigai MCP servers pin the
same version.

## Working alongside other agents

* One branch per agent; draft PRs by default.
* CI must be green, and never green by deletion. If a test is wrong, fix it *and say why in
  the commit*.
* Code rolls back; schema does not.
* Network-wide conventions: `docs/ikigai_app_contract.md` in `ikigai-supply-chain`.

## Style

Plain Python, stdlib where it's enough. psycopg3. No ORM. Comments explain *why*, especially
where a subtle bug was fixed; the code already says what.
