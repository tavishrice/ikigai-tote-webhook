"""
Manually-curated identity mappings for the contribution app.

Most identities resolve automatically (exact name, nickname/variant, HR sync,
and station-based mis-scan recovery in identity_resolve.py). This file is only
for cases a human settled:

  ALIASES       raw `event.person` string  ->  canonical HR "Employee Name".
                identity_resolve.py upserts these into person_alias. `ON CONFLICT
                DO NOTHING` means a mapping made directly in the DB is never
                overwritten by this file; to CHANGE a seeded mapping, edit it here.

  NON_EMPLOYEES real people who show up in the data but are NOT HR employees
                (owners / family / guests). Shown under their own name, untagged,
                not flagged as "unidentified".

Engraving mis-scans: when someone scans a tote into the tablet's login box, the
"engraver" becomes the tote/order barcode. identity_resolve.reattribute_misscans()
now auto-credits these to the real engraver at that station+time. The entries
below are the ones recovered by hand on 2026-07-21 (also present in the DB); they
double as documentation and as a fallback if a DB alias is ever removed.
"""

ALIASES = {
    # Engraving mis-scans recovered 2026-07-21 (barcode scanned into login box):
    "787802425": "Manu Bekele",    # Engraving 2, 2026-07-21
    "962":       "Manu Bekele",    # Engraving 2, 2026-07-21
    "IC202138":  "Halil Gurler",   # Engraving 1, 2026-07-21
    "787681011": "Halil Gurler",   # Engraving 1, 2026-07-21
    "787802395": "Halil Gurler",   # Engraving 1, 2026-07-17
    # ShipHero inventory users with no name in any connected system (ignore unless
    # they become active again — the board hides them, Data Issues ages them out):
    # "User-VXNlcjo1ODEwMDM=": "Full Name",   # ShipHero user 581003
    # "User-VXNlcjozODc2OTQ=": "Full Name",   # ShipHero user 387694
}

NON_EMPLOYEES = {
    "Broghan Rice",   # Rice family / owner side; real person, not an HR employee
}
