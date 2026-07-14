"""
Read API + dashboard for the Ikigai contribution store.
Serves the "Warehouse Picking & Packing" dashboard (Dashboard / Floor Time / Analytics)
plus JSON endpoints. Reads pre-aggregated + raw event data; live on every open.
"""
import os, json
from flask import Flask, request, jsonify, Response
from psycopg.rows import tuple_row
from db import connect

app = Flask(__name__)

# Full-timer / Intern designation (sourced from the HR employee database:
# warehouse_operations team = FT, interns team = Intern). Keyed by the name as it
# appears in ShipHero pick/pack data. Update by re-pulling list_employees.
PERSON_TYPE = {
    # warehouse_operations (full-time)
    "Nic Cox":"FT","Halil Gurler":"FT","Kadil Ladson":"FT","Manu Bekele":"FT",
    "Maurice Williams":"FT","Jeffrey Kwan":"FT","Shambria Green":"FT","Breton Rice":"FT",
    # interns
    "Esra Altug":"Intern","Simay Guner":"Intern","Cindy Lin":"Intern",
    "Brennen Myrick":"Intern","Lara Nielsen":"Intern","Patrick Robin":"Intern",
    # seen in data but not on the active roster (likely departed) — shown, unbadged
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
        WITH e AS (SELECT person,stage,subtype,source,order_number,quantity,ts
                   FROM event WHERE et_day(ts) BETWEEN %s AND %s)
        SELECT person,
          COALESCE(sum(quantity) FILTER (WHERE stage='pick'),0)                                  pk_items,
          count(DISTINCT order_number) FILTER (WHERE stage='pick')                                pk_orders,
          COALESCE(sum(quantity) FILTER (WHERE stage='pack' AND source='shiphero'),0)             packsh_items,
          count(DISTINCT order_number) FILTER (WHERE stage='pack' AND source='shiphero')          packsh_orders,
          COALESCE(sum(quantity) FILTER (WHERE stage='pack' AND source='shopify'),0)              packshop_items,
          count(DISTINCT order_number) FILTER (WHERE stage='pack' AND source='shopify')           packshop_orders,
          COALESCE(sum(quantity) FILTER (WHERE stage='replenish' AND subtype='physical'),0)       repl_units,
          COALESCE(sum(quantity) FILTER (WHERE stage='engrave'),0)                                 eng_items,
          count(*) FILTER (WHERE stage='pick')                                pick_cnt,
          count(*) FILTER (WHERE stage='pack' AND source='shiphero')          pack_cnt,
          count(*) FILTER (WHERE stage='pack' AND source='shopify')           fulfill_cnt,
          count(*) FILTER (WHERE stage='replenish' AND subtype='physical')    move_cnt,
          count(*) FILTER (WHERE stage='count')                              count_cnt,
          count(*) FILTER (WHERE stage='engrave')                            eng_cnt,
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
    tot = dict(pk_i=0,packsh_i=0,packshop_i=0,pk_o=0,packsh_o=0,packshop_o=0,repl=0,eng=0)
    for r in rows:
        (person,pk_i,pk_o,psh_i,psh_o,psp_i,psp_o,repl,eng_i,pick_c,pack_c,ful_c,mov_c,cnt_c,eng_c,first,last)=r
        people.append(dict(person=person, type=PERSON_TYPE.get(person,""),
            items_picked_sh=pk_i, items_packed_sh=psh_i, items_packed_shop=psp_i,
            replenished=repl, engraved=eng_c, orders_picked_sh=pk_o, orders_packed_sh=psh_o, orders_packed_shop=psp_o))
        tot["pk_i"]+=pk_i; tot["packsh_i"]+=psh_i; tot["packshop_i"]+=psp_i
        tot["pk_o"]+=pk_o; tot["packsh_o"]+=psh_o; tot["packshop_o"]+=psp_o; tot["repl"]+=repl; tot["eng"]+=eng_c
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

@app.route("/")
def dashboard():
    return Response(DASHBOARD_HTML, mimetype="text/html")

DASHBOARD_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Warehouse Picking &amp; Packing</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.5.0/dist/chart.umd.js"></script>
<style>
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#f4f6fa;color:#111827;font-size:14px}
.wrap{max-width:1400px;margin:0 auto;padding:24px 28px 80px}
h1{margin:0;font-size:26px;font-weight:700}
.sub{color:#6b7280;margin:4px 0 14px;font-size:14px}
.sub b{color:#374151}
.tabs{display:flex;gap:26px;border-bottom:1px solid #e5e7eb;margin-bottom:18px}
.tab{padding:10px 2px;font-weight:600;color:#6b7280;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}
.tab.on{color:#2563eb;border-bottom-color:#2563eb}
.ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.ctl .lbl{font-size:11px;color:#9ca3af;text-transform:uppercase;letter-spacing:.05em;margin-right:2px}
.seg{display:inline-flex;background:#111827;border-radius:9px;padding:3px}
.seg button{border:0;background:transparent;color:#d1d5db;padding:6px 14px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer}
.seg button.on{background:#fff;color:#111827}
.seg.gray{background:#e5e7eb}.seg.gray button{color:#6b7280}.seg.gray button.on{background:#374151;color:#fff}
.pill{border:1px solid #d1d5db;background:#fff;border-radius:8px;padding:7px 13px;font-size:13px;font-weight:600;color:#374151;cursor:pointer}
.pill.on{background:#2563eb;border-color:#2563eb;color:#fff}
input[type=date]{border:1px solid #d1d5db;border-radius:8px;padding:6px 8px;font-size:13px}
.spacer{flex:1}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:18px 20px}
.shipped{border-left:5px solid #16a34a;margin:16px 0;display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.shipped .big{font-size:44px;font-weight:800;color:#16a34a;line-height:1}
.shipped .t{font-size:18px;font-weight:600}
.shipped .d{color:#6b7280;font-size:14px;flex-basis:100%;margin-top:6px}
.shipped .d b{color:#16a34a}.shipped .d .o{color:#c2620c}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:8px}
.stat .k{font-size:13px;font-weight:600;color:#374151}
.stat .k .s{font-weight:500;font-size:12px;margin-left:4px}
.stat .v{font-size:32px;font-weight:800;margin-top:6px}
.s-sh{color:#2563eb}.s-shop{color:#c2620c}.s-repl{color:#7c3aed}.s-sel{color:#9ca3af}
.note{color:#6b7280;font-size:13px;margin:14px 0}
h2{font-size:16px;margin:0 0 4px}
.chartwrap{height:340px;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{padding:9px 10px;border-bottom:1px solid #f0f2f6;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:#6b7280;font-weight:600;font-size:11px;cursor:pointer}
th .s{color:#9ca3af;font-weight:500}
tr:hover td{background:#fafbfe}
td.name{font-weight:600;color:#111827}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
.badge.ft{background:#e0edff;color:#2563eb}.badge.in{background:#ede9fe;color:#7c3aed}
.o{color:#c2620c}.p{color:#7c3aed;font-weight:600}.eng{color:#0d9488;font-weight:600}.s-eng{color:#0d9488}
tr.tot td{font-weight:700;border-top:2px solid #e5e7eb}
.red{color:#dc2626;font-weight:600}
.lunch{background:#fef3c7;color:#92400e;border-radius:20px;padding:1px 7px;font-size:11px;margin-left:6px}
.foot{color:#9ca3af;font-size:12px;margin-top:22px;line-height:1.6}
.foot b{color:#6b7280}
#status{color:#6b7280;font-size:13px;margin-left:8px}
.hide{display:none}
</style></head><body><div class=wrap>
<h1>Warehouse Picking &amp; Packing</h1>
<div class=sub>Live contribution from ShipHero <b>+ direct-in-Shopify fulfillments + engraving</b>, PTO-aware. Toggle <b>Combined / Items / Orders</b> to cut the clutter.</div>
<div class=tabs>
  <div class="tab on" data-tab=dash onclick="tab('dash')">Dashboard</div>
  <div class=tab data-tab=floor onclick="tab('floor')">Floor Time</div>
  <div class=tab data-tab=an onclick="tab('an')">Analytics</div>
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
  <span class=lbl style=margin-left:14px>Stage</span><span class=seg id=stage><button class=on data-v=all>All</button><button data-v=pick>Picked</button><button data-v=pack>Packed</button><button data-v=repl>Replenished</button></span>
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
  <div class=note>Showing <b>activity</b> for the selected Unit / Stage / Source. &ldquo;Total&rdquo; sums the shown stages &amp; sources &mdash; an order counts in each stage it touched, so it is workload, <b>not</b> the deduped Orders shipped up top.</div>
  <div class=card style=margin-top:8px>
    <h2>Contribution by person</h2>
    <div class=sub style=margin:0>Items: Picked&middot;ShipHero + Packed&middot;ShipHero + Packed&middot;Shopify + Replenished&middot;ShipHero + Engraving scans (chart) &middot; click a bar for that person.</div>
    <div class=chartwrap><canvas id=chart></canvas></div>
  </div>
  <div class=card style=margin-top:16px>
    <h2>Per-person detail</h2>
    <div class=sub style=margin:0>Columns follow the Unit / Stage / Source toggles above. Click a header to sort.</div>
    <div id=detail></div>
  </div>
</div>

<div id=floor class=hide>
  <div class=card>
    <h2>Floor Time <span style="color:#9ca3af;font-weight:400">&mdash; when each person was actually active, and their gaps</span></h2>
    <div class=sub style=margin:0><b>Team-wide.</b> All ShipHero activity &mdash; picking, packing, restocking, receiving, cycle counts &mdash; plus engraving scans, attributed by user, in Eastern time.</div>
    <div id=floortable></div>
  </div>
</div>

<div id=an class=hide>
  <div class=card><h2>Analytics</h2><div id=analytics></div></div>
</div>

<div class=foot>
  <b>Chart colours:</b> <span class=s-sh>Picked&middot;ShipHero</span>, <span style=color:#16a34a>Packed&middot;ShipHero</span>, <span class=s-shop>Packed&middot;Shopify</span>, <span class=s-repl>Replenished&middot;ShipHero</span>, <span class=s-eng>Engraved</span>. &ldquo;Packed&middot;Shopify&rdquo; = fulfilled by hand in Shopify. &ldquo;Replenished&rdquo; = inventory work; &ldquo;Engraved&rdquo; = engraving-station cart scans. Both are parallel tracks, never added into the items/orders totals.
</div>
</div>
<script>
const C={pick:'#2563eb',pack:'#16a34a',fulfill:'#f59e0b',repl:'#7c3aed',engrave:'#0d9488'};
let DATA=null, sortKey='items_total', sortDir=-1, chart=null;
function etToday(){return new Date(Date.now()-4*3600*1000).toISOString().slice(0,10);}
function etAgo(n){return new Date(Date.now()-4*3600*1000-n*86400000).toISOString().slice(0,10);}
function segval(id){return document.querySelector('#'+id+' button.on').dataset.v;}
function seg(id,v){document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('on',b.dataset.v===v));render();}
document.querySelectorAll('.seg').forEach(s=>s.addEventListener('click',e=>{if(e.target.dataset.v){seg(s.id,e.target.dataset.v);}}));
function tab(t){['dash','floor','an'].forEach(x=>{document.getElementById(x).classList.toggle('hide',x!==t);});
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('on',el.dataset.tab===t));}
function preset(p){document.querySelectorAll('.pill[data-preset]').forEach(b=>b.classList.toggle('on',b.dataset.preset===p));
  let f=etToday(),t=etToday();
  if(p==='yest'){f=t=etAgo(1);}else if(p==='7'){f=etAgo(6);}else if(p==='30'){f=etAgo(29);}
  else if(p==='week'){const d=new Date(Date.now()-4*3600*1000);f=etAgo((d.getUTCDay()+6)%7);}
  document.getElementById('from').value=f;document.getElementById('to').value=t;load();}
async function getj(u){for(let i=0;i<8;i++){try{const r=await fetch(u);if(r.ok)return await r.json();}catch(e){}
  document.getElementById('status').textContent='waking server ('+(i+1)+')…';await new Promise(s=>setTimeout(s,4000));}throw 0;}
async function load(){document.getElementById('status').textContent='loading…';
  try{DATA=await getj('/warehouse?from='+document.getElementById('from').value+'&to='+document.getElementById('to').value);
  document.getElementById('status').textContent='';render();}catch(e){document.getElementById('status').textContent='could not reach API';}}
function fmt(n){return (n||0).toLocaleString();}
function fmtmin(m){if(m==null)return '—';const h=Math.floor(m/60),x=Math.round(m%60);return h?h+'h '+x+'m':x+'m';}
function ampm(iso){if(!iso)return '';return new Date(iso).toLocaleTimeString([], {hour:'numeric',minute:'2-digit',timeZone:'America/New_York'})+' ET';}
function teamFilter(p){const t=segval('team');return t==='all'||p.type===t;}
function render(){if(!DATA)return;
  const unit=segval('unit'),stage=segval('stage'),src=segval('source');
  const ppl=DATA.people.filter(teamFilter);
  // ---- summary + shipped ----
  const sh=DATA.shipped;
  document.getElementById('summary').innerHTML=ppl.length+' people &middot; <b>'+fmt(sh.total)+'</b> orders shipped &middot; '+
    fmt(DATA.totals.pk_i+DATA.totals.packsh_i+DATA.totals.packshop_i)+' items of activity ('+fmt(DATA.totals.pk_i)+' picked + '+fmt(DATA.totals.packsh_i)+' packed + '+fmt(DATA.totals.packshop_i)+' direct) &middot; '+DATA.range.from;
  document.getElementById('shipped').innerHTML='<div class=big>'+fmt(sh.total)+'</div><div class=t>orders shipped out the door</div>'+
    '<div class=d><b>'+fmt(sh.shiphero)+'</b> ShipHero &middot; <span class=o>'+fmt(sh.shopify_only)+'</span> Shopify-only ('+fmt(sh.both)+' ShipHero orders were finished by hand in Shopify)</div>';
  // ---- stat cards ----
  const T=DATA.totals;
  const showItems=unit!=='orders', showOrders=unit!=='items';
  function itemsSel(){let s=0; if(stage==='all'||stage==='pick'){if(src!=='shopify')s+=T.pk_i;} if(stage==='all'||stage==='pack'){if(src!=='shopify')s+=T.packsh_i; if(src!=='shiphero')s+=T.packshop_i;} return s;}
  function ordersSel(){let s=0; if(stage==='all'||stage==='pick'){if(src!=='shopify')s+=T.pk_o;} if(stage==='all'||stage==='pack'){if(src!=='shiphero')s+=T.packsh_o; if(src!=='shopify'){}if(src!=='shiphero')s+=T.packshop_o;} return s;}
  const si=document.getElementById('statsItems'), so=document.getElementById('statsOrders');
  si.className=so.className='cards';
  si.innerHTML=!showItems?'':[
    card('Items picked','ShipHero','s-sh',T.pk_i),
    card('Items packed','ShipHero','s-sh',T.packsh_i),
    card('Items packed','Shopify','s-shop',T.packshop_i),
    card('Items — total','selected','s-sel',itemsSel())].join('');
  so.innerHTML=!showOrders?'':[
    card('Orders picked','ShipHero','s-sh',T.pk_o),
    card('Orders packed','ShipHero','s-sh',T.packsh_o),
    card('Orders packed','Shopify','s-shop',T.packshop_o),
    card('Orders — total','selected','s-sel',ordersSel())].join('');
  si.classList.toggle('hide',!showItems);so.classList.toggle('hide',!showOrders);
  // ---- chart ----
  drawChart(ppl);
  // ---- detail table ----
  drawDetail(ppl,unit);
  // ---- floor ----
  drawFloor();
  drawAnalytics(ppl);
}
function card(k,s,cls,v){return '<div class="card stat"><div class=k>'+k+' <span class="s '+cls+'">'+s+'</span></div><div class=v>'+fmt(v)+'</div></div>';}
function drawChart(ppl){const labels=ppl.map(p=>p.person);
  const ds=[
    {label:'Picked · ShipHero',backgroundColor:C.pick,data:ppl.map(p=>p.items_picked_sh)},
    {label:'Packed · ShipHero',backgroundColor:C.pack,data:ppl.map(p=>p.items_packed_sh)},
    {label:'Packed · Shopify',backgroundColor:C.fulfill,data:ppl.map(p=>p.items_packed_shop)},
    {label:'Replenished · ShipHero',backgroundColor:C.repl,data:ppl.map(p=>p.replenished)},
    {label:'Engraving scans',backgroundColor:C.engrave,data:ppl.map(p=>p.engraved)}];
  if(chart)chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'bar',data:{labels,datasets:ds},
    options:{responsive:true,maintainAspectRatio:false,scales:{x:{stacked:true,grid:{display:false}},y:{stacked:true,beginAtZero:true}},
    plugins:{legend:{position:'bottom'}}}});}
function drawDetail(ppl,unit){
  const showI=unit!=='orders',showO=unit!=='items';
  let h='<table><tr><th onclick="sortBy(\'person\')">Person</th><th>Type</th><th>PTO</th>';
  if(showI)h+='<th onclick="sortBy(\'items_picked_sh\')">Items picked <span class=s>ShipHero</span></th><th onclick="sortBy(\'items_packed_sh\')">Items packed <span class=s>ShipHero</span></th><th onclick="sortBy(\'items_packed_shop\')">Items packed <span class="s o">Shopify</span></th><th onclick="sortBy(\'items_total\')">Items total</th>';
  h+='<th onclick="sortBy(\'replenished\')">Replenished <span class="s p">units</span></th>';
  h+='<th onclick="sortBy(\'engraved\')">Engraving <span class="s eng">scans</span></th>';
  if(showO)h+='<th onclick="sortBy(\'orders_picked_sh\')">Orders picked <span class=s>ShipHero</span></th><th onclick="sortBy(\'orders_packed_sh\')">Orders packed <span class=s>ShipHero</span></th><th onclick="sortBy(\'orders_packed_shop\')">Orders packed <span class="s o">Shopify</span></th>';
  h+='</tr>';
  const arr=ppl.map(p=>({...p,items_total:p.items_picked_sh+p.items_packed_sh+p.items_packed_shop}));
  arr.sort((a,b)=>((a[sortKey]>b[sortKey]?1:-1)*sortDir));
  const T={pk_i:0,psh:0,psp:0,it:0,rp:0,en:0,po:0,pso:0,pspo:0};
  arr.forEach(p=>{T.pk_i+=p.items_picked_sh;T.psh+=p.items_packed_sh;T.psp+=p.items_packed_shop;T.it+=p.items_total;T.rp+=p.replenished;T.en+=p.engraved;T.po+=p.orders_picked_sh;T.pso+=p.orders_packed_sh;T.pspo+=p.orders_packed_shop;
    h+='<tr><td class=name>'+p.person+'</td><td><span class="badge '+(p.type==='Intern'?'in':'ft')+'">'+(p.type==='Intern'?'Intern':(p.type?'Full-timer':'—'))+'</span></td><td>—</td>';
    if(showI)h+='<td>'+fmt(p.items_picked_sh)+'</td><td>'+fmt(p.items_packed_sh)+'</td><td class=o>'+fmt(p.items_packed_shop)+'</td><td><b>'+fmt(p.items_total)+'</b></td>';
    h+='<td class=p>'+fmt(p.replenished)+'</td><td class=eng>'+fmt(p.engraved)+'</td>';
    if(showO)h+='<td>'+fmt(p.orders_picked_sh)+'</td><td>'+fmt(p.orders_packed_sh)+'</td><td class=o>'+fmt(p.orders_packed_shop)+'</td>';
    h+='</tr>';});
  h+='<tr class=tot><td>Total</td><td></td><td></td>';
  if(showI)h+='<td>'+fmt(T.pk_i)+'</td><td>'+fmt(T.psh)+'</td><td class=o>'+fmt(T.psp)+'</td><td>'+fmt(T.it)+'</td>';
  h+='<td class=p>'+fmt(T.rp)+'</td><td class=eng>'+fmt(T.en)+'</td>';
  if(showO)h+='<td>'+fmt(T.po)+'</td><td>'+fmt(T.pso)+'</td><td class=o>'+fmt(T.pspo)+'</td>';
  h+='</tr></table>';
  document.getElementById('detail').innerHTML=h;}
function sortBy(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=-1;}render();}
function drawFloor(){const f=[...DATA.floor].filter(x=>DATA.people.find(p=>p.person===x.person&&teamFilter(p)));
  f.sort((a,b)=>b.gap_min-a.gap_min);
  let h='<table><tr><th>Person</th><th>First (ET)</th><th>Last (ET)</th><th>On floor</th><th>Biggest gap (ET)</th><th style=text-align:left>Activity mix</th></tr>';
  f.forEach(r=>{const span=r.first_ts&&r.last_ts?(new Date(r.last_ts)-new Date(r.first_ts))/60000:0;
    const lunch=r.gap_min>=25&&r.gap_min<=90?'<span class=lunch>lunch?</span>':'';
    const mix=[r.mix.pick?'pick '+r.mix.pick:'',r.mix.pack?'pack '+r.mix.pack:'',r.mix.move?'move '+r.mix.move:'',r.mix.fulfill?'fulfill '+r.mix.fulfill:'',r.mix.count?'count '+r.mix.count:'',r.mix.engrave?'engrave '+r.mix.engrave:''].filter(Boolean).join(' · ');
    h+='<tr><td class=name>'+r.person+'</td><td>'+ampm(r.first_ts)+'</td><td>'+ampm(r.last_ts)+'</td><td>~'+fmtmin(Math.round(span))+'</td>'+
      '<td style=text-align:left><span class=red>'+fmtmin(r.gap_min)+'</span> <span style=color:#9ca3af>'+ampm(r.gap_from)+'–'+ampm(r.gap_to)+'</span>'+lunch+'</td><td style=text-align:left>'+mix+'</td></tr>';});
  h+='</table>';document.getElementById('floortable').innerHTML=h;}
function drawAnalytics(ppl){const arr=ppl.map(p=>p.items_picked_sh+p.items_packed_sh+p.items_packed_shop).sort((a,b)=>a-b);
  const sum=arr.reduce((a,b)=>a+b,0),mean=arr.length?Math.round(sum/arr.length):0,med=arr.length?arr[Math.floor(arr.length/2)]:0;
  document.getElementById('analytics').innerHTML='<div class=cards>'+card('People','active','s-sel',ppl.length)+card('Mean items','per person','s-sel',mean)+card('Median items','per person','s-sel',med)+card('Total items','activity','s-sel',sum)+'</div>';}
function copyChat(){const rows=DATA.people.filter(teamFilter).map(p=>p.person+': picked '+p.items_picked_sh+', packed '+(p.items_packed_sh+p.items_packed_shop)+', replenished '+p.replenished+', engraving '+p.engraved);
  navigator.clipboard.writeText('Warehouse '+DATA.range.from+'\n'+DATA.shipped.total+' orders shipped\n'+rows.join('\n'));document.getElementById('status').textContent='copied!';setTimeout(()=>document.getElementById('status').textContent='',1500);}
function dl(kind){const ppl=DATA.people.filter(teamFilter);let blob,name;
  if(kind==='json'){blob=new Blob([JSON.stringify(DATA,null,2)],{type:'application/json'});name='warehouse.json';}
  else{const hdr=['person','type','items_picked_sh','items_packed_sh','items_packed_shopify','replenished','engraving_scans','orders_picked_sh','orders_packed_sh','orders_packed_shopify'];
    const lines=[hdr.join(',')].concat(ppl.map(p=>[p.person,p.type,p.items_picked_sh,p.items_packed_sh,p.items_packed_shop,p.replenished,p.engraved,p.orders_picked_sh,p.orders_packed_sh,p.orders_packed_shop].join(',')));
    blob=new Blob([lines.join('\n')],{type:'text/csv'});name='warehouse.csv';}
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();}
document.getElementById('from').value=etAgo(7);document.getElementById('to').value=etToday();
load();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
