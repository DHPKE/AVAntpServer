#!/bin/sh
# Update the box from GitHub and install:   sudo /opt/avantp/deploy/update.sh
# Needs Update mode ON (WebUI -> System -> Update mode): the firewall otherwise blocks the box's own downloads.
# First run: installs git, clones the repository into ~/AVAntpServer-src. Later runs: fetch + reset to main. Then install.sh.
set -eu
REPO=${AVANTP_REPO:-https://github.com/DHPKE/AVAntpServer.git}
[ "$(id -u)" -eq 0 ] || { echo "run it with sudo" >&2; exit 1; }
OWNER=${SUDO_USER:-$(cat /etc/avantp/owner 2>/dev/null || echo node)}
HOME_DIR=$(getent passwd "$OWNER" | cut -d: -f6)
[ -n "$HOME_DIR" ] || { echo "unknown user $OWNER" >&2; exit 1; }
SRC="$HOME_DIR/AVAntpServer-src"
hint() { echo >&2; echo "Could not reach GitHub/Debian. Is Update mode ON? (WebUI -> System -> ENTER UPDATE MODE, it lasts 15 minutes)" >&2; exit 2; }

if ! command -v git >/dev/null 2>&1; then
  echo "git is not installed: installing it..."
  apt-get update -qq || true                       # a failed index refresh is tolerated; the install below decides
  apt-get install -y -qq git || hint
fi

if [ -d "$SRC/.git" ]; then
  # The clone is only a mirror of GitHub's main: never depend on its previous state (an empty repository left by an
  # earlier failed clone, local edits, a detached HEAD from a rollback, diverged history). Fetch and reset every time.
  before=$(runuser -u "$OWNER" -- git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo none)
  runuser -u "$OWNER" -- git -C "$SRC" remote set-url origin "$REPO" 2>/dev/null || true
  runuser -u "$OWNER" -- git -C "$SRC" fetch --depth 50 origin main || hint
  runuser -u "$OWNER" -- git -C "$SRC" checkout -q -f -B main FETCH_HEAD || { echo "could not switch to the downloaded version" >&2; exit 1; }
  after=$(runuser -u "$OWNER" -- git -C "$SRC" rev-parse --short HEAD)
  echo "code: $before -> $after"
else
  echo "first run: cloning $REPO into $SRC"
  runuser -u "$OWNER" -- git clone --depth 50 "$REPO" "$SRC" || hint
  echo "code: $(runuser -u "$OWNER" -- git -C "$SRC" rev-parse --short HEAD)"
fi
exec "$SRC/deploy/install.sh"
