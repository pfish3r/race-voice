# Race Voice

Windows 11 audio and announcement relay for race clubs. Send LiveRC system sound, microphone audio and spoken announcements from race control to another room through a Linux cloud relay.

Both PCs make outbound connections to the relay, so they work across isolated guest networks without a direct LAN connection.

## Features

- System-output capture, large push-to-talk button/global F8, and optional always-on microphone.
- Five editable announcement slots plus one-off messages. Use {minutes} and {round} with the large counter controls.
- Text announcements spoken directly by Windows on the receiver's selected speakers.
- Modern sidebar, single-line message editors, full previews and large Send/STOP buttons.
- Separate sender/receiver keys protected locally with Windows DPAPI.
- Remembered mode, address, devices and templates; optional startup auto-connect.
- Configurable WebSocket port, default 8843.
- Linux Docker relay with Let's Encrypt certificates using GoDaddy DNS validation and automatic renewal.
- HTTPS dashboard with connections/traffic, client logs, keys, certificate status, renewal and opt-in browser listening.

The Windows client is **v0.5.0**. The relay/dashboard retain their **v0.3.6** version label; interface-only updates do not require a relay rebuild.

## Windows setup

Requires Windows 11 and Python 3.12 x64.

1. Clone or download this repository.
2. Run **setup.cmd** to install dependencies in a virtual environment.
3. Run **Start-Race-Voice.cmd**.
4. On Connection, select mode, relay URL and matching access key.
5. Select devices in Audio setup, then connect.

Receiver audio defaults on. On the sender, enable Send system audio for timing sounds, hold F8 for the microphone, or use the announcement buttons.

For a portable executable, run **Build-EXE.cmd** and copy the entire dist/RaceVoice directory. This repository contains source, not a signed installer or prebuilt EXE.

Auto-connect is optional. It requires a saved address, usable protected key and available saved devices. System-send and always-on microphone switches start off.

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
~~~

Tests cover authentication/forwarding, portal controls/log sanitization, playback writes, speech queue/STOP/deduplication and native Tk layouts. Audio/SAPI tests use fakes; passing does not prove audibility. Verify Docker/certificate provisioning, hardware playback and EXE packaging in the deployment environment.

## Current scope

One sender, one Windows receiver, up to two browser listeners. One-way audio only. System capture includes every application on the selected output. Live PCM uses WebSocket/TCP and can stall or drop stale frames on poor connections. No installer, automatic updates or device hotplug recovery.
