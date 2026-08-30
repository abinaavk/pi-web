"""
vm_manager.py — Phase 2

Real QEMU launch (KVM-accelerated aarch64), a websockify bridge from
QEMU's VNC output to a websocket noVNC can consume in the browser, and
PID-based process control.

Everything here targets running ON the Pi itself. It will not boot a
guest in an environment without /dev/kvm and matching ARM64 hardware —
that's a hardware requirement, not a bug.
"""
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import uuid

IMAGES_DIR = os.path.join(os.path.dirname(__file__), "images")
DISKS_DIR = os.path.join(os.path.dirname(__file__), "disks")
SEEDS_DIR = os.path.join(os.path.dirname(__file__), "seeds")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

VNC_PORT = 5900          # fixed — only one machine ever runs at a time
WS_PORT = 6080           # fixed — matches the one extra Cloudflare Tunnel ingress rule
VNC_HOST = "127.0.0.1"   # never bind QEMU's VNC to anything but localhost

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(DISKS_DIR, exist_ok=True)
os.makedirs(SEEDS_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

BASE_IMAGES = {
    "alpine": os.path.join(IMAGES_DIR, "alpine-base.qcow2"),
    "debian": os.path.join(IMAGES_DIR, "debian-base.qcow2"),
    "ubuntu": os.path.join(IMAGES_DIR, "ubuntu-base.qcow2"),
}

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")


class VMError(Exception):
    pass


def _qemu_img_available():
    return shutil.which("qemu-img") is not None


def _bin_available(name):
    return shutil.which(name) is not None


def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((VNC_HOST, port)) == 0


def validate_credentials(username, password):
    if not USERNAME_RE.match(username or ""):
        raise VMError(
            "Username must be 3-32 characters, start with a lowercase "
            "letter, and use only lowercase letters, numbers, - or _."
        )
    if not password or len(password) < 8:
        raise VMError("Guest password must be at least 8 characters.")


def _hash_password(password):
    """
    SHA-512 crypt hash via openssl — this is the ONLY form of the guest
    password that ever touches disk or the database. The plaintext is
    used once, in memory, to produce this hash, and then discarded.
    """
    if not _bin_available("openssl"):
        raise VMError("openssl isn't installed — needed to hash the guest password.")
    result = subprocess.run(
        ["openssl", "passwd", "-6", "-stdin"],
        input=password, capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise VMError("Failed to hash the guest password.")
    return result.stdout.strip()


def _write_machine_seed(name, guest_username, password_hash):
    """
    Builds the per-machine cloud-init seed: creates the chosen user
    account (from its already-hashed password — plaintext is never
    written here) and points LXDM/LightDM's autologin at that user.
    Cloud-init treats each machine as a new instance (unique
    instance-id) so this re-runs even though the cloned disk already
    went through a cloud-init pass during prepare_images.sh.
    """
    if not _bin_available("cloud-localds"):
        raise VMError("cloud-localds isn't installed. Run: sudo apt install cloud-image-utils")

    seed_path = os.path.join(SEEDS_DIR, f"{name}-seed.img")
    work_dir = os.path.join(SEEDS_DIR, f".{name}-work")
    os.makedirs(work_dir, exist_ok=True)
    try:
        user_data_path = os.path.join(work_dir, "user-data")
        meta_data_path = os.path.join(work_dir, "meta-data")

        with open(user_data_path, "w") as f:
            f.write(f"""#cloud-config
hostname: {name}
users:
  - name: {guest_username}
    passwd: "{password_hash}"
    lock_passwd: false
    sudo: ALL=(ALL) NOPASSWD:ALL
    groups: sudo
    shell: /bin/bash
chpasswd:
  expire: false
runcmd:
  - mkdir -p /etc/lightdm/lightdm.conf.d
  - |
    cat > /etc/lightdm/lightdm.conf.d/50-piweb-autologin.conf <<CONF
    [Seat:*]
    autologin-user={guest_username}
    autologin-user-timeout=0
    user-session=lxqt
    CONF
  - systemctl enable lightdm || true
""")
        with open(meta_data_path, "w") as f:
            f.write(f"instance-id: piweb-{name}-{uuid.uuid4().hex[:8]}\n")
            f.write(f"local-hostname: {name}\n")

        subprocess.run(
            ["cloud-localds", seed_path, user_data_path, meta_data_path],
            check=True, capture_output=True, text=True,
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return seed_path


# ------------------------------------------------------------- disk/db ops

def create_machine(db, name, os_type, ram_mb, disk_gb, guest_username, guest_password):
    existing = db.execute("SELECT id FROM machines WHERE name = ?", (name,)).fetchone()
    if existing:
        raise VMError(f"A machine named '{name}' already exists.")

    validate_credentials(guest_username, guest_password)

    disk_path = os.path.join(DISKS_DIR, f"{name}.qcow2")
    base_image = BASE_IMAGES.get(os_type)

    if not base_image or not os.path.exists(base_image):
        raise VMError(
            f"Base image for '{os_type}' hasn't been prepared yet. "
            f"Run prepare_images.sh on the Pi first."
        )
    if not _qemu_img_available():
        raise VMError("qemu-img isn't installed. Run: sudo apt install qemu-utils")

    subprocess.run(
        ["qemu-img", "create", "-f", "qcow2", "-b", base_image,
         "-F", "qcow2", disk_path, f"{disk_gb}G"],
        check=True,
    )

    password_hash = _hash_password(guest_password)
    seed_path = _write_machine_seed(name, guest_username, password_hash)

    db.execute(
        "INSERT INTO machines (name, os_type, ram_mb, disk_gb, disk_path, status, "
        "guest_username, guest_password_hash, seed_path) "
        "VALUES (?, ?, ?, ?, ?, 'stopped', ?, ?, ?)",
        (name, os_type, ram_mb, disk_gb, disk_path, guest_username, password_hash, seed_path),
    )
    db.commit()


def delete_machine(db, machine):
    disk_path = machine["disk_path"]
    if disk_path and os.path.exists(disk_path):
        os.remove(disk_path)
    seed_path = machine["seed_path"] if "seed_path" in machine.keys() else None
    if seed_path and os.path.exists(seed_path):
        os.remove(seed_path)
    db.execute("DELETE FROM machines WHERE id = ?", (machine["id"],))
    db.commit()


# --------------------------------------------------------------- lifecycle

def start_machine(db, machine):
    if not _bin_available("qemu-system-aarch64"):
        raise VMError("qemu-system-aarch64 isn't installed. Run: sudo apt install qemu-system-arm")
    if not _bin_available("websockify"):
        raise VMError("websockify isn't installed. Run: pip install websockify")
    if not os.path.exists(machine["disk_path"]):
        raise VMError("This machine's disk is missing on disk — recreate the machine.")
    if not machine["seed_path"] or not os.path.exists(machine["seed_path"]):
        raise VMError("This machine's credential seed is missing — recreate the machine.")

    # Fixed ports rely on app.py's "one machine at a time" rule. If
    # either port is somehow already bound (a stale process from a
    # crash that the DB doesn't know about), fail loudly rather than
    # silently colliding with it.
    if _port_in_use(VNC_PORT) or _port_in_use(WS_PORT):
        raise VMError(
            "Port 5900 or 6080 is already in use by something outside "
            "piweb's tracking. Check `ps aux | grep qemu` / `| grep "
            "websockify` on the Pi and clear it manually, then try again."
        )

    vnc_port = VNC_PORT
    ws_port = WS_PORT
    vnc_display = 0  # QEMU's -vnc takes a display number: :0 == port 5900

    qemu_log = open(os.path.join(LOG_DIR, f"{machine['name']}-qemu.log"), "ab")
    qemu_cmd = [
        "qemu-system-aarch64",
        "-M", "virt", "-cpu", "host", "-enable-kvm",
        "-m", str(machine["ram_mb"]),
        "-drive", f"file={machine['disk_path']},if=virtio,format=qcow2",
        "-drive", f"file={machine['seed_path']},if=virtio,format=raw",
        "-device", "virtio-net-pci,netdev=net0",
        "-netdev", "user,id=net0",
        "-device", "virtio-gpu-pci",       # -M virt has no default video adapter — without this the VNC framebuffer is just empty/black
        "-device", "virtio-keyboard-pci",
        "-device", "virtio-tablet-pci",   # absolute pointer — maps 1:1 to browser mouse coords
        "-vnc", f"{VNC_HOST}:{vnc_display}",
        "-bios", "/usr/share/qemu-efi-aarch64/QEMU_EFI.fd",
        "-chardev", f"file,id=serial0,path={os.path.join(LOG_DIR, machine['name'] + '-serial.log')}",
        "-serial", "chardev:serial0",   # guest's own boot/console output — was previously invisible entirely
    ]

    try:
        qemu_proc = subprocess.Popen(
            qemu_cmd, stdout=qemu_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise VMError(f"Failed to launch QEMU: {e}")

    # Give QEMU a moment to open its VNC listener before we bridge it.
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex((VNC_HOST, vnc_port)) == 0:
                break
        time.sleep(0.2)
    else:
        qemu_proc.terminate()
        raise VMError("QEMU started but its VNC port never came up — check logs/.")

    ws_log = open(os.path.join(LOG_DIR, f"{machine['name']}-websockify.log"), "ab")
    ws_cmd = [
        "websockify", "--web", os.path.join(os.path.dirname(__file__), "static", "novnc"),
        "--heartbeat", "30",   # Cloudflare Tunnel drops idle websockets around ~100s of silence
        f"{VNC_HOST}:{ws_port}", f"{VNC_HOST}:{vnc_port}",
    ]
    ws_proc = subprocess.Popen(
        ws_cmd, stdout=ws_log, stderr=subprocess.STDOUT, start_new_session=True,
    )

    db.execute(
        "UPDATE machines SET status='running', pid=?, ws_pid=?, vnc_port=?, "
        "websock_port=?, last_active=datetime('now') WHERE id=?",
        (qemu_proc.pid, ws_proc.pid, vnc_port, ws_port, machine["id"]),
    )
    db.commit()


def stop_machine(db, machine):
    for pid in (machine["pid"], machine["ws_pid"]):
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        else:
            # Give it a moment, then force-kill if it's still alive —
            # QEMU should exit cleanly on SIGTERM but don't leave a
            # process pinning RAM if it hangs.
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.2)
            else:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    db.execute(
        "UPDATE machines SET status='stopped', pid=NULL, ws_pid=NULL, "
        "vnc_port=NULL, websock_port=NULL WHERE id=?",
        (machine["id"],),
    )
    db.commit()
