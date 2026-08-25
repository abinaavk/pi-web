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

mkdir -p "$IMAGES_DIR"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "== Working in $WORK_DIR =="

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

  cloud-localds "$out" "$WORK_DIR/user-data" "$WORK_DIR/meta-data"
}

# Boots the seed against the disk once, headlessly, letting cloud-init
# do its thing, then waits for the VM to power itself off (the
# `poweroff` at the end of runcmd above).
customize_image() {
  local disk="$1"
  local seed="$2"
  local ram="$3"

  echo "== Customizing $disk (this can take 15-30+ min on Pi hardware) =="
  qemu-system-aarch64 \
    -M virt -cpu host -enable-kvm \
    -m "$ram" \
    -drive file="$disk",if=virtio,format=qcow2 \
    -drive file="$seed",if=virtio,format=raw \
    -device virtio-net-pci,netdev=net0 \
    -netdev user,id=net0 \
    -nographic \
    -bios /usr/share/qemu-efi-aarch64/QEMU_EFI.fd
}

# ------------------------------------------------------------------ alpine
build_alpine() {
  local raw="$WORK_DIR/alpine-cloud.qcow2"
  local final="$IMAGES_DIR/alpine-base.qcow2"
  echo "== Fetching Alpine aarch64 cloud image =="
  wget -q --show-progress -O "$raw" \
    "https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/cloud/nocloud_alpine-3.20.3-aarch64-bios-cloudinit-r0.qcow2"
  qemu-img resize "$raw" 8G
  make_seed_iso "$WORK_DIR/alpine-seed.img" "  - lxqt\n  - lxqt-session\n  - xterm\n  - sudo\n  - chromium"
  customize_image "$raw" "$WORK_DIR/alpine-seed.img" 1024
  mv "$raw" "$final"
  echo "== Alpine base ready: $final =="
}

# ------------------------------------------------------------------ debian
build_debian() {
  local raw="$WORK_DIR/debian-cloud.qcow2"
  local final="$IMAGES_DIR/debian-base.qcow2"
  echo "== Fetching Debian 12 arm64 generic cloud image =="
  wget -q --show-progress -O "$raw" \
    "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-arm64.qcow2"
  qemu-img resize "$raw" 8G
  make_seed_iso "$WORK_DIR/debian-seed.img" "  - lxqt\n  - lxqt-core\n  - xterm\n  - sudo"
  customize_image "$raw" "$WORK_DIR/debian-seed.img" 1024
  mv "$raw" "$final"
  echo "== Debian base ready: $final =="
}

# ------------------------------------------------------------------ ubuntu
build_ubuntu() {
  local raw="$WORK_DIR/ubuntu-cloud.qcow2"
  local final="$IMAGES_DIR/ubuntu-base.qcow2"
  echo "== Fetching Ubuntu Server minimal arm64 cloud image =="
  wget -q --show-progress -O "$raw" \
    "https://cloud-images.ubuntu.com/minimal/releases/jammy/release/ubuntu-22.04-minimal-cloudimg-arm64.img"
  qemu-img resize "$raw" 8G
  make_seed_iso "$WORK_DIR/ubuntu-seed.img" "  - lxqt\n  - lxqt-core\n  - xterm\n  - sudo"
  customize_image "$raw" "$WORK_DIR/ubuntu-seed.img" 1024
  mv "$raw" "$final"
  echo "== Ubuntu base ready: $final =="
}

build_alpine
build_debian
build_ubuntu

echo ""
echo "All three base images are in $IMAGES_DIR"
echo "No account exists on them yet — each machine gets its own username"
echo "and password, set in the 'New machine' form when you create it."
