"""fulfill_resolve.py -- CONTRIBUTION-SIDE companion for the Fulfillment logger.

Belongs in the `ikigai-tote-webhook` mono-repo, run as a cron on the contribution
service (same way `engrave_resolve.py` runs). It fills the item COUNT for the
fulfillment-logger pack rows.

The logger writes one row per packed order:
    stage='pack', source='logger', action='fulfill', order_number=<ORDER>, quantity=NULL
Order counts work immediately (each row is one order). This script fills the
`quantity` (item count) per order so item totals show too.

QUANTITY SOURCE -- no new secrets:
It copies the item count from the Shopify fulfillment ShipHero/Shopify already
knows about -- i.e. the existing pack events for the same order. Keep
`shopify_ingest.py` RUNNING (it still ingests each Shopify fulfillment's item
quantity). This script reattributes nothing -- it only borrows the per-order qty.

MATCHING: both sides are reduced to their NUMERIC CORE -- every non-digit is
stripped (upper/trim first for good measure) -- because the same Shopify order is
written in several shapes across the pipeline and the scanner:
  Shopify order NAME  -> "#IC201198"   (carries '#IC')
  logger scan (clean) -> "IC201198" or bare "201198"  (scanner drops '#IC' or not)
Reducing both to "201198" makes all those shapes match. Matching on the raw value
(or only stripping '#') silently fails on the 'IC' or a bare-number scan, which is
why item counts can read 0. Order names here are IC+6-digit, so the numeric core
is unique -- no false matches. Rows with no digits at all are skipped.

Short/garbled scans (a truncated barcode read like "1854" or "IC2011") have no
6-digit core and simply stay unmatched -- their ORDER credit is still correct
(one row = one order to the right packer); only the item COUNT is missing. Those
show up in the diagnostic below so a scanner problem is visible.

Idempotent: only fills rows where quantity IS NULL. Safe to run every few minutes.
Prints a short diagnostic (matched count + sample unmatched logger orders + sample
Shopify order names) so a mismatch is visible in the cron log.
"""
import os, sys
import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "")
CONNECT_TIMEOUT = int(os.environ.get("PG_CONNECT_TIMEOUT", "25"))

# normalized order key: the numeric core, applied identically to both join sides.
NORM = "regexp_replace(upper(btrim({col})), '[^0-9]', '', 'g')"

FILL_SQL = """
WITH src AS (
    SELECT {norm_src} AS ordn, SUM(COALESCE(quantity,0))::int AS qty
    FROM event
    WHERE stage = 'pack' AND source <> 'logger'
      AND order_number IS NOT NULL AND quantity IS NOT NULL
      AND order_number ~ '[0-9]'
    GROUP BY 1
    HAVING SUM(COALESCE(quantity,0)) > 0
)
UPDATE event L
SET quantity = src.qty
FROM src
WHERE L.source = 'logger' AND L.stage = 'pack' AND L.quantity IS NULL
  AND L.order_number ~ '[0-9]'
  AND {norm_l} = src.ordn
RETURNING L.id, L.order_number, L.quantity, et_day(L.ts) AS d;
""".format(norm_src=NORM.format(col="order_number"),
           norm_l=NORM.format(col="L.order_number"))

# ET-days that changed, so we refresh the rollups (same helpers engrave_resolve uses)
REFRESH = [
    "SELECT refresh_stage_contribution_day(%s)",
    "SELECT refresh_contribution_day(%s)",
    "SELECT refresh_shift_day(%s)",
]

UNMATCHED_SAMPLE = (
    "SELECT order_number FROM event "
    "WHERE source='logger' AND stage='pack' AND quantity IS NULL "
    "ORDER BY ts DESC LIMIT 8"
)
SHOPIFY_SAMPLE = (
    "SELECT DISTINCT order_number FROM event "
    "WHERE stage='pack' AND source='shopify' AND quantity IS NOT NULL "
    "AND et_day(ts) = et_day(now()) ORDER BY order_number DESC LIMIT 8"
)


def _alnum(s):
    return "".join(ch for ch in (s or "").upper() if ch.isalnum())


def _order_norm(s):
    """The shape the logger stores an order_number in: strip a leading '#', upper."""
    return (s or "").strip().lstrip("#").strip().upper()


def resolve_tracking(cur):
    """DUAL-METHOD: turn a logger row that was a shipping-label TRACKING scan into
    the real order.

    A packer can scan the tracking barcode instead of the order barcode; that drops
    the tracking number as order_number. `tracking_sync.py` maps tracking->order_name.
    Here we (a) rewrite such a row to the real order (so item-count fill + the
    dashboard credit work), and (b) DEDUP -- if the same real order was also scanned
    via its order barcode, delete the redundant tracking row so nobody is credited
    twice. Returns (changed_days, resolved_count, deleted_count).
    """
    cur.execute("CREATE TABLE IF NOT EXISTS tracking_order ("
                " tracking text PRIMARY KEY, order_name text NOT NULL,"
                " updated_at timestamptz DEFAULT now())")
    cur.execute("SELECT tracking, order_name FROM tracking_order")
    tmap = {r[0]: r[1] for r in cur.fetchall()}
    if not tmap:
        return [], 0, 0
    tkeys = [k for k in tmap if len(k) >= 10]
    # logger fulfillment rows whose scanned value looks like a tracking number (long alnum)
    cur.execute("SELECT id, order_number, et_day(ts) AS d FROM event "
                "WHERE source='logger' AND stage='pack' "
                "AND length(regexp_replace(order_number, '[^0-9A-Za-z]', '', 'g')) >= 12")
    days, resolved, deleted = set(), 0, 0
    for rid, ordn, d in cur.fetchall():
        key = _alnum(ordn)
        real = tmap.get(key)
        if real is None:                       # scanned barcode may embed the tracking (e.g. USPS prefix)
            for tk in tkeys:
                if tk in key:
                    real = tmap[tk]; break
        if real is None:
            continue
        stored = _order_norm(real)             # "#IC201854" -> "IC201854"
        core = "".join(ch for ch in stored if ch.isdigit())
        if not core:
            continue
        cur.execute("SELECT id FROM event WHERE source='logger' AND stage='pack' AND id<>%s "
                    "AND regexp_replace(upper(btrim(order_number)),'[^0-9]','','g')=%s LIMIT 1",
                    (rid, core))
        if cur.fetchone():                     # order already captured -> drop the redundant tracking row
            cur.execute("DELETE FROM event WHERE id=%s", (rid,))
            deleted += 1
        else:
            cur.execute("UPDATE event SET order_number=%s, dedup_key=%s WHERE id=%s",
                        (stored, "logger|fulfill|" + stored, rid))
            resolved += 1
        if d:
            days.add(d)
    return sorted(days), resolved, deleted


def main():
    if not DATABASE_URL:
        print("DATABASE_URL not set", file=sys.stderr); sys.exit(1)
    with psycopg.connect(DATABASE_URL, connect_timeout=CONNECT_TIMEOUT) as c, c.cursor() as cur:
        try:
            t_days, t_res, t_del = resolve_tracking(cur)
        except Exception as e:
            print(f"[fulfill_resolve] tracking-resolve skipped: {e!r}", flush=True)
            t_days, t_res, t_del = [], 0, 0
        cur.execute(FILL_SQL)
        rows = cur.fetchall()
        days = sorted(set(r[3] for r in rows if r[3] is not None) | set(t_days))
        for d in days:
            for stmt in REFRESH:
                try:
                    cur.execute(stmt, (d,))
                except Exception as e:
                    print(f"[fulfill_resolve] refresh {stmt} for {d} skipped: {e!r}", flush=True)
        # diagnostics
        cur.execute(UNMATCHED_SAMPLE)
        unmatched = [r[0] for r in cur.fetchall()]
        cur.execute(SHOPIFY_SAMPLE)
        shop = [r[0] for r in cur.fetchall()]
        c.commit()
    print(f"[fulfill_resolve] filled {len(rows)} order(s); tracking-resolved {t_res}, "
          f"deduped {t_del}; refreshed {len(days)} day(s): {days}", flush=True)
    if unmatched:
        print(f"[fulfill_resolve] still-unmatched logger orders (sample): {unmatched}", flush=True)
        print(f"[fulfill_resolve] today's shopify pack order_numbers (sample): {shop}", flush=True)


if __name__ == "__main__":
    main()
