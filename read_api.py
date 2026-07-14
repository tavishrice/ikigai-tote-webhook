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
from flask import Flask, request, jsonify
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8090")))
