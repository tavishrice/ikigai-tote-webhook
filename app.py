"""
ShipHero Tote Complete -> Engraving Logger sheet (Totes tab).

Receives ShipHero's Tote Complete webhook, verifies the HMAC signature, flattens each
tote's orders+items into rows, and posts them (one batch call) to the same Google Apps
Script web app that backs your Engraving Logger sheet — routed to a "Totes" tab as:

    received_at, tote_barcode, tote_name, batch_id, order_number, sku, quantity,
    is_engraving, engraving_type

That gives the tote_barcode -> orders -> engraving SKUs mapping, so the barcodes engravers
scan at the station resolve to real ShipHero orders + DOTW/LID/IPE counts.

Ready-to-run: deploy to Render, then register the Tote Complete webhook once ShipHero support
enables the account flag (webhook_create returns the shared_signature_secret -> set it here).

Env: GSHEET_WEBAPP_URL (same /exec URL as the logger), SHIPHERO_WEBHOOK_SECRET,
     VERIFY_SIGNATURE (default true), PORT
"""
import base64, hashlib, hmac, json, os, urllib.request
from datetime import datetime, timezone
from flask import Flask, request, jsonify

app = Flask(__name__)
SECRET = os.environ.get("SHIPHERO_WEBHOOK_SECRET", "")
VERIFY = os.environ.get("VERIFY_SIGNATURE", "true").lower() != "false"
WEBAPP_URL = os.environ.get("GSHEET_WEBAPP_URL", "")

def eng_type(sku):
    s = (sku or "").upper()
    if s.startswith("ENG-OPS"):  return "OPS-CHECK"   # 'Double Check Engravings' — not output
    if s.startswith("ENG-DOTW"): return "DOTW"
    if s.startswith("ENG-LID"):  return "LID"
    if s.startswith("ENG-IPE"):  return "IPE"
    if s.startswith("ENG-"):     return "OTHER-ENG"
    return ""

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def post_rows(rows):
    if not WEBAPP_URL:
        print("[sink] no GSHEET_WEBAPP_URL set", flush=True); return
    body = json.dumps({"kind": "tote_batch", "rows": rows}).encode()
    req = urllib.request.Request(WEBAPP_URL, data=body, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        print(f"[sink] wrote {len(rows)} tote row(s)", flush=True)
    except Exception as e:
        print("[sink] error:", repr(e), flush=True)

def sig_ok(raw):
    if not VERIFY:
        return True
    header = request.headers.get("x-shiphero-hmac-sha256", "")
    if not SECRET or not header:
        return False
    calc = base64.b64encode(hmac.new(SECRET.encode("utf-8"), raw, hashlib.sha256).digest())
    return hmac.compare_digest(calc, header.encode("utf-8"))

@app.route("/", methods=["GET"])
def health():
    return jsonify(status="ok", verify=VERIFY, has_secret=bool(SECRET), has_webapp=bool(WEBAPP_URL))

@app.route("/webhook", methods=["POST", "HEAD"])
def webhook():
    if request.method == "HEAD":          # ShipHero validates reachability with a HEAD
        return "", 200
    raw = request.get_data()
    if not sig_ok(raw):
        return jsonify(code="401", Message="invalid signature"), 401
    try:
        payload = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return jsonify(code="400", Message="bad json"), 400

    if payload.get("webhook_type") == "Tote Complete":
        received = now_iso(); batch = payload.get("batch_id", ""); rows = []
        for tote in payload.get("totes", []):
            tname, tbc = tote.get("tote_name", ""), tote.get("tote_barcode", "")
            for order in tote.get("orders", []):
                onum = order.get("order_number", "")
                for it in order.get("items", []):
                    sku = it.get("sku", ""); et = eng_type(sku)
                    rows.append({"received_at": received, "tote_barcode": tbc, "tote_name": tname,
                                 "batch_id": batch, "order_number": onum, "sku": sku,
                                 "quantity": it.get("quantity", ""),
                                 "is_engraving": ("yes" if et and et != "OPS-CHECK" else "no"),
                                 "engraving_type": et})
        if rows:
            post_rows(rows)
    return jsonify(code="200", Message="Success"), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
