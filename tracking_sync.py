"""tracking_sync.py -- Shopify tracking-number -> order-name map for the
fulfillment logger's DUAL-METHOD scan.

A packer can scan the shipping label's TRACKING barcode instead of the order
barcode (the order barcode on the packing slip doesn't always print/scan). This
table lets `fulfill_resolve.py` turn a scanned tracking number into the real
order credit -- so scanning the label counts exactly like scanning the order.

Belongs in the `ikigai-tote-webhook` mono-repo. Runs as a step in the 5-min
ingest cron, right after `shopify_ingest.py`, and REUSES that module's
authenticated GraphQL client (`gql`) -- **no new secrets**. `shopify_ingest` is
`__main__`-guarded, so importing it does not trigger an ingest.

It pulls recently-updated orders' `fulfillments.trackingInfo.number` and upserts
`tracking_order(tracking, order_name)`. `tracking` is stored NORMALIZED
(upper-case, alphanumerics only) so it matches whatever the scanner reads off the
label barcode. Idempotent; safe every few minutes.
"""
import os, sys, datetime as dt
import psycopg
from shopify_ingest import gql          # uses get_token()/URL internally; module is __main__-guarded

DATABASE_URL = os.environ.get("DATABASE_URL", "")
CONNECT_TIMEOUT = int(os.environ.get("PG_CONNECT_TIMEOUT", "25"))
LOOKBACK_DAYS = int(os.environ.get("TRACKING_LOOKBACK_DAYS", "3"))
MAX_PAGES = int(os.environ.get("TRACKING_MAX_PAGES", "40"))

DDL = (
    "CREATE TABLE IF NOT EXISTS tracking_order ("
    " tracking   text PRIMARY KEY,"
    " order_name text NOT NULL,"
    " updated_at timestamptz DEFAULT now())"
)
UPSERT = (
    "INSERT INTO tracking_order (tracking, order_name, updated_at) "
    "VALUES (%s, %s, now()) "
    "ON CONFLICT (tracking) DO UPDATE SET order_name = EXCLUDED.order_name, updated_at = now()"
)


def norm(s):
    """Upper-case, keep only A-Z0-9 -- the shape a scanned barcode is matched against."""
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def build_query(cursor, since_iso):
    after = ', after: "%s"' % cursor if cursor else ""
    return (
        "{ orders(first: 100" + after + ", query: \"updated_at:>=" + since_iso + "\", "
        "sortKey: UPDATED_AT, reverse: true) { "
        "nodes { name fulfillments(first: 10) { trackingInfo { number } } } "
        "pageInfo { hasNextPage endCursor } } }"
    )


def main():
    if not DATABASE_URL:
        print("DATABASE_URL not set", file=sys.stderr); sys.exit(1)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    pairs = {}          # norm(tracking) -> order_name  (dedup within the run)
    cursor, pages = None, 0
    while pages < MAX_PAGES:
        data = gql(build_query(cursor, since)) or {}
        orders = data.get("orders") or {}
        for node in orders.get("nodes") or []:
            name = node.get("name")
            if not name:
                continue
            for f in node.get("fulfillments") or []:
                for ti in (f.get("trackingInfo") or []):
                    key = norm(ti.get("number"))
                    if len(key) >= 8:               # real carrier trackings are long; skip junk
                        pairs[key] = name
        pages += 1
        pi = orders.get("pageInfo") or {}
        if not pi.get("hasNextPage"):
            break
        cursor = pi.get("endCursor")
    with psycopg.connect(DATABASE_URL, connect_timeout=CONNECT_TIMEOUT) as c, c.cursor() as cur:
        cur.execute(DDL)
        for tracking, name in pairs.items():
            cur.execute(UPSERT, (tracking, name))
        c.commit()
    print("[tracking_sync] upserted %d tracking->order rows (since %s, %d page(s))"
          % (len(pairs), since, pages), flush=True)


if __name__ == "__main__":
    main()
