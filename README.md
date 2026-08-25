# piweb — Phase 2

Everything from Phase 1, plus: real QEMU VM boot (KVM-accelerated
aarch64), a real vendored noVNC client wired to keyboard/mouse, and
websockify bridging QEMU's VNC output to the browser.

## What changed from what was originally discussed

Two corrections worth knowing about, made for real hardware/software
reasons rather than convenience:

1. **Base images.** Raw Raspberry Pi OS and DietPi SD-card images are
   built for real Pi hardware (VideoCore GPU, Pi-specific bootloader)
   and don't reliably boot under QEMU's generic `-M virt` machine,
   which is what KVM acceleration on the Pi 4 actually supports. The
   three base images are now:
   - **Alpine Linux** (official aarch64 cloud/virt image)
   - **Debian 12** (arm64 generic cloud image — same base Raspberry Pi
     OS is built from, labeled "Pi OS-style" in the UI)
   - **Ubuntu Server (minimal, arm64)** — replacing DietPi, which also
     doesn't publish a virt-ready generic image

2. **Fixed VNC/websocket ports (5900 / 6080)** instead of a
   dynamically-hunted port per machine, since only one machine ever
   runs at a time — this also means your Cloudflare Tunnel config only
   needs one extra static rule (below), not a dynamic range.

## What's real vs. what needs your hardware to prove out

Tested in this build environment: the full Flask/DB/auth flow, and the
websockify bridge mechanism itself (verified end-to-end with a dummy
TCP server standing in for QEMU's VNC output — that exact bridge is
what `console.html`'s noVNC client connects through).

**Not testable without your Pi**: the actual QEMU boot of each guest,
and `prepare_images.sh`'s image customization pass. This sandbox has
no ARM64 hardware and no `/dev/kvm`. The QEMU/cloud-init commands are
written against the documented, standard syntax for this exact
setup, but the first real boot is the true test — check `logs/` if
something doesn't come up.

## Guest OS credentials — now chosen per machine, in the UI

The "New machine" form asks for a username and password for that
machine's desktop. Under the hood:

- On create, the password is hashed once (SHA-512 crypt, via
  `openssl passwd -6`) — the plaintext is never written to disk or
  the database, only the hash.
- A small per-machine cloud-init seed ISO is generated
  (`seeds/<name>-seed.img`) containing that username + hash, and gets
  attached as a second boot drive every time the machine starts.
- On first boot, cloud-init creates the account and sets LXQt to
  auto-login as that user — you land straight on the desktop with no
  second password prompt, since piweb's own login already gated
  access.
- `prepare_images.sh` no longer bakes in any fixed account — the base
  images only have packages installed. Every machine's login is
  genuinely its own.

If you ever need `sudo` inside a given machine, that's the username +
password you set when you created it.

## Setup on the Pi

```bash
sudo apt update
sudo apt install -y python3-venv qemu-system-arm qemu-utils qemu-efi-aarch64 \
    cloud-image-utils sqlite3 wget openssl

cd piweb
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

sqlite3 piweb.db < schema.sql
python3 setup_password.py    # sets YOUR admin login for the site itself

./prepare_images.sh          # downloads + preps all 3 base images — slow, run once
```

```bash
python3 app.py
```

Binds to `127.0.0.1:5000` only.

Then, separately, start the tunnel so it's reachable from the internet
(see the Cloudflare Tunnel section below) — run both `python3 app.py`
and `cloudflared tunnel run piweb` (in two terminals, or as systemd
services for anything beyond quick testing).

Once it's up: open the tunnel's URL → sign in with the piweb admin
password from `setup_password.py` → "+ New machine" → pick a base OS,
set a guest username/password → Create → Run.


## Cloudflare Tunnel — now needs TWO ingress rules

The main app and the VNC websocket bridge are two different local
ports, so both need routing through the tunnel to the same public
hostname:

```yaml
tunnel: piweb
credentials-file: /home/pi/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: your-subdomain.yourdomain.com
    path: ^/vncws/.*
    service: http://127.0.0.1:6080
  - hostname: your-subdomain.yourdomain.com
    service: http://127.0.0.1:5000
  - service: http_status:404
```

Order matters — the path-matched rule must come before the catch-all.

```bash
cloudflared tunnel run piweb
```

## What Phase 3 could cover

- systemd unit files for `piweb` and `cloudflared` so both survive
  reboots
- Resource/CPU usage indicator on the dashboard per running machine
- Snapshot/restore for a machine's disk state
- Optional Cloudflare Access as a second auth layer in front of the
  tunnel (dashboard checkbox, no code needed — can walk you through it)
