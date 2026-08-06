# UniTrack — Backend

API + workers for **UniTrack**: a digital ticketing + live bus-tracking platform for a university's own bus fleet.

This repo is the **hub** — every client talks only to this API; nothing else touches Postgres/Redis/Elasticsearch directly. It is also the **source of truth for the spec and the API contract**.

## Core constraint

No IoT hardware on buses. The **helper's smartphone is the only sensor** (GPS, QR scan, seat occupancy). Revenue arrives **only** via bKash. Every flow must survive that phone going offline mid-trip.

## Stack

| Piece | Tech |
|---|---|
| API | FastAPI (async) + Uvicorn, SQLAlchemy 2.0 async + asyncpg, Pydantic v2 |
| Realtime / cache / queue | Redis — latest-state cache, pub/sub fan-out, `gps_ingest` Stream |
| Relational DB | PostgreSQL 16 — identity, fleet, commerce, history |
| GPS store | **Elasticsearch 8** — all GPS fixes as `geo_point` docs (geo queries) |
| Workers | Python asyncio — GPS→ES indexer (more jobs later), same repo / separate process |
| Edge / deploy | Nginx (TLS, WS upgrade) · Docker Compose on a single 2–4 GB VPS |
| External (later) | bKash Checkout · Mapbox Directions · SMTP |

- **Redis = "now"** (latest position, ETAs, seats). **Postgres = "forever"** (identity, money). **Elasticsearch = GPS** (geo search over the fix history).
- All third-party calls are server-side — quotas/secrets never reach a client.

> **Note on Elasticsearch.** The spec (`docs/spec.md` §5.1) originally dropped ES from v1. It was later re-introduced **as the sole GPS store** on request, to get geo queries (nearby / viewport / heatmap) that Postgres can't do without PostGIS. Postgres no longer holds GPS at all — migration `b7f3c1a9d2e4` drops the old `gps_points` table. This is an intentional deviation from the written spec.

---

## Status at a glance

**Functional now** — built, and verified end-to-end against real Postgres +
Redis + Elasticsearch by [`scripts/smoke_test.py`](scripts/smoke_test.py) (36 checks):

| Area | State |
|---|---|
| Identity & JWT auth (access + refresh, argon2) | ✅ |
| Default-deny authorization, Redis-cached `Principal`, instant revocation | ✅ |
| Admin: helper approval queue, approve, suspend | ✅ |
| Fleet reference data: buses, routes, stops | ✅ |
| Trip lifecycle: start / end / active, one-live-per-bus/helper | ✅ |
| GPS pipeline: ingest → Redis → worker → Elasticsearch → `/track/nearby` | ✅ |
| Seat reports + latest-state cache | ✅ |
| Alerts / SOS + admin console (list / acknowledge / resolve) | ✅ |
| Token revocation, refresh rotation, `/auth/logout` | ✅ |
| Commerce: products, orders, SSLCommerz payment, ticket issuance | ✅ |

**Not built yet** — in the spec, not started (roadmap order):

| Area | Notes |
|---|---|
| Payment reconciler | Nightly diff of the gateway statement against `orders`. Until it exists, an order where neither the browser return nor the IPN ever arrives stays `pending` forever |
| Offline QR validation + fraud sweep | `tickets.qr_secret` is generated and stored; nothing reads it yet. Spec §7.2/§7.5 |
| Live-tracking WebSocket `/ws/track/{route_id}` | nginx already carries the upgrade headers |
| ETA engine | Free path (route-offset + rolling speed) if no Mapbox key |
| Materialized report tables + admin dashboards | Spec §10 |
| Admin CRUD for stops and routes | Buses have endpoints; stops/routes are seed-script only |
| Email/SMTP (verification is logged to stdout for now) | Later phase |

Sibling clients: **[unitrack-helper](https://github.com/mjobayerr/unitrack-helper)**
(Flutter, functional) · **[unitrack-web](https://github.com/mjobayerr/unitrack-web)**
(Next.js — admin console works: helper approval and the alerts queue. Student
PWA not started).

---

## What's done

### 1. Identity core (P1)
- `users` / `students` / `helpers` tables; roles `student` / `helper` / `admin`.
- **JWT** access (15 min) + refresh (30 day) — [`app/core/security.py`](app/core/security.py).
- **Server-side varsity-email gate**: student signup rejects non-allow-listed domains (403), not just in the UI — [`app/api/routes/auth.py`](app/api/routes/auth.py).
- Helpers register as `pending_approval`; helper-only endpoints are gated on `status='approved'` — [`app/api/deps.py`](app/api/deps.py) `get_current_helper`.
- Email verification link is logged to stdout (SMTP is a later phase).

### 2. Live GPS pipeline → Elasticsearch (P2, partial)
The end-to-end path **helper phone → API → Redis → worker → Elasticsearch** is wired:

1. Helper `POST /helper/gps` with a batch of fixes ([`app/api/routes/helper.py`](app/api/routes/helper.py)). The endpoint:
   - checks the bus exists,
   - writes the newest fix to Redis `bus:{id}:pos` (HASH, TTL 60 s) — the "latest position",
   - publishes to `fleet:ch` (admin live-map fan-out),
   - `XADD`s every fix to the `gps_ingest` Redis Stream.
2. The worker ([`app/worker/gps_es_indexer.py`](app/worker/gps_es_indexer.py)) reads the stream via consumer group `es_indexers` and **bulk-indexes** each fix into the ES `gps_points` index as a `geo_point` doc (the stream id is the ES doc id → reprocess-safe).
3. Read side: `GET /track/nearby?lat=&lng=&radius_km=` runs an ES `geo_distance` query, collapses to one hit per bus, closest first ([`app/api/routes/tracking.py`](app/api/routes/tracking.py)).

```
                                   ┌──────────► Redis bus:{id}:pos  (latest, TTL 60s)
helper POST /helper/gps ──► API ──┼──────────► Redis fleet:ch      (live-map pub/sub)
                                   └─ XADD ───► gps_ingest stream
                                                     │
                                     group es_indexers │  (worker)
                                                     ▼
                                            Elasticsearch gps_points  ◄── GET /track/nearby
```

### 3. Trip spine (P2)
`stops`, `routes`, `route_stops`, `trips` — spec §6's requirement that every GPS
point, redemption and seat report hangs off a trip.

- Helper-initiated lifecycle: `POST /helper/trips/start` → `live`, `/end` →
  `completed`, `/active` to recover after an app restart.
- **One live trip per bus and per helper, enforced by partial unique indexes**
  (`WHERE status = 'live'`), not by a check-then-insert that a double-tapped
  Start button would race through.
- The live trip is cached in Redis (`helper:{id}:trip`), so GPS ingest resolves
  its `trip_id` without touching Postgres, and the trip's bus overrides whatever
  bus the client claimed.
- `service_date` is the **local** day (`SERVICE_TIMEZONE`, default `Asia/Dhaka`).
  Deriving it from UTC would roll the day at 06:00 local and split a morning's
  trips across two dates.
- Fixes sent with no live trip are still accepted with a null `trip_id` — a
  transition allowance until the helper app ships trip UI.

`schedules` is not built; trips are ad-hoc. Recurring timetables add a
`schedule_id` later without changing anything above.

### 4. Ops / scaffold
- Docker Compose: `postgres`, `redis`, `elasticsearch`, `api`, `worker`, `nginx` — [`docker-compose.yml`](docker-compose.yml).
- Alembic migrations (identity core → fleet → drop gps_points) — [`alembic/versions/`](alembic/versions/).
- Dev seed scripts: initial admin + a bus/approved-helper for GPS testing — [`scripts/`](scripts/).

---

## Repo layout

```
app/
  main.py                 FastAPI app factory + /health
  core/
    config.py             pydantic-settings (env)
    security.py           JWT encode/decode, argon2 hashing
    redis.py              async client, stream + keyspace helpers
    elasticsearch.py      async client, gps_points geo_point mapping, ensure-index
  db/                     async engine + session, declarative Base
  models/                 user, fleet (Bus)  — no GPS model (ES-only)
  schemas/                auth, gps request/response (Pydantic)
  api/
    deps.py               auth guards (get_current_user/helper, require_role)
    routes/auth.py        register / verify / login / refresh / me
    routes/helper.py      POST /helper/gps  (ingest)
    routes/tracking.py    GET /track/nearby (ES geo query)
  worker/
    __main__.py           worker entrypoint (asyncio.gather of jobs)
    gps_es_indexer.py     gps_ingest stream → Elasticsearch
alembic/                  migrations
scripts/                  seed_admin, dev_seed_fleet
deploy/nginx.conf         edge config
docs/spec.md              full grep-able spec
```

---

## Run (dev)

```bash
cp .env.example .env            # then set a real JWT_SECRET

# Elasticsearch needs a high mmap limit or it won't boot:
sudo sysctl -w vm.max_map_count=262144      # persist in /etc/sysctl.conf for reboots

# All services (postgres, redis, elasticsearch, api, worker, nginx):
docker compose up -d --build
docker compose run --rm api alembic upgrade head
```

Local Python (uv) instead of the api/worker containers:

```bash
uv sync
docker compose up -d postgres redis elasticsearch
uv run alembic upgrade head
uv run uvicorn app.main:app --reload        # http://localhost:8000/docs
uv run python -m app.worker                 # GPS → ES indexer
```

### Smoke-test the GPS pipeline (no phone needed)

```bash
# 1. seed a bus (prints bus_id) + approve a helper
uv run python -m scripts.seed_admin                       # admin login
BUS_REG_NO=DHK-01 uv run python -m scripts.dev_seed_fleet # -> bus_id=<uuid>
# register + approve + login a helper to get an access token (see Auth table)

# 2. post a fix
curl -X POST localhost:8000/helper/gps -H "authorization: Bearer <token>" \
  -H 'content-type: application/json' \
  -d '{"bus_id":"<uuid>","points":[{"lat":23.78,"lng":90.40,"ts":"2026-07-14T10:00:00Z"}]}'

# 3. read it back out of Elasticsearch
curl "localhost:8000/track/nearby?lat=23.78&lng=90.40&radius_km=5"
```

---

## API (current)

| Method | Path | Notes |
|---|---|---|
| POST | `/auth/register/student` | Rejects non-varsity email domains **server-side** (403). |
| POST | `/auth/register/helper` | Creates a `pending_approval` account. |
| GET | `/auth/verify-email?token=` | Verify link logged to stdout (SMTP later). |
| POST | `/auth/login` | Access + refresh tokens; blocks non-active accounts. |
| POST | `/auth/refresh` | New token pair from a refresh token. |
| GET | `/auth/me` | Current user (Bearer access token). |
| GET | `/admin/helpers` | Approval queue; `?helper_status=pending` to filter. **admin** |
| POST | `/admin/helpers/{id}/approve` | Approve a helper so they can send GPS. **admin** |
| POST | `/admin/users/{id}/suspend` | Suspend an account; effective immediately. **admin** |
| GET | `/fleet/buses` | Bus picker for the helper app. |
| GET | `/fleet/routes` | Route list. |
| GET | `/fleet/routes/{id}` | One route with its ordered stops + polyline. |
| GET | `/fleet/stops` | All stops. |
| POST | `/helper/trips/start` | Begin a trip (bus + route). 409 if either is already live. |
| POST | `/helper/trips/end` | Close the caller's live trip. |
| GET | `/helper/trips/active` | Recover state after an app restart. |
| POST | `/helper/gps` | Ingest a batch of fixes (approved helper only). |
| POST | `/helper/seats` | Report occupancy for the live trip. |
| POST | `/helper/alerts` | Raise an alert (SOS / breakdown / …); severity set server-side. |
| GET | `/admin/alerts` | Open alerts, worst first. **admin** |
| POST | `/admin/alerts/{id}/acknowledge` | Claim an alert. **admin** |
| POST | `/admin/alerts/{id}/resolve` | Close an alert with a note. **admin** |
| GET | `/track/nearby` | Buses within `radius_km`, closest first (ES `geo_distance`). |
| GET | `/shop/products` | Ticket catalogue. |
| POST | `/shop/orders` | Start a purchase; returns the gateway checkout URL. Idempotent. **student** |
| GET | `/shop/orders` | The caller's own orders. **student** |
| POST | `/shop/payments/return` | Where the gateway sends the browser back. Public by necessity. |
| POST | `/shop/payments/ipn` | Where the gateway reports server-to-server. Public by necessity. |
| GET | `/shop/tickets` | The caller's wallet. **student** |

### Payments (SSLCommerz)

**Deviation from the spec.** §7.1 and §9 were written around bKash PGW
(Grant Token → Create Payment → Execute → Query). The integration is
**SSLCommerz**, an aggregator that offers bKash alongside cards and other
wallets, and its flow is different: session init returns a `GatewayPageURL`,
the student pays there, and the outcome is confirmed by validating a `val_id`
server-to-server. Order columns are therefore named `gateway_*` rather than
`bkash_*` — the provider has already changed once.

**Nothing the browser says is trusted.** The student returns via a redirect
carrying a `val_id`, but that is an ordinary HTTP request anyone can forge by
typing a URL. A ticket is issued only after a direct call to SSLCommerz
confirms three things: the status is `VALID`/`VALIDATED`, the settled amount
**and** currency equal the order's own, and the transaction is not risk-flagged.
Checking only the status would sell a 100 BDT ticket for 1 BDT.

Two reports of the same payment arrive — the browser return and the IPN — and
both run the same settlement path so they cannot disagree. Whichever lands
first settles the order; the other finds it `paid` and stops. A unique
`tickets.order_id` catches the race if they slip past that check.

Money is stored in **paisa as an integer**, never a float.

`SSLCOMMERZ_STORE_ID` / `SSLCOMMERZ_STORE_PASSWORD` come from the merchant
panel. For a sandbox store the API password is usually `<store_id>@ssl`, which
is **not** the password shown in the panel's store-detail view. `ipn_url` is
only registered when `PUBLIC_BASE_URL` is publicly resolvable, so on localhost
settlement is redirect-only.

Full contract: `GET /openapi.json` (clients generate types from it). All 20
endpoints are exercised end-to-end by [`scripts/smoke_test.py`](scripts/smoke_test.py).

### Auth

Every route is authenticated and authorized except the handful in
`PUBLIC_PATHS` (register / verify / login / refresh / health / docs) — enforced
by [`tests/test_auth_coverage.py`](tests/test_auth_coverage.py), which fails the
build on any unguarded route. Guards live on the router; the caller's role and
status are resolved into a Redis-cached `Principal` (~0.15 ms, no Postgres hit
on the hot path) and invalidated on every write to `users` / `helpers`, so
suspension takes effect on the next request.

**Read [`docs/auth.md`](docs/auth.md) before adding an endpoint.** Worked
example: [`app/api/routes/admin.py`](app/api/routes/admin.py).

## Config

Env vars: copy [`.env.example`](.env.example) to `.env` for local dev.
Postgres, Redis (`REDIS_PASSWORD` empty in dev), `ELASTICSEARCH_URL`, `GPS_INDEX`,
`JWT_SECRET`, token TTLs, `ALLOWED_STUDENT_EMAIL_DOMAINS`, `SERVICE_TIMEZONE`.
Real `.env` is gitignored — never commit secrets.

---

## Deploying to a VPS (behind Cloudflare)

The dev `docker-compose.yml` publishes Postgres/Redis/Elasticsearch on host
ports so the local `uv` workflow can reach them. **On a public VPS those ports
would expose the databases to the internet**, so production uses a separate
[`docker-compose.prod.yml`](docker-compose.prod.yml) that publishes only nginx.

Topology:

```
Flutter app ──HTTPS──► Cloudflare ──HTTPS──► VPS :443 nginx ──► api:8000
             api.kodewithmj.xyz   (Full Strict)   (internal Docker network:
                                                    postgres · redis · es · worker)
```

Steps on a fresh 4 GB+ VPS (Ubuntu):

```bash
# 1. Elasticsearch needs this or it crash-loops on boot
sudo sysctl -w vm.max_map_count=262144      # persist in /etc/sysctl.conf

# 2. firewall: only SSH + HTTPS reach the box
sudo ufw allow 22 && sudo ufw allow 443 && sudo ufw enable

# 3. secrets
cp .env.prod.example .env.prod              # then fill every CHANGE_ME
python -c "import secrets; print(secrets.token_urlsafe(48))"   # -> JWT_SECRET
openssl rand -base64 24                      # -> POSTGRES_PASSWORD, REDIS_PASSWORD

# 4. TLS: Cloudflare Origin Certificate (free, 15-year), saved as
#    deploy/certs/origin.pem and deploy/certs/origin.key

# 5. DNS: A record  api.kodewithmj.xyz -> VPS IP, proxied (orange cloud)
#    Cloudflare SSL/TLS mode: Full (Strict).  NOT Flexible.

# 6. up
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod run --rm api alembic upgrade head
```

Then point the app's release build at it:
`flutter build apk --release --dart-define=UNITRACK_BASE_URL=https://api.kodewithmj.xyz`

**Security checklist** (details in the deploy files' comments):

| Do | Why |
|---|---|
| Never publish DB ports on the VPS | An open Redis/Postgres on the internet is compromised in minutes — the prod compose already omits them |
| Real `JWT_SECRET` | Default lets anyone forge an admin token |
| Strong Postgres + Redis passwords | Default `unitrack` / no-auth is trivially breached |
| Cloudflare **Full (Strict)**, never Flexible | Flexible leaves Cloudflare→origin in cleartext |
| `ufw` to 22 + 443 only | Defense in depth behind the port choices |

Not yet hardened (documented, follow-up): Elasticsearch auth (kept internal-only
for now), ES replica + snapshot policy, rate limiting on `/auth/login`, and
refresh-token rotation/revocation (see [`docs/auth.md`](docs/auth.md) "Known gaps").

## What's next

- **Payment reconciler**: nightly diff of the gateway statement against `orders`, flagging paid-but-no-ticket, ticket-but-no-payment and amount mismatches (spec §9). Closes the last gap where a payment can be lost entirely.
- **Boarding QR & redemption** (spec §7.2/§7.5): rotating HMAC QR from `tickets.qr_secret`, a `redemptions` table, the helper app scanner and its offline nonce log, then the cross-device fraud sweep. A ticket cannot be used at all until this exists.
- **Student PWA**: buying is API-only today — no UI. Live map over `/track/nearby` plus the wallet.
- **Admin CRUD** for stops/routes — buses have endpoints, the rest is `scripts/dev_seed_routes.py`.
- **Live tracking WebSocket**: `/ws/track/{route_id}` fan-out of position + ETA + seats (spec §7.3 step 4).
- **ETA engine**: Mapbox `driving-traffic` worker job (spec §7.4), or the free route-offset path.
- **Reports** (§10) — `orders` and `tickets` now exist to aggregate.
- **ES hardening**: single-node ES is not durable — add a replica + snapshot policy before production; report/fraud jobs must query ES, not Postgres joins.

Build order: **P1 money & identity → P2 live ops → P3 validation & ETA → P4 reports & polish**.

## Sibling repos

- **[unitrack-web](https://github.com/mjobayerr/unitrack-web)** — Next.js student PWA + admin dashboard.
- **[unitrack-helper](https://github.com/mjobayerr/unitrack-helper)** — Flutter helper app (the on-bus sensor).

---

_Parts of this project were built with the help of AI._
