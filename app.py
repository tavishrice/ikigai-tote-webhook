"""
ShipHero Tote Complete -> Postgres (tote_content).  DB-backed successor to the
Google-Sheets webhook. Verifies the HMAC signature, keeps only real engraving
rows (DOTW/LID/IPE), and writes them to Postgres with ON CONFLICT DO NOTHING so
duplicate webhook deliveries (same batch_id) collapse automatically.

Env:
  DATABASE_URL              Postgres (Render Internal Connection String)
  SHIPHERO_WEBHOOK_SECRET   shared_signature_secret from webhook_create
  VERIFY_SIGNATURE          "true" (default) — 401 unsigned requests
  ENGRAVING_ONLY            "true" (default) — store only DOTW/LID/IPE rows
"""
import base64, hashlib, hmac, json, os
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from db import connect

app = Flask(__name__)
SECRET  = os.environ.get("SHIPHERO_WEBHOOK_SECRET", "")
VERIFY  = os.environ.get("VERIFY_SIGNATURE", "true").lower() != "false"
ENG_ONLY = os.environ.get("ENGRAVING_ONLY", "true").lower() != "false"

INSERT = """
INSERT INTO tote_content
  (received_at, tote_barcode, tote_name, batch_id, order_number, sku, quantity, engraving_type)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (tote_barcode, order_number, sku, batch_id) DO NOTHING
"""

def eng_type(sku):
    s = (sku or "").upper()
    if s.startswith("ENG-OPS"):  return "OPS-CHECK"
    if s.startswith("ENG-DOTW"): return "DOTW"
    if s.startswith("ENG-LID"):  return "LID"
    if s.startswith("ENG-IPE"):  return "IPE"
    if s.startswith("ENG-"):     return "OTHER-ENG"
    return ""

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def sig_ok(raw):
    if not VERIFY:
        return True
    header = request.headers.get("x-shiphero-hmac-sha256", "")
    if not SECRET or not header:
        return False
    calc = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest())
    return hmac.compare_digest(calc, header.encode())

@app.route("/", methods=["GET"])
def health():
    db_ok = False
    try:
        with connect() as c, c.cursor() as cur:
            cur.execute("SELECT 1"); db_ok = cur.fetchone() is not None
    except Exception as e:
        db_ok = f"error: {e!r}"
    return jsonify(status="ok", verify=VERIFY, has_secret=bool(SECRET),
                   engraving_only=ENG_ONLY, db=db_ok)

@app.route("/webhook", methods=["POST", "HEAD"])
def webhook():
    if request.method == "HEAD":
        return "", 200
    raw = request.get_data()
    if not sig_ok(raw):
        return jsonify(code="401", Message="invalid signature"), 401
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return jsonify(code="400", Message="bad json"), 400

    if payload.get("webhook_type") == "Tote Complete":
        received = now_iso()
        batch = payload.get("batch_id", "")
        rows = []
        for tote in payload.get("totes", []):
            tname, tbc = tote.get("tote_name", ""), tote.get("tote_barcode", "")
            for order in tote.get("orders", []):
                onum = order.get("order_number", "")
                for it in order.get("items", []):
                    sku = it.get("sku", ""); et = eng_type(sku)
                    if ENG_ONLY and not (et and et != "OPS-CHECK"):
                        continue
                    rows.append((received, str(tbc), tname, batch, onum, sku,
                                 it.get("quantity", 0), et))
        if rows:
            try:
                with connect() as c, c.cursor() as cur:
                    cur.executemany(INSERT, rows)
                    c.commit()
                    print(f"[db] wrote {cur.rowcount} tote_content row(s)", flush=True)
            except Exception as e:
                print("[db] error:", repr(e), flush=True)
                return jsonify(code="500", Message="db error"), 500
    return jsonify(code="200", Message="Success"), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
