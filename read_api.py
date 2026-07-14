"""
Read API — the thin, fast layer the dashboards and docs call. It only ever reads
the pre-aggregated contribution_daily table, so responses are instant no matter
how big the raw event log gets.

Endpoints
  GET /health
  GET /contribution?from=YYYY-MM-DD&to=YYYY-MM-DD[&person=Name][&stage=engrave]
        -> per-person totals across the window (the dashboard's main call)
  GET /contribution/daily?from=&to=[&person=]
        -> one row per person per day (for trend charts)
  GET /people
        -> everyone who has any contribution on record

CORS is open GET-only so a Cowork artifact / static dashboard can fetch directly.
"""
import os
from flask import Flask, request, jsonify, Response
from db import connect

app = Flask(__name__)

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return resp

def _range():
    return request.args.get("from"), request.args.get("to")

@app.route("/health")
def health():
    try:
        with connect() as c, c.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM contribution_daily")
            return jsonify(status="ok", rollup_rows=cur.fetchone()["n"])
    except Exception as e:
        return jsonify(status="error", detail=repr(e)), 500

@app.route("/contribution")
def contribution():
    frm, to = _range()
    person, stage = request.args.get("person"), request.args.get("stage")
    where = ["et_day BETWEEN %s AND %s"]; params = [frm, to]
    if person: where.append("person = %s"); params.append(person)
    if stage:  where.append("stage = %s");  params.append(stage)
    sql = f"""
        SELECT person, stage,
               sum(scans) scans, sum(totes) totes, sum(matched_totes) matched_totes,
               sum(dotw) dotw, sum(lid) lid, sum(ipe) ipe, sum(eng_units) eng_units,
               sum(orders) orders, round(sum(hours),2) hours,
               CASE WHEN sum(hours)>0 THEN round(sum(totes)/sum(hours),1) END AS totes_per_hour
        FROM contribution_daily
        WHERE {' AND '.join(where)}
        GROUP BY person, stage
        ORDER BY eng_units DESC NULLS LAST, person
    """
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return jsonify(rows=cur.fetchall(), range={"from": frm, "to": to})

@app.route("/contribution/daily")
def contribution_daily():
    frm, to = _range()
    person = request.args.get("person")
    where = ["et_day BETWEEN %s AND %s"]; params = [frm, to]
    if person: where.append("person = %s"); params.append(person)
    with connect() as c, c.cursor() as cur:
        cur.execute(f"""SELECT * FROM contribution_daily
                        WHERE {' AND '.join(where)}
                        ORDER BY et_day, person""", params)
        return jsonify(rows=cur.fetchall())

@app.route("/people")
def people():
    with connect() as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT person FROM contribution_daily ORDER BY person")
        return jsonify(people=[r["person"] for r in cur.fetchall()])

@app.route("/orders-shipped")
def orders_shipped():
    """Deduped orders out the door, split ShipHero vs Shopify-only, over a window."""
    frm, to = _range()
    with connect() as c, c.cursor() as cur:
        cur.execute("""SELECT via, count(*) orders
                       FROM orders_shipped WHERE et_day BETWEEN %s AND %s
                       GROUP BY via""", [frm, to])
        rows = {r["via"]: r["orders"] for r in cur.fetchall()}
    total = sum(rows.values())
    return jsonify(total=total, shiphero=rows.get("shiphero", 0),
                   shopify_only=rows.get("shopify", 0), range={"from": frm, "to": to})

@app.route("/shift")
def shift():
    """Per-person shift: first/last, active vs break minutes, utilization, stages."""
    frm, to = _range()
    person = request.args.get("person")
    where = ["et_day BETWEEN %s AND %s"]; params = [frm, to]
    if person: where.append("person = %s"); params.append(person)
    with connect() as c, c.cursor() as cur:
        cur.execute(f"""SELECT et_day, person, first_ts, last_ts, span_min, active_min,
                               break_min, breaks, longest_break_min, lunch_flag,
                               utilization, actions, stages
                        FROM shift_daily WHERE {' AND '.join(where)}
                        ORDER BY et_day, person""", params)
        return jsonify(rows=cur.fetchall())

@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")

DASHBOARD_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Ikigai — Team Contribution</title>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#f4f6fa;color:#1f2733}
header{background:#1f3864;color:#fff;padding:18px 24px}
header h1{margin:0;font-size:20px;font-weight:600}
header .sub{opacity:.8;font-size:13px;margin-top:2px}
.wrap{max-width:1100px;margin:0 auto;padding:20px 24px 60px}
.controls{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:18px 0}
.controls label{display:block;font-size:11px;color:#6b7280;margin-bottom:4px;text-transform:uppercase;letter-spacing:.04em}
.controls input,.controls select{padding:8px 10px;border:1px solid #cbd5e1;border-radius:8px;font-size:14px;background:#fff}
button{padding:9px 16px;border:0;border-radius:8px;background:#2e5496;color:#fff;font-size:14px;font-weight:500;cursor:pointer}
button:hover{background:#24406f}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin:8px 0 24px}
.card{background:#fff;border:1px solid #e5e9f0;border-radius:12px;padding:16px 18px}
.card .k{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:30px;font-weight:600;color:#1f3864;margin-top:4px;line-height:1}
.card .d{font-size:12px;color:#6b7280;margin-top:6px}
h2{font-size:15px;color:#1f3864;margin:26px 0 10px;font-weight:600}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e9f0;border-radius:12px;overflow:hidden;font-size:13px}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #eef1f6}
th{background:#f0f3f9;color:#3a4a63;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:500;background:#e7edf7;color:#2e5496;margin-right:4px}
.bar{height:8px;border-radius:4px;background:#2e5496}
.muted{color:#94a3b8}
.util-hi{color:#166534;font-weight:600}.util-lo{color:#b45309}
#status{font-size:13px;color:#6b7280;padding:14px 0}
.foot{font-size:12px;color:#94a3b8;margin-top:30px}
</style></head><body>
<header><h1>Ikigai Cases — Team Contribution</h1>
<div class=sub>Live from the contribution database · engraving · pick · pack · counts · replenishment</div></header>
<div class=wrap>
 <div class=controls>
   <div><label>From</label><input type=date id=from></div>
   <div><label>To</label><input type=date id=to></div>
   <button onclick="load()">Refresh</button>
   <span id=status></span>
 </div>
 <div class=cards id=cards></div>
 <h2>Contribution by person &amp; activity</h2>
 <div id=contrib></div>
 <h2>Shifts &amp; utilization</h2>
 <div id=shift></div>
 <div class=foot id=foot></div>
</div>
<script>
const API="";
function etToday(){const d=new Date(Date.now()-4*3600*1000);return d.toISOString().slice(0,10);}
function etDaysAgo(n){const d=new Date(Date.now()-4*3600*1000-n*86400000);return d.toISOString().slice(0,10);}
async function getj(path){for(let i=0;i<6;i++){try{const r=await fetch(API+path);if(r.ok)return await r.json();}catch(e){}
  document.getElementById('status').textContent='Waking the server… ('+(i+1)+')';await new Promise(s=>setTimeout(s,4000));}
  throw new Error('unreachable');}
function card(k,v,d){return `<div class=card><div class=k>${k}</div><div class=v>${v}</div><div class=d>${d||''}</div></div>`;}
function fmtmin(m){if(m==null)return '—';const h=Math.floor(m/60),mm=Math.round(m%60);return h?`${h}h ${mm}m`:`${mm}m`;}
async function load(){
 const f=document.getElementById('from').value,t=document.getElementById('to').value;
 document.getElementById('status').textContent='Loading…';
 try{
  const [os_,con,shf]=await Promise.all([
    getj(`/orders-shipped?from=${f}&to=${t}`),
    getj(`/contribution?from=${f}&to=${t}`),
    getj(`/shift?from=${f}&to=${t}`)]);
  document.getElementById('status').textContent='';
  // ---- cards ----
  const rows=con.rows||[];
  const engUnits=rows.filter(r=>r.stage==='engrave').reduce((a,r)=>a+(+r.eng_units||0),0);
  const ppl=new Set(rows.map(r=>r.person)).size;
  document.getElementById('cards').innerHTML=
    card('Orders shipped',os_.total??0,`${os_.shiphero||0} ShipHero · ${os_.shopify_only||0} Shopify`)+
    card('Engraving units',engUnits,'DOTW + LID + IPE')+
    card('People active',ppl,'with logged activity')+
    card('Activities',rows.reduce((a,r)=>a+(+r.scans||0),0),'scans / picks / packs');
  // ---- contribution table (person x stage) ----
  const byp={};rows.forEach(r=>{(byp[r.person]=byp[r.person]||[]).push(r);});
  let h=`<table><tr><th>Person</th><th>Activities</th><th class=n>Items/units</th><th class=n>Orders</th><th>Stage mix</th></tr>`;
  Object.keys(byp).sort().forEach(p=>{
    const rs=byp[p];const items=rs.reduce((a,r)=>a+(+r.eng_units||0),0);const ords=rs.reduce((a,r)=>a+(+r.orders||0),0);
    const acts=rs.reduce((a,r)=>a+(+r.scans||0),0);
    const mix=rs.map(r=>`<span class=pill>${r.stage} ${r.eng_units||0}</span>`).join('');
    h+=`<tr><td><b>${p}</b></td><td class=n>${acts}</td><td class=n>${items}</td><td class=n>${ords}</td><td>${mix}</td></tr>`;});
  h+=`</table>`;document.getElementById('contrib').innerHTML=rows.length?h:'<div class=muted>No contribution rows in range.</div>';
  // ---- shift table ----
  const sr=shf.rows||[];
  let s=`<table><tr><th>Person</th><th>Window (ET)</th><th class=n>Active</th><th class=n>Break</th><th class=n>Breaks</th><th class=n>Util</th><th>Stages</th></tr>`;
  sr.forEach(r=>{
    const w=r.first_ts?new Date(r.first_ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',timeZone:'America/New_York'})+'–'+new Date(r.last_ts).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',timeZone:'America/New_York'}):'—';
    const u=r.utilization!=null?Math.round(r.utilization*100)+'%':'—';
    const uc=r.utilization>=.6?'util-hi':(r.utilization!=null?'util-lo':'');
    s+=`<tr><td><b>${r.person}</b></td><td>${w}</td><td class=n>${fmtmin(r.active_min)}</td><td class=n>${fmtmin(r.break_min)}</td><td class=n>${r.breaks}${r.lunch_flag?' 🍽':''}</td><td class="n ${uc}">${u}</td><td>${(r.stages||'').split(',').map(x=>`<span class=pill>${x}</span>`).join('')}</td></tr>`;});
  s+=`</table>`;document.getElementById('shift').innerHTML=sr.length?s:'<div class=muted>No shift rows in range.</div>';
  document.getElementById('foot').textContent='Range '+f+' → '+t+' · loaded '+new Date().toLocaleTimeString();
 }catch(e){document.getElementById('status').textContent='Could not reach the API — try Refresh.';}
}
document.getElementById('from').value=etDaysAgo(7);
document.getElementById('to').value=etToday();
load();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
