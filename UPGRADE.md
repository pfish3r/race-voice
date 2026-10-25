# Updates and operations

## Windows

Close Race Voice and double-click Update-RaceVoice.cmd. It fetches the latest source ZIP from GitHub and installs into C:\\apps\\RaceVoice, including updated copies of both updater files. If you only have the CMD, it automatically downloads its PowerShell helper. It does not require Git.

The updater validates/extracts into a temporary folder before touching the installation, rejects unsafe ZIP paths/links, preserves local configuration and creates a timestamped source backup under update-backups. Failed source copies are rolled back. If Python dependency installation fails, source is restored but dependencies may have partially changed; correct the reported error and rerun the updater. Temporary download/extraction files are cleaned automatically.

Python 3.12 x64 with the Python launcher is required. Dependencies are installed for a missing virtual environment or changed requirements. Right-click the CMD and Run as administrator if access to C:\\apps is denied. The updater asks you to close an identified running RaceVoice process instead of stopping it.

Launch the updated installation using C:\\apps\\RaceVoice\\Start-Race-Voice.cmd. If using an EXE, rebuild with Build-EXE.cmd. Source updates do not replace existing EXE builds.

For an offline ZIP or an alternate absolute local folder, run the PowerShell helper with -ArchivePath or -InstallPath. -SkipDependencies is available for source-only/offline testing; normal updates should leave it off. Existing user files are retained; the updater does not delete obsolete files.

Settings and DPAPI-protected keys live under %APPDATA%/RaceVoice. Keys are tied to the Windows account and should not be copied to another PC.

The v0.5.0 interface needs no new dependencies or Linux update when the relay already has v0.3.6 client diagnostics.

## Linux

From the existing deployment directory, preserve .env, the Compose project name and Docker volumes:

~~~bash
git pull --ff-only
sudo docker compose up -d --build relay caddy
~~~

For relay/dashboard v0.3.7, rebuild both relay and caddy: each image needs the new container accounting module. This briefly interrupts connections. If Compose, Caddy or setup scripts changed, review those changes and run setup-relay.sh as root instead.

For installations originally from ZIP, overwrite source in the existing directory while preserving .env, then rebuild. Never replace real credentials with .env.example values. Do not run docker compose down -v unless you intend to remove certificate state.

## Dashboard

Open https://YOUR_HOSTNAME:8843/admin with username admin and ADMIN_PASSWORD from .env.

- Keys can be shown/copied after admin authentication.
- Windows client log can filter by role and download JSON.
- Latest 100 records, rate limited per role, stored in memory until relay restart.
- Times are server receipt times, including events buffered during reconnect.
- Speech completion reports Windows SAPI status, not proof of speaker audibility.

Authentication uses HTTPS Basic authentication; there is no custom logout control.

## Traffic and container graphs (relay v0.3.7)

Usage graphs show received/sent application bandwidth, cumulative payload data, announcement/STOP events, dropped audio frames, and CPU/memory for both containers. Select 5 minutes, 15 minutes or 1 hour and download the retained measurements as JSON.

Sampling runs every 5 seconds in a background thread, independently of portal visits and live audio. Up to 721 samples (one hour) are kept in relay memory. Restarting the relay resets history and counters. No audio, text or keys are stored in metrics.

Events received means valid announcement/STOP commands accepted from the sender. Events forwarded counts successful WebSocket sends to the enabled Windows receiver. Browser copies are separate. Receiver-reported speech completions/errors are separate counters; forwarding alone does not confirm audibility.

Payload bytes include authenticated client audio/control/diagnostic messages and outbound browser audio/events. They exclude authentication/setup, WebSocket framing, TLS, dashboard HTTP and certificate traffic. They are not billable hosting data totals.

CPU/memory are read from each container's own Linux cgroup v2 or legacy v1 accounting files, without mounting the Docker socket or adding privileges. CPU 100% means one core and can exceed 100%. Memory includes charged file cache, so it can differ from Docker CLI's working-set display. Memory limits are shown when reported; unlimited means no explicit container memory cap. Missing cgroup data or an older/unreachable Caddy controller displays unavailable. CPU needs two samples.

The internal Caddy metrics endpoint uses the existing admin bearer token. Public graph data uses the same HTTPS admin authentication as the rest of the dashboard. There are no additional dependencies or public ports.

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
