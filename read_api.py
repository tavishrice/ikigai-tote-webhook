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
import os, json
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
    tot = dict(pk_i=0,packsh_i=0,packshop_i=0,eng_i=0,pk_o=0,packsh_o=0,packshop_o=0,repl=0)
    for r in rows:
        (person,pk_i,pk_o,psh_i,psh_o,psp_i,psp_o,repl,eng_i,pick_c,pack_c,ful_c,mov_c,cnt_c,eng_c,first,last)=r
        people.append(dict(person=person, type=PERSON_TYPE.get(person,""),
            items_picked_sh=pk_i, items_packed_sh=psh_i, items_packed_shop=psp_i,
            engraved_items=eng_i, engraved_totes=eng_c, replenished=repl,
            orders_picked_sh=pk_o, orders_packed_sh=psh_o, orders_packed_shop=psp_o))
        tot["pk_i"]+=pk_i; tot["packsh_i"]+=psh_i; tot["packshop_i"]+=psp_i; tot["eng_i"]+=eng_i
        tot["pk_o"]+=pk_o; tot["packsh_o"]+=psh_o; tot["packshop_o"]+=psp_o; tot["repl"]+=repl
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
.cards{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:8px}
.stat .k{font-size:13px;font-weight:600;color:#374151}
.stat .k .s{font-weight:500;font-size:12px;margin-left:4px}
.stat .v{font-size:30px;font-weight:800;margin-top:6px}
.s-sh{color:#2563eb}.s-shop{color:#c2620c}.s-repl{color:#7c3aed}.s-sel{color:#111827}.s-eng{color:#0d9488}
.note{color:#6b7280;font-size:13px;margin:14px 0}
h2{font-size:16px;margin:0 0 4px}
.chartwrap{height:360px;margin-top:10px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
th,td{padding:9px 10px;border-bottom:1px solid #f0f2f6;text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:#6b7280;font-weight:600;font-size:11px;cursor:pointer}
th .s{color:#9ca3af;font-weight:500}
tr:hover td{background:#fafbfe}
td.name{font-weight:600;color:#111827}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
.badge.ft{background:#e0edff;color:#2563eb}.badge.in{background:#ede9fe;color:#7c3aed}
.o{color:#c2620c}.p{color:#7c3aed;font-weight:600}.eng{color:#0d9488;font-weight:600}
tr.tot td{font-weight:700;border-top:2px solid #e5e7eb}
.red{color:#dc2626;font-weight:600}
.lunch{background:#fef3c7;color:#92400e;border-radius:20px;padding:1px 7px;font-size:11px;margin-left:6px}
.foot{color:#9ca3af;font-size:12px;margin-top:22px;line-height:1.6}
.foot b{color:#6b7280}
#status{color:#6b7280;font-size:13px;margin-left:8px}
.hide{display:none}
</style></head><body><div class=wrap>
<h1>Warehouse Picking &amp; Packing</h1>
<div class=sub>Live contribution from ShipHero <b>+ direct-in-Shopify fulfillments + engraving</b>, PTO-aware. <b>Fulfillment</b> (pick + pack + engrave) and <b>Replenishment</b> are two separate tracks.</div>
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
  <div class=note>Cards + chart + table all follow the Unit / Stage / Source toggles. <b>Fulfillment items total = Picked + Packed + Engraved</b> for the selected filters. Replenishment is a separate track and is never added into that total.</div>
  <div class=card style=margin-top:8px>
    <h2>Contribution by person</h2>
    <div class=sub style=margin:0>Left bar = <b>Fulfillment</b> (Picked&middot;ShipHero + Packed&middot;ShipHero + Packed&middot;Shopify + Engraved) &mdash; its height is the Items total. Right bar = <b>Replenished</b> (separate track). Click a bar for that person.</div>
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
    <div class=sub style=margin:0><b>Team-wide.</b> All floor activity &mdash; picking, packing, replenishing, engraving &mdash; attributed by user, in Eastern time.</div>
    <div id=floortable></div>
  </div>
</div>

<div id=an class=hide>
  <div class=card><h2>Analytics</h2><div id=analytics></div></div>
</div>

<div class=foot>
  <b>Chart colours:</b> <span class=s-sh>Picked&middot;ShipHero</span>, <span style=color:#16a34a>Packed&middot;ShipHero</span>, <span class=s-shop>Packed&middot;Shopify</span>, <span class=s-eng>Engraved</span> &mdash; these four stack into the <b>Fulfillment</b> bar. <span class=s-repl>Replenished</span> is drawn as its own separate bar (a parallel track, never added into the fulfillment/items total).
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
// which fulfillment components are visible for the current Stage/Source toggles
function vis(){const stage=segval('stage'),src=segval('source');return{
  pick:  (stage==='all'||stage==='pick')    && src!=='shopify',
  packsh:(stage==='all'||stage==='pack')    && src!=='shopify',
  packshop:(stage==='all'||stage==='pack')  && src!=='shiphero',
  eng:   (stage==='all'||stage==='engrave') && src!=='shopify',
  repl:  (stage==='all'||stage==='repl')    && src!=='shopify'};}
// visible fulfillment ITEMS total for a person (pick+pack+engrave) — this is the fulfillment bar height
function fulItems(p,v){return (v.pick?p.items_picked_sh:0)+(v.packsh?p.items_packed_sh:0)+(v.packshop?p.items_packed_shop:0)+(v.eng?p.engraved_items:0);}
function fulOrders(p,v){return (v.pick?p.orders_picked_sh:0)+(v.packsh?p.orders_packed_sh:0)+(v.packshop?p.orders_packed_shop:0);}
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
  const ordersSel=(v.pick?T.pk_o:0)+(v.packsh?T.packsh_o:0)+(v.packshop?T.packshop_o:0);
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
  const arr=[...ppl].sort((a,b)=>fulItems(b,v)-fulItems(a,v));
  const labels=arr.map(p=>p.person);
  const ds=[
    {label:'Picked · ShipHero',stack:'ful',backgroundColor:C.pick,data:arr.map(p=>v.pick?p.items_picked_sh:0)},
    {label:'Packed · ShipHero',stack:'ful',backgroundColor:C.pack,data:arr.map(p=>v.packsh?p.items_packed_sh:0)},
    {label:'Packed · Shopify',stack:'ful',backgroundColor:C.fulfill,data:arr.map(p=>v.packshop?p.items_packed_shop:0)},
    {label:'Engraved',stack:'ful',backgroundColor:C.engrave,data:arr.map(p=>v.eng?p.engraved_items:0)},
    {label:'Replenished (separate track)',stack:'repl',backgroundColor:C.repl,data:arr.map(p=>v.repl?p.replenished:0)}];
  if(chart)chart.destroy();
  chart=new Chart(document.getElementById('chart'),{type:'bar',data:{labels,datasets:ds},
    options:{responsive:true,maintainAspectRatio:false,
      scales:{x:{stacked:true,grid:{display:false}},y:{stacked:true,beginAtZero:true}},
      plugins:{legend:{position:'bottom'},tooltip:{callbacks:{footer:(items)=>{
        let f=0;items.forEach(i=>{if(i.dataset.stack==='ful')f+=i.parsed.y;});return f?'Fulfillment items: '+f:'';}}}}}});
}
function drawDetail(ppl,unit,v){
  const showI=unit!=='orders',showO=unit!=='items';
  // build columns dynamically so the toggles actually filter the table
  const icols=[];
  if(showI){
    if(v.pick)   icols.push(['items_picked_sh','Items picked','<span class=s>ShipHero</span>','']);
    if(v.packsh) icols.push(['items_packed_sh','Items packed','<span class=s>ShipHero</span>','']);
    if(v.packshop)icols.push(['items_packed_shop','Items packed','<span class="s o">Shopify</span>','o']);
    if(v.eng)    icols.push(['engraved_items','Items engraved','<span class="s eng">logger</span>','eng']);
  }
  const ocols=[];
  if(showO){
    if(v.pick)   ocols.push(['orders_picked_sh','Orders picked','<span class=s>ShipHero</span>','']);
    if(v.packsh) ocols.push(['orders_packed_sh','Orders packed','<span class=s>ShipHero</span>','']);
    if(v.packshop)ocols.push(['orders_packed_shop','Orders packed','<span class="s o">Shopify</span>','o']);
  }
  const arr=ppl.map(p=>({...p,
    items_total:fulItems(p,v), orders_total:fulOrders(p,v)}));
  arr.sort((a,b)=>((a[sortKey]>b[sortKey]?1:-1)*sortDir));
  let h='<table><tr><th onclick="sortBy(\'person\')">Person</th><th>Type</th>';
  icols.forEach(c=>h+='<th onclick="sortBy(\''+c[0]+'\')">'+c[1]+' '+c[2]+'</th>');
  if(showI)h+='<th onclick="sortBy(\'items_total\')">Items total</th>';
  if(v.repl)h+='<th onclick="sortBy(\'replenished\')">Replenished <span class="s p">units·sep.</span></th>';
  ocols.forEach(c=>h+='<th onclick="sortBy(\''+c[0]+'\')">'+c[1]+' '+c[2]+'</th>');
  h+='</tr>';
  const Tt={};
  arr.forEach(p=>{
    h+='<tr><td class=name>'+p.person+'</td><td><span class="badge '+(p.type==='Intern'?'in':'ft')+'">'+(p.type==='Intern'?'Intern':(p.type?'Full-timer':'—'))+'</span></td>';
    icols.forEach(c=>{h+='<td class="'+c[3]+'">'+fmt(p[c[0]])+'</td>';Tt[c[0]]=(Tt[c[0]]||0)+p[c[0]];});
    if(showI){h+='<td><b>'+fmt(p.items_total)+'</b></td>';Tt.items_total=(Tt.items_total||0)+p.items_total;}
    if(v.repl){h+='<td class=p>'+fmt(p.replenished)+'</td>';Tt.replenished=(Tt.replenished||0)+p.replenished;}
    ocols.forEach(c=>{h+='<td class="'+c[3]+'">'+fmt(p[c[0]])+'</td>';Tt[c[0]]=(Tt[c[0]]||0)+p[c[0]];});
    h+='</tr>';});
  h+='<tr class=tot><td>Total</td><td></td>';
  icols.forEach(c=>h+='<td>'+fmt(Tt[c[0]]||0)+'</td>');
  if(showI)h+='<td>'+fmt(Tt.items_total||0)+'</td>';
  if(v.repl)h+='<td class=p>'+fmt(Tt.replenished||0)+'</td>';
  ocols.forEach(c=>h+='<td>'+fmt(Tt[c[0]]||0)+'</td>');
  h+='</tr></table>';
  document.getElementById('detail').innerHTML=h;
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
  h+='</table>';document.getElementById('floortable').innerHTML=h;}
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
document.getElementById('from').value=etAgo(7);document.getElementById('to').value=etToday();
load();
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
