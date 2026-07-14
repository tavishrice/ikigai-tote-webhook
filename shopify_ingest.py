"""
Shopify -> Postgres `event` ingest, using the ARTIFACT method (order timeline),
so hand-fulfillments are attributed to the real staffer and deduped vs ShipHero.

Why the timeline (not the fulfillments API): every fulfilled order shows a
"Manual" fulfillment, so the fulfillments field cannot tell ShipHero-pushed from
hand-done, and it hides that ONE order is often split -- a staffer hand-fulfills
some items in Shopify AND ShipHero fulfills the rest. The order timeline exposes
this: each `fulfillment_success` event's message says WHO did it.

Rule (identical to reference/dashboard.html fetchDirect):
  - skip events whose message starts with "ShipHero"   (already counted via ShipHero packs)
  - "<Name> marked N items as fulfilled" + attributeToUser  -> credit <Name>, N items (hand pack)
  - "Shopify marked N items as fulfilled" (no user) -> credit the nearest
        "shipping_label_created_success" purchaser ("<Name> purchased a ...")
  - anything we cannot attribute to a person is DROPPED (finished inside Shopify,
        no human -> not warehouse labor)

Each qualifying event becomes a stage='pack', source='shopify' row keyed by
order+createdAt (idempotent). The read API then dedups orders across sources.

Backfill:  SHOPIFY_ADMIN_TOKEN=... DATABASE_URL=... python shopify_ingest.py 2026-07-01 2026-07-15
Live cron: python shopify_ingest.py                 (defaults: last 3 days ET -> now)
"""
import os, sys, re, json, time, html, urllib.request, urllib.error, datetime as dt
from db import connect

SHOP  = os.environ.get("SHOPIFY_SHOP", "ikigai-cases.myshopify.com")
TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "")
APIV  = os.environ.get("SHOPIFY_API_VERSION", "2024-10")
URL   = f"https://{SHOP}/admin/api/{APIV}/graphql.json"
PAGE_SLEEP = float(os.environ.get("SHOPIFY_PAGE_SLEEP", "0.4"))

# --- Name reconciliation: Shopify staff names differ from ShipHero/DB names ---
# The DB's canonical person names come from ShipHero (e.g. "Nic Cox", "Manu Bekele").
# Shopify shows the staffer's own account name (e.g. "Nicholas Cox", "Manuhe Bekele").
# We match tolerantly to the warehouse roster: same LAST name + first-name is a prefix
# of the other (the rule the warehouse skill uses: "Manu Bekele" <-> "Manuhe Bekele").
# ROSTER = the canonical names the DB already uses for pick/pack (keep in sync w/ read_api).
ROSTER = ["Nic Cox", "Halil Gurler", "Jeffrey Kwan", "Kadil Ladson", "Manu Bekele",
          "Maurice Williams", "Shambria Green", "Breton Rice", "Broghan Rice",
          "Esra Altug", "Simay Guner", "Cindy Lin", "Brennen Myrick", "Lara Nielsen",
          "Patrick Robin", "Daniella Gross"]
# Explicit overrides for anything the tolerant rule can't resolve. Add pairs as found.
NAME_ALIASES = {}
_UNKNOWN = set()

def _norm(s):
    return " ".join((s or "").strip().lower().replace(",", " ").split())

def canon(name):
    if name in NAME_ALIASES:
        return NAME_ALIASES[name]
    parts = _norm(name).split()
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        for r in ROSTER:
            rp = _norm(r).split()
            rf, rl = rp[0], rp[-1]
            if last == rl and (first.startswith(rf) or rf.startswith(first)):
                return r
    if name not in ROSTER:
        _UNKNOWN.add(name)     # surfaced at end of run so we can add an alias
    return name

TAG = re.compile(r"<[^>]+>")
def strip_tags(s):
    return html.unescape(TAG.sub("", s or "")).strip()

RE_MARK  = re.compile(r"^(.+?) marked (\d+) items? as fulfilled")
RE_SHOP  = re.compile(r"^Shopify marked (\d+) items? as fulfilled")
RE_LABEL = re.compile(r"^(.+?) purchased a")

def _post(body, tries=6):
    data = json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(URL, data=data, headers={
            "Content-Type": "application/json", "X-Shopify-Access-Token": TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < tries - 1:
                time.sleep(3 * (attempt + 1)); continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(5); continue
            raise

def gql(q, tries=8):
    for attempt in range(tries):
        resp = _post({"query": q})
        if resp.get("data") is not None:
            return resp["data"]
        errs = json.dumps(resp.get("errors") or [])[:300]
        if "throttl" in errs.lower() and attempt < tries - 1:
            time.sleep(4 * (attempt + 1)); continue
        raise RuntimeError(f"Shopify GraphQL error: {errs}")
    raise RuntimeError("Shopify throttled past retry budget")

def parse_order(order, start, end):
    """Yield (created_iso, person, qty, order_name) for hand-fulfillment events."""
    name = order["name"]
    evs = ((order.get("events") or {}).get("nodes")) or []
    labelers = []
    for e in evs:
        if e.get("action") == "shipping_label_created_success" and e.get("attributeToUser"):
            m = RE_LABEL.match(strip_tags(e.get("message")))
            if m:
                labelers.append((m.group(1).strip(), _ms(e.get("createdAt"))))
    out = []
    for e in evs:
        if e.get("action") != "fulfillment_success":
            continue
        t = _ms(e.get("createdAt"))
        if t is None or t < start or t > end:
            continue
        msg = strip_tags(e.get("message"))
        if re.match(r"^ShipHero", msg, re.I):
            continue
        person = None; qty = 0
        m = RE_MARK.match(msg)
        if m and e.get("attributeToUser"):
            person = m.group(1).strip(); qty = int(m.group(2))
        else:
            a = RE_SHOP.match(msg)
            if a and labelers:
                qty = int(a.group(1))
                person = min(labelers, key=lambda l: abs(l[1] - t))[0]
        if not person or qty <= 0:
            continue
        out.append((e["createdAt"], canon(person), qty, name))
    return out

def _ms(iso):
    if not iso:
        return None
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

INS = ("INSERT INTO event (ts,person,stage,station,action,order_number,sku,quantity,"
       "subtype,source,ext_id,dedup_key) VALUES (%s,%s,'pack',NULL,'pack',%s,NULL,%s,"
       "'hand_fulfillment','shopify',%s,%s) ON CONFLICT (dedup_key) DO NOTHING")

def run(df, dt_):
    if not TOKEN:
        raise SystemExit("SHOPIFY_ADMIN_TOKEN not set")
    start = _ms(df + "T00:00:00Z")
    end   = _ms(dt_ + "T23:59:59Z")
    qrange = f"updated_at:>={df}T00:00:00Z updated_at:<={dt_}T23:59:59Z"
    after = None; pages = 0; rows = []
    while pages < 400:
        q = ('{ orders(first:50, sortKey:UPDATED_AT, reverse:true, query:%s%s){ '
             'pageInfo{hasNextPage endCursor} nodes{ name events(first:15, sortKey:CREATED_AT, reverse:true){ '
             'nodes{ createdAt action attributeToUser message } } } } }') % (
             json.dumps(qrange), (", after:" + json.dumps(after)) if after else "")
        data = gql(q); pages += 1
        orders = (data.get("orders") or {})
        for o in (orders.get("nodes") or []):
            for created, person, qty, name in parse_order(o, start, end):
                dk = "shopify|" + name + "|" + created
                rows.append((created, person, name, qty, name + "|" + created, dk))
        pi = orders.get("pageInfo") or {}
        after = pi.get("endCursor") if pi.get("hasNextPage") else None
        if not after:
            break
        time.sleep(PAGE_SLEEP)
    # dedup within this run by dedup_key
    seen = {}
    for r in rows:
        seen[r[5]] = r
    rows = list(seen.values())
    with connect() as c, c.cursor() as cur:
        cur.executemany(INS, rows)
        inserted = cur.rowcount
        cur.execute("SELECT DISTINCT (ts AT TIME ZONE 'America/New_York')::date d "
                    "FROM event WHERE source='shopify' AND (ts AT TIME ZONE 'America/New_York')::date "
                    "BETWEEN %s AND %s", (df, dt_))
        days = [list(x.values())[0] if isinstance(x, dict) else x[0] for x in cur.fetchall()]
        for d in days:
            cur.execute("SELECT refresh_stage_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_shift_day(%s)", (d,))
        c.commit()
    print(f"[shopify] pages={pages} events={len(rows)} newly_inserted={inserted} "
          f"days_refreshed={len(days)} window={df}..{dt_}", flush=True)
    if _UNKNOWN:
        print("[shopify] WARN unmatched fulfiller names (add to NAME_ALIASES): "
              + ", ".join(sorted(_UNKNOWN)), flush=True)

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        run(sys.argv[1], sys.argv[2])
    else:
        now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))
        run(str((now - dt.timedelta(days=3)).date()), str(now.date()))
