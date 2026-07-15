"""
ShipHero -> Postgres `event` ingest (GraphQL). Pulls picks, packs, and inventory
work (receiving, restocking, tote->bin moves, cycle counts) for a date range and
writes them to the contribution store. Idempotent via (source, ext_id) / dedup_key.

Backfill:   DATABASE_URL=... SHIPHERO_REFRESH_TOKEN=... python shiphero_ingest.py 2026-07-01 2026-07-15
Daily cron: python shiphero_ingest.py            (defaults to yesterday+today ET)

REPLENISHMENT METHOD (identical to the warehouse dashboard artifact's invKind /
loadFloor): every non-pick inventory_change is classified by its REASON into a
kind (receive / return / move / restock / count / adjust); we credit the person
and count UNITS ON THE POSITIVE LEG ONLY -- a move writes two rows at the same
second (-N leaving reserve, +N into the pick bin), so a 40-unit replenish must
read as 40, not 80. There is NO size threshold: big physical restocks are real
labor and are counted, not discarded (the old abs(delta)>=10 -> 'bulk_csv' rule
was wrong and buried the top replenishers). Cycle counts are inventory work too
and are included, matching the artifact's replUnits. Rows whose positive leg is 0
(pure shrink/negative adjustments, and the negative leg of every move) are skipped
-- that is also what dedups the two legs.

Robustness: iterates DAY BY DAY (small queries), commits picks+packs before the
slower inventory pass, retries on ShipHero throttling. Auth: refresh_token -> access_token.
"""
import os, sys, re, json, time, urllib.request, urllib.error, datetime as dt
from db import connect

AUTH = "https://public-api.shiphero.com/auth/refresh"
GQL  = "https://public-api.shiphero.com/graphql"
REFRESH = os.environ.get("SHIPHERO_REFRESH_TOKEN", "")
PAGE_SLEEP = float(os.environ.get("SHIPHERO_PAGE_SLEEP", "1.0"))  # gentle on credits

def _post(url, body, headers, tries=6):
    data = json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < tries - 1:
                wait = 15 * (attempt + 1)
                print(f"[shiphero] HTTP {e.code}, retry in {wait}s", flush=True)
                time.sleep(wait); continue
            raise
        except urllib.error.URLError as e:
            if attempt < tries - 1:
                time.sleep(10); continue
            raise

def access_token():
    if not REFRESH:
        raise SystemExit("SHIPHERO_REFRESH_TOKEN not set")
    return _post(AUTH, {"refresh_token": REFRESH}, {})["access_token"]

def gql(query, token, variables=None, tries=8):
    """Run a query; retry on ShipHero throttling; raise on real GraphQL errors."""
    for attempt in range(tries):
        resp = _post(GQL, {"query": query, "variables": variables or {}},
                     {"Authorization": "Bearer " + token})
        if resp.get("data") is not None:
            return resp
        errs = resp.get("errors") or []
        msg = json.dumps(errs)[:300]
        if any(k in msg.lower() for k in ("throttle", "credit", "exceeded", "rate")) and attempt < tries - 1:
            wait = 20 * (attempt + 1)
            print(f"[shiphero] throttled, wait {wait}s", flush=True)
            time.sleep(wait); continue
        raise RuntimeError(f"ShipHero GraphQL error: {msg}")
    raise RuntimeError("ShipHero: throttled past retry budget")

# ---- GraphQL queries (connection-style with cursor pagination) --------------
# picks/packs carry BOTH the display name and the user_id, so we build a live
# user_id -> name map from them and use it to attribute inventory rows (which
# only carry user_id). This is exactly what the dashboard artifact does.
Q_PICKS = """query($df:ISODateTime!,$dt:ISODateTime!,$after:String){
  picks_per_day(date_from:$df,date_to:$dt){ data(first:100,after:$after){
    edges{ node{ id order_number user_id user_first_name user_last_name sku picked_quantity created_at } }
    pageInfo{ hasNextPage endCursor } } } }"""
Q_PACKS = """query($df:ISODateTime!,$dt:ISODateTime!,$after:String){
  packs_per_day(date_from:$df,date_to:$dt){ data(first:100,after:$after){
    edges{ node{ id order_number user_id user_first_name user_last_name total_items created_at } }
    pageInfo{ hasNextPage endCursor } } } }"""
Q_INV = """query($df:ISODateTime!,$dt:ISODateTime!,$after:String){
  inventory_changes(date_from:$df,date_to:$dt){ data(first:100,after:$after){
    edges{ node{ id user_id sku reason change_in_on_hand cycle_counted created_at } }
    pageInfo{ hasNextPage endCursor } } } }"""

def _pages(query, token, vars_, root):
    after = None
    while True:
        v = dict(vars_); v["after"] = after
        conn = gql(query, token, v)["data"][root]["data"]
        for e in conn["edges"]:
            yield e["node"]
        if conn["pageInfo"]["hasNextPage"]:
            after = conn["pageInfo"]["endCursor"]
            time.sleep(PAGE_SLEEP)
        else:
            break

INS = ("INSERT INTO event (ts,person,stage,station,action,order_number,sku,quantity,"
       "subtype,source,ext_id,dedup_key,raw) VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,'shiphero',%s,%s,%s) "
       "ON CONFLICT (dedup_key) DO NOTHING")

# Seed map (base64 ShipHero global id -> name). The live id2name from picks/packs
# extends this every run; anything still unknown is re-credited by fix_historical.
USER_MAP = {
    "VXNlcjo3NzY5OTM=": "Halil Gurler", "VXNlcjo3MjU3Mjg=": "Manu Bekele",
    "VXNlcjo3NzcwMDA=": "Nic Cox", "VXNlcjo3NzcwMDE=": "Maurice Williams",
    "VXNlcjo3NzY5OTY=": "Jeffrey Kwan", "VXNlcjo3Nzk0NDE=": "Daniella Gross",
    "VXNlcjo3NzgyMjE=": "Kadil Ladson", "VXNlcjo3ODQ0MjU=": "Esra Altug",
}

def _name(n): return f"{n.get('user_first_name','') or ''} {n.get('user_last_name','') or ''}".strip()

# --- Replenishment classification: replicate the artifact's invKind + reason ---
# families exactly. The artifact only pulls inventory rows whose reason matches
# one of these substrings, which is what keeps initial-stock / bulk CSV imports
# (not floor labor) out of the count. We pull all inventory_changes and apply the
# same gate here, so the result is identical to the dashboard.
INV_REASONS = ("transfer", "receiv", "restock", "replenish", "putaway",
               "cycle count", "adjust", "return")

def inv_kind(reason, delta, cycle):
    """Return the inventory-work kind, or None to DROP. Picks/shipments and any
    reason outside the tracked families (e.g. CSV initial-stock loads) are dropped."""
    r = (reason or "").lower()
    if "picked into" in r or "pick type:" in r or "shipped" in r: return None   # fulfillment
    if not any(k in r for k in INV_REASONS):                   return None   # not tracked inv labor
    if cycle or "cycle count" in r:                            return "count"
    if re.search(r"receiv|purchase order|\bpo\b|vendor", r):   return "receive"
    if re.search(r"return|rma|exchange", r):                   return "return"
    if re.search(r"moved|move to|transfer|relocat", r):        return "move"
    if re.search(r"replenish|restock|put ?away|added|created", r): return "restock"
    return "restock" if (delta or 0) > 0 else "adjust"          # catch-all within a matched family

def _iso(d, end=False):
    return f"{d}T23:59:59" if end else f"{d}T00:00:00"

def _refresh_day(cur, d):
    cur.execute("SELECT refresh_stage_contribution_day(%s)", (d,))
    cur.execute("SELECT refresh_contribution_day(%s)", (d,))
    cur.execute("SELECT refresh_shift_day(%s)", (d,))

def fix_historical(cur, umap):
    """Re-credit any 'User-<id>' rows now that we have names (from picks/packs)."""
    cur.execute("SELECT DISTINCT person FROM event WHERE person LIKE 'User-%'")
    fixed = 0
    for (p,) in cur.fetchall():
        nm = umap.get(p[5:])            # strip 'User-' -> base64 id
        if nm and nm != p:
            cur.execute("UPDATE event SET person=%s WHERE person=%s", (nm, p))
            fixed += cur.rowcount
    if fixed:
        print(f"[shiphero] re-credited {fixed} historical inventory rows", flush=True)
    return fixed

def run(df, dt_):
    tok = access_token()
    d0 = dt.date.fromisoformat(df); d1 = dt.date.fromisoformat(dt_)
    id2name = dict(USER_MAP)           # extended live from picks/packs below
    inv_all = []                       # (ts, user_id, kind, sku, qty, reason, eid)
    inv_days = set()
    day = d0
    while day < d1:
        nxt = day + dt.timedelta(days=1)
        a, b = _iso(str(day)), _iso(str(day), end=True)
        rows = []
        for n in _pages(Q_PICKS, tok, {"df": a, "dt": b}, "picks_per_day"):
            eid = str(n["id"]); nm = _name(n)
            if n.get("user_id") and nm: id2name[n["user_id"]] = nm
            rows.append((n["created_at"], nm, "pick", "pick", n.get("order_number"),
                         n.get("sku"), n.get("picked_quantity") or 0, None, eid, "shiphero|"+eid, None))
        for n in _pages(Q_PACKS, tok, {"df": a, "dt": b}, "packs_per_day"):
            eid = str(n["id"]); nm = _name(n)
            if n.get("user_id") and nm: id2name[n["user_id"]] = nm
            rows.append((n["created_at"], nm, "pack", "pack", n.get("order_number"),
                         None, n.get("total_items") or 0, None, eid, "shiphero|"+eid, None))
        # pick/pack commit up front (fast, and independent of inventory)
        with connect() as c, c.cursor() as cur:
            cur.executemany(INS, rows)
            _refresh_day(cur, day)
            c.commit()
        # collect inventory work for this day; resolve names + insert after the loop
        n_inv = 0
        try:
            for n in _pages(Q_INV, tok, {"df": a, "dt": b}, "inventory_changes"):
                kind = inv_kind(n.get("reason"), n.get("change_in_on_hand"), n.get("cycle_counted"))
                if not kind: continue
                chg = n.get("change_in_on_hand") or 0
                qty = chg if chg > 0 else 0          # POSITIVE LEG ONLY (dedups the two legs)
                if qty <= 0: continue
                inv_all.append((n["created_at"], n.get("user_id"), kind, n.get("sku"),
                                qty, n.get("reason"), str(n["id"])))
                inv_days.add(day); n_inv += 1
        except Exception as e:
            print(f"[shiphero] {day} inventory skipped: {e}", flush=True)
        print(f"[shiphero] {day}: {len(rows)} pick/pack + {n_inv} inv(+leg) rows", flush=True)
        day = nxt
    # ---- resolve inventory names with the COMPLETE id2name, then insert --------
    inv_rows = []
    unknown = set()
    for (ts, uid, kind, sku, qty, reason, eid) in inv_all:
        person = id2name.get(uid) or (f"User-{uid}" if uid else "Unknown")
        if uid and uid not in id2name: unknown.add(uid)
        raw = json.dumps({"reason": reason, "kind": kind})
        inv_rows.append((ts, person, "replenish", kind, None, sku, qty, kind, eid, "shiphero|"+eid, raw))
    with connect() as c, c.cursor() as cur:
        if inv_rows:
            cur.executemany(INS, inv_rows)
        fix_historical(cur, id2name)
        for d in sorted(inv_days):
            _refresh_day(cur, d)
        c.commit()
    print(f"[shiphero] inventory: {len(inv_rows)} rows over {len(inv_days)} days; "
          f"user directory {len(id2name)} names"
          + (f"; UNRESOLVED user_ids: {sorted(unknown)}" if unknown else ""), flush=True)

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        run(sys.argv[1], sys.argv[2])
    else:
        today = (dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))).date()
        run(str(today - dt.timedelta(days=1)), str(today + dt.timedelta(days=1)))
