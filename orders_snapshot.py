"""
ShipHero -> Postgres `open_order` snapshot: the OUTSTANDING (not-yet-shipped) order
backlog, so the dashboard can show "what we owe" — how many orders, their dollar value,
how aged they are, and their status. Full-refresh each run (a fresh snapshot mirroring
live ShipHero state), so it never shows a ghost order that already shipped.

Run once:   DATABASE_URL=... SHIPHERO_REFRESH_TOKEN=... python orders_snapshot.py
Cron:       python orders_snapshot.py   (append to the ikigai-shiphero-ingest command)

Auth + GraphQL client mirror shiphero_ingest.py (refresh_token -> access_token, retry on throttle).
"""
import os, json, time, urllib.request, urllib.error
from db import connect

# rev: credit-safe query (no line_items, first:50) — 2026-07-16

AUTH = "https://public-api.shiphero.com/auth/refresh"
GQL  = "https://public-api.shiphero.com/graphql"
REFRESH = os.environ.get("SHIPHERO_REFRESH_TOKEN", "")
PAGE_SLEEP = float(os.environ.get("SHIPHERO_PAGE_SLEEP", "1.0"))

def _post(url, body, headers, tries=6):
    data = json.dumps(body).encode()
    for a in range(tries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and a < tries - 1:
                time.sleep(15 * (a + 1)); continue
            raise
        except urllib.error.URLError:
            if a < tries - 1:
                time.sleep(10); continue
            raise

def access_token():
    if not REFRESH:
        raise SystemExit("SHIPHERO_REFRESH_TOKEN not set")
    return _post(AUTH, {"refresh_token": REFRESH}, {})["access_token"]

def gql(query, token, variables=None, tries=8):
    for a in range(tries):
        resp = _post(GQL, {"query": query, "variables": variables or {}},
                     {"Authorization": "Bearer " + token})
        if resp.get("data") is not None:
            return resp
        msg = json.dumps(resp.get("errors") or [])[:400]
        if any(k in msg.lower() for k in ("throttle", "credit", "exceeded", "rate")) and a < tries - 1:
            time.sleep(20 * (a + 1)); continue
        raise RuntimeError("ShipHero GraphQL error: " + msg)
    raise RuntimeError("ShipHero throttled past retry budget")

DDL = """CREATE TABLE IF NOT EXISTS open_order(
  order_number       text PRIMARY KEY,
  order_date         timestamptz,
  fulfillment_status text,
  total_price        numeric,
  shop_name          text,
  required_ship_date timestamptz,
  on_hold            boolean DEFAULT false,
  hold_reason        text,
  items_open         integer,
  snapshot_at        timestamptz DEFAULT now())"""

# ShipHero `orders` connection, cursor-paginated. fulfillment_status='pending' = the
# open backlog (not fulfilled, not canceled). ShipHero charges query "credits" by
# complexity and caps each operation at ~4004 credits; a nested line_items connection
# blows past that (needs 10101), so we deliberately DON'T pull line items and keep the
# page at 50. Order-level fields + holds (a cheap flat object) stay well under budget.
Q = """query($fs:String,$after:String){
  orders(fulfillment_status:$fs){
    data(first:50, after:$after){
      edges{ node{
        order_number order_date fulfillment_status total_price shop_name required_ship_date
        holds{ address_hold operator_hold fraud_hold payment_hold client_hold }
      } }
      pageInfo{ hasNextPage endCursor } } } }"""

# Fallback query if `holds` sub-selection isn't valid on this account (order-level only).
Q_MIN = """query($fs:String,$after:String){
  orders(fulfillment_status:$fs){
    data(first:50, after:$after){
      edges{ node{ order_number order_date fulfillment_status total_price shop_name required_ship_date } }
      pageInfo{ hasNextPage endCursor } } } }"""

def _pages(token, fs, query):
    after = None
    while True:
        conn = gql(query, token, {"fs": fs, "after": after})["data"]["orders"]["data"]
        for e in conn["edges"]:
            yield e["node"]
        if conn["pageInfo"]["hasNextPage"]:
            after = conn["pageInfo"]["endCursor"]; time.sleep(PAGE_SLEEP)
        else:
            break

def _probe_ok(token, query):
    """A query is usable only if ShipHero returns a non-null `orders` (it returns
    data={orders:null} alongside an errors[] for bad sub-fields or credit overruns)."""
    try:
        r = gql(query, token, {"fs": "pending", "after": None})
        return (r.get("data") or {}).get("orders") is not None
    except RuntimeError as e:
        print("[orders_snapshot] probe error:", str(e)[:160], flush=True)
        return False

def _rows(token):
    """Try the rich query; fall back to the minimal one if the schema rejects sub-fields."""
    if _probe_ok(token, Q):
        query = Q
    elif _probe_ok(token, Q_MIN):
        print("[orders_snapshot] rich query rejected, using minimal", flush=True)
        query = Q_MIN
    else:
        raise RuntimeError("ShipHero orders query returned null for both Q and Q_MIN")
    out = []
    for n in _pages(token, "pending", query):
        h = n.get("holds") or {}
        held = {k: bool(h.get(k)) for k in ("address_hold","operator_hold","fraud_hold","payment_hold","client_hold")}
        on_hold = any(held.values())
        hold_reason = ", ".join(k.replace("_hold","") for k,v in held.items() if v) or None
        items_open = None
        li = (n.get("line_items") or {}).get("edges")
        if li is not None:
            items_open = 0
            for e in li:
                nd = e.get("node") or {}
                q = nd.get("quantity_pending_fulfillment")
                items_open += int(q if q is not None else (nd.get("quantity") or 0))
        out.append((n.get("order_number"), n.get("order_date"), n.get("fulfillment_status"),
                    n.get("total_price"), n.get("shop_name"), n.get("required_ship_date"),
                    on_hold, hold_reason, items_open))
    return out

def run():
    token = access_token()
    rows = _rows(token)
    with connect() as c, c.cursor() as cur:
        cur.execute(DDL)
        cur.execute("TRUNCATE open_order")
        if rows:
            cur.executemany(
                "INSERT INTO open_order(order_number,order_date,fulfillment_status,total_price,"
                "shop_name,required_ship_date,on_hold,hold_reason,items_open,snapshot_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT (order_number) DO NOTHING", rows)
        c.commit()
    print(f"[orders_snapshot] {len(rows)} outstanding orders snapshotted", flush=True)

if __name__ == "__main__":
    # Never let a snapshot failure break the ingest cron: log and exit 0.
    # A failed run leaves the previous snapshot intact (run() truncates only after
    # a successful fetch), and the dashboard surfaces staleness from snapshot_at.
    try:
        run()
    except Exception as e:
        print(f"[orders_snapshot] ERROR (non-fatal, keeping last snapshot): {e}", flush=True)
