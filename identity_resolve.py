"""
identity_resolve.py — canonical identity resolution for the contribution app.

Runs daily on Render (in the nightly cron, AFTER hr_sync.py so the employee
table is fresh). Two jobs:

  1. (RE)CREATE the `event_canon` view — `event` with `person` resolved to the
     canonical HR name via person_alias -> employee. Every dashboard query reads
     this view, so multiple source identities for one human (ShipHero name/id,
     Shopify name/id, engraving-logger login) roll up into ONE row with the real
     name. If nothing is aliased, canon = raw person, so the view is a no-op.

  2. RESOLVE unmatched identities into person_alias, conservatively:
       - `known_aliases.ALIASES` (human-curated seed) — highest priority.
       - exact / accent- & case-normalized full-name match to an employee.
       - nickname + first/last match (e.g. "Jeff Kwan" -> "Jeffrey Kwan"),
         but ONLY when exactly one employee matches (never guess between two).
     A pure id (bare number / base64 `User-...`) has no name to match, so it is
     left UNRESOLVED on purpose — the dashboard's Data Issues tab surfaces it
     with its decoded id + counts for a human to name (nobody is named a number).

Never clobbers existing aliases (ON CONFLICT DO NOTHING) — manual DB mappings and
the HR-name identity aliases from hr_sync always win.

stdlib only. ENV: DATABASE_URL (via db.connect).
"""
import sys
import re
import unicodedata

from db import connect

try:
    from known_aliases import ALIASES, NON_EMPLOYEES
except Exception:
    ALIASES, NON_EMPLOYEES = {}, set()

# People deliberately hidden everywhere (fraud / departed) — mirror of read_api.
EXCLUDED = {"Roland Tilk", "Brennen Myrick"}

def _sqlstr(names):
    return ",".join("'" + str(n).replace("'", "''") + "'" for n in sorted(names))

def _canon_where():
    allow = "e.id IS NOT NULL"
    if NON_EMPLOYEES:
        allow = "(e.id IS NOT NULL OR ev.person IN (" + _sqlstr(NON_EMPLOYEES) + "))"
    where = "WHERE " + allow
    if EXCLUDED:
        where += " AND COALESCE(e.name, ev.person) NOT IN (" + _sqlstr(EXCLUDED) + ")"
    return where

# event_canon = the board's data, filtered to REAL known people only (resolved
# employees + known non-employees, minus hidden). Junk/unresolved ids never show
# on any dashboard view; Data Issues still lists them (it reads raw `event`).
CANON_VIEW_DDL = (
    "CREATE OR REPLACE VIEW event_canon AS "
    "SELECT ev.id, ev.ts, COALESCE(e.name, ev.person) AS person, "
    "ev.stage, ev.station, ev.action, ev.order_number, ev.tote_barcode, "
    "ev.sku, ev.quantity, ev.subtype, ev.source, ev.ext_id, "
    "ev.dedup_key, ev.raw, ev.ingested_at "
    "FROM event ev "
    "LEFT JOIN person_alias a ON a.alias = ev.person "
    "LEFT JOIN employee e ON e.id = a.employee_id " + _canon_where())

# columns read_api's queries actually touch — the view must expose all of these
_REQUIRED_COLS = {"person", "ts", "stage", "subtype", "source", "order_number",
                  "quantity", "tote_barcode", "station", "action", "sku"}

NICK = {
    "jeff": "jeffrey", "geoff": "jeffrey", "mike": "michael", "chris": "christopher",
    "alex": "alexander", "nick": "nicholas", "nic": "nicholas", "dan": "daniel",
    "danny": "daniel", "dani": "daniella", "tony": "anthony", "will": "william",
    "bill": "william", "rob": "robert", "bob": "robert", "matt": "matthew",
    "tom": "thomas", "tommy": "thomas", "joe": "joseph", "sam": "samuel",
    "ben": "benjamin", "dave": "david", "manu": "emmanuel", "gabe": "gabriel",
    "kate": "katherine", "liz": "elizabeth", "steve": "steven", "andy": "andrew",
}


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def _keyname(s):
    """(first, last) with nickname expansion; None if not a plausible name."""
    toks = _norm(s).split()
    if len(toks) < 2:
        return None
    first = NICK.get(toks[0], toks[0])
    return (first, toks[-1])


def _looks_like_id(s):
    return bool(re.fullmatch(r"\d+", s or "")) or (s or "").startswith("User-") \
        or (s or "").startswith("User:")


def reattribute_misscans(cur):
    """Mis-scan recovery (engraving): a tote/barcode scanned into the tablet's
    login box becomes an 'engraver' named after the barcode. Credit that work to
    the REAL engraver at that station at that time — the resolved employee with a
    logger event at the SAME station within 20 min of the mis-scan login, and ONLY
    when exactly one real engraver is in that window (unambiguous). Writes an alias
    so the work flows to the right person on the board; never overrides an existing
    alias. Anything ambiguous is left flagged for a human.
    """
    cur.execute("""
      WITH resolved AS (
             SELECT a.alias, e.name FROM person_alias a JOIN employee e ON e.id=a.employee_id),
           unresolved AS (
             SELECT ev.person, ev.station, min(ev.ts) t0, max(ev.ts) t1
             FROM event ev
             WHERE ev.source='logger'
               AND ev.station IS NOT NULL
               AND ev.person NOT IN (SELECT alias FROM resolved)
             GROUP BY ev.person, ev.station)
      SELECT u.person, u.station,
             (SELECT r.name FROM event e2 JOIN resolved r ON r.alias=e2.person
               WHERE e2.source='logger' AND e2.station=u.station
                 AND e2.ts BETWEEN u.t0 - interval '20 min' AND u.t1 + interval '20 min'
                 AND r.name <> ALL(%s)
               GROUP BY r.name
               ORDER BY min(abs(extract(epoch from (e2.ts - u.t0)))) ASC LIMIT 1) nearest,
             (SELECT count(DISTINCT r.name) FROM event e2 JOIN resolved r ON r.alias=e2.person
               WHERE e2.source='logger' AND e2.station=u.station
                 AND e2.ts BETWEEN u.t0 - interval '20 min' AND u.t1 + interval '20 min'
                 AND r.name <> ALL(%s)) n_cands
      FROM unresolved u
    """, (list(EXCLUDED), list(EXCLUDED)))
    added = 0
    for person, station, nearest, n_cands in cur.fetchall():
        if person in EXCLUDED or person in NON_EMPLOYEES:
            continue
        if nearest and n_cands == 1:
            cur.execute(
                "INSERT INTO person_alias (alias, employee_id, source, note) "
                "VALUES (%s,(SELECT id FROM employee WHERE name=%s LIMIT 1),'auto-station',%s) "
                "ON CONFLICT (alias) DO NOTHING",
                (person, nearest, "auto: mis-scan credited to %s @ %s" % (nearest, station)))
            if cur.rowcount:
                added += 1
                print("    re-attributed %r -> %s (@%s)" % (person, nearest, station))
    print("identity_resolve: re-attributed %d mis-scan login(s) to the station engraver" % added)
    return added


def ensure_view(cur):
    cur.execute(CANON_VIEW_DDL)
    # self-test: view resolves and exposes the columns read_api needs
    cur.execute("SELECT * FROM event_canon LIMIT 0")
    cols = {d[0] for d in cur.description}
    missing = _REQUIRED_COLS - cols
    if missing:
        raise RuntimeError("event_canon missing columns: %s" % sorted(missing))
    cur.execute("SELECT count(*) FROM event_canon")
    print("identity_resolve: event_canon OK (%d rows, %d cols)" % (cur.fetchone()[0], len(cols)))


def build_indexes(cur):
    cur.execute("SELECT id, name, email FROM employee")
    by_norm, by_key, id_by_name = {}, {}, {}
    for eid, name, email in cur.fetchall():
        id_by_name[name] = eid
        by_norm.setdefault(_norm(name), set()).add(name)
        k = _keyname(name)
        if k:
            by_key.setdefault(k, set()).add(name)
        if email:
            local = _norm(email.split("@")[0].replace(".", " "))
            if local:
                by_norm.setdefault(local, set()).add(name)
    return by_norm, by_key, id_by_name


def resolve():
    with connect() as c:
        cur = c.cursor()
        ensure_view(cur)
        c.commit()

        by_norm, by_key, id_by_name = build_indexes(cur)

        # identities already mapped to an employee -> skip
        cur.execute("SELECT alias FROM person_alias WHERE employee_id IS NOT NULL")
        mapped = {r[0] for r in cur.fetchall()}

        cur.execute("SELECT person, count(*) FROM event GROUP BY person")
        persons = cur.fetchall()

        added, still = 0, []
        for person, cnt in persons:
            if person in mapped or person in EXCLUDED or person in NON_EMPLOYEES:
                continue
            target = None
            if person in ALIASES:                       # 1) human seed
                target = ALIASES[person]
                if target not in id_by_name:
                    print("identity_resolve: WARN seed '%s' -> unknown employee '%s'"
                          % (person, target))
                    target = None
            if target is None and not _looks_like_id(person):
                cands = by_norm.get(_norm(person)) or by_key.get(_keyname(person) or ())
                if cands and len(cands) == 1:            # 2/3) unique name/nickname
                    target = next(iter(cands))
            if target:
                cur.execute(
                    "INSERT INTO person_alias (alias, employee_id, source, note) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (alias) DO NOTHING",
                    (person, id_by_name[target], "auto",
                     "auto-resolved -> %s" % target))
                added += cur.rowcount
            else:
                still.append((person, int(cnt)))
        reattribute_misscans(cur)
        c.commit()

    still.sort(key=lambda x: -x[1])
    print("identity_resolve: +%d aliases resolved; %d identities still unidentified"
          % (added, len(still)))
    for p, cnt in still[:20]:
        hint = ""
        if p.startswith("User-"):
            import base64
            try:
                hint = " (%s)" % base64.b64decode(p[5:]).decode("ascii", "ignore")
            except Exception:
                hint = ""
        print("    unidentified: %r  x%d%s" % (p, cnt, hint))


if __name__ == "__main__":
    try:
        resolve()
    except Exception as e:
        # never break the nightly job; surface the error and move on
        print("identity_resolve: ERROR %r" % e, file=sys.stderr)
