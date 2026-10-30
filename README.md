# Race Voice

Windows 11 audio and announcement relay for race clubs. Send LiveRC system sound, microphone audio and spoken announcements from race control to multiple rooms through a Linux cloud relay.

All PCs make outbound connections to the relay, so they work across isolated guest networks without a direct LAN connection.

## Features

- System-output capture, large push-to-talk button, app-only Space/global F8 shortcuts, and optional always-on microphone.
- Five editable announcement slots plus one-off messages. Use {minutes} and {round} with the large counter controls.
- Text announcements spoken directly by Windows on the receiver's selected speakers.
- Compact announcement rows, automatic Fit, editable single-line messages and large Send/STOP buttons. Ctrl+plus/minus zooms, Ctrl+0 resets to 100%.
- Separate sender/receiver keys protected locally with Windows DPAPI.
- Remembered mode, address, devices and templates; optional startup auto-connect.
- Configurable WebSocket port, default 8843.
- Linux Docker relay with Let's Encrypt certificates using GoDaddy DNS validation and automatic renewal.
- HTTPS dashboard with traffic/event graphs, one hour of server-retained history, per-container CPU/memory, client logs, keys, certificate status, renewal and opt-in browser listening.

The Windows client is **v0.5.1**. The relay/dashboard are **v0.3.8**. Multi-receiver support requires rebuilding the relay container. Existing Windows clients and access keys remain compatible.

## Windows setup

Requires Windows 11 and Python 3.12 x64.

1. Clone or download this repository.
2. Run **setup.cmd** to install dependencies in a virtual environment.
3. Run **Start-Race-Voice.cmd**.
4. On Connection, select mode, relay URL and matching access key.
5. Select devices in Audio setup, then connect.

Receiver audio defaults on. On the sender, enable Send system audio for timing sounds, hold Space while this app is active or F8 globally for the microphone, or use the announcement buttons. Space types normally in text fields and never activates a focused Send button.

For a portable executable, run **Build-EXE.cmd** and copy the entire dist/RaceVoice directory. This repository contains source, not a signed installer or prebuilt EXE.

Auto-connect is optional. It requires a saved address, usable protected key and available saved devices. System-send and always-on microphone switches start off.

The Announcements page fits the five presets to the available window by default. Fit restores automatic fitting after a manual zoom. Ctrl+plus (or Ctrl+equals) zooms in, Ctrl+minus zooms out, and Ctrl+0 resets to 100%; keypad plus/minus work too. Zoom and Fit preferences are saved. At small sizes Fit may hide secondary previews/status tiles and spacing; the editable messages and audio controls remain available. Extremely small/high-DPI windows can still scroll. Zoom range is 50-150%.

## Windows updates

Close Race Voice, then double-click **Update-RaceVoice.cmd**. It downloads the latest GitHub source ZIP, validates/extracts it and installs to **C:\\apps\\RaceVoice**. The launcher can run from any folder; if its PowerShell helper is missing, it downloads it automatically. Future copies of both updater files are included at the project root.

The updater preserves your saved settings/access keys, local .env, virtual environment and existing EXE builds. It backs up files before replacing them under C:\\apps\\RaceVoice\\update-backups. It refreshes Python dependencies when requirements change or the virtual environment is missing. Python 3.12 x64 with the Python launcher is required. If C:\\apps requires elevated permissions, right-click the CMD and select Run as administrator.

Start the updated app with Start-Race-Voice.cmd. This updates source installations; use Build-EXE.cmd to rebuild an existing EXE.

## Linux relay setup

Requires Docker Engine and Docker Compose, a public hostname, and DNS hosted by GoDaddy.

~~~bash
git clone https://github.com/pfish3r/race-voice.git
cd race-voice
sudo bash setup-relay.sh
~~~

Run as root if sudo is unavailable. The script prompts for hostname, certificate email, port and GoDaddy Production Classic API key/secret, then generates client keys and an admin password in .env.

Point DNS at the relay and allow inbound TCP on the configured port. DNS validation does not require public port 80. Preserve .env and Docker certificate volumes during updates.

Open https://YOUR_HOSTNAME:8843/admin with username admin and the generated ADMIN_PASSWORD. Use SENDER_KEY and RECEIVER_KEY in the corresponding Windows apps.

See [UPGRADE.md](UPGRADE.md) for operations and diagnosis. Never commit or share .env, real keys, GoDaddy credentials or local settings.

## Diagnostics and behavior

Announcements queue on the receiver. STOP, disabling receiver audio or manual Disconnect cancels speech. Accepted announcements can finish during a relay reconnect. Message IDs suppress repeated deliveries within the client session.

Portal client logs show IDs, character counts, speech starts/completions/cancellations and duplicate events. The latest 100 metadata records are held in memory and clear on relay restart. Local logs rotate under %APPDATA%/RaceVoice. Announcement text and access keys are excluded.

No audio is recorded by the relay. TLS protects transport, but the relay can access audio; this is not end-to-end encryption. Browser listening is explicit and independent of Windows receiver playback. Test on another device or an output the sender is not capturing to avoid feedback.

## Development checks

~~~bash
python -m unittest discover -s tests -v
node tests/test_monitor.cjs
node tests/test_client_logs.cjs
node tests/test_metrics.cjs
node tests/test_receivers.cjs
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/test_windows_updater.ps1
~~~

Tests cover authentication/forwarding, portal controls/log sanitization, playback writes, speech queue/STOP/deduplication and native Tk layouts. Audio/SAPI tests use fakes; passing does not prove audibility. Verify Docker/certificate provisioning, hardware playback and EXE packaging in the deployment environment.

## Current scope

One sender, up to 32 Windows receivers, up to two browser listeners. One-way audio only. System capture includes every application on the selected output. Live PCM uses WebSocket/TCP and can stall or drop stale frames on poor connections. No packaged installer, unattended updates or device hotplug recovery.

## Multiple receivers (relay v0.3.8)

One sender broadcasts live system/microphone audio and text announcements to up to 32 Windows receivers. Use the same relay URL and Receiver key on every receiving PC. Enable receiver audio on each PC that should play sound; disabled PCs remain connected and count in the portal but receive no audio or announcement commands.

The portal shows connected receivers and how many have audio enabled. Browser listeners are counted separately. Each receiver has an independent bounded output queue, so a slow room cannot hold up other rooms. Recent audio is preferred over stale buffered audio; announcement commands retain FIFO order. New/reconnected receivers do not replay previous announcements. There is no cross-room playback synchronization or room targeting.

Outgoing bandwidth and forwarded event counters count each receiver delivery separately. A command delivered to three PCs counts as one event received and three forwarded. Client diagnostics include a temporary receiver connection ID, which changes on reconnect. The sender remains limited to one connection.

## Dedicated public Linux server

Use a fresh server with Docker Engine, Docker Compose, curl, unzip and OpenSSL installed. A dedicated hostname pointing at the server's public IP avoids private DNS overrides that may still point the existing relay hostname at a home LAN address.

As root, download and extract the source without needing Git:

~~~bash
mkdir -p /opt/race-voice
curl -fL https://github.com/pfish3r/race-voice/archive/refs/heads/main.zip -o /tmp/race-voice-main.zip
unzip -q /tmp/race-voice-main.zip -d /opt/race-voice
cd /opt/race-voice/race-voice-main
bash setup-relay.sh
~~~

For a dedicated server with TCP 443 free, enter 443 when setup asks for the public WebSocket port. Allow inbound TCP 443 in the hosting firewall and the server firewall. DNS certificate validation still uses GoDaddy; inbound port 80 is unnecessary. Outbound DNS and HTTPS must work.

Use wss://YOUR_HOSTNAME:443 in all Windows clients and https://YOUR_HOSTNAME/admin for the portal. Use the newly generated role keys unless you deliberately migrated the existing private configuration. Never paste GoDaddy credentials into chat. Verify certificate issuance with docker compose logs --tail=100 caddy, then test from the guest network. Port 443 can help where custom ports are filtered, but networks may still restrict WebSockets or particular destinations.
