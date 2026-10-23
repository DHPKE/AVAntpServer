#!/bin/sh
# Update the box from GitHub, then install:  sudo ./deploy/update.sh      (run inside the git clone)
# One-time setup:  sudo apt-get install -y git && git clone https://github.com/DHPKE/AVAntpServer ~/AVAntpServer-src
set -eu
cd "$(dirname "$0")/.."
[ -d .git ] || { echo "this folder is not a git clone, so there is nothing to pull (see the setup line above)" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
OWNER=$(stat -c %U .git)                                   # pull as the clone's owner, not as root
before=$(runuser -u "$OWNER" -- git rev-parse --short HEAD)
runuser -u "$OWNER" -- git pull --ff-only
after=$(runuser -u "$OWNER" -- git rev-parse --short HEAD)
echo "code: $before -> $after"
exec ./deploy/install.sh
