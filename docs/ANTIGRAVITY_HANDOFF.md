# Antigravity Handoff — RiverSongAI Side of River Vector Integration

**Read this file end-to-end, then read `docs/RIVER_VECTOR_INTEGRATION_SPEC.md` end-to-end before writing any code.**

You are implementing the **River Song side** of the River Vector mower fleet integration. The device side (River Vector) is already complete and committed to `github.com/cassu123/river-vector`. Your work is on `github.com/cassu123/RiverSongAI` only.

---

## Identity & Environment

- **Repo:** `git@github-riversongai:cassu123/RiverSongAI.git`
- **Production server:** `riversong@192.168.1.221` (Tailscale: `100.72.215.100`)
- **Repo path on production:** `~/RiverSongAI`
- **Service:** `river-song.service` (`sudo systemctl restart river-song`)
- **DB:** `/mnt/data/river-song/db/river_song.db` (SQLite)
- **Chroma:** `/mnt/data/river-song/chroma`
- **Python:** 3.14 (Ubuntu 26.04 LTS)
- **Public URL:** `https://riversongai.com` (Cloudflare Tunnel → localhost:8000)
- **Local clone exists** at `/home/hoke/RiverSongAI/` on the dev machine — this is a **reference clone**, may not match production. **Do all work against the production repo.**

**You will work on the production server directly.** Never edit the local clone except for read reference.

---

## Source of Truth

Before any decision, consult **`docs/RIVER_VECTOR_INTEGRATION_SPEC.md`** in this repo. It is the canonical specification covering:

- §1 Bootstrap / §2 Device lifecycle / §3 Connectivity
- §4 First-time setup flow (Google Home model)
- §5 Data models — full DDL for all `vector_*` tables
- §6 API contract — every endpoint, every payload
- §7 Long-poll control channel — implementation detail
- §8 Telemetry cadence
- §9 Safety floor enforcement (three layers)
- §10 Commands (program / manual / teach)
- §11 Sessions
- §12 Schedules (with daemon design)
- §13 River Vector code changes — **already complete, do not modify**
- §14 **River Song implementation — your scope**
- §15 Frontend specifications (per-page)
- §16 Permissions matrix
- §17 Failure modes & recovery
- §18 Observability
- §19 Scaling
- §20 Out of scope
- Appendix A — universal constants
- Appendix B — example payloads
- Appendix C — implementation order

The spec lives in BOTH repos. If they disagree, the version in the RiverSongAI repo is authoritative (commit the spec there first if it isn't already).

---

## Pre-flight (do this before any code change)

1. SSH into the production server: `ssh riversong@192.168.1.221`.
2. `cd ~/RiverSongAI && git pull` — confirm you are on `main` and up to date.
3. Confirm spec is present: `ls docs/RIVER_VECTOR_INTEGRATION_SPEC.md`. If missing, copy it from `github.com/cassu123/river-vector/docs/RIVER_VECTOR_INTEGRATION_SPEC.md` and commit it to RiverSongAI as the same path before proceeding.
4. Run the existing tests once to capture a green baseline: `python -m pytest`.
5. Restart the service to confirm it's currently healthy: `journalctl -u river-song -n 50`.

---

## Scope of Work — §14 only

You implement exactly these:

### §14.1 — Database schema

Add all tables from spec §5 to `providers/memory/sqlite_store.py`:

- `vector_units` (extend existing partial table if present)
- `vector_config_revisions`
- `vector_zones`
- `vector_programs`
- `vector_schedules`
- `vector_sessions`
- `vector_commands`
- `vector_telemetry`
- `vector_alerts` (extend existing partial table if present)
- `vector_session_events`

All DDL in spec §5. All indexes in spec §5. Migrations must be **idempotent** — they will run against an existing DB that already has partial `vector_units` and `vector_alerts` tables from earlier work. Use `CREATE TABLE IF NOT EXISTS` and `ALTER TABLE ... ADD COLUMN` (catch `OperationalError` on the latter for already-applied columns).

Add CRUD methods for each table following the existing patterns in `sqlite_store.py`.

### §14.2 — Route file

**Replace** the existing partial `api/routes/vector_fleet.py` with the full implementation per spec §6.

Endpoints required (full list, see §6.1, §6.2, §6.3):

**Device-facing (X-Unit-Token auth):**
- `POST /api/vector/register`
- `GET /api/vector/config/{unit_id}` (response schema in spec §6.4)
- `GET /api/vector/command/stream/{unit_id}` (**LONG-POLL** — see spec §7)
- `POST /api/vector/command/{command_id}/ack`
- `POST /api/vector/command/{command_id}/complete`
- `POST /api/vector/status`
- `POST /api/vector/telemetry` (batch up to 50 snapshots)
- `POST /api/vector/alert`
- `POST /api/vector/event`
- `POST /api/vector/session/start` → returns `session_id`
- `POST /api/vector/session/end`
- `POST /api/vector/zones/teach`

**Discovery (no auth, LAN-only):**
- `GET /api/vector/units/discovered`

**UI-facing (JWT + role auth per spec §16):**
- Units CRUD + commands + telemetry + alerts + events + sessions + camera snapshot + SSE stream
- Zones CRUD
- Programs CRUD + run-now
- Schedules CRUD
- Sessions list + detail

**Long-poll implementation (spec §7):** server holds up to 30 seconds via `asyncio.Event` per `unit_id`. When a command is queued, the matching event fires and the held request returns immediately with the command body and `X-Config-Version` header. On 30s timeout, return `204 No Content`. The asyncio.Event must be created lazily and held in a module-level dict keyed by `unit_id`.

**SSE implementation (spec §6.4):** `GET /api/vector/units/{id}/stream` returns `text/event-stream` with events: `telemetry`, `status`, `alert`, `command_update`, `heartbeat` (every 15s). Use FastAPI `StreamingResponse` with an async generator. New telemetry/status/alert rows are pushed to subscribed connections via an asyncio pub/sub primitive (one queue per active connection, populated by the relevant POST handlers).

**Token validation middleware:** every device-facing endpoint validates `X-Unit-Token` against `vector_units.unit_token` for the URL's `unit_id`. 401 on mismatch.

**JWT + role gates:** existing `core/auth.py::decode_token`. Add a `require_role(*roles)` dependency. Apply per the matrix in spec §16.

### §14.3 — mDNS listener daemon

New module: `daemons/vector_discovery/listener.py`.

- Uses `zeroconf` to listen for `_rivervector._tcp.local.` broadcasts on the LAN.
- Maintains an in-memory map: `{unit_id → (last_seen_ts, ip_address, proto_version)}`.
- Entries expire after 60s without re-broadcast.
- Expose via `GET /api/vector/units/discovered` returning a list filtered to **un-claimed** units (units not present in `vector_units`).
- Register the daemon in the existing daemon registry (`daemons/registry.py`).

### §14.4 — Scheduler daemon

New module: `daemons/vector_scheduler/scheduler.py`. Spec §12.2.

- Wakes every 60s.
- Selects `vector_schedules` rows where `enabled=1 AND next_run <= now()`.
- For each, fires `mow_start` via the same code path as `POST /api/vector/programs/{id}/run` (issues a `vector_commands` row).
- Uses idempotency key `"schedule:{schedule_id}:{utc_minute}"` to dedupe.
- Advances `next_run` via `croniter`.
- Honors `missed_run_policy` (`skip` | `run_once_on_recovery`).
- Cron expressions are stored in UTC; the daemon evaluates in UTC.

Also include an hourly **telemetry pruner** in the same daemon: deletes `vector_telemetry` older than `VECTOR_TELEMETRY_RETENTION_DAYS` (default 90), downsamples rows 7+ days old to one per 5 minutes (group by unit_id and rounded timestamp; keep one row per group).

### §14.5 — Web Push integration

In the `POST /api/vector/alert` handler, when `level == 'critical'`, call `providers/push/sender.py` to push to all users with `operator` or `admin` role.

### §14.6 — Frontend

All new pages under `frontend/src/pages/fleet/`. Routes added to `frontend/src/App.jsx` and nav entries in `frontend/src/utils/constants.js`.

Required pages (spec §15):

1. `/fleet` — Overview (map + unit cards + discovered devices)
2. `/fleet/units/:id` — Unit detail with tabs: Live | History | Settings | Camera (conditional)
3. `/fleet/units/:id/setup` — Setup wizard (multi-step form per spec §4.2)
4. `/fleet/zones` — Zone manager with Leaflet polygon editor
5. `/fleet/programs` — Program builder
6. `/fleet/schedules` — Schedule manager with cron picker
7. `/fleet/sessions` — Session history (filterable table)

**Map tiles:** Use Esri World Imagery via Leaflet TileLayer (free, no API key required). Document the URL pattern: `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}`.

**Live updates:** subscribe to `GET /api/vector/units/{id}/stream` (or `/api/vector/units/stream` for fleet-wide) via the browser EventSource API. Update local state on each event. No more 3-second polling.

**Design language:** Use the existing theme tokens — Universe × Environment × Mood system from `frontend/src/styles/themes.css`. Do not introduce new color variables; consume `--bg-card`, `--accent-primary`, `--text-primary`, etc.

**Responsive:** 768px mobile, 1024px tablet breakpoints. Fleet overview reflows to single-column on mobile.

**E-stop button:** Distinct red, requires a 2-second hold-to-confirm in non-emergency states. In FAULT or AUTO, single-click fires.

### §14.7 — Permission gating

Implement the matrix in spec §16 using a `require_role(*roles)` FastAPI dependency. Roles come from the existing JWT payload's `role` claim.

### §14.8 — Tests

- Backend: smoke tests for every endpoint (200 happy path + 401 unauthenticated + 403 wrong role).
- Backend: long-poll test using a synthetic command (post a command, expect the held GET to return within 1s).
- Backend: token validation test (correct token = 200, wrong token = 401, missing = 401).
- Frontend: component tests for the setup wizard (each step's validation), the zone map editor (polygon close behavior), the program builder.

### §14.9 — Documentation

- Commit this spec at `docs/RIVER_VECTOR_INTEGRATION_SPEC.md`.
- Add an entry to `docs/INTEGRATIONS.md` under a new "Robotics / Fleet" section (or extend the existing "Telemetry / Robotics" section already present).
- Update `HANDOFF.md` with a short note that fleet management is now live.

---

## Constraints — non-negotiable

1. **Do not modify** anything in the `river-vector` repo. Your work is RiverSongAI-only.
2. **Long-poll for commands, not periodic polling.** This is the critical low-latency path.
3. **Token-authenticate every device endpoint.** No exceptions, even on LAN.
4. **Safety floor validation server-side AND device-side.** Server rejects programs that violate a unit's safety_floors. The device clamps to absolute floors regardless.
5. **`config_version` is a monotonic integer.** Not a timestamp. Bumped on every UPDATE affecting a unit's config (including changes to its assigned program or any zone the program references).
6. **mid-session config policy** (spec §9): safety floor tightenings apply immediately, everything else applies on next session start. The device handles this; the server just bumps the version.
7. **Idempotency:** all `vector_commands` inserts honor the `idempotency_key` unique constraint.
8. **Use existing patterns** — auth, daemons, frontend components — match the style already in the repo. Do not invent new patterns.
9. **Migrations idempotent** — assume the DB already has partial vector tables from prior work.

---

## Verification — done = these pass

Before declaring §14 complete:

1. All 30+ endpoints from spec §6 return correct status codes per `curl` checks.
2. Long-poll test: `POST /api/vector/units/{id}/command` with `mow_stop`, then `GET /api/vector/command/stream/{id}` returns the command in under 200ms with `X-Config-Version` header set.
3. mDNS test: start a fake broadcaster on the LAN; `GET /api/vector/units/discovered` returns it.
4. Schedule test: insert a row with `next_run = now()`; daemon fires within 60s, `vector_commands` row appears with idempotency key.
5. Frontend: claim flow works end-to-end against a simulated unit (run the river-vector code in sim mode on a separate machine on the LAN).
6. Setup wizard: create a unit, walk through all 8 steps, save. Verify `vector_units` row + `vector_config_revisions` row with revision=1.
7. Zone editor: draw a polygon, save. `vector_zones` row with correct boundary and `capture_method='drawn'`.
8. Program: create one with `obstacle_clearance_m` below the assigned unit's safety floor. Server returns 400.
9. Permission gate: a `child`-role JWT cannot start a program (403).
10. All existing RiverSongAI tests still pass.

---

## When you're done

1. `git status` — confirm only intended files changed.
2. `git diff` — final review.
3. Commit in logical chunks (DB schema, routes, daemons, frontend pages — separate commits).
4. Push to `origin/main`.
5. On the production server: `cd ~/RiverSongAI && git pull && ./deploy.sh` (or restart the service).
6. Tail logs: `journalctl -u river-song -f` and confirm no startup errors.
7. Hit `https://riversongai.com/fleet` and confirm the page loads.
8. Report completion with the list of commits and any deviations from the spec, plus the verification checklist results.

If any spec ambiguity blocks progress: **do not guess. Stop, write up the ambiguity precisely, and pause.** The spec is authoritative; if it lacks a needed decision, it gets fixed first.
