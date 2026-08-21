"""
Read API + dashboard for Ikigai Warehouse — the fulfillment/contribution analytics
dashboard (warehouse picking, packing, engraving and restocking) for Ikigai Cases.
Serves the "Ikigai Warehouse" dashboard (Dashboard / Floor Time / Analytics)
plus JSON endpoints. Reads pre-aggregated + raw event data; live on every open.

CORRECTED 2026-07-15 (frontend presentation):
  - Counting model per Tavish: pick + pack + engrave are ONE type (fulfillment); replenishment is a
    SECOND, parallel type. The fulfillment bar's height now equals the "Items total" (engrave is
    included in items total; it used to be left out). Replenishment is drawn as its own separate bar
    (a second Chart.js stack), never summed into the fulfillment total.
  - The Stage (All/Picked/Packed/Engraved/Replenished) and Source (Both/ShipHero/Shopify) toggles now
    actually filter the chart AND the detail table (previously only the summary line).
  - /warehouse now returns engraved ITEMS (sum quantity) per person, not just a tote count.
"""
import os, json, datetime as dt
from flask import Flask, request, jsonify, Response
from psycopg.rows import tuple_row
from db import connect

app = Flask(__name__)

import time
class _Roster(dict):
    """FT / Intern / Seasonal roster for the dashboard's Type column.

    Seeded with the static fallback below (used only if the DB roster is empty
    or unreachable). At runtime .get() overlays the LIVE roster synced daily
    from the HR Employee Database (Notion) into the employee + person_alias
    tables by hr_sync.py -- so new warehouse hires / interns / seasonals show up
    automatically without editing code. dash_type in {'FT','Intern','Seasonal',''}.
    """
    _at = 0.0
    _live = {}

    @classmethod
    def _refresh(cls):
        now = time.time()
        if cls._live and now - cls._at < 300:      # 5-min cache
            return
        try:
            with connect() as c:
                cur = c.cursor()
                cur.execute(
                    "SELECT a.alias, e.dash_type "
                    "FROM person_alias a JOIN employee e ON e.id = a.employee_id "
                    "WHERE e.is_active AND COALESCE(e.dash_type,'') <> ''")
                cls._live = {row[0]: row[1] for row in cur.fetchall()}
                cls._at = now
        except Exception:
            pass          # tables not created yet / transient DB issue -> keep fallback

    def get(self, key, default=""):
        self._refresh()
        v = _Roster._live.get(key)
        return v if v else dict.get(self, key, default)

PERSON_TYPE = _Roster({
    "Nic Cox":"FT","Halil Gurler":"FT","Kadil Ladson":"FT","Manu Bekele":"FT",
    "Maurice Williams":"FT","Jeffrey Kwan":"FT","Shambria Green":"FT","Breton Rice":"FT",
    "Esra Altug":"Intern","Simay Guner":"Intern","Cindy Lin":"Intern",
    "Lara Nielsen":"Intern","Patrick Robin":"Intern",
    "Broghan Rice":"","Daniella Gross":"",
})
# People no longer on the team — hidden from every view (Roland Tilk: terminated for fraudulent
# submissions; Brennen Myrick: departed). Their historical rows stay in the DB but never surface.
EXCLUDED = {"Roland Tilk", "Brennen Myrick"}

try:
    from known_aliases import NON_EMPLOYEES
except Exception:
    NON_EMPLOYEES = set()

# Unidentified identities stop being flagged once they haven't appeared in this
# many days (stale one-off ids age off the Data Issues board automatically).
UNMATCHED_WINDOW_DAYS = int(os.environ.get("UNMATCHED_WINDOW_DAYS", "14"))

def _sqlstr(names):
    return ",".join("'" + str(n).replace("'", "''") + "'" for n in sorted(names))

def _canon_where():
    """Board shows ONLY real people: resolved employees + known non-employees,
    minus hidden (EXCLUDED). Junk / unresolved ids never reach any event_canon
    view. Data Issues still surfaces them (it reads raw `event`)."""
    allow = "e.id IS NOT NULL"
    if NON_EMPLOYEES:
        allow = "(e.id IS NOT NULL OR ev.person IN (" + _sqlstr(NON_EMPLOYEES) + "))"
    where = "WHERE " + allow
    if EXCLUDED:
        where += " AND COALESCE(e.name, ev.person) NOT IN (" + _sqlstr(EXCLUDED) + ")"
    return where

_CANON_VIEW_DDL = (
    "CREATE OR REPLACE VIEW event_canon AS "
    "SELECT ev.id, ev.ts, COALESCE(e.name, ev.person) AS person, "
    "ev.stage, ev.station, ev.action, ev.order_number, ev.tote_barcode, "
    "ev.sku, ev.quantity, ev.subtype, ev.source, ev.ext_id, "
    "ev.dedup_key, ev.raw, ev.ingested_at "
    "FROM event ev "
    "LEFT JOIN person_alias a ON a.alias = ev.person "
    "LEFT JOIN employee e ON e.id = a.employee_id " + _canon_where())

def _ensure_canon():
    try:
        with connect() as _c:
            _c.cursor().execute(_CANON_VIEW_DDL); _c.commit()
    except Exception:
        pass
_ensure_canon()

# ---------------- Data flags (days whose numbers are known to be wrong) ----------------
# A day where a service was down, a feed never landed, or scanning went sideways still LOOKS
# like a normal row on every chart. A flag is the annotation that says otherwise: it never
# edits a number, it marks the day so a broken day is never read as real performance.
#   scope     -- which part of the data is affected ('all', or one stage / hours / orders)
#   severity  -- 'suspect'       (numbers look wrong, verify them),
#                'incomplete'    (known missing, a backfill can still fix it),
#                'unrecoverable' (the events are gone for good; no backfill will fix this day)
#   status    -- 'open' until someone has checked / backfilled it, then 'resolved'
FLAG_SCOPES = ("all", "pick", "pack", "engrave", "replenish", "hours", "orders")
FLAG_SEVERITIES = ("suspect", "incomplete", "unrecoverable")

_FLAG_DDL = """CREATE TABLE IF NOT EXISTS data_flag (
  id          bigserial PRIMARY KEY,
  d           date NOT NULL,                    -- first affected ET day
  d_end       date NOT NULL,                    -- last affected ET day (= d for a single day)
  scope       text NOT NULL DEFAULT 'all',
  severity    text NOT NULL DEFAULT 'suspect',
  status      text NOT NULL DEFAULT 'open',
  reason      text NOT NULL DEFAULT '',
  author      text NOT NULL DEFAULT '',
  created_at  timestamptz NOT NULL DEFAULT now(),
  resolved_at timestamptz,
  resolution  text NOT NULL DEFAULT ''
)"""
_FLAG_IDX = "CREATE INDEX IF NOT EXISTS data_flag_span_idx ON data_flag (d, d_end)"

def _ensure_flags():
    try:
        with connect() as _c:
            _cur = _c.cursor(); _cur.execute(_FLAG_DDL); _cur.execute(_FLAG_IDX); _c.commit()
    except Exception:
        pass
_ensure_flags()

def _flags(cur, frm=None, to=None, include_resolved=False, limit=200):
    """Flags overlapping [frm, to] (or every flag when no range is given), newest day first."""
    w = []; a = []
    if frm and to:
        w.append("d <= %s AND d_end >= %s"); a += [to, frm]
    if not include_resolved:
        w.append("status = 'open'")
    cur.execute("SELECT id,d,d_end,scope,severity,status,reason,author,created_at,resolved_at,resolution "
                "FROM data_flag" + (" WHERE " + " AND ".join(w) if w else "") +
                " ORDER BY d DESC, id DESC LIMIT %s", a + [limit])
    return [dict(id=r[0], d=str(r[1]), d_end=str(r[2]), scope=r[3], severity=r[4], status=r[5],
                 reason=r[6] or "", author=r[7] or "",
                 created_at=(r[8].isoformat() if r[8] else None),
                 resolved_at=(r[9].isoformat() if r[9] else None),
                 resolution=r[10] or "")
            for r in cur.fetchall()]


def _decode_hint(pn):
    if isinstance(pn, str) and pn.startswith("User-"):
        import base64
        try:
            return base64.b64decode(pn[5:]).decode("ascii", "ignore")
        except Exception:
            return ""
    return ""

def _unmatched(cur):
    """Source identities in event not resolved to an employee (excluding hidden
    people + known non-employees) -- the Data Issues 'unidentified' list."""
    cur.execute("""
        SELECT ev.person, string_agg(DISTINCT ev.source, ',') srcs, count(*) c,
               to_char(max(ts) AT TIME ZONE 'America/New_York','YYYY-MM-DD') last
        FROM event ev
        LEFT JOIN person_alias a ON a.alias = ev.person
        LEFT JOIN employee e ON e.id = a.employee_id
        WHERE e.id IS NULL AND ev.person <> ALL(%s)
        GROUP BY ev.person
        HAVING max(ev.ts) >= now() - make_interval(days => %s)
        ORDER BY c DESC""",
        [list(EXCLUDED | NON_EMPLOYEES), UNMATCHED_WINDOW_DAYS])
    return [dict(person=r[0], sources=r[1], events=int(r[2]), last=r[3],
                 hint=_decode_hint(r[0])) for r in cur.fetchall()]

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

def _range():
    return request.args.get("from"), request.args.get("to")

try:                                         # real Eastern tz so EST/EDT (winter/summer) is always correct
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = dt.timezone(dt.timedelta(hours=-4))
def _ampm(ts):
    if not ts: return ""
    return ts.astimezone(_ET).strftime("%-I:%M %p")
def _hm(ts):   # 24h HH:MM in ET, for prefilling <input type=time>
    if not ts: return ""
    return ts.astimezone(_ET).strftime("%H:%M")
def _hm_ampm(s):   # "13:30" -> "1:30p"
    try:
        hh,mm=map(int,s.split(":")); ap="a" if hh<12 else "p"; h12=hh%12 or 12
        return f"{h12}:{mm:02d}{ap}"
    except Exception: return s

def _daylist(frm, to):
    out=[]; d=dt.date.fromisoformat(frm); end=dt.date.fromisoformat(to)
    while d<=end:
        out.append(dict(d=str(d), dow=d.isoweekday())); d+=dt.timedelta(days=1)
    return out

@app.route("/health")
def health():
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT count(*) FROM event"); n = cur.fetchone()[0]
    return jsonify(status="ok", events=n)

@app.route("/roster")
def roster():
    """Read-only view of the HR-synced people roster (for verification/debug)."""
    try:
        with connect() as c, c.cursor(row_factory=tuple_row) as cur:
            cur.execute("SELECT count(*) FROM employee"); emp=cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM person_alias"); al=cur.fetchone()[0]
            cur.execute("SELECT name, dash_type, hr_status, is_active, "
                        "COALESCE(teams::text,'[]') FROM employee "
                        "WHERE is_active AND dash_type<>'' ORDER BY dash_type, name")
            tagged=[dict(name=r[0], type=r[1], hr_status=r[2], teams=r[4]) for r in cur.fetchall()]
            unmatched=_unmatched(cur)
            cur.execute("SELECT name FROM employee WHERE is_active ORDER BY name")
            active_names=[r[0] for r in cur.fetchall() if r[0] not in EXCLUDED]
            # floor crew for the engraving-tablet name buttons (warehouse-tagged, minus hidden)
            engravers=[t["name"] for t in tagged if t["name"] not in EXCLUDED]
        return jsonify(employees=emp, aliases=al, tagged=tagged, unmatched=unmatched,
                       engravers=engravers, active_names=active_names)
    except Exception as e:
        return jsonify(error=str(e), employees=0, aliases=0, tagged=[])

@app.route("/warehouse")
def warehouse():
    """Everything the dashboard needs for a date range, in one call."""
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH e AS (SELECT person,stage,subtype,source,order_number,quantity,ts,tote_barcode
                   FROM event_canon WHERE et_day(ts) BETWEEN %s AND %s)
        SELECT person,
          COALESCE(sum(quantity) FILTER (WHERE stage='pick'),0)                                  pk_items,
          count(DISTINCT order_number) FILTER (WHERE stage='pick')                                pk_orders,
          COALESCE(sum(quantity) FILTER (WHERE stage='pack' AND source='shiphero'),0)             packsh_items,
          count(DISTINCT order_number) FILTER (WHERE stage='pack' AND source='shiphero')          packsh_orders,
          COALESCE(sum(quantity) FILTER (WHERE stage='pack' AND (source='logger' OR (source='shopify' AND et_day(ts) < DATE '2026-07-21'))),0) packshop_items,
          count(DISTINCT order_number) FILTER (WHERE stage='pack' AND (source='logger' OR (source='shopify' AND et_day(ts) < DATE '2026-07-21'))) packshop_orders,
          COALESCE(sum(quantity) FILTER (WHERE stage='replenish'),0)                              repl_units,
          COALESCE(sum(quantity) FILTER (WHERE stage='engrave'),0)                                 eng_items,
          count(DISTINCT order_number) FILTER (WHERE stage='engrave')                             eng_orders,
          count(*) FILTER (WHERE stage='pick')                                pick_cnt,
          count(*) FILTER (WHERE stage='pack' AND source='shiphero')          pack_cnt,
          count(*) FILTER (WHERE stage='pack' AND (source='logger' OR (source='shopify' AND et_day(ts) < DATE '2026-07-21'))) fulfill_cnt,
          count(*) FILTER (WHERE stage='replenish')                          move_cnt,
          count(*) FILTER (WHERE stage='count')                              count_cnt,
          count(DISTINCT tote_barcode) FILTER (WHERE stage='engrave')        eng_cnt,
          count(DISTINCT et_day(ts)) FILTER (WHERE is_floor_labor(stage,subtype)) active_days,
          min(ts)  FILTER (WHERE is_floor_labor(stage,subtype))              first_ts,
          max(ts)  FILTER (WHERE is_floor_labor(stage,subtype))              last_ts
        FROM e GROUP BY person""", [frm, to])
        rows = cur.fetchall()

        cur.execute("""
        -- SHIPPED = orders with a ShipHero OR Logger pack event. Shopify label prints do NOT
        -- count from the Logger cutover (2026-07-21) onward (a label is not a shipment); but
        -- BEFORE the cutover the Logger did not exist, so Shopify is the only hand-pack signal
        -- and DOES count for those historical days (keeps history consistent, no gap).
        -- Key is normalized with ltrim('#') because the Logger stores order numbers WITHOUT the
        -- leading '#' -- see project note order-number-normalization-CRITICAL.
        WITH o AS (SELECT ltrim(order_number,'#') ordn,
                     bool_or(source='shiphero') sh,
                     bool_or(source='logger') lg,
                     bool_or(source='shopify' AND et_day(ts) < DATE '2026-07-21') shop_pre
                   FROM event WHERE stage='pack' AND order_number IS NOT NULL AND order_number<>''
                     AND person <> ALL(%s)              -- fired/departed packers don't count an order as shipped
                     AND et_day(ts) BETWEEN %s AND %s GROUP BY ltrim(order_number,'#'))
        SELECT count(*) FILTER (WHERE sh OR lg OR shop_pre) total,
               count(*) FILTER (WHERE sh) shiphero,
               count(*) FILTER (WHERE (lg OR shop_pre) AND NOT sh) shopify_only,
               count(*) FILTER (WHERE sh AND (lg OR shop_pre)) both FROM o""", [list(EXCLUDED), frm, to])
        shipped = cur.fetchone()

        flags = _flags(cur, frm, to)      # days in this range whose numbers are known to be wrong

    people = []
    tot = dict(pk_i=0,packsh_i=0,packshop_i=0,eng_i=0,pk_o=0,packsh_o=0,packshop_o=0,eng_o=0,repl=0)
    for r in rows:
        if r[0] in EXCLUDED: continue
        (person,pk_i,pk_o,psh_i,psh_o,psp_i,psp_o,repl,eng_i,eng_o,pick_c,pack_c,ful_c,mov_c,cnt_c,eng_c,adays,first,last)=r
        people.append(dict(person=person, type=PERSON_TYPE.get(person,""), active_days=int(adays or 0),
            items_picked_sh=pk_i, items_packed_sh=psh_i, items_packed_shop=psp_i,
            engraved_items=eng_i, engraved_totes=eng_c, engraved_orders=eng_o, replenished=repl,
            orders_picked_sh=pk_o, orders_packed_sh=psh_o, orders_packed_shop=psp_o))
        tot["pk_i"]+=pk_i; tot["packsh_i"]+=psh_i; tot["packshop_i"]+=psp_i; tot["eng_i"]+=eng_i
        tot["pk_o"]+=pk_o; tot["packsh_o"]+=psh_o; tot["packshop_o"]+=psp_o; tot["eng_o"]+=eng_o; tot["repl"]+=repl
    return jsonify(range={"from":frm,"to":to},
        shipped=dict(total=shipped[0], shiphero=shipped[1], shopify_only=shipped[2], both=shipped[3]),
        totals=tot, people=people, flags=flags)

ACTIVE_BREAK = 2700  # seconds = 45 min. ONE definition of "active time" app-wide: from a person's first
                     # scan to their last, with any gap >= 45 min removed as a break (Floor Time, Speed,
                     # engraving hours all use it).
# Replenishment is logged differently: a picker/replenisher does the physical work FIRST (find boxes, cut
# them open, place inventory on the shelf) and then, in a short burst, scans the empty boxes at their
# locations. So the GAP BEFORE a replenish scan is real work, not a break — but it must be capped so that a
# 2.5h gap before a single box isn't credited as 2.5h of replenishing. We credit min(gap, cap) where the cap
# scales with the box size (units placed): a full 40-unit box ~= 14 min, a 1-unit tote move ~= 2 min.
REPL_BASE = 120           # sec: fixed handling per box (walk to it, open it, log it)
REPL_PER_UNIT = 18        # sec per unit placed on the shelf (40-unit box => 120 + 720 = 840s = 14 min)
REPL_BURST_WINDOW = 600   # sec: replenish scans within 10 min of each other are ONE batch (worked together,
                          # then logged in a burst). So the long gap before the batch is credited to the
                          # WHOLE batch's fair time, not just the first box's.
REPL_BURST_MAX = 3600     # sec: ceiling on the work credited before any one batch (60 min)

@app.route("/floor")
def floor_stats():
    """Effectiveness auditor: per person, active hours (45-min-break session spans) and items, by day."""
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH ev AS (
          SELECT person, ts, (ts AT TIME ZONE 'America/New_York')::date d, stage, quantity
          FROM event_canon WHERE is_floor_labor(stage,subtype) AND et_day(ts) BETWEEN %s AND %s),
        seq AS (
          SELECT person, d, ts, stage, quantity,
            EXTRACT(epoch FROM (ts - lag(ts) OVER w)) gap,
            lag(stage) OVER w prev_stage
          FROM ev WINDOW w AS (PARTITION BY person,d ORDER BY ts)),
        brs AS (   -- a replenish scan starts a new BATCH if the previous scan wasn't replenish or was long ago
          SELECT *, CASE WHEN stage='replenish' AND (prev_stage IS DISTINCT FROM 'replenish' OR gap >= %s)
                         THEN 1 ELSE 0 END bstart
          FROM seq),
        bid AS (SELECT *, sum(bstart) OVER (PARTITION BY person,d ORDER BY ts) batch FROM brs),
        capd AS (   -- fair work time for a whole batch = sum of per-box time over its replenish scans
          SELECT *, sum(CASE WHEN stage='replenish' THEN %s + %s*COALESCE(quantity,0) ELSE 0 END)
                        OVER (PARTITION BY person,d,batch) batch_cap
          FROM bid),
        cred AS (   -- active seconds = sum of credited gaps between consecutive scans.
                    -- Any sub-break gap counts fully (identical to the old session-span model for everyone).
                    -- The ONLY change: a >= 45-min gap that lands right before the FIRST scan of a replenish
                    -- batch is the physical box work (find/cut/place, logged in a burst afterward), so instead
                    -- of discarding it as a break we credit it up to the whole batch's fair time (scaled by the
                    -- units placed across the batch, capped). A break before pick/pack is still a break, and a
                    -- long gap before one small box can never be credited as hours.
          SELECT person, d, sum(CASE
              WHEN gap IS NULL THEN 0
              WHEN stage='replenish' AND bstart=1 AND gap >= %s THEN LEAST(gap, LEAST(%s, batch_cap))
              WHEN gap < %s THEN gap
              ELSE 0 END) active_s
          FROM capd GROUP BY person,d),
        it AS (
          SELECT person, d,
            COALESCE(sum(quantity) FILTER (WHERE stage IN ('pick','pack','engrave')),0) ful,
            COALESCE(sum(quantity) FILTER (WHERE stage='replenish'),0) repl,
            min(ts) first_ts, max(ts) last_ts
          FROM ev GROUP BY person,d)
        SELECT it.person, it.d, EXTRACT(isodow FROM it.d)::int dow,
               COALESCE(sp.active_s,0), it.ful, it.repl, it.first_ts, it.last_ts,
               EXTRACT(epoch FROM (it.last_ts-it.first_ts)) span_s
        FROM it LEFT JOIN cred sp USING (person,d) ORDER BY it.person, it.d""",
        [frm, to, REPL_BURST_WINDOW, REPL_BASE, REPL_PER_UNIT, ACTIVE_BREAK, REPL_BURST_MAX, ACTIVE_BREAK])
        rows = cur.fetchall()
        cur.execute("SELECT id,person,d,hours,note,author FROM floor_note WHERE d BETWEEN %s AND %s "
                    "ORDER BY d, id", [frm, to])
        note_rows = cur.fetchall()
    notes = {}
    for (nid,person,nd,nh,note,author) in note_rows:
        notes.setdefault(person, []).append(dict(id=nid, d=str(nd), hours=float(nh or 0), note=note, author=author or ""))
    ppl = {}
    for (person,d,dow,active_s,ful,repl,first,last,span_s) in rows:
        if person in EXCLUDED: continue
        p = ppl.setdefault(person, dict(person=person, type=PERSON_TYPE.get(person,""),
            active_days=0, active_s=0.0, span_s=0.0, ful=0, repl=0, days=[], _fi=(None,""), _lo=(None,"")))
        active_s=float(active_s or 0); span_s=float(span_s or 0)
        p["active_days"]+=1; p["active_s"]+=active_s; p["span_s"]+=span_s
        p["ful"]+=int(ful or 0); p["repl"]+=int(repl or 0)
        # earliest/latest by TIME OF DAY (ET), not chronologically — "how early do they start, how late finish"
        if first:
            ft=first.astimezone(_ET).time()
            if p["_fi"][0] is None or ft < p["_fi"][0]: p["_fi"]=(ft,_ampm(first))
        if last:
            lt=last.astimezone(_ET).time()
            if p["_lo"][0] is None or lt > p["_lo"][0]: p["_lo"]=(lt,_ampm(last))
        p["days"].append(dict(d=str(d), dow=dow, hours=round(active_s/3600.0,2),
            span=round(span_s/3600.0,2),
            ful=int(ful or 0), repl=int(repl or 0),
            first=_ampm(first), last=_ampm(last),
            util=(round(100*active_s/span_s) if span_s>0 else 0)))
    out=[]
    for p in ppl.values():
        p["first_in"]=p.pop("_fi")[1]; p["last_out"]=p.pop("_lo")[1]
        hrs=p["active_s"]/3600.0; items=p["ful"]+p["repl"]
        p["hours"]=round(hrs,2)
        p["hours_per_day"]=round(hrs/p["active_days"],2) if p["active_days"] else 0
        p["items"]=items; p["ful_items"]=p["ful"]; p["repl_items"]=p["repl"]
        p["items_per_day"]=round(items/p["active_days"]) if p["active_days"] else 0
        p["items_per_hr"]=round(items/hrs) if hrs>0 else 0
        p["util"]=round(100*p["active_s"]/p["span_s"]) if p["span_s"]>0 else 0
        p["avg_span"]=round(p["span_s"]/p["active_days"]/3600.0,1) if p["active_days"] else 0   # typical first->last window/day
        p["span_h"]=round(p["span_s"]/3600.0,1)   # total on-floor (first->last) across days
        nl=notes.get(p["person"],[]); p["notes"]=nl; p["proj_hours"]=round(sum(x["hours"] for x in nl),1)
        del p["active_s"]; del p["span_s"]; del p["ful"]; del p["repl"]
        out.append(p)
    seen={p["person"] for p in out}   # people with ONLY logged project time (no scans) still show up
    for person,nl in notes.items():
        if person in seen or person in EXCLUDED: continue
        out.append(dict(person=person, type=PERSON_TYPE.get(person,""), active_days=0,
            first_in="", last_out="", hours=0, hours_per_day=0, items=0, ful_items=0, repl_items=0,
            items_per_day=0, items_per_hr=0, util=0, avg_span=0, span_h=0, days=[],
            notes=nl, proj_hours=round(sum(x["hours"] for x in nl),1)))
    out.sort(key=lambda x:-(x["hours"]+x.get("proj_hours",0)))
    dl=_daylist(frm,to); work_days=sum(1 for x in dl if x["dow"]<=5)   # weekdays (Mon-Fri) in the window
    return jsonify(range={"from":frm,"to":to}, days=dl, work_days=work_days, people=out)

@app.route("/engraving")
def engraving():
    """Detailed engraving view, from the daily rollup (per engraver per day)."""
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""SELECT person, et_day, scans, totes, matched_totes, dotw, lid, ipe,
                              eng_units, orders, hours
                       FROM contribution_daily WHERE stage='engrave' AND et_day BETWEEN %s AND %s
                         AND person IN (SELECT a.alias FROM person_alias a JOIN employee e ON e.id=a.employee_id)
                       ORDER BY person, et_day""", [frm, to])
        rows = cur.fetchall()
        flags = _flags(cur, frm, to)
    ppl={}
    for (person,d,scans,totes,matched,dotw,lid,ipe,units,orders,hours) in rows:
        if person in EXCLUDED: continue
        if not scans: continue
        p=ppl.setdefault(person, dict(person=person, type=PERSON_TYPE.get(person,""),
            active_days=0, scans=0, totes=0, matched=0, dotw=0, lid=0, ipe=0, items=0, orders=0, hours=0.0, days=[]))
        h=float(hours or 0)
        p["active_days"]+=1; p["scans"]+=int(scans); p["totes"]+=int(totes); p["matched"]+=int(matched or 0)
        p["dotw"]+=int(dotw or 0); p["lid"]+=int(lid or 0); p["ipe"]+=int(ipe or 0)
        p["items"]+=int(units or 0); p["orders"]+=int(orders or 0); p["hours"]+=h
        p["days"].append(dict(d=str(d), dow=d.isoweekday(), totes=int(totes), items=int(units or 0),
            hours=round(h,2), lid=int(lid or 0), ipe=int(ipe or 0), dotw=int(dotw or 0)))
    out=[]
    for p in ppl.values():
        hrs=p["hours"]
        p["hours"]=round(hrs,2)
        p["items_per_hr"]=round(p["items"]/hrs) if hrs>0 else 0
        p["totes_per_hr"]=round(p["totes"]/hrs,1) if hrs>0 else 0
        p["items_per_day"]=round(p["items"]/p["active_days"]) if p["active_days"] else 0
        p["items_per_tote"]=round(p["items"]/p["totes"],2) if p["totes"] else 0
        p["match_rate"]=round(100*p["matched"]/p["totes"]) if p["totes"] else 0
        out.append(p)
    out.sort(key=lambda x:-x["items"])
    return jsonify(range={"from":frm,"to":to}, days=_daylist(frm,to), engravers=out, flags=flags)

# ---------------- Leader annotations (special-project / off-scanner time) ----------------
@app.route("/note", methods=["POST"])
def add_note():
    d = request.get_json(silent=True) or {}
    person=(d.get("person") or "").strip()[:80]
    day=(d.get("date") or "").strip()[:10]
    note=(d.get("note") or "").strip()[:500]
    author=(d.get("author") or "").strip()[:80]
    try: hours=float(d.get("hours") or 0)
    except Exception: hours=0.0
    # If a start+end time-of-day was picked, derive hours from it and stamp the window onto the note.
    start=(d.get("start") or "").strip(); end=(d.get("end") or "").strip()
    if start and end:
        try:
            sh,sm=map(int,start.split(":")); eh,em=map(int,end.split(":"))
            mins=(eh*60+em)-(sh*60+sm)
            if mins>0:
                hours=round(mins/60.0,2)
                rng=_hm_ampm(start)+"–"+_hm_ampm(end)
                note=(rng+("  "+note if note else "")).strip()[:500]
        except Exception: pass
    hours=max(0.0, min(24.0, hours))
    try: dt.date.fromisoformat(day)
    except Exception: return jsonify(ok=False, error="bad date"), 400
    if not person or (hours<=0 and not note):
        return jsonify(ok=False, error="need a person and hours or a note"), 400
    with connect() as c, c.cursor() as cur:
        cur.execute("INSERT INTO floor_note (person,d,hours,note,author) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                    (person, day, hours, note, author)); nid=cur.fetchone()[0]; c.commit()
    return jsonify(ok=True, id=nid)

@app.route("/note/delete", methods=["POST"])
def del_note():
    nid=(request.get_json(silent=True) or {}).get("id")
    if not nid: return jsonify(ok=False), 400
    with connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM floor_note WHERE id=%s", (int(nid),)); c.commit()
    return jsonify(ok=True)

# ---------------- Data flags (read + write) ----------------
@app.route("/dataflags")
def list_flags():
    """Flags for a date range (?from=&to=), or every open flag with no range. ?all=1 includes
    the resolved ones, so the Data Issues tab can show what has already been dealt with."""
    frm=(request.args.get("from") or "").strip(); to=(request.args.get("to") or "").strip()
    allf=(request.args.get("all") or "").lower() in ("1","true","yes")
    if frm and to:
        try: dt.date.fromisoformat(frm); dt.date.fromisoformat(to)
        except Exception: return jsonify(ok=False, error="bad date"), 400
    else:
        frm=to=None
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        return jsonify(ok=True, flags=_flags(cur, frm, to, include_resolved=allf))

@app.route("/dataflag", methods=["POST"])
def add_flag():
    d=request.get_json(silent=True) or {}
    day=(d.get("date") or "").strip()[:10]
    end=((d.get("end") or "").strip() or day)[:10]
    scope=(d.get("scope") or "all").strip().lower()
    sev=(d.get("severity") or "suspect").strip().lower()
    reason=(d.get("reason") or "").strip()[:500]
    author=(d.get("author") or "").strip()[:80]
    try: d0=dt.date.fromisoformat(day); d1=dt.date.fromisoformat(end)
    except Exception: return jsonify(ok=False, error="bad date"), 400
    if d1 < d0: d0, d1 = d1, d0
    if scope not in FLAG_SCOPES: return jsonify(ok=False, error="bad scope"), 400
    if sev not in FLAG_SEVERITIES: return jsonify(ok=False, error="bad severity"), 400
    if not reason: return jsonify(ok=False, error="need a reason"), 400
    with connect() as c, c.cursor() as cur:
        cur.execute("INSERT INTO data_flag (d,d_end,scope,severity,reason,author) "
                    "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id", (d0,d1,scope,sev,reason,author))
        fid=cur.fetchone()[0]; c.commit()
    return jsonify(ok=True, id=fid)

@app.route("/dataflag/resolve", methods=["POST"])
def resolve_flag():
    """Mark a flag dealt with (checked, or backfilled). The flag stays in the table as the
    record that the day was once wrong -- resolving it only stops the warning banner."""
    d=request.get_json(silent=True) or {}
    fid=d.get("id"); res=(d.get("resolution") or "").strip()[:500]
    if not fid: return jsonify(ok=False, error="need an id"), 400
    with connect() as c, c.cursor() as cur:
        cur.execute("UPDATE data_flag SET status='resolved', resolved_at=now(), resolution=%s "
                    "WHERE id=%s", (res, int(fid))); c.commit()
    return jsonify(ok=True)

@app.route("/dataflag/delete", methods=["POST"])
def del_flag():
    fid=(request.get_json(silent=True) or {}).get("id")
    if not fid: return jsonify(ok=False), 400
    with connect() as c, c.cursor() as cur:
        cur.execute("DELETE FROM data_flag WHERE id=%s", (int(fid),)); c.commit()
    return jsonify(ok=True)

GAP_SHOW = 1800   # seconds = 30 min: gaps this long or longer are surfaced as fillable windows
@app.route("/person_day")
def person_day():
    """One person, one ET day: their scan schedule broken into work blocks and the gaps between
    them, so a leader can SEE the empty windows and log off-scanner time straight into a gap.
    Active hours use the same 45-min-break rule as everywhere else; span = first->last scan."""
    person=(request.args.get("person") or "").strip()
    day=(request.args.get("d") or "").strip()[:10]
    try: dt.date.fromisoformat(day)
    except Exception: return jsonify(ok=False, error="bad date"), 400
    if not person or person in EXCLUDED: return jsonify(ok=False, error="unknown person"), 400
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""SELECT ts FROM event_canon
                       WHERE person=%s AND is_floor_labor(stage,subtype) AND et_day(ts)=%s
                       ORDER BY ts""", [person, day])
        ts=[r[0] for r in cur.fetchall()]
        cur.execute("SELECT id,hours,note,author FROM floor_note WHERE person=%s AND d=%s ORDER BY id",[person,day])
        notes=[dict(id=r[0],hours=float(r[1] or 0),note=r[2] or "",author=r[3] or "") for r in cur.fetchall()]
    if not ts:
        return jsonify(ok=True, person=person, d=day, scans=0, first="", last="",
                       active_h=0, span_h=0, timeline=[], notes=notes)
    first,last=ts[0],ts[-1]
    blocks=[]; gaps=[]; s_start=ts[0]; prev=ts[0]
    for cur_ts in ts[1:]:
        g=(cur_ts-prev).total_seconds()
        if g>=GAP_SHOW:
            blocks.append((s_start,prev)); gaps.append((prev,cur_ts,g,g>=ACTIVE_BREAK)); s_start=cur_ts
        prev=cur_ts
    blocks.append((s_start,prev))
    span_s=(last-first).total_seconds()
    active_s=span_s-sum(g for (_,_,g,brk) in gaps if brk)   # remove only 45-min+ breaks (matches /floor)
    tl=[]
    for i,(s,e) in enumerate(blocks):
        tl.append(dict(kind="work", start=_hm(s), end=_hm(e), start_l=_ampm(s), end_l=_ampm(e),
                       mins=round((e-s).total_seconds()/60)))
        if i < len(gaps):
            gs,ge,g,brk=gaps[i]
            tl.append(dict(kind="gap", start=_hm(gs), end=_hm(ge), start_l=_ampm(gs), end_l=_ampm(ge),
                           mins=round(g/60), brk=brk))
    return jsonify(ok=True, person=person, d=day, scans=len(ts),
                   first=_ampm(first), last=_ampm(last),
                   active_h=round(active_s/3600,2), span_h=round(span_s/3600,2),
                   timeline=tl, notes=notes)

# ---------------- Speed & Rankings ----------------
# How fast each person works at each activity, so the right people get assigned to the right task.
# ACTIVE HOURS (one clear definition, same as Floor Time & engraving hours): from a person's FIRST scan of
# a task to their LAST, with any gap of 45+ minutes removed as a break (lunch / switched task / stepped
# away). Equivalently: sum of the gaps between consecutive scans that are UNDER 45 min. Gaps under 45 min
# DO count as active time, so genuinely slow stretches count against the rate, but time away never does.
# Speed = units done in that active time ÷ active hours. Near-simultaneous scans (<=5s) merge into one
# "chunk" first (fixes replenishment bulk pallet scans stamped at the same second).
SPEED_STAGES = ["pick", "pack", "engrave", "replenish", "pick_mgn", "pick_norm"]
SPEED_BREAK  = 2700   # seconds = 45 min: a gap this long or longer splits active time (a break)
SPEED_SRC  = {"pick":"shiphero","pack":"shiphero","replenish":"shiphero","engrave":"logger",
              "pick_mgn":"shiphero","pick_norm":"shiphero"}
SPEED_BURST = 5          # scans within this many seconds = one physical action (chunk)
SPEED_GATE = {"min_intervals":30, "min_days":2, "min_active_min":15}  # ranked only if all three met
SPEED_UNIT = {"pick":"items","pack":"items","engrave":"totes","replenish":"boxes",
              "pick_mgn":"items","pick_norm":"items"}
# Rate mode: "units" = units per active hour (pick/pack/engrave). "moves" = discrete actions per active
# hour — replenish is ranked as BOXES/hr, because a 40-unit box isn't 40x the work of a 1-unit move, so
# units/hr would just rank box size, not speed.
SPEED_RATE = {"pick":"units","pack":"units","engrave":"units","replenish":"moves",
              "pick_mgn":"units","pick_norm":"units"}

@app.route("/speed")
def speed():
    """Per person x activity PACE (typical time per item) + throughput, for the window.

    ACCURATE by construction:
    (1) Built on each person's WHOLE scan timeline, so a gap counts toward an activity only when the
        scan before it was the SAME activity — switching tasks is never counted as active time for
        another task (that was the old bug that tanked multitaskers).
    (2) Ranked by the MEDIAN gap between consecutive same-activity scans → the typical time to do one
        item. The median ignores pauses, breaks and one-off slow items, so the number is stable and
        reflects real pace. (Throughput per active hour is shown alongside for context.)
    """
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH base AS (   -- tote-moves are a separate pseudo-activity so they don't count as box moves,
                         -- but still break a pick/pack bout (a real task switch). mgn flags a MagNano-case
                         -- pick (SPC-MGN) so picking can be split into MagNano vs normal without disturbing
                         -- the shared timeline (task-switch handling) or the overall 'pick' the planner uses.
          SELECT person,
            CASE WHEN stage='replenish' AND ((raw->>'reason') ILIKE '%%tote%%' OR coalesce(quantity,0)<=1)
                 THEN 'move_tote' ELSE stage END AS estage,
            (stage='pick' AND left(sku,7)='SPC-MGN') AS mgn, quantity, ts
          FROM event_canon
          WHERE ((source='shiphero' AND stage IN ('pick','pack','replenish'))
                 OR (source='logger' AND stage='engrave'))
            AND et_day(ts) BETWEEN %s AND %s),
        b2 AS (SELECT person, estage, mgn, quantity, ts,
            EXTRACT(epoch FROM (ts - lag(ts) OVER (PARTITION BY person,estage ORDER BY ts))) sg FROM base),
        chunked AS (SELECT person, estage, mgn, quantity, ts,   -- collapse <=5s bursts into one action
            sum(CASE WHEN sg IS NULL OR sg > %s THEN 1 ELSE 0 END)
                OVER (PARTITION BY person,estage ORDER BY ts) cid FROM b2),
        chunks AS (SELECT person, estage, bool_or(mgn) mgn, sum(quantity) units, min(ts) ts
                   FROM chunked GROUP BY person,estage,cid),
        tl AS (SELECT person, estage, mgn, units, ts,   -- cross-activity timeline
            EXTRACT(epoch FROM (ts - lag(ts) OVER (PARTITION BY person ORDER BY ts))) gap,
            lag(estage) OVER (PARTITION BY person ORDER BY ts) pstage,
            (ts AT TIME ZONE 'America/New_York')::date d FROM chunks),
        iv AS (SELECT person, estage AS stage, mgn, units, gap, d FROM tl
               WHERE pstage=estage AND gap>0 AND gap<%s)   -- continuous same activity, under the break
        SELECT person, stage, count(*) n, count(DISTINCT d) days,
          round(sum(gap)/60.0,1) active_min, sum(units) units,
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap/nullif(units,0))::numeric,1) med_spi,
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)::numeric,1) med_move
        FROM iv WHERE stage IN ('pick','pack','engrave','replenish')
        GROUP BY person, stage
        UNION ALL   -- MagNano-only pick pace (same intervals, shared timeline)
        SELECT person, 'pick_mgn', count(*), count(DISTINCT d), round(sum(gap)/60.0,1), sum(units),
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap/nullif(units,0))::numeric,1),
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)::numeric,1)
        FROM iv WHERE stage='pick' AND mgn GROUP BY person
        UNION ALL   -- everything-else pick pace
        SELECT person, 'pick_norm', count(*), count(DISTINCT d), round(sum(gap)/60.0,1), sum(units),
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap/nullif(units,0))::numeric,1),
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)::numeric,1)
        FROM iv WHERE stage='pick' AND NOT mgn GROUP BY person""", [frm, to, SPEED_BURST, SPEED_BREAK])
        rows = cur.fetchall()
    rows_by_stage = {s: [] for s in SPEED_STAGES}
    for (person, stage, n, days, amin, units, med_spi, med_move) in rows:
        if not n or person in EXCLUDED: continue
        n=int(n); days=int(days or 0); amin=float(amin or 0); units=int(units or 0)
        med_spi=float(med_spi) if med_spi is not None else None
        med_move=float(med_move) if med_move is not None else None
        ranked = (n>=SPEED_GATE["min_intervals"] and (days or 0)>=SPEED_GATE["min_days"]
                  and (amin or 0)>=SPEED_GATE["min_active_min"])
        reason=""
        if not ranked:
            bits=[]
            if n<SPEED_GATE["min_intervals"]: bits.append(f"only {n} timed unit"+("s" if n!=1 else ""))
            if (days or 0)<SPEED_GATE["min_days"]: bits.append(f"only {days or 0} day"+("s" if (days or 0)!=1 else ""))
            if (amin or 0)<SPEED_GATE["min_active_min"]: bits.append(f"only {amin or 0} active min")
            reason=", ".join(bits)
        active_s = float(amin)*60.0 if amin else 0
        if SPEED_RATE[stage]=="moves":     # boxes: pace from median gap between box moves
            med = float(med_move) if med_move is not None else None
            throughput = round(3600.0*n/active_s) if active_s else 0
        else:                              # items: pace from median sec/item
            med = float(med_spi) if med_spi is not None else None
            throughput = round(3600.0*(units or 0)/active_s) if active_s else 0
        pace = round(3600.0/med) if med else 0
        rows_by_stage[stage].append(dict(person=person, type=PERSON_TYPE.get(person,""),
            pace=pace, throughput=throughput, uph=pace,   # uph=pace so the board ranks by typical pace
            med_spi=(round(med) if med else None), n=int(n),
            active_min=float(amin) if amin is not None else 0.0,
            days=int(days or 0), units=int(units or 0), moves=int(n), ranked=ranked, reason=reason))
    cfg=dict(break_min=SPEED_BREAK//60, burst_s=SPEED_BURST,
             gate=SPEED_GATE, unit=SPEED_UNIT, source=SPEED_SRC, rate=SPEED_RATE)
    return jsonify(range={"from":frm,"to":to}, config=cfg, stages=rows_by_stage)

@app.route("/trend")
def trend():
    """One person's PACE trend over time. Same robust median-sec-per-item method as /speed, but bucketed
    by ISO week per activity, so you can see whether someone is getting faster (a new hire ramping, say).
    pace = items/hr from the median gap between consecutive same-activity chunks; units = throughput volume.
    Weeks with too few timed items show volume only (pace null)."""
    person=(request.args.get("person") or "").strip()
    if not person or person in EXCLUDED: return jsonify(ok=False, error="unknown person"), 400
    gran = "day" if (request.args.get("gran") == "day") else "week"
    to = request.args.get("to") or (dt.datetime.now(_ET).date()).isoformat()
    if gran == "day":                   # per-DAY buckets: see a person improve within a week
        try: nd = max(5, min(45, int(request.args.get("days") or 14)))
        except Exception: nd = 14
        frm = request.args.get("from") or (dt.date.fromisoformat(to) - dt.timedelta(days=nd-1)).isoformat()
        MIN_N = 4                        # min timed items in a DAY to trust that day's pace
        bucket = "((ts AT TIME ZONE 'America/New_York')::date)"
    else:                               # per-ISO-week buckets (default)
        try: wks=max(2, min(26, int(request.args.get("weeks") or 8)))
        except Exception: wks=8
        frm = request.args.get("from") or (dt.date.fromisoformat(to) - dt.timedelta(days=wks*7-1)).isoformat()
        MIN_N = 8                        # min timed items in a WEEK to trust that week's pace
        bucket = "(date_trunc('week',(ts AT TIME ZONE 'America/New_York'))::date)"
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute(("""
        WITH base AS (
          SELECT CASE WHEN stage='replenish' AND ((raw->>'reason') ILIKE '%%tote%%' OR coalesce(quantity,0)<=1)
                      THEN 'move_tote' ELSE stage END AS estage, quantity, ts
          FROM event_canon
          WHERE person=%s AND ((source='shiphero' AND stage IN ('pick','pack','replenish'))
                               OR (source='logger' AND stage='engrave'))
            AND et_day(ts) BETWEEN %s AND %s),
        b2 AS (SELECT estage, quantity, ts,
            EXTRACT(epoch FROM (ts - lag(ts) OVER (PARTITION BY estage ORDER BY ts))) sg FROM base),
        chunked AS (SELECT estage, quantity, ts,
            sum(CASE WHEN sg IS NULL OR sg > %s THEN 1 ELSE 0 END) OVER (PARTITION BY estage ORDER BY ts) cid FROM b2),
        chunks AS (SELECT estage, sum(quantity) units, min(ts) ts FROM chunked GROUP BY estage,cid),
        tl AS (SELECT estage, units, ts,
            EXTRACT(epoch FROM (ts - lag(ts) OVER (ORDER BY ts))) gap,
            lag(estage) OVER (ORDER BY ts) pstage FROM chunks),
        iv AS (SELECT estage AS stage, units, gap,
            {BUCKET} wk
            FROM tl WHERE pstage=estage AND gap>0 AND gap<%s)
        SELECT wk, stage, count(*) n, sum(units) units,
          round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap/nullif(units,0))::numeric,1) med_spi
        FROM iv WHERE stage IN ('pick','pack','engrave')
        GROUP BY wk, stage ORDER BY wk""").replace("{BUCKET}", bucket), [person, frm, to, SPEED_BURST, SPEED_BREAK])
        rows = cur.fetchall()
    wkmap = {}
    for (wk, stage, n, units, med_spi) in rows:
        w = wkmap.setdefault(str(wk), {"wk": str(wk)})
        n=int(n or 0); units=int(units or 0); med=float(med_spi) if med_spi is not None else None
        uph = round(3600.0/med) if (med and n>=MIN_N) else None
        w[stage] = dict(uph=uph, units=units, n=n)
    weeks = [wkmap[k] for k in sorted(wkmap.keys())]
    return jsonify(ok=True, person=person, gran=gran, range={"from":frm,"to":to}, min_n=MIN_N, weeks=weeks)

# ---------------- Watch List (metric-based flags) ----------------
# A single metric always lies: pace ignores whether you showed up; hours ignore whether you worked.
# So this looks at pace + hours + UTILIZATION (active/floor) + output + attendance + consistency together,
# and raises specific, evidence-bearing flags. It is a lead, not a verdict (a low number can be legit —
# e.g. waiting on restock). Scan-only for now; scheduled-shift adherence is a planned add-on.
WATCH_IDLE = 2700  # 45 min: same active-time rule as Floor Time / Speed (a gap this long = a break)
# Standard: everyone is expected to work 50h/week = 10h/day x 5 days (can be split up).
WATCH = {"util_low":50, "min_floor_hr":6, "target_day_hr":10, "target_days_wk":5, "short_day_hr":7,
         "pace_hi_pct":67, "out_lo_pct":33, "out_bottom_pct":25, "incon_ratio":2.5}
# Engravers are shown in a SEPARATE, un-flagged group for now: engraving time isn't cleanly tracked,
# so their utilization / hours / output read artificially low and shouldn't be flagged yet.
WATCH_ENGRAVERS = {"Manu Bekele","Maurice Williams","Halil Gurler"}

@app.route("/watch")
def watch():
    frm, to = _range()
    try:   # expected work-days = actual weekdays (Mon-Fri) in the window; 10h each => 50h in a normal week
        d0=dt.date.fromisoformat(frm); d1=dt.date.fromisoformat(to)
        exp_days=max(1, sum(1 for i in range((d1-d0).days+1) if (d0+dt.timedelta(days=i)).isoweekday()<=5))
    except Exception:
        exp_days=5
    exp_hours=WATCH["target_day_hr"]*exp_days                       # 10h per weekday
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH ev AS (
          SELECT person, ts, quantity, stage,
            EXTRACT(epoch FROM (ts - lag(ts) OVER (PARTITION BY person,
                  (ts AT TIME ZONE 'America/New_York')::date ORDER BY ts))) gap
          FROM event_canon
          WHERE ((source='shiphero' AND stage IN ('pick','pack','replenish'))
                 OR (source='logger' AND stage='engrave'))
            AND et_day(ts) BETWEEN %s AND %s),
        perday AS (
          SELECT person, (ts AT TIME ZONE 'America/New_York')::date d,
            EXTRACT(epoch FROM (max(ts)-min(ts))) span_sec,
            sum(CASE WHEN gap>0 AND gap<=%s THEN gap ELSE 0 END) active_sec,
            sum(CASE WHEN stage IN ('pick','pack','engrave') THEN quantity ELSE 0 END) outp
          FROM ev GROUP BY person,(ts AT TIME ZONE 'America/New_York')::date)
        SELECT person, count(*) days, sum(span_sec) span_sec, sum(active_sec) active_sec,
          sum(outp) output, percentile_cont(0.5) WITHIN GROUP (ORDER BY outp) med_day, max(outp) best_day
        FROM perday GROUP BY person""", [frm, to, WATCH_IDLE])
        rows = cur.fetchall()
    ppl = []
    for (person,days,span,active,output,med,best) in rows:
        if person in EXCLUDED: continue
        span=float(span or 0); active=float(active or 0); output=int(output or 0); days=int(days)
        ppl.append(dict(person=person, type=PERSON_TYPE.get(person,""), days=days,
            floor_hr=round(span/3600,1), active_hr=round(active/3600,1),
            util=(round(100*active/span) if span>0 else 0),
            avg_span=(round(span/days/3600,1) if days else 0),
            output=output, pace=(round(output/(active/3600)) if active>0 else 0),
            med_day=round(float(med or 0)), best_day=round(float(best or 0))))
    cohort=[p for p in ppl if p["person"] not in WATCH_ENGRAVERS and p["days"]>=2 and p["active_hr"]>=1]
    def pct(vals,v):
        s=sorted(vals)
        if len(s)<=1: return 100
        return round(100*sum(1 for x in s if x<v)/(len(s)-1))
    paces=[p["pace"] for p in cohort]; outs=[p["output"] for p in cohort]
    for p in ppl:
        eng = p["person"] in WATCH_ENGRAVERS
        inc = p in cohort
        p["engraver"]=eng; p["cohort"]=inc
        p["pace_pct"]=pct(paces,p["pace"]) if inc else None
        p["out_pct"]=pct(outs,p["output"]) if inc else None
        f=[]
        ftr = (p["type"]=="FT")   # hours/attendance flags apply to FULL-TIMERS only (interns are part-time by design)
        if not eng:   # engravers exempt for now (engraving time not cleanly tracked)
            if p["floor_hr"]>=WATCH["min_floor_hr"] and p["util"]<WATCH["util_low"]:
                f.append(dict(t="Bursty / idle", d=f"on floor {p['floor_hr']}h but active only {p['active_hr']}h ({p['util']}%)", sev="r"))
            if ftr and p["floor_hr"] < 0.7*exp_hours and (p["days"]>=2 or exp_days<=2):
                f.append(dict(t="Under hours", d=f"~{p['floor_hr']}h on floor vs ~{exp_hours}h target (50h/wk) — verify vs PTO", sev="r"))
            if ftr and p["days"]>=2 and p["avg_span"]<WATCH["short_day_hr"]:
                f.append(dict(t="Short shifts", d=f"averages {p['avg_span']}h/day vs 10h target", sev="r"))
            if ftr and exp_days>=3 and p["days"] < exp_days-1:
                f.append(dict(t="Missed days", d=f"present {p['days']} of ~{exp_days} expected days — check PTO app", sev="r"))
            if ftr and inc and p["out_pct"]<=WATCH["out_bottom_pct"] and p["floor_hr"]>=WATCH["min_floor_hr"]:
                f.append(dict(t="Low output", d=f"{p['output']} items — bottom {WATCH['out_bottom_pct']}% despite {p['floor_hr']}h on floor", sev="r"))
            if ftr and inc and p["pace_pct"]>=WATCH["pace_hi_pct"] and p["out_pct"]<=WATCH["out_lo_pct"]:
                f.append(dict(t="Fast but low total", d=f"top-tier pace but low total output ({p['output']})", sev="a"))
            if ftr and p["days"]>=3 and p["med_day"]>0 and p["best_day"]>=WATCH["incon_ratio"]*p["med_day"]:
                f.append(dict(t="Inconsistent", d=f"best day {p['best_day']} vs typical {p['med_day']}/day", sev="a"))
        p["flags"]=f
    hard=lambda p:sum(1 for x in p["flags"] if x["sev"]=="r")
    ppl.sort(key=lambda p:(0 if p["flags"] else 1, -hard(p), -len(p["flags"]), p["util"]))
    return jsonify(range={"from":frm,"to":to},
        config=dict(idle_min=WATCH_IDLE//60, exp_days=exp_days, exp_hours=exp_hours,
                    engravers=sorted(WATCH_ENGRAVERS), **WATCH), people=ppl)

@app.route("/outstanding")
def outstanding():
    """The demand side: the current outstanding (not-yet-shipped) order backlog from ShipHero,
    aged and valued — what we owe. Reads the open_order snapshot (refreshed by orders_snapshot.py)."""
    AGE_LABELS = ["under 1 day","1–2 days","2–3 days","3–5 days","5–7 days","7+ days"]
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT to_regclass('public.open_order')")
        if cur.fetchone()[0] is None:
            return jsonify(ready=False, orders=0)
        cur.execute("SELECT count(*), COALESCE(sum(total_price),0), COALESCE(sum(items_open),0), "
                    "count(*) FILTER (WHERE on_hold), COALESCE(sum(total_price) FILTER (WHERE on_hold),0), "
                    "max(snapshot_at), "
                    "COALESCE(avg(EXTRACT(epoch FROM (now()-order_date))/86400.0),0), "
                    "COALESCE(percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(epoch FROM (now()-order_date))/86400.0),0), "
                    "COALESCE(max(EXTRACT(epoch FROM (now()-order_date))/86400.0),0) FROM open_order")
        n, val, items, nhold, valhold, snap, avg_age, med_age, max_age = cur.fetchone()
        cur.execute("""
          WITH a AS (SELECT total_price, items_open,
              EXTRACT(epoch FROM (now()-order_date))/86400.0 age FROM open_order WHERE order_date IS NOT NULL)
          SELECT CASE WHEN age<1 THEN 0 WHEN age<2 THEN 1 WHEN age<3 THEN 2 WHEN age<5 THEN 3 WHEN age<7 THEN 4 ELSE 5 END b,
                 count(*), COALESCE(sum(total_price),0), COALESCE(sum(items_open),0)
          FROM a GROUP BY b""")
        bk = {int(b): (int(cnt), float(v or 0), int(it or 0)) for b, cnt, v, it in cur.fetchall()}
        cur.execute("""SELECT order_number, order_date, total_price, fulfillment_status, on_hold, hold_reason,
                          items_open, EXTRACT(epoch FROM (now()-order_date))/86400.0 age
                       FROM open_order ORDER BY order_date ASC NULLS LAST LIMIT 50""")
        oldest = [dict(order=r[0], value=float(r[2] or 0), status=r[3], on_hold=bool(r[4]),
                       hold=r[5], items=int(r[6] or 0), age_days=round(float(r[7] or 0),1)) for r in cur.fetchall()]
        cur.execute("""SELECT order_number, order_date, total_price, hold_reason,
                          EXTRACT(epoch FROM (now()-order_date))/86400.0 age
                       FROM open_order WHERE on_hold ORDER BY order_date ASC NULLS LAST LIMIT 60""")
        holds = [dict(order=r[0], value=float(r[2] or 0), hold=r[3], age_days=round(float(r[4] or 0),1))
                 for r in cur.fetchall()]
        cur.execute("SELECT COALESCE(fulfillment_status,'unknown'), count(*), COALESCE(sum(total_price),0) "
                    "FROM open_order GROUP BY 1 ORDER BY 2 DESC")
        status = [dict(status=r[0], count=int(r[1]), value=float(r[2] or 0)) for r in cur.fetchall()]
    aging = [dict(label=AGE_LABELS[i], count=bk.get(i,(0,0,0))[0], value=bk.get(i,(0,0,0))[1],
                  items=bk.get(i,(0,0,0))[2], aged=(i>=3)) for i in range(6)]
    aged_n = sum(a["count"] for a in aging if a["aged"]); aged_v = sum(a["value"] for a in aging if a["aged"])
    return jsonify(ready=True, orders=int(n or 0), value=float(val or 0), items=int(items or 0),
                   avg_value=(float(val or 0)/n if n else 0), avg_age=round(float(avg_age or 0),1),
                   median_age=round(float(med_age or 0),1), oldest_age=round(float(max_age or 0),1),
                   on_hold=int(nhold or 0), on_hold_value=float(valhold or 0), on_hold_orders=holds,
                   aged=aged_n, aged_value=aged_v,
                   snapshot_at=(snap.isoformat() if snap else None), aging=aging, oldest=oldest, status=status)

@app.route("/dataqc")
def dataqc():
    """Data-integrity guardrails so a weird number gets questioned before it drives a decision:
      (1) DOUBLE-SCAN / two machines on one login — a person's scans jumping between DIFFERENT
          orders within seconds (physically impossible for one human); near-zero is clean.
      (2) IN-PROGRESS day — today's numbers are partial, so a low 'today' isn't a collapse.
      (3) ANOMALIES — a completed day far off a person's own recent baseline, flagged for review.
      (4) FRESHNESS — how long since the last scan landed."""
    import statistics
    now = dt.datetime.now(_ET); today = now.date()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT max(ts) FROM event"); last_ts = cur.fetchone()[0]
        cur.execute("""WITH s AS (SELECT person, order_number,
              EXTRACT(epoch FROM (ts-lag(ts) OVER w)) g,
              order_number IS DISTINCT FROM lag(order_number) OVER w diff
            FROM event_canon WHERE stage IN ('pick','pack') AND person <> ALL(%s) AND et_day(ts)>=%s
            WINDOW w AS (PARTITION BY person ORDER BY ts))
          SELECT person, count(*) FILTER (WHERE g>=0 AND g<3 AND diff) jump,
            count(*) FILTER (WHERE g>=0 AND g<1) sub1, count(*) tot
          FROM s GROUP BY person HAVING count(*)>50 ORDER BY jump DESC, sub1 DESC""",
          [list(EXCLUDED), str(today-dt.timedelta(days=30))])
        conc = [dict(person=r[0], jump=int(r[1]), sub1=int(r[2]), tot=int(r[3])) for r in cur.fetchall()]
        cur.execute("""SELECT person, et_day(ts) d,
            COALESCE(sum(quantity) FILTER (WHERE stage IN ('pick','pack','engrave')),0) ful, max(ts) l
          FROM event_canon WHERE person <> ALL(%s) AND is_floor_labor(stage,subtype) AND et_day(ts)>=%s
          GROUP BY person, et_day(ts)""", [list(EXCLUDED), str(today-dt.timedelta(days=21))])
        by = {}
        for (p, d, ful, l) in cur.fetchall():
            by.setdefault(p, []).append((d, int(ful), l))
        unidentified = _unmatched(cur)
        flags = _flags(cur, include_resolved=True)
    anomalies = []; today_rows = []
    for p, rows in by.items():
        rows.sort(key=lambda x: x[0])
        comp = [r for r in rows if r[0] != today and r[1] > 0]          # completed active days
        med = statistics.median([r[1] for r in comp]) if comp else 0
        tr = [r for r in rows if r[0] == today]
        if tr:
            today_rows.append(dict(person=p, today=tr[0][1], median=round(med),
                pct=(round(100*tr[0][1]/med) if med > 0 else None), last=_ampm(tr[0][2])))
        if len(comp) >= 4:
            last_d = comp[-1]; base = statistics.median([r[1] for r in comp[:-1]])
            if base > 0:
                pct = round(100*last_d[1]/base)
                if pct < 50 or pct > 200:
                    anomalies.append(dict(person=p, d=str(last_d[0]), ful=last_d[1],
                        base=round(base), pct=pct, kind=("drop" if pct < 50 else "spike")))
    anomalies.sort(key=lambda a: a["pct"])
    today_rows.sort(key=lambda r: (r["pct"] if r["pct"] is not None else 999))
    dow = today.isoweekday()
    return jsonify(now=now.isoformat(), today=str(today), today_dow=dow,
        today_hm=now.strftime("%-I:%M %p"), is_weekend=(dow >= 6),
        last_ts=(last_ts.isoformat() if last_ts else None),
        last_min_ago=(round((now - last_ts.astimezone(_ET)).total_seconds()/60) if last_ts else None),
        concurrency=conc, anomalies=anomalies, today_partial=today_rows,
        unidentified=unidentified, flags=flags, flag_scopes=list(FLAG_SCOPES),
        flag_severities=list(FLAG_SEVERITIES))

@app.route("/teamdaily")
def teamdaily():
    """Team-wide per-ET-day totals for the window: items picked / packed / engraved /
    replenished, distinct fulfillment orders, and distinct people — one row per working
    day. Fast simple aggregate (no window functions) so it stays snappy on free tier."""
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        # Pack + shipped use the SAME rule as the main dashboard: ShipHero + Logger, plus
        # pre-Logger-cutover (2026-07-21) Shopify as the historical hand-pack proxy. Shipped
        # orders keyed on the normalized order number (Logger omits the leading '#').
        cur.execute("""
        SELECT to_char(et_day(ts),'YYYY-MM-DD') AS d,
               COALESCE(SUM(quantity) FILTER (WHERE stage='pick'),0)                                   AS picks,
               COALESCE(SUM(quantity) FILTER (WHERE stage='pack' AND source='shiphero'),0)             AS packsh,
               COALESCE(SUM(quantity) FILTER (WHERE stage='pack' AND (source='logger' OR (source='shopify' AND et_day(ts) < DATE '2026-07-21'))),0) AS packhand,
               COALESCE(SUM(quantity) FILTER (WHERE stage='engrave'),0)                                AS engraves,
               COALESCE(SUM(quantity) FILTER (WHERE stage='replenish'),0)                              AS restocks,
               COUNT(DISTINCT ltrim(order_number,'#')) FILTER (WHERE stage='pack' AND (source='shiphero' OR source='logger' OR (source='shopify' AND et_day(ts) < DATE '2026-07-21'))) AS shipped,
               COUNT(DISTINCT person) AS people
        FROM event_canon
        WHERE et_day(ts) BETWEEN %s AND %s
        GROUP BY et_day(ts)
        ORDER BY et_day(ts)
        """, (frm, to))
        rows = cur.fetchall()
        # Team active clocked hours per day (breaks removed); time clock is the source of
        # truth. Reliably populated only from 2026-07-22 on — earlier days have no rows.
        cur.execute("""
        WITH sh AS (
          SELECT tc.id, et_day(tc.clock_in) AS d,
                 EXTRACT(EPOCH FROM (COALESCE(tc.clock_out, now()) - tc.clock_in)) AS total_s,
                 COALESCE((SELECT SUM(EXTRACT(EPOCH FROM (COALESCE(tb.end_ts, now()) - tb.start_ts)))
                           FROM time_break tb WHERE tb.shift_id = tc.id), 0) AS break_s
          FROM time_clock tc)
        SELECT to_char(d,'YYYY-MM-DD') AS d, SUM(total_s - break_s)/3600.0 AS active_h
        FROM sh WHERE d BETWEEN %s AND %s GROUP BY d
        """, (frm, to))
        active = {r[0]: round(float(r[1] or 0), 1) for r in cur.fetchall()}
    days = []
    tot = {"picks": 0, "packsh": 0, "packhand": 0, "packs": 0, "engraves": 0,
           "restocks": 0, "ful": 0, "shipped": 0, "orders": 0, "active_h": 0.0}
    for (d, picks, packsh, packhand, engraves, restocks, shipped, people) in rows:
        picks = int(picks or 0); packsh = int(packsh or 0); packhand = int(packhand or 0)
        engraves = int(engraves or 0); restocks = int(restocks or 0); shipped = int(shipped or 0)
        packs = packsh + packhand
        ful = picks + packs + engraves
        days.append({"d": d, "picks": picks, "packsh": packsh, "packhand": packhand,
                     "packs": packs, "engraves": engraves, "restocks": restocks, "ful": ful,
                     "shipped": shipped, "orders": shipped, "active_h": active.get(d),
                     "people": int(people or 0)})
        tot["picks"] += picks; tot["packsh"] += packsh; tot["packhand"] += packhand
        tot["packs"] += packs; tot["engraves"] += engraves; tot["restocks"] += restocks
        tot["ful"] += ful; tot["shipped"] += shipped; tot["active_h"] += active.get(d) or 0
    tot["orders"] = tot["shipped"]
    tot["active_h"] = round(tot["active_h"], 1)
    return jsonify({"range": {"from": frm, "to": to}, "days": days, "totals": tot})


@app.route("/hours")
def hours():
    """Real clocked hours from the time clock (source of truth) for the window, per
    person: TOTAL (clock-in to clock-out span), BREAK (lunch + short breaks), and
    ACTIVE (total minus break) hours, with a per-ET-day breakdown incl. shift in/out
    times. Open shifts count up to now(); a shift is attributed to the ET day it
    started. Names already match the contribution data (canonical roster names)."""
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH sh AS (
          SELECT tc.id, tc.person, tc.clock_in, tc.clock_out,
                 et_day(tc.clock_in) AS d,
                 EXTRACT(EPOCH FROM (COALESCE(tc.clock_out, now()) - tc.clock_in)) AS total_s,
                 COALESCE((SELECT SUM(EXTRACT(EPOCH FROM (COALESCE(tb.end_ts, now()) - tb.start_ts)))
                           FROM time_break tb WHERE tb.shift_id = tc.id), 0) AS break_s,
                 (tc.clock_out IS NULL) AS is_open,
                 EXISTS(SELECT 1 FROM time_break tb WHERE tb.shift_id = tc.id
                        AND tb.end_ts IS NULL) AS on_break
          FROM time_clock tc
        )
        SELECT person, d,
               SUM(total_s) AS total_s, SUM(break_s) AS break_s,
               MIN(clock_in) AS first_in, MAX(clock_out) AS last_out,
               bool_or(clock_out IS NULL) AS day_open,
               count(*) AS shifts,
               bool_or(is_open) AS is_open, bool_or(on_break) AS on_break
        FROM sh
        WHERE d BETWEEN %s AND %s
        GROUP BY person, d
        ORDER BY person, d
        """, (frm, to))
        rows = cur.fetchall()
    people = {}
    for (person, d, total_s, break_s, first_in, last_out,
         day_open, shifts, is_open, on_break) in rows:
        total_s = float(total_s or 0)
        break_s = float(break_s or 0)
        active_s = total_s - break_s
        if active_s < 0:
            active_s = 0.0
        p = people.get(person)
        if not p:
            p = people[person] = {"person": person, "total_h": 0.0, "active_h": 0.0,
                                  "break_h": 0.0, "days": 0, "open": False,
                                  "on_break": False, "days_detail": []}
        p["total_h"]  += total_s / 3600.0
        p["active_h"] += active_s / 3600.0
        p["break_h"]  += break_s / 3600.0
        p["days"]     += 1
        p["open"]      = p["open"] or bool(is_open)
        p["on_break"]  = p["on_break"] or bool(on_break)
        p["days_detail"].append({
            "d": d.isoformat(),
            "total_h": round(total_s / 3600.0, 2),
            "active_h": round(active_s / 3600.0, 2),
            "break_h": round(break_s / 3600.0, 2),
            "shifts": int(shifts),
            "first_in": _ampm(first_in),
            "last_out": ("" if day_open else _ampm(last_out)),
            "open": bool(is_open),
        })
    out = []
    for p in people.values():
        p["total_h"]  = round(p["total_h"], 2)
        p["active_h"] = round(p["active_h"], 2)
        p["break_h"]  = round(p["break_h"], 2)
        out.append(p)
    out.sort(key=lambda r: r["active_h"], reverse=True)
    return jsonify({"range": {"from": frm, "to": to}, "people": out})


@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")

DASHBOARD_HTML = open(os.path.join(os.path.dirname(__file__), "dashboard.html"), encoding="utf-8").read()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
