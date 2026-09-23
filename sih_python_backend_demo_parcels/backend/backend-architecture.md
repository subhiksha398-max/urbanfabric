# UrbanFabric — Backend Architecture & Requirements
### From prototype to production: what it takes to run the pipeline for real

The current build is a fully client-side prototype — all parcels, conflicts, and audit entries are JavaScript objects generated in the browser and lost on refresh. This document specs the real backend needed to run the pipeline shown on the Harmonization page: ingest → validate → georeference → AI/GeoAI match → conflict/anomaly detection → human review → master record → audit trail.

---

## 1. System architecture

A pipeline this shape — many source systems, a multi-stage async processing chain, and a human-in-the-loop step — fits an **event-driven microservices** architecture better than one monolith. Each pipeline stage becomes a service that reads from a queue, does its job, writes results, and emits an event for the next stage.

```
Source systems ─▶ Ingestion API ─▶ Message queue ─▶ ┌ Validation service
                                                       ├ CRS/Georeferencing service
                                                       ├ Schema mapping service
                                                       ├ GeoAI matching service (spatial/attribute/change)
                                                       ├ Conflict & anomaly detection service
                                                       └ Confidence scoring service
                                                             │
                                              ┌──────────────┴──────────────┐
                                       Verified path                 Review queue service ─▶ Officer decision API
                                              └──────────────┬──────────────┘
                                                     Master record service ─▶ Audit log service
```

**Recommended stack**

| Layer | Choice | Why |
|---|---|---|
| API gateway | Node.js/NestJS or Python/FastAPI | Typed contracts, good GIS library support (FastAPI + GeoAlchemy2 pairs well) |
| Pipeline services | Python (FastAPI/Celery workers) | Best ecosystem for GIS + ML (GDAL, Shapely, GeoPandas, rasterio, scikit-learn) |
| Message queue | Apache Kafka (or AWS SQS/EventBridge for a simpler start) | Durable event log between stages; replayable if a stage needs to rerun |
| Primary database | PostgreSQL + PostGIS | Industry standard for parcel geometry, spatial joins, topology checks |
| File/raster storage | S3-compatible object storage (drone orthophotos, DSM/DTM tiles, scanned ORI/GT documents) | Large binary geospatial assets don't belong in the relational DB |
| Search/audit index | OpenSearch/Elasticsearch | Fast full-text + filtered search over the audit trail and case history |
| Cache | Redis | Session state, rate limiting, hot parcel lookups |
| GeoAI/ML serving | Python service behind an internal API (PyTorch/TensorFlow + GDAL) | Isolates heavy compute (change detection on satellite/drone imagery) from the request path |
| Frontend | Keep the current SPA, point it at the real API instead of in-memory arrays | Minimizes rework of what's already built |

---

## 2. Service breakdown (mapped to the pipeline diagram)

| Pipeline stage | Service | Core responsibility |
|---|---|---|
| Data upload | **Ingestion API** | Accepts uploads/feeds from Cadastral, Revenue, Municipal sources (Drone, GNSS, Utility, ORI, GT, DSM/DTM); virus-scans, stores raw file, emits `source.received` event |
| Data validation | **Validation service** | Schema/type checks, required-field checks, file integrity (checksum), geometry validity (no self-intersections) |
| CRS check | **CRS service** | Detects source CRS, flags mismatches |
| Geo-referencing/transform | **Reprojection service** | Transforms every source into one working CRS (e.g., EPSG:4326 or a local UTM zone) using GDAL/PROJ |
| Common data schema | **Schema mapping service** | Maps source fields → master schema (the "Schema mapping" table already in the prototype) |
| AI/GeoAI processing | **GeoAI service(s)** | Three sub-jobs, can scale independently: spatial matching (geometry/centroid comparison), attribute mapping (name/address/area similarity scoring), feature/change detection (multi-temporal imagery diff for building footprints) |
| Conflict detection | **Conflict service** | Applies tolerance rules across sources, raises `conflict.detected` events |
| Anomaly detection | **Anomaly service** | Flags features with no matching regulatory record (e.g., construction with no permit) |
| Confidence score | **Scoring service** | Weighted aggregation into one confidence number per parcel |
| Review queue / Human officer | **Review service** | Case assignment, role-based visibility (matches the six roles already in Governance), approve/reject/investigate actions |
| Master record | **Master record service** | Versioned, source-attributed record of truth; every write creates a new version, never overwrites |
| Audit trail | **Audit service** | Append-only log of every automated and human action, timestamped and actor-attributed |

---

## 3. Data model (core tables, PostGIS)

- `sources` — source system registry (type, CRS, reliability score, connection config)
- `raw_uploads` — file metadata, storage path, checksum, ingestion timestamp, status
- `parcels` — canonical parcel table: `geom` (PostGIS geometry), survey_no, ward, area fields per source
- `parcel_versions` — every change to a parcel as an immutable version (append-only, foreign key to `parcels`)
- `matches` — spatial/attribute/temporal match results between source records and a parcel, with per-signal confidence scores
- `conflicts` — type (area/geometry/attribute), sources involved, tolerance exceeded, resolution status
- `anomalies` — type, detected-vs-expected evidence (e.g., building footprint delta vs permit record)
- `review_cases` — parcel_id, reason, assigned officer, status (pending/approved/rejected/investigating), SLA timer
- `audit_log` — actor, actor_role, action, target_id, before/after diff, timestamp (append-only, never updated or deleted)
- `users_roles` — the six roles already modeled in the prototype (Admin, Survey/GIS Officer, Revenue Officer, Municipal Officer, Utility Officer, Ground Survey Officer), with page/action-level permissions

---

## 4. API surface (representative endpoints)

```
POST   /api/v1/sources/{type}/upload          # ingestion
GET    /api/v1/parcels/{id}                    # current master record
GET    /api/v1/parcels/{id}/versions            # full version history
POST   /api/v1/pipeline/{parcelId}/run          # trigger harmonization run
GET    /api/v1/pipeline/{parcelId}/status        # poll stage-by-stage status
GET    /api/v1/conflicts?status=open
GET    /api/v1/anomalies?status=open
GET    /api/v1/review-queue?assignee=me
POST   /api/v1/review-queue/{caseId}/decision   # approve | reject | investigate
GET    /api/v1/audit?parcelId=&actor=&from=&to=
```
All write endpoints require an authenticated, role-checked session and emit an audit event — no silent writes.

---

## 5. AI/GeoAI processing requirements

- **Spatial matching**: geometry comparison (IoU/overlap, centroid distance) — Shapely/PostGIS `ST_Intersection`, `ST_Distance`.
- **Attribute mapping**: fuzzy string matching for owner name/address (e.g., Levenshtein/Jaro-Winkler or a small trained similarity model), numeric tolerance scoring for area.
- **Feature/change detection**: multi-temporal building footprint comparison from drone/satellite imagery — typically a CNN-based segmentation model (e.g., a U-Net variant) run on orthophoto tiles, diffed year-over-year.
- **Compute**: change detection is the only GPU-bound stage; the rest can run on CPU. Plan for a small GPU pool (or a managed inference endpoint) sized to imagery volume, not to overall request volume.
- **Model governance**: version every model, log which model version produced each confidence score (needed for auditability of "why was this flagged").

---

## 6. Non-functional requirements

| Area | Requirement |
|---|---|
| Auditability | Every automated decision and every human action is immutable and timestamped — no update/delete on `audit_log` |
| Data provenance | Every field in the master record traces back to its source system and upload event |
| Availability | Ingestion and review-queue APIs should target high availability (officers need the review queue during work hours); batch GeoAI jobs can tolerate brief downtime |
| Scalability | Pipeline stages scale independently — GeoAI/change-detection is the heaviest and should scale separately from the API layer |
| Security | Role-based access control matching the six existing roles; encryption at rest for PII (owner names, addresses) and in transit (TLS everywhere) |
| Compliance | Land records typically fall under data-localization / government data-handling rules — data residency and access-logging requirements should be confirmed with the relevant department before build |
| Idempotency | Re-running a pipeline stage on the same input must not create duplicate records — use upload/event IDs as idempotency keys |

---

## 7. Infrastructure requirements checklist

- [ ] PostgreSQL + PostGIS instance (managed, e.g., RDS/Cloud SQL with PostGIS extension)
- [ ] Object storage bucket(s) for raw imagery/scans, with lifecycle policy
- [ ] Message queue/broker (Kafka or managed equivalent)
- [ ] Container orchestration (Kubernetes or a managed container service) for independently scalable services
- [ ] GPU-capable compute for the change-detection model (can start as a managed inference endpoint to avoid owning GPU infra)
- [ ] Identity provider / SSO integration for government officer logins, mapped to the six roles
- [ ] Secrets management (source-system credentials, API keys)
- [ ] Observability: structured logging, metrics per pipeline stage, alerting on stuck/failed jobs
- [ ] CI/CD pipeline with staging environment fed by anonymized/sample data (never production PII in staging)
- [ ] Backup/restore and disaster-recovery plan for the master record and audit log specifically (these are the legal source of truth)

---

## 8. Suggested build sequence

1. **Data model + ingestion API** — stand up PostGIS schema, get real uploads flowing in, replacing the in-memory `parcels` array.
2. **Validation → CRS → schema mapping** — deterministic stages first; no ML needed yet, but they unblock everything downstream.
3. **Master record + audit log** — get the "record of truth" and immutable logging working early, since every later stage writes to them.
4. **Review queue + role-based actions** — wire the existing UI's approve/reject/investigate buttons to real endpoints.
5. **GeoAI matching + conflict/anomaly detection** — the highest-effort stage; can be built and validated against historical/sample data before going live.
6. **Confidence scoring + full pipeline wiring** — connect all stages end-to-end, replacing the prototype's simulated `runHarmonization()` timer sequence with real async job polling.

---

*This spec describes architecture and requirements only — no infrastructure has been provisioned. Treat compute/storage sizing as rough guidance to refine with actual data volumes (parcel count, imagery resolution/frequency, officer headcount) before procurement.*
