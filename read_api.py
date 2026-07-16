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

PERSON_TYPE = {
    "Nic Cox":"FT","Halil Gurler":"FT","Kadil Ladson":"FT","Manu Bekele":"FT",
    "Maurice Williams":"FT","Jeffrey Kwan":"FT","Shambria Green":"FT","Breton Rice":"FT",
    "Esra Altug":"Intern","Simay Guner":"Intern","Cindy Lin":"Intern",
    "Brennen Myrick":"Intern","Lara Nielsen":"Intern","Patrick Robin":"Intern",
    "Broghan Rice":"","Daniella Gross":"","Roland Tilk":"",
}

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp

def _range():
    return request.args.get("from"), request.args.get("to")

@app.route("/health")
def health():
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT count(*) FROM event"); n = cur.fetchone()[0]
    return jsonify(status="ok", events=n)

@app.route("/warehouse")
def warehouse():
    """Everything the dashboard needs for a date range, in one call."""
    frm, to = _range()
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH e AS (SELECT person,stage,subtype,source,order_number,quantity,ts,tote_barcode
                   FROM event WHERE et_day(ts) BETWEEN %s AND %s)
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
          min(ts)  FILTER (WHERE is_floor_labor(stage,subtype))              first_ts,
          max(ts)  FILTER (WHERE is_floor_labor(stage,subtype))              last_ts
        FROM e GROUP BY person""", [frm, to])
        rows = cur.fetchall()

        cur.execute("""
        WITH o AS (SELECT order_number, bool_or(source='shiphero') sh, bool_or(source='shopify') shop
                   FROM event WHERE stage='pack' AND order_number IS NOT NULL
                     AND et_day(ts) BETWEEN %s AND %s GROUP BY order_number)
        SELECT count(*) total, count(*) FILTER (WHERE sh) shiphero,
               count(*) FILTER (WHERE NOT sh AND shop) shopify_only,
               count(*) FILTER (WHERE sh AND shop) both FROM o""", [frm, to])
        shipped = cur.fetchone()

        cur.execute("""
        WITH ev AS (SELECT person, ts,
                      EXTRACT(epoch FROM (ts-lag(ts) OVER w))/60 gap, lag(ts) OVER w prev
                    FROM event WHERE et_day(ts) BETWEEN %s AND %s AND is_floor_labor(stage,subtype)
                    WINDOW w AS (PARTITION BY person ORDER BY ts)),
        mx AS (SELECT person,gap,prev,ts,row_number() OVER (PARTITION BY person ORDER BY gap DESC NULLS LAST) rn FROM ev)
        SELECT person, round(gap::numeric,0) gap_min, prev, ts FROM mx WHERE rn=1""", [frm, to])
        gaps = {r[0]: r for r in cur.fetchall()}

    people, floor = [], []
    tot = dict(pk_i=0,packsh_i=0,packshop_i=0,eng_i=0,pk_o=0,packsh_o=0,packshop_o=0,eng_o=0,repl=0)
    for r in rows:
        (person,pk_i,pk_o,psh_i,psh_o,psp_i,psp_o,repl,eng_i,eng_o,pick_c,pack_c,ful_c,mov_c,cnt_c,eng_c,first,last)=r
        people.append(dict(person=person, type=PERSON_TYPE.get(person,""),
            items_picked_sh=pk_i, items_packed_sh=psh_i, items_packed_shop=psp_i,
            engraved_items=eng_i, engraved_totes=eng_c, engraved_orders=eng_o, replenished=repl,
            orders_picked_sh=pk_o, orders_packed_sh=psh_o, orders_packed_shop=psp_o))
        tot["pk_i"]+=pk_i; tot["packsh_i"]+=psh_i; tot["packshop_i"]+=psp_i; tot["eng_i"]+=eng_i
        tot["pk_o"]+=pk_o; tot["packsh_o"]+=psh_o; tot["packshop_o"]+=psp_o; tot["eng_o"]+=eng_o; tot["repl"]+=repl
        g = gaps.get(person)
        floor.append(dict(person=person, first_ts=first.isoformat() if first else None,
            last_ts=last.isoformat() if last else None,
            gap_min=(int(g[1]) if g and g[1] is not None else 0),
            gap_from=(g[2].isoformat() if g and g[2] else None),
            gap_to=(g[3].isoformat() if g and g[3] else None),
            mix=dict(pick=pick_c, pack=pack_c, fulfill=ful_c, move=mov_c, count=cnt_c, engrave=eng_c)))
    return jsonify(range={"from":frm,"to":to},
        shipped=dict(total=shipped[0], shiphero=shipped[1], shopify_only=shipped[2], both=shipped[3]),
        totals=tot, people=people, floor=floor)

# ---------------- Speed & Rankings ----------------
# How fast each person works at each activity, so the right people get assigned to the right task.
# Method (all params visible on the page): sort each person's scans for a stage in time; the gap to the
# next scan is the time to do that unit of work. A gap LONGER than the stage's BREAK threshold means they
# stopped (lunch / switched task / stepped away) and is dropped from active time — so absence never looks
# like slowness, but many small gaps (genuine slowness) do count. Near-simultaneous scans (<=5s apart) are
# collapsed into one "chunk" first, which fixes replenishment (bulk pallet scans stamped at the same second).
# Break thresholds are set just above each task's normal per-item time, from that task's own gap distribution.
SPEED_IDLE = {"pick":300, "pack":1200, "engrave":600, "replenish":600}   # seconds; gap beyond = a break
SPEED_SRC  = {"pick":"shiphero","pack":"shiphero","replenish":"shiphero","engrave":"logger"}
SPEED_BURST = 5          # scans within this many seconds = one physical action (chunk)
SPEED_GATE = {"min_intervals":30, "min_days":2, "min_active_min":15}  # ranked only if all three met
SPEED_UNIT = {"pick":"items","pack":"items","engrave":"totes","replenish":"boxes"}
# Rate mode: "units" = units per active hour (pick/pack/engrave). "moves" = discrete actions per active
# hour — replenish is ranked as BOXES/hr, because a 40-unit box isn't 40x the work of a 1-unit move, so
# units/hr would just rank box size, not speed.
SPEED_RATE = {"pick":"units","pack":"units","engrave":"units","replenish":"moves"}
# Per-stage row filter (a raw SQL fragment built ONLY from these constants — never user input).
# Replenish counts only real boxes moved in (bin transfers); the qty=1 tote moves (98% of replenish rows)
# aren't replenishment and are excluded.
SPEED_FILTER = {"replenish":"AND (raw->>'reason') NOT ILIKE '%%tote%%' AND quantity>1"}

@app.route("/speed")
def speed():
    """Per person x stage working speed (units per ACTIVE hour) for the selected window."""
    frm, to = _range()
    rows_by_stage = {}
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        for stage, idle in SPEED_IDLE.items():
            flt = SPEED_FILTER.get(stage, "")   # constant-only SQL fragment (see SPEED_FILTER)
            cur.execute(f"""
            WITH r AS (
              SELECT person, quantity, ts,
                EXTRACT(epoch FROM (ts - lag(ts) OVER (PARTITION BY person ORDER BY ts))) g
              FROM event WHERE stage=%s AND source=%s {flt} AND et_day(ts) BETWEEN %s AND %s),
            ch AS (  -- collapse <=5s bursts into one chunk (one physical action)
              SELECT person, quantity, ts,
                sum(CASE WHEN g IS NULL OR g > %s THEN 1 ELSE 0 END)
                    OVER (PARTITION BY person ORDER BY ts) cid
              FROM r),
            chunks AS (SELECT person, cid, sum(quantity) units, min(ts) st
                       FROM ch GROUP BY person, cid),
            iv AS (  -- gap between consecutive chunks = time to process one chunk/box
              SELECT person, units,
                EXTRACT(epoch FROM (st - lag(st) OVER (PARTITION BY person ORDER BY st))) gap,
                (st AT TIME ZONE 'America/New_York')::date d
              FROM chunks)
            SELECT person,
              count(*)              FILTER (WHERE gap>0 AND gap<=%s)                         n,
              round(sum(gap)        FILTER (WHERE gap>0 AND gap<=%s)/60.0, 1)                active_min,
              sum(units)            FILTER (WHERE gap>0 AND gap<=%s)                         units,
              count(DISTINCT d)     FILTER (WHERE gap>0 AND gap<=%s)                         days,
              round(3600.0*sum(units) FILTER (WHERE gap>0 AND gap<=%s)
                    / nullif(sum(gap) FILTER (WHERE gap>0 AND gap<=%s),0), 0)               uph,
              round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap/nullif(units,0))
                    FILTER (WHERE gap>0 AND gap<=%s)::numeric, 0)                           med_spi,
              round(percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)
                    FILTER (WHERE gap>0 AND gap<=%s)::numeric, 0)                           med_move
            FROM iv GROUP BY person""",
            [stage, SPEED_SRC[stage], frm, to, SPEED_BURST] + [idle]*8)
            movemode = (SPEED_RATE[stage]=="moves")
            out = []
            for (person,n,amin,units,days,uph,med_spi,med_move) in cur.fetchall():
                if not n: continue
                ranked = (n>=SPEED_GATE["min_intervals"] and (days or 0)>=SPEED_GATE["min_days"]
                          and (amin or 0)>=SPEED_GATE["min_active_min"])
                reason = ""
                if not ranked:
                    bits=[]
                    if n<SPEED_GATE["min_intervals"]: bits.append(f"only {n} sample"+("s" if n!=1 else ""))
                    if (days or 0)<SPEED_GATE["min_days"]: bits.append(f"only {days or 0} day"+("s" if (days or 0)!=1 else ""))
                    if (amin or 0)<SPEED_GATE["min_active_min"]: bits.append(f"only {amin or 0} active min")
                    reason=", ".join(bits)
                if movemode:                              # replenish -> boxes/hr, median sec/box
                    rate = round(n*60.0/float(amin)) if amin else 0
                    med  = int(med_move) if med_move is not None else None
                else:                                     # pick/pack/engrave -> units/hr, median sec/item
                    rate = int(uph) if uph is not None else 0
                    med  = int(med_spi) if med_spi is not None else None
                out.append(dict(person=person, type=PERSON_TYPE.get(person,""),
                    uph=rate, med_spi=med, n=int(n),
                    active_min=float(amin) if amin is not None else 0.0,
                    days=int(days or 0), units=int(units) if units is not None else 0,
                    moves=int(n), ranked=ranked, reason=reason))
            rows_by_stage[stage]=out
    cfg=dict(idle_min={s:v//60 for s,v in SPEED_IDLE.items()}, burst_s=SPEED_BURST,
             gate=SPEED_GATE, unit=SPEED_UNIT, source=SPEED_SRC, rate=SPEED_RATE)
    return jsonify(range={"from":frm,"to":to}, config=cfg, stages=rows_by_stage)

# ---------------- Watch List (metric-based flags) ----------------
# A single metric always lies: pace ignores whether you showed up; hours ignore whether you worked.
# So this looks at pace + hours + UTILIZATION (active/floor) + output + attendance + consistency together,
# and raises specific, evidence-bearing flags. It is a lead, not a verdict (a low number can be legit —
# e.g. waiting on restock). Scan-only for now; scheduled-shift adherence is a planned add-on.
WATCH_IDLE = 900   # gap (s) beyond which floor time is idle, for the active-time part
# Standard: everyone is expected to work 50h/week = 10h/day x 5 days (can be split up).
WATCH = {"util_low":50, "min_floor_hr":6, "target_day_hr":10, "target_days_wk":5, "short_day_hr":7,
         "pace_hi_pct":67, "out_lo_pct":33, "out_bottom_pct":25, "incon_ratio":2.5}
# Engravers are shown in a SEPARATE, un-flagged group for now: engraving time isn't cleanly tracked,
# so their utilization / hours / output read artificially low and shouldn't be flagged yet.
WATCH_ENGRAVERS = {"Manu Bekele","Maurice Williams","Halil Gurler"}

@app.route("/watch")
def watch():
    frm, to = _range()
    try:
        wdays=(dt.date.fromisoformat(to)-dt.date.fromisoformat(frm)).days+1
    except Exception:
        wdays=7
    exp_days=max(1, round(wdays*WATCH["target_days_wk"]/7.0))       # expected work-days in this window
    exp_hours=WATCH["target_day_hr"]*exp_days                       # expected floor hours in this window
    with connect() as c, c.cursor(row_factory=tuple_row) as cur:
        cur.execute("""
        WITH ev AS (
          SELECT person, ts, quantity, stage,
            EXTRACT(epoch FROM (ts - lag(ts) OVER (PARTITION BY person,
                  (ts AT TIME ZONE 'America/New_York')::date ORDER BY ts))) gap
          FROM event
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
        if not eng:   # engravers exempt for now (engraving time not cleanly tracked)
            if p["floor_hr"]>=WATCH["min_floor_hr"] and p["util"]<WATCH["util_low"]:
                f.append(dict(t="Bursty / idle", d=f"on floor {p['floor_hr']}h but active only {p['active_hr']}h ({p['util']}%)", sev="r"))
            if p["floor_hr"] < 0.7*exp_hours and (p["days"]>=2 or exp_days<=2):
                f.append(dict(t="Under hours", d=f"~{p['floor_hr']}h on floor vs ~{exp_hours}h target (50h/wk) — verify vs PTO", sev="r"))
            if p["days"]>=2 and p["avg_span"]<WATCH["short_day_hr"]:
                f.append(dict(t="Short shifts", d=f"averages {p['avg_span']}h/day vs 10h target", sev="r"))
            if exp_days>=3 and p["days"] < exp_days-1:
                f.append(dict(t="Missed days", d=f"present {p['days']} of ~{exp_days} expected days — check PTO app", sev="r"))
            if inc and p["out_pct"]<=WATCH["out_bottom_pct"] and p["floor_hr"]>=WATCH["min_floor_hr"]:
                f.append(dict(t="Low output", d=f"{p['output']} items — bottom {WATCH['out_bottom_pct']}% despite {p['floor_hr']}h on floor", sev="r"))
            if inc and p["pace_pct"]>=WATCH["pace_hi_pct"] and p["out_pct"]<=WATCH["out_lo_pct"]:
                f.append(dict(t="Fast but low total", d=f"top-tier pace but low total output ({p['output']})", sev="a"))
            if p["days"]>=3 and p["med_day"]>0 and p["best_day"]>=WATCH["incon_ratio"]*p["med_day"]:
                f.append(dict(t="Inconsistent", d=f"best day {p['best_day']} vs typical {p['med_day']}/day", sev="a"))
        p["flags"]=f
    hard=lambda p:sum(1 for x in p["flags"] if x["sev"]=="r")
    ppl.sort(key=lambda p:(0 if p["flags"] else 1, -hard(p), -len(p["flags"]), p["util"]))
    return jsonify(range={"from":frm,"to":to},
        config=dict(idle_min=WATCH_IDLE//60, exp_days=exp_days, exp_hours=exp_hours,
                    engravers=sorted(WATCH_ENGRAVERS), **WATCH), people=ppl)

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
  --ink:#0f172a;--ink-2:#475569;--muted:#94a3b8;
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
.s-sh{color:var(--accent)}.s-shop{color:var(--amber)}.s-repl{color:var(--violet)}.s-sel{color:var(--ink)}.s-eng{color:var(--teal)}
.note{color:var(--ink-2);font-size:12.5px;margin:14px 0;line-height:1.55}
h2{font-size:15px;font-weight:700;margin:0 0 4px;letter-spacing:-.01em}
.chartwrap{height:360px;margin-top:12px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}
th,td{padding:10px 12px;border-bottom:1px solid var(--line-2);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left;padding-left:4px}
th:last-child,td:last-child{padding-right:4px}
th{color:var(--muted);font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;cursor:pointer;user-select:none}
th:hover{color:var(--ink-2)}
th .s{color:#b8c0cc;font-weight:500;text-transform:none;letter-spacing:0}
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
.badge.ft{background:var(--accent-weak);color:var(--accent)}.badge.in{background:#f3f0ff;color:var(--violet)}
.o{color:var(--amber)}.p{color:var(--violet);font-weight:600}.eng{color:var(--teal);font-weight:600}
tr.tot td{font-weight:700;color:var(--ink);border-top:1.5px solid var(--line);background:#fcfcfd}
.red{color:var(--red);font-weight:600}
.lunch{background:#fef3c7;color:#92400e;border-radius:20px;padding:1px 8px;font-size:10.5px;font-weight:600;margin-left:6px}
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
.wchip{display:inline-block;padding:2px 8px;border-radius:6px;font-size:10.5px;font-weight:600;margin:2px 4px 2px 0;white-space:nowrap;cursor:default}
.wchip.r{background:#fef2f2;color:#b91c1c}
.wchip.a{background:#fffbeb;color:#b45309}
.wok{color:var(--green);font-weight:600;font-size:12px}
.ured{color:var(--red);font-weight:700}.uamb{color:var(--amber);font-weight:700}.ugrn{color:var(--green);font-weight:700}
@media(max-width:1100px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(max-width:900px){.spgrid{grid-template-columns:1fr}.wrap{padding:20px 16px 80px}}
</style></head><body><div class=wrap>
<div class=apphead><h1>Warehouse Picking &amp; Packing</h1><span class=dot></span><span class=live>Live</span></div>
<div class=sub>Live contribution from ShipHero <b>+ direct-in-Shopify fulfillments + engraving</b>, PTO-aware. <b>Fulfillment</b> (pick + pack + engrave) and <b>Replenishment</b> are two separate tracks.</div>
<div class=tabs>
  <div class="tab on" data-tab=dash onclick="tab('dash')">Dashboard</div>
  <div class=tab data-tab=floor onclick="tab('floor')">Floor Time</div>
  <div class=tab data-tab=an onclick="tab('an')">Analytics</div>
  <div class=tab data-tab=speed onclick="tab('speed')">Speed &amp; Rankings</div>
  <div class=tab data-tab=watch onclick="tab('watch')">Watch List</div>
</div>
<div class=ctl>
  <button class=pill data-preset=today onclick="preset('today')">Today</button>
  <button class=pill data-preset=yest onclick="preset('yest')">Yesterday</button>
  <button class=pill data-preset=week onclick="preset('week')">This week</button>
  <button class="pill on" data-preset=7 onclick="preset('7')">Last 7 days</button>
  <button class=pill data-preset=30 onclick="preset('30')">Last 30 days</button>
  <input type=date id=from> <span style=color:#9ca3af>to</span> <input type=date id=to>
  <button class=pill onclick="load()">Apply</button><span id=status></span>
</div>
<div class=ctl>
  <span class=lbl>Unit</span><span class=seg id=unit><button class=on data-v=both>Both</button><button data-v=items>Items</button><button data-v=orders>Orders</button></span>
  <span class=lbl style=margin-left:14px>Stage</span><span class=seg id=stage><button class=on data-v=all>All</button><button data-v=pick>Picked</button><button data-v=pack>Packed</button><button data-v=engrave>Engraved</button><button data-v=repl>Replenished</button></span>
  <span class=lbl style=margin-left:14px>Source</span><span class=seg id=source><button class=on data-v=both>Both</button><button data-v=shiphero>ShipHero</button><button data-v=shopify>Shopify</button></span>
  <div class=spacer></div>
  <button class=pill onclick="copyChat()">Copy for chat</button>
  <button class=pill onclick="dl('csv')">CSV</button>
  <button class=pill onclick="dl('json')">JSON</button>
</div>
<div class=ctl>
  <span class=seg id=team class="seg gray"><button class=on data-v=all>Everyone</button><button data-v=FT>Full-timers</button><button data-v=Intern>Interns</button></span>
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
    <div class=sub style=margin:0>Pick, pack &amp; engrave are combined into one <b>Fulfillment</b> figure (breakdown shown beneath each number). <b>Restocked</b> is separate. Click any column header to sort.</div>
    <div id=detail></div>
  </div>
</div>

<div id=floor class=hide>
  <div class=card>
    <h2>Floor Time <span style="color:#9ca3af;font-weight:400">&mdash; when each person was actually active, and their gaps</span></h2>
    <div class=sub style=margin:0><b>Team-wide.</b> All floor activity &mdash; picking, packing, replenishing, engraving &mdash; attributed by user, in Eastern time.</div>
    <div id=floortable></div>
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
    <h2>Who&rsquo;s best at what &mdash; assignment matrix</h2>
    <div class=sub style=margin:0>Each cell = units/active-hr with a percentile bar within that activity (green = fast). <b>Best fit</b> = the activity where the person ranks highest. Grey dot = has some data but not enough to rank; blank = never did it.</div>
    <div id=speed_matrix style=margin-top:8px></div>
  </div>
</div>

<div id=watch class=hide><div id=watch_body></div></div>

<div class=foot>
  <b>Chart colours:</b> <span class=s-sh>Picked&middot;ShipHero</span>, <span style=color:#16a34a>Packed&middot;ShipHero</span>, <span class=s-shop>Packed&middot;Shopify</span>, <span class=s-eng>Engraved</span> &mdash; these four stack into the <b>Fulfillment</b> bar. <span class=s-repl>Replenished</span> is drawn as its own separate bar (a parallel track, never added into the fulfillment/items total).
</div>
</div>
<script>
const C={pick:'#2563eb',pack:'#16a34a',fulfill:'#d97706',repl:'#7c3aed',engrave:'#0d9488'};
if(window.Chart){Chart.defaults.font.family="'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif";Chart.defaults.font.size=12;Chart.defaults.color='#475569';Chart.defaults.plugins.legend.labels.usePointStyle=true;Chart.defaults.plugins.legend.labels.boxWidth=8;Chart.defaults.plugins.legend.labels.boxHeight=8;Chart.defaults.plugins.legend.labels.padding=16;}
let DATA=null, sortKey='items_total', sortDir=-1, chart=null;
function etToday(){return new Date(Date.now()-4*3600*1000).toISOString().slice(0,10);}
function etAgo(n){return new Date(Date.now()-4*3600*1000-n*86400000).toISOString().slice(0,10);}
function segval(id){return document.querySelector('#'+id+' button.on').dataset.v;}
function seg(id,v){document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));render();}
document.querySelectorAll('.seg').forEach(s=>s.addEventListener('click',e=>{if(e.target.dataset.v){seg(s.id,e.target.dataset.v);}}));
function tab(t){['dash','floor','an','speed','watch'].forEach(x=>{document.getElementById(x).classList.toggle('hide',x!==t);});
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('on',el.dataset.tab===t));
  if(t==='speed')loadSpeed();if(t==='watch')loadWatch();}
function preset(p){document.querySelectorAll('.pill[data-preset]').forEach(b=>b.classList.toggle('on',b.dataset.preset===p));
  let f=etToday(),t=etToday();
  if(p==='yest'){f=t=etAgo(1);}else if(p==='7'){f=etAgo(6);}else if(p==='30'){f=etAgo(29);}
  else if(p==='week'){const d=new Date(Date.now()-4*3600*1000);f=etAgo((d.getUTCDay()+6)%7);}
  document.getElementById('from').value=f;document.getElementById('to').value=t;load();}
async function getj(u){for(let i=0;i<8;i++){try{const r=await fetch(u);if(r.ok)return await r.json();}catch(e){}
  document.getElementById('status').textContent='waking server ('+(i+1)+')…';await new Promise(s=>setTimeout(s,4000));}throw 0;}
async function load(){document.getElementById('status').textContent='loading…';
  try{DATA=await getj('/warehouse?from='+document.getElementById('from').value+'&to='+document.getElementById('to').value);
  document.getElementById('status').textContent='';render();
  if(!document.getElementById('speed').classList.contains('hide')){speedKey=null;loadSpeed();}
  if(!document.getElementById('watch').classList.contains('hide')){watchKey=null;loadWatch();}
  }catch(e){document.getElementById('status').textContent='could not reach API';}}
function fmt(n){return (n||0).toLocaleString();}
function fmtmin(m){if(m==null)return '—';const h=Math.floor(m/60),x=Math.round(m%60);return h?h+'h '+x+'m':x+'m';}
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
    fmt(actItems)+' fulfillment items ('+fmt(T.pk_i)+' picked + '+fmt(T.packsh_i)+' packed·SH + '+fmt(T.packshop_i)+' packed·Shopify + '+fmt(T.eng_i)+' engraved) &middot; '+fmt(T.repl)+' replenished &middot; '+DATA.range.from;
  document.getElementById('shipped').innerHTML='<div class=big>'+fmt(sh.total)+'</div><div class=t>orders shipped out the door</div>'+
    '<div class=d><b>'+fmt(sh.shiphero)+'</b> ShipHero &middot; <span class=o>'+fmt(sh.shopify_only)+'</span> Shopify-only ('+fmt(sh.both)+' ShipHero orders were finished by hand in Shopify)</div>';
  // ---- stat cards (honor toggles) ----
  const showItems=unit!=='orders', showOrders=unit!=='items';
  const itemsSel=(v.pick?T.pk_i:0)+(v.packsh?T.packsh_i:0)+(v.packshop?T.packshop_i:0)+(v.eng?T.eng_i:0);
  const ordersSel=(v.pick?T.pk_o:0)+(v.packsh?T.packsh_o:0)+(v.packshop?T.packshop_o:0)+(v.eng?(T.eng_o||0):0);
  const si=document.getElementById('statsItems'), so=document.getElementById('statsOrders');
  si.innerHTML=!showItems?'':[
    card('Items picked','ShipHero','s-sh',v.pick?T.pk_i:0),
    card('Items packed','ShipHero','s-sh',v.packsh?T.packsh_i:0),
    card('Items packed','Shopify','s-shop',v.packshop?T.packshop_i:0),
    card('Items engraved','logger','s-eng',v.eng?T.eng_i:0),
    card('Items — total','fulfillment','s-sel',itemsSel)].join('');
  so.innerHTML=!showOrders?'':[
    card('Orders picked','ShipHero','s-sh',v.pick?T.pk_o:0),
    card('Orders packed','ShipHero','s-sh',v.packsh?T.packsh_o:0),
    card('Orders packed','Shopify','s-shop',v.packshop?T.packshop_o:0),
    card('Replenished','units·separate','s-repl',v.repl?T.repl:0),
    card('Orders — total','fulfillment','s-sel',ordersSel)].join('');
  si.classList.toggle('hide',!showItems);so.classList.toggle('hide',!showOrders);
  drawChart(ppl,v);
  drawDetail(ppl,unit,v);
  drawFloor();
  drawAnalytics(ppl,v);
}
function card(k,s,cls,val){return '<div class="card stat"><div class=k>'+k+' <span class="s '+cls+'">'+s+'</span></div><div class=v>'+fmt(val)+'</div></div>';}
function drawChart(ppl,v){
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
      if(sum>0&&x!=null){ctx.fillStyle='#334155';ctx.fillText(sum.toLocaleString(),x,topY-3);}});});
    ctx.restore();}};
  if(chart)chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'bar',data:{labels,datasets:ds},
    options:{responsive:true,maintainAspectRatio:false,layout:{padding:{top:24}},
      scales:{x:{stacked:true,grid:{display:false},ticks:{autoSkip:false,maxRotation:40,font:{size:11}}},
              y:{stacked:true,beginAtZero:true,grid:{color:'#eef1f5'},
                 title:{display:true,text:(ord?'Orders':'Items')+' fulfilled',color:'#64748b',font:{size:11,weight:'600'}}}},
      plugins:{
        title:{display:true,text:'Fulfillment '+ulbl+' per person — tallest first'+((v.repl&&!ord)?'  (Restocked shown as a separate bar)':''),
               color:'#0f172a',font:{size:13,weight:'600'},padding:{bottom:12}},
        legend:{position:'bottom'},
        tooltip:{callbacks:{footer:(items)=>{let f=0;items.forEach(i=>{if(i.dataset.stack==='ful')f+=i.parsed.y;});
          return f?'Fulfillment '+ulbl+': '+f.toLocaleString():'';}}}}},
    plugins:[stackTotals]});
}
function drawDetail(ppl,unit,v){
  // Simplified layout: pick + pack + engrave collapse into ONE Fulfillment figure (items AND orders),
  // shown right next to the name; restocking is its own separate column. Every header is click-to-sort.
  const teamItems=ppl.reduce((a,p)=>a+fulItems(p,v),0)||1;
  const arr=ppl.map(p=>({person:p.person, type:p.type,
    items:fulItems(p,v), orders:fulOrders(p,v), restock:(v.repl?p.replenished:0),
    _pick:(v.pick?p.items_picked_sh:0),
    _pack:(v.packsh?p.items_packed_sh:0)+(v.packshop?p.items_packed_shop:0),
    _eng:(v.eng?p.engraved_items:0)}));
  arr.forEach(p=>p.share=Math.round(p.items/teamItems*100));
  // map legacy sort keys onto the collapsed columns
  const K={items_total:'items',orders_total:'orders',replenished:'restock'}[sortKey]||sortKey;
  arr.sort((a,b)=>{const x=a[K],y=b[K];return (x>y?1:(x<y?-1:0))*sortDir;});
  const arw=k=>sortKey===k?'<span class=arw>'+(sortDir<0?'▼':'▲')+'</span>':'';
  const th=(k,lab,sub)=>'<th class="srt'+(sortKey===k?' act':'')+'" onclick="sortBy(\''+k+'\')">'+lab+(sub?' <span class=s>'+sub+'</span>':'')+arw(k)+'</th>';
  let h='<table><tr>'+
    th('person','Person','')+
    '<th class=srt onclick="sortBy(\'type\')">Type'+arw('type')+'</th>'+
    th('items_total','Fulfillment items','pick + pack + engrave')+
    th('orders_total','Fulfillment orders','')+
    th('replenished','Restocked','separate track')+
    th('share','Share','of team')+
    '</tr>';
  const T={items:0,orders:0,restock:0};
  arr.forEach(p=>{
    const parts=[]; if(p._pick)parts.push('Pick '+fmt(p._pick)); if(p._pack)parts.push('Pack '+fmt(p._pack)); if(p._eng)parts.push('Engrave '+fmt(p._eng));
    const brk=parts.length?'<div class=brk>'+parts.join(' &middot; ')+'</div>':'';
    h+='<tr><td class=name>'+p.person+'</td>'+
      '<td><span class="badge '+(p.type==='Intern'?'in':'ft')+'">'+(p.type==='Intern'?'Intern':(p.type?'Full-timer':'—'))+'</span></td>'+
      '<td class=fi><b>'+fmt(p.items)+'</b>'+brk+'</td>'+
      '<td>'+fmt(p.orders)+'</td>'+
      '<td class=p>'+fmt(p.restock)+'</td>'+
      '<td class=shr>'+p.share+'%</td></tr>';
    T.items+=p.items;T.orders+=p.orders;T.restock+=p.restock;});
  h+='<tr class=tot><td>Total</td><td></td><td><b>'+fmt(T.items)+'</b></td><td>'+fmt(T.orders)+'</td><td class=p>'+fmt(T.restock)+'</td><td>100%</td></tr>';
  h+='</table>';
  document.getElementById('detail').innerHTML='<div class=tablewrap>'+h+'</div>';
}
function sortBy(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();}
function drawFloor(){const f=[...DATA.floor].filter(x=>DATA.people.find(p=>p.person===x.person&&teamFilter(p)));
  f.sort((a,b)=>b.gap_min-a.gap_min);
  let h='<table><tr><th>Person</th><th>First (ET)</th><th>Last (ET)</th><th>On floor</th><th>Biggest gap (ET)</th><th style=text-align:left>Activity mix</th></tr>';
  f.forEach(r=>{const span=r.first_ts&&r.last_ts?(new Date(r.last_ts)-new Date(r.first_ts))/60000:0;
    const lunch=r.gap_min>=25&&r.gap_min<=90?'<span class=lunch>lunch?</span>':'';
    const mix=[r.mix.pick?'pick '+r.mix.pick:'',r.mix.pack?'pack '+r.mix.pack:'',r.mix.move?'replenish '+r.mix.move:'',r.mix.fulfill?'shopify '+r.mix.fulfill:'',r.mix.engrave?'engrave '+r.mix.engrave:''].filter(Boolean).join(' · ');
    h+='<tr><td class=name>'+r.person+'</td><td>'+ampm(r.first_ts)+'</td><td>'+ampm(r.last_ts)+'</td><td>~'+fmtmin(Math.round(span))+'</td>'+
      '<td style=text-align:left><span class=red>'+fmtmin(r.gap_min)+'</span> <span style=color:#9ca3af>'+ampm(r.gap_from)+'–'+ampm(r.gap_to)+'</span>'+lunch+'</td><td style=text-align:left>'+mix+'</td></tr>';});
  h+='</table>';document.getElementById('floortable').innerHTML='<div class=tablewrap>'+h+'</div>';}
function drawAnalytics(ppl,v){const arr=ppl.map(p=>fulItems(p,v)).sort((a,b)=>a-b);
  const sum=arr.reduce((a,b)=>a+b,0),mean=arr.length?Math.round(sum/arr.length):0,med=arr.length?arr[Math.floor(arr.length/2)]:0;
  document.getElementById('analytics').innerHTML='<div class=cards>'+card('People','active','s-sel',ppl.length)+card('Mean items','fulfillment','s-sel',mean)+card('Median items','fulfillment','s-sel',med)+card('Total items','fulfillment','s-sel',sum)+card('Replenished','separate','s-repl',ppl.reduce((a,p)=>a+(v.repl?p.replenished:0),0))+'</div>';}
function copyChat(){const v=vis();const rows=DATA.people.filter(teamFilter).map(p=>p.person+': fulfillment '+fulItems(p,v)+' (picked '+p.items_picked_sh+', packed '+(p.items_packed_sh+p.items_packed_shop)+', engraved '+p.engraved_items+'), replenished '+p.replenished);
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
  m+='Speed = <b>units per ACTIVE hour</b>. The gap between your consecutive scans of a task is the time to do that unit of work. ';
  m+='A gap longer than the task&rsquo;s <b>break threshold</b> means you stopped — lunch, switched task, or stepped away — and is removed, so time off never counts as slowness. But many small gaps (genuinely slow work) <b>do</b> count. Scans within <code>'+cfg.burst_s+'s</code> of each other are merged into one action.<br>';
  m+='<b>Break thresholds (a gap bigger than this = not working):</b> '+SP_STAGES.map(s=>cap(s)+' <code>&gt;'+cfg.idle_min[s]+' min</code>').join(' &middot; ')+'. Set just above each task&rsquo;s normal per-item time, from its own data.<br>';
  m+='<b>Replenishment</b> is ranked as <b>boxes moved per active hour</b> (each bin transfer = one box, any size) — not units/hr, which would just rank box size. The single-unit tote moves (98% of replenish scans) aren&rsquo;t replenishment and are excluded.<br>';
  m+='<b>Sample size — ranked only if</b> a person has <code>&ge;'+g.min_intervals+' samples</code> across <code>&ge;'+g.min_days+' days</code> with <code>&ge;'+g.min_active_min+' active min</code>; otherwise shown as &ldquo;insufficient&rdquo; with the reason. A &ldquo;sample&rdquo; = one timed unit-of-work.<br>';
  m+='<b>Window:</b> uses the date range above (<b>'+SPEED.range.from+' → '+SPEED.range.to+'</b>). Early-July data is less reliable — prefer a recent window and enough days.<br>';
  m+='<b>Scans only:</b> pick/pack/replenish from ShipHero, engraving from the logger. Shopify hand-fulfillment timing isn&rsquo;t granular enough for pace, and off-scanner work isn&rsquo;t captured.';
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
    b+='<div class=card style="padding:14px 16px"><div style="font-weight:700">'+cap(s)+' <span class=sub style="font-weight:400">('+cfg.unit[s]+'/active-hr · break &gt;'+cfg.idle_min[s]+'m)</span></div>';
    if(!rk.length)b+='<div class=sub style=margin-top:6px>No one has enough data yet in this window.</div>';
    else{b+='<table style=margin-top:6px><tr><th>#</th><th style=text-align:left>Person</th><th>'+cfg.unit[s]+'/hr</th><th>med s/ea</th><th>samples</th><th>days</th></tr>';
      rk.forEach((r,i)=>{b+='<tr><td>'+(i+1)+'</td><td class=name style=text-align:left>'+r.person+'</td><td><b style="color:'+pctColor(pct[r.person][s].p)+'">'+fmt(r.uph)+'</b></td><td>'+(r.med_spi==null?'—':r.med_spi)+'</td><td>'+r.n+'</td><td>'+r.days+'</td></tr>';});
      b+='</table>';}
    if(un.length)b+='<div class=sub style=margin-top:8px><b>Insufficient:</b> '+un.map(r=>r.person+' <span class=ins>('+r.reason+')</span>').join(', ')+'</div>';
    b+='</div>';
  });
  document.getElementById('speed_boards').innerHTML=b;
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
  x+='<div class=sub style=margin-top:8px><b>Active hrs (tracked)</b> = time between scans, breaks removed — a floor, not a full timesheet (off-scanner work isn&rsquo;t counted).</div>';
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
function esc(s){return (''+s).replace(/"/g,'&quot;');}
function renderWatch(){
  if(!WATCH)return;const c=WATCH.config,P=WATCH.people;
  const eng=P.filter(p=>p.engraver), main=P.filter(p=>!p.engraver&&(p.cohort||p.flags.length)), insuff=P.filter(p=>!p.engraver&&!p.cohort&&!p.flags.length);
  let h='<div class=mbox><h3>How to read this — leads, not verdicts</h3>';
  h+='A single metric always lies: fast pace hides that someone barely showed up; long hours hide that they were idle. This weighs <b>pace + hours + utilization + output + attendance</b> together and flags specific patterns with the evidence. A flag is a place to <b>look</b>, not a verdict — a low number can be legit (e.g. waiting on restock). Investigate anyone flagged with the activity-audit / performance-review tools.<br>';
  h+='<b>Standard:</b> 50h/week = 10h/day × 5 days. This window&rsquo;s target: ~<b>'+c.exp_days+' work-days / '+c.exp_hours+'h</b>. <b>Utilization</b> = active scan time ÷ time on floor.<br>';
  h+='<b>Flags:</b> Bursty/idle (util &lt;'+c.util_low+'%) · Under hours (&lt;70% of target) · Short shifts (avg &lt;'+c.short_day_hr+'h/day) · Missed days (check the <b>PTO app</b>) · Low output (bottom '+c.out_bottom_pct+'%) · Fast-but-low-total · Inconsistent.<br>';
  h+='<b>Presence</b> comes from scan timestamps (a lower bound on real clock time) and does not yet cross-check the PTO app or scheduled shifts — so verify hours/attendance flags there. <b>Engravers ('+c.engravers.join(', ')+')</b> are shown separately and un-flagged for now, since engraving time isn&rsquo;t cleanly tracked.</div>';
  h+='<div class=card><h2>Flags &amp; profiles</h2><div class=sub style=margin:0>Most-flagged first. Hover a chip for the evidence. Util is coloured (red &lt;50%, amber 50–65%, green &gt;65%).</div>';
  h+='<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Type</th><th>Output</th><th>Pace<span class=s> /act-hr</span></th><th>Active h</th><th>Floor h</th><th>Util</th><th>Days</th><th>Avg/day</th><th style=text-align:left>Flags</th></tr>';
  main.forEach(p=>{
    h+='<tr><td class=name>'+p.person+'</td><td><span class="badge '+(p.type==='Intern'?'in':'ft')+'">'+(p.type==='Intern'?'Intern':(p.type?'FT':'—'))+'</span></td>';
    h+='<td>'+fmt(p.output)+'</td><td>'+fmt(p.pace)+'</td><td>'+p.active_hr+'</td><td>'+p.floor_hr+'</td><td class='+utilCls(p.util)+'>'+p.util+'%</td><td>'+p.days+'</td><td>'+p.avg_span+'h</td>';
    h+='<td style=text-align:left>'+(p.flags.length?p.flags.map(x=>'<span class="wchip '+x.sev+'" title="'+esc(x.d)+'">'+x.t+'</span>').join(''):'<span class=wok>✓ clear</span>')+'</td></tr>';
  });
  h+='</table></div></div>';
  if(eng.length){h+='<div class=card style=margin-top:16px><h2>Engravers <span style="color:#94a3b8;font-weight:400">— separate; engraving time not fully tracked, not flagged yet</span></h2>';
    h+='<div class=tablewrap><table><tr><th style=text-align:left>Person</th><th>Output</th><th>Pace</th><th>Active h</th><th>Floor h</th><th>Util</th><th>Days</th></tr>';
    eng.forEach(p=>{h+='<tr><td class=name>'+p.person+'</td><td>'+fmt(p.output)+'</td><td>'+fmt(p.pace)+'</td><td>'+p.active_hr+'</td><td>'+p.floor_hr+'</td><td>'+p.util+'%</td><td>'+p.days+'</td></tr>';});
    h+='</table></div></div>';}
  if(insuff.length)h+='<div class=note style=margin-top:14px><b>Not enough data to assess:</b> '+insuff.map(p=>p.person+' <span class=ins>('+p.days+' day'+(p.days===1?'':'s')+')</span>').join(', ')+'</div>';
  document.getElementById('watch_body').innerHTML=h;
}
document.getElementById('from').value=etAgo(7);document.getElementById('to').value=etToday();
load();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
