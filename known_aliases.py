"""
Manually-curated identity mappings for the contribution app.

Most identities resolve automatically (exact name, nickname/variant, HR sync).
This file is only for the cases a human has to settle:

  ALIASES       raw `event.person` string  ->  canonical HR "Employee Name".
                Use for bare source identities that can't be auto-resolved
                (ShipHero user ids with no pick/pack name, odd logger logins,
                one-off nicknames the matcher won't catch). identity_resolve.py
                upserts these into person_alias. `ON CONFLICT DO NOTHING` means a
                mapping you make directly in the DB is never overwritten by this
                file; to CHANGE a seeded mapping, edit it here and it re-applies.

  NON_EMPLOYEES real people who show up in the data but are NOT HR employees
                (owners / family / guests). They stay visible under their own
                name, untagged, and are NOT flagged as "unidentified".

To identify a ShipHero user id: the string `User-XXXX` base64-decodes to
`User:<id>` (e.g. User-VXNlcjo1ODEwMDM=  ->  User:581003). A bare number is a
legacy ShipHero user id. Ask whoever runs the floor / check ShipHero user admin,
then add `"<raw string>": "<their HR name>"` below.
"""

ALIASES = {
    # ShipHero identities seen on engraving/inventory with no pick/pack name and
    # no match in any connected system (~1-4 items each). Fill in once named:
    # "787802395":              "Full Name",   # legacy ShipHero user id
    # "User-VXNlcjo1ODEwMDM=":  "Full Name",   # ShipHero user 581003
    # "User-VXNlcjozODc2OTQ=":  "Full Name",   # ShipHero user 387694
}

NON_EMPLOYEES = {
    "Broghan Rice",   # Rice family / owner side; real person, not an HR employee
}
