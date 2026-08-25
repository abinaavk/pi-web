#!/usr/bin/env bash
#
# prepare_images.sh — builds the three base VM templates.
#
# Run this ONCE on the Pi itself (not in the sandbox this was written in —
# it needs real ARM64 hardware to do the first-boot customization pass at
# a sane speed). Needs: qemu-system-aarch64, qemu-img, wget, cloud-image-utils
# (for cloud-localds).
#
#   sudo apt install -y qemu-system-arm qemu-utils cloud-image-utils wget
#   chmod +x prepare_images.sh
#   ./prepare_images.sh
#
# This stage only installs LXQt + a terminal + baseline packages onto
# each image — it does NOT create a user account or set up autologin.
# That happens per-machine, at creation time, from whatever username
# and password you enter in the "New machine" form (see vm_manager.py
# make_machine_seed()). Each clone gets its own cloud-init pass with a
# fresh instance-id, which is what makes cloud-init re-run on top of an
# already-provisioned disk instead of being skipped as "already done".

set -euo pipefail

IMAGES_DIR="$(dirname "$0")/images"
WORK_DIR="$(mktemp -d)"

# ---------------------------------------------------------- image sources
# Debian and Ubuntu pin a specific release/codename — these directories
# are permanent once published, so they don't go stale. Alpine cuts a
# new stable branch roughly every 6 months and retires old ones off the
# mirror, so instead of pinning a version we point at "latest-stable"
# and discover the exact filename at runtime (see fetch_alpine below).
#
# If a URL below 404s (mirrors do reorganize occasionally), the error
# message will tell you exactly which line to edit — this script does
# NOT fail silently anymore.
ALPINE_INDEX="https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/cloud/"
DEBIAN_URL="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2"
UBUNTU_URL="https://cloud-images.ubuntu.com/minimal/releases/noble/release/ubuntu-24.04-minimal-cloudimg-arm64.img"

mkdir -p "$IMAGES_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "== Working in $WORK_DIR =="

# Check every external tool this script needs BEFORE downloading
# anything — a missing package should fail in a second, not after a
# 400MB download that then silently produces a broken image.
REQUIRED_TOOLS=(wget qemu-img qemu-system-aarch64 cloud-localds grep sort)
missing=""
for tool in "${REQUIRED_TOOLS[@]}"; do
  command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
done
if [ -n "$missing" ]; then
  echo "ERROR: missing required tools:$missing"
  echo "Install with:"
  echo "  sudo apt install -y qemu-system-arm qemu-utils cloud-image-utils wget"
  exit 1
fi
echo "-- All required tools found."

# Pre-flight check + real download, with a loud, specific failure
# instead of the silent exit `-q` used to cause. `--spider` does a
# HEAD-equivalent check so a bad URL fails in seconds, not after
# however long a multi-GB download takes to time out.
fetch() {
  local url="$1"
  local out="$2"
  local label="$3"

  echo "-- Checking $label URL: $url"
  if ! wget --spider -q "$url"; then
    echo ""
    echo "ERROR: $label download URL doesn't resolve (404 or DNS failure):"
    echo "  $url"
    echo "Mirrors reorganize their layout occasionally. Open that URL's"
    echo "parent directory in a browser to find the current filename,"
    echo "then edit the corresponding *_URL variable near the top of"
    echo "this script and re-run."
    return 1
  fi

  echo "-- Downloading $label (this is a real download, can take a while)"
  wget --progress=bar:force:noscroll -O "$out" "$url"
}

# ---------------------------------------------------------------- cloud-init
# Package-install only. Powers itself off once done so we know the pass
# is complete.
make_seed_iso() {
  local out="$1"
  local packages="$2"

  cat > "$WORK_DIR/user-data" <<EOF
#cloud-config
hostname: piweb-base
package_update: true
packages:
${packages}
runcmd:
  - poweroff
EOF

  cat > "$WORK_DIR/meta-data" <<EOF
instance-id: piweb-prep-${RANDOM}
local-hostname: piweb-base
EOF

  if ! cloud-localds "$out" "$WORK_DIR/user-data" "$WORK_DIR/meta-data"; then
    echo "ERROR: cloud-localds failed building $out"
    return 1
  fi
}

# Boots the seed against the disk once, headlessly, letting cloud-init
# do its thing, then waits for the VM to power itself off (the
# `poweroff` at the end of runcmd above).
customize_image() {
  local disk="$1"
  local seed="$2"
  local ram="$3"

  echo "== Customizing $disk (this can take 15-30+ min on Pi hardware) =="
  if ! qemu-system-aarch64 \
    -M virt -cpu host -enable-kvm \
    -m "$ram" \
    -drive file="$disk",if=virtio,format=qcow2 \
    -drive file="$seed",if=virtio,format=raw \
    -device virtio-net-pci,netdev=net0 \
    -netdev user,id=net0 \
    -nographic \
    -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd; then
    echo "ERROR: qemu-system-aarch64 exited with an error customizing $disk"
    return 1
  fi
}

# ------------------------------------------------------------------ alpine
build_alpine() {
  local raw="$WORK_DIR/alpine-cloud.qcow2"
  local final="$IMAGES_DIR/alpine-base.qcow2"

  echo "== Finding current Alpine aarch64 cloud image =="
  local listing filename
  listing="$(wget -qO- "$ALPINE_INDEX")" || {
    echo "ERROR: couldn't reach $ALPINE_INDEX — check network / mirror URL."
    return 1
  }
  # Loosened on purpose: match any *-aarch64-*cloudinit*.qcow2 filename
  # rather than one exact prefix, since Alpine's naming has shifted
  # before (nocloud_ vs generic_ vs no prefix at all across releases).
  filename="$(echo "$listing" | grep -oE '[A-Za-z0-9_.-]*aarch64[A-Za-z0-9_.-]*cloudinit[A-Za-z0-9_.-]*\.qcow2' | sort -V | tail -1)"
  if [ -z "$filename" ]; then
    echo "ERROR: couldn't find an aarch64 cloudinit image in the Alpine listing."
    echo "Here's what .qcow2 files ARE listed at $ALPINE_INDEX :"
    echo "$listing" | grep -oE '[A-Za-z0-9_.-]*\.qcow2' | sort -u | sed 's/^/  /'
    echo "Pick the right one from that list and set it directly, e.g.:"
    echo "  ALPINE_URL=\"${ALPINE_INDEX}<filename-from-above>\""
    echo "then swap the alpine fetch line in build_alpine() to use \$ALPINE_URL"
    echo "instead of the discovery logic, and re-run."
    return 1
  fi
  echo "-- Found: $filename"

  fetch "${ALPINE_INDEX}${filename}" "$raw" "Alpine" || return 1
  qemu-img resize "$raw" 8G || { echo "ERROR: qemu-img resize failed for Alpine"; return 1; }
  make_seed_iso "$WORK_DIR/alpine-seed.img" "  - lxqt\n  - lxqt-session\n  - xterm\n  - sudo\n  - chromium" || return 1
  customize_image "$raw" "$WORK_DIR/alpine-seed.img" 1024 || return 1
  mv "$raw" "$final"
  echo "== Alpine base ready: $final =="
}

# ------------------------------------------------------------------ debian
build_debian() {
  local raw="$WORK_DIR/debian-cloud.qcow2"
  local final="$IMAGES_DIR/debian-base.qcow2"
  echo "== Fetching Debian 12 arm64 generic cloud image =="
  fetch "$DEBIAN_URL" "$raw" "Debian" || return 1
  qemu-img resize "$raw" 8G || { echo "ERROR: qemu-img resize failed for Debian"; return 1; }
  make_seed_iso "$WORK_DIR/debian-seed.img" "  - lxqt\n  - lxqt-core\n  - xterm\n  - sudo" || return 1
  customize_image "$raw" "$WORK_DIR/debian-seed.img" 1024 || return 1
  mv "$raw" "$final"
  echo "== Debian base ready: $final =="
}

# ------------------------------------------------------------------ ubuntu
build_ubuntu() {
  local raw="$WORK_DIR/ubuntu-cloud.qcow2"
  local final="$IMAGES_DIR/ubuntu-base.qcow2"
  echo "== Fetching Ubuntu Server minimal arm64 cloud image (24.04 LTS) =="
  fetch "$UBUNTU_URL" "$raw" "Ubuntu" || return 1
  qemu-img resize "$raw" 8G || { echo "ERROR: qemu-img resize failed for Ubuntu"; return 1; }
  make_seed_iso "$WORK_DIR/ubuntu-seed.img" "  - lxqt\n  - lxqt-core\n  - xterm\n  - sudo" || return 1
  customize_image "$raw" "$WORK_DIR/ubuntu-seed.img" 1024 || return 1
  mv "$raw" "$final"
  echo "== Ubuntu base ready: $final =="
}

FAILED=""
build_alpine || FAILED="$FAILED alpine"
build_debian || FAILED="$FAILED debian"
build_ubuntu || FAILED="$FAILED ubuntu"

echo ""
if [ -n "$FAILED" ]; then
  echo "Done, but these failed:$FAILED — see the specific error above"
  echo "for each, fix the relevant URL variable near the top of this"
  echo "script, and re-run just this script again (it always rebuilds"
  echo "all three, so re-running is safe even for the ones that worked)."
  exit 1
fi
echo "All three base images are in $IMAGES_DIR"
echo "No account exists on them yet — each machine gets its own username"
echo "and password, set in the 'New machine' form when you create it."
