"""
Shopify -> Postgres `event` ingest, using the ORDER TIMELINE so hand-fulfillments
are attributed to the real staffer and deduped vs ShipHero.

============================ CORRECTED 2026-07-15 (attribution fix) ============================
Three kinds of `fulfillment_success` events appear on Ikigai orders:
  1. "ShipHero Inventory & Shipping marked N items ..."  -> already counted in ShipHero packs. SKIP.
  2. "Shopify marked N items as fulfilled" (no user)     -> whoever PURCHASED THE SHIPPING LABEL
                                                            physically packed & shipped it. CREDIT them.
  3. "<Person> marked N items as fulfilled" (a user)     -> the "double check engravings" / QC /
                                                            archival close-out step. NOT packing labor. DROP.

The previous version credited (3) as a pack. That produced large phantom credit for people who only
ever do the QC mark and never buy a label (e.g. Breton Rice, credited ~752 phantom items; also inflated
Shambria/Kadil/Esra/Simay/Maurice). Verified against live order timelines on 2026-07-15: person-"marked
as fulfilled" events are the engraving/QC handling of the personalised line items, while the actual
Shopify hand-pack is signalled by the shipping-label purchase (kind 2). So we now credit ONLY kind 2.
If Tavish later confirms some person-marks ARE genuine solo hand-packs, re-add a guarded branch that
credits a person-mark only when that SAME person also bought the label on the order and there was no
prior system fulfillment. (See test_shopify_attribution.py for the fixtures proving this behaviour.)
===============================================================================================

Env: SHOPIFY_SHOP, SHOPIFY_CLIENT_ID, SHOPIFY_CLIENT_SECRET, DATABASE_URL
Backfill:  python shopify_ingest.py 2026-07-01 2026-07-15
Live cron: python shopify_ingest.py                 (defaults: last 3 days ET -> now)
"""
import os, sys, re, json, time, html, urllib.request, urllib.error, urllib.parse, datetime as dt
from db import connect

SHOP   = os.environ.get("SHOPIFY_SHOP", "ikigai-cases.myshopify.com")
CLIENT_ID     = os.environ.get("SHOPIFY_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("SHOPIFY_CLIENT_SECRET", "")
APIV   = os.environ.get("SHOPIFY_API_VERSION", "2025-01")
URL    = f"https://{SHOP}/admin/api/{APIV}/graphql.json"
TOKEN_URL = f"https://{SHOP}/admin/oauth/access_token"
PAGE_SLEEP = float(os.environ.get("SHOPIFY_PAGE_SLEEP", "0.4"))

_TOKEN = {"val": None}
def get_token():
    if _TOKEN["val"]:
        return _TOKEN["val"]
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            _TOKEN["val"] = json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"[shopify] token request failed HTTP {e.code}: {detail}  "
                         f"(check: app installed on this store? read_orders scope? same org?)")
    return _TOKEN["val"]

# --- Name reconciliation: Shopify staff names differ from ShipHero/DB names ---
ROSTER = ["Nic Cox", "Halil Gurler", "Jeffrey Kwan", "Kadil Ladson", "Manu Bekele",
          "Maurice Williams", "Shambria Green", "Breton Rice", "Broghan Rice",
          "Esra Altug", "Simay Guner", "Cindy Lin", "Brennen Myrick", "Lara Nielsen",
          "Patrick Robin", "Daniella Gross"]
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
        _UNKNOWN.add(name)
    return name

TAG = re.compile(r"<[^>]+>")
def strip_tags(s):
    return html.unescape(TAG.sub("", s or "")).strip()

RE_SHOP  = re.compile(r"^Shopify marked (\d+) items? as fulfilled")
RE_LABEL = re.compile(r"^(.+?) purchased a")

def _post(body, tries=6):
    data = json.dumps(body).encode()
    for attempt in range(tries):
        req = urllib.request.Request(URL, data=data, headers={
            "Content-Type": "application/json", "X-Shopify-Access-Token": get_token()})
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

def _ms(iso):
    if not iso:
        return None
    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)

def parse_order(order, start, end):
    """Yield (created_iso, person, qty, order_name) for REAL Shopify hand-pack events only.

    Kind 2 ("Shopify marked N") -> credit the nearest shipping-label purchaser (the packer).
    Kind 1 ("ShipHero ...")     -> skip (counted in ShipHero packs).
    Kind 3 ("<Person> marked N")-> DROP: engraving/QC/archival close-out, not packing.
    """
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
            continue                                    # kind 1
        a = RE_SHOP.match(msg)
        if a and labelers:                              # kind 2 -> real hand-pack by label purchaser
            qty = int(a.group(1))
            person = min(labelers, key=lambda l: abs(l[1] - t))[0]
            out.append((e["createdAt"], canon(person), qty, name))
            continue
        # kind 3 ("<Person> marked N items as fulfilled") -> intentionally DROPPED (QC / close-out)
    return out

INS = ("INSERT INTO event (ts,person,stage,station,action,order_number,sku,quantity,"
       "subtype,source,ext_id,dedup_key) VALUES (%s,%s,'pack',NULL,'pack',%s,NULL,%s,"
       "'hand_fulfillment','shopify',%s,%s) ON CONFLICT (dedup_key) DO NOTHING")

def run(df, dt_):
    if not (CLIENT_ID and CLIENT_SECRET):
        raise SystemExit("SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET not set")
    start = _ms(df + "T00:00:00Z")
    end   = _ms(dt_ + "T23:59:59Z")
    qrange = f"updated_at:>={df}T00:00:00Z updated_at:<={dt_}T23:59:59Z"
    after = None; pages = 0; rows = []; orders_seen = 0
    while pages < 400:
        q = ('{ orders(first:50, sortKey:UPDATED_AT, reverse:true, query:%s%s){ '
             'pageInfo{hasNextPage endCursor} nodes{ name events(first:15, sortKey:CREATED_AT, reverse:true){ '
             'nodes{ createdAt action attributeToUser message } } } } }') % (
             json.dumps(qrange), (", after:" + json.dumps(after)) if after else "")
        data = gql(q); pages += 1
        orders = (data.get("orders") or {})
        for o in (orders.get("nodes") or []):
            orders_seen += 1
            for created, person, qty, name in parse_order(o, start, end):
                dk = "shopify|" + name + "|" + created
                rows.append((created, person, name, qty, name + "|" + created, dk))
        pi = orders.get("pageInfo") or {}
        after = pi.get("endCursor") if pi.get("hasNextPage") else None
        if not after:
            break
        time.sleep(PAGE_SLEEP)
    seen = {}
    for r in rows:
        seen[r[5]] = r
    rows = list(seen.values())

    # SELF-HEALING (2026-07-15): the daily cron only INSERTs, so any phantom "person marked as
    # fulfilled" rows written by the OLD ingest (esp. same-day close-out sweeps) would linger until a
    # manual repair. Instead, each run REBUILDS its window: delete this window's Shopify pack rows,
    # then re-insert the corrected set. Because the rolling cron covers the last 3 days INCLUDING
    # today, today's phantom credit is cleaned on every 5-min tick — no separate repair needed.
    # Safety: only rebuild if we actually fetched orders (never wipe the window on an empty/anomalous
    # response). The whole delete+insert+refresh runs in ONE transaction, so a failure rolls back.
    if orders_seen == 0:
        print(f"[shopify] SKIP rebuild: 0 orders fetched for {df}..{dt_} (kept existing rows).", flush=True)
        return
    d0 = dt.date.fromisoformat(df); d1 = dt.date.fromisoformat(dt_)
    win_days = [d0 + dt.timedelta(days=i) for i in range((d1 - d0).days + 1)]
    with connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM event WHERE source='shopify' AND stage='pack' "
                    "AND (ts AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s", (df, dt_))
        deleted = cur.rowcount
        cur.executemany(INS, rows)
        inserted = cur.rowcount
        for d in win_days:            # refresh EVERY day in the window so cleaned days recompute
            cur.execute("SELECT refresh_stage_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_shift_day(%s)", (d,))
        c.commit()
    print(f"[shopify] pages={pages} orders_seen={orders_seen} corrected_rows={len(rows)} "
          f"deleted_old={deleted} inserted={inserted} days_refreshed={len(win_days)} "
          f"window={df}..{dt_}", flush=True)
    if _UNKNOWN:
        print("[shopify] WARN unmatched fulfiller names (add to NAME_ALIASES): "
              + ", ".join(sorted(_UNKNOWN)), flush=True)

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        run(sys.argv[1], sys.argv[2])
    else:
        now = dt.datetime.now(dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=-4)))
        run(str((now - dt.timedelta(days=3)).date()), str(now.date()))
