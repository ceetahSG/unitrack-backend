# Authentication & authorization

Every endpoint in this API is authenticated and authorized unless it appears in
`PUBLIC_PATHS` ([`app/api/routes/__init__.py`](../app/api/routes/__init__.py)).
That is enforced by a test, not by convention —
[`tests/test_auth_coverage.py`](../tests/test_auth_coverage.py) fails the build
for any route that is neither guarded nor explicitly listed.

Worked example to copy: [`app/api/routes/admin.py`](../app/api/routes/admin.py).

## Adding a secured endpoint

```python
from fastapi import APIRouter, Depends
from app.api.deps import require_admin

router = APIRouter(prefix="/reports", dependencies=[Depends(require_admin)])

@router.get("/revenue")          # already admin-only; no auth code needed here
async def revenue(): ...
```

Then add `router` to `ROUTERS` in `app/api/routes/__init__.py`. Done.

Guard it on the **router**, not the route. Routes added later inherit it
automatically — which is the case that actually bites, because nobody re-reads
this document before adding an endpoint.

### The available guards

| Guard | Requires |
|---|---|
| `require_authenticated` | any signed-in, active account |
| `require_student` | `role = student` |
| `require_admin` | `role = admin` |
| `require_approved_helper` | `role = helper` **and** `helpers.status = approved` |
| `require(...)` | build your own — roles, `approved_helper`, `active_only` |

Need the caller's identity in the handler? Declare the same guard as a
parameter. It is free — FastAPI caches dependency results per request.

```python
async def create_thing(admin: Principal = Depends(require_admin)): ...
```

## The request flow

```
Authorization: Bearer <access_token>
        │
        ▼
  HTTPBearer            extract the token, 401 if the header is absent/malformed
        │
        ▼
  decode_token(         HS256 verify, exp check, and — critically —
    expected_type=      reject anything that is not an *access* token
    "access")           (a 30-day refresh token must not open a 15-min door)
        │
        ▼
  get_principal_cached  Redis GET authz:principal:{user_id}      ~0.15 ms
        │  miss ─────►  one LEFT JOIN over users + helpers       ~1–2 ms
        │               then SET with a 300 s TTL
        ▼
  Principal             user_id · role · status · helper_id · helper_status
        │
        ▼
  require(...)          403 if status is not active, role is not allowed,
        │               or the route needs an approved helper and this is not
        ▼
  your handler          runs only for a caller who has already passed all of it
```

`get_principal` is the only place a token is parsed. Never decode one in a
handler.

## Why it is fast

The obvious implementation queries Postgres on every request — two round trips,
~1–2 ms, to re-read rows that change maybe twice in an account's lifetime.
`POST /helper/gps` fires every 5 s per bus, and the live-map WebSocket will be
heavier, so that cost is paid tens of thousands of times a day.

| Step | Cost |
|---|---|
| HS256 verify | ~10 µs |
| Redis GET (cache hit) | ~0.15 ms |
| Postgres LEFT JOIN (cache miss) | ~1–2 ms |

At a ~99 % hit rate the auth path averages ~0.16 ms — roughly 10× cheaper than
querying every time, and the miss path is itself one query instead of two.

Stacked guards cost nothing extra. `require_admin` on the router plus
`Depends(require_admin)` in the handler resolves the token **once**: FastAPI
caches each dependency's result for the duration of a request.

Argon2 runs only at `/auth/login`. It is deliberately slow (~100 ms) and must
never appear on a per-request path.

## Why revocation still works

Caching authorization data usually means stale authorization data. Here it does
not, because correctness comes from **explicit invalidation**, not from the TTL:

> Any code path that writes to `users` or `helpers` calls
> `invalidate_principal(r, user_id)` after its commit.

Suspend an account and the next request re-reads the snapshot from Postgres,
sees `suspended`, and 401s. The user's access token is still cryptographically
valid — nothing can un-issue a JWT — but it no longer resolves to a usable
principal.

The 300 s TTL is only a backstop for changes made outside the API, like a manual
`UPDATE` in psql or a seed script.

**If you write an endpoint that mutates `users` or `helpers` and skip the
invalidation, you have written a security bug**: a suspended account keeps
working for up to five minutes. It is one cheap, idempotent `DEL`. When in
doubt, call it.

## Rules

| Rule | Reason |
|---|---|
| Guard the router, not the route | New routes inherit it |
| Pin `algorithms=[ALGORITHM]` | Blocks `alg: none` and HS/RS confusion |
| Always check the `type` claim | Stops refresh tokens being used as access tokens |
| Authorize from `Principal`, never the JWT `role` claim | A token minted before a demotion still carries the old role |
| `invalidate_principal()` after every user/helper write | Revocation depends on it. A flush hook now does this for you as a backstop, but calling it explicitly lands the change *before* the response rather than just after |
| Same 401 for every auth failure | Distinguishing them tells an attacker which half of the guess was right |
| `JWT_SECRET` from env, never a literal | A committed default is a permanent backdoor |

## Revocation

Every token carries a `jti`. Revoking one writes `auth:revoked:{jti}` to Redis
with a TTL equal to the token's remaining life — past `exp` the signature check
rejects it anyway, so storing it longer only wastes memory. See
[`app/core/revocation.py`](../app/core/revocation.py).

Three things use it:

| Where | What it does |
|---|---|
| `get_access_claims` | Rejects a revoked access token on every request |
| `POST /auth/refresh` | Revokes the token presented, then issues a new pair |
| `POST /auth/logout` | Revokes the caller's access **and** refresh token |

**Rotation makes replay detectable.** Because `/auth/refresh` denies the token
it consumed, presenting the same refresh token twice is never normal traffic —
it is a stolen token or a broken client. The second attempt gets the same 401 as
every other refresh failure, and the event is logged.

The failure policy is deliberately asymmetric, and the reasoning is in the
module docstring: the access-token check fails **open** (it runs on every
request; a Redis outage must not become an API outage, and the 15-minute TTL
bounds the exposure), while refresh and logout fail **closed** (rare, and the
30-day credential is worth more than the availability).

A logout only ends the session it was given. Signing out *every* device needs a
per-user token epoch — see gap 1 below.

## Known gaps

Not yet built, in priority order:

1. **No family-wide revocation.** Revocation is per-`jti`, so a detected refresh
   replay kills the replayed token but not the other tokens issued to that user.
   Fix: a per-user epoch counter in Redis, bumped on logout-everywhere or on a
   detected replay, with the epoch carried as a claim. Needs to survive a Redis
   restart, so it belongs in Postgres with Redis as the cache.
2. **Rate limiting lives only at nginx**, not in the app — see the table below.
3. No `iss` / `aud` claims. Low priority for a single-audience system.

## Rate limiting

Enforced at the edge, so it depends on which deployment you run:

| Config | Auth endpoints | Everything else |
|---|---|---|
| [`nginx.prod.conf`](../deploy/nginx.prod.conf) | 5 req/min per IP, burst 3 | 120 req/min, burst 30 |
| [`nginx.cloudflared.conf`](../deploy/nginx.cloudflared.conf) | 5 req/min per IP, burst 3 | 120 req/min, burst 30 |
| [`nginx.conf`](../deploy/nginx.conf) (dev) | none | none |

Both production configs key the limit on the **real visitor IP**, not on
`$remote_addr` as Cloudflare presents it — bucketing every user behind a handful
of edge IPs would throttle innocent traffic while barely slowing an attacker.
They get there differently, and the difference is the security boundary:

- `nginx.prod.conf` uses `set_real_ip_from` with Cloudflare's published ranges
  plus `real_ip_header CF-Connecting-IP`. A request from anywhere else keeps its
  true `$remote_addr` and **cannot spoof the header**. Refresh that range list
  from <https://www.cloudflare.com/ips/> periodically.
- `nginx.cloudflared.conf` trusts `CF-Connecting-IP` unconditionally, which is
  safe **only** because the tunnel container is the sole route to nginx and the
  compose file publishes no host ports. Publish one and the limiter is bypassable.

Since the limit is at the edge, anything that reaches the API directly is
unlimited. Today nothing can — neither production compose publishes `api:8000`.
That is an invariant worth keeping, not an accident.
