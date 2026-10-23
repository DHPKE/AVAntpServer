#!/bin/sh
# AVAntpServer installer for Debian 13 (Trixie). Run as root on the box:  sudo ./deploy/install.sh
# Installs the packages and the code, creates the services (WebUI unprivileged, apply helper as root) and a
# self-signed certificate, and blocks WiFi. It does NOT change the network or the firewall: that happens when you
# press APPLY TO SYSTEM in the WebUI, with automatic rollback unless you confirm.
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
. /etc/os-release
[ "${VERSION_ID:-}" = "13" ] || { echo "this installer targets Debian 13, found ${PRETTY_NAME:-unknown}" >&2; exit 1; }

SRC=$(cd "$(dirname "$0")/.." && pwd)
SD=""; command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] && SD=1   # empty in containers
DATA=/var/lib/avantp

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq chrony nftables systemd-resolved python3 rfkill openssl iproute2

# ---- user, directories, code
id avantp >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin avantp
install -d -o avantp -g avantp -m 750 "$DATA"
install -d -m 755 /etc/avantp
rm -rf /opt/avantp && mkdir /opt/avantp
cp -r "$SRC/avantp" "$SRC/ui" /opt/avantp/
find /opt/avantp -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root /opt/avantp && chmod -R go-w /opt/avantp

# ---- keep what an earlier hand-started WebUI created (login + certificate)
OLD=$(getent passwd "${SUDO_USER:-node}" | cut -d: -f6)/avantp-data
for f in users.json tls.crt tls.key; do
  [ -f "$DATA/$f" ] || { [ -f "$OLD/$f" ] && cp "$OLD/$f" "$DATA/$f" && echo "kept $f from $OLD"; } || true
done
if [ ! -f "$DATA/tls.key" ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=avantp" -keyout "$DATA/tls.key" -out "$DATA/tls.crt" 2>/dev/null
  echo "created a self-signed certificate"
fi
chown avantp:avantp "$DATA"/* && chmod 600 "$DATA"/tls.key "$DATA"/users.json 2>/dev/null || true

# ---- the password tool
cat > /usr/local/sbin/avantp-passwd <<'WRAP'
#!/bin/sh
# set or change a WebUI password:  sudo avantp-passwd [user]
exec runuser -u avantp -- env PYTHONPATH=/opt/avantp python3 -m avantp passwd "${1:-admin}"
WRAP
chmod 755 /usr/local/sbin/avantp-passwd

# ---- services
cp "$SRC"/deploy/systemd/avantp-* /etc/systemd/system/
if [ -n "$SD" ]; then
  systemctl daemon-reload
  systemctl disable nftables.service >/dev/null 2>&1 || true   # Debian's own unit would flush our rules at boot
  pkill -f "[a]vantp web" 2>/dev/null || true                  # a hand-started WebUI would hold port 8443
  sleep 1
fi

# ---- WiFi would be a third path between the two sides: block it and keep its driver from loading
rfkill block wifi 2>/dev/null || true
for d in /sys/class/net/*/wireless; do
  [ -d "$d" ] || continue
  dev=$(basename "$(dirname "$d")")
  mod=$(basename "$(readlink -f "/sys/class/net/$dev/device/driver/module" 2>/dev/null)" 2>/dev/null || true)
  if [ -n "$mod" ] && [ "$mod" != "." ]; then
    echo "install $mod /bin/false" > "/etc/modprobe.d/avantp-no-wifi-$mod.conf"
    echo "WiFi $dev: driver $mod disabled (effective after reboot)"
  fi
done

if [ ! -f "$DATA/users.json" ]; then
  if [ -t 0 ]; then echo "create the first WebUI user (admin):"; /usr/local/sbin/avantp-passwd admin
  else echo "no WebUI user yet: run  sudo avantp-passwd admin"; fi
fi

if [ -n "$SD" ]; then
  systemctl enable avantp-nftables.service avantp-apply.path >/dev/null 2>&1
  systemctl enable --now avantp-web.service avantp-apply.path
  sleep 1
  systemctl is-active avantp-web.service avantp-apply.path
fi
echo "installed: $(chronyd --version | head -1 | cut -c1-30), $(nft --version)"
echo "WebUI: https://$(hostname -I | awk '{print $1}'):8443   (network and firewall are unchanged until you APPLY)"
