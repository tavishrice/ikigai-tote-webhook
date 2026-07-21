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


def main():
    if not DATABASE_URL:
        print("DATABASE_URL not set", file=sys.stderr); sys.exit(1)
    with psycopg.connect(DATABASE_URL, connect_timeout=CONNECT_TIMEOUT) as c, c.cursor() as cur:
        cur.execute(FILL_SQL)
        rows = cur.fetchall()
        days = sorted({r[3] for r in rows if r[3] is not None})
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
    print(f"[fulfill_resolve] filled {len(rows)} order(s); refreshed {len(days)} day(s): {days}",
          flush=True)
    if unmatched:
        print(f"[fulfill_resolve] still-unmatched logger orders (sample): {unmatched}", flush=True)
        print(f"[fulfill_resolve] today's shopify pack order_numbers (sample): {shop}", flush=True)


if __name__ == "__main__":
    main()
