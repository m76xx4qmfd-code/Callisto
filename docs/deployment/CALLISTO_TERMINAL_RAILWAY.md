# Callisto Terminal on Railway

Callisto Terminal is a dedicated public deployment of Callisto. Use a separate Railway project with three services:

- **frontend** — public Railway service and the only service assigned a public domain.
- **backend** — private Railway service reachable only through Railway private networking.
- **PostgreSQL** — Railway-managed database reachable only by the backend.

Do not add venue credentials and do not start scanner, trading, execution, reconciliation, or other worker planes in this project. This topology serves the API process and web UI only; it does not change Callisto trading defaults or safety controls.

## Variables

### Backend

Set these variables on the backend service:

- `DATABASE_URL` — Railway PostgreSQL connection URL (use Railway variable references).
- `PUBLIC_AUTH_ENABLED=true`
- `PUBLIC_AUTH_PASSWORD_HASH` — generated hash described below; never store the plaintext password.
- `PUBLIC_AUTH_IDLE_TIMEOUT_MINUTES=60` (or another operator-approved value; the app clamps it to 1–1440).
- `PUBLIC_APP_ORIGIN=https://callistoterminal.ai`
- `CORS_ORIGINS=["https://callistoterminal.ai"]`
- `REDIS_ENABLED=false` unless a separately reviewed private Redis service is added.

Railway supplies `PORT`; the backend Docker image reads it dynamically. Do **not** configure Polymarket, Kalshi, wallet, exchange, signing, venue API, or private-key variables.

### Frontend

Set these variables on the frontend service:

- `BACKEND_HOST` — the backend service's Railway private DNS hostname (for example the value Railway exposes for the backend service).
- `BACKEND_PORT` — backend private port, normally `8000`.

Railway supplies `PORT`. Nginx renders its startup template with all three values and proxies `/api`, `/mcp`, and `/ws` over private networking.

## Generate the password hash

Run this locally from the repository's `backend` directory. The password is entered without terminal echo and is never printed or written by the command:

```sh
python - <<'PY'
from getpass import getpass
from utils.passwords import hash_password
password = getpass("Callisto Terminal password: ")
confirmation = getpass("Confirm password: ")
if password != confirmation:
    raise SystemExit("Passwords do not match")
print(hash_password(password))
PY
```

Copy only the resulting hash to Railway's `PUBLIC_AUTH_PASSWORD_HASH` variable. Do not put a password or hash in source control, documentation, build arguments, or frontend variables.

## Deploy

1. Create a dedicated Railway project and add PostgreSQL.
2. Create the backend service from this repository with root directory `backend`. Railway uses `backend/railway.toml`, builds `backend/Dockerfile`, runs `alembic upgrade head`, and checks `/health`.
3. Add the backend variables above. Keep the backend private and confirm it has no public domain.
4. Create the frontend service with root directory `frontend`. Railway uses `frontend/railway.toml`, builds `frontend/Dockerfile`, and checks `/healthz`.
5. Set `BACKEND_HOST` to the backend's private Railway hostname and expose only the frontend.
6. Verify unauthenticated `/api` and `/mcp` requests are rejected, cross-origin login is rejected, same-origin login sets a Secure/HttpOnly/SameSite=Strict cookie, and `/healthz` remains healthy.

## Cloudflare and DNS

Attach `callistoterminal.ai` only to the frontend Railway service. Railway returns two ownership records for the custom domain: a CNAME routing target and a TXT verification record. Create **both** records in Cloudflare exactly as Railway returns them; Railway will return 404 until the TXT ownership record is present and verified.

Keep the verification TXT record DNS-only. Begin the CNAME as DNS-only while Railway validates ownership and provisions its origin certificate. After Railway reports the domain and certificate healthy, enable the Cloudflare proxy and set SSL/TLS mode to **Full (strict)**. Keep HTTPS enabled end to end. Never expose the backend or database through Cloudflare or a Railway public domain.

## Rollback

1. Roll back frontend and backend independently to the last known-good Railway deployment.
2. If authentication or origin validation is uncertain, remove or disable the frontend public domain first; do not bypass forced authentication.
3. Restore the prior variables and deployment revisions, then verify health and authentication before restoring DNS traffic.
4. Database migrations run before backend deployment; review migration compatibility before rolling application code back. Restore PostgreSQL from a Railway backup only when migration rollback cannot preserve data.

## License and source

Callisto is licensed under AGPL-3.0. Operators providing the application over a network must preserve the license notices and offer corresponding source code, including deployed modifications, as required by the AGPL. Source: <https://github.com/m76xx4qmfd-code/Callisto>.
