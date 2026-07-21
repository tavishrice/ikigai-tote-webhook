"""
Read API + dashboard for the Ikigai contribution store.
Serves the "Warehouse Picking & Packing" dashboard (Dashboard / Floor Time / Analytics)
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
          COALESCE(sum(quantity) FILTER (WHERE stage='pack' AND source='shopify'),0)              packshop_items,
          count(DISTINCT order_number) FILTER (WHERE stage='pack' AND source='shopify')           packshop_orders,
          COALESCE(sum(quantity) FILTER (WHERE stage='replenish'),0)                              repl_units,
          COALESCE(sum(quantity) FILTER (WHERE stage='engrave'),0)                                 eng_items,
          count(DISTINCT order_number) FILTER (WHERE stage='engrave')                             eng_orders,
          count(*) FILTER (WHERE stage='pick')                                pick_cnt,
          count(*) FILTER (WHERE stage='pack' AND source='shiphero')          pack_cnt,
          count(*) FILTER (WHERE stage='pack' AND source='shopify')           fulfill_cnt,
          count(*) FILTER (WHERE stage='replenish')                          move_cnt,
          count(*) FILTER (WHERE stage='count')                              count_cnt,
          count(DISTINCT tote_barcode) FILTER (WHERE stage='engrave')        eng_cnt,
          count(DISTINCT et_day(ts)) FILTER (WHERE is_floor_labor(stage,subtype)) active_days,
          min(ts)  FILTER (WHERE is_floor_labor(stage,subtype))              first_ts,
          max(ts)  FILTER (WHERE is_floor_labor(stage,subtype))              last_ts
        FROM e GROUP BY person""", [frm, to])
        rows = cur.fetchall()

        cur.execute("""
        WITH o AS (SELECT order_number, bool_or(source='shiphero') sh, bool_or(source='shopify') shop
                   FROM event WHERE stage='pack' AND order_number IS NOT NULL
                     AND person <> ALL(%s)              -- fired/departed packers don't count an order as shipped
                     AND et_day(ts) BETWEEN %s AND %s GROUP BY order_number)
        SELECT count(*) total, count(*) FILTER (WHERE sh) shiphero,
               count(*) FILTER (WHERE NOT sh AND shop) shopify_only,
               count(*) FILTER (WHERE sh AND shop) both FROM o""", [list(EXCLUDED), frm, to])
        shipped = cur.fetchone()

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
        totals=tot, people=people)

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
    return jsonify(range={"from":frm,"to":to}, days=_daylist(frm,to), engravers=out)

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

@app.route("/daily")
def daily():
    """Per-DAY team report for the window: one row per active working day (empty days —
    weekends, holidays — are dropped), so you can scan day-by-day performance. Orders
    shipped, fulfillment (pick/pack/engrave, MagNano-corrected), restock, people on the
    floor, active person-hours (45-min-break rule) and the day's UPLH."""
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH ev AS (
          SELECT person, ts, et_day(ts) d, stage, subtype, quantity, order_number
          FROM event_canon WHERE et_day(ts) BETWEEN %s AND %s AND person <> ALL(%s)),
        act AS (   -- total active person-seconds/day = consecutive floor-labor gaps under the 45-min break
          SELECT d, sum(CASE WHEN gap>0 AND gap<%s THEN gap ELSE 0 END) active_s
          FROM (SELECT d, EXTRACT(epoch FROM (ts - lag(ts) OVER (PARTITION BY person,d ORDER BY ts))) gap
                FROM ev WHERE is_floor_labor(stage,subtype)) g GROUP BY d),
        shp AS (SELECT d, count(DISTINCT order_number) shipped
                FROM ev WHERE stage='pack' AND order_number IS NOT NULL GROUP BY d)
        SELECT e.d, EXTRACT(isodow FROM e.d)::int dow,
          count(DISTINCT e.person) FILTER (WHERE is_floor_labor(e.stage,e.subtype))  people,
          COALESCE(sum(e.quantity) FILTER (WHERE e.stage='pick'),0)                  pick,
          COALESCE(sum(e.quantity) FILTER (WHERE e.stage='pack'),0)                  pack,
          COALESCE(sum(e.quantity) FILTER (WHERE e.stage='engrave'),0)               engrave,
          COALESCE(sum(e.quantity) FILTER (WHERE e.stage='replenish'),0)             restock,
          COALESCE(max(a.active_s),0) active_s, COALESCE(max(s.shipped),0) shipped
        FROM ev e LEFT JOIN act a USING(d) LEFT JOIN shp s USING(d)
        GROUP BY e.d ORDER BY e.d""", [frm, to, list(EXCLUDED), ACTIVE_BREAK])
        rows = cur.fetchall()
    days = []
    for (d, dow, people, pick, pack, eng, restock, active_s, shipped) in rows:
        ful = int(pick) + int(pack) + int(eng)
        if ful == 0 and int(restock) == 0 and not shipped:
            continue                                   # skip inactive days (weekends / holidays)
        hrs = float(active_s or 0) / 3600.0
        days.append(dict(d=str(d), dow=int(dow), people=int(people or 0), shipped=int(shipped or 0),
            pick=int(pick), pack=int(pack), engrave=int(eng), fulfillment=ful, restock=int(restock),
            hours=round(hrs, 1), uplh=(round((ful + int(restock)) / hrs) if hrs > 0 else 0)))
    return jsonify(range={"from": frm, "to": to}, days=days)

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
        unidentified=unidentified)

@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")

DASHBOARD_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Warehouse Picking &amp; Packing</title>
<link rel=preconnect href="https://fonts.googleapis.com"><link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel=stylesheet>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js"></script>
<style>
:root{color-scheme:light;
  --bg:#f5f6f8;--surface:#fff;--line:#e6e8ec;--line-2:#eef0f3;
  --ink:#0f172a;--ink-2:#475569;--muted:#64748b;
  --accent:#2563eb;--accent-weak:#eff4ff;
  --green:#16a34a;--amber:#d97706;--violet:#7c3aed;--teal:#0d9488;--red:#dc2626;
  --r:14px;--r-sm:9px;--r-xs:7px;
  --sh:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.05);}
*{box-sizing:border-box}
body{margin:0;font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  background:var(--bg);color:var(--ink);font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
td,th,.stat .v,.shipped .big{font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1}
.wrap{max-width:1360px;margin:0 auto;padding:26px 28px 96px}
.apphead{display:flex;align-items:center;gap:11px;margin-bottom:3px}
.apphead .dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.16)}
.apphead .live{font-size:10.5px;font-weight:700;color:var(--green);text-transform:uppercase;letter-spacing:.07em}
h1{margin:0;font-size:22px;font-weight:700;letter-spacing:-.02em}
.sub{color:var(--ink-2);margin:6px 0 18px;font-size:13.5px;max-width:940px;line-height:1.55}
.sub b{color:var(--ink);font-weight:600}
.tabs{display:flex;gap:24px;border-bottom:1px solid var(--line);margin-bottom:20px}
.tab{padding:11px 2px;font-weight:600;font-size:14px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .12s}
.tab:hover{color:var(--ink-2)}
.tab.on{color:var(--accent);border-bottom-color:var(--accent)}
.ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:11px}
.ctl .lbl{font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.07em;font-weight:600;margin-right:2px}
.arf{font-size:12.5px;color:var(--ink-2);display:inline-flex;align-items:center;gap:5px;cursor:pointer;font-weight:600}
.arf input{cursor:pointer;width:15px;height:15px}
.seg{display:inline-flex;background:#0f172a;border-radius:var(--r-sm);padding:3px;gap:2px}
.seg button{border:0;background:transparent;color:#cbd5e1;padding:6px 13px;border-radius:6px;font:inherit;font-size:12.5px;font-weight:600;cursor:pointer;transition:background .12s,color .12s}
.seg button:hover{color:#fff}
.seg button.on{background:#fff;color:#0f172a}
.seg.gray{background:#eceef2}.seg.gray button{color:var(--ink-2)}.seg.gray button:hover{color:var(--ink)}.seg.gray button.on{background:#334155;color:#fff}
.pill{border:1px solid var(--line);background:var(--surface);border-radius:var(--r-xs);padding:7px 13px;font:inherit;font-size:12.5px;font-weight:600;color:var(--ink-2);cursor:pointer;transition:border-color .12s,color .12s}
.pill:hover{border-color:#cbd5e1;color:var(--ink)}
.pill.on{background:var(--accent);border-color:var(--accent);color:#fff}
input[type=date]{border:1px solid var(--line);border-radius:var(--r-xs);padding:6px 9px;font:inherit;font-size:12.5px;color:var(--ink);background:var(--surface)}
input[type=date]:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-weak)}
.spacer{flex:1}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:18px 20px;box-shadow:var(--sh)}
.shipped{border:1px solid var(--line);border-left:4px solid var(--green);margin:18px 0;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;background:linear-gradient(180deg,#f6fdf9,#fff)}
.shipped .big{font-size:42px;font-weight:800;color:var(--green);line-height:1;letter-spacing:-.02em}
.shipped .t{font-size:16px;font-weight:600;color:var(--ink)}
.shipped .d{color:var(--ink-2);font-size:13px;flex-basis:100%;margin-top:8px}
.shipped .d b{color:var(--green)}.shipped .d .o{color:var(--amber)}
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:8px}
.stat{padding:15px 16px}
.stat .k{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.stat .k .s{font-weight:600;font-size:10.5px;margin-left:5px;text-transform:none;letter-spacing:0}
.stat .v{font-size:27px;font-weight:700;margin-top:7px;letter-spacing:-.02em}
.s-sh{color:var(--accent)}.s-pksh{color:#16a34a}.s-shop{color:var(--amber)}.s-repl{color:var(--violet)}.s-sel{color:var(--ink)}.s-eng{color:var(--teal)}
.note{color:var(--ink-2);font-size:12.5px;margin:14px 0;line-height:1.55}
h2{font-size:15px;font-weight:700;margin:0 0 4px;letter-spacing:-.01em}
.chartwrap{height:360px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{padding:10px 12px;border-bottom:1px solid var(--line-2);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;padding-left:4px}
th:last-child,td:last-child{padding-right:4px}
th{color:var(--muted);font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;cursor:pointer;user-select:none}
th:hover{color:var(--ink-2)}
th .s{color:#7c8698;font-weight:500;text-transform:none;letter-spacing:0}
tr:hover td{background:#f8fafc}
td.name{font-weight:600;color:var(--ink)}
.tablewrap{overflow-x:auto;overflow-y:hidden}
.tablewrap>table{margin-top:6px}
.tablewrap td:first-child,.tablewrap th:first-child{position:sticky;left:0;z-index:2;background:var(--surface);box-shadow:1px 0 0 var(--line-2)}
.tablewrap tr:hover td:first-child{background:#f8fafc}
.tablewrap tr.tot td:first-child{background:#fcfcfd}
.tablewrap::-webkit-scrollbar{height:8px}
.tablewrap::-webkit-scrollbar-thumb{background:#d7dbe2;border-radius:8px}
.tablewrap::-webkit-scrollbar-track{background:transparent}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:10.5px;font-weight:600}
.badge.ft{background:var(--accent-weak);color:var(--accent)}.badge.in{background:#f3f0ff;color:var(--violet)}.badge.sea{background:#fff7ed;color:#c2410c}
.o{color:var(--amber)}.p{color:var(--violet);font-weight:600}.eng{color:var(--teal);font-weight:600}
tr.tot td{font-weight:700;color:var(--ink);border-top:1.5px solid var(--line);background:#fcfcfd}
.red{color:var(--red);font-weight:600}
.foot{color:var(--muted);font-size:11.5px;margin-top:24px;line-height:1.7}
.foot b{color:var(--ink-2)}
#status{color:var(--ink-2);font-size:12.5px;margin-left:8px}
.hide{display:none}
.mbox{background:linear-gradient(180deg,#fbfcfe,#f7f9fc);border:1px solid var(--line);border-radius:var(--r);padding:16px 18px;font-size:12.8px;color:var(--ink-2);margin-bottom:16px;line-height:1.65}
.mbox h3{margin:0 0 8px;font-size:12px;color:var(--ink);text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.mbox b{color:var(--ink)}
.mbox code{background:#eef1f6;color:#334155;padding:1px 6px;border-radius:5px;font-size:11.5px;font-family:ui-monospace,'SFMono-Regular',Menlo,Consolas,monospace}
.spgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px}
.pill2{display:inline-block;padding:2px 10px;border-radius:20px;font-size:10.5px;font-weight:700}
.matrix td.cell{text-align:center}
.spbar{height:5px;border-radius:4px;background:#edf0f4;margin-top:4px;overflow:hidden}
.spbar>i{display:block;height:5px;border-radius:4px}
.best{background:#f0fdf4;outline:1.5px solid #86efac;border-radius:8px}
.ins{color:var(--muted)}
th.act{color:var(--ink)}
.arw{font-size:9px;margin-left:4px;color:var(--accent)}
td.fi{line-height:1.25}
td.fi>b{font-size:14px}
td.fi .brk{font-size:10px;color:var(--muted);font-weight:400;margin-top:3px;letter-spacing:.2px}
td.shr{color:var(--ink-2);font-weight:600}
.dcol{text-align:center;font-size:9.5px;line-height:1.15;color:var(--muted);font-weight:700}
.dcol .s{color:#c2c8d2;font-weight:700}
td.dcell{text-align:center;font-size:11px;line-height:1.25;min-width:46px}
td.dcell>b{font-size:12px;color:var(--ink)}
.dcell .dsub{font-size:9.5px;color:var(--muted)}
.dcell .dmt{color:#cbd2db}
.wknd{background:#fbfbfc}
th.dsep,td.dsep{width:10px;min-width:10px;max-width:10px;padding:0;background:#f2f5f9}
td.mix{white-space:nowrap;font-size:12px}
.mlid{color:#2563eb;font-weight:700}.mipe{color:#0d9488;font-weight:700}.mdotw{color:#b45309;font-weight:700}
.nbox{border:1px solid var(--line);border-radius:10px;padding:10px 14px;background:#fbfcfe}
.nbox summary{cursor:pointer;font-size:12.5px;font-weight:600;color:var(--ink-2)}
.nbox summary .s{color:var(--muted);font-weight:400}
.nrow{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px}
.nin{padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px;font-family:inherit;background:#fff}
.nin:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-weak)}
.nhrs{width:78px}.nnote{flex:1;min-width:220px}
.notechip{display:inline-flex;align-items:center;gap:6px;background:#eef2ff;color:#3730a3;border-radius:8px;padding:3px 9px;font-size:11.5px;margin:8px 6px 0 0}
.notechip b{color:#1e1b4b}.notechip .x{cursor:pointer;color:#818cf8;font-weight:700;margin-left:2px}
.projh{color:#4338ca;font-weight:700}
td.sub2{color:var(--muted)}
/* Trust & coverage layer */
.covwrap{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:#fbfcfe;margin:14px 0}
.covhead{font-size:13px;font-weight:700;color:var(--ink-2);margin-bottom:2px}
.covsum{font-size:12px;color:var(--muted);margin-bottom:10px}
.covsum b{color:var(--ink-2)}
.covlegend{font-size:11px;color:var(--muted);margin:2px 0 12px}
.covkey{display:inline-flex;align-items:center;gap:5px;margin-right:14px}
.covkey i{width:11px;height:11px;border-radius:3px;display:inline-block}
.covrow{display:flex;align-items:center;gap:10px;padding:4px 0}
.covname{width:150px;font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.covpct{width:42px;text-align:right;font-weight:700;font-size:12.5px}
.covpct.g{color:#15803d}.covpct.a{color:#b45309}.covpct.r{color:#b91c1c}
.covbar{flex:1;height:17px;border-radius:5px;overflow:hidden;display:flex;background:#eef2f7;min-width:120px}
.covbar i{display:block;height:100%}
.cov-sc{background:#34d399}.cov-lg{background:#6366f1}.cov-un{background:#f87171}
.covmeta{width:150px;text-align:right;font-size:11px;color:var(--muted)}
.covmeta b{color:var(--ink-2)}
.covlog{color:#4f46e5;cursor:pointer;font-weight:600}.covlog:hover{text-decoration:underline}
/* Staffing planner */
.planbar{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin:14px 0}
.planhero{display:flex;gap:30px;align-items:baseline;background:linear-gradient(180deg,#f2f6ff,#fff);border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:12px;padding:16px 20px;margin:6px 0 4px;flex-wrap:wrap}
.phn{font-size:36px;font-weight:800;color:var(--accent);line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.phl{font-size:12px;color:var(--muted);margin-top:3px}
.phd{font-size:15px;color:var(--ink-2);font-weight:600}
.plancmp{font-size:13px;border-radius:9px;padding:9px 13px;margin:10px 0}
.plancmp.ok{background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46}
.plancmp.short{background:#fff7ed;border:1px solid #fdba74;color:#9a3412}
.plantbl{margin-top:4px}
.plan-rate{width:66px;text-align:center;padding:5px 6px}
.lineup{display:flex;gap:14px;flex-wrap:wrap}
.lncol{flex:1;min-width:150px;border:1px solid var(--line);border-radius:10px;padding:10px 12px;background:#fbfcfe}
.lnh{font-weight:700;font-size:12.5px;margin-bottom:6px;color:var(--ink-2)}
.lnrow{display:flex;justify-content:space-between;font-size:12px;padding:2px 0}
.lnrow b{font-variant-numeric:tabular-nums}
.logptr{margin-top:14px;font-size:12.5px;color:var(--muted);background:#f7f9fc;border:1px solid var(--line);border-radius:9px;padding:9px 13px}
.tablink{color:var(--accent);font-weight:700;cursor:pointer}.tablink:hover{text-decoration:underline}
.trendkpis{display:flex;gap:14px;flex-wrap:wrap;margin:12px 0 6px}
.trendkpi{font-size:13px;background:#fbfcfe;border:1px solid var(--line);border-radius:9px;padding:8px 12px}
.plout{opacity:.42}
.plbn td{background:#fff7ed}
.plhrs{width:58px;text-align:center;padding:5px 6px}
.plsel{padding:5px 8px;border:1px solid var(--line);border-radius:7px;font-size:12.5px;font-family:inherit;background:#fff;cursor:pointer}
.plsel:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-weak)}
.plin{width:15px;height:15px;cursor:pointer}
/* Log off-scanner time card */
.logcard{border:1px solid var(--line);border-radius:12px;padding:14px 16px;background:#fbfcfe;margin-top:16px}
.logtitle{font-size:13px;font-weight:700;color:var(--ink-2);margin-bottom:10px}
.logtitle .s{color:var(--muted);font-weight:400}
.logrow{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.lf{display:flex;flex-direction:column;gap:4px}
.lf label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}
.lf select,.lf input{padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-size:13px;font-family:inherit;background:#fff}
.lf select:focus,.lf input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-weak)}
.lf select{min-width:150px}
.lfh input{width:78px}
.lgrow{flex:1;min-width:200px}.lgrow input{width:100%;box-sizing:border-box}
.pill.add{padding:8px 18px;font-weight:700}
.nstat{font-size:12px;color:var(--muted);min-height:0;margin-top:2px}
.nstat.ok{color:#16a34a}.nstat.err{color:#dc2626}
.sched{margin-top:4px}
.schead{font-size:12px;color:var(--ink-2);margin:8px 0 6px}.schead b{color:var(--ink)}
.tline{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.twork{display:inline-flex;flex-direction:column;background:#ecfdf5;border:1px solid #a7f3d0;color:#065f46;border-radius:8px;padding:4px 10px;font-size:11.5px;line-height:1.3}
.twork b{font-weight:700}.twork .m{color:#059669;font-size:10px}
.tgap{display:inline-flex;flex-direction:column;cursor:pointer;background:#fff7ed;border:1px dashed #fdba74;color:#9a3412;border-radius:8px;padding:4px 10px;font-size:11.5px;line-height:1.3;transition:all .12s}
.tgap:hover{background:#ffedd5;border-color:#fb923c}
.tgap.brk{background:#fef2f2;border-color:#fecaca;color:#991b1b}.tgap.brk:hover{background:#fee2e2}
.tgap b{font-weight:700}.tgap .m{font-size:10px;opacity:.85}.tgap .fill{font-size:9.5px;text-transform:uppercase;letter-spacing:.03em;font-weight:700;opacity:.7}
.tarrow{color:var(--muted);font-size:11px}
.grp th{border-bottom:none;padding:0 8px 3px}
.gh{text-align:center;font-size:10px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;padding:4px 6px;border-radius:6px}
.gt{color:#334155;background:#eef2f7}
.gf{color:#1e40af;background:#eaf1ff}
.gr{color:#6d28d9;background:#f3eeff}
th.gsep,td.gsep{width:16px;min-width:16px;max-width:16px;padding:0;border-bottom:none;background:transparent}
tr:hover td.gsep{background:transparent}
/* whole-column group tints (the eye follows the band down the table) */
td.gct,th.gct{background:#f4f6fa}
td.gcf,th.gcf{background:#eff5ff}
td.gcr,th.gcr{background:#f7f3ff}
tr:hover td.gct{background:#eaeef4}tr:hover td.gcf{background:#e5eeff}tr:hover td.gcr{background:#efe8ff}
tr.tot td.gct{background:#eef1f6}tr.tot td.gcf{background:#e6eeff}tr.tot td.gcr{background:#efe7ff}
.wchip{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10.5px;font-weight:600;margin:2px 4px 2px 0;white-space:nowrap;cursor:default}
.wchip.r{background:#fef2f2;color:#b91c1c}
.wchip.a{background:#fffbeb;color:#b45309}
.wok{color:var(--green);font-weight:600;font-size:12px}
.ured{color:var(--red);font-weight:700}.uamb{color:var(--amber);font-weight:700}.ugrn{color:var(--green);font-weight:700}
@media(max-width:1100px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(max-width:900px){.spgrid{grid-template-columns:1fr}.wrap{padding:20px 16px 80px}}
/* ===== TV / clean mode + gear drawer (presentation layer; renderers unchanged) ===== */
#gearbtn{position:fixed;top:14px;right:16px;z-index:50;width:42px;height:42px;border-radius:11px;border:1px solid var(--line);background:var(--surface);color:var(--ink-2);font-size:20px;cursor:pointer;box-shadow:var(--sh);display:none;align-items:center;justify-content:center}
#gearbtn:hover{color:var(--ink)}
body.clean #gearbtn{display:inline-flex}
#scrim{position:fixed;inset:0;background:rgba(15,23,42,.42);opacity:0;pointer-events:none;transition:opacity .2s;z-index:60}
#scrim.open{opacity:1;pointer-events:auto}
#drawer{position:fixed;top:0;right:0;height:100%;width:334px;background:var(--surface);border-left:1px solid var(--line);transform:translateX(100%);transition:transform .22s ease;z-index:70;padding:20px;overflow-y:auto;box-shadow:-18px 0 40px rgba(15,23,42,.18)}
#drawer.open{transform:translateX(0)}
#drawer h3{margin:0 0 3px;font-size:16px}
#drawer .dh{color:var(--muted);font-size:12px;margin-bottom:6px;line-height:1.5}
#drawer .dsec{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:700;margin:18px 0 8px}
#drawer .navlist{display:flex;flex-direction:column;gap:2px}
#drawer .navlist button{text-align:left;border:0;background:transparent;color:var(--ink-2);font:inherit;font-size:13.5px;font-weight:600;padding:8px 10px;border-radius:8px;cursor:pointer}
#drawer .navlist button:hover{background:var(--accent-weak);color:var(--accent)}
#drawer .navlist button.cur{background:var(--accent);color:#fff}
#drawer .dclose{position:absolute;top:14px;right:16px;border:0;background:transparent;font-size:23px;color:var(--muted);cursor:pointer;line-height:1}
#drawerctl .ctl{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px;align-items:center}
#drawerctl .ctl .spacer,#drawerctl .lbl{width:100%;flex-basis:100%}
#drawerctl .lbl{margin-top:4px}
/* clean mode: strip chrome + compact so the dashboard fits one screen */
body.clean .tabs,body.clean #summary,body.clean .foot,body.clean #statsOrders{display:none!important}
body.clean>.wrap>.sub{display:none}
body.clean #dash>.note{display:none}
body.clean .card>.sub{display:none}
body.clean{overflow:hidden}
body.clean .wrap{max-width:none;padding:12px 22px 14px;height:100vh;overflow:hidden;display:flex;flex-direction:column}
body.clean .apphead{margin-bottom:8px}
/* clean mode replaces the app's banner + 6 by-type cards with the approved 4-tile hero strip */
body.clean .apphead,body.clean .shipped,body.clean .cards{display:none!important}
body.clean #tvhead{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;margin-bottom:10px}
body.clean #tvkpis{display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr;gap:12px;margin-bottom:10px}
#tvhead,#tvkpis{display:none}
.tvbrand .tvtitle{font-size:19px;font-weight:700;letter-spacing:-.01em}
.tvbrand .tvlive{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.05em;margin-top:3px}
.tvdatewrap{text-align:center;flex:1}
.tvdate{font-size:26px;font-weight:700;letter-spacing:-.02em;line-height:1}
.tvdsub{color:var(--muted);font-size:12.5px;margin-top:3px}
.tvref{color:var(--muted);font-size:11.5px;text-align:right;min-width:130px;padding-right:46px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:13px;padding:11px 16px;position:relative;overflow:hidden;box-shadow:var(--sh)}
.kpi .acc{position:absolute;left:0;top:0;bottom:0;width:4px}
.kpi .kl{color:var(--muted);font-size:10.5px;letter-spacing:.05em;text-transform:uppercase}
.kpi .kv{font-weight:700;letter-spacing:-.02em;line-height:1;margin-top:5px;font-size:30px}
.kpi.hero .kv{font-size:44px}
.kpi .kn{margin-top:6px;color:var(--ink-2);font-size:12px;display:flex;gap:12px;flex-wrap:wrap}
.kpi .kn .sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle}
body.clean #dash{flex:1;display:flex;flex-direction:column;min-height:0;gap:10px}
body.clean #dash>.card{margin:0!important}
body.clean #dash .card:has(.chartwrap){flex:1;display:flex;flex-direction:column;min-height:0;padding-top:10px}
body.clean #dash .chartwrap{flex:1;height:auto!important;min-height:0;margin-top:2px}
body.clean #dash .card:has(#detail){flex:1.5;min-height:0;display:flex;flex-direction:column;padding-top:10px}
body.clean #dash .card:has(#detail) #detail{flex:1;min-height:0;overflow:auto}
body.clean #dash .card:has(#detail) td{padding:5px 10px}
body.clean #dash .card:has(#detail) th{padding:4px 10px}
body.clean #dash>.card>h2{font-size:13px;margin-bottom:2px}
/* dark (TV) — flips the surfaces; chart text is re-themed in drawChart() */
body.dark{--bg:#0d0d0d;--surface:#1a1a19;--line:#2c2c2a;--line-2:#242422;--ink:#f5f5f0;--ink-2:#c3c2b7;--muted:#8a887f;--accent-weak:#1e2a3f}
body.dark .shipped{background:linear-gradient(180deg,#15211a,#1a1a19)}
body.dark tr:hover td{background:#232320}
body.dark td.gct,body.dark th.gct{background:#20201e}
body.dark td.gcf,body.dark th.gcf{background:#172233}
body.dark td.gcr,body.dark th.gcr{background:#221b33}
body.dark tr:hover td.gct{background:#26261f}body.dark tr:hover td.gcf{background:#1d2942}body.dark tr:hover td.gcr{background:#291f40}
body.dark tr.tot td{background:#161615}
body.dark tr.tot td.gct{background:#20201e}body.dark tr.tot td.gcf{background:#172233}body.dark tr.tot td.gcr{background:#221b33}
body.dark .tablewrap td:first-child,body.dark .tablewrap th:first-child{background:var(--surface)}
body.dark .tablewrap tr:hover td:first-child{background:#232320}
body.dark .tablewrap tr.tot td:first-child{background:#161615}
body.dark .seg.gray{background:#2c2c2a}body.dark .seg.gray button{color:var(--ink-2)}
body.dark .badge.ft{background:#1e2a3f;color:#7fb0ff}body.dark .badge.in{background:#2a1f40;color:#b9a4f0}
</style></head><body>
<button id=gearbtn onclick="openDrawer()" title="Settings &amp; panels">&#9881;</button>
<div id=scrim onclick="closeDrawer()"></div>
<aside id=drawer>
  <button class=dclose onclick="closeDrawer()" title="Close">&times;</button>
  <h3>Board settings</h3>
  <div class=dh>Filters and the other views live here, so the board itself stays clean for the TV.</div>
  <div class=dsec>Go to panel</div>
  <div class=navlist id=drawernav></div>
  <div class=dsec>Filters &amp; date</div>
  <div id=drawerctl></div>
  <div class=dsec>Display</div>
  <div class=navlist>
    <button onclick="toggleDark()" id=darktoggle>Switch to dark (TV)</button>
    <button onclick="toggleTV()" id=tvtoggle>Exit TV mode (full controls)</button>
  </div>
</aside>
<div class=wrap>
<div id=tvhead>
  <div class=tvbrand><div class=tvtitle>Warehouse Contribution</div><div class=tvlive><span class=dot></span>Live &middot; ShipHero &middot; Shopify &middot; Engraving</div></div>
  <div class=tvdatewrap><div class=tvdate id=tvdate></div><div class=tvdsub id=tvdsub></div></div>
  <div class=tvref><span id=tvrefstamp></span></div>
</div>
<div id=tvkpis></div>
<div class=apphead><h1>Warehouse Picking &amp; Packing</h1><span class=dot></span><span class=live>Live</span></div>
<div class=sub>Live contribution from ShipHero <b>+ direct-in-Shopify fulfillments + engraving</b>. <b>Fulfillment</b> (pick + pack + engrave) and <b>Restock</b> are two separate tracks.</div>
<div class=tabs>
  <div class="tab on" data-tab=dash onclick="tab('dash')">Dashboard</div>
  <div class=tab data-tab=out onclick="tab('out')">Outstanding</div>
  <div class=tab data-tab=floor onclick="tab('floor')">Floor Time</div>
  <div class=tab data-tab=log onclick="tab('log')">Log time</div>
  <div class=tab data-tab=plan onclick="tab('plan')">Planner</div>
  <div class=tab data-tab=trend onclick="tab('trend')">Trends</div>
  <div class=tab data-tab=speed onclick="tab('speed')">Speed &amp; Rankings</div>
  <div class=tab data-tab=engt onclick="tab('engt')">Engraving</div>
  <div class=tab data-tab=watch onclick="tab('watch')">Watch List</div>
  <div class=tab data-tab=an onclick="tab('an')">Analytics</div>
  <div class=tab data-tab=dataqc onclick="tab('dataqc')">Data Issues</div>
</div>
<div class=ctl>
  <button class="pill on" data-preset=today onclick="preset('today')">Today</button>
  <button class=pill data-preset=yest onclick="preset('yest')">Yesterday</button>
  <button class=pill data-preset=week onclick="preset('week')">This week</button>
  <button class=pill data-preset=7 onclick="preset('7')">Last 7 days</button>
  <button class=pill data-preset=30 onclick="preset('30')">Last 30 days</button>
  <input type=date id=from onchange="dateEdit()"> <span style=color:#9ca3af>to</span> <input type=date id=to onchange="dateEdit()">
  <button class=pill onclick="load()">Apply</button><span id=status></span>
  <span class=spacer></span>
  <label class=arf title="Auto-refresh — for leaving on a wall or TV display"><input type=checkbox id=autoref onchange="toggleAuto()"> Auto-refresh</label>
  <select id=autoint class=nin style="padding:5px 6px" onchange="toggleAuto()"><option value=2>2 min</option><option value=5 selected>5 min</option><option value=15>15 min</option></select>
  <span id=refstamp class=sub></span>
</div>
<div class=ctl id=ctl1>
  <span class=lbl>Unit</span><span class=seg id=unit><button class=on data-v=both>Both</button><button data-v=items>Items</button><button data-v=orders>Orders</button></span>
  <span class=lbl style=margin-left:14px>Stage</span><span class=seg id=stage><button class=on data-v=all>All</button><button data-v=pick>Picked</button><button data-v=pack>Packed</button><button data-v=engrave>Engraved</button><button data-v=repl>Restocked</button></span>
  <span class=lbl style=margin-left:14px>Source</span><span class=seg id=source><button class=on data-v=both>Both</button><button data-v=shiphero>ShipHero</button><button data-v=shopify>Shopify</button></span>
  <div class=spacer></div>
  <button class=pill onclick="copyChat()">Copy for chat</button>
  <button class=pill onclick="dl('csv')">CSV</button>
  <button class=pill onclick="dl('json')">JSON</button>
</div>
<div class=ctl id=ctl2>
  <span class="seg gray" id=team><button class=on data-v=all>Everyone</button><button data-v=FT>Full-timers</button><button data-v=Seasonal>Seasonal</button><button data-v=Intern>Interns</button></span>
  <span class=lbl style=margin-left:14px>Detail</span><span class=seg id=view><button class=on data-v=simple>Simple</button><button data-v=detailed>Detailed</button></span>
</div>
<div class=note id=summary></div>

<div id=dash>
  <div class="card shipped" id=shipped></div>
  <div class=cards id=statsItems></div>
  <div class=cards id=statsOrders style=margin-top:14px></div>
  <div class=note>Cards and chart follow the Unit / Stage / Source toggles; the table always shows both items and orders. <b>Fulfillment = Picked + Packed + Engraved</b> for the selected filters. Restocking is a separate track, never added into that total.</div>
  <div class=card style=margin-top:8px>
    <h2>Contribution by person</h2>
    <div class=sub style=margin:0>Each bar is one person&rsquo;s <b>Fulfillment</b> total (Picked + Packed + Engraved) for the selected <b>Unit</b>, tallest first &mdash; the number above each bar is that total. <b>Restocked</b> is a separate track, drawn as its own bar. Click a bar to focus that person.</div>
    <div class=chartwrap><canvas id=chart></canvas></div>
  </div>
  <div class=card style=margin-top:16px>
    <h2>Per-person detail</h2>
    <div class=sub style=margin:0>Three groups, left to right: <b>On the clock</b> (days &amp; hours worked, so you can see whether someone's ahead just because they put in more time), <b>Fulfillment</b> (pick + pack + engrave, one figure), and <b>Restocking</b> (separate). Click any column header to sort. <b>Hours</b> = clock time from first to last scan &mdash; the default &ldquo;hours worked,&rdquo; since gaps may be special-project time rather than idle. <b>Active</b> = that same window minus 45-min+ breaks (the stricter, calculated number).</div>
    <div id=detail></div>
  </div>
</div>

<div id=dataqc class=hide>
  <div class=card>
    <h2>Data issues &amp; warnings <span style="color:#9ca3af;font-weight:400">&mdash; question weird numbers before they drive a decision</span></h2>
    <div class=sub style=margin:0>Automated checks on the scan data itself: whether today is just a partial (in-progress) day, day-over-day anomalies worth a sanity-check, how fresh the data is, and an honest note on what the scan data can and can&rsquo;t tell us about shared logins. This view ignores the date range above.</div>
    <div id=dataqc_body><div class=sub style=margin-top:12px>loading&hellip;</div></div>
  </div>
</div>
<div id=out class=hide>
  <div class=card>
    <h2>Outstanding orders <span style="color:#9ca3af;font-weight:400">&mdash; the backlog we owe: how much, how aged, what status</span></h2>
    <div class=sub style=margin:0>Every order in ShipHero that hasn&rsquo;t shipped yet &mdash; the demand side, next to your output. <b>Aged</b> = open <b>3+ days</b> (past a normal 1&ndash;2 business-day ship window). <b>On hold</b> = flagged in ShipHero (address / payment / fraud / operator) and can&rsquo;t ship until cleared. Snapshot refreshes with the ShipHero ingest; this view ignores the date range above.</div>
    <div id=out_body><div class=sub style=margin-top:12px>loading&hellip;</div></div>
  </div>
</div>

<div id=floor class=hide>
  <div class=card>
    <h2>Floor Time <span style="color:#9ca3af;font-weight:400">&mdash; the effectiveness auditor: hours &amp; output, by day</span></h2>
    <div class=sub style=margin:0>The headline <b>Hours</b> is the raw window from a person&rsquo;s <b>first scan to their last</b> (First in &rarr; Last out) &mdash; the default measure of time on the floor, and the one we trust most today. <b>Active</b> strips 45-min+ breaks out of that window; it&rsquo;s a stricter, still-maturing calculation, so it sits beside Hours rather than replacing it. The gap between them is break / off-scanner time &mdash; log the real off-scanner work under <b>Proj h</b> to keep it honest. <b>Util</b> = active &divide; span; <b>Items/hr</b> = units per floor-hour. All floor work (pick + pack + engrave + restock), Eastern time. <b>Replenishment is credited fairly:</b> because boxes are worked first and scanned in a burst afterward, a long gap right before a replenish scan counts as box work (not a break) &mdash; capped at a reasonable per-box amount, so a 2-hour gap before one box can never read as 2 hours of replenishing.</div>
    <div id=floorcover></div>
    <div id=floortable></div>
    <div class=logptr>Off-scanner work (returns, cleanup, meetings, training) is logged in the <b><span class=tablink onclick="tab('log')">Log time</span></b> tab &mdash; it feeds the <b>Proj h</b> column and the coverage bars above.</div>
  </div>
</div>

<div id=log class=hide>
  <div class=card>
    <h2>Log time <span style="color:#9ca3af;font-weight:400">&mdash; account for real work that doesn&rsquo;t hit a scanner</span></h2>
    <div class=sub style=margin:0>Returns, floor resets, receiving, cleanup, meetings, training &mdash; the work that never scans. Logging it here closes each person&rsquo;s unexplained-time gap (see the coverage bars on <span class=tablink onclick="tab('floor')">Floor Time</span>) and feeds their <b>Proj h</b>. Pick a person and a date and their scan schedule appears &mdash; tap any gap to fill it in.</div>
    <div class=logcard>
      <div class=logtitle>Log off-scanner time</div>
      <div class=logrow>
        <div class=lf><label>Person</label>
          <select id=n_person onchange="onPersonPick()"><option value="">Select&hellip;</option></select>
          <input id=n_person_custom class=nin placeholder="New name&hellip;" style="display:none" oninput="loadPersonDay()">
        </div>
        <div class=lf><label>Date</label><input id=n_date type=date class=nin onchange="onPersonPick()"></div>
        <div class=lf><label>From</label><input id=n_start type=time class=nin onchange="calcHrs()"></div>
        <div class=lf><label>To</label><input id=n_end type=time class=nin onchange="calcHrs()"></div>
        <div class="lf lfh"><label>Hours</label><input id=n_hours type=number step=0.25 min=0 max=24 class="nin nhrs" placeholder="auto"></div>
        <div class="lf lgrow"><label>What were they doing?</label><input id=n_note class=nin placeholder="e.g. processed returns, floor reset"></div>
        <button class="pill add" onclick="addNote()">Add</button>
      </div>
      <div id=n_status class=nstat></div>
      <div id=n_sched class=sched></div>
      <div id=n_list></div>
    </div>
  </div>
</div>

<div id=engt class=hide>
  <div class=card>
    <h2>Engraving <span style="color:#9ca3af;font-weight:400">&mdash; per engraver: output, speed, mix &amp; match quality</span></h2>
    <div class=sub style=margin:0>From the live logger, matched to each tote&rsquo;s order. <b>Totes</b> = jobs finished; <b>Items</b> = engravings (LID + IPE + DOTW); <b>Match</b> = % of totes resolved to an order. Hours use the same 45-min-break active-time rule.</div>
    <div id=engtable></div>
  </div>
</div>

<div id=an class=hide>
  <div class=card><h2>Analytics</h2><div id=analytics></div></div>
</div>

<div id=speed class=hide>
  <div id=speed_method></div>
  <div class=card>
    <h2>Speed leaderboards <span style="color:#9ca3af;font-weight:400">&mdash; units per active hour, fastest first</span></h2>
    <div class=sub style=margin:0>Only people with enough data are ranked; everyone else is listed as &ldquo;insufficient&rdquo; with the reason. Uses the date range above.</div>
    <div id=speed_boards class=spgrid style=margin-top:12px></div>
  </div>
  <div class=card style=margin-top:16px>
    <h2>Picking speed &mdash; MagNano vs Normal <span style="color:#9ca3af;font-weight:400">&mdash; MagNano cases pick much faster</span></h2>
    <div class=sub style=margin:0>MagNano cases snap together as one strip, so picking them racks up items far faster than a normal pick. This splits each picker&rsquo;s pace (same median method) on MagNano cases vs everything else, so an easy MagNano-heavy day is obvious.</div>
    <div id=speed_split style=margin-top:8px></div>
  </div>
  <div class=card style=margin-top:16px>
    <h2>Where the time goes <span style="color:#9ca3af;font-weight:400">&mdash; tracked hours by activity, per person</span></h2>
    <div class=sub style=margin:0>Hours actively spent on each task (continuous same-task scans, breaks removed). Answers &ldquo;how much of the week did each person spend picking vs packing vs engraving.&rdquo; A floor, not a full timesheet.</div>
    <div id=speed_hours style=margin-top:8px></div>
  </div>
  <div class=card style=margin-top:16px>
    <h2>Who&rsquo;s best at what &mdash; assignment matrix</h2>
    <div class=sub style=margin:0>Each cell = units/active-hr with a percentile bar within that activity (green = fast). <b>Best fit</b> = the activity where the person ranks highest. Grey dot = has some data but not enough to rank; blank = never did it.</div>
    <div id=speed_matrix style=margin-top:8px></div>
  </div>
</div>

<div id=watch class=hide><div id=watch_body></div></div>

<div id=plan class=hide>
  <div class=card>
    <h2>Day plan <span style="color:#9ca3af;font-weight:400">&mdash; who&rsquo;s in, where they go, and will we clear the orders</span></h2>
    <div class=sub style=margin:0>Set who&rsquo;s on the floor and their hours (10 = a full day), and a realistic <b>Utilization %</b> (nobody runs at peak pace for 10 hours straight). <b>Auto-assign</b> pours every person-hour into whatever station is the current bottleneck, using each person&rsquo;s own all-time pace &mdash; so engraving only draws the few hours its demand needs and every spare hour (over-capacity pickers, extra engravers) cascades to the bottleneck. It will <b>split</b> a person&rsquo;s day when that&rsquo;s optimal (e.g. 2h engrave + 8h pack, shown next to their output). Override any <b>station</b>, flip <b>in/out</b>, or change <b>hours</b> and it recomputes live. Restock / receiving / returns &rarr; <b>Other</b>.</div>
    <div class=planbar>
      <div class=lf><label>Order target</label><input id=pl_orders type=number min=0 step=10 oninput=planCompute()></div>
      <div class=lf><label>Default hours</label><input id=pl_defhrs type=number min=1 max=14 step=0.5 value=10></div>
      <div class=lf><label>Utilization %</label><input id=pl_util type=number min=10 max=100 step=5 value=80 oninput=planCompute() title="Nobody works at peak pace for 10 hours straight — this discounts every rate to a realistic sustained level."></div>
      <div class=lf><label>&nbsp;</label><button class=pill onclick="planAllHours()">Set all to default</button></div>
      <div class=lf><label>&nbsp;</label><button class="pill add" onclick="planRecommend()">Auto-assign</button></div>
    </div>
    <div id=plan_verdict></div>
    <div id=plan_stations></div>
    <div id=plan_roster></div>
    <div class=logptr style="margin-top:12px">Rates are each person&rsquo;s own typical pace from <span class=tablink onclick="tab('speed')">Speed &amp; Rankings</span>; a &ldquo;&middot;&rdquo; means we don&rsquo;t have enough data to rate them at that station yet. Watch a person ramp on <span class=tablink onclick="tab('trend')">Trends</span>.</div>
  </div>
</div>

<div id=trend class=hide>
  <div class=card>
    <h2>Individual trends <span style="color:#9ca3af;font-weight:400">&mdash; is this person getting faster over time?</span></h2>
    <div class=sub style=margin:0>One person&rsquo;s pace (items/hr) per activity, using the robust median method (typical time per item, pauses ignored). Switch <b>Weekly</b> (watch a new hire ramp over months) or <b>Daily</b> (see day-by-day improvement across a week). History starts when scan-tracking began.</div>
    <div class=planbar>
      <div class=lf><label>Person</label><select id=tr_person onchange="loadTrendData()"></select></div>
      <div class=lf><label>View</label><select id=tr_gran onchange="loadTrendData()"><option value=week selected>Weekly</option><option value=day>Daily</option></select></div>
      <div class=lf id=tr_wkwrap><label>Weeks</label><select id=tr_weeks onchange="loadTrendData()"><option>8</option><option selected>12</option><option>20</option></select></div>
      <div class=lf id=tr_daywrap style=display:none><label>Days</label><select id=tr_days onchange="loadTrendData()"><option>14</option><option selected>21</option><option>30</option></select></div>
    </div>
    <div id=tr_summary></div>
    <div class=chartwrap style="max-width:840px"><canvas id=trendChart></canvas></div>
    <div id=tr_table></div>
  </div>
</div>

<div class=foot>
  <b>Chart colours:</b> <span class=s-sh>Picked&middot;ShipHero</span>, <span style=color:#16a34a>Packed&middot;ShipHero</span>, <span class=s-shop>Packed&middot;Shopify</span>, <span class=s-eng>Engraved</span> &mdash; these four stack into the <b>Fulfillment</b> bar. <span class=s-repl>Restocked</span> is drawn as its own separate bar (a parallel track, never added into the fulfillment/items total).
</div>
</div>
<script>
const C={pick:'#2563eb',pack:'#16a34a',fulfill:'#d97706',repl:'#7c3aed',engrave:'#0d9488'};
if(window.Chart){Chart.defaults.font.family="'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";Chart.defaults.font.size=12;Chart.defaults.color='#475569';Chart.defaults.plugins.legend.labels.usePointStyle=true;Chart.defaults.plugins.legend.labels.boxWidth=8;Chart.defaults.plugins.legend.labels.boxHeight=8;Chart.defaults.plugins.legend.labels.padding=16;}
let DATA=null, sortKey='items_total', sortDir=-1, chart=null, anTrend=null, anRank=null, anMix=null, anSort='items', anDir=-1;
function etDstr(d){return d.toLocaleDateString('en-CA',{timeZone:'America/New_York'});}   // YYYY-MM-DD in real ET
function etToday(){return etDstr(new Date());}
function etAgo(n){return etDstr(new Date(Date.now()-n*86400000));}
function segval(id){return document.querySelector('#'+id+' button.on').dataset.v;}
function seg(id,v){document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));render();}
document.querySelectorAll('.seg').forEach(s=>s.addEventListener('click',e=>{if(e.target.dataset.v){seg(s.id,e.target.dataset.v);}}));
let curTab='dash';
function tab(t){curTab=t;['dash','dataqc','out','floor','log','plan','trend','engt','an','speed','watch'].forEach(x=>{document.getElementById(x).classList.toggle('hide',x!==t);});
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('on',el.dataset.tab===t));
  // Only show the controls a tab actually uses (Unit/Stage/Source + exports = Dashboard only;
  // Team/Detail = Dashboard/Floor/Engraving/Analytics), so no toggle is ever an inert no-op.
  var showU=(t==='dash'), showTV=(['dash','floor','engt','an'].indexOf(t)>=0);
  var c1=document.getElementById('ctl1');if(c1)c1.style.display=showU?'':'none';
  var c2=document.getElementById('ctl2');if(c2)c2.style.display=showTV?'':'none';
  var sm=document.getElementById('summary');if(sm)sm.style.display=showU?'':'none';
  if(t==='speed')loadSpeed();if(t==='watch')loadWatch();if(t==='floor')loadFloor();if(t==='engt')loadEngraving();if(t==='an')loadAnalytics();if(t==='plan')loadPlanner();if(t==='log')loadLog();if(t==='trend')loadTrend();if(t==='out')loadOutstanding();if(t==='dataqc')loadDataqc();}
// ===== Outstanding orders (the demand-side backlog from ShipHero) =====
let OUT=null, outSort='age', outDir=-1, outFilter='all';
function money(v){return '$'+Math.round(v||0).toLocaleString();}
function oSetSort(k){if(outSort===k)outDir=-outDir;else{outSort=k;outDir=(k==='order'?1:-1);}renderOutstanding();}
function oSetFilt(f){outFilter=f;renderOutstanding();}
async function loadOutstanding(){var host=document.getElementById('out_body');
  try{OUT=await getj('/outstanding');}catch(e){host.innerHTML='<div class=sub>could not load outstanding orders</div>';return;}
  if(!OUT.ready){host.innerHTML='<div class=logptr style="margin-top:12px">The outstanding-orders snapshot hasn&rsquo;t run yet. Once the ShipHero orders snapshot populates, this fills in automatically.</div>';return;}
  renderOutstanding();}
function renderOutstanding(){var o=OUT;
  var agedPct=o.orders?Math.round(100*o.aged/o.orders):0;
  var hero='<div class=planhero style="border-left-color:#d97706;background:linear-gradient(180deg,#fffaf3,#fff)">'+
    '<div><div class=phn style=color:#b45309>'+money(o.value)+'</div><div class=phl>owed &mdash; value of open orders</div></div>'+
    '<div><div class=phn>'+fmt(o.orders)+'</div><div class=phl>orders outstanding</div></div>'+
    '<div class=phd>'+fmt(o.aged)+' <b>aged</b> (3+ days) &middot; '+money(o.aged_value)+'</div></div>';
  // secondary stats line
  var strip='<div class=nrow style="margin:8px 0 2px;gap:20px">'+
    '<span class=sub2><b>'+money(o.avg_value)+'</b> avg order</span>'+
    '<span class=sub2><b>'+(o.avg_age||0).toFixed(1)+'d</b> avg age</span>'+
    '<span class=sub2><b>'+(o.median_age||0).toFixed(1)+'d</b> median age</span>'+
    '<span class=sub2><b>'+(o.oldest_age||0).toFixed(1)+'d</b> oldest</span></div>';
  var cmp='';
  if(o.on_hold)cmp='<div class="plancmp short" style="margin-top:10px"><b>'+fmt(o.on_hold)+'</b> orders ('+money(o.on_hold_value)+') are <b>on hold</b> in ShipHero and can&rsquo;t ship until cleared &mdash; work these first.</div>';
  else if(o.aged>0)cmp='<div class="plancmp short" style="margin-top:10px"><b>'+fmt(o.aged)+'</b> orders are aged 3+ days ('+agedPct+'% of the backlog) &mdash; prioritise the oldest below.</div>';
  else cmp='<div class="plancmp ok" style="margin-top:10px">Nothing aged past 3 days &mdash; the backlog is current.</div>';
  // aging table (with % of orders)
  var maxc=Math.max.apply(null,o.aging.map(function(a){return a.count;}).concat([1]));
  var ag='<div class=sub style="margin:16px 0 6px"><b>Aging</b> &mdash; how long orders have been waiting.</div>'+
    '<table class=plantbl><tr><th style=text-align:left>Age</th><th>Orders</th><th>%</th><th></th><th>Value</th></tr>';
  o.aging.forEach(function(a){var w=Math.round(100*a.count/maxc);var col=a.aged?(a.label.indexOf("7")===0?'#dc2626':'#d97706'):'#16a34a';var pct=o.orders?Math.round(100*a.count/o.orders):0;
    ag+='<tr'+(a.aged?' class=plbn':'')+'><td style=text-align:left>'+a.label+(a.aged?' <span class=s style=color:#b45309>aged</span>':'')+'</td>'+
      '<td><b>'+fmt(a.count)+'</b></td>'+
      '<td class=sub2>'+pct+'%</td>'+
      '<td style="width:180px"><div style="background:#eef2f7;border-radius:5px;height:14px;overflow:hidden"><div style="height:100%;width:'+w+'%;background:'+col+'"></div></div></td>'+
      '<td>'+money(a.value)+'</td></tr>';});
  ag+='</table>';
  // on-hold table (dedicated)
  var hold='';
  if(o.on_hold_orders&&o.on_hold_orders.length){
    hold='<div class=sub style="margin:18px 0 6px"><b>On hold</b> ('+fmt(o.on_hold)+') &mdash; flagged in ShipHero (address / payment / fraud / operator); can&rsquo;t ship until cleared.</div>'+
      '<div class=tablewrap><table><tr><th style=text-align:left>Order</th><th>Age</th><th style=text-align:left>Hold reason</th><th>Value</th></tr>'+
      o.on_hold_orders.map(function(r){var ac=r.age_days>=7?'ured':(r.age_days>=3?'uamb':'ugrn');
        return '<tr><td class=name style=text-align:left>'+esc(r.order)+'</td>'+
          '<td><span class='+ac+'>'+r.age_days.toFixed(1)+'d</span></td>'+
          '<td style=text-align:left>'+esc(r.hold||'—')+'</td>'+
          '<td>'+money(r.value)+'</td></tr>';}).join('')+
      '</table></div>';}
  // status breakdown
  var st='';
  if(o.status&&o.status.length>1){st='<div class=sub style="margin:16px 0 6px"><b>By status</b></div><div class=lineup>'+
    o.status.map(function(s){return '<div class=lncol><div class=lnh>'+esc(s.status)+'</div><div class=lnrow><span>'+fmt(s.count)+' orders</span><b>'+money(s.value)+'</b></div></div>';}).join('')+'</div>';}
  // oldest orders (filterable + sortable)
  function oth(k,label,align){var ar=(outSort===k)?(outDir>0?' ▲':' ▼'):'';
    return '<th onclick="oSetSort(\''+k+'\')" style="cursor:pointer'+(align?';text-align:'+align:'')+'">'+label+ar+'</th>';}
  var rows=o.oldest.slice();
  if(outFilter==='aged')rows=rows.filter(function(r){return r.age_days>=3;});
  else if(outFilter==='hold')rows=rows.filter(function(r){return r.on_hold;});
  rows.sort(function(a,b){if(outSort==='order')return (a.order<b.order?-1:a.order>b.order?1:0)*outDir;
    var av=(outSort==='value'?a.value:a.age_days),bv=(outSort==='value'?b.value:b.age_days);return (av-bv)*outDir;});
  var chips='<div class=nrow style="margin:6px 0 8px">'+
    ['all','aged','hold'].map(function(f){var lbl={all:'All',aged:'Aged 3+ d',hold:'On hold'}[f];
      return '<button class="pill'+(outFilter===f?' on':'')+'" onclick="oSetFilt(\''+f+'\')">'+lbl+'</button>';}).join('')+'</div>';
  var ol='<div class=sub style="margin:18px 0 4px"><b>Oldest open orders</b> &mdash; work these down first. Click a column header to sort.</div>'+chips+
    '<div class=tablewrap><table><tr>'+oth('order','Order','left')+oth('age','Age')+oth('value','Value')+'<th style=text-align:left>Status</th></tr>'+
    rows.map(function(r){var ac=r.age_days>=7?'ured':(r.age_days>=3?'uamb':'ugrn');
      return '<tr><td class=name style=text-align:left>'+esc(r.order)+'</td>'+
        '<td><span class='+ac+'>'+r.age_days.toFixed(1)+'d</span></td>'+
        '<td>'+money(r.value)+'</td>'+
        '<td style=text-align:left>'+esc(r.status||'')+(r.on_hold?' <span class=s style=color:#dc2626>on hold'+(r.hold?' ('+esc(r.hold)+')':'')+'</span>':'')+'</td></tr>';}).join('')+
    '</table></div>'+(rows.length?'':'<div class=sub2 style="padding:8px 2px">No orders match this filter.</div>');
  // staleness-aware snapshot stamp
  var stamp='';
  if(o.snapshot_at){var ageMin=(Date.now()-new Date(o.snapshot_at).getTime())/60000;
    var human=ageMin<60?Math.round(ageMin)+' min':(ageMin/60).toFixed(1)+' h';var stale=ageMin>120;
    stamp='<div class=sub style="margin-top:14px">Snapshot '+human+' ago &middot; '+new Date(o.snapshot_at).toLocaleString()+
      (stale?' &middot; <span style="color:#b45309;font-weight:600">may be stale &mdash; auto-refresh pending</span>':'')+'.</div>';}
  document.getElementById('out_body').innerHTML=hero+strip+cmp+ag+hold+st+ol+stamp;}
// ===== Daily report =====
let DAILY=null, dailyKey=null, dailyChart=null;
const DOW=['','Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
async function loadDaily(){var host=document.getElementById('daily_body');
  const f=document.getElementById('from').value,t=document.getElementById('to').value,k=f+'|'+t;
  if(DAILY&&dailyKey===k){renderDaily();return;}
  host.innerHTML='<div class=sub>loading&hellip;</div>';
  try{DAILY=await getj('/daily?from='+f+'&to='+t);dailyKey=k;}catch(e){host.innerHTML='<div class=sub>could not load daily report</div>';return;}
  renderDaily();}
function renderDaily(){if(!DAILY)return;const D=DAILY.days||[];const host=document.getElementById('daily_body');if(!host)return;
  if(!D.length){host.innerHTML='<div class=sub>No activity in this range.</div>';if(dailyChart){dailyChart.destroy();dailyChart=null;}return;}
  let h='<div class=tablewrap><table><tr><th style=text-align:left>Day</th><th>People</th><th>Orders shipped</th><th>Fulfillment</th><th>Pick</th><th>Pack</th><th>Engrave</th><th>Restock</th><th>Active h</th><th>UPLH</th></tr>';
  const T={people:0,shipped:0,ful:0,pick:0,pack:0,eng:0,rest:0,hrs:0};
  D.forEach(function(r){
    h+='<tr><td class=name style=text-align:left>'+DOW[r.dow]+' '+r.d.slice(5)+'</td>'+
      '<td>'+fmt(r.people)+'</td><td><b>'+fmt(r.shipped)+'</b></td><td><b>'+fmt(r.fulfillment)+'</b></td>'+
      '<td>'+fmt(r.pick)+'</td><td>'+fmt(r.pack)+'</td><td>'+fmt(r.engrave)+'</td><td>'+fmt(r.restock)+'</td>'+
      '<td>'+r.hours.toFixed(1)+'</td><td><b>'+fmt(r.uplh)+'</b></td></tr>';
    T.people+=r.people;T.shipped+=r.shipped;T.ful+=r.fulfillment;T.pick+=r.pick;T.pack+=r.pack;T.eng+=r.engrave;T.rest+=r.restock;T.hrs+=r.hours;});
  const nd=D.length;
  h+='<tr class=tot><td>Total ('+nd+'d)</td><td>'+Math.round(T.people/nd)+'<span class=sub2> avg</span></td><td><b>'+fmt(T.shipped)+'</b></td><td><b>'+fmt(T.ful)+'</b></td><td>'+fmt(T.pick)+'</td><td>'+fmt(T.pack)+'</td><td>'+fmt(T.eng)+'</td><td>'+fmt(T.rest)+'</td><td>'+T.hrs.toFixed(1)+'</td><td><b>'+fmt(T.hrs>0?Math.round((T.ful+T.rest)/T.hrs):0)+'</b></td></tr></table></div>';
  host.innerHTML=h;
  if(curTab!=='daily')return;
  const labels=D.map(r=>DOW[r.dow]+' '+r.d.slice(5));
  if(dailyChart)dailyChart.destroy();
  dailyChart=new Chart(document.getElementById('daily_chart'),{data:{labels:labels,datasets:[
    {type:'bar',label:'Fulfillment items',data:D.map(r=>r.fulfillment),backgroundColor:C.fulfill,yAxisID:'y',order:3},
    {type:'line',label:'Orders shipped',data:D.map(r=>r.shipped),borderColor:C.pack,backgroundColor:C.pack,yAxisID:'y1',tension:.3,pointRadius:3,order:1},
    {type:'line',label:'UPLH',data:D.map(r=>r.uplh),borderColor:'#0f172a',backgroundColor:'#0f172a',yAxisID:'y1',tension:.3,pointRadius:2,borderDash:[4,3],order:2}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      scales:{x:{grid:{display:false},ticks:{font:{size:10}}},
        y:{beginAtZero:true,position:'left',grid:{color:'#eef1f5'},title:{display:true,text:'Fulfillment items / day',color:'#64748b',font:{size:11,weight:'600'}}},
        y1:{beginAtZero:true,position:'right',grid:{display:false},title:{display:true,text:'Orders / UPLH',color:'#64748b',font:{size:11,weight:'600'}}}},
      plugins:{legend:{position:'bottom'},title:{display:true,text:'Daily output, orders shipped & UPLH',color:'#0f172a',font:{size:13,weight:'600'}}}}});
}
// ===== Data issues & warnings =====
let DATAQC=null;
async function loadDataqc(){var host=document.getElementById('dataqc_body');
  host.innerHTML='<div class=sub>running checks&hellip;</div>';
  try{DATAQC=await getj('/dataqc');}catch(e){host.innerHTML='<div class=sub>could not run data checks</div>';return;}
  renderDataqc();}
function renderDataqc(){if(!DATAQC)return;var q=DATAQC;var host=document.getElementById('dataqc_body');if(!host)return;
  var fm=q.last_min_ago;var fcol=fm==null?'#64748b':(fm<=15?'#15803d':(fm<=60?'#b45309':'#b91c1c'));
  var out='<div class=nrow style="margin:6px 0 2px;gap:20px">'+
    '<span class=sub2>Data freshness: <b style="color:'+fcol+'">'+(fm==null?'no data':(fm+' min ago'))+'</b></span>'+
    '<span class=sub2>Now: <b>'+q.today_hm+'</b> &middot; '+q.today+'</span></div>';
  if((q.unidentified||[]).length){out+='<div class=sub style="margin:16px 0 6px"><b>Unidentified people</b> &mdash; source scans not matched to an employee. Nobody is named a number &mdash; map them in known_aliases.py (or tell me who they are).</div>'+
    '<div class=tablewrap><table><tr><th style=text-align:left>Raw identity</th><th>Decoded</th><th>Source</th><th>Events</th><th>Last seen</th></tr>'+
    q.unidentified.map(function(u){return '<tr><td class=name style=text-align:left>'+esc(u.person)+'</td><td class=sub2>'+esc(u.hint||'')+'</td><td class=sub2>'+esc(u.sources||'')+'</td><td><b>'+fmt(u.events)+'</b></td><td class=sub2>'+esc(u.last||'')+'</td></tr>';}).join('')+
    '</table></div>';}
  if(q.is_weekend)out+='<div class="plancmp ok" style="margin-top:10px">Today is a weekend &mdash; low or no activity is expected.</div>';
  else out+='<div class="plancmp short" style="margin-top:10px"><b>Today is in progress</b> (as of '+q.today_hm+'). Today&rsquo;s numbers are partial &mdash; a low &ldquo;today&rdquo; is almost always just the day not being over, not a real collapse. Compare completed days on the <span class=tablink onclick="tab(\'daily\')">Daily</span> tab.</div>';
  if(q.today_partial&&q.today_partial.length){
    out+='<div class=sub style="margin:16px 0 6px"><b>Today so far vs each person&rsquo;s typical day</b> &mdash; context for &ldquo;why is X so low today.&rdquo;</div>'+
      '<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Today (fulfillment)</th><th>Typical day</th><th>% of typical</th><th>Last scan</th></tr>';
    q.today_partial.forEach(function(r){var pc=r.pct;var col=pc==null?'#64748b':(pc<40?'#b45309':'#334155');
      out+='<tr><td class=name style=text-align:left>'+esc(r.person)+'</td><td><b>'+fmt(r.today)+'</b></td><td class=sub2>'+fmt(r.median)+'</td>'+
        '<td><b style="color:'+col+'">'+(pc==null?'&mdash;':pc+'%')+'</b></td><td class=sub2>'+(r.last||'&middot;')+'</td></tr>';});
    out+='</table></div>';}
  out+='<div class=sub style="margin:18px 0 6px"><b>Shared login / two machines &mdash; what we can and can&rsquo;t see</b></div>'+
    '<div class="plancmp" style="background:#f8fafc;border-left-color:#94a3b8"><div style="line-height:1.55">ShipHero records <b>who</b> scanned (the badge), <b>which order</b>, <b>which warehouse</b>, and <b>when</b> &mdash; but <b>not which workstation/machine</b> a scan came from. That means timing alone <b>cannot</b> tell a shared badge (two people on one login) apart from one fast person clearing a queue &ldquo;pack, pack, pack&rdquo; or doing a normal <b>batch pick</b> across several orders &mdash; all three look identical in the data. So we deliberately <b>don&rsquo;t flag it here</b>, rather than risk pointing at your fastest people. If you suspect a login is shared, the dependable check is the actual device sign-ins. The day this scan data ever carries a workstation/terminal id, this page can turn it into a real, trustworthy flag.</div></div>';
  out+='<div class=sub style="margin:18px 0 6px"><b>Day-over-day anomalies</b> (completed days only) &mdash; a person&rsquo;s most recent finished day that&rsquo;s far off their own recent baseline. Worth a sanity-check: could be a genuinely slow/heavy day, PTO, or a data gap.</div>';
  if(!(q.anomalies&&q.anomalies.length))out+='<div class=sub2>None &mdash; every completed day is within a normal range of each person&rsquo;s baseline.</div>';
  else{out+='<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Day</th><th>Fulfillment</th><th>Their baseline</th><th>% of baseline</th><th>Flag</th></tr>';
    q.anomalies.forEach(function(a){var drop=a.kind==='drop';
      out+='<tr><td class=name style=text-align:left>'+esc(a.person)+'</td><td>'+a.d.slice(5)+'</td><td><b>'+fmt(a.ful)+'</b></td><td class=sub2>'+fmt(a.base)+'</td>'+
        '<td><b style="color:'+(drop?'#b91c1c':'#b45309')+'">'+a.pct+'%</b></td><td><span class="wchip '+(drop?'r':'a')+'">'+(drop?'sharp drop':'spike')+'</span></td></tr>';});
    out+='</table></div>';}
  host.innerHTML=out;}
function dateEdit(){document.querySelectorAll('.pill[data-preset]').forEach(b=>b.classList.remove('on'));load();}   // manual date change: drop preset highlight + reload
function preset(p){document.querySelectorAll('.pill[data-preset]').forEach(b=>b.classList.toggle('on',b.dataset.preset===p));
  let f=etToday(),t=etToday();
  if(p==='yest'){f=t=etAgo(1);}else if(p==='7'){f=etAgo(6);}else if(p==='30'){f=etAgo(29);}
  else if(p==='week'){const d=new Date(Date.now()-4*3600*1000);f=etAgo((d.getUTCDay()+6)%7);}
  document.getElementById('from').value=f;document.getElementById('to').value=t;load();}
async function getj(u){for(let i=0;i<8;i++){try{const r=await fetch(u);if(r.ok)return await r.json();}catch(e){}
  document.getElementById('status').textContent='waking server ('+(i+1)+')…';await new Promise(s=>setTimeout(s,4000));}throw 0;}
async function load(){document.getElementById('status').textContent='loading…';
  const f=document.getElementById('from').value,t=document.getElementById('to').value;
  try{DATA=await getj('/warehouse?from='+f+'&to='+t);
  try{FLOOR=await getj('/floor?from='+f+'&to='+t);floorKey=f+'|'+t;}catch(e){FLOOR=null;}  // hours/days for the detail table
  document.getElementById('status').textContent='';render();
  if(!document.getElementById('speed').classList.contains('hide')){speedKey=null;loadSpeed();}
  if(!document.getElementById('watch').classList.contains('hide')){watchKey=null;loadWatch();}
  }catch(e){document.getElementById('status').textContent='could not reach API';}}
// ---- Auto-refresh (leave it on a wall / TV): re-pull live data on an interval, no full page reload ----
let autoTimer=null, wakeLock=null;
function stampRefresh(){var el=document.getElementById('refstamp');if(el)el.textContent='updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});}
async function refreshNow(){
  var a=document.activeElement;   // don't rebuild / steal focus while someone is editing an input
  if(a&&/^(INPUT|SELECT|TEXTAREA)$/.test(a.tagName)&&a.id!=='autoref'&&a.id!=='autoint')return;
  speedKey=null;floorKey=null;watchKey=null;engKey=null;pspeedKey=null;
  await load();
  if(curTab==='engt')loadEngraving(); else if(curTab==='an')loadAnalytics();
  else if(curTab==='plan')loadPlanner(); else if(curTab==='trend')loadTrendData(); else if(curTab==='log')loadLog();
  else if(curTab==='out')loadOutstanding(); else if(curTab==='dataqc')loadDataqc();
  stampRefresh();
}
async function reqWake(){try{if('wakeLock' in navigator)wakeLock=await navigator.wakeLock.request('screen');}catch(e){}}
function relWake(){try{if(wakeLock){wakeLock.release();wakeLock=null;}}catch(e){}}
document.addEventListener('visibilitychange',function(){if(document.visibilityState==='visible'&&autoTimer)reqWake();});
function toggleAuto(){
  var on=document.getElementById('autoref').checked, mins=parseInt(document.getElementById('autoint').value,10)||5;
  try{localStorage.setItem('wh_auto',on?mins:'0');}catch(e){}
  if(autoTimer){clearInterval(autoTimer);autoTimer=null;}
  if(on){autoTimer=setInterval(refreshNow, mins*60*1000);reqWake();refreshNow();}
  else{relWake();var el=document.getElementById('refstamp');if(el)el.textContent='';}
}
function initAuto(){try{var v=parseInt(localStorage.getItem('wh_auto')||'0',10);
  if(v>0){document.getElementById('autoref').checked=true;var s=document.getElementById('autoint');if(s&&[2,5,15].indexOf(v)>=0)s.value=v;toggleAuto();return true;}}catch(e){}return false;}
function fmt(n){return (n||0).toLocaleString();}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function ampm(iso){if(!iso)return '';return new Date(iso).toLocaleTimeString([], {hour:'numeric',minute:'2-digit',timeZone:'America/New_York'})+' ET';}
function teamFilter(p){const t=segval('team');return t==='all'||p.type===t;}
// which fulfillment components are visible for the current Stage/Source toggles
function vis(){const stage=segval('stage'),src=segval('source');return{
  pick:  (stage==='all'||stage==='pick')    && src!=='shopify',
  packsh:(stage==='all'||stage==='pack')    && src!=='shopify',
  packshop:(stage==='all'||stage==='pack')  && src!=='shiphero',
  eng:   (stage==='all'||stage==='engrave') && src!=='shopify',
  repl:  (stage==='all'||stage==='repl')    && src!=='shopify'};}
// visible fulfillment ITEMS total for a person (pick+pack+engrave) — this is the fulfillment bar height
function fulItems(p,v){return (v.pick?p.items_picked_sh:0)+(v.packsh?p.items_packed_sh:0)+(v.packshop?p.items_packed_shop:0)+(v.eng?p.engraved_items:0);}
function fulOrders(p,v){return (v.pick?p.orders_picked_sh:0)+(v.packsh?p.orders_packed_sh:0)+(v.packshop?p.orders_packed_shop:0)+(v.eng?(p.engraved_orders||0):0);}
function render(){if(!DATA)return;
  const unit=segval('unit'),v=vis();
  const ppl=DATA.people.filter(teamFilter);
  const sh=DATA.shipped, T=DATA.totals;
  const actItems=T.pk_i+T.packsh_i+T.packshop_i+T.eng_i;
  document.getElementById('summary').innerHTML=ppl.length+' people &middot; <b>'+fmt(sh.total)+'</b> orders shipped &middot; '+
    fmt(actItems)+' fulfillment items ('+fmt(T.pk_i)+' picked + '+fmt(T.packsh_i)+' packed·SH + '+fmt(T.packshop_i)+' packed·Shopify + '+fmt(T.eng_i)+' engraved) &middot; '+fmt(T.repl)+' restocked &middot; '+DATA.range.from;
  document.getElementById('shipped').innerHTML='<div class=big>'+fmt(sh.total)+'</div><div class=t>orders shipped out the door</div>'+
    '<div class=d><b>'+fmt(sh.shiphero)+'</b> ShipHero &middot; <span class=o>'+fmt(sh.shopify_only)+'</span> Shopify-only ('+fmt(sh.both)+' ShipHero orders were finished by hand in Shopify)</div>';
  // ---- stat cards (honor toggles) ----
  const showItems=unit!=='orders', showOrders=unit!=='items';
  const itemsSel=(v.pick?T.pk_i:0)+(v.packsh?T.packsh_i:0)+(v.packshop?T.packshop_i:0)+(v.eng?T.eng_i:0);
  const ordersSel=(v.pick?T.pk_o:0)+(v.packsh?T.packsh_o:0)+(v.packshop?T.packshop_o:0)+(v.eng?(T.eng_o||0):0);
  const si=document.getElementById('statsItems'), so=document.getElementById('statsOrders');
  si.innerHTML=!showItems?'':[
    card('Items picked','ShipHero','s-sh',v.pick?T.pk_i:0),
    card('Items packed','ShipHero','s-pksh',v.packsh?T.packsh_i:0),
    card('Items packed','Shopify','s-shop',v.packshop?T.packshop_i:0),
    card('Items engraved','logger','s-eng',v.eng?T.eng_i:0),
    card('Items — total','fulfillment','s-sel',itemsSel),
    card('Restocked','units · incl. tote moves','s-repl',v.repl?T.repl:0)].join('');
  so.innerHTML=!showOrders?'':[
    card('Orders picked','ShipHero','s-sh',v.pick?T.pk_o:0),
    card('Orders packed','ShipHero','s-pksh',v.packsh?T.packsh_o:0),
    card('Orders packed','Shopify','s-shop',v.packshop?T.packshop_o:0),
    card('Order-lines','pick+pack+engrave, not distinct','s-sel',ordersSel)].join('');
  si.classList.toggle('hide',!showItems);so.classList.toggle('hide',!showOrders);
  drawChart(ppl,v);
  drawDetail(ppl,unit,v);
  fillTV(ppl,T,sh);
  if(FLOOR){renderFloor();renderAnalytics();}
  if(ENGR)renderEngraving();
}
// the approved TV hero: centered date + 4 consolidated tiles (replaces the 6 by-type cards in clean mode)
function fillTV(ppl,T,sh){
  if(!DATA)return;
  var r=(DATA.range.from||'').split('-'), dt=document.getElementById('tvdate');
  if(dt&&r.length===3){var d=new Date(+r[0],+r[1]-1,+r[2]);
    dt.textContent=d.toLocaleDateString('en-US',{weekday:'long',month:'long',day:'numeric'});}
  var ds=document.getElementById('tvdsub');
  if(ds){var one=DATA.range.from===DATA.range.to;
    ds.textContent=one?(DATA.range.from===etToday()?'Today':DATA.range.from):(DATA.range.from+' → '+DATA.range.to);}
  var rs=document.getElementById('tvrefstamp');
  if(rs)rs.textContent='updated '+new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  var host=document.getElementById('tvkpis'); if(!host)return;
  var ful=T.pk_i+T.packsh_i+T.packshop_i+T.eng_i;
  var working=ppl.filter(function(p){return p.type;}).length;   // matched staff on the floor (excludes unmatched scanner IDs)
  host.innerHTML=
    '<div class="kpi hero"><div class=acc style="background:var(--green)"></div><div class=kl>Orders shipped out the door</div><div class=kv>'+fmt(sh.total)+'</div>'+
      '<div class=kn><span><span class=sw style="background:var(--accent)"></span>'+fmt(sh.shiphero)+' ShipHero</span><span><span class=sw style="background:var(--amber)"></span>'+fmt(sh.shopify_only)+' Shopify</span></div></div>'+
    '<div class=kpi><div class=acc style="background:var(--accent)"></div><div class=kl>Items fulfilled</div><div class=kv>'+fmt(ful)+'</div><div class=kn>pick + pack + engrave</div></div>'+
    '<div class=kpi><div class=acc style="background:var(--violet)"></div><div class=kl>Items restocked</div><div class=kv>'+fmt(T.repl)+'</div><div class=kn>replenishment &middot; separate track</div></div>'+
    '<div class=kpi><div class=acc style="background:var(--teal)"></div><div class=kl>People working</div><div class=kv>'+working+'</div><div class=kn>on the floor today</div></div>';
}
function card(k,s,cls,val){return '<div class="card stat"><div class=k>'+k+' <span class="s '+cls+'">'+s+'</span></div><div class=v>'+fmt(val)+'</div></div>';}
function drawChart(ppl,v){
  const DK=document.body.classList.contains('dark');
  const C_AX=DK?'#c3c2b7':'#64748b', C_GRID=DK?'#2c2c2a':'#eef1f5', C_TTL=DK?'#f5f5f0':'#0f172a', C_LBL=DK?'#e2e2dc':'#334155';
  const ord=segval('unit')==='orders';            // chart follows the Items/Orders toggle
  const val=(p,c)=>{
    if(ord){ if(c==='pick')return v.pick?p.orders_picked_sh:0;
             if(c==='packsh')return v.packsh?p.orders_packed_sh:0;
             if(c==='packshop')return v.packshop?p.orders_packed_shop:0;
             if(c==='eng')return v.eng?(p.engraved_orders||0):0; return 0; }
    if(c==='pick')return v.pick?p.items_picked_sh:0;
    if(c==='packsh')return v.packsh?p.items_packed_sh:0;
    if(c==='packshop')return v.packshop?p.items_packed_shop:0;
    if(c==='eng')return v.eng?p.engraved_items:0;
    if(c==='repl')return v.repl?p.replenished:0; return 0; };
  const ftot=p=>val(p,'pick')+val(p,'packsh')+val(p,'packshop')+val(p,'eng');
  const arr=[...ppl].sort((a,b)=>ftot(b)-ftot(a));  // most -> least, left -> right
  const labels=arr.map(p=>p.person);
  const ulbl=ord?'orders':'items';
  const ds=[
    {label:'Picked · ShipHero',stack:'ful',backgroundColor:C.pick,data:arr.map(p=>val(p,'pick'))},
    {label:'Packed · ShipHero',stack:'ful',backgroundColor:C.pack,data:arr.map(p=>val(p,'packsh'))},
    {label:'Packed · Shopify',stack:'ful',backgroundColor:C.fulfill,data:arr.map(p=>val(p,'packshop'))},
    {label:'Engraved',stack:'ful',backgroundColor:C.engrave,data:arr.map(p=>val(p,'eng'))}];
  if(!ord) ds.push({label:'Restocked (separate track)',stack:'repl',backgroundColor:C.repl,data:arr.map(p=>val(p,'repl'))});
  // plugin: print each bar's stack total just above it, so the numbers are readable at a glance
  const stackTotals={id:'stackTotals',afterDatasetsDraw(ch){
    const ctx=ch.ctx, groups={};
    ch.data.datasets.forEach((d,di)=>{(groups[d.stack]=groups[d.stack]||[]).push(di);});
    ctx.save();ctx.font='700 11px Inter';ctx.textAlign='center';ctx.textBaseline='bottom';
    ch.data.labels.forEach((_,i)=>{Object.values(groups).forEach(dis=>{
      let sum=0,topY=1e9,x=null;
      dis.forEach(di=>{const vv=ch.data.datasets[di].data[i]||0,bar=ch.getDatasetMeta(di).data[i];
        if(vv>0&&bar){sum+=vv;if(bar.y<topY)topY=bar.y;x=bar.x;}});
      if(sum>0&&x!=null){ctx.fillStyle=C_LBL;ctx.fillText(sum.toLocaleString(),x,topY-3);}});});
    ctx.restore();}};
  if(chart)chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'bar',data:{labels,datasets:ds},
    options:{responsive:true,maintainAspectRatio:false,layout:{padding:{top:24}},
      scales:{x:{stacked:true,grid:{display:false},ticks:{autoSkip:false,maxRotation:40,font:{size:11},color:C_AX}},
              y:{stacked:true,beginAtZero:true,grid:{color:C_GRID},ticks:{color:C_AX},
                 title:{display:true,text:(ord?'Orders':'Items')+' fulfilled',color:C_AX,font:{size:11,weight:'600'}}}},
      plugins:{
        title:{display:true,text:'Fulfillment '+ulbl+' per person — tallest first'+((v.repl&&!ord)?'  (Restocked shown as a separate bar)':''),
               color:C_TTL,font:{size:13,weight:'600'},padding:{bottom:12}},
        legend:{position:'bottom',labels:{color:C_AX}},
        tooltip:{callbacks:{footer:(items)=>{let f=0;items.forEach(i=>{if(i.dataset.stack==='ful')f+=i.parsed.y;});
          return f?'Fulfillment '+ulbl+': '+f.toLocaleString():'';}}}}},
    plugins:[stackTotals]});
}
function drawDetail(ppl,unit,v){
  // Three visually separated groups: ON THE CLOCK (days + hours, merged from /floor so you can tell
  // whether someone leads because they worked more time), FULFILLMENT (pick+pack+engrave, items+orders),
  // and RESTOCKING (separate). Every header is click-to-sort.
  const det=DET();
  const teamItems=ppl.reduce((a,p)=>a+fulItems(p,v),0)||1;
  const teamRestock=ppl.reduce((a,p)=>a+(v.repl?p.replenished:0),0)||1;
  const fmap={}; if(FLOOR&&FLOOR.people)FLOOR.people.forEach(p=>fmap[p.person]=p);
  const wd=(FLOOR&&FLOOR.work_days)||0;
  const arr=ppl.map(p=>{const fl=fmap[p.person]||{};return {person:p.person, type:p.type,
    items:fulItems(p,v), orders:fulOrders(p,v), restock:(v.repl?p.replenished:0),
    _pick:(v.pick?p.items_picked_sh:0),
    _pack:(v.packsh?p.items_packed_sh:0)+(v.packshop?p.items_packed_shop:0),
    _eng:(v.eng?p.engraved_items:0),
    worked_h:(fl.hours||0), span_h:(fl.span_h||0),
    active_days:(fl.active_days!=null?fl.active_days:(p.active_days||0))};});
  arr.forEach(p=>{p.share=Math.round(p.items/teamItems*100); p.rshare=Math.round(p.restock/teamRestock*100);});
  const K={items_total:'items',orders_total:'orders',replenished:'restock',share:'share',rshare:'rshare'}[sortKey]||sortKey;
  arr.sort((a,b)=>{const x=a[K],y=b[K];return (x>y?1:(x<y?-1:0))*sortDir;});
  const arw=k=>sortKey===k?'<span class=arw>'+(sortDir<0?'▼':'▲')+'</span>':'';
  const th=(k,lab,sub)=>'<th class="srt'+(sortKey===k?' act':'')+'" onclick="sortBy(\''+k+'\')">'+lab+(sub?' <span class=s>'+sub+'</span>':'')+arw(k)+'</th>';
  const nful=det?6:3;   // items,[pick,pack,eng],share,orders
  // sortable header with a per-group tint class (gct/gcf/gcr) so the whole column band is coloured
  const thg=(g,k,l,s)=>'<th class="srt '+g+(sortKey===k?' act':'')+'" onclick="sortBy(\''+k+'\')">'+l+(s?' <span class=s>'+s+'</span>':'')+arw(k)+'</th>';
  // group-title header row
  let h='<table><tr class=grp>'+
    '<th></th><th></th>'+
    '<th colspan=3 class="gh gt">On the clock</th>'+
    '<th class=gsep></th>'+
    '<th colspan='+nful+' class="gh gf">Fulfillment</th>'+
    '<th class=gsep></th>'+
    '<th colspan=2 class="gh gr">Restocking</th>'+
    '</tr>';
  // column header row
  h+='<tr>'+th('person','Person','')+
    '<th class=srt onclick="sortBy(\'type\')">Type'+arw('type')+'</th>'+
    thg('gct','active_days','Days',(wd?'of '+wd:'active'))+
    thg('gct','span_h','Hours','1st&rarr;last')+
    thg('gct','worked_h','Active','breaks out')+
    '<th class=gsep></th>'+
    thg('gcf','items_total','Items','pick+pack+engrave')+
    (det? thg('gcf','_pick','Picked','ShipHero')+thg('gcf','_pack','Packed','SH + Shopify')+thg('gcf','_eng','Engraved','logger') : '')+
    thg('gcf','share','Share','of team')+
    thg('gcf','orders_total','Orders','fulfillment')+
    '<th class=gsep></th>'+
    thg('gcr','replenished','Units','')+
    thg('gcr','rshare','Share','of team')+
    '</tr>';
  const T={items:0,orders:0,restock:0,_pick:0,_pack:0,_eng:0,worked:0,span:0};
  arr.forEach(p=>{
    const dstr=p.active_days?(p.active_days+(wd?' / '+wd:'')):'&mdash;';
    h+='<tr><td class=name>'+esc(p.person)+'</td><td>'+badge(p.type)+'</td>'+
      '<td class=gct>'+dstr+'</td>'+
      '<td class=gct><b>'+(p.span_h?p.span_h.toFixed(1):'&mdash;')+'</b></td>'+
      '<td class="gct sub2">'+(p.worked_h?p.worked_h.toFixed(1):'&mdash;')+'</td>'+
      '<td class=gsep></td>'+
      '<td class="gcf fi"><b>'+fmt(p.items)+'</b></td>'+
      (det? '<td class=gcf>'+fmt(p._pick)+'</td><td class=gcf>'+fmt(p._pack)+'</td><td class="gcf eng">'+fmt(p._eng)+'</td>' : '')+
      '<td class="gcf shr">'+p.share+'%</td>'+
      '<td class=gcf>'+fmt(p.orders)+'</td>'+
      '<td class=gsep></td>'+
      '<td class="gcr p"><b>'+fmt(p.restock)+'</b></td>'+
      '<td class="gcr shr">'+(p.restock?p.rshare+'%':'&mdash;')+'</td></tr>';
    T.items+=p.items;T.orders+=p.orders;T.restock+=p.restock;T._pick+=p._pick;T._pack+=p._pack;T._eng+=p._eng;T.worked+=p.worked_h;T.span+=p.span_h;});
  h+='<tr class=tot><td>Total</td><td></td>'+
    '<td class=gct></td><td class=gct><b>'+(T.span?T.span.toFixed(0):'')+'</b></td><td class="gct sub2">'+(T.worked?T.worked.toFixed(0):'')+'</td>'+
    '<td class=gsep></td>'+
    '<td class=gcf><b>'+fmt(T.items)+'</b></td>'+
    (det? '<td class=gcf>'+fmt(T._pick)+'</td><td class=gcf>'+fmt(T._pack)+'</td><td class=gcf>'+fmt(T._eng)+'</td>' : '')+
    '<td class=gcf>100%</td><td class=gcf>'+fmt(T.orders)+'</td>'+
    '<td class=gsep></td>'+
    '<td class="gcr p"><b>'+fmt(T.restock)+'</b></td><td class=gcr>100%</td></tr>';
  h+='</table>';
  document.getElementById('detail').innerHTML='<div class=tablewrap>'+h+'</div>';
}
function sortBy(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();}
// ===== Floor Time (effectiveness auditor) & Engraving — lazy-loaded, day-by-day =====
let FLOOR=null,floorKey=null,floorSort='items',floorDir=-1;
let ENGR=null,engKey=null,engSort='items',engDir=-1;
const DOWN=['','Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
function tfilter(p){const t=segval('team');return t==='all'||p.type===t;}
function DET(){return segval('view')==='detailed';}   // Simple vs Detailed view toggle
function badge(ty){var cls=ty==='Intern'?'in':(ty==='Seasonal'?'sea':'ft');var lbl=ty==='Intern'?'Intern':(ty==='Seasonal'?'Seasonal':(ty==='FT'?'Full-timer':(ty?ty:'—')));return '<span class="badge '+cls+'">'+lbl+'</span>';}
function dhead(d){return '<th class="dcol'+(d.dow>=6?' wknd':'')+'">'+DOWN[d.dow]+'<br><span class=s>'+(+d.d.slice(8))+'</span></th>';}
function chip(u){const c=u>=80?'ugrn':(u>=55?'uamb':'ured');return '<span class="'+c+'">'+u+'%</span>';}

async function loadFloor(){const f=document.getElementById('from').value,t=document.getElementById('to').value,k=f+'|'+t;
  if(FLOOR&&floorKey===k){renderFloor();return;}
  document.getElementById('floortable').innerHTML='<div class=sub>loading…</div>';
  try{FLOOR=await getj('/floor?from='+f+'&to='+t);floorKey=k;renderFloor();}
  catch(e){document.getElementById('floortable').innerHTML='<div class=sub>could not load floor data</div>';}}
function floorSortBy(k){if(floorSort===k)floorDir*=-1;else{floorSort=k;floorDir=-1;}renderFloor();}
// Trust & coverage: split each person's on-floor span into scanned work / logged off-scanner / unexplained gap.
function renderCoverage(ppl){var cw=document.getElementById('floorcover');if(!cw)return;
  var rows=ppl.filter(function(p){return (p.span_h||0)>0;}).map(function(p){
    var sp=p.span_h||0, sc=p.hours||0, lg=p.proj_hours||0;
    var unex=Math.max(0, sp-sc-lg), base=Math.max(sp, sc+lg)||1;
    return {person:p.person, sp:sp, sc:sc, lg:lg, unex:unex, base:base, cov:(sp>0?Math.round(100*(sp-unex)/sp):100)};});
  if(!rows.length){cw.innerHTML='';return;}
  rows.sort(function(a,b){return b.unex-a.unex;});   // biggest unexplained gap first — who needs logging
  var tSp=rows.reduce(function(a,r){return a+r.sp;},0), tUn=rows.reduce(function(a,r){return a+r.unex;},0), tLg=rows.reduce(function(a,r){return a+r.lg;},0);
  var tCov=tSp>0?Math.round(100*(tSp-tUn)/tSp):100, col=function(c){return c>=85?'g':(c>=65?'a':'r');};
  var h='<div class=covwrap><div class=covhead>Time accounted for</div>'+
    '<div class=covsum><b>'+tCov+'%</b> of on-floor time is accounted for — scanned work or logged off-scanner. <b>'+tUn.toFixed(1)+'h</b> still unexplained'+(tLg>0?', <b>'+tLg.toFixed(1)+'h</b> logged so far':'')+'. The more red a bar shows, the more of that person&rsquo;s day the data can&rsquo;t see &mdash; log it below to close the gap.</div>'+
    '<div class=covlegend><span class=covkey><i class=cov-sc></i>Scanned work</span><span class=covkey><i class=cov-lg></i>Logged off-scanner</span><span class=covkey><i class=cov-un></i>Unexplained gap</span></div>';
  rows.forEach(function(r){var w=function(x){return (100*x/r.base).toFixed(1);};
    h+='<div class=covrow><div class=covname>'+esc(r.person)+'</div>'+
      '<div class="covpct '+col(r.cov)+'">'+r.cov+'%</div>'+
      '<div class=covbar><i class=cov-sc style="width:'+w(r.sc)+'%"></i><i class=cov-lg style="width:'+w(r.lg)+'%"></i><i class=cov-un style="width:'+w(r.unex)+'%"></i></div>'+
      '<div class=covmeta><b>'+r.sp.toFixed(1)+'h</b> on floor'+(r.unex>0.05?' &middot; '+r.unex.toFixed(1)+'h gap <span class=covlog onclick="covLogFor(\''+encodeURIComponent(r.person)+'\')">&#43; log</span>':' &middot; <span style="color:#15803d">clear</span>')+'</div></div>';});
  h+='</div>';cw.innerHTML=h;}
function covLogFor(person){person=decodeURIComponent(person);tab('log');var sel=document.getElementById('n_person');if(sel){sel.value=person;onPersonPick();}}
function renderFloor(){if(!FLOOR)return;
  const days=FLOOR.days,showDays=DET()&&days.length<=16;
  const ppl=FLOOR.people.filter(tfilter);
  ppl.forEach(p=>{p.iphr_floor=((p.span_h||0)>0?Math.round(p.items/p.span_h):0);});   // items per FLOOR hour (span-based)
  renderCoverage(ppl);
  const arr=[...ppl].sort((a,b)=>{const x=a[floorSort],y=b[floorSort];return (x>y?1:(x<y?-1:0))*floorDir;});
  const arw=k=>floorSort===k?'<span class=arw>'+(floorDir<0?'▼':'▲')+'</span>':'';
  const th=(k,l,s)=>'<th class="srt'+(floorSort===k?' act':'')+'" onclick="floorSortBy(\''+k+'\')">'+l+(s?' <span class=s>'+s+'</span>':'')+arw(k)+'</th>';
  let h='<table><tr>'+th('person','Person','')+'<th>Type</th>'+
    '<th>First in</th><th>Last out</th>'+th('active_days','Days','active')+
    th('span_h','Hours','1st&rarr;last')+th('hours','Active','breaks out')+th('avg_span','Hrs/day','1st&rarr;last')+th('util','Util','active÷span')+
    th('items','Items','all work')+th('items_per_day','Items/day','')+th('iphr_floor','Items/hr','per floor-hr')+th('proj_hours','Proj h','off-scanner');
  if(showDays)h+='<th class=dsep></th>'+days.map(dhead).join('');
  h+='</tr>';const T={span:0,hours:0,items:0};const pad='<td></td><td></td>';
  arr.forEach(p=>{const m={};p.days.forEach(x=>m[x.d]=x);const sph=p.span_h||0;
    const projT=(p.notes||[]).map(n=>n.d+' · '+n.hours+'h · '+(n.note||'')).join(' | ').replace(/"/g,'&quot;');
    h+='<tr><td class=name>'+esc(p.person)+'</td><td>'+badge(p.type)+'</td>'+
      '<td>'+(p.first_in||'—')+'</td><td>'+(p.last_out||'—')+'</td>'+
      '<td>'+p.active_days+'</td><td><b>'+sph.toFixed(1)+'</b></td><td class=sub2>'+p.hours.toFixed(1)+'</td><td>'+p.avg_span.toFixed(1)+'</td>'+
      '<td>'+chip(p.util)+'</td><td><b>'+fmt(p.items)+'</b></td><td>'+fmt(p.items_per_day)+'</td><td><b>'+fmt(p.iphr_floor)+'</b></td>'+
      '<td>'+(p.proj_hours?'<span class=projh title="'+projT+'">'+p.proj_hours.toFixed(1)+'</span>':'<span class=dmt>·</span>')+'</td>';
    if(showDays)h+='<td class=dsep></td>'+days.map(d=>{const x=m[d.d];
      if(!x||(!x.span&&!x.ful&&!x.repl))return '<td class="dcell'+(d.dow>=6?' wknd':'')+'"><span class=dmt>·</span></td>';
      return '<td class="dcell'+(d.dow>=6?' wknd':'')+'" title="'+(x.first||'')+'–'+(x.last||'')+' · active '+x.hours.toFixed(1)+'h · '+x.util+'% util"><b>'+x.span.toFixed(1)+'h</b><div class=dsub>'+fmt(x.ful+x.repl)+'</div></td>';}).join('');
    h+='</tr>';T.span+=sph;T.hours+=p.hours;T.items+=p.items;});
  h+='<tr class=tot><td>Team</td><td></td>'+pad+'<td></td><td><b>'+T.span.toFixed(1)+'</b></td><td class=sub2>'+T.hours.toFixed(1)+'</td><td></td><td></td><td><b>'+fmt(T.items)+'</b></td><td></td><td><b>'+fmt(T.span>0?Math.round(T.items/T.span):0)+'</b></td><td></td>'+(showDays?'<td class=dsep></td>'+days.map(()=>'<td></td>').join(''):'')+'</tr>';
  h+='</table>';
  const note='<div class=sub style=margin-top:8px>Sorted by '+floorSort.replace(/_/g,' ')+'. <b>Hours (1st&rarr;last)</b> is the default measure of time on the floor — earliest to latest scan. <b>Active</b> strips 45-min+ breaks out of that window (a stricter, still-maturing calculation, so we lead with the fuller number). The gap between them is what <b>Proj h</b> is for — log real off-scanner work (returns, cleanup, meetings) below and it becomes accounted-for time instead of a mystery gap. '+(showDays?'Day cell = <b>span h</b> over <b>items</b> (hover for active + util).':'Switch <b>Detail → Detailed</b> for the day-by-day grid.')+'</div>';
  document.getElementById('floortable').innerHTML='<div class=tablewrap>'+h+'</div>'+note;
  renderLogPanel();}
// The Log-time tab shares the roster + notes list; keep it fresh whenever floor or log loads.
function renderLogPanel(){fillRoster();if(!FLOOR)return;
  const allNotes=[];FLOOR.people.forEach(p=>(p.notes||[]).forEach(n=>allNotes.push(Object.assign({person:p.person},n))));
  allNotes.sort((a,b)=>a.d<b.d?1:-1);
  const nl=document.getElementById('n_list');
  if(nl)nl.innerHTML=allNotes.length?('<div class=schead style="margin-top:10px"><b>Logged off-scanner time</b> in this range</div><div>'+allNotes.map(n=>'<span class=notechip><b>'+esc(n.person)+'</b> '+n.d+(n.hours?' · '+n.hours+'h':'')+(n.note?' · '+esc(n.note):'')+' <span class=x title="delete" onclick="delNote('+n.id+')">✕</span></span>').join('')+'</div>'):'<div class=sub style="margin-top:8px">No off-scanner time logged in this range yet.</div>';}
async function loadLog(){const f=document.getElementById('from').value,t=document.getElementById('to').value,k=f+'|'+t;
  if(!(FLOOR&&floorKey===k)){try{FLOOR=await getj('/floor?from='+f+'&to='+t);floorKey=k;}catch(e){}}
  renderLogPanel();var d=document.getElementById('n_date');if(d&&!d.value)d.value=t||etToday();}

function nRoster(){const s=new Set();if(DATA&&DATA.people)DATA.people.forEach(p=>s.add(p.person));if(FLOOR&&FLOOR.people)FLOOR.people.forEach(p=>s.add(p.person));return [...s].sort((a,b)=>a.localeCompare(b));}
function fillRoster(){const sel=document.getElementById('n_person');if(!sel)return;const cur=sel.value;
  const opts=nRoster().map(n=>'<option value="'+esc(n)+'">'+esc(n)+'</option>').join('');
  sel.innerHTML='<option value="">Select&hellip;</option>'+opts+'<option value="__new__">&#43; New person&hellip;</option>';
  if(cur)sel.value=cur;
  const d=document.getElementById('n_date');if(d&&!d.value)d.value=document.getElementById('to').value||etToday();}
function curPerson(){const sel=document.getElementById('n_person');if(sel&&sel.value==='__new__')return document.getElementById('n_person_custom').value.trim();return sel?sel.value:'';}
function onPersonPick(){const sel=document.getElementById('n_person'),cust=document.getElementById('n_person_custom');
  if(sel&&sel.value==='__new__'){cust.style.display='';cust.focus();}else if(cust){cust.style.display='none';}
  loadPersonDay();}
function calcHrs(){const s=document.getElementById('n_start').value,e=document.getElementById('n_end').value,h=document.getElementById('n_hours');
  if(s&&e){const a=s.split(':').map(Number),b=e.split(':').map(Number),m=(b[0]*60+b[1])-(a[0]*60+a[1]);h.value=m>0?(m/60).toFixed(2):'';}}
function fmtDur(m){if(m>=60){const h=Math.floor(m/60),r=m%60;return h+'h'+(r?' '+r+'m':'');}return m+'m';}
function gapFill(s,e){document.getElementById('n_start').value=s;document.getElementById('n_end').value=e;calcHrs();document.getElementById('n_note').focus();}
async function loadPersonDay(){const person=curPerson(),date=document.getElementById('n_date').value,box=document.getElementById('n_sched');
  if(!box)return;
  if(!person||person==='__new__'||!date){box.innerHTML='';return;}
  box.innerHTML='<div class=schead>loading schedule&hellip;</div>';
  try{const j=await getj('/person_day?person='+encodeURIComponent(person)+'&d='+date);
    if(!j.ok||!j.scans){box.innerHTML='<div class=schead>No scans for <b>'+person+'</b> on '+date+' &mdash; log the full off-scanner block below.</div>';return;}
    let h='<div class=schead><b>'+person+'</b> &middot; '+date+' &mdash; on floor <b>'+j.first+' &ndash; '+j.last+'</b>, active <b>'+j.active_h.toFixed(1)+'h</b> of a <b>'+j.span_h.toFixed(1)+'h</b> span. Tap a gap to fill it in.</div><div class=tline>';
    j.timeline.forEach((t,i)=>{
      if(i)h+='<span class=tarrow>&rsaquo;</span>';
      if(t.kind==='work')h+='<span class=twork><b>'+t.start_l+'&ndash;'+t.end_l+'</b><span class=m>worked '+fmtDur(t.mins)+'</span></span>';
      else h+='<span class="tgap'+(t.brk?' brk':'')+'" onclick="gapFill(\''+t.start+'\',\''+t.end+'\')"><b>'+t.start_l+'&ndash;'+t.end_l+'</b><span class=m>'+(t.brk?'break':'gap')+' '+fmtDur(t.mins)+'</span><span class=fill>&#43; log</span></span>';});
    h+='</div>';box.innerHTML=h;
  }catch(e){box.innerHTML='<div class=schead>could not load schedule</div>';}}

async function addNote(){
  const person=curPerson();
  const date=document.getElementById('n_date').value||document.getElementById('to').value;
  const start=document.getElementById('n_start').value,end=document.getElementById('n_end').value;
  const hours=document.getElementById('n_hours').value, note=document.getElementById('n_note').value.trim();
  const st=document.getElementById('n_status');st.className='nstat';
  if(!person){st.textContent='Pick a person first.';st.className='nstat err';return;}
  if(!hours&&!(start&&end)&&!note){st.textContent='Add a time window, hours, or a note.';st.className='nstat err';return;}
  st.textContent='saving…';
  try{const r=await fetch('/note',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({person,date,start,end,hours,note})});
    const j=await r.json();
    if(j.ok){st.textContent='Added ✓';st.className='nstat ok';
      ['n_hours','n_note','n_start','n_end'].forEach(id=>document.getElementById(id).value='');
      FLOOR=null;floorKey=null;loadFloor();}
    else{st.textContent=j.error||'error';st.className='nstat err';}
  }catch(e){st.textContent='could not save';st.className='nstat err';}}
async function delNote(id){try{await fetch('/note/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:id})});FLOOR=null;floorKey=null;loadFloor();}catch(e){}}

// ===== Day plan: roster-based staffing — who's in, their hours, their station, and the orders we clear =====
// Rates come from ALL-TIME pace (PSPEED), not the dashboard's date range — you plan with every rep's full
// track record, so everyone is rated even if they didn't work the selected window.
let PLAN=null, PSPEED=null, pspeedKey=null;
function planDaysInRange(){if(!DATA||!DATA.range)return 1;var a=new Date(DATA.range.from+'T00:00'),b=new Date(DATA.range.to+'T00:00');return Math.max(1,Math.round((b-a)/86400000)+1);}
function planItemsOrders(){var o={pick:{i:0,o:0},pack:{i:0,o:0},engrave:{i:0,o:0}};
  (DATA.people||[]).forEach(function(p){
    o.pick.i+=p.items_picked_sh||0; o.pick.o+=p.orders_picked_sh||0;
    o.pack.i+=(p.items_packed_sh||0)+(p.items_packed_shop||0); o.pack.o+=(p.orders_packed_sh||0)+(p.orders_packed_shop||0);
    o.engrave.i+=p.engraved_items||0; o.engrave.o+=p.engraved_orders||0;});
  return o;}
function planRate(person,stage){if(!PSPEED||!PSPEED.stages||!PSPEED.stages[stage])return 0;
  var r=(PSPEED.stages[stage]||[]).find(function(x){return x.person===person;});
  if(!r)return 0; return Math.round(r.ranked?(r.pace||r.throughput||0):(r.throughput||r.pace||0));}
function planTeamRate(stage){if(!PSPEED||!PSPEED.stages||!PSPEED.stages[stage])return 0;
  var u=0,m=0;PSPEED.stages[stage].forEach(function(r){u+=r.units||0;m+=r.active_min||0;});return m>0?Math.round(u/(m/60)):0;}
function planRosterPeople(){var set={};['pick','pack','engrave'].forEach(function(s){((PSPEED&&PSPEED.stages&&PSPEED.stages[s])||[]).forEach(function(r){set[r.person]=1;});});
  return Object.keys(set).sort();}
function num(id,d){var v=parseFloat((document.getElementById(id)||{}).value);return isFinite(v)?v:(d||0);}
function set(id,html){var el=document.getElementById(id);if(el)el.innerHTML=html;}
async function loadPlanner(){var host=document.getElementById('plan_roster');if(!host)return;
  if(!DATA){host.innerHTML='<div class=sub>loading…</div>';return;}
  var to=etToday(), k='all|'+to;   // ALL-TIME pace, independent of the dashboard date range
  if(!(PSPEED&&pspeedKey===k)){host.innerHTML='<div class=sub>learning each person&rsquo;s all-time pace…</div>';
    try{PSPEED=await getj('/speed?from=2020-01-01&to='+to);pspeedKey=k;}catch(e){}}
  if(!PLAN||PLAN.key!==k)planInit(k);
  planRenderRoster();planCompute();}
function planInit(k){var io=planItemsOrders(),shipped=(DATA.shipped&&DATA.shipped.total)||1,days=planDaysInRange();
  var def=num('pl_defhrs',10)||10;
  var tmap={};['pick','pack','engrave'].forEach(function(s){((PSPEED&&PSPEED.stages&&PSPEED.stages[s])||[]).forEach(function(r){if(r.type)tmap[r.person]=r.type;});});
  var roster=planRosterPeople();
  roster.sort(function(a,b){var ai=(tmap[a]==='Intern')?1:0,bi=(tmap[b]==='Intern')?1:0;return ai!==bi?ai-bi:a.localeCompare(b);});  // full-timers first, interns last
  PLAN={key:k, ipo:{pick:io.pick.i/shipped, pack:io.pack.i/shipped, engrave:io.engrave.i/shipped}, avgDaily:Math.round(shipped/days),
    people:roster.map(function(p){var t=tmap[p]||'';return {person:p, type:t, pick:planRate(p,'pick'), pack:planRate(p,'pack'), eng:planRate(p,'engrave'), hours:def, inn:(t!=='Intern'), assign:'Other'};})};
  var oi=document.getElementById('pl_orders'); if(oi&&!oi.value)oi.value=PLAN.avgDaily||'';
  planRecommend(true);}
function planStationOpts(p,sel){var opts=[['Pick',p.pick],['Pack',p.pack]]; if(p.eng>0)opts.push(['Engrave',p.eng]); opts.push(['Other',0]);
  return opts.map(function(o){return '<option'+(o[0]===sel?' selected':'')+'>'+o[0]+'</option>';}).join('');}
function planRenderRoster(){var host=document.getElementById('plan_roster');if(!host||!PLAN)return;
  if(!PLAN.people.length){host.innerHTML='<div class=sub>Not enough Speed data yet to build a roster.</div>';return;}
  var h='<table class=plantbl id=plros><tr><th>In</th><th style=text-align:left>Person</th><th>Hours</th><th>Pick/hr</th><th>Pack/hr</th><th>Eng/hr</th><th style=text-align:left>Station</th><th>Their output</th></tr>';
  PLAN.people.forEach(function(p,i){h+='<tr class="'+(p.inn?'':'plout')+'">'+
    '<td><input type=checkbox class=plin id=plin_'+i+' '+(p.inn?'checked':'')+' onchange="planToggleIn('+i+')"></td>'+
    '<td class=name style=text-align:left>'+esc(p.person)+' '+badge(p.type)+'</td>'+
    '<td><input class="nin plhrs" id=plhrs_'+i+' type=number min=0 max=14 step=0.5 value='+p.hours+' oninput="planEdit()"></td>'+
    '<td>'+(p.pick||'<span class=dmt>·</span>')+'</td><td>'+(p.pack||'<span class=dmt>·</span>')+'</td><td>'+(p.eng||'<span class=dmt>·</span>')+'</td>'+
    '<td style=text-align:left><select class=plsel id=plsel_'+i+' onchange="planEdit()">'+planStationOpts(p,p.assign)+'</select></td>'+
    '<td id=plout_'+i+'></td></tr>';});
  host.innerHTML=h+'</table>';}
function planToggleIn(i){PLAN.people[i].inn=document.getElementById('plin_'+i).checked;PLAN.people[i].alloc=null;
  var tr=document.getElementById('plros').rows[i+1];if(tr)tr.className=PLAN.people[i].inn?'':'plout';planCompute();}
function planEdit(){PLAN.people.forEach(function(p,i){
  var hv=document.getElementById('plhrs_'+i), sv=document.getElementById('plsel_'+i);
  var nh=hv?(parseFloat(hv.value)||0):p.hours, ns=sv?sv.value:p.assign;
  if(nh!==p.hours||ns!==p.assign)p.alloc=null;   // person edited by hand -> drop their auto-split, honour the manual choice
  p.hours=nh;p.assign=ns;});planCompute();}
function planAllHours(){var def=num('pl_defhrs',10)||10;PLAN.people.forEach(function(p,i){p.hours=def;p.alloc=null;var el=document.getElementById('plhrs_'+i);if(el)el.value=def;});planCompute();}
// Auto-assign: find the exact-optimal pick/pack split (max-min by comparative advantage), and reserve ONLY
// the engraver-hours the engraving demand needs — iterated so it converges to the achievable order count.
// Every spare hour (an over-capacity picker, an engraver past what's needed) cascades to the bottleneck.
// A person can be SPLIT (e.g. 8h engrave + 2h pack) when that's optimal.
function planRecommend(silent){if(!PLAN)return;
  var ipoP=PLAN.ipo.pick>0.2?PLAN.ipo.pick:3.5, ipoK=PLAN.ipo.pack>0.2?PLAN.ipo.pack:3.5, ipoE=PLAN.ipo.engrave||0;
  var crew=[];PLAN.people.forEach(function(p,i){p.alloc=null;if(p.inn){p.assign='Other';crew.push(i);}});
  if(!crew.length){if(!silent){planRenderRoster();planCompute();}return;}
  function bestPP(idxs,hoursOf){   // optimal pick/pack partition; exact for <=18, greedy above
    if(idxs.length<=18){var nC=1<<idxs.length,best={o:-1,m:0};
      for(var m=0;m<nC;m++){var pc=0,kc=0;for(var j=0;j<idxs.length;j++){var p=PLAN.people[idxs[j]],h=hoursOf(idxs[j]);if(m&(1<<j))pc+=p.pick*h;else kc+=p.pack*h;}
        var o=Math.min(pc/ipoP,kc/ipoK);if(o>best.o)best={o:o,m:m};}
      var a={};for(var q=0;q<idxs.length;q++)a[idxs[q]]=(best.m&(1<<q))?'Pick':'Pack';return {o:best.o,asg:a};}
    var cap={Pick:0,Pack:0},un=idxs.slice(),g=0,asg={};
    while(un.length&&g++<600){var st=(cap.Pick/ipoP<=cap.Pack/ipoK)?'Pick':'Pack',rk=st==='Pick'?'pick':'pack',bi=-1,br=-1;
      un.forEach(function(i,jj){var r=PLAN.people[i][rk];if(r>br){br=r;bi=jj;}});if(bi<0)break;var pi=un.splice(bi,1)[0];asg[pi]=st;cap[st]+=PLAN.people[pi][rk]*hoursOf(pi);}
    return {o:Math.min(cap.Pick/ipoP,cap.Pack/ipoK),asg:asg};}
  var engers=crew.filter(function(i){return PLAN.people[i].eng>0;}).sort(function(a,b){return PLAN.people[b].eng-PLAN.people[a].eng;});
  var ordEst=bestPP(crew,function(i){return PLAN.people[i].hours;}).o, engH={}, asg={};
  var ppH=function(i){return PLAN.people[i].hours-(engH[i]||0);};
  for(var it=0;it<6;it++){
    engH={}; var need=(ipoE>0?ordEst*ipoE:0);
    for(var e=0;e<engers.length&&need>0.5;e++){var ei=engers[e],er=PLAN.people[ei].eng,H=PLAN.people[ei].hours,use=Math.min(H,need/er);engH[ei]=use;need-=use*er;}
    var ppCrew=crew.filter(function(i){return ppH(i)>0.05;});
    var bp=bestPP(ppCrew,ppH); asg=bp.asg;
    var cP=0,cK=0,cE=0;crew.forEach(function(i){var eh=engH[i]||0,ph=ppH(i);cE+=PLAN.people[i].eng*eh;if(asg[i]==='Pick')cP+=PLAN.people[i].pick*ph;else if(asg[i]==='Pack')cK+=PLAN.people[i].pack*ph;});
    var o=Math.min(cP/ipoP,cK/ipoK,(ipoE>0?cE/ipoE:1e12));
    if(Math.abs(o-ordEst)<1){ordEst=o;break;} ordEst=o;}
  crew.forEach(function(i){var eH=engH[i]||0, pH=ppH(i), st=asg[i]||(PLAN.people[i].pack>=PLAN.people[i].pick?'Pack':'Pick');
    if(eH>0.1&&pH>0.1){var a={Pick:0,Pack:0,Engrave:eH};a[st]=pH;PLAN.people[i].alloc=a;PLAN.people[i].assign=(eH>=pH?'Engrave':st);}
    else if(eH>0.1){PLAN.people[i].alloc=null;PLAN.people[i].assign='Engrave';}
    else{PLAN.people[i].alloc=null;PLAN.people[i].assign=st;}});
  if(!silent){planRenderRoster();planCompute();}}
function planCompute(){if(!PLAN)return;
  var N=num('pl_orders',0), util=num('pl_util',80)/100; if(!(util>0))util=0.8;
  var cap={Pick:0,Pack:0,Engrave:0}, hrs={Pick:0,Pack:0,Engrave:0,Other:0}, nA={Pick:0,Pack:0,Engrave:0,Other:0};
  function rt(p,st){return st==='Pick'?p.pick:st==='Pack'?p.pack:st==='Engrave'?p.eng:0;}
  PLAN.people.forEach(function(p,i){var out=0,label='';
    if(p.inn){
      if(p.alloc){var segs=[],dom='Pick',dm=-1;['Pick','Pack','Engrave'].forEach(function(st){var h=p.alloc[st]||0;if(h>dm){dm=h;dom=st;}});
        ['Pick','Pack','Engrave'].forEach(function(st){var h=p.alloc[st]||0;if(h>0.05){var it=Math.round(rt(p,st)*h*util);out+=it;cap[st]+=it;hrs[st]+=h;segs.push(st.slice(0,2)+' '+h.toFixed(1)+'h');}});
        nA[dom]++;label=segs.join(' · ');}   // count a split person once, under their main station, so People = headcount
      else{var a=p.assign,it=Math.round(rt(p,a)*p.hours*util);out=it;if(cap[a]!=null)cap[a]+=it;if(hrs[a]!=null)hrs[a]+=p.hours;nA[a]=(nA[a]||0)+1;}}
    set('plout_'+i, p.inn?((out?'<b>'+fmt(out)+'</b> items':'<span class=dmt>—</span>')+(label?' <span class=s>'+label+'</span>':'')):'<span class=dmt>out</span>');});
  var ipoP=PLAN.ipo.pick>0.2?PLAN.ipo.pick:3.5, ipoK=PLAN.ipo.pack>0.2?PLAN.ipo.pack:3.5, ipoE=PLAN.ipo.engrave||0;
  var oPick=cap.Pick/ipoP, oPack=cap.Pack/ipoK, orders=Math.floor(Math.min(oPick,oPack));
  var bneck=oPick<=oPack?'Pick':'Pack';
  var engNeed=Math.round(orders*ipoE), engHave=Math.round(cap.Engrave), engOK=(engHave>=engNeed)||engNeed===0;
  var stat='<table class=plantbl style="margin:8px 0 4px"><tr><th style=text-align:left>Station</th><th>People</th><th>Hours</th><th>Item capacity</th><th>Orders it supports</th></tr>';
  [['Pick',oPick],['Pack',oPack]].forEach(function(r){var isb=(r[0]===bneck);
    stat+='<tr'+(isb?' class=plbn':'')+'><td style=text-align:left>'+r[0]+(isb?' <span class=s style=color:#b45309>&larr; bottleneck</span>':'')+'</td><td>'+(nA[r[0]]||0)+'</td><td>'+hrs[r[0]].toFixed(1)+'</td><td>'+fmt(Math.round(cap[r[0]]))+'</td><td><b>'+fmt(Math.floor(r[1]))+'</b></td></tr>';});
  stat+='<tr><td style=text-align:left>Engrave</td><td>'+(nA.Engrave||0)+'</td><td>'+hrs.Engrave.toFixed(1)+'</td><td>'+fmt(engHave)+'</td><td>'+(engNeed?(engOK?'<span style="color:#15803d">keeps pace ('+fmt(engNeed)+' needed)</span>':'<span style="color:#b91c1c">short '+fmt(engNeed-engHave)+' items &mdash; assign an engraver</span>'):'<span class=dmt>none needed</span>')+'</td></tr>';
  if(nA.Other)stat+='<tr><td style=text-align:left>Other <span class=s>restock / returns / off-line</span></td><td>'+nA.Other+'</td><td>'+hrs.Other.toFixed(1)+'</td><td class=dmt colspan=2>not on the order line</td></tr>';
  stat+='</table>';set('plan_stations',stat);
  var inN=0;PLAN.people.forEach(function(p){if(p.inn)inN++;});
  var totH=hrs.Pick+hrs.Pack+hrs.Engrave+hrs.Other;
  var v='<div class=planhero><div><div class=phn>'+fmt(orders)+'</div><div class=phl>orders/day this crew ships</div></div>'+
    '<div><div class=phn>'+inN+'</div><div class=phl>on the floor &middot; '+totH.toFixed(0)+'h</div></div>'+
    '<div class=phd>bottleneck: <b>'+bneck+'</b></div></div>';
  var cmp='';
  if(N>0){ if(orders>=N){var xtra=(orders>N)?(' &mdash; room for <b>'+fmt(orders-N)+'</b> more'):'';
      cmp='<div class="plancmp ok">Clears your target of <b>'+fmt(N)+'</b> orders'+xtra+'.'+(engNeed&&!engOK?' But engraving is short &mdash; put someone on Engrave.':'')+'</div>';}
    else{var ipoB=(bneck==='Pick'?ipoP:ipoK), capB=cap[bneck], need=N*ipoB, extra=need-capB;
      var avgB=hrs[bneck]>0?capB/hrs[bneck]:(planTeamRate(bneck.toLowerCase())||1), addH=avgB>0?extra/avgB:0;
      cmp='<div class="plancmp short"><b>'+fmt(N-orders)+'</b> short of your <b>'+fmt(N)+'</b>-order target. Bottleneck is <b>'+bneck+'</b> &mdash; you need about <b>'+addH.toFixed(1)+'h</b> more '+bneck.toLowerCase()+' capacity: move a picker to '+bneck.toLowerCase()+', extend hours, or add a person.'+(engNeed&&!engOK?' Engraving is also short.':'')+'</div>';}}
  set('plan_verdict', v+cmp);}

// ===== Individual trends: one person's weekly pace per activity =====
let TREND=null, trendChartObj=null;
function loadTrend(){var sel=document.getElementById('tr_person');
  if(sel&&sel.options.length===0){var r=nRoster();sel.innerHTML=r.map(function(n){return '<option value="'+esc(n)+'">'+esc(n)+'</option>';}).join('');}
  if(sel&&!sel.value&&sel.options.length)sel.value=sel.options[0].value;
  loadTrendData();}
async function loadTrendData(){var person=(document.getElementById('tr_person')||{}).value;
  var gran=(document.getElementById('tr_gran')||{}).value||'week';
  var wk=document.getElementById('tr_wkwrap'),dw=document.getElementById('tr_daywrap');
  if(wk)wk.style.display=(gran==='day')?'none':''; if(dw)dw.style.display=(gran==='day')?'':'none';
  if(!person){return;}
  document.getElementById('tr_summary').innerHTML='<div class=sub>loading…</div>';
  var url='/trend?person='+encodeURIComponent(person)+(gran==='day'
    ?'&gran=day&days='+((document.getElementById('tr_days')||{}).value||21)
    :'&weeks='+((document.getElementById('tr_weeks')||{}).value||12));
  try{TREND=await getj(url);renderTrend();}
  catch(e){document.getElementById('tr_summary').innerHTML='<div class=sub>could not load trend</div>';}}
function trendActs(){return [['pick','Pick','#2563eb'],['pack','Pack','#16a34a'],['engrave','Engrave','#0d9488']];}
function renderTrend(){if(!TREND)return;var acts=trendActs(),weeks=TREND.weeks;
  var labels=weeks.map(function(w){return w.wk.slice(5);});
  var sHtml='';
  acts.forEach(function(a){var pts=[];weeks.forEach(function(w){var o=w[a[0]]||{};if(o.uph!=null)pts.push(o.uph);});
    if(!pts.length)return;var first=pts[0],last=pts[pts.length-1],delta=last-first,pct=first?Math.round(100*delta/first):0;
    var dir=delta>0?'&#9650; improving':(delta<0?'&#9660; slower':'&ndash; flat'),col=delta>0?'#15803d':(delta<0?'#b91c1c':'#64748b');
    sHtml+='<span class=trendkpi><b style="color:'+a[2]+'">'+a[1]+'</b> '+first+' &rarr; <b>'+last+'</b> /hr <span style="color:'+col+'">'+(delta>=0?'+':'')+pct+'% '+dir+'</span></span>';});
  document.getElementById('tr_summary').innerHTML='<div class=trendkpis>'+(sHtml||'<span class=sub>Not enough timed data yet for '+esc(TREND.person)+' &mdash; needs a few more days of scans.</span>')+'</div>';
  if(window.Chart){var ds=acts.map(function(a){return {label:a[1],data:weeks.map(function(w){var o=w[a[0]]||{};return o.uph==null?null:o.uph;}),
      borderColor:a[2],backgroundColor:a[2],tension:.3,spanGaps:true,pointRadius:4,borderWidth:2};});
    if(trendChartObj)trendChartObj.destroy();
    trendChartObj=new Chart(document.getElementById('trendChart'),{type:'line',data:{labels:labels,datasets:ds},
      options:{responsive:true,plugins:{legend:{position:'bottom'},tooltip:{callbacks:{label:function(c){return c.dataset.label+': '+(c.parsed.y==null?'—':c.parsed.y+' /hr');}}}},
        scales:{y:{title:{display:true,text:'items / hr (typical pace)'},beginAtZero:true}}}});}
  var th='<tr><th style=text-align:left>'+(TREND.gran==='day'?'Day':'Week of')+'</th>';acts.forEach(function(a){th+='<th>'+a[1]+' /hr</th><th>'+a[1]+' units</th>';});th+='</tr>';
  var body=weeks.map(function(w){var r='<tr><td class=name style=text-align:left>'+w.wk+'</td>';
    acts.forEach(function(a){var o=w[a[0]]||{};r+='<td>'+(o.uph!=null?'<b>'+o.uph+'</b>':'<span class=dmt>·</span>')+'</td><td>'+(o.units?fmt(o.units):'<span class=dmt>·</span>')+'</td>';});
    return r+'</tr>';}).join('');
  document.getElementById('tr_table').innerHTML='<div class=tablewrap style="margin-top:14px"><table>'+th+body+'</table></div>';}
async function loadEngraving(){const f=document.getElementById('from').value,t=document.getElementById('to').value,k=f+'|'+t;
  if(ENGR&&engKey===k){renderEngraving();return;}
  document.getElementById('engtable').innerHTML='<div class=sub>loading…</div>';
  try{ENGR=await getj('/engraving?from='+f+'&to='+t);engKey=k;renderEngraving();}
  catch(e){document.getElementById('engtable').innerHTML='<div class=sub>could not load engraving data</div>';}}
function engSortBy(k){if(engSort===k)engDir*=-1;else{engSort=k;engDir=-1;}renderEngraving();}
function renderEngraving(){if(!ENGR)return;
  const days=ENGR.days,showDays=DET()&&days.length<=16;
  const ppl=ENGR.engravers.filter(tfilter);
  if(!ppl.length){document.getElementById('engtable').innerHTML='<div class=sub style=margin-top:8px>No engraving in this window.</div>';return;}
  const arr=[...ppl].sort((a,b)=>{const x=a[engSort],y=b[engSort];return (x>y?1:(x<y?-1:0))*engDir;});
  const arw=k=>engSort===k?'<span class=arw>'+(engDir<0?'▼':'▲')+'</span>':'';
  const th=(k,l,s)=>'<th class="srt'+(engSort===k?' act':'')+'" onclick="engSortBy(\''+k+'\')">'+l+(s?' <span class=s>'+s+'</span>':'')+arw(k)+'</th>';
  let h='<table><tr>'+th('person','Engraver','')+'<th>Type</th>'+th('active_days','Days','')+
    th('totes','Totes','jobs')+th('items','Items','engravings')+th('hours','Hours','active')+
    th('items_per_hr','Items/hr','')+th('totes_per_hr','Totes/hr','')+th('items_per_day','Items/day','')+
    th('match_rate','Match','% to order')+'<th>Mix <span class=s>LID·IPE·DOTW</span></th>';
  if(showDays)h+='<th class=dsep></th>'+days.map(dhead).join('');
  h+='</tr>';const T={totes:0,items:0,hours:0,lid:0,ipe:0,dotw:0};
  arr.forEach(p=>{const m={};p.days.forEach(x=>m[x.d]=x);
    h+='<tr><td class=name>'+esc(p.person)+'</td><td>'+badge(p.type)+'</td><td>'+p.active_days+'</td>'+
      '<td><b>'+fmt(p.totes)+'</b></td><td><b>'+fmt(p.items)+'</b></td><td>'+p.hours.toFixed(1)+'</td>'+
      '<td><b>'+fmt(p.items_per_hr)+'</b></td><td>'+p.totes_per_hr.toFixed(1)+'</td><td>'+fmt(p.items_per_day)+'</td>'+
      '<td>'+chip(p.match_rate)+'</td>'+
      '<td class=mix><span class=mlid>'+fmt(p.lid)+'</span> · <span class=mipe>'+fmt(p.ipe)+'</span> · <span class=mdotw>'+fmt(p.dotw)+'</span></td>';
    if(showDays)h+='<td class=dsep></td>'+days.map(d=>{const x=m[d.d];
      if(!x||!x.totes)return '<td class="dcell'+(d.dow>=6?' wknd':'')+'"><span class=dmt>·</span></td>';
      return '<td class="dcell'+(d.dow>=6?' wknd':'')+'"><b>'+fmt(x.items)+'</b><div class=dsub>'+x.hours.toFixed(1)+'h</div></td>';}).join('');
    h+='</tr>';T.totes+=p.totes;T.items+=p.items;T.hours+=p.hours;T.lid+=p.lid;T.ipe+=p.ipe;T.dotw+=p.dotw;});
  h+='<tr class=tot><td>Total</td><td></td><td></td><td><b>'+fmt(T.totes)+'</b></td><td><b>'+fmt(T.items)+'</b></td><td>'+T.hours.toFixed(1)+'</td><td><b>'+fmt(T.hours>0?Math.round(T.items/T.hours):0)+'</b></td><td></td><td></td><td></td><td class=mix><span class=mlid>'+fmt(T.lid)+'</span> · <span class=mipe>'+fmt(T.ipe)+'</span> · <span class=mdotw>'+fmt(T.dotw)+'</span></td>'+(showDays?'<td class=dsep></td>'+days.map(()=>'<td></td>').join(''):'')+'</tr>';
  h+='</table>';
  const note='<div class=sub style=margin-top:8px><b>Totes</b> = engraving jobs; <b>Items</b> = individual engravings (LID+IPE+DOTW). '+(showDays?'Each day cell = <b>items</b> over <b>hours</b>.':'Switch <b>Detail → Detailed</b> above for the day-by-day grid.')+' <b>Match</b> under 80% means some scans couldn&rsquo;t be tied to an order (a tote built before tote-tracking, or a mis-scan).</div>';
  document.getElementById('engtable').innerHTML='<div class=tablewrap>'+h+'</div>'+note;}
async function loadAnalytics(){const f=document.getElementById('from').value,t=document.getElementById('to').value,k=f+'|'+t;
  if(!(FLOOR&&floorKey===k)){document.getElementById('analytics').innerHTML='<div class=sub>loading…</div>';
    try{FLOOR=await getj('/floor?from='+f+'&to='+t);floorKey=k;}catch(e){document.getElementById('analytics').innerHTML='<div class=sub>could not load</div>';return;}}
  renderAnalytics();}
function anSetSort(k){if(anSort===k)anDir=-anDir;else{anSort=k;anDir=(k==='person'||k==='type'?1:-1);}renderAnalytics();}
function renderAnalytics(){if(!FLOOR)return;
  const el=document.getElementById('analytics'); if(!el)return;
  [anTrend,anRank,anMix].forEach(c=>{try{if(c)c.destroy();}catch(e){}});anTrend=anRank=anMix=null;
  const ppl=FLOOR.people.filter(tfilter);
  if(!ppl.length){el.innerHTML='<div class=sub>No activity in this window.</div>';return;}
  const sum=(g)=>ppl.reduce((a,p)=>a+g(p),0);
  const H=sum(p=>p.hours), SPAN=sum(p=>p.span_h), I=sum(p=>p.items), FUL=sum(p=>p.ful_items), REP=sum(p=>p.repl_items);
  const pdays=sum(p=>p.active_days);
  const uplh=H>0?Math.round(I/H):0, fulHr=H>0?Math.round(FUL/H):0;
  const avgUtil=Math.round(ppl.reduce((a,p)=>a+p.util,0)/ppl.length);
  const uphs=ppl.map(p=>p.items_per_hr).filter(x=>x>0).sort((a,b)=>a-b);
  const _n=uphs.length, med=_n?(_n%2?uphs[(_n-1)/2]:Math.round((uphs[_n/2-1]+uphs[_n/2])/2)):0, top=_n?uphs[_n-1]:0, bot=_n?uphs[0]:0;
  // dashboard rollup (same range & team filter) for orders + fulfillment mix
  let shipped=0, stage=null;
  if(DATA&&DATA.people){const dp=DATA.people.filter(teamFilter);stage={pick:0,packsh:0,packshop:0,eng:0};
    dp.forEach(p=>{stage.pick+=p.items_picked_sh||0;stage.packsh+=p.items_packed_sh||0;stage.packshop+=p.items_packed_shop||0;stage.eng+=p.engraved_items||0;});
    shipped=(DATA.shipped&&DATA.shipped.total)||0;}
  const _tl={FT:'Full-timers',Intern:'Interns',Seasonal:'Seasonal'};
  const teamLbl=segval('team')==='all'?'':' &middot; <b>'+(_tl[segval('team')]||segval('team'))+'</b> only';
  let h='<div class=note style=margin-top:0>Team performance for <b>'+FLOOR.range.from+' → '+FLOOR.range.to+'</b>'+teamLbl+'. <b>UPLH</b> = items per active labour hour (the core productivity KPI); active hours use the 45-min-break rule.</div>';
  h+='<div class=cards>'+card('People','on the floor','s-sel',ppl.length)+
    (shipped?card('Orders shipped','out the door','s-sh',shipped):'')+
    card('Fulfillment','pick+pack+engrave','s-sel',FUL)+card('Restocked','separate track','s-repl',REP)+
    card('Active hrs','hands-on','s-sel',Math.round(H))+card('Floor hrs','first→last','s-sel',Math.round(SPAN))+'</div>';
  h+='<div class=cards style=margin-top:10px>'+card('Team UPLH','items / active hr','s-sel',uplh)+
    card('Fulfillment/hr','ful items / active hr','s-sel',fulHr)+card('Avg utilization','active ÷ on-floor','s-repl',avgUtil+'%')+
    card('Top UPLH','best person','s-sel',top)+card('Median UPLH','person','s-sel',med)+card('Bottom UPLH','person','s-sel',bot)+'</div>';
  // chart placeholders
  h+='<div style="height:300px;margin:16px 0 4px"><canvas id=anTrendC></canvas></div>';
  h+='<div style="display:flex;gap:16px;flex-wrap:wrap;margin:6px 0">'+
     '<div style="flex:2 1 360px;min-width:300px;height:340px"><canvas id=anRankC></canvas></div>'+
     '<div style="flex:1 1 240px;min-width:240px;height:340px"><canvas id=anMixC></canvas></div></div>';
  // detailed per-person table (sortable)
  const teamItems=I||1; ppl.forEach(p=>{p._share=Math.round(100*p.items/teamItems);});
  const cols=[['person','Person','left'],['type','Type','left'],['active_days','Days'],['hours','Active h'],['span_h','Floor h'],['util','Util%'],['items','Items'],['items_per_hr','UPLH'],['items_per_day','Items/day'],['ful_items','Fulfill'],['repl_items','Restock'],['_share','Share']];
  function ath(k,label,align){var ar=(anSort===k)?(anDir>0?' ▲':' ▼'):'';return '<th onclick="anSetSort(\''+k+'\')" style="cursor:pointer'+(align?';text-align:'+align:'')+'">'+label+ar+'</th>';}
  const rows=[...ppl].sort((a,b)=>{const k=anSort;if(k==='person'||k==='type'){const av=(a[k]||''),bv=(b[k]||'');return (av<bv?-1:av>bv?1:0)*anDir;}return ((a[k]||0)-(b[k]||0))*anDir;});
  let tbl='<div class=sub style="margin:16px 0 6px"><b>Full team &mdash; detailed metrics.</b> Click a column header to sort.</div>'+
    '<div class=tablewrap><table><tr>'+cols.map(c=>ath(c[0],c[1],c[2])).join('')+'</tr>';
  rows.forEach(p=>{const ty=p.type==='FT'?'FT':(p.type==='Intern'?'Intern':(p.type==='Seasonal'?'Seasonal':'—'));
    tbl+='<tr><td class=name style=text-align:left>'+esc(p.person)+'</td><td style=text-align:left>'+ty+'</td>'+
      '<td>'+fmt(p.active_days)+'</td><td>'+p.hours.toFixed(1)+'</td><td class=sub2>'+p.span_h.toFixed(1)+'</td>'+
      '<td>'+chip(p.util)+'</td><td><b>'+fmt(p.items)+'</b></td><td><b>'+fmt(p.items_per_hr)+'</b></td>'+
      '<td>'+fmt(p.items_per_day)+'</td><td>'+fmt(p.ful_items)+'</td><td>'+fmt(p.repl_items)+'</td>'+
      '<td><div style="display:flex;align-items:center;gap:6px;min-width:96px"><span style="width:34px;text-align:right">'+p._share+'%</span><div style="flex:1;height:6px;background:#eef2f7;border-radius:3px;overflow:hidden"><div style="height:100%;width:'+p._share+'%;background:'+C.pick+'"></div></div></div></td></tr>';});
  tbl+='<tr class=tot><td>Team</td><td></td><td>'+fmt(pdays)+'</td><td>'+H.toFixed(1)+'</td><td class=sub2>'+SPAN.toFixed(1)+'</td><td>'+avgUtil+'%</td><td><b>'+fmt(I)+'</b></td><td><b>'+fmt(uplh)+'</b></td><td></td><td>'+fmt(FUL)+'</td><td>'+fmt(REP)+'</td><td>100%</td></tr></table></div>';
  // FT vs Intern
  const grp=(ty)=>{const g=ppl.filter(p=>p.type===ty);if(!g.length)return null;
    const gh=g.reduce((a,p)=>a+p.hours,0),gi=g.reduce((a,p)=>a+p.items,0),gd=g.reduce((a,p)=>a+p.active_days,0);
    return {n:g.length,hours:Math.round(gh),items:gi,uplh:gh>0?Math.round(gi/gh):0,util:Math.round(g.reduce((a,p)=>a+p.util,0)/g.length),ipd:gd>0?Math.round(gi/gd):0};};
  const ft=grp('FT'),se=grp('Seasonal'),it=grp('Intern');
  let cmp='';
  const cmpRows=[['Full-timers',ft],['Seasonal',se],['Interns',it]].filter(x=>x[1]);
  if(cmpRows.length>1){cmp='<div class=sub style="margin:16px 0 6px"><b>Full-timers vs Seasonal vs Interns</b></div>'+
    '<table class=plantbl><tr><th style=text-align:left>Group</th><th>People</th><th>Active hrs</th><th>Items</th><th>UPLH</th><th>Items/day</th><th>Avg util</th></tr>'+
    cmpRows.map(x=>'<tr><td class=name style=text-align:left>'+x[0]+'</td><td>'+x[1].n+'</td><td>'+x[1].hours+'</td><td>'+fmt(x[1].items)+'</td><td><b>'+x[1].uplh+'</b></td><td>'+x[1].ipd+'</td><td>'+x[1].util+'%</td></tr>').join('')+'</table>';}
  el.innerHTML=h+tbl+cmp;
  if(curTab!=='an')return;   // only build charts when the tab is actually visible
  // daily team output + UPLH trend
  const dm={};ppl.forEach(p=>p.days.forEach(d=>{const x=dm[d.d]||(dm[d.d]={ful:0,repl:0,hrs:0});x.ful+=d.ful;x.repl+=d.repl;x.hrs+=d.hours;}));
  const days=Object.keys(dm).sort(), fulA=days.map(k=>dm[k].ful), replA=days.map(k=>dm[k].repl), uplhA=days.map(k=>dm[k].hrs>0?Math.round((dm[k].ful+dm[k].repl)/dm[k].hrs):0);
  anTrend=new Chart(document.getElementById('anTrendC'),{data:{labels:days,datasets:[
    {type:'bar',label:'Fulfillment',data:fulA,backgroundColor:C.fulfill,stack:'s',yAxisID:'y',order:3},
    {type:'bar',label:'Restocked',data:replA,backgroundColor:C.repl,stack:'s',yAxisID:'y',order:3},
    {type:'line',label:'UPLH',data:uplhA,borderColor:'#0f172a',backgroundColor:'#0f172a',yAxisID:'y1',tension:.3,pointRadius:3,order:1}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      scales:{x:{stacked:true,grid:{display:false},ticks:{font:{size:10}}},
        y:{stacked:true,beginAtZero:true,position:'left',grid:{color:'#eef1f5'},title:{display:true,text:'Items / day',color:'#64748b',font:{size:11,weight:'600'}}},
        y1:{beginAtZero:true,position:'right',grid:{display:false},title:{display:true,text:'UPLH',color:'#64748b',font:{size:11,weight:'600'}}}},
      plugins:{legend:{position:'bottom'},title:{display:true,text:'Daily team output & UPLH trend',color:'#0f172a',font:{size:13,weight:'600'}}}}});
  // UPLH by person (ranked, coloured by type)
  const rk=[...ppl].filter(p=>p.items_per_hr>0).sort((a,b)=>a.items_per_hr-b.items_per_hr);
  anRank=new Chart(document.getElementById('anRankC'),{type:'bar',data:{labels:rk.map(p=>p.person),datasets:[{label:'UPLH',data:rk.map(p=>p.items_per_hr),backgroundColor:rk.map(p=>p.type==='Intern'?'#94a3b8':C.pick)}]},
    options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},title:{display:true,text:'UPLH by person (blue = full-timer, grey = intern)',color:'#0f172a',font:{size:12,weight:'600'}}},
      scales:{x:{beginAtZero:true,grid:{color:'#eef1f5'},title:{display:true,text:'items / active hr',color:'#64748b',font:{size:10}}},y:{grid:{display:false},ticks:{font:{size:10}}}}}});
  // fulfillment mix doughnut
  if(stage&&(stage.pick+stage.packsh+stage.packshop+stage.eng)>0){
    anMix=new Chart(document.getElementById('anMixC'),{type:'doughnut',data:{labels:['Picked·SH','Packed·SH','Packed·Shopify','Engraved'],datasets:[{data:[stage.pick,stage.packsh,stage.packshop,stage.eng],backgroundColor:[C.pick,C.pack,C.fulfill,C.engrave]}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'55%',
        plugins:{legend:{position:'bottom',labels:{font:{size:10},boxWidth:12}},title:{display:true,text:'Fulfillment mix',color:'#0f172a',font:{size:12,weight:'600'}}}}});}
}
function copyChat(){const v=vis();const rows=DATA.people.filter(teamFilter).map(p=>{
    var parts=[];
    if(v.pick)parts.push('picked '+p.items_picked_sh);
    if(v.packsh||v.packshop)parts.push('packed '+((v.packsh?p.items_packed_sh:0)+(v.packshop?p.items_packed_shop:0)));
    if(v.eng)parts.push('engraved '+p.engraved_items);
    return p.person+': fulfillment '+fulItems(p,v)+(parts.length?' ('+parts.join(', ')+')':'')+(v.repl?', restocked '+p.replenished:'');});
  navigator.clipboard.writeText('Warehouse '+DATA.range.from+'\n'+DATA.shipped.total+' orders shipped\n'+rows.join('\n'));document.getElementById('status').textContent='copied!';setTimeout(()=>document.getElementById('status').textContent='',1500);}
function dl(kind){const ppl=DATA.people.filter(teamFilter);let blob,name;
  if(kind==='json'){blob=new Blob([JSON.stringify(DATA,null,2)],{type:'application/json'});name='warehouse.json';}
  else{const hdr=['person','type','items_picked_sh','items_packed_sh','items_packed_shopify','engraved_items','items_total','replenished','orders_picked_sh','orders_packed_sh','orders_packed_shopify'];
    const lines=[hdr.join(',')].concat(ppl.map(p=>[p.person,p.type,p.items_picked_sh,p.items_packed_sh,p.items_packed_shop,p.engraved_items,(p.items_picked_sh+p.items_packed_sh+p.items_packed_shop+p.engraved_items),p.replenished,p.orders_picked_sh,p.orders_packed_sh,p.orders_packed_shop].join(',')));
    blob=new Blob([lines.join('\n')],{type:'text/csv'});name='warehouse.csv';}
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();}
// ---------------- Speed & Rankings tab ----------------
let SPEED=null, speedKey=null;
const SP_STAGES=['pick','pack','engrave','replenish'];
function cap(s){return s[0].toUpperCase()+s.slice(1);}
function pctColor(p){return 'hsl('+Math.round(p*1.2)+',68%,40%)';}  // 0=red(slow) -> 100=green(fast)
async function loadSpeed(){
  const f=document.getElementById('from').value,t=document.getElementById('to').value,k=f+'|'+t;
  if(SPEED&&speedKey===k){renderSpeed();return;}
  document.getElementById('speed_method').innerHTML='';
  document.getElementById('speed_boards').innerHTML='<div class=sub>loading…</div>';
  document.getElementById('speed_matrix').innerHTML='';
  try{SPEED=await getj('/speed?from='+f+'&to='+t);speedKey=k;renderSpeed();}
  catch(e){document.getElementById('speed_boards').innerHTML='<div class=sub>could not load speed data</div>';}
}
function spType(S,person){for(const st of SP_STAGES){const r=(S[st]||[]).find(x=>x.person===person);if(r&&r.type)return r.type;}return '';}
function spActiveMin(S,person){let m=0;SP_STAGES.forEach(st=>{const r=(S[st]||[]).find(x=>x.person===person);if(r)m+=r.active_min;});return m;}
function renderSpeed(){
  if(!SPEED)return;const cfg=SPEED.config,S=SPEED.stages,g=cfg.gate;
  // ---- methodology (all assumptions visible) ----
  let m='<div class=mbox><h3>How this is measured — read me</h3>';
  m+='Two numbers per person, because they answer different questions:<br>';
  m+='&bull; <b>Pace</b> (the ranking) = how fast when hands are on the work. It&rsquo;s the <b>median time between consecutive scans of the same task</b>, turned into items/hr. Using the <b>median</b> (the middle gap) means one lunch break, a long pause, or switching tasks <b>can&rsquo;t distort it</b> — that&rsquo;s why it stays stable day-to-day and is the fair way to compare people.<br>';
  m+='&bull; <b>Thru</b> (throughput) = items ÷ active hours on that task (active hours = consecutive same-task scans with gaps &ge;'+cfg.break_min+' min removed as breaks). Throughput is always &le; pace; the gap between them is that person&rsquo;s pauses. <b>High pace + low throughput = fast hands but stop-start</b>; low pace = genuinely slow.<br>';
  m+='Built on each person&rsquo;s <b>whole scan timeline</b>, so a gap counts toward a task only if the scan before it was the same task — <b>switching tasks never counts against another task</b>. Scans within <code>'+cfg.burst_s+'s</code> merge into one action.<br>';
  m+='<b>Replenishment</b> is ranked as <b>boxes/hr</b> (each bin transfer = one box, any size); single-unit tote moves (98% of replenish scans) are excluded.<br>';
  m+='<b>Ranked only if</b> a person has <code>&ge;'+g.min_intervals+' timed units</code> across <code>&ge;'+g.min_days+' days</code>; otherwise &ldquo;insufficient&rdquo; with the reason. <b>Scans only</b> (pick/pack/replenish from ShipHero, engraving from the logger) — a <b>floor on speed, not a timesheet</b>.';
  m+='</div>';
  document.getElementById('speed_method').innerHTML=m;
  // ---- percentiles per stage (among ranked) ----
  const pct={};
  SP_STAGES.forEach(s=>{const rk=(S[s]||[]).filter(r=>r.ranked).sort((a,b)=>a.uph-b.uph);
    rk.forEach((r,i)=>{const p=rk.length>1?Math.round(100*i/(rk.length-1)):100;
      (pct[r.person]=pct[r.person]||{})[s]={uph:r.uph,p,med:r.med_spi,n:r.n,days:r.days};});});
  // ---- leaderboards ----
  let b='';
  SP_STAGES.forEach(s=>{
    const rows=S[s]||[],rk=rows.filter(r=>r.ranked).sort((a,b)=>b.uph-a.uph),un=rows.filter(r=>!r.ranked);
    b+='<div class=card style="padding:14px 16px"><div style="font-weight:700">'+cap(s)+' <span class=sub style="font-weight:400">(pace = typical '+cfg.unit[s]+'/hr, fastest first)</span></div>';
    if(!rk.length)b+='<div class=sub style=margin-top:6px>No one has enough data yet in this window.</div>';
    else{b+='<table style=margin-top:6px><tr><th>#</th><th style=text-align:left>Person</th><th title="typical items/hr when hands are on the task (ranking metric)">Pace</th><th title="throughput: items per active hour, pauses included">Thru</th><th title="median seconds per item">s/ea</th><th title="number of timed intervals behind the number">n</th><th>days</th></tr>';
      rk.forEach((r,i)=>{b+='<tr><td>'+(i+1)+'</td><td class=name style=text-align:left>'+esc(r.person)+'</td><td><b style="color:'+pctColor(pct[r.person][s].p)+'">'+fmt(r.pace)+'</b></td><td class=sub>'+fmt(r.throughput)+'</td><td>'+(r.med_spi==null?'—':r.med_spi+'s')+'</td><td>'+r.n+'</td><td>'+r.days+'</td></tr>';});
      b+='</table>';}
    if(un.length)b+='<div class=sub style=margin-top:8px><b>Insufficient:</b> '+un.map(r=>r.person+' <span class=ins>('+r.reason+')</span>').join(', ')+'</div>';
    b+='</div>';
  });
  document.getElementById('speed_boards').innerHTML=b;
  // ---- MagNano vs Normal picking split ----
  const spEl=document.getElementById('speed_split');
  if(spEl){
    const ppl=new Set();['pick_mgn','pick_norm'].forEach(s=>(S[s]||[]).forEach(r=>ppl.add(r.person)));
    const srows=[...ppl].map(p=>{const m=(S['pick_mgn']||[]).find(r=>r.person===p),nn=(S['pick_norm']||[]).find(r=>r.person===p);
        return {person:p,type:(m&&m.type)||(nn&&nn.type)||'',
          mgn:(m&&m.ranked)?m.pace:null, mgn_n:m?m.n:0, mgn_thru:m?m.throughput:0,
          norm:(nn&&nn.ranked)?nn.pace:null, norm_n:nn?nn.n:0, norm_thru:nn?nn.throughput:0};})
      .filter(r=>(r.mgn!=null||r.norm!=null) && (segval('team')==='all'||r.type===segval('team')))
      .sort((a,b)=>(b.mgn||0)-(a.mgn||0));
    if(!srows.length)spEl.innerHTML='<div class=sub>No ranked picking data in this window yet.</div>';
    else{let sp='<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Type</th>'+
        '<th title="typical MagNano items/hr (pace)">MagNano /hr</th><th title="timed intervals">n</th>'+
        '<th title="typical normal-pick items/hr (pace)">Normal /hr</th><th title="timed intervals">n</th>'+
        '<th title="how many times faster MagNano picks vs normal">MagNano &times;</th></tr>';
      srows.forEach(r=>{const mult=(r.mgn&&r.norm)?(r.mgn/r.norm):null;
        sp+='<tr><td class=name style=text-align:left>'+esc(r.person)+'</td><td>'+badge(r.type)+'</td>'+
          '<td><b>'+(r.mgn!=null?fmt(r.mgn):'<span class=dmt>&middot;</span>')+'</b></td><td class=sub2>'+(r.mgn_n||'&middot;')+'</td>'+
          '<td><b>'+(r.norm!=null?fmt(r.norm):'<span class=dmt>&middot;</span>')+'</b></td><td class=sub2>'+(r.norm_n||'&middot;')+'</td>'+
          '<td>'+(mult?'<b style="color:'+(mult>=1.5?'#b45309':'#334155')+'">'+mult.toFixed(1)+'&times;</b>':'<span class=dmt>&mdash;</span>')+'</td></tr>';});
      sp+='</table></div><div class=sub2 style="margin-top:6px">Pace = typical items/hr when hands are on the task (median method, same as the boards above). A person only appears in a column once they have enough timed MagNano (or normal) picks to rank.</div>';
      spEl.innerHTML=sp;}
  }
  // ---- hours by activity (where each person's time went) ----
  const HB={};
  SP_STAGES.forEach(s=>(S[s]||[]).forEach(r=>{(HB[r.person]=HB[r.person]||{type:r.type})[s]=(r.active_min||0)/60;}));
  const hrows=Object.entries(HB).map(([person,o])=>({person,type:o.type,
    pick:o.pick||0,pack:o.pack||0,engrave:o.engrave||0,replenish:o.replenish||0,
    total:(o.pick||0)+(o.pack||0)+(o.engrave||0)+(o.replenish||0)}))
    .filter(r=>r.total>0 && (segval('team')==='all'||r.type===segval('team'))).sort((a,b)=>b.total-a.total);
  const hcell=v=>v>0?v.toFixed(1):'<span class=dmt>·</span>';
  let hh='<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Type</th><th>Pick h</th><th>Pack h</th><th>Engrave h</th><th>Restock h</th><th>Total h</th></tr>';
  hrows.forEach(r=>{hh+='<tr><td class=name>'+esc(r.person)+'</td><td>'+badge(r.type)+'</td><td>'+hcell(r.pick)+'</td><td>'+hcell(r.pack)+'</td><td>'+hcell(r.engrave)+'</td><td>'+hcell(r.replenish)+'</td><td><b>'+r.total.toFixed(1)+'</b></td></tr>';});
  hh+='</table></div>';
  document.getElementById('speed_hours').innerHTML=hrows.length?hh:'<div class=sub>No tracked activity in this window.</div>';
  // ---- assignment matrix ----
  const all=new Set();SP_STAGES.forEach(s=>(S[s]||[]).forEach(r=>all.add(r.person)));
  const order=[...all].sort((a,b)=>spActiveMin(S,b)-spActiveMin(S,a));
  let x='<div class=tablewrap><table class=matrix><tr><th style=text-align:left>Person</th><th>Type</th>'+SP_STAGES.map(s=>'<th>'+cap(s)+'</th>').join('')+'<th>Best fit</th><th>Active hrs<span class=s> tracked</span></th></tr>';
  order.forEach(person=>{
    const pr=pct[person]||{};let best=null;SP_STAGES.forEach(s=>{if(pr[s]&&(!best||pr[s].p>pr[best].p))best=s;});
    const ty=spType(S,person);
    x+='<tr><td class=name>'+person+'</td><td><span class="badge '+(ty==='Intern'?'in':'ft')+'">'+(ty==='Intern'?'Intern':(ty?'Full-timer':'—'))+'</span></td>';
    SP_STAGES.forEach(s=>{
      if(pr[s]){const c=pctColor(pr[s].p);
        x+='<td class="cell'+(s===best?' best':'')+'" title="'+pr[s].p+'th pct · '+pr[s].n+' samples · '+pr[s].days+' days"><b style="color:'+c+'">'+fmt(pr[s].uph)+'</b><div class=spbar><i style="width:'+Math.max(4,pr[s].p)+'%;background:'+c+'"></i></div></td>';}
      else{const row=(S[s]||[]).find(r=>r.person===person);x+='<td class="cell ins" title="'+(row?row.reason.replace(/"/g,''):'never did this task')+'">'+(row?'·':'')+'</td>';}
    });
    x+='<td>'+(best?'<span class=pill2 style="background:#dcfce7;color:#166534">'+cap(best)+'</span>':'<span class=ins>—</span>')+'</td>';
    x+='<td>'+(spActiveMin(S,person)/60).toFixed(1)+'</td></tr>';
  });
  x+='</table></div>';
  x+='<div class=sub style=margin-top:8px>Each cell = that person&rsquo;s <b>pace</b> (typical units/hr) with a percentile bar within the activity (green = fast). <b>Best fit</b> = where they rank highest — assign them there. <b>Active hrs (tracked)</b> = time between same-task scans, breaks removed (a floor, not a full timesheet).</div>';
  document.getElementById('speed_matrix').innerHTML=x;
}
// ---------------- Watch List tab ----------------
let WATCH=null, watchKey=null;
async function loadWatch(){
  const f=document.getElementById('from').value,t=document.getElementById('to').value,k=f+'|'+t;
  if(WATCH&&watchKey===k){renderWatch();return;}
  document.getElementById('watch_body').innerHTML='<div class=sub>loading…</div>';
  try{WATCH=await getj('/watch?from='+f+'&to='+t);watchKey=k;renderWatch();}
  catch(e){document.getElementById('watch_body').innerHTML='<div class=sub>could not load watch data</div>';}
}
function utilCls(u){return u<50?'ured':(u<65?'uamb':'ugrn');}
function renderWatch(){
  if(!WATCH)return;const c=WATCH.config,P=WATCH.people;
  const eng=P.filter(p=>p.engraver), main=P.filter(p=>!p.engraver&&(p.cohort||p.flags.length)), insuff=P.filter(p=>!p.engraver&&!p.cohort&&!p.flags.length);
  let h='<div class=mbox><h3>How to read this — leads, not verdicts</h3>';
  h+='A single metric always lies: fast pace hides that someone barely showed up; long hours hide that they were idle. This weighs <b>pace + hours + utilization + output + attendance</b> together and flags specific patterns with the evidence. A flag is a place to <b>look</b>, not a verdict — a low number can be legit (e.g. waiting on restock). Investigate anyone flagged with the activity-audit / performance-review tools.<br>';
  h+='<b>Standard (full-timers):</b> 50h/week = 10h/day × 5 days. This window&rsquo;s target: ~<b>'+c.exp_days+' work-days / '+c.exp_hours+'h</b>. <b>Utilization</b> = active time (45-min-break rule) ÷ time on floor. <b>Interns are part-time</b>, so the hours/attendance flags (under-hours, short-shifts, missed-days) don&rsquo;t apply to them — only output &amp; utilization do.<br>';
  h+='<b>Flags:</b> Bursty/idle (util &lt;'+c.util_low+'%) · Under hours (&lt;70% of target) · Short shifts (avg &lt;'+c.short_day_hr+'h/day) · Missed days (check the <b>PTO app</b>) · Low output (bottom '+c.out_bottom_pct+'%) · Fast-but-low-total · Inconsistent.<br>';
  h+='<b>Presence</b> comes from scan timestamps (a lower bound on real clock time) and does not yet cross-check the PTO app or scheduled shifts — so verify hours/attendance flags there. <b>Engravers ('+c.engravers.join(', ')+')</b> are shown separately and un-flagged for now, since engraving time isn&rsquo;t cleanly tracked.</div>';
  h+='<div class=card><h2>Flags &amp; profiles</h2><div class=sub style=margin:0>Most-flagged first. Hover a chip for the evidence. Util is coloured (red &lt;50%, amber 50–65%, green &gt;65%).</div>';
  h+='<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Type</th><th>Output</th><th>Output/hr<span class=s> pick+pack+engrave</span></th><th>Active h</th><th>Floor h</th><th>Util</th><th>Days</th><th>Avg/day</th><th style=text-align:left>Flags</th></tr>';
  main.forEach(p=>{
    h+='<tr><td class=name>'+esc(p.person)+'</td><td><span class="badge '+(p.type==='Intern'?'in':'ft')+'">'+(p.type==='Intern'?'Intern':(p.type?'FT':'—'))+'</span></td>';
    h+='<td>'+fmt(p.output)+'</td><td>'+fmt(p.pace)+'</td><td>'+p.active_hr+'</td><td>'+p.floor_hr+'</td><td class='+utilCls(p.util)+'>'+p.util+'%</td><td>'+p.days+'</td><td>'+p.avg_span+'h</td>';
    h+='<td style=text-align:left>'+(p.flags.length?p.flags.map(x=>'<span class="wchip '+x.sev+'" title="'+esc(x.d)+'">'+x.t+'</span>').join(''):'<span class=wok>✓ clear</span>')+'</td></tr>';
  });
  h+='</table></div></div>';
  if(eng.length){h+='<div class=card style=margin-top:16px><h2>Engravers <span style="color:#94a3b8;font-weight:400">— separate; engraving time not fully tracked, not flagged yet</span></h2>';
    h+='<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Output</th><th>Pace</th><th>Active h</th><th>Floor h</th><th>Util</th><th>Days</th></tr>';
    eng.forEach(p=>{h+='<tr><td class=name>'+esc(p.person)+'</td><td>'+fmt(p.output)+'</td><td>'+fmt(p.pace)+'</td><td>'+p.active_hr+'</td><td>'+p.floor_hr+'</td><td>'+p.util+'%</td><td>'+p.days+'</td></tr>';});
    h+='</table></div></div>';}
  if(insuff.length)h+='<div class=note style=margin-top:14px><b>Not enough data to assess:</b> '+insuff.map(p=>p.person+' <span class=ins>('+p.days+' day'+(p.days===1?'':'s')+')</span>').join(', ')+'</div>';
  document.getElementById('watch_body').innerHTML=h;
}
// ===== Board settings drawer: hides filters + panel nav so the home stays TV-clean =====
function openDrawer(){document.getElementById('drawer').classList.add('open');document.getElementById('scrim').classList.add('open');}
function closeDrawer(){document.getElementById('drawer').classList.remove('open');document.getElementById('scrim').classList.remove('open');}
var _ctlSel=['.ctl','#ctl1','#ctl2'];
function _ctlEls(){return _ctlSel.map(function(s){return document.querySelector(s);}).filter(Boolean);}
function moveControlsToDrawer(){var host=document.getElementById('drawerctl');_ctlEls().forEach(function(el){if(el.parentNode!==host)host.appendChild(el);});}
function restoreControls(){var wrap=document.querySelector('.wrap'),anchor=document.getElementById('summary');_ctlEls().forEach(function(el){wrap.insertBefore(el,anchor);});}
function buildDrawerNav(){var nav=document.getElementById('drawernav');if(!nav||nav.childElementCount)return;
  document.querySelectorAll('.tabs .tab').forEach(function(tb){var b=document.createElement('button');b.textContent=tb.textContent;b.dataset.tab=tb.dataset.tab;if(tb.classList.contains('on'))b.classList.add('cur');
    b.onclick=function(){tab(tb.dataset.tab);document.querySelectorAll('#drawernav button').forEach(function(x){x.classList.remove('cur');});b.classList.add('cur');closeDrawer();};nav.appendChild(b);});}
function toggleDark(){document.body.classList.toggle('dark');var on=document.body.classList.contains('dark');
  var b=document.getElementById('darktoggle');if(b)b.textContent=on?'Switch to light':'Switch to dark (TV)';
  try{localStorage.setItem('wh_dark',on?'1':'0');}catch(e){} if(DATA)render();}
function toggleTV(){var on=!document.body.classList.contains('clean');document.body.classList.toggle('clean',on);
  if(on)moveControlsToDrawer();else restoreControls();
  var b=document.getElementById('tvtoggle');if(b)b.textContent=on?'Exit TV mode (full controls)':'Enter TV mode (clean board)';
  try{localStorage.setItem('wh_tv',on?'1':'0');}catch(e){} setTimeout(function(){if(chart)chart.resize();},60);}
function initView(){buildDrawerNav();
  var tv='1',dk='0';try{var a=localStorage.getItem('wh_tv');if(a!==null)tv=a;var d=localStorage.getItem('wh_dark');if(d!==null)dk=d;}catch(e){}
  if(dk==='1')document.body.classList.add('dark');
  if(tv==='1'){document.body.classList.add('clean');moveControlsToDrawer();}
  var tb=document.getElementById('tvtoggle');if(tb)tb.textContent=document.body.classList.contains('clean')?'Exit TV mode (full controls)':'Enter TV mode (clean board)';
  var db=document.getElementById('darktoggle');if(db)db.textContent=document.body.classList.contains('dark')?'Switch to light':'Switch to dark (TV)';
  setTimeout(function(){if(chart)chart.resize();},80);}
document.getElementById('from').value=etToday();document.getElementById('to').value=etToday();
load();initAuto();initView();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
