# Team Setup — UniTrack Backend

This guide is for developers who are **building and testing APIs** against the shared development environment. You do not need to run Postgres, Redis, or Elasticsearch locally — they are already running on the shared VPS and pre-loaded with test data.

---

## What's available

| Service | Host | Port | Notes |
|---|---|---|---|
| PostgreSQL 16 | `103.110.78.246` | `5433` | Database with all tables migrated |
| Redis 7 | `103.110.78.246` | `6380` | Auth cache, GPS stream |
| Elasticsearch 8 | `103.110.78.246` | `9201` | GPS index (`gps_points`) |
| Live API | `https://api.kodewithmj.xyz` | `443` | Prod API (use Postman to test) |

---

## 1. Clone the repo

```bash
git clone https://github.com/mjobayerr/unitrack-backend.git
cd unitrack-backend
```

---

## 2. Configure your `.env`

Copy the example and point everything at the shared VPS:

```bash
# Linux / macOS
cp .env.example .env

# Windows (PowerShell)
Copy-Item .env.example .env
```

Then open `.env` and set these values (ask the team lead for credentials):

```ini
ENV=dev
POSTGRES_HOST=103.110.78.246
POSTGRES_PORT=5433
POSTGRES_USER=unitrack
POSTGRES_PASSWORD=<ask team lead>
POSTGRES_DB=unitrack

REDIS_HOST=103.110.78.246
REDIS_PORT=6380
REDIS_PASSWORD=<ask team lead>

ELASTICSEARCH_URL=http://103.110.78.246:9201
ELASTICSEARCH_USER=elastic
ELASTICSEARCH_PASSWORD=<ask team lead>

JWT_SECRET=<any value works in dev, e.g. dev-secret-key>
ACCESS_TOKEN_TTL_MIN=15
REFRESH_TOKEN_TTL_DAYS=30
ALLOWED_STUDENT_EMAIL_DOMAINS=ulab.edu.bd
SERVICE_TIMEZONE=Asia/Dhaka
```

> **Note for Windows users:** The `.env` file uses Unix line endings (`LF`). VS Code handles this automatically. If you open it in Notepad it may look odd — use VS Code or Notepad++ instead.

---

## 3. Install dependencies

The project uses [uv](https://docs.astral.sh/uv/) for dependency management. If you don't have it:

```bash
# Linux / macOS
curl -Lsf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then install:

```bash
uv sync
```

---

## 4. Run your API locally

No migrations needed — the shared database is already set up. Just start the API:

```bash
# Linux / macOS
uv run uvicorn app.main:app --reload

# Windows (PowerShell)
uv run uvicorn app.main:app --reload
```

The API connects to the shared databases defined in your `.env`. Open `http://localhost:8000/docs` to explore endpoints.

To also run the GPS worker (streams fixes from Redis into Elasticsearch):

```bash
uv run python -m app.worker
```

---

## 5. Seed the database

The shared database already has test data. If it ever gets dirty or needs a reset, run the seed script. It asks before wiping anything.

```bash
# Seed everything (asks before overwriting)
uv run python -m scripts.seed

# Wipe and reseed without being asked
uv run python -m scripts.seed all --wipe

# Seed specific groups only
uv run python -m scripts.seed users
uv run python -m scripts.seed buses stops routes
uv run python -m scripts.seed trips reports alerts --wipe
```

### Available groups

| Group | What it creates |
|---|---|
| `users` | 1 admin, 2 approved helpers, 3 active students |
| `buses` | 4 buses (3 active, 1 inactive for testing) |
| `stops` | 7 boarding stops, Dhanmondi → Uttara corridor |
| `routes` | Campus Shuttle outbound + inbound with stop sequences |
| `trips` | 1 **live** trip (in progress), 2 completed trips |
| `reports` | 5 seat reports on the completed trips |
| `alerts` | 2 open alerts (1 critical SOS, 1 warning breakdown) |

> **Smart wipe:** if wiping a group requires clearing dependents first (e.g., buses needs trips gone due to FK constraints), the script handles it automatically and tells you what it wiped.

### Test accounts (seeded)

| Role | Email | Password |
|---|---|---|
| Admin | `admin@ulab.edu.bd` | `Admin@1234` |
| Helper | `helper1@unitrack.test` | `Helper@1234` |
| Helper | `helper2@unitrack.test` | `Helper@1234` |
| Student | `student1@ulab.edu.bd` | `Student@1234` |
| Student | `student2@ulab.edu.bd` | `Student@1234` |
| Student | `student3@ulab.edu.bd` | `Student@1234` |

Both helpers are pre-approved, so `POST /helper/gps` and all helper endpoints work immediately after login.

---

## 6. Test with Postman

Import both files from the `postman/` folder:

1. Open Postman → **Import** → select both files at once:
   - `postman/UniTrack.postman_collection.json`
   - `postman/UniTrack.postman_environment.json`
2. In the top-right environment dropdown, select **"UniTrack Prod"**.
3. Go to **Auth → Login**, fill in any of the test account credentials above, and hit **Send**.
4. The login response auto-saves the token — every authenticated request picks it up automatically.

The collection has all 27 endpoints organised by tag (Auth, Admin, Fleet, Helper, Tracking), with example request bodies pre-filled.

> To test your **locally-running API** instead of the live one, duplicate the environment and change `base_url` to `http://localhost:8000`.

---

## 7. API quick reference

Base URL (live): `https://api.kodewithmj.xyz`

### Auth

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/auth/register/student` | None | Requires `@ulab.edu.bd` email |
| POST | `/auth/register/helper` | None | Account starts as `pending_approval` |
| POST | `/auth/login` | None | Returns `access_token` + `refresh_token` |
| POST | `/auth/refresh` | None | Exchange refresh token for a new pair |
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

### Fleet (public, authenticated)

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

### Auth flow summary

```
POST /auth/login
  → { access_token, refresh_token }
  → add  Authorization: Bearer <access_token>  to protected requests
  → access token expires in 15 min
  → call POST /auth/refresh with { refresh_token } for a new pair
```

Roles are enforced at the router level — not just in middleware. A 403 means the token is valid but the account doesn't have the required role. A 401 means the token is missing, expired, or malformed.

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `Connection refused` on port 5433/6380/9201 | The VPS firewall port may not be open yet — ask the team lead to run `sudo ufw allow 5433/tcp` etc. |
| `password authentication failed for user "unitrack"` | Check `POSTGRES_PASSWORD` in your `.env` matches what the team lead gave you |
| `POST /helper/gps` returns 403 | The helper account is `pending` — run `uv run python -m scripts.seed users --wipe` to reset and re-approve |
| `GET /track/nearby` returns empty | The GPS worker isn't running, or no GPS has been posted yet. Run `uv run python -m app.worker` |
| Login returns 403 (not 401) | Account exists but isn't `active` — re-seed or approve via `POST /admin/helpers/{id}/approve` |
| `curl` fails with unknown flags on Windows | Use `curl.exe` instead of `curl` (PowerShell aliases `curl` to `Invoke-WebRequest`) |
| Elasticsearch `max virtual memory` crash in Docker | See `docs/dev-windows.md` §2.1 — needs a `.wslconfig` setting |

---

## 9. Windows-specific setup

If you are on Windows and need to run Docker locally (for a fully local stack instead of the shared VPS), follow `docs/dev-windows.md` for Docker Desktop + WSL 2 setup before the steps above.
