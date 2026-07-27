#!/usr/bin/env python3
"""Cross-VLAN SSDP proxy for Sonos discovery.

Why this exists
---------------
Sonos Era 300 (firmware 95.x) discovery is SSDP-only. The speakers answer a
multicast M-SEARCH (ST: urn:schemas-upnp-org:device:ZonePlayer:1) with a unicast
200 OK, and they IGNORE ALL mDNS queries (QM, QU, unicast, and reflected). This
means no mDNS reflector (UniFi's built-in one, or Avahi) can ever make cross-VLAN
discovery work -- verified by packet capture. See the README for the wire evidence.

SSDP is link-local multicast, so it does not cross a VLAN boundary on its own.
This proxy bridges just the SSDP discovery exchange between the controller VLAN
(where your phone/app lives) and the IoT VLAN (where the speakers live).

What it does
------------
- Relays M-SEARCH (Sonos ST only) from the Trusted leg onto the IoT leg,
  collects the speakers' unicast replies, and forwards them back to the original
  querier from the Trusted leg (same-VLAN unicast -- the firewall never sees it).
  The app then reads the LOCATION header (http://<speaker>:1400/...) and connects
  there directly over your ordinary Trusted -> IoT forward allow.
- Re-announces Sonos NOTIFY presence broadcasts from the IoT leg onto the Trusted
  leg (rate-limited), so passive rediscovery after idle keeps working.

Why it needs no firewall holes and no privileges
------------------------------------------------
SSDP clients follow the LOCATION header, not the reply's source IP, so the proxy
never spoofs a source address, never needs NET_RAW, and needs no inbound rule
from IoT toward Trusted. The only firewall rule required is the Trusted -> IoT
forward allow you already have for the speakers' :1400 control port.

Hardening
---------
- ST filter: only ZonePlayer searches are relayed, so Trusted-VLAN clients cannot
  enumerate your other IoT SSDP devices through this proxy.
- Concurrency cap: at most MAX_RELAYS in-flight relays; excess M-SEARCH is dropped
  (the app retries anyway).

Setup
-----
Run on a small container/VM/host with one NIC in each VLAN. Give it static IPs
(they are used for multicast join and source binding). Then edit the two IPs
below and run under systemd. Assumes /24 subnets; adjust *_PREFIX if not.
"""
import socket
import struct
import threading
import time

# --- EDIT THESE TWO to your proxy host's addresses --------------------------
TRUSTED_IP = "192.168.10.53"   # this host's leg in the controller/phone VLAN
IOT_IP     = "192.168.30.65"   # this host's leg in the speaker VLAN
# ----------------------------------------------------------------------------

# Derived from the IPs above so the "edit two lines" promise holds (assumes /24).
TRUSTED_PREFIX = TRUSTED_IP.rsplit(".", 1)[0] + "."   # e.g. "192.168.10."
IOT_PREFIX     = IOT_IP.rsplit(".", 1)[0] + "."       # e.g. "192.168.30."

SSDP_GRP, SSDP_PORT = "239.255.255.250", 1900
SELF_IPS = {TRUSTED_IP, IOT_IP}
ST_ALLOW = b"zoneplayer"       # case-insensitive substring of the ST header
MAX_RELAYS = 8                 # concurrent in-flight M-SEARCH relays
NOTIFY_MIN_INTERVAL = 1.0      # seconds, rate-limit for relayed NOTIFYs

_relay_slots = threading.Semaphore(MAX_RELAYS)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def st_allowed(payload: bytes) -> bool:
    for line in payload.split(b"\r\n"):
        if line[:3].upper() == b"ST:":
            return ST_ALLOW in line.lower()
    return False


def relay_msearch(payload, src):
    """Re-issue an M-SEARCH on the IoT leg, forward replies to the original querier."""
    if not _relay_slots.acquire(blocking=False):
        return  # over cap - drop silently; the app retries anyway
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((IOT_IP, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(IOT_IP))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        s.sendto(payload, (SSDP_GRP, SSDP_PORT))
        s.settimeout(0.5)
        back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        back.bind((TRUSTED_IP, 0))
        t0, n = time.time(), 0
        while time.time() - t0 < 3.0:
            try:
                d, a = s.recvfrom(4096)
            except socket.timeout:
                continue
            if a[0] in SELF_IPS:
                continue
            back.sendto(d, src)
            n += 1
        if n:
            log("M-SEARCH from %s:%s -> %d replies forwarded" % (src[0], src[1], n))
        s.close()
        back.close()
    except Exception as e:
        log("relay_msearch error:", e)
    finally:
        _relay_slots.release()


def notify_out(payload):
    """Re-announce a Sonos NOTIFY on the Trusted leg."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((TRUSTED_IP, 0))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(TRUSTED_IP))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        s.sendto(payload, (SSDP_GRP, SSDP_PORT))
        s.close()
    except Exception as e:
        log("notify_out error:", e)


def main():
    r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    r.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    r.bind(("", SSDP_PORT))
    for ifip in (TRUSTED_IP, IOT_IP):
        mreq = struct.pack("4s4s", socket.inet_aton(SSDP_GRP), socket.inet_aton(ifip))
        r.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    log("ssdp-proxy up: M-SEARCH(%s) %s->%s, Sonos NOTIFY %s->%s"
        % (ST_ALLOW.decode(), TRUSTED_IP, IOT_IP, IOT_IP, TRUSTED_IP))
    last_notify = 0.0
    while True:
        d, a = r.recvfrom(4096)
        if a[0] in SELF_IPS:
            continue
        head = d[:20].upper()
        if head.startswith(b"M-SEARCH") and a[0].startswith(TRUSTED_PREFIX) and st_allowed(d):
            threading.Thread(target=relay_msearch, args=(d, a), daemon=True).start()
        elif head.startswith(b"NOTIFY") and a[0].startswith(IOT_PREFIX) and (b"Sonos" in d or b"ZonePlayer" in d):
            now = time.time()
            if now - last_notify > NOTIFY_MIN_INTERVAL:
                last_notify = now
                notify_out(d)


if __name__ == "__main__":
    main()
