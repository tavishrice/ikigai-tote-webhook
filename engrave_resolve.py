"""
Engraving RESOLVER — the missing brain.

An engraving scan only carries a `tote_barcode`, and totes are reused across many orders all day, so a
scan is meaningless until we resolve *what was in that tote at that moment*. The ShipHero Tote-Complete
webhook records that in `tote_content` (tote_barcode -> order + engraving SKUs + qty + type, stamped
`received_at`). This joins them:

  For each engrave scan (tote, ts): the matching tote_content batch = the rows for that tote with the
  GREATEST received_at <= ts (+ a small grace for clock skew). A tote is built (webhook) then engraved
  (scan), so the most recent build before the scan is the tote's occupant at scan time — which is how
  reuse is handled. Fallback: the nearest received_at within FALLBACK_HR if none at/just-before the scan.

It writes onto the engrave `event` row: quantity = number of engraving ITEMS in the tote, order_number =
the primary order, raw = the full breakdown. So the dashboard's "Engraved" (distinct totes) and engrave
ITEMS (sum quantity) both become real. Set-based (one UPDATE), idempotent — safe to re-run as a cron
(also re-resolves scans whose tote_content arrived late).

Run:  python engrave_resolve.py
"""
from db import connect

GRACE_MIN = 10      # a tote_content batch up to this long after the scan still counts (clock skew)
FALLBACK_HR = 12    # if nothing at/just-before the scan, take the nearest batch within this window

UPDATE_SQL = f"""
WITH b AS (   -- the batch received_at we attribute each scan to
  SELECT e.id, e.person, e.tote_barcode, e.ts,
    COALESCE(
      (SELECT max(t.received_at) FROM tote_content t
         WHERE t.tote_barcode=e.tote_barcode AND t.received_at <= e.ts + interval '{GRACE_MIN} minutes'),
      (SELECT t.received_at FROM tote_content t
         WHERE t.tote_barcode=e.tote_barcode
           AND t.received_at BETWEEN e.ts - interval '{FALLBACK_HR} hours' AND e.ts + interval '{FALLBACK_HR} hours'
         ORDER BY abs(extract(epoch FROM (t.received_at - e.ts))) LIMIT 1)
    ) bt
  FROM event e WHERE e.stage='engrave'
),
a AS (   -- resolve order(s), item count and line breakdown for that batch
  SELECT b.id, b.bt,
    (SELECT string_agg(DISTINCT t.order_number, ', ') FROM tote_content t
       WHERE t.tote_barcode=b.tote_barcode AND t.received_at=b.bt) orders,
    (SELECT sum(t.quantity) FROM tote_content t
       WHERE t.tote_barcode=b.tote_barcode AND t.received_at=b.bt) items,
    (SELECT jsonb_agg(jsonb_build_object('order',t.order_number,'sku',t.sku,'qty',t.quantity,'type',t.engraving_type))
       FROM tote_content t WHERE t.tote_barcode=b.tote_barcode AND t.received_at=b.bt) lines,
    -- DEDUPE re-scans: within one person + tote_barcode + batch, only the FIRST scan carries the item
    -- count; any re-scan of the same tote-occupancy carries 0 items (it's the same physical engraving,
    -- not new work). Distinct-tote count and the scan/action record are untouched.
    row_number() OVER (PARTITION BY b.person, b.tote_barcode, b.bt ORDER BY b.ts, b.id) AS rn
  FROM b
)
UPDATE event e SET
  quantity     = CASE WHEN a.rn > 1 THEN 0 ELSE COALESCE(a.items, 1) END,
  order_number = NULLIF(split_part(COALESCE(a.orders,''), ', ', 1), ''),
  raw = CASE WHEN a.bt IS NULL THEN jsonb_build_object('matched',false,'dup',a.rn>1)
             ELSE jsonb_build_object('matched',true,'batch_ts',a.bt,'orders',a.orders,
                                     'items',a.items,'lines',a.lines,'dup',a.rn>1) END
FROM a WHERE e.id=a.id
"""

def resolve(refresh_days_back=None):
    """Resolve every engrave scan to its tote_content batch (set-based UPDATE over all rows —
    cheap and idempotent), then refresh the daily rollup.

    refresh_days_back: if given (e.g. 2 from the 5-min cron), only refresh rollups for engrave
    days within that many days of today ET — historical days are static, so re-rolling them
    every 5 min is wasted work. Pass None (default, e.g. a manual/backfill run) to refresh all."""
    with connect() as c, c.cursor() as cur:
        cur.execute(UPDATE_SQL)
        n = cur.rowcount
        if refresh_days_back is None:
            cur.execute("SELECT DISTINCT (ts AT TIME ZONE 'America/New_York')::date "
                        "FROM event WHERE stage='engrave'")
        else:
            cur.execute("SELECT DISTINCT (ts AT TIME ZONE 'America/New_York')::date "
                        "FROM event WHERE stage='engrave' "
                        "AND ts >= (now() AT TIME ZONE 'America/New_York')::date - make_interval(days => %s)",
                        (int(refresh_days_back),))
        days = [r[0] for r in cur.fetchall()]
        for d in days:
            cur.execute("SELECT refresh_stage_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_contribution_day(%s)", (d,))
            cur.execute("SELECT refresh_shift_day(%s)", (d,))
        cur.execute("SELECT count(*) FILTER (WHERE order_number IS NOT NULL), count(*), coalesce(sum(quantity),0) "
                    "FROM event WHERE stage='engrave'")
        matched, total, items = cur.fetchone()
        c.commit()
    print(f"[engrave-resolve] updated={n} scans={total} resolved={matched} "
          f"unmatched={total-matched} total_items={items} days_refreshed={len(days)}", flush=True)

if __name__ == "__main__":
    import sys
    # `python engrave_resolve.py`        -> live cron mode: refresh only the last 2 days
    # `python engrave_resolve.py all`    -> refresh every engrave day (one-off / backfill)
    resolve(refresh_days_back=None if (len(sys.argv) > 1 and sys.argv[1] == "all") else 2)
