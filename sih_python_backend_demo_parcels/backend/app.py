"""UrbanFabric backend (Flask).

Python port of the original Node/Express server. Same routes, same JSON shapes,
same JSON-file datastore, so the existing frontend in public/index.html works unchanged.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:3000
"""
import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", 3000))
DB_FILE = BASE_DIR / "data" / "db.json"
SEED_FILE = BASE_DIR / "data" / "seed.json"
PUBLIC_DIR = BASE_DIR / "public"

# ---------- tiny JSON-file "database" ----------
# A real deployment should swap this for PostgreSQL + PostGIS (see backend-architecture.md).
_db_lock = threading.RLock()


def _read_seed():
    with open(SEED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_db(db):
    with _db_lock:
        tmp = DB_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        os.replace(tmp, DB_FILE)  # atomic swap so a crash can't leave a half-written file


def load_db():
    if not DB_FILE.exists():
        seed = _read_seed()
        save_db(seed)
        return seed
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"db.json was corrupt, reseeding from seed.json: {e}")
        seed = _read_seed()
        save_db(seed)
        return seed


db = load_db()
for key in ("parcels", "auditLog", "sources"):
    if not isinstance(db.get(key), list):
        db[key] = []

app = Flask(__name__, static_folder=None)


@app.after_request
def add_cors(resp):
    # Equivalent of Express `cors()` with defaults (allow any origin).
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


# ---------- helpers ----------
def now_time():
    return datetime.now().strftime("%H:%M")


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def js_round(x):
    """JavaScript Math.round (half rounds up), unlike Python's banker's rounding."""
    return math.floor(x + 0.5)


def clamp(lo, hi, v):
    return max(lo, min(hi, v))


def find_parcel(parcel_id):
    return next((p for p in db["parcels"] if p.get("id") == parcel_id), None)


def bldg(p, year):
    """buildingHistory[year] ?? 0"""
    v = (p.get("buildingHistory") or {}).get(year)
    return 0 if v is None else v


def not_found():
    return jsonify({"error": "parcel not found"}), 404


# ---------- health / whole-state sync ----------
@app.get("/api/state")
def get_state():
    return jsonify(db)


@app.put("/api/state")
def put_state():
    body = request.get_json(silent=True) or {}
    with _db_lock:
        if isinstance(body.get("parcels"), list):
            db["parcels"] = body["parcels"]
        if isinstance(body.get("auditLog"), list):
            db["auditLog"] = body["auditLog"][:500]  # cap growth
        if isinstance(body.get("sources"), list):
            db["sources"] = body["sources"]
        save_db(db)
    return jsonify({"ok": True, "savedAt": iso_now()})


# ---------- individual resources ----------
@app.get("/api/parcels")
def list_parcels():
    return jsonify(db["parcels"])


@app.get("/api/parcels/<parcel_id>")
def get_parcel(parcel_id):
    p = find_parcel(parcel_id)
    return jsonify(p) if p else not_found()


@app.get("/api/sources")
def list_sources():
    return jsonify(db["sources"])


@app.get("/api/audit")
def list_audit():
    return jsonify(db["auditLog"])


@app.get("/api/review-queue")
def review_queue():
    return jsonify([p for p in db["parcels"] if p.get("status") == "review"])


# ---------- server-side harmonization / GeoAI pipeline run ----------
@app.post("/api/pipeline/<parcel_id>/run")
def run_pipeline(parcel_id):
    p = find_parcel(parcel_id)
    if not p:
        return not_found()

    conf = p["matchConfidence"]
    area_diff = abs(p["area"]["cadastral"] - p["area"]["municipal"])
    bldg_delta = bldg(p, "2026") - bldg(p, "2025")
    b2025 = bldg(p, "2025")
    bldg_pct = js_round(bldg_delta / b2025 * 100) if b2025 else 0

    result = {
        "parcelId": p["id"],
        "areaDiff": area_diff,
        "bldgDelta": bldg_delta,
        "bldgPct": bldg_pct,
        "ownerSim": clamp(75, 99, conf - 6),
        "addressSim": clamp(72, 98, conf - 9),
        "areaSim": max(70, 100 - area_diff),
        "spatialOverlap": clamp(70, 99, conf + 3),
        "centroidMatch": clamp(70, 99, conf + 5),
        "confidence": conf,
        "computedAt": iso_now(),
    }

    with _db_lock:
        db["auditLog"].insert(0, {
            "time": now_time(),
            "actor": "AI",
            "text": f"Harmonization pipeline run on {p['id']}",
            "detail": f"Confidence {conf}% · area diff {area_diff} sq.m · "
                      f"building delta {bldg_delta} sq.m — computed server-side",
        })
        save_db(db)
    return jsonify(result)


# ---------- live pipeline diagram run ----------
@app.post("/api/pipeline/<parcel_id>/run-live")
def run_pipeline_live(parcel_id):
    p = find_parcel(parcel_id)
    if not p:
        return not_found()

    area_diff = abs(p["area"]["cadastral"] - p["area"]["municipal"])
    bldg_delta = bldg(p, "2026") - bldg(p, "2025")
    has_conflict = area_diff > 0
    has_anomaly = bool(p.get("anomaly"))
    confidence = p["matchConfidence"]
    uncertain = has_conflict or has_anomaly or confidence < 92

    stages = [
        {"key": "upload", "label": "Data upload", "status": "pass", "detail": f"Received {p['id']} from connected sources."},
        {"key": "validation", "label": "Data validation", "status": "pass", "detail": "Schema and required-field checks passed."},
        {"key": "crs", "label": "CRS check", "status": "pass", "detail": "Source CRS detected — reprojection required."},
        {"key": "georef", "label": "Geo-referencing / transform", "status": "pass", "detail": "Transformed to WGS84 / EPSG:4326."},
        {"key": "schema", "label": "Common data schema", "status": "pass", "detail": "Fields mapped to master schema."},
        {"key": "ai", "label": "AI / GeoAI processing", "status": "pass", "detail": "Spatial, attribute and change-detection models run."},
        {"key": "spatial", "label": "Spatial matching", "status": "pass", "detail": f"Spatial overlap {clamp(70, 99, confidence + 3)}%."},
        {"key": "attribute", "label": "Attribute mapping", "status": "pass", "detail": f"Owner similarity {clamp(75, 99, confidence - 6)}%."},
        {"key": "change", "label": "Feature / change detection",
         "status": "flag" if bldg_delta > 0 else "pass",
         "detail": f"Building footprint +{bldg_delta} sq.m detected." if bldg_delta > 0 else "No significant footprint change."},
        {"key": "conflict", "label": "Conflict detection",
         "status": "flag" if has_conflict else "pass",
         "detail": f"Area conflict: cadastral {p['area']['cadastral']} vs municipal {p['area']['municipal']} sq.m." if has_conflict else "No conflicts."},
        {"key": "anomaly", "label": "Anomaly detection",
         "status": "flag" if has_anomaly else "pass",
         "detail": f"{p.get('anomaly')} anomaly — expansion without matching permit." if has_anomaly else "No anomalies."},
        {"key": "confidence", "label": "Confidence score", "status": "pass", "detail": f"Overall confidence {confidence}%."},
        {"key": "route", "label": "Uncertain" if uncertain else "Verified",
         "status": "uncertain" if uncertain else "verified",
         "detail": "Routed to human review." if uncertain else "Auto-verified — no review needed."},
    ]

    with _db_lock:
        if uncertain:
            p["status"] = "review"
            stages.append({"key": "review", "label": "Review queue", "status": "pass", "detail": f"Case {p['id']} queued for officer review."})
            stages.append({"key": "officer", "label": "Human officer", "status": "pending", "detail": "Awaiting officer decision (approve/reject/investigate)."})
        else:
            p["status"] = "verified"
        stages.append({"key": "master", "label": "Master record", "status": "pass",
                       "detail": "Will update once officer decision is recorded." if uncertain else "Master record updated — new version created."})
        stages.append({"key": "audit", "label": "Audit trail", "status": "pass", "detail": "Run logged to audit trail."})

        if uncertain:
            reasons = " & ".join(r for r in (
                "area conflict" if has_conflict else None,
                f"{p.get('anomaly')} anomaly" if has_anomaly else None,
            ) if r)
            detail = f"Routed to review — {reasons}"
        else:
            detail = f"Auto-verified at {confidence}% confidence"

        db["auditLog"].insert(0, {
            "time": now_time(),
            "actor": "AI",
            "text": f"Live pipeline run on {p['id']}",
            "detail": detail,
        })
        save_db(db)

    return jsonify({"parcelId": p["id"], "uncertain": uncertain, "stages": stages, "parcel": p})


# ---------- review decisions ----------
@app.post("/api/review-queue/<parcel_id>/decision")
def review_decision(parcel_id):
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    actor = body.get("actor")
    p = find_parcel(parcel_id)
    if not p:
        return not_found()
    if action not in ("approve", "reject", "investigate"):
        return jsonify({"error": "action must be approve | reject | investigate"}), 400

    status_map = {"approve": "verified", "reject": "rejected", "investigate": "investigating"}
    with _db_lock:
        p["status"] = status_map[action]
        if action == "approve":
            versions = p.setdefault("versions", [])
            versions.append({
                "v": len(versions) + 1,
                "year": 2026,
                "area": p["area"]["cadastral"],
                "owner": p["owners"]["registration"],
                "building": bldg(p, "2026"),
            })
        details = {
            "approve": "New master record version created",
            "reject": "Reverted to prior verified state",
            "investigate": "Assigned to Reviewer role",
        }
        db["auditLog"].insert(0, {
            "time": now_time(),
            "actor": actor or "Officer",
            "text": f"{action[0].upper() + action[1:]}d harmonization for {p['id']}",
            "detail": details[action],
        })
        save_db(db)
    return jsonify(p)


# ---------- serve the frontend ----------
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def frontend(path):
    if path.startswith("api/"):
        return jsonify({"error": "not found"}), 404
    target = PUBLIC_DIR / path
    if path and target.is_file():
        return send_from_directory(PUBLIC_DIR, path)
    return send_from_directory(PUBLIC_DIR, "index.html")


if __name__ == "__main__":
    print(f"UrbanFabric backend running at http://localhost:{PORT}")
    print(f"Data persisted to {DB_FILE}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
