#!/usr/bin/env bash
# Install the Nook controller on Raspberry Pi OS (Bookworm, 64-bit Lite).
# Run from the project folder:  sudo ./deploy/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> System packages"
apt-get update -q
apt-get install -y -q python3-venv python3-gpiozero python3-lgpio bluez

echo "==> Service user"
id nook >/dev/null 2>&1 || useradd --system --home /var/lib/nook --shell /usr/sbin/nologin nook
for g in dialout gpio i2c bluetooth; do getent group "$g" >/dev/null && usermod -aG "$g" nook; done

echo "==> Code in /opt/nook"
install -d -m 755 /opt/nook
cp -r "$SRC/nook" "$SRC/requirements.txt" /opt/nook/
python3 -m venv --system-site-packages /opt/nook/venv
/opt/nook/venv/bin/pip install -q --upgrade pip
/opt/nook/venv/bin/pip install -q -r /opt/nook/requirements.txt

echo "==> Configuration in /etc/nook"
install -d -m 750 -o root -g nook /etc/nook
install -d -m 750 -o nook -g nook /var/lib/nook
if [[ ! -f /etc/nook/config.toml ]]; then
  install -m 640 -o root -g nook "$SRC/config.example.toml" /etc/nook/config.toml
  echo "    edit /etc/nook/config.toml (cabinet id, broker, serial port)"
fi
if [[ ! -f /etc/nook/secret ]]; then
  head -c 32 /dev/urandom | base64 > /etc/nook/secret
  chown root:nook /etc/nook/secret && chmod 640 /etc/nook/secret
  echo "    new cabinet secret in /etc/nook/secret: register it in the cloud backend"
fi

echo "==> Clean shutdown on low battery"
echo "nook ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" > /etc/sudoers.d/nook
chmod 440 /etc/sudoers.d/nook
visudo -cf /etc/sudoers.d/nook >/dev/null

echo "==> systemd"
install -m 644 "$SRC/deploy/nook-controller.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable nook-controller
echo
echo "Done. Put the broker certificates in /etc/nook (ca.pem, cabinet.crt, cabinet.key),"
echo "check the config, then:  sudo systemctl start nook-controller && journalctl -u nook-controller -f"
