# Bull Desk deploy notes

- URL: https://desk.72.62.169.3.nip.io (Traefik, `websecure`, Let's Encrypt). No host ports published.
- Dir: `/opt/stacks/bull-desk` (clone of the fork `main`). Secrets in `.env` (mode 600). Never commit or paste it.

## Compose

```sh
cd /opt/stacks/bull-desk
docker compose --env-file .env -f infra/compose/docker-compose.yml -f docker-compose.vps.yml <cmd>
```

## Rebuild after pulling

api, worker and web share one image (`infra/compose/Dockerfile`).

```sh
git pull --ff-only
docker compose --env-file .env -f infra/compose/docker-compose.yml -f docker-compose.vps.yml build api worker web
docker compose --env-file .env -f infra/compose/docker-compose.yml -f docker-compose.vps.yml up -d
```

Web only (UI/brand changes): `... build web && ... up -d web`.

## Computers switch (default OFF)

`bin/bull-desk-computers.sh status | on --i-understand-docker-socket | off`

- `on` sets `SANDBOX_PROVIDER=docker` and starts `supervisor` + `computer` (profile `docker-computers`).
  The supervisor mounts the Docker socket, which is root on this host. Only on explicit owner request.
- `off` sets `SANDBOX_PROVIDER=none`, removes `supervisor` + `computer`, re-ups the stack.
  Bot computers the supervisor already started (label `rakazo.managed=true`) are left alone; `status` counts them.

**Never set `SANDBOX_PROVIDER=none`.** The app's contract has no `none` kind: new bots get a computer row with kind `none`, and every `threads/get` then fails with HTTP 500 "Output validation failed" (empty chat). Computers are OFF when the `supervisor` service (profile `docker-computers`) is not running; api/worker keep `SANDBOX_PROVIDER=docker`, no docker.sock is mounted, computers fail closed.

## Signups

Env names (api): `SIGNUPS_ENABLED`, `SIGNUP_ALLOWLIST`. Change in `.env`, then `up -d api`.

Allowlist is on (one address). Email verification goes through Resend (EMAIL_PROVIDER=resend). Hermes service account: credentials in /root/.hermes/credentials/bull-desk-hermes.env (mode 600).
