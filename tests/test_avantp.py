import copy
import unittest

from avantp import config, render

BASE = {
    "wan": {"iface": "enp4s0", "mode": "dhcp"},
    "lan": {"iface": "enp3s0"},
    "upstream": {"default": ["pool.ntp.org"], "backup": ["time.cloudflare.com"], "custom": []},
    "vlans": [
        {"id": 10, "name": "mgmt", "mode": "static", "address": "10.10.10.2/24", "mgmt": True, "timezone": "Europe/Berlin"},
        {"id": 20, "name": "show", "mode": "static", "address": "10.20.0.2/24", "timezone": "America/New_York",
         "timecode": {"artnet": True}},
        {"id": 30, "name": "dhcp-site", "mode": "dhcp"},
    ],
}


def cfg(**edit):
    c = copy.deepcopy(BASE)
    for k, v in edit.items():
        c[k] = v
    return config.validate(c)


class Validation(unittest.TestCase):
    def bad(self, c):
        with self.assertRaises(config.ConfigError):
            config.validate(c)

    def test_ok(self):
        cfg()

    def test_max_20_vlans(self):
        vl = [{"id": 100 + i, "name": f"v{i}", "mode": "dhcp", "mgmt": i == 0} for i in range(21)]
        self.bad({**copy.deepcopy(BASE), "vlans": vl})
        config.validate({**copy.deepcopy(BASE), "vlans": vl[:20]})

    def test_duplicate_id_and_overlap(self):
        c = copy.deepcopy(BASE)
        c["vlans"][1]["id"] = 10
        self.bad(c)
        c = copy.deepcopy(BASE)
        c["vlans"][1]["address"] = "10.10.10.99/24"
        self.bad(c)

    def test_timezone_by_city(self):
        for ok in ("Europe/Berlin", "America/Argentina/Buenos_Aires", "UTC", "Asia/Kathmandu"):
            c = copy.deepcopy(BASE)
            c["vlans"][0]["timezone"] = ok
            self.assertEqual(config.validate(c)["vlans"][0]["timezone"], ok)
        for bad in ("Mars/Olympus", "../../etc/passwd", "Europe/Berlin\nx", "", 60, "A" * 50):
            c = copy.deepcopy(BASE)
            c["vlans"][0]["timezone"] = bad
            self.bad(c)

    def test_default_timezone_utc_and_old_offset_dropped(self):
        c = copy.deepcopy(BASE)
        del c["vlans"][0]["timezone"]
        c["vlans"][0]["utc_offset_min"] = 60
        v = config.validate(c)["vlans"][0]
        self.assertEqual(v["timezone"], "UTC")
        self.assertNotIn("utc_offset_min", v)

    def test_needs_mgmt_unless_commissioning(self):
        c = copy.deepcopy(BASE)
        c["vlans"][0]["mgmt"] = False
        self.bad(c)
        c["wan"]["commissioning"] = True
        config.validate(c)

    def test_first_boot_has_no_vlans(self):
        config.validate({**copy.deepcopy(BASE), "vlans": [], "wan": {**BASE["wan"], "commissioning": True}})

    def test_injection_rejected(self):
        for h in ("pool.ntp.org\nlocal stratum 1", "a b", "x;y", ""):
            c = copy.deepcopy(BASE)
            c["upstream"]["custom"] = [h]
            self.bad(c)
        c = copy.deepcopy(BASE)
        c["vlans"][0]["name"] = "a\nb"
        self.bad(c)

    def test_same_nic_rejected(self):
        c = copy.deepcopy(BASE)
        c["lan"]["iface"] = "enp4s0"
        self.bad(c)

    def test_wan_overlap_rejected(self):
        c = copy.deepcopy(BASE)
        c["wan"] = {"iface": "enp4s0", "mode": "static", "address": "10.10.10.5/24", "gateway": "10.10.10.1"}
        self.bad(c)


class Render(unittest.TestCase):
    def test_nft_isolation(self):
        n = render.nftables(cfg())
        self.assertIn("hook forward priority 0; policy drop;", n)
        self.assertIn("hook input priority 0; policy drop;", n)
        self.assertIn("hook output priority 0; policy drop;", n)
        # nothing may initiate towards the box from WAN
        self.assertNotIn('iifname "enp4s0" udp dport 123', n)
        self.assertNotIn('iifname "enp4s0" tcp', n)
        self.assertNotIn("dport 22", n.split("# VLAN 10")[0])

    def test_webui_and_ssh_only_on_mgmt_vlan(self):
        n = render.nftables(cfg())
        self.assertIn('iifname "enp3s0.10" tcp dport { 22, 8443 } accept', n)
        self.assertNotIn('iifname "enp3s0.20" tcp', n)

    def test_commissioning_flag(self):
        self.assertNotIn('iifname "enp4s0" tcp dport', render.nftables(cfg()))
        c = copy.deepcopy(BASE)
        c["wan"]["commissioning"] = True
        self.assertIn('iifname "enp4s0" tcp dport { 22, 8443 } accept', render.nftables(config.validate(c)))

    def test_timecode_toggles_off_by_default(self):
        n = render.nftables(cfg())
        self.assertEqual(n.count("udp dport 6454"), 1)  # only VLAN 20 enabled it
        self.assertNotIn("5004", n)

    def test_networkd_lan_never_routes(self):
        files = render.networkd(cfg())
        for name in ("30-avantp-enp3s0.10.network", "30-avantp-enp3s0.20.network", "30-avantp-enp3s0.30.network"):
            self.assertNotIn("\nGateway=", files[name])
            self.assertNotIn("\nDNS=", files[name])
        d = files["30-avantp-enp3s0.30.network"]
        for key in ("UseRoutes=no", "UseGateway=no", "UseDNS=no", "UseNTP=no"):
            self.assertIn(key, d)

    def test_chrony(self):
        c = render.chrony(cfg())
        self.assertIn("pool pool.ntp.org iburst prefer", c)
        self.assertIn("server time.cloudflare.com iburst", c)
        self.assertIn("allow all", c)  # VLAN 30 is DHCP
        static = copy.deepcopy(BASE)
        static["vlans"] = static["vlans"][:2]
        c = render.chrony(config.validate(static))
        self.assertIn("allow 10.10.10.0/24", c)
        self.assertNotIn("allow all", c)

    def test_sysctl_forwarding_off(self):
        s = render.sysctl(cfg())
        self.assertIn("net.ipv4.ip_forward=0", s)
        self.assertIn("net.ipv6.conf.all.forwarding=0", s)


if __name__ == "__main__":
    unittest.main()
