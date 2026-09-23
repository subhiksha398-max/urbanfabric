# UrbanFabric — backend

A small Python/Flask server that gives the UrbanFabric prototype real persistence and
a server-side pipeline run, without needing any external database to be installed.

## Run it

```bash
cd backend
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open **http://localhost:3000** — that's the same prototype UI, now served by (and
talking to) this backend instead of running purely in the browser.

## What's actually wired up

- **State persistence** — parcels, the audit log, and source records are stored in
  `data/db.json` (auto-created from `data/seed.json` on first run). Every approve /
  reject / investigate, review routing, and data upload in the UI is saved here, so it
  survives a page reload and is shared across anyone hitting this server.
- **Server-side pipeline run** — `POST /api/pipeline/:id/run` computes the harmonization
  numbers (area diff, building change, owner/address/area similarity, spatial overlap,
  confidence) from the stored parcel record. The "Run harmonization demo" button on the
  Harmonization pipeline page calls this and animates the returned numbers, instead of
  computing them in the browser.
- **Review decisions** — `POST /api/review-queue/:id/decision` (`approve` / `reject` /
  `investigate`) is available as a standalone endpoint for other integrations; the UI
  itself currently persists decisions via the bulk `/api/state` sync (see below).

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | Full snapshot: `{ parcels, auditLog, sources }` |
| PUT | `/api/state` | Overwrite and persist the snapshot (called by the UI after every change) |
| GET | `/api/parcels` | List parcels |
| GET | `/api/parcels/:id` | One parcel |
| GET | `/api/sources` | Source system registry |
| GET | `/api/audit` | Audit log |
| GET | `/api/review-queue` | Parcels with `status: "review"` |
| POST | `/api/pipeline/:id/run` | Run the harmonization/GeoAI scoring for one parcel |
| POST | `/api/review-queue/:id/decision` | Body: `{ action: "approve"\|"reject"\|"investigate", actor }` |

## If you open `index.html` directly instead

The frontend still works completely standalone if you just open `public/index.html` in
a browser (no `python app.py` needed) — it detects there's no server (`location.protocol ===
'file:'`) and falls back to the original in-browser demo data and computation. You'll
see "Offline demo mode" in the sidebar instead of "Backend connected."

## Honest scope note

This is a real, running backend — not a mock. But it's a single Python process with a
JSON file as its datastore, sized for a local prototype/demo, not a production
deployment. For what a production build needs (PostGIS, a message queue between
pipeline stages, a GPU-backed GeoAI service for change detection, RBAC/SSO, etc.), see
`../backend-architecture.md`.
