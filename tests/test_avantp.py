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

    def test_vlan_names_are_human_labels(self):
        for ok in ("Dante Prim", "Dante Sec", "Audio Control", "MA\u2013NET", "MA-NET", "sACN", "VideoStreaming",
                   "\u00dcber (1)", "A", "x" * 32, "  spaced   out  "):
            c = copy.deepcopy(BASE)
            c["vlans"][0]["name"] = ok
            v = config.validate(c)["vlans"][0]
            self.assertEqual(v["name"], " ".join(ok.split()))        # trimmed + collapsed
        for bad in ("", "   ", "x" * 33, "a#b", 'a"b', "a;b", "<script>", "a\nb", "a\tb", "a\x00b", "a\u2028b", "-lead", None, 5):
            c = copy.deepcopy(BASE)
            c["vlans"][0]["name"] = bad
            self.bad(c)

    def test_name_error_says_what_is_allowed(self):
        c = copy.deepcopy(BASE)
        c["vlans"][0]["name"] = "a#b"
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(c)
        self.assertIn("letters, digits, space", str(cm.exception))
        self.assertIn("vlan 10", str(cm.exception))

    def test_names_unique_ignoring_case(self):
        c = copy.deepcopy(BASE)
        c["vlans"][0]["name"], c["vlans"][1]["name"] = "Dante Prim", "dante  PRIM"
        self.bad(c)

    def test_rendered_files_survive_odd_names(self):
        c = copy.deepcopy(BASE)
        c["vlans"][0]["name"] = "Dante Prim (A\u2013B)"
        v = config.validate(c)
        self.assertIn("# VLAN 10 Dante Prim (A\u2013B)", render.nftables(v))
        for name, body in render.networkd(v).items():
            self.assertNotIn("Dante", body)                          # names never reach the network files

    def test_same_nic_rejected(self):
        c = copy.deepcopy(BASE)
        c["lan"]["iface"] = "enp4s0"
        self.bad(c)

    def test_address_needs_a_prefix_and_the_error_says_so(self):
        c = copy.deepcopy(BASE)
        c["wan"] = {"iface": "enp4s0", "mode": "static", "address": "10.1.1.24", "gateway": "10.1.1.254"}
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(c)
        self.assertIn("needs a prefix length", str(cm.exception))
        self.assertIn("10.1.1.24/24", str(cm.exception))
        self.assertNotIn("gateway", str(cm.exception))            # not the misleading complaint about the gateway
        c["wan"]["address"] = "10.1.1.24/24"
        self.assertEqual(config.validate(c)["wan"]["address"], "10.1.1.24/24")
        c["wan"]["address"] = "10.1.1.24/255.255.255.0"            # netmask notation works too
        config.validate(c)
        c["wan"]["address"] = " 10.1.1.24/24 "
        config.validate(c)
        for bad in ("", "10.1.1.24/33", "10.1.1.999/24", "abc", "10.1.1.24/", None, 24):
            c["wan"]["address"] = bad
            self.bad(c)
        c["wan"]["address"], c["wan"]["gateway"] = "10.1.1.24/24", "10.9.9.9"
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(c)
        self.assertIn("10.9.9.9 is not inside the WAN network 10.1.1.0/24", str(cm.exception))
        c["wan"]["gateway"] = "gw"
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(c)
        self.assertIn("not an IP address", str(cm.exception))

    def test_static_vlan_address_needs_a_prefix_too(self):
        c = copy.deepcopy(BASE)
        c["vlans"][0]["address"] = "10.10.10.2"
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(c)
        self.assertIn("needs a prefix length", str(cm.exception))

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
        self.assertIn('iifname "enp3s0.10" ip daddr 10.10.10.2 tcp dport { 22, 8443 } accept', n)
        self.assertIn('iifname "enp3s0.20" ip daddr 10.20.0.2 udp dport 123 accept', n)
        self.assertNotIn('iifname "enp3s0.20" tcp', n)

    def test_commissioning_flag(self):
        self.assertNotIn('iifname "enp4s0" tcp dport', render.nftables(cfg()))
        c = copy.deepcopy(BASE)
        c["wan"]["commissioning"] = True
        self.assertIn('iifname "enp4s0" tcp dport { 22, 8443 } accept', render.nftables(config.validate(c)))

    def test_artnet_destination_validation(self):
        def with_dest(dest, vlan=1):
            c = copy.deepcopy(BASE)
            c["vlans"][vlan]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": dest}
            return c
        # VLAN 20 is 10.20.0.2/24 (static)
        self.assertEqual(config.validate(with_dest("broadcast"))["vlans"][1]["timecode"]["artnet_dest"], "broadcast")
        self.assertEqual(config.validate(with_dest(" BROADCAST "))["vlans"][1]["timecode"]["artnet_dest"], "broadcast")
        self.assertEqual(config.validate(with_dest(""))["vlans"][1]["timecode"]["artnet_dest"], "broadcast")
        self.assertEqual(config.validate(with_dest(" 10.20.0.50 "))["vlans"][1]["timecode"]["artnet_dest"], "10.20.0.50")
        for bad, why in (("10.20.1.5", "not on this VLAN"), ("8.8.8.8", "not on this VLAN"), ("10.20.0.2", "this box"),
                         ("10.20.0.0", "this box or the network/broadcast"), ("10.20.0.255", "this box or the network/broadcast"),
                         ("224.0.0.1", "unicast"), ("127.0.0.1", "unicast"), ("0.0.0.0", "unicast"), ("169.254.1.1", "unicast"),
                         ("255.255.255.255", "unicast"), ("::1", "unicast"), ("host.example.org", "not an IPv4"),
                         ("10.20.0.5; reboot", "not an IPv4"), ("10.20.0", "not an IPv4"), (5, "must be")):
            with self.assertRaises(config.ConfigError, msg=repr(bad)) as cm:
                config.validate(with_dest(bad))
            self.assertIn(why, str(cm.exception), repr(bad))
        # a DHCP VLAN cannot be checked against a subnet yet (the sender does it at run time), but must still be a plain unicast IP
        self.assertEqual(config.validate(with_dest("10.99.1.1", vlan=2))["vlans"][2]["timecode"]["artnet_dest"], "10.99.1.1")
        with self.assertRaises(config.ConfigError):
            config.validate(with_dest("239.1.1.1", vlan=2))

    def test_unicast_receiver_on_a_static_wan_network_is_refused(self):
        c = copy.deepcopy(BASE)
        c["wan"] = {"iface": "enp4s0", "mode": "static", "address": "10.77.0.24/24", "gateway": "10.77.0.254"}
        c["vlans"][2]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": "10.77.0.50"}   # VLAN 30, DHCP
        with self.assertRaises(config.ConfigError) as cm:
            config.validate(c)
        self.assertIn("on the WAN network 10.77.0.0/24", str(cm.exception))
        self.assertIn("never send to the WAN", str(cm.exception))
        c["vlans"][2]["timecode"]["artnet_dest"] = "10.3.5.5"
        config.validate(c)

    def test_unicast_destination_is_pinned_in_the_firewall(self):
        c = copy.deepcopy(BASE)
        c["vlans"][1]["timecode"] = {"artnet": True, "mtc_rtp": False, "fps": 25, "artnet_dest": "10.20.0.50"}
        n = render.nftables(config.validate(c))
        self.assertIn('oifname "enp3s0.20" ip daddr 10.20.0.50 udp dport 6454 accept', n)
        self.assertNotIn('oifname "enp3s0.20" udp dport 6454 accept', n)                # no broadcast allowance left
        c["vlans"][1]["timecode"]["artnet_dest"] = "broadcast"
        self.assertIn('oifname "enp3s0.20" udp dport 6454 accept', render.nftables(config.validate(c)))

    def test_timecode_toggles_off_by_default(self):
        n = render.nftables(cfg())
        self.assertEqual(n.count("udp dport 6454"), 1)  # only VLAN 20 enabled it
        self.assertNotIn("5004", n)

    def test_wan_dhcp_identifies_itself_by_mac(self):
        wan = render.networkd(cfg())["10-avantp-wan.network"]
        self.assertIn("ClientIdentifier=mac", wan)          # the router recognises the box by MAC, so it keeps its address
        self.assertNotIn("ClientIdentifier", render.networkd(cfg())["30-avantp-enp3s0.30.network"])

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



class NodeRed(unittest.TestCase):
    """Node-RED on the box: its page on management VLANs, and HTTPS out for its account only."""

    def setUp(self):
        self._uid = config._uid
        config._uid = lambda u: 1000 if u == "node" else None

    def tearDown(self):
        config._uid = self._uid

    def nr(self, **edit):
        c = copy.deepcopy(BASE)
        c["nodered"] = {"ui": True, "palette": True}
        c["nodered"].update(edit)
        c["vlans"][2]["mgmt"] = False
        return c

    def bad(self, c, text=""):
        with self.assertRaises(config.ConfigError) as e:
            config.validate(c)
        self.assertIn(text, str(e.exception))

    def test_off_by_default_opens_nothing(self):
        n = render.nftables(cfg())
        self.assertEqual(cfg()["nodered"], {"ui": False, "palette": False, "user": "node"})
        self.assertNotIn("1880", n)
        self.assertNotIn("skuid", n)

    def test_page_only_on_management_vlans(self):
        n = render.nftables(config.validate(self.nr()))
        self.assertIn('iifname "enp3s0.10" ip daddr 10.10.10.2 tcp dport 1880 accept', n)   # the mgmt VLAN, pinned to its address
        self.assertEqual(n.count("tcp dport 1880"), 1)                                      # VLAN 20 and 30 have mgmt off
        self.assertNotIn('iifname "enp4s0" tcp dport 1880', n)                              # never from the WAN side

    def test_page_follows_every_mgmt_vlan(self):
        c = self.nr()
        c["vlans"][1]["mgmt"] = True
        n = render.nftables(config.validate(c))
        self.assertIn('iifname "enp3s0.20" ip daddr 10.20.0.2 tcp dport 1880 accept', n)
        self.assertEqual(n.count("tcp dport 1880"), 2)

    def test_dhcp_mgmt_vlan_has_no_address_pin(self):
        c = self.nr()
        c["vlans"][2]["mgmt"] = True
        self.assertIn('iifname "enp3s0.30" tcp dport 1880 accept', render.nftables(config.validate(c)))

    def test_palette_is_https_out_for_one_account_on_the_wan(self):
        n = render.nftables(config.validate(self.nr()))
        self.assertIn('oifname "enp4s0" meta skuid 1000 tcp dport 443 accept', n)
        self.assertEqual(n.count("tcp dport 443"), 1)           # the update window's "{ 80, 443 }" line is separate
        self.assertEqual(n.count("skuid"), 1)
        self.assertIn("hook output priority 0; policy drop;", n)

    def test_palette_off_leaves_update_mode_as_the_only_way(self):
        n = render.nftables(config.validate(self.nr(palette=False)))
        self.assertNotIn("skuid", n)
        self.assertIn("oifname @update_window tcp dport { 80, 443 } accept", n)

    def test_palette_needs_an_existing_account(self):
        self.bad(self.nr(user="nobody-such"), "no account")
        config.validate(self.nr(user="nobody-such", palette=False))   # only checked when the rule is wanted

    def test_account_name_is_checked_before_it_reaches_a_rule(self):
        for bad in ("node; drop", "No De", "../x", "", 5, "a" * 40):
            self.bad(self.nr(user=bad))

    def test_switches_must_be_real_booleans(self):
        self.bad(self.nr(ui="no"), "true or false")
        self.bad(self.nr(palette=1), "true or false")

    def test_existing_config_without_the_section_loads(self):
        c = copy.deepcopy(BASE)
        c.pop("nodered", None)
        self.assertFalse(config.validate(c)["nodered"]["ui"])

    def test_survives_dump_and_reload(self):
        c = config.validate(self.nr())
        self.assertEqual(config.validate(config.dump(c))["nodered"], c["nodered"])

if __name__ == "__main__":
    unittest.main()
