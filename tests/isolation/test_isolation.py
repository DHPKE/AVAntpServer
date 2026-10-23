"""Runtime isolation test: loads the GENERATED nftables + sysctl into the kernel of a network namespace
('box') wired to a WAN peer and a LAN host (two VLANs), then tries to get through in every direction.

Run as root on Linux (needs iproute2, nftables, iputils-ping, python3):
    docker run --rm --privileged -v $PWD:/src:ro debian:trixie /src/tests/isolation/run.sh
"""
import copy
import datetime
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import zoneinfo

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


RECEIVER = r'''
import json, socket, struct, sys, time
socks = {}
for dev in ("lan0.10", "lan0.20"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, 25, dev.encode() + b"\0")          # SO_BINDTODEVICE: one socket per VLAN
    s.setsockopt(socket.IPPROTO_IP, socket.IP_PKTINFO, 1)               # tells us the destination address of each packet
    s.bind(("", 6454)); s.settimeout(0.05); socks[dev] = s
got = {d: [] for d in socks}
dst = {d: {} for d in socks}
end = time.time() + float(sys.argv[1])
while time.time() < end:
    for d, s in socks.items():
        try:
            data, anc, _, _ = s.recvmsg(100, 100)
        except socket.timeout:
            continue
        got[d].append(data.hex())
        for lvl, typ, a in anc:
            if lvl == socket.IPPROTO_IP and typ == socket.IP_PKTINFO:
                ip = socket.inet_ntoa(a[8:12]); dst[d][ip] = dst[d].get(ip, 0) + 1
got["dst"] = dst
print(json.dumps(got))
'''


def artnet_tests(check, root):
    cfg_dir = tempfile.mkdtemp()
    both = copy.deepcopy(CFG)
    both["vlans"][0]["timecode"] = {"artnet": True, "fps": 25}    # VLAN 10: sender ON, firewall rule OFF (CFG has none)
    both["vlans"][1]["timecode"] = {"artnet": True, "fps": 25}    # VLAN 20: sender ON, firewall rule ON
    both["vlans"][1]["timezone"] = "America/New_York"
    json.dump(config.dump(config.validate(both)), open(f"{cfg_dir}/config.json", "w"))
    open("/tmp/receiver.py", "w").write(RECEIVER)
    rx = subprocess.Popen(["ip", "netns", "exec", "lanhost", "python3", "/tmp/receiver.py", "3"], stdout=subprocess.PIPE, text=True)
    time.sleep(0.4)
    code = ("import sys; sys.path.insert(0, '/work'); from avantp import timecode as t; import time\n"
            f"s = t.Sender('{cfg_dir}/config.json', '{cfg_dir}/status.json'); end = time.time() + 2.0\n"
            "while time.time() < end:\n    s.tick(); time.sleep(0.004)\n"
            "s.write_status(force=True)\n")
    r = subprocess.run(["ip", "netns", "exec", "box", "python3", "-c", code], capture_output=True, text=True)
    out = json.loads(rx.communicate(timeout=10)[0])
    status = json.load(open(f"{cfg_dir}/status.json"))
    p20 = [bytes.fromhex(h) for h in out["lan0.20"]]
    check("artnet: sender ran without errors", r.returncode, 0)
    check("artnet: VLAN 20 host receives about 25 packets/s for 2 s", 40 <= len(p20) <= 55, True)
    check("artnet: ... they are valid ArtTimeCode packets", all(p[:10] == b"Art-Net\x00\x00\x97" and len(p) == 19 for p in p20), True)
    ny = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York"))
    check("artnet: ... carrying New York's hour (the VLAN's time zone)", {p[17] for p in p20} <= {ny.hour, (ny.hour + 23) % 24}, True)
    check("artnet: VLAN 10 sender is ON but its firewall rule is OFF: nothing gets out", len(out["lan0.10"]), 0)
    st = {v["id"]: v["artnet"] for v in status["vlans"]}
    check("artnet: status says sending on VLAN 20", st[20]["state"], "sending")
    check("artnet: status shows the subnet broadcast as destination", st[20]["dest"], "10.20.0.255:6454 (broadcast)")
    check("artnet: status does not claim success on VLAN 10 forever (the packets left the sender, the kernel dropped them)",
          st[10]["state"] in ("sending", "error"), True)
    check("artnet: broadcast mode: the packets are addressed to the VLAN's broadcast address",
          out["dst"]["lan0.20"], {"10.20.0.255": len(p20)})
    unicast_tests(check, cfg_dir)


def unicast_tests(check, d_cfg):
    print("--- Art-Net unicast: to one receiver, pinned in the firewall")
    d = tempfile.mkdtemp()
    uni = copy.deepcopy(CFG)
    uni["vlans"][1]["timecode"] = {"artnet": True, "fps": 25, "artnet_dest": "10.20.0.10"}
    load(uni, d)                                                       # the firewall now allows exactly 10.20.0.10 on VLAN 20
    json.dump(config.dump(config.validate(uni)), open(f"{d_cfg}/config.json", "w"))
    rx = subprocess.Popen(["ip", "netns", "exec", "lanhost", "python3", "/tmp/receiver.py", "3.5"], stdout=subprocess.PIPE, text=True)
    wan_code = ("import socket, time\n"
                "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('', 6454)); s.settimeout(0.05); n = 0; e = time.time() + 3.5\n"
                "while time.time() < e:\n"
                "    try:\n        s.recv(100); n += 1\n    except socket.timeout:\n        pass\n"
                "print(n)\n")
    wan = subprocess.Popen(["ip", "netns", "exec", "wanpeer", "python3", "-c", wan_code], stdout=subprocess.PIPE, text=True)
    time.sleep(0.4)
    code = ("import sys, socket, time\n"
            "sys.path.insert(0, '/work')\n"
            "from avantp import timecode as t\n"
            f"s = t.Sender('{d_cfg}/config.json', '{d_cfg}/status.json'); end = time.time() + 2.0\n"
            "while time.time() < end:\n    s.tick(); time.sleep(0.004)\n"
            "s.write_status(force=True)\n"
            # a buggy or hostile sender: plain packets to addresses the config does not name, from the VLAN's own address
            "def raw(dst, n=6, src='10.20.0.2'):\n"
            "    x = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); x.bind((src, 0))\n"
            "    x.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)\n"
            "    for _ in range(n):\n"
            "        try:\n            x.sendto(b'stray', (dst, 6454))\n        except OSError:\n            pass\n"
            "for dst in ('10.20.0.99', '10.20.0.255', '10.99.0.1'):\n    raw(dst)\n")
    r = subprocess.run(["ip", "netns", "exec", "box", "python3", "-c", code], capture_output=True, text=True)
    out = json.loads(rx.communicate(timeout=10)[0])
    wan_n = int(wan.communicate(timeout=10)[0])
    st = {v["id"]: v["artnet"] for v in json.load(open(f"{d_cfg}/status.json"))["vlans"]}
    pk = [bytes.fromhex(h) for h in out["lan0.20"]]
    art = [p for p in pk if p[:8] == b"Art-Net\x00"]
    check("unicast: sender ran without errors", r.returncode, 0)
    check("unicast: the receiver on VLAN 20 gets the timecode (about 25 packets/s for 2 s)", 40 <= len(art) <= 55, True)
    check("unicast: ... addressed to the receiver itself, not to the broadcast address", out["dst"]["lan0.20"].get("10.20.0.10"), len(pk))
    check("unicast: ... status says unicast to that address", st[20]["dest"], "10.20.0.10:6454 (unicast)")
    check("unicast: stray packets to another host on the VLAN (10.20.0.99) are blocked by the firewall", out["dst"]["lan0.20"].get("10.20.0.99"), None)
    check("unicast: ... and so are stray broadcasts now that the broadcast allowance is gone", out["dst"]["lan0.20"].get("10.20.0.255"), None)
    check("unicast: a packet aimed at the WAN side (10.99.0.1:6454) never arrives there", wan_n, 0)
    check("unicast: nothing goes to VLAN 10 at all", len(out["lan0.10"]), 0)
    wan_overlap_test(check, d_cfg)
    mtc_tests(check, d_cfg)
    software_window_test(check, None)


def wan_overlap_test(check, d_cfg):
    print("--- Art-Net unicast: an address the kernel would route to the WAN is refused (VLAN network overlaps the WAN)")
    d = tempfile.mkdtemp()
    ov = copy.deepcopy(CFG)
    ov["vlans"][1]["timecode"] = {"artnet": True, "fps": 25, "artnet_dest": "10.20.0.130"}   # inside VLAN 20's /24 ...
    load(ov, d)
    json.dump(config.dump(config.validate(ov)), open(f"{d_cfg}/config.json", "w"))
    run("ip route add 10.20.0.128/25 dev enp4s0", "box")                                      # ... but the WAN has a more specific route
    wan_code = ("import socket, time\n"
                "s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.bind(('', 6454)); s.settimeout(0.05); n = 0; e = time.time() + 3.0\n"
                "while time.time() < e:\n    try:\n        s.recv(100); n += 1\n    except socket.timeout:\n        pass\nprint(n)\n")
    wan = subprocess.Popen(["ip", "netns", "exec", "wanpeer", "python3", "-c", wan_code], stdout=subprocess.PIPE, text=True)
    run("ip addr add 10.20.0.130/32 dev wan0", "wanpeer")                                    # a host that really lives on the WAN side
    time.sleep(0.4)
    code = ("import sys, time, errno, socket\n"
            "sys.path.insert(0, '/work')\n"
            "from avantp import timecode as t\n"
            f"s = t.Sender('{d_cfg}/config.json', '{d_cfg}/status.json'); end = time.time() + 1.5\n"
            "while time.time() < end:\n    s.tick(); time.sleep(0.004)\n"
            "s.write_status(force=True)\n"
            "x = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); x.bind(('10.20.0.2', 0))\n"
            "blocked = 0\n"
            "for _ in range(5):\n"
            "    try:\n        x.sendto(b'forced', ('10.20.0.130', 6454))\n"
            "    except OSError as e:\n        blocked += e.errno == errno.EPERM\n"
            "print('forced sends blocked by the firewall:', blocked)\n")
    r = subprocess.run(["ip", "netns", "exec", "box", "python3", "-c", code], capture_output=True, text=True)
    wan_n = int(wan.communicate(timeout=10)[0])
    a = {v["id"]: v["artnet"] for v in json.load(open(f"{d_cfg}/status.json"))["vlans"]}[20]
    run("ip route del 10.20.0.128/25 dev enp4s0", "box")
    check("overlap: the sender refuses and says the address is on the WAN side", a["state"] == "error" and "on the WAN side (enp4s0)" in a["error"], True)
    check("overlap: ... without sending a single packet itself", a.get("sent", 0), 0)
    check("overlap: ... a deliberately forced packet is stopped by the firewall (EPERM, the error you saw)", "forced sends blocked by the firewall: 5" in r.stdout, True)
    check("overlap: ... and nothing at all reaches the WAN side", wan_n, 0)


MTC_PEER = r'''
import json, socket, struct, sys, time
sys.path.insert(0, "/work")
from avantp import rtpmidi as r
src, dst, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); ctrl.bind((src, 0)); ctrl.settimeout(0.6)
data = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); data.bind((src, 0)); data.settimeout(0.05)
res = {"ok_ctrl": None, "ok_data": None, "ck": None, "rtp": 0, "qf": 0, "full": 0, "pieces": [], "hour_field": None}
def ask(sock, port, pkt):
    sock.sendto(pkt, (dst, port))
    try:
        return r.parse(sock.recvfrom(1500)[0])
    except socket.timeout:
        return None
inv = lambda: r.build_inv("IN", 4242, 0xCAFEBABE, b"Test Mac")
a = ask(ctrl, 5004, inv()); res["ok_ctrl"] = a and a["cmd"]
if a and a["cmd"] == "OK":
    data.settimeout(0.6)
    b = ask(data, 5005, inv()); res["ok_data"] = b and b["cmd"]
    data.settimeout(0.05)
    data.sendto(r.build_ck(0xCAFEBABE, 0, (777, 0, 0)), (dst, 5005))
end = time.time() + secs
while time.time() < end:
    try:
        p = data.recvfrom(1500)[0]
    except socket.timeout:
        continue
    m = r.parse(p)
    if m and m["cmd"] == "CK":
        res["ck"] = m["count"]
    elif p[:1] == b"\x80":
        res["rtp"] += 1
        cmd = p[13:]
        if cmd[:5] == bytes([0xF0, 0x7F, 0x7F, 0x01, 0x01]):
            res["full"] += 1; res["hour_field"] = cmd[5] & 31; res["rate"] = cmd[5] >> 5
        elif cmd[:1] == b"\xf1":
            res["qf"] += 1
            res["pieces"].append(cmd[1])
print(json.dumps(res))
'''


def mtc_tests(check, d_cfg):
    print("--- MTC over RTP-MIDI: a receiver connects through the firewall; other VLANs and the WAN get nothing")
    d = tempfile.mkdtemp()
    m = copy.deepcopy(CFG)
    m["vlans"][1]["timecode"] = {"artnet": False, "mtc_rtp": True, "fps": 25}          # VLAN 20 on, VLAN 10 off
    m["vlans"][1]["timezone"] = "America/New_York"
    load(m, d)
    json.dump(config.dump(config.validate(m)), open(f"{d_cfg}/config.json", "w"))
    open("/tmp/mtc_peer.py", "w").write(MTC_PEER)
    code = ("import sys, time\n"
            "sys.path.insert(0, '/work')\n"
            "from avantp import timecode as t\n"
            f"s = t.Sender('{d_cfg}/config.json', '{d_cfg}/status.json'); end = time.time() + 5.5\n"
            "while time.time() < end:\n    s.tick(); time.sleep(0.002)\n"
            "s.write_status(force=True)\n")
    sender = subprocess.Popen(["ip", "netns", "exec", "box", "python3", "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.0)
    run_peer = lambda ns, src, dst, secs: subprocess.Popen(["ip", "netns", "exec", ns, "python3", "/tmp/mtc_peer.py", src, dst, str(secs)],
                                                           stdout=subprocess.PIPE, text=True)
    ok = run_peer("lanhost", "10.20.0.10", "10.20.0.2", 2.5)           # VLAN 20: MTC on
    v10 = run_peer("lanhost", "10.10.10.10", "10.10.10.2", 1.5)        # VLAN 10: MTC off, no firewall rule
    wan = run_peer("wanpeer", "10.99.0.1", "10.99.0.2", 1.5)            # the WAN side
    res = {n: json.loads(p.communicate(timeout=20)[0]) for n, p in (("ok", ok), ("v10", v10), ("wan", wan))}
    sender.communicate(timeout=20)
    st = {v["id"]: v["mtc_rtp"] for v in json.load(open(f"{d_cfg}/status.json"))["vlans"]}
    o = res["ok"]
    check("mtc: the receiver on VLAN 20 completes the AppleMIDI handshake through the firewall (control OK, data OK, clock sync)",
          (o["ok_ctrl"], o["ok_data"], o["ck"]), ("OK", "OK", 1))
    check("mtc: ... gets a full-frame message and about 100 quarter frames per second (2.5 s)", o["full"] >= 1 and 200 <= o["qf"] <= 300, True)
    pieces = [p >> 4 for p in o["pieces"]]
    check("mtc: ... the quarter-frame pieces cycle 0..7 without gaps", all((b - a) % 8 == 1 for a, b in zip(pieces, pieces[1:])), True)
    ny = datetime.datetime.now(zoneinfo.ZoneInfo("America/New_York")).hour
    check("mtc: ... in the VLAN's own time zone (New York hour) and at 25 fps", o["hour_field"] in (ny, (ny + 23) % 24) and o["rate"] == 1, True)
    check("mtc: a host on VLAN 10 (MTC off) gets no answer at all", (res["v10"]["ok_ctrl"], res["v10"]["rtp"]), (None, 0))
    check("mtc: a host on the WAN side gets no answer at all", (res["wan"]["ok_ctrl"], res["wan"]["rtp"]), (None, 0))
    check("mtc: status shows the session with that receiver", st[20]["state"] == "sending" and [p["ip"] for p in st[20]["peers"]] == ["10.20.0.10"], True)
    check("mtc: status: VLAN 10 is off", st[10]["state"], "off")


def software_window_test(check, root):
    """The real helper logic with the real firewall: web access (tcp 443 on the WAN) exists only while it asks GitHub."""
    print("--- software update helper: web access only while it downloads")
    from avantp import software
    p = apply.Paths(tempfile.mkdtemp())
    for dd in (p.data, p.run, p.etc, os.path.join(p.root, "opt/avantp")):
        os.makedirs(dd, exist_ok=True)
    open(os.path.join(p.root, "opt/avantp/VERSION"), "w").write("abc1234")
    cfgd = copy.deepcopy(CFG)
    json.dump(config.dump(config.validate(cfgd)), open(os.path.join(p.etc, "config.json"), "w"))
    load(cfgd, tempfile.mkdtemp())                                   # the generated firewall incl. the update_window set
    ex = NetnsExecutor(p)
    server = ("import socket, threading\n"
              "s = socket.socket(); s.setsockopt(1, 2, 1); s.bind(('', 443)); s.listen(20)\n"
              "while True:\n    c, _ = s.accept(); c.close()\n")
    srv = subprocess.Popen(["ip", "netns", "exec", "wanpeer", "python3", "-c", server])
    time.sleep(0.5)
    reach_wan = lambda: sh("python3 /tmp/probe.py tcp 10.99.0.1 443", "box").returncode == 0
    inside = {}

    def fetch_ok(path, timeout=10):
        inside["reach"] = reach_wan()
        inside["set"] = sh("nft list set inet avantp update_window", "box").stdout
        return {"sha": "fff0001" + "0" * 33, "commit": {"message": "x", "committer": {"date": "2026-10-23"}}} if path == "/commits/main" else {"status": "ahead", "ahead_by": 1}

    def fetch_fail(path, timeout=10):
        inside["reach_fail"] = reach_wan()
        raise OSError("connection reset")
    open(os.path.join(p.data, "software.request"), "w").write("check")
    check("software: before the helper runs, the box cannot open a web connection to the WAN", reach_wan(), False)
    result = software.handle(p, ex, fetch=fetch_ok)
    check("software: while it asks GitHub the web window is open (a real TCP connection to the WAN side works)", inside["reach"], True)
    check("software: ... the window carries a kernel timeout (it closes by itself after 2 minutes at the latest)", "timeout 2m" in inside["set"].replace("\n", " ") or "timeout 120s" in inside["set"], True)
    check("software: ... the check succeeded", result, "checked")
    check("software: afterwards the window is closed again", reach_wan(), False)
    open(os.path.join(p.data, "software.request"), "w").write("check")
    result = software.handle(p, ex, fetch=fetch_fail)
    check("software: when GitHub cannot be reached the helper reports an error ...", result, "error")
    check("software: ... the window was open during the attempt and is closed again afterwards", (inside["reach_fail"], reach_wan()), (True, False))
    check("software: and the LAN/WAN separation is untouched by all of this (LAN host still cannot reach the WAN peer)", ping("lanhost", "10.99.0.1"), False)
    srv.kill()


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

    print("--- update mode (real kernel): web access to the WAN for a limited time")
    check("update: before, box -> WAN web port 80 is blocked", box_out("tcp", "10.99.0.1", 80, "wanpeer"), False)
    ex.update_window("enp4s0", True, 3)
    check("update: window open -> box reaches WAN web port 80", box_out("tcp", "10.99.0.1", 80, "wanpeer"), True)
    check("update: ... and port 443", box_out("tcp", "10.99.0.1", 443, "wanpeer"), True)
    check("update: ... other WAN ports stay closed", box_out("tcp", "10.99.0.1", 8080, "wanpeer"), False)
    check("update: ... LAN side gets no web access from the box", box_out("tcp", "10.10.10.10", 80, "lanhost"), False)
    check("update: ... LAN host still cannot reach the WAN peer", ping("lanhost", "10.99.0.1"), False)
    check("update: ... WAN peer still cannot reach the LAN host", ping("wanpeer", "10.10.10.10"), False)
    check("update: ... WAN peer still cannot open the box's WebUI", reach("wanpeer", "tcp", "10.99.0.2", 8443), False)
    time.sleep(3.5)
    check("update: the kernel closes the window by itself after its timeout", box_out("tcp", "10.99.0.1", 80, "wanpeer"), False)
    ex.update_window("enp4s0", True, 600)
    ex.update_window("enp4s0", False, 600)
    check("update: END UPDATE MODE closes it at once", box_out("tcp", "10.99.0.1", 80, "wanpeer"), False)
    ex.update_window("enp4s0", False, 600)
    check("update: closing a closed window is harmless", True, True)
    ex.update_window("enp4s0", True, 600)
    request(CFG)
    threading.Thread(target=lambda: (time.sleep(0.5), open(os.path.join(p.data, "confirm"), "w").close()), daemon=True).start()
    apply.apply_once(p, ex, confirm_seconds=10, poll=0.1)
    check("update: a firewall reload (apply) also closes an open window", box_out("tcp", "10.99.0.1", 80, "wanpeer"), False)

    print("--- Art-Net timecode sender (the real service code) through the real firewall")
    artnet_tests(check, root)

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
