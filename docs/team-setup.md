# Team Setup — UniTrack Backend

This guide is for developers who are **building and testing APIs**. You run the full database stack (Postgres, Redis, Elasticsearch) locally via Docker — no shared server, no credentials to ask for.

---

## What you get

| Service | Local port | Notes |
|---|---|---|
| PostgreSQL 16 | `localhost:5433` | All tables migrated, test data pre-loaded |
| Redis 7 | `localhost:6380` | Auth cache, GPS stream |
| Elasticsearch 8 | `localhost:9201` | GPS index (`gps_points`) |

---

## 1. Clone the repo

```bash
git clone https://github.com/mjobayerr/unitrack-backend.git
cd unitrack-backend
```

---

## 2. Start the local database stack

Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows) or Docker Engine (Linux).

```bash
# Copy the env file — default credentials work out of the box
cp .env.local.example .env.local          # Linux / macOS
Copy-Item .env.local.example .env.local   # Windows PowerShell

# Build and start (first run takes ~2 min to pull images and seed data)
docker compose -f docker-compose.local.yml up -d --build
```

The init container runs automatically: it applies all Alembic migrations and seeds the full dataset. Check progress with:

```bash
docker compose -f docker-compose.local.yml logs init -f
```

Wait for `--- local database ready ---` before starting the API.

**To wipe and start fresh:**

```bash
docker compose -f docker-compose.local.yml down -v
docker compose -f docker-compose.local.yml up -d --build
```

---

## 3. Configure your `.env`

Copy the example and point it at your local Docker stack:

```bash
cp .env.example .env          # Linux / macOS
Copy-Item .env.example .env   # Windows PowerShell
```

Set these values in `.env`:

```ini
ENV=dev
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
POSTGRES_USER=unitrack
POSTGRES_PASSWORD=localdev
POSTGRES_DB=unitrack

REDIS_HOST=localhost
REDIS_PORT=6380
REDIS_PASSWORD=localdev

ELASTICSEARCH_URL=http://localhost:9201
ELASTICSEARCH_USER=elastic
ELASTICSEARCH_PASSWORD=localdev

JWT_SECRET=local-dev-secret
ACCESS_TOKEN_TTL_MIN=15
REFRESH_TOKEN_TTL_DAYS=30
ALLOWED_STUDENT_EMAIL_DOMAINS=ulab.edu.bd
SERVICE_TIMEZONE=Asia/Dhaka
```

> **Windows users:** `.env` uses Unix line endings (`LF`). Use VS Code or Notepad++, not Notepad.

---

## 4. Install dependencies

The project uses [uv](https://docs.astral.sh/uv/) for dependency management.

```bash
# Install uv if you don't have it
curl -Lsf https://astral.sh/uv/install.sh | sh        # Linux / macOS
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows

# Install project dependencies
uv sync
```

---

## 5. Run the API

```bash
uv run uvicorn app.main:app --reload
```

Open `http://localhost:8000/docs` to explore all endpoints.

To also run the GPS worker (streams fixes from Redis into Elasticsearch):

```bash
uv run python -m app.worker
```

---

## 6. Test accounts (pre-seeded)

| Role | Email | Password |
|---|---|---|
| Admin | `admin@ulab.edu.bd` | `Admin@1234` |
| Helper | `helper1@buscrew.com.bd` | `Helper@1234` |
| Helper | `helper2@buscrew.com.bd` | `Helper@1234` |
| Student | `student1@ulab.edu.bd` | `Student@1234` |
| Student | `student2@ulab.edu.bd` | `Student@1234` |
| Student | `student3@ulab.edu.bd` | `Student@1234` |

Both helpers are pre-approved — `POST /helper/gps` and all helper endpoints work immediately after login.

---

## 7. Reseed manually (optional)

### Full reset — Postgres + Elasticsearch + Redis

When the environment has drifted (leftover smoke-test accounts, stale GPS fixes,
cached tokens), reset all three stores and reseed in one step:

```bash
docker compose exec api python -m scripts.reset_dev --yes
```

This truncates every table, drops and recreates the `gps_points` index, flushes
Redis, and reseeds. `alembic_version` is left alone, so the schema stays where
it is — run `alembic upgrade head` separately if migrations are pending.

Refuses to run unless `ENV=dev`, and `--yes` is required. **It destroys all
local data.**

### Reseed only

The Docker stack auto-seeds on startup. If you need to reseed without restarting Docker:

```bash
# Reseed everything (asks before wiping existing data)
uv run python -m scripts.seed

# Wipe and reseed without being asked
uv run python -m scripts.seed all --wipe

# Seed specific groups only
uv run python -m scripts.seed users
uv run python -m scripts.seed buses stops routes
uv run python -m scripts.seed trips reports alerts --wipe
```

### Available seed groups

| Group | What it creates |
|---|---|
| `users` | 1 admin, 2 approved helpers, 3 active students |
| `buses` | 4 buses (3 active, 1 inactive for testing) |
| `stops` | 7 boarding stops, Dhanmondi → Uttara corridor |
| `routes` | Campus Shuttle outbound + inbound with stop sequences |
| `trips` | 1 **live** trip (in progress), 2 completed trips |
| `reports` | 5 seat reports on the completed trips |
| `alerts` | 2 open alerts (1 critical SOS, 1 warning breakdown) |

---

## 8. Test with Postman

Import both files from the `postman/` folder:

1. Open Postman → **Import** → select both files at once:
   - `postman/UniTrack.postman_collection.json`
   - `postman/UniTrack.postman_environment.json`
2. In the top-right dropdown, select **"UniTrack Prod"** (or duplicate it and change `base_url` to `http://localhost:8000` for local testing).
3. Go to **Auth → Login**, fill in any test account above, and hit **Send**.
4. The login response auto-saves the token — every authenticated request picks it up automatically.

---

## 9. API quick reference

### Auth

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/register/student` | None | Requires `@ulab.edu.bd` email |
| POST | `/auth/register/helper` | None | Account starts as `pending_approval` |
| POST | `/auth/login` | None | Returns `access_token` + `refresh_token` |
| POST | `/auth/refresh` | None | Exchange refresh token for a new pair. **The token you send is consumed** — store the one that comes back |
| POST | `/auth/logout` | Bearer | Revokes the access token, and the refresh token if you send one in the body |
| GET | `/auth/me` | Bearer | Current user profile |

### Admin (requires admin account)

| Method | Path | Notes |
|---|---|---|
| GET | `/admin/helpers` | Approval queue; `?helper_status=pending` to filter |
| POST | `/admin/helpers/{id}/approve` | Approve a helper |
| POST | `/admin/users/{id}/suspend` | Suspend any account immediately |
| POST | `/admin/buses` | Create a bus |
| POST | `/admin/buses/batch` | Create multiple buses at once |
| GET | `/admin/alerts` | Open alerts, worst first |
| POST | `/admin/alerts/{id}/acknowledge` | Claim an alert |
| POST | `/admin/alerts/{id}/resolve` | Close an alert with a note |

### Fleet (authenticated)

| Method | Path | Notes |
|---|---|---|
| GET | `/fleet/buses` | All buses |
| GET | `/fleet/routes` | All routes |
| GET | `/fleet/routes/{id}` | One route with ordered stops |
| GET | `/fleet/stops` | All stops |

### Helper (requires approved helper account)

| Method | Path | Notes |
|---|---|---|
| POST | `/helper/trips/start` | Begin a trip (`bus_id` + `route_id`) |
| POST | `/helper/trips/end` | End the caller's live trip |
| GET | `/helper/trips/active` | Recover trip state after app restart |
| POST | `/helper/gps` | Ingest a batch of GPS fixes |
| POST | `/helper/seats` | Report current seat occupancy |
| POST | `/helper/alerts` | Raise an SOS or operational alert |

### Tracking

| Method | Path | Notes |
|---|---|---|
| GET | `/track/nearby` | `?lat=&lng=&radius_km=` — buses near a location |

### Shop (student accounts)

| Method | Path | Notes |
|---|---|---|
| GET | `/shop/products` | Ticket catalogue. Any signed-in account |
| POST | `/shop/orders` | `{product_id, idempotency_key}` → checkout URL. Retrying the same key returns the original order, never a second charge |
| GET | `/shop/orders` | The caller's own orders |
| GET | `/shop/tickets` | The caller's wallet |
| POST | `/shop/payments/return` | Gateway redirect target. Unauthenticated — see below |
| POST | `/shop/payments/ipn` | Gateway server-to-server report. Unauthenticated — see below |

Set `SSLCOMMERZ_STORE_ID` and `SSLCOMMERZ_STORE_PASSWORD` in `.env` before
buying anything, or order creation answers **502**. For a sandbox store the API
password is normally `<store_id>@ssl` — **not** the password shown in the
merchant panel's store detail. A wrong one fails with
`Store Credential Error Or Store is De-active`.

The two payment endpoints are unauthenticated because they must be: the gateway
carries no credential of ours. Nothing in those requests is trusted beyond
`tran_id` as a lookup key — the payment is confirmed by calling SSLCommerz back
and comparing the settled amount and currency with the order. On localhost no
`ipn_url` is registered, so settlement happens only when the browser returns.

### Auth flow

```
POST /auth/login  →  { access_token, refresh_token, expires_in }
Add: Authorization: Bearer <access_token>  to protected requests
Access token expires in `expires_in` seconds — call POST /auth/refresh to renew
POST /auth/refresh  →  a new pair; the refresh token you sent is now dead
```

Refresh tokens rotate, so a client that keeps reusing its original one gets a
401 on the second call. Always persist the pair the refresh returns.

A `403` means the token is valid but the role is wrong. A `401` means the token is missing, expired, or malformed.

---

## 10. Troubleshooting

| Problem | Fix |
|---|---|
| `docker compose` command not found | Use `docker-compose` (older Docker versions) or update Docker Desktop |
| Init container exits with error | Run `docker compose -f docker-compose.local.yml logs init` to see the cause |
| `Connection refused` on 5433/6380/9201 | Wait for the health checks to pass — run `docker compose -f docker-compose.local.yml ps` |
| `password authentication failed` | Make sure `.env` values match `.env.local` (both default to `localdev`) |
| `POST /helper/gps` returns 403 | Helper account is `pending` — run `uv run python -m scripts.seed users --wipe` |
| `GET /track/nearby` returns empty | GPS worker isn't running, or no GPS posted yet — run `uv run python -m app.worker` |
| Login returns 403 (not 401) | Account exists but isn't `active` — reseed or approve via `POST /admin/helpers/{id}/approve` |
| Elasticsearch `max virtual memory` error | On Linux/WSL: `sudo sysctl -w vm.max_map_count=262144`. On Windows, see `docs/dev-windows.md` |

---

## 11. Windows-specific setup

If you hit Elasticsearch memory errors on Windows (WSL 2), follow `docs/dev-windows.md` for the required `.wslconfig` setting before running Docker.
