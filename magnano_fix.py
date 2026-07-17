"""
MagNano / packaging pack-count correction.

Ikigai's warehouse "packable items" rules (per Tavish & Brogan):
  - MagNano cases (SKU prefix SPC-MGN, minus the travel box): a strip is up to 7
    magnetic cases, and customers order multiple strips, so they collapse in
    increments of 7  ->  ceil(cases / 7)   (7->1, 14->2, 21->3; a partial 1-6 -> 1).
  - Travel box (any SKU ending in -LB, e.g. SPC-MGN2P-LB): counts as 1 (kept as-is).
  - Paper box (any SKU ending in -PB): counts as 0 packable items, on every order.
  - Everything else: 1 each, unchanged.

Picking is deliberately left alone (each case / box is a real, physical pick). Pack
rows carry no SKU, so we derive each order's case + matchbox counts from its own pick
rows and adjust the pack quantity:

    packed = original_total_items
             - (cases - ceil(cases / 7))    # collapse MagNano cases into strips of 7
             - matchboxes                    # paper box -> 0

original_total_items is captured ONCE into raw.orig_items, so this is fully idempotent
(always recomputed from the original) and reversible. Both /warehouse and /floor read
event.quantity directly, so the correction shows up everywhere at once.

Run:  python magnano_fix.py all    # recompute ALL history (one-time / periodic)
      python magnano_fix.py        # recompute the last 7 days (cron default)
"""
import sys
from db import connect

# Capture the original ShipHero total_items once, before we ever change quantity.
CAP = ("UPDATE event SET raw = jsonb_set(COALESCE(raw,'{}'::jsonb),'{orig_items}', to_jsonb(quantity)) "
       "WHERE stage='pack' AND (raw->>'orig_items') IS NULL")

# Per order: MagNano cases (SPC-MGN minus the -LB travel box and -PB paper box) and
# paper boxes (any -PB). Collapse cases to strips of 7 and zero the paper boxes; the
# travel box (-LB) is not in `cases`, so it keeps counting as 1.
FIX = ("WITH m AS ("
       "  SELECT order_number,"
       "    COALESCE(sum(quantity) FILTER (WHERE left(sku,7)='SPC-MGN' AND right(sku,3) NOT IN ('-LB','-PB')),0) cases,"
       "    COALESCE(sum(quantity) FILTER (WHERE right(sku,3)='-PB'),0) pb"
       "  FROM event WHERE stage='pick' AND (left(sku,7)='SPC-MGN' OR right(sku,3)='-PB')"
       "  GROUP BY order_number)"
       " UPDATE event pk"
       " SET quantity = (pk.raw->>'orig_items')::int"
       "   - (m.cases - CEIL(m.cases::numeric/7))::int"
       "   - m.pb"
       " FROM m"
       " WHERE pk.stage='pack' AND pk.order_number = m.order_number"
       "   AND (pk.raw->>'orig_items') IS NOT NULL"
       "   AND (m.cases > 0 OR m.pb > 0)")

RECENT = " AND pk.ts >= now() - interval '7 days'"


def run(full=False):
    with connect() as c, c.cursor() as cur:
        cur.execute(CAP); cap = cur.rowcount
        cur.execute(FIX if full else FIX + RECENT); fix = cur.rowcount
        c.commit()
    print(f"[magnano_fix] captured {cap} new pack rows; recomputed {fix} pack quantities "
          f"({'all history' if full else 'last 7 days'})", flush=True)


if __name__ == "__main__":
    run(full=(len(sys.argv) > 1 and sys.argv[1] == "all"))
