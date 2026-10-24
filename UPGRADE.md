# Updates and operations

## Windows

Close Race Voice, replace the whole source folder (including speech.py, playback.py and modern_widgets.py), then run Start-Race-Voice.cmd. If using an EXE, rebuild with Build-EXE.cmd. Copying source does not update an EXE.

Settings and DPAPI-protected keys live under %APPDATA%/RaceVoice. Keys are tied to the Windows account and should not be copied to another PC.

The v0.5.0 interface needs no new dependencies or Linux update when the relay already has v0.3.6 client diagnostics.

## Linux

From the existing deployment directory, preserve .env, the Compose project name and Docker volumes:

~~~bash
git pull --ff-only
docker compose up -d --build relay
~~~

This updates the relay/dashboard and briefly interrupts connections. If Compose, Caddy or setup scripts changed, review those changes and run setup-relay.sh as root instead.

For installations originally from ZIP, overwrite source in the existing directory while preserving .env, then rebuild. Never replace real credentials with .env.example values. Do not run docker compose down -v unless you intend to remove certificate state.

## Dashboard

Open https://YOUR_HOSTNAME:8843/admin with username admin and ADMIN_PASSWORD from .env.

- Keys can be shown/copied after admin authentication.
- Windows client log can filter by role and download JSON.
- Latest 100 records, rate limited per role, stored in memory until relay restart.
- Times are server receipt times, including events buffered during reconnect.
- Speech completion reports Windows SAPI status, not proof of speaker audibility.

Authentication uses HTTPS Basic authentication; there is no custom logout control.

## Certificates

Caddy issues and renews Let's Encrypt certificates with GoDaddy DNS-01. DNS must be on GoDaddy authoritative nameservers. Use Production Classic developer key:secret credentials, not OTE credentials or a PAT.

Certificates persist in Docker volumes. The portal shows the active TLS certificate and renewal status. Manual renewal is limited to one attempt per 24 hours and successful renewal briefly restarts Caddy. Automatic renewal runs separately.

Valid DNS, GoDaddy permissions and outbound HTTPS/DNS connectivity are required. Diagnose initial certificate failures in logs because dashboard HTTPS requires a certificate.

~~~bash
docker compose ps
docker compose logs --tail=100 relay
docker compose logs --tail=100 caddy
~~~

## Audio diagnosis

Choose devices before connecting. To isolate text announcements, turn off sender system capture, always-on microphone and browser listening.

Send Qualifying once. Match sender command_sent ID to receiver speech_queued, speech_started and speech_completed. Duplicate IDs should be ignored; cancellations carry reasons.

Test receiver locally exercises the same SAPI path without relay text transport. Open local logs opens the log/settings folder. Never share .env or settings.json with diagnostics.

Older diagnostic builds may have left last-announcement.wav. Current builds do not use or update it.
