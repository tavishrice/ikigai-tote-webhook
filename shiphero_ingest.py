"""
ShipHero -> Postgres `event` ingest (GraphQL). Pulls picks, packs, and inventory
changes (cycle counts + physical replenishment) for a date range and writes them
to the contribution store. Idempotent via (source, ext_id) / dedup_key.

Backfill:   DATABASE_URL=... SHIPHERO_REFRESH_TOKEN=... python shiphero_ingest.py 2026-07-01 2026-07-15
Daily cron: python shiphero_ingest.py            (defaults to yesterday+today ET)

Robustness: iterates DAY BY DAY (small queries), commits picks+packs before the
slower inventory pass, retries on ShipHero throttling, and surfaces real GraphQL
errors instead of a bare NoneType crash. Auth: refresh_token -> access_token.
"""
import os, sys, json, time, urllib.request, urllib.error, datetime as dt
from db import connect

AUTH = "https://public-api.shiphero.com/auth/refresh"
GQL  = "https://public-api.shiphero.com/graphql"
REFRESH = os.environ.get("SHIPHERO_REFRESH_TOKEN", "")
BULK_MIN = int(os.environ.get("REPLENISH_BULK_MIN", "10"))
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
Q_PICKS = """query($df:ISODateTime!,$dt:ISODateTime!,$after:String){
  picks_per_day(date_from:$df,date_to:$dt){ data(first:100,after:$after){
    edges{ node{ id order_number user_first_name user_last_name sku picked_quantity created_at } }
    pageInfo{ hasNextPage endCursor } } } }"""
Q_PACKS = """query($df:ISODateTime!,$dt:ISODateTime!,$after:String){
  packs_per_day(date_from:$df,date_to:$dt){ data(first:100,after:$after){
    edges{ node{ id order_number user_first_name user_last_name total_items created_at } }
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
       "subtype,source,ext_id,dedup_key) VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,'shiphero',%s,%s) "
       "ON CONFLICT (dedup_key) DO NOTHING")

# user_id (base64 ShipHero global id) -> name, so inventory rows attribute correctly.
USER_MAP = {
    "VXNlcjo3NzY5OTM=": "Halil Gurler", "VXNlcjo3MjU3Mjg=": "Manu Bekele",
    "VXNlcjo3NzcwMDA=": "Nic Cox", "VXNlcjo3NzcwMDE=": "Maurice Williams",
    "VXNlcjo3NzY5OTY=": "Jeffrey Kwan", "VXNlcjo3Nzk0NDE=": "Daniella Gross",
    "VXNlcjo3NzgyMjE=": "Kadil Ladson", "VXNlcjo3ODQ0MjU=": "Esra Altug",
}

# Resolve EVERY ShipHero user_id -> name from the account directory, so inventory
# rows (which only carry user_id) are credited to the right person automatically.
Q_USERS = """query{ account{ data{ users{ id account_id first_name last_name } } } }"""

def resolve_users(token):
    m = dict(USER_MAP)
    try:
        users = gql(Q_USERS, token)["data"]["account"]["data"]["users"] or []
        for u in users:
            nm = f"{u.get('first_name','') or ''} {u.get('last_name','') or ''}".strip()
            if u.get("id") and nm:
                m[u["id"]] = nm
        print(f"[shiphero] user directory: {len(m)} names", flush=True)
        for uid in ("VXNlcjo1ODEwMDM=","VXNlcjo3Nzg0NjM=","VXNlcjo3NzY5OTc=","VXNlcjozODc2OTQ="):
            print(f"[shiphero]   {uid} -> {m.get(uid,'?')}", flush=True)
    except Exception as e:
        print(f"[shiphero] user resolve failed ({e}); using static map", flush=True)
    return m

def _name(n): return f"{n.get('user_first_name','') or ''} {n.get('user_last_name','') or ''}".strip()

def classify(reason, delta, cycle):
    r = (reason or "").lower()
    if cycle: return ("count", "cycle")
    if "picked" in r or "shipped" in r: return (None, None)          # fulfillment
    if "csv" in r or "adjust" in r or "bulk" in r or abs(delta or 0) >= BULK_MIN:
        return ("replenish", "bulk_csv")                              # excluded from labor
    if any(k in r for k in ("transfer", "tote clear", "put", "restock", "receiv")):
        return ("replenish", "physical")
    return (None, None)

def _iso(d, end=False):
    return f"{d}T23:59:59" if end else f"{d}T00:00:00"

def _refresh_day(cur, d):
    cur.execute("SELECT refresh_stage_contribution_day(%s)", (d,))
    cur.execute("SELECT refresh_contribution_day(%s)", (d,))
    cur.execute("SELECT refresh_shift_day(%s)", (d,))

def fix_historical(cur, umap):
    """Re-credit any previously-loaded 'User-<id>' rows now that we have names."""
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
    umap = resolve_users(tok)
    d0 = dt.date.fromisoformat(df); d1 = dt.date.fromisoformat(dt_)
    # one-time repair of earlier mis-attributed rows + refresh their rollups
    with connect() as c, c.cursor() as cur:
        if fix_historical(cur, umap):
            cur.execute("SELECT DISTINCT et_day(ts) FROM event WHERE source='shiphero'")
            for (d,) in cur.fetchall():
                cur.execute("SELECT refresh_stage_contribution_day(%s)", (d,))
                cur.execute("SELECT refresh_shift_day(%s)", (d,))
        c.commit()
    day = d0
    while day < d1:
        nxt = day + dt.timedelta(days=1)
        a, b = _iso(str(day)), _iso(str(day), end=True)
        rows = []
        for n in _pages(Q_PICKS, tok, {"df": a, "dt": b}, "picks_per_day"):
            eid = str(n["id"])
            rows.append((n["created_at"], _name(n), "pick", "pick", n.get("order_number"),
                         n.get("sku"), n.get("picked_quantity") or 0, None, eid, "shiphero|"+eid))
        for n in _pages(Q_PACKS, tok, {"df": a, "dt": b}, "packs_per_day"):
            eid = str(n["id"])
            rows.append((n["created_at"], _name(n), "pack", "pack", n.get("order_number"),
                         None, n.get("total_items") or 0, None, eid, "shiphero|"+eid))
        inv = []
        try:
            for n in _pages(Q_INV, tok, {"df": a, "dt": b}, "inventory_changes"):
                stage, sub = classify(n.get("reason"), n.get("change_in_on_hand"), n.get("cycle_counted"))
                if not stage: continue
                eid = str(n["id"])
                uid = n.get("user_id")
                person = umap.get(uid, f"User-{uid}")
                inv.append((n["created_at"], person, stage, stage, None, n.get("sku"),
                            abs(n.get("change_in_on_hand") or 0), sub, eid, "shiphero|"+eid))
        except Exception as e:
            print(f"[shiphero] {day} inventory skipped: {e}", flush=True)
        with connect() as c, c.cursor() as cur:
            cur.executemany(INS, rows + inv)
            _refresh_day(cur, day)
            c.commit()
        print(f"[shiphero] {day}: {len(rows)} pick/pack + {len(inv)} inv rows", flush=True)
        day = nxt

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        run(sys.argv[1], sys.argv[2])
    else:
        today = (dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))).date()
        run(str(today - dt.timedelta(days=1)), str(today + dt.timedelta(days=1)))
