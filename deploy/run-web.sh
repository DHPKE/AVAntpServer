#!/bin/sh
# Run the WebUI in the foreground, reachable from the network: https://<this box>:8443
# Interim until the systemd service exists. Run as a normal user, not root:  ./deploy/run-web.sh
# Creates a self-signed certificate and the first user (admin) on first run.
set -eu
DIR=${AVANTP_DIR:-$HOME/avantp-data}
PORT=${PORT:-8443}
SRC=$(cd "$(dirname "$0")/.." && pwd)

mkdir -p "$DIR"
chmod 700 "$DIR"
if [ ! -f "$DIR/tls.key" ] || [ ! -f "$DIR/tls.crt" ]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 3650 -subj "/CN=avantp" \
    -keyout "$DIR/tls.key" -out "$DIR/tls.crt" 2>/dev/null
  chmod 600 "$DIR/tls.key"
  echo "created self-signed certificate in $DIR"
fi
export AVANTP_DIR="$DIR" PYTHONPATH="$SRC"
if [ ! -f "$DIR/users.json" ]; then
  echo "create the first user (admin):"
  python3 -m avantp passwd admin
fi
echo "WebUI: https://$(hostname -I | awk '{print $1}'):$PORT  (browser warns about the self-signed certificate)"
exec python3 -m avantp web 0.0.0.0 "$PORT"
