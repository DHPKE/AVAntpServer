"""Runtime isolation test: loads the GENERATED nftables + sysctl into the kernel of a network namespace
('box') wired to a WAN peer and a LAN host (two VLANs), then tries to get through in every direction.

Run as root on Linux (needs iproute2, nftables, iputils-ping, python3):
    docker run --rm --privileged -v $PWD:/src:ro debian:trixie /src/tests/isolation/run.sh
"""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from avantp import apply, config, render  # noqa: E402

CFG = {
    "wan": {"iface": "enp4s0", "mode": "dhcp", "commissioning": False},
    "lan": {"iface": "enp3s0"},
    "upstream": {"default": ["pool.ntp.org"]},
    "vlans": [
        {"id": 10, "name": "mgmt", "mode": "static", "address": "10.10.10.2/24", "mgmt": True},
        {"id": 20, "name": "show", "mode": "static", "address": "10.20.0.2/24", "timecode": {"artnet": True}},
    ],
}
LISTENER = r'''
import socket, threading
def udp(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(("0.0.0.0", port))
    while True:
        d, a = s.recvfrom(100)
        try:
            s.sendto(b"reply", a)
        except OSError:
            pass  # the firewall refused the reply: that is a result, not a crash
def tcp(port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(("0.0.0.0", port)); s.listen(5)
    while True:
        c, _ = s.accept(); c.close()
for t in [(udp, 123), (udp, 6454), (tcp, 8443), (tcp, 22), (tcp, 80)]:
    threading.Thread(target=t[0], args=(t[1],), daemon=True).start()
threading.Event().wait()
'''
PROBE = r'''
import socket, sys
kind, dst, port = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    if kind == "udp":
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(1.0); s.sendto(b"x", (dst, port)); s.recvfrom(100)
    else:
        s = socket.socket(); s.settimeout(1.0); s.connect((dst, port))
    sys.exit(0)
except Exception:
    sys.exit(1)
'''


def sh(cmd, ns=None):
    argv = ["ip", "netns", "exec", ns, "sh", "-c", cmd] if ns else ["sh", "-c", cmd]
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode and "check" in cmd:
        pass
    return r


def run(cmd, ns=None):
    r = sh(cmd, ns)
    if r.returncode:
        raise RuntimeError(f"{cmd!r} in {ns}: {r.stderr.strip()}")


def reach(ns, kind, dst, port):
    return sh(f"python3 /tmp/probe.py {kind} {dst} {port}", ns).returncode == 0


def ping(ns, dst):
    return sh(f"ping -c1 -W1 {dst}", ns).returncode == 0


def build():
    for n in ("box", "wanpeer", "lanhost"):
        sh(f"ip netns del {n}")
        run(f"ip netns add {n}")
    # WAN side: box.enp4s0 <-> wanpeer.wan0
    run("ip link add enp4s0 type veth peer name wan0 netns wanpeer")
    run("ip link set enp4s0 netns box")
    run("ip addr add 10.99.0.2/24 dev enp4s0; ip link set enp4s0 up; ip route add default via 10.99.0.1", "box")
    run("ip addr add 10.99.0.1/24 dev wan0; ip link set wan0 up; ip link set lo up", "wanpeer")
    run("ip route add 10.10.10.0/24 via 10.99.0.2; ip route add 10.20.0.0/24 via 10.99.0.2", "wanpeer")
    # LAN side: trunk with two VLANs, as the generated networkd files create them
    run("ip link add enp3s0 type veth peer name lan0 netns lanhost")
    run("ip link set enp3s0 netns box")
    run("ip link set enp3s0 up", "box")
    run("ip link set lan0 up; ip link set lo up", "lanhost")
    for vid, box_ip, host_ip in ((10, "10.10.10.2", "10.10.10.10"), (20, "10.20.0.2", "10.20.0.10")):
        run(f"ip link add link enp3s0 name enp3s0.{vid} type vlan id {vid}; ip addr add {box_ip}/24 dev enp3s0.{vid}; "
            f"ip link set enp3s0.{vid} up", "box")
        run(f"ip link add link lan0 name lan0.{vid} type vlan id {vid}; ip addr add {host_ip}/24 dev lan0.{vid}; "
            f"ip link set lan0.{vid} up", "lanhost")
    run("ip route add default via 10.10.10.2; ip route add 10.99.0.0/24 via 10.10.10.2", "lanhost")
    # the LAN host plays the attacker: it does no reverse-path filtering of its own
    run("sysctl -q -w net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0 "
        "net.ipv4.conf.lan0/10.rp_filter=0 net.ipv4.conf.lan0/20.rp_filter=0 net.ipv4.conf.lan0.rp_filter=0", "lanhost")
    run("ip link set lo up", "box")


def load(cfg, d):
    c = config.validate(copy.deepcopy(cfg))
    open(f"{d}/nft", "w").write(render.nftables(c))
    open(f"{d}/sysctl", "w").write(render.sysctl(c))
    run(f"nft -f {d}/nft", "box")
    run(f"sysctl -q -p {d}/sysctl", "box")


def main():
    d = tempfile.mkdtemp()
    open("/tmp/probe.py", "w").write(PROBE)
    open("/tmp/listen.py", "w").write(LISTENER)
    build()
    listener = subprocess.Popen(["ip", "netns", "exec", "box", "python3", "/tmp/listen.py"])
    time.sleep(0.5)
    results = []

    def check(name, got, want):
        ok = got == want
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"   (got {got}, wanted {want})"))

    # ---- baseline sanity: without rules the paths exist, so the tests below can fail
    run("sysctl -q -w net.ipv4.ip_forward=1", "box")
    check("sanity: with NO firewall and forwarding on, LAN reaches WAN peer (test can fail)", ping("lanhost", "10.99.0.1"), True)
    check("sanity: ... and WAN peer reaches LAN host", ping("wanpeer", "10.10.10.10"), True)
    run("sysctl -q -w net.ipv4.ip_forward=0", "box")

    load(CFG, d)
    print("--- generated rules loaded, commissioning OFF, forwarding 0")
    for fwd in (0, 1):
        run(f"sysctl -q -w net.ipv4.ip_forward={fwd}", "box")
        tag = f"[ip_forward={fwd}]"
        check(f"{tag} LAN host cannot ping WAN peer through the box", ping("lanhost", "10.99.0.1"), False)
        check(f"{tag} LAN host cannot open TCP to WAN peer through the box", reach("lanhost", "tcp", "10.99.0.1", 80), False)
        check(f"{tag} LAN host cannot send UDP to WAN peer through the box", reach("lanhost", "udp", "10.99.0.1", 123), False)
        check(f"{tag} WAN peer cannot ping LAN host through the box", ping("wanpeer", "10.10.10.10"), False)
        check(f"{tag} WAN peer cannot open TCP to LAN host through the box", reach("wanpeer", "tcp", "10.10.10.10", 80), False)
        check(f"{tag} WAN peer cannot open the other VLAN either", reach("wanpeer", "udp", "10.20.0.10", 123), False)
    run("sysctl -q -w net.ipv4.ip_forward=0", "box")

    print("--- the box itself, from the WAN side")
    check("WAN: NTP port 123 gives no reply", reach("wanpeer", "udp", "10.99.0.2", 123), False)
    check("WAN: WebUI 8443 closed", reach("wanpeer", "tcp", "10.99.0.2", 8443), False)
    check("WAN: SSH 22 closed", reach("wanpeer", "tcp", "10.99.0.2", 22), False)
    check("WAN: ping gets no reply", ping("wanpeer", "10.99.0.2"), False)
    check("WAN: Art-Net port closed", reach("wanpeer", "udp", "10.99.0.2", 6454), False)

    print("--- the box itself, from the LAN side")
    check("VLAN 10: NTP answered", reach("lanhost", "udp", "10.10.10.2", 123), True)
    check("VLAN 20: NTP answered", reach("lanhost", "udp", "10.20.0.2", 123), True)
    check("VLAN 10 (mgmt): WebUI reachable", reach("lanhost", "tcp", "10.10.10.2", 8443), True)
    check("VLAN 10 (mgmt): SSH reachable", reach("lanhost", "tcp", "10.10.10.2", 22), True)
    check("VLAN 20: WebUI NOT reachable", reach("lanhost", "tcp", "10.20.0.2", 8443), False)
    check("VLAN 20: SSH NOT reachable", reach("lanhost", "tcp", "10.20.0.2", 22), False)
    check("VLAN 10 host cannot use VLAN 20's address (NTP)", reach_via("lanhost", "lan0.10", "10.20.0.2", 123), False)
    check("LAN: other TCP ports closed", reach("lanhost", "tcp", "10.10.10.2", 80), False)

    print("--- what the box may start itself")
    check("box -> WAN peer UDP 123 (NTP client) allowed", box_out("udp", "10.99.0.1", 123, "wanpeer"), True)
    check("box -> WAN peer TCP 80 blocked", box_out("tcp", "10.99.0.1", 80, "wanpeer"), False)
    check("box -> LAN host TCP 80 blocked", box_out("tcp", "10.10.10.10", 80, "lanhost"), False)
    check("box -> LAN host UDP 6454 on VLAN 20 (Art-Net on) allowed", box_out("udp", "10.20.0.10", 6454, "lanhost"), True)
    check("box -> LAN host UDP 6454 on VLAN 10 (Art-Net off) blocked", box_out("udp", "10.10.10.10", 6454, "lanhost"), False)

    print("--- commissioning ON: WebUI + SSH on the WAN side, nothing else changes")
    c2 = copy.deepcopy(CFG)
    c2["wan"]["commissioning"] = True
    load(c2, d)
    check("commissioning: WAN WebUI reachable", reach("wanpeer", "tcp", "10.99.0.2", 8443), True)
    check("commissioning: WAN SSH reachable", reach("wanpeer", "tcp", "10.99.0.2", 22), True)
    check("commissioning: WAN still gets no NTP service", reach("wanpeer", "udp", "10.99.0.2", 123), False)
    run("sysctl -q -w net.ipv4.ip_forward=1", "box")
    check("commissioning + forwarding on: LAN still cannot reach WAN", ping("lanhost", "10.99.0.1"), False)
    check("commissioning + forwarding on: WAN still cannot reach LAN", ping("wanpeer", "10.10.10.10"), False)

    print("--- negative controls: the cross-VLAN check must be able to fail, and a second layer backs the first")
    import re
    load(CFG, d)
    open(f"{d}/nft_loose", "w").write(re.sub(r" ip daddr [0-9.]+", "", open(f"{d}/nft").read()))
    run(f"nft -f {d}/nft_loose", "box")
    check("layer 2: even WITHOUT the destination pin the reply is dropped (box answers from VLAN 10's address, a new "
          "connection the default-drop output policy refuses)", reach_via("lanhost", "lan0.10", "10.20.0.2", 123), False)
    run("nft add rule inet avantp output udp sport 123 accept", "box")
    check("control: pin removed AND output opened -> the VLAN 10 host DOES get an answer (check can detect a leak)",
          reach_via("lanhost", "lan0.10", "10.20.0.2", 123), True)

    helper_tests(check, True)

    listener.kill()
    for n in ("box", "wanpeer", "lanhost"):
        sh(f"ip netns del {n}")
    print("ALL PASS" if all(results) else "SOME FAILED", f"({sum(results)}/{len(results)})")
    return 0 if all(results) else 1


class NetnsExecutor(apply.SystemExecutor):
    """The real helper logic with REAL nft/sysctl, but inside the 'box' namespace; service/network manager calls are skipped."""

    def sh(self, *argv, check=True, timeout=60):
        if argv[0] in ("nft", "sysctl"):
            argv = ("ip", "netns", "exec", "box") + argv
        elif argv[0] in ("systemctl", "networkctl", "ifdown", "ip"):
            return subprocess.CompletedProcess(argv, 0, "", "")
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if check and r.returncode:
            raise RuntimeError(f"{' '.join(argv)}: {(r.stderr or r.stdout).strip()[:300]}")
        return r


def helper_tests(check, listener_ok):
    """Run apply_once against the live box namespace and look at the kernel afterwards."""
    root = tempfile.mkdtemp()
    p = apply.Paths(root)
    os.makedirs(p.data)
    os.makedirs(p.run)
    ex = NetnsExecutor(p)
    sh("nft delete table inet avantp", "box")
    run("sysctl -q -w net.ipv4.ip_forward=1", "box")          # start from a deliberately bad state

    def request(cfg):
        json.dump(cfg, open(os.path.join(p.data, "pending.json"), "w"))
        open(os.path.join(p.data, "apply.request"), "w").close()

    def tables():
        return sh("nft list tables", "box").stdout

    print("--- root helper with the real kernel: unconfirmed change")
    request(CFG)
    check("helper: unconfirmed apply reports rolled-back", apply.apply_once(p, ex, confirm_seconds=1, poll=0.1), "rolled-back")
    check("helper: ... and the firewall table is gone again (previous state restored)", "avantp" in tables(), False)
    check("helper: ... and no generated file is left behind", os.path.exists(os.path.join(root, "etc/avantp/nftables.nft")), False)

    print("--- root helper with the real kernel: confirmed change")
    request(CFG)

    def press():
        while json.load(open(os.path.join(p.run, "status.json"))).get("state") != "pending-confirm":
            time.sleep(0.05)
        open(os.path.join(p.data, "confirm"), "w").close()
    threading.Thread(target=press, daemon=True).start()
    check("helper: confirmed apply reports applied", apply.apply_once(p, ex, confirm_seconds=10, poll=0.1), "applied")
    check("helper: firewall table is loaded in the kernel", "avantp" in tables(), True)
    check("helper: forwarding switched off by the generated sysctl file",
          sh("sysctl -n net.ipv4.ip_forward", "box").stdout.strip(), "0")
    check("helper: after apply the LAN host still cannot reach the WAN peer", ping("lanhost", "10.99.0.1"), False)
    check("helper: after apply the WAN peer cannot reach the LAN host", ping("wanpeer", "10.10.10.10"), False)
    check("helper: after apply VLAN 10 still gets NTP", reach("lanhost", "udp", "10.10.10.2", 123), True)

    print("--- root helper with the real kernel: a later unconfirmed change restores the CONFIRMED ruleset")
    changed = copy.deepcopy(CFG)
    changed["wan"]["commissioning"] = True
    request(changed)
    check("helper: second change, nobody confirms -> rolled-back", apply.apply_once(p, ex, confirm_seconds=1, poll=0.1), "rolled-back")
    check("helper: ... WAN WebUI port is closed again (commissioning undone in the kernel)", reach("wanpeer", "tcp", "10.99.0.2", 8443), False)
    check("helper: ... the confirmed firewall is still loaded", "avantp" in tables(), True)

    print("--- root helper: a pending file that fails validation changes nothing")
    bad = copy.deepcopy(CFG)
    bad["vlans"][1]["id"] = 10
    request(bad)
    check("helper: invalid pending is refused", apply.apply_once(p, ex, confirm_seconds=1, poll=0.1), "error")
    check("helper: ... the confirmed firewall is untouched", "avantp" in tables(), True)


def reach_via(ns, dev, dst, port):
    """UDP probe that is forced out of one VLAN interface (source = that VLAN's address)."""
    src = {"lan0.10": "10.10.10.10", "lan0.20": "10.20.0.10"}[dev]
    code = (f"import socket,sys;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.setsockopt(socket.SOL_SOCKET,"
            f"25,b'{dev}\\0');s.bind(('{src}',0));s.settimeout(1.0);s.sendto(b'x',('{dst}',{port}));s.recvfrom(100)")
    box_mac = sh("cat /sys/class/net/enp3s0/address", "box").stdout.strip()
    sh(f"ip neigh replace {dst} lladdr {box_mac} dev {dev} nud permanent", ns)  # attacker pins the box's MAC for the foreign address
    return subprocess.run(["ip", "netns", "exec", ns, "python3", "-c", code], capture_output=True).returncode == 0


def box_out(kind, dst, port, peer_ns):
    """Start a listener in the peer namespace, probe from the box, report whether it got through."""
    if kind == "tcp":
        srv = ["python3", "-c", f"import socket;s=socket.socket();s.setsockopt(1,2,1);s.bind(('0.0.0.0',{port}));s.listen(5);"
                                "import time;[s.accept()[0].close() for _ in range(3)]"]
    else:
        srv = ["python3", "-c", f"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',{port}));"
                                "d,a=s.recvfrom(100);s.sendto(b'r',a)"]
    p = subprocess.Popen(["ip", "netns", "exec", peer_ns] + srv)
    time.sleep(0.4)
    ok = sh(f"python3 /tmp/probe.py {kind} {dst} {port}", "box").returncode == 0
    p.kill()
    return ok


if __name__ == "__main__":
    sys.exit(main())
