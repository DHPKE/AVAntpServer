#!/bin/sh
# Runs inside a privileged debian:trixie container; see test_isolation.py
set -e
apt-get update -qq >/dev/null
apt-get install -y -qq iproute2 nftables iputils-ping python3 procps >/dev/null 2>&1
cp -r /src /work
cd /work
exec python3 tests/isolation/test_isolation.py
