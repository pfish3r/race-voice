# AGENTS.md — Race Voice sandbox setup

Non-obvious things learned running this repo in the Base44 sandbox.

## What runs here

- `relay` service runs `relay.py` from the bind-mounted working copy (image
  `server/Dockerfile.dev` installs `server/requirements.txt` only — no source is baked in).
  The relay process serves two ports in one process: the WebSocket relay (`PORT`) and the
  dashboard HTTP server (`admin.py`, hardcoded `8090`, Basic auth).
- `proxy` service is `nginx:alpine` on host port 3000 (`server/nginx-dev.conf`). It replaces
  the production Caddy container, whose Let's Encrypt + GoDaddy DNS issuance cannot work in
  the sandbox. It mirrors Caddy's routes: `/admin/monitor` -> relay WS port, `/admin*` ->
  `8090`, everything else -> relay WS port, including WebSocket handshakes on `/`.
- The one divergence: a plain browser GET on `/` serves `server/preview-index.html` (a small
  landing page linking into the dashboard) instead of the relay's HTTP 400, because the
  preview entry point has to render something. A request carrying `Sec-WebSocket-Key` still
  goes to the relay, so WS clients behave as in production. `/favicon.ico` returns 204 so
  browser favicon probes stop logging relay handshake tracebacks.
- The Windows client (`app.py`, `speech.py`, `playback.py`) needs Windows, `pywin32` and
  `soundcard`; it is NOT part of this stack and cannot run in the sandbox.

## Quirks

- `PORT` is `443` inside the relay container so the dashboard's public endpoint field
  (`wss://<hostname>:<PORT>`) matches the port browser/Windows clients actually reach
  through the preview proxy. `RELAY_DOMAIN` is derived from `BASE44_PUBLIC_HOST_SUFFIX`
  (display + CSP origin only); the relay never validates the Host header.
- `admin.py` and `metrics.py` talk to a `caddy:8091` container that does not exist here.
  Both degrade gracefully: certificate status shows "unavailable" and the Caddy container
  card reports unavailable. That is expected, not a defect.
- The dashboard uses HTTP Basic auth (`admin` / `ADMIN_PASSWORD`). The managed preview
  browser cannot fill a native Basic-auth dialog, so verify it with authenticated `fetch`
  calls instead of screenshots.
- Dev credentials live in the committed `.env.base44-defaults` (placeholders, ≥24 chars,
  all different — `relay.py` refuses to start otherwise). `/run/base44/app.env` is loaded
  last so real values win.
- `relay.py` has no reload hook; restart the service or call `reload_preview` after backend
  edits.

## Verify it works

```bash
docker compose -f docker-compose.base44.yml ps           # both services healthy
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:3000/admin            # 401
curl -s -u "admin:$ADMIN_PASSWORD" http://localhost:3000/admin/api/status | head
docker compose -f docker-compose.base44.yml logs relay --tail=20
```

## Tests (inside the relay container — the sandbox host has no Python)

```bash
docker compose -f docker-compose.base44.yml exec -T relay \
  sh -c 'cd /app/tests && python -m unittest test_relay test_admin test_metrics -v'
```

Run it from inside `tests/`: `python -m unittest discover -s tests` from the repo root makes
`tests/test_metrics.py` fail to import `test_admin`. On Linux `discover` also reports 4
import errors for the Windows client modules (`test_client`, `test_playback`, `test_ui`,
`test_widgets`) — they need pywin32/Tk and are not runnable here.

`tests/test_ui.py`, `test_widgets.py`, `test_speech.py`, `test_playback.py` need Windows
Tk/SAPI/audio and are not runnable here. Node tests (`tests/*.cjs`) need a Node runtime.
