# ShipHero Tote Complete → Engraving Logger (Totes tab)

Receives ShipHero's **Tote Complete** webhook and writes `tote_barcode → orders → engraving SKUs`
into a **Totes** tab of your Engraving Logger Google Sheet (via the same Apps Script web app the
logger uses). This is what makes the tote barcodes engravers scan resolve to real ShipHero orders
and DOTW/LID/IPE counts.

## Status: deployed and waiting
It goes live now, but **Tote Complete won't fire until ShipHero support enables the account flag**
(Breton's email). Registering the webhook (below) also needs that flag on + a ShipHero API token.

## Endpoints
- `GET /` — health (`verify`, `has_secret`, `has_webapp`)
- `POST /webhook` — the Tote Complete receiver (also answers `HEAD` with 200 for registration)

## Env
- `GSHEET_WEBAPP_URL` — the same `/exec` Apps Script URL as the logger
- `SHIPHERO_WEBHOOK_SECRET` — the `shared_signature_secret` returned by `webhook_create` (set after registering)
- `VERIFY_SIGNATURE` — `true` (default). Requests without a valid signature get 401.

## Register the webhook (after the flag is on)
```bash
export SHIPHERO_REFRESH_TOKEN=...        # ShipHero → Settings → API
# webhook_create mutation, name "Tote Complete", url https://<this-host>/webhook
```
`webhook_create` returns `shared_signature_secret` → set it as `SHIPHERO_WEBHOOK_SECRET` and redeploy.

## Notes
- One batch POST per webhook (fast, stays under ShipHero's 10s timeout).
- The Apps Script routes `{kind:"tote_batch", rows:[...]}` to the **Totes** tab; the logger's own
  events still go to the first tab.
