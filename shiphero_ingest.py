"""
ShipHero -> Postgres `event` ingest (GraphQL). Pulls picks, packs, and inventory
changes (cycle counts + physical replenishment) for a date range and writes them
to the contribution store. Idempotent via (source, ext_id) / dedup_key.

Backfill:   DATABASE_URL=... SHIPHERO_REFRESH_TOKEN=... python shiphero_ingest.py 2026-07-01 2026-07-14
Daily cron: python shiphero_ingest.py            (defaults to yesterday+today ET)

Auth: refresh_token -> short-lived access_token (POST /auth/refresh), then GraphQL.
Field names below match the live data shapes already verified via the ShipHero MCP.
"""
import os, sys, json, urllib.request, datetime as dt
from db import connect

AUTH = "https://public-api.shiphero.com/auth/refresh"
GQL  = "https://public-api.shiphero.com/graphql"
REFRESH = os.environ.get("SHIPHERO_REFRESH_TOKEN", "")
BULK_MIN = int(os.environ.get("REPLENISH_BULK_MIN", "10"))

def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def access_token():
    if not REFRESH:
        raise SystemExit("SHIPHERO_REFRESH_TOKEN not set")
    return _post(AUTH, {"refresh_token": REFRESH}, {})["access_token"]

def gql(query, token, variables=None):
    return _post(GQL, {"query": query, "variables": variables or {}},
                 {"Authorization": "Bearer " + token})

# ---- GraphQL queries (connection-style with cursor pagination) --------------
Q_PICKS = """query($df:ISODateTime!,$dt:ISODateTime!,$after:String){
  picks_per_day(date_from:$df,date_to:$dt){ data(first:100,after:$after){
    edges{ node{ id order_number user_first_name user_last_name sku picked_quantity created_at } }
    pageInfo{ hasNextPage endCursor } } } }"""
Q_PACKS = """query($df:ISODateTime!,$dt:ISODateTime!,$after:String){
  packs_per_day(date_from:$df,date_to:$dt){ data(first:100,after:$after){
    edges{ node{ id order_number user_first_name user_last_name total_items created_at } }
    pageInfo{ hasNextPage endCursor } } } }"""
Q_INV = """query($df:ISODateTime!,$after:String){
  inventory_changes(date_from:$df){ data(first:100,after:$after){
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
        else:
            break

INS = ("INSERT INTO event (ts,person,stage,station,action,order_number,sku,quantity,"
       "subtype,source,ext_id,dedup_key) VALUES (%s,%s,%s,NULL,%s,%s,%s,%s,%s,'shiphero',%s,%s) "
       "ON CONFLICT (dedup_key) DO NOTHING")

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

def run(df, dt_):
    tok = access_token()
    rows = []
    for n in _pages(Q_PICKS, tok, {"df": df, "dt": dt_}, "picks_per_day"):
        eid = str(n["id"])
        rows.append((n["created_at"], _name(n), "pick", "pick", n.get("order_number"),
                     n.get("sku"), n.get("picked_quantity") or 0, None, eid, "shiphero|"+eid))
    for n in _pages(Q_PACKS, tok, {"df": df, "dt": dt_}, "packs_per_day"):
        eid = str(n["id"])
        rows.append((n["created_at"], _name(n), "pack", "pack", n.get("order_number"),
                     None, n.get("total_items") or 0, None, eid, "shiphero|"+eid))
    for n in _pages(Q_INV, tok, {"df": df}, "inventory_changes"):
        stage, sub = classify(n.get("reason"), n.get("change_in_on_hand"), n.get("cycle_counted"))
        if not stage: continue
        eid = str(n["id"]); person = f"User-{n.get('user_id')}"   # map user_id->name in a follow-up
        rows.append((n["created_at"], person, stage, stage, None, n.get("sku"),
                     abs(n.get("change_in_on_hand") or 0), sub, eid, "shiphero|"+eid))

    with connect() as c, c.cursor() as cur:
        cur.executemany(INS, rows); c.commit()
        # refresh rollups for each ET day in range
        d0 = dt.date.fromisoformat(df); d1 = dt.date.fromisoformat(dt_)
        d = d0
        while d <= d1:
            cur.execute("SELECT refresh_stage_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_shift_day(%s)", (d,))
            d += dt.timedelta(days=1)
        c.commit()
    print(f"[shiphero] ingested ~{len(rows)} rows for {df}..{dt_}", flush=True)

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        run(sys.argv[1], sys.argv[2])
    else:
        today = (dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))).date()
        run(str(today - dt.timedelta(days=1)), str(today))
