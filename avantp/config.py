"""Load and validate the appliance config. Everything that ends up in a generated file passes through here."""
import ipaddress
import json
import re
import zoneinfo

IFACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,14}$")
TZ_RE = re.compile(r"^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+){0,2}$")
# a VLAN name is a display label ("Dante Prim", "MA-NET", "Über (1)"): letters in any language, digits, space, - _ . ( )
NAME_RE = re.compile(r"^\w[\w .()\-\u2010-\u2015]{0,31}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")
HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
MAX_VLANS = 20
FRAME_RATES = (24, 25, 29.97, 30)


class ConfigError(ValueError):
    pass


def _need(cond, msg):
    if not cond:
        raise ConfigError(msg)


def _host(h, where):
    _need(isinstance(h, str) and HOST_RE.match(h), f"{where}: invalid host {h!r}")
    return h


def _net(cidr, where):
    if not isinstance(cidr, str):
        raise ConfigError(f"{where}: an address with prefix length is required, e.g. 10.1.1.24/24")
    cidr = cidr.strip()
    # "10.1.1.24" would silently become a /32 and every later check would blame the gateway: ask for the prefix
    if "/" not in cidr:
        raise ConfigError(f"{where}: {cidr!r} needs a prefix length, e.g. {cidr or '10.1.1.24'}/24")
    try:
        return ipaddress.ip_interface(cidr)
    except ValueError:
        raise ConfigError(f"{where}: invalid address {cidr!r} (example: 10.1.1.24/24)")


def validate(cfg):
    """Return a normalised copy of cfg or raise ConfigError."""
    _need(isinstance(cfg, dict), "config must be an object")
    wan = cfg.get("wan", {})
    lan = cfg.get("lan", {})
    _need(IFACE_RE.match(wan.get("iface", "")), "wan.iface invalid")
    _need(IFACE_RE.match(lan.get("iface", "")), "lan.iface invalid")
    _need(wan["iface"] != lan["iface"], "wan.iface and lan.iface must differ")

    wan.setdefault("mode", "dhcp")
    # temporary SSH + WebUI on the WAN side while no management VLAN exists yet; must be switched off afterwards
    wan.setdefault("commissioning", False)
    _need(isinstance(wan["commissioning"], bool), "wan.commissioning must be true or false")
    _need(wan["mode"] in ("dhcp", "static"), "wan.mode must be dhcp or static")
    wan_net = None
    if wan["mode"] == "static":
        wan_net = _net(wan.get("address"), "wan.address")
        _need(wan_net.version == 4, "wan.address must be IPv4")
        try:
            gw = ipaddress.ip_address(str(wan.get("gateway", "")).strip())
        except ValueError:
            raise ConfigError(f"wan.gateway: {wan.get('gateway')!r} is not an IP address")
        _need(gw in wan_net.network, f"wan.gateway {gw} is not inside the WAN network {wan_net.network} (from {wan_net})")
        wan["dns"] = [str(ipaddress.ip_address(d)) for d in wan.get("dns", [])]

    up = cfg.setdefault("upstream", {})
    for k in ("default", "backup", "custom"):
        up.setdefault(k, [])
        _need(isinstance(up[k], list), f"upstream.{k} must be a list")
        up[k] = [_host(h, f"upstream.{k}") for h in up[k]]
    _need(up["default"] or up["backup"] or up["custom"], "at least one upstream NTP source required")
    up.setdefault("local_fallback", False)

    vlans = cfg.setdefault("vlans", [])
    _need(len(vlans) <= MAX_VLANS, f"at most {MAX_VLANS} VLANs")
    ids, names, nets = set(), set(), []
    for v in vlans:
        vid = v.get("id")
        _need(isinstance(vid, int) and 1 <= vid <= 4094, f"vlan id {vid!r} out of range")
        _need(vid not in ids, f"duplicate vlan id {vid}")
        ids.add(vid)
        name = v.get("name")
        _need(isinstance(name, str) and not CONTROL_RE.search(name), f"vlan {vid}: the name must be plain text")
        name = v["name"] = " ".join(name.split())          # trim, collapse repeated spaces
        _need(NAME_RE.match(name), f"vlan {vid}: invalid name {name!r} (1-32 characters: letters, digits, space, - _ . ( ))")
        _need(name.casefold() not in names, f"duplicate vlan name {name!r}")
        names.add(name.casefold())
        v.setdefault("mode", "dhcp")
        _need(v["mode"] in ("dhcp", "static"), f"vlan {vid}: mode must be dhcp or static")
        if v["mode"] == "static":
            ifc = _net(v.get("address"), f"vlan {vid}.address")
            _need(ifc.version == 4, f"vlan {vid}: IPv4 only")
            _need(ifc.network.prefixlen <= 30, f"vlan {vid}: prefix too long")
            _need(ifc.ip not in (ifc.network.network_address, ifc.network.broadcast_address),
                  f"vlan {vid}: address is network/broadcast address")
            for other_id, other in nets:
                _need(not ifc.network.overlaps(other), f"vlan {vid} subnet overlaps vlan {other_id}")
            if wan_net:
                _need(not ifc.network.overlaps(wan_net.network), f"vlan {vid} subnet overlaps WAN")
            nets.append((vid, ifc.network))
            v["_if"] = ifc
        v.pop("utc_offset_min", None)  # replaced by a named time zone, which follows daylight saving
        tz = v.setdefault("timezone", "UTC")
        _need(isinstance(tz, str) and len(tz) <= 40 and TZ_RE.match(tz), f"vlan {vid}: invalid timezone {tz!r}")
        try:
            zoneinfo.ZoneInfo(tz)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            raise ConfigError(f"vlan {vid}: unknown timezone {tz!r}")
        v.setdefault("ntp", True)
        v.setdefault("mgmt", False)
        tc = v.setdefault("timecode", {})
        tc.setdefault("artnet", False)
        tc.setdefault("mtc_rtp", False)
        tc.setdefault("fps", 25)
        _need(tc["fps"] in FRAME_RATES, f"vlan {vid}: timecode.fps must be one of {FRAME_RATES}")
        # Art-Net goes out as a broadcast on the VLAN, or as unicast to one receiver on that VLAN
        dest = tc.setdefault("artnet_dest", "broadcast")
        _need(isinstance(dest, str), f"vlan {vid}: timecode.artnet_dest must be 'broadcast' or an IPv4 address")
        dest = tc["artnet_dest"] = "broadcast" if dest.strip().lower() in ("", "broadcast") else dest.strip()
        if dest != "broadcast":
            try:
                ip = ipaddress.ip_address(dest)
            except ValueError:
                raise ConfigError(f"vlan {vid}: Art-Net destination {dest!r} is not an IPv4 address (or 'broadcast')")
            _need(ip.version == 4 and not (ip.is_multicast or ip.is_loopback or ip.is_unspecified or ip.is_link_local
                                           or ip.is_reserved), f"vlan {vid}: Art-Net destination {dest} must be a normal unicast IPv4 address")
            if wan_net and ip in wan_net.network:
                raise ConfigError(f"vlan {vid}: Art-Net destination {dest} is on the WAN network {wan_net.network}: the VLANs "
                                  f"never send to the WAN side. Choose a receiver on this VLAN.")
            if v["mode"] == "static":              # the LAN side has no gateway: the receiver has to be on this VLAN
                net = v["_if"].network
                _need(ip in net, f"vlan {vid}: Art-Net destination {dest} is not on this VLAN's network {net}")
                _need(ip not in (v["_if"].ip, net.network_address, net.broadcast_address),
                      f"vlan {vid}: Art-Net destination {dest} is this box or the network/broadcast address; use 'broadcast' for that")
        v["iface"] = f"{lan['iface']}.{vid}"
    _need(wan["commissioning"] or any(v["mgmt"] for v in vlans),
          "at least one VLAN must have mgmt=true (WebUI/SSH access), or enable wan.commissioning")
    web = cfg.setdefault("webui", {})
    web.setdefault("port", 8443)
    _need(web["port"] == 8443, "webui.port is fixed at 8443")
    cfg["wan"], cfg["lan"] = wan, lan
    return cfg


def load(path):
    with open(path) as f:
        return validate(json.load(f))


def dump(cfg):
    """JSON-safe copy of a validated config (drops computed fields)."""
    c = json.loads(json.dumps(cfg, default=lambda o: None))
    for v in c["vlans"]:
        v.pop("_if", None)
        v.pop("iface", None)
    return c
