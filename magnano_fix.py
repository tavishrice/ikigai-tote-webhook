"""
MagNano pack-count correction.

A "MagNano" is up to 7 individual magnetic Nano cases (SKU prefix SPC-MGN) that
snap together into ONE physical strip; a customer can buy anywhere from 1 to 7.
ShipHero picks each nano as its own line, so "N items picked" is physically real
and we leave picking alone. But the pack event's total_items ALSO sums all N
nanos, which over-counts the packing side (packing one strip is ~one action).

Per Tavish/Brogan, a MagNano should count as ONE pack item regardless of how many
nanos it holds. Pack rows carry no SKU, so we derive each order's MagNano nano
count from its own pick rows and collapse:

    pack.quantity = original_total_items - GREATEST(mgn_units - 1, 0)

The original total_items is captured ONCE into raw.orig_items, so this is fully
idempotent (always recomputed from the original) and reversible (restore quantity
from raw.orig_items). Orders with no MagNano are unchanged. Both /warehouse and
/floor read event.quantity directly, so the correction shows up everywhere at once.

Run:
    python magnano_fix.py all     # recompute ALL history (one-time / periodic)
    python magnano_fix.py         # recompute the last 7 days (cron default)
"""
import sys
from db import connect

# Capture the original ShipHero total_items once, before we ever change quantity.
CAP = ("UPDATE event SET raw = jsonb_set(COALESCE(raw,'{}'::jsonb),'{orig_items}', to_jsonb(quantity)) "
       "WHERE stage='pack' AND (raw->>'orig_items') IS NULL")

# Collapse each order's MagNano nanos to one on the pack side. left(sku,7)='SPC-MGN'
# matches every MagNano case/box variant (SPC-MGN1P-*, SPC-MGN2P-*, travel box)
# without any '%' LIKE placeholder ambiguity.
FIX = ("UPDATE event pk SET quantity = (pk.raw->>'orig_items')::int "
       "- GREATEST(COALESCE((SELECT sum(p.quantity) FROM event p "
       "WHERE p.stage='pick' AND left(p.sku,7)='SPC-MGN' AND p.order_number=pk.order_number),0)::int - 1, 0) "
       "WHERE pk.stage='pack' AND (pk.raw->>'orig_items') IS NOT NULL")

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
