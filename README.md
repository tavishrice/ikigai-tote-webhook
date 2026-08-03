# Ikigai Warehouse

Warehouse fulfillment & contribution analytics for **Ikigai Cases** — how much each person
picked, packed, engraved and restocked, how long they were on the floor, and how the team is
trending. A live dashboard plus the ingest/resolve jobs that feed it.

> Originally `ikigai-tote-webhook`, this repo began as the ShipHero "Tote Complete" webhook
> listener (below) and has since grown into the full warehouse dashboard — now the main thing here.

## What's in the repo

Two Flask services share this repo (deployed as separate Render services), plus the jobs that
populate the database:

| Component | File | Role |
|-----------|------|------|
| **Dashboard + read API** | `read_api.py` (`read_api:app`) | The **Ikigai Warehouse** UI and its JSON endpoints. Serves `/` (the dashboard) and read endpoints like `/warehouse`, `/floor`, `/speed`, `/trend`, `/engraving`, `/outstanding`, `/teamdaily`, `/hours`, `/dataqc`. Host: `ikigai-contribution-api.onrender.com`. |
| **Tote Complete webhook** | `app.py` (`app:app`) | Receives ShipHero's Tote Complete webhook and writes `tote_barcode → orders → engraving SKUs` into the Engraving Logger sheet. Deployed by `render.yaml`. See [Webhook](#tote-complete-webhook) below. |
| Ingest / sync | `shiphero_ingest.py`, `shopify_ingest.py`, `orders_snapshot.py`, `tracking_sync.py`, `hr_sync.py` | Pull raw fulfillment, order, tracking and roster data into the DB. |
| Resolve / normalize | `identity_resolve.py`, `known_aliases.py`, `engrave_resolve.py`, `fulfill_resolve.py`, `magnano_fix.py` | Map raw scan identities to real people and normalize engraving / fulfillment events. |
| Rollup | `rollup_cron.py` | Daily rollup into the per-person/day contribution tables the dashboard reads. |
| DB helper | `db.py` | Postgres connection. |
| MCP server | `mcp_server.py` | Exposes the warehouse data to assistants over MCP. |

## Dashboard

`read_api.py` renders a single-page dashboard (light "office" theme + a dark TV/kiosk mode) with
tabs: **Dashboard** (per-person contribution table + team chart), **Outstanding**, **Floor Time**,
**Planner**, **Trends**, **Speed & Rankings**, **Engraving**, **Analytics**, and **Data Issues**.
Data is read live on every open from pre-aggregated rollup tables plus raw event data.

Contribution is counted as: **pick + pack + engrave = one fulfillment figure**; **restock** is a
separate, parallel track. Active hours use a 45-minute-break rule (a scan gap ≥ 45 min is a break,
not work). Engravers scan **tote barcodes**, which is where the "tote" vocabulary throughout the
code comes from — those are real warehouse units, not leftover naming.

## Tote Complete webhook

Receives ShipHero's **Tote Complete** webhook and writes `tote_barcode → orders → engraving SKUs`
into a **Totes** tab of the Engraving Logger Google Sheet (via the same Apps Script web app the
logger uses). This is what makes the tote barcodes engravers scan resolve to real ShipHero orders
and DOTW/LID/IPE counts.

### Endpoints (`app:app`)
- `GET /` — health (`verify`, `has_secret`, `has_webapp`, `db`)
- `POST /webhook` — the Tote Complete receiver (also answers `HEAD` with 200 for registration)

### Env
- `GSHEET_WEBAPP_URL` — the same `/exec` Apps Script URL as the logger
- `SHIPHERO_WEBHOOK_SECRET` — the `shared_signature_secret` returned by `webhook_create` (set after registering)
- `VERIFY_SIGNATURE` — `true` (default). Requests without a valid signature get 401.

### Register the webhook
```bash
export SHIPHERO_REFRESH_TOKEN=...        # ShipHero → Settings → API
# webhook_create mutation, name "Tote Complete", url https://<this-host>/webhook
```
`webhook_create` returns `shared_signature_secret` → set it as `SHIPHERO_WEBHOOK_SECRET` and redeploy.

Notes:
- One batch POST per webhook (fast, stays under ShipHero's 10s timeout).
- The Apps Script routes `{kind:"tote_batch", rows:[...]}` to the **Totes** tab; the logger's own
  events still go to the first tab.

## Deploy

- **Webhook service** — `render.yaml` in this repo (`gunicorn app:app`).
- **Dashboard service** — runs `read_api:app` (Render service on the `ikigai-contribution-api` host).
- Shared env: a Postgres `DATABASE_URL` (see `db.py`); the ingest/rollup jobs run on a schedule.
