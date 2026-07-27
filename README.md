# sonos-ssdp-proxy

A ~130-line Python SSDP proxy that makes **Sonos speakers discoverable across VLANs** — with **zero firewall openings** from the IoT VLAN toward your trusted VLAN, no source spoofing, and no elevated privileges.

It exists because of one finding, proven at the packet level:

> **Sonos Era 300 (firmware 95.x) never answers a single mDNS query of any kind.**
> Its actual discovery mechanism is **SSDP**: a multicast `M-SEARCH` with
> `ST: urn:schemas-upnp-org:device:ZonePlayer:1`, answered instantly by unicast.
> Because UniFi (and Avahi) reflect **mDNS only** — never SSDP — **no mDNS reflector
> configuration can ever make cross-VLAN discovery work.**

If you've enabled mDNS reflection, followed every WLAN best-practice, and still get **"No products found"** from a controller on a different VLAN than the speakers — this is almost certainly why, and this is the fix.

---

## The problem

Typical segmented setup: phone/controller on a Trusted VLAN, speakers on an IoT VLAN, mDNS reflection enabled, a Trusted → IoT firewall allow in place. Textbook. And yet:

- Phone on the **same** VLAN as the speakers → discovers them **instantly**.
- Phone on the Trusted VLAN, reflection + firewall in place → **"No products found"**, always.

## The evidence

Captured on the router bridges and inside a dual-homed container, probing the speakers with every discovery flavor:

| Probe (sent to the speakers on their own VLAN) | Response |
|:---|:---|
| mDNS PTR `_sonos._tcp.local`, QM, multicast | ❌ nothing |
| mDNS PTR, **QU** (unicast-response bit), multicast, proper `:5353` source | ❌ nothing |
| mDNS query, unicast direct to speaker `:5353` | ❌ nothing |
| SSDP `M-SEARCH`, **unicast** to speaker `:1900` | ❌ nothing |
| **SSDP `M-SEARCH`, multicast** to `239.255.255.250:1900`, `ST: ...:ZonePlayer:1` | ✅ **instant unicast `HTTP/1.1 200 OK` + `LOCATION`, every time** |

Additional notes from the captures:

- The speakers' **mDNS is announce-only**: a burst at boot, then silence. They send their own known-answer queries looking for household peers, but they **never answer anyone else's queries** — not the phone's, not a reflector's, not `avahi-browse`'s.
- They emit periodic **SSDP `NOTIFY`** presence broadcasts.
- The current iOS app's own discovery traffic on the wire is **multicast `M-SEARCH` with `ST: ZonePlayer`** (plus mDNS queries the speakers ignore). So the common claim that the post-2024 app "dropped SSDP and is mDNS-only" does **not** match what this app/firmware combination actually does on the wire.

## Why no reflector can fix it

1. **UniFi's reflector (`ubntmdnsd`) handles mDNS only** — there is no SSDP relay anywhere in UniFi OS. The reflector was verified working (it reflected `_smb`, `_home-assistant`, `_device-info` fine, and it reflected the Sonos *queries* into the speaker VLAN too). The speakers simply never answer them.
2. **Avahi was falsified too**: an Avahi reflector with legs in both VLANs re-originated the phone's query onto the speaker VLAN within ~0.2 ms (both sides captured). Speakers: silence. Not UniFi's fault, not Avahi's fault — the speakers do not answer mDNS queries, full stop.
3. **Bonus subtlety:** mDNS reflectors rewrite QU → QM when re-originating (by design). So even a device that only answered QU queries would break behind any reflector. Moot here, since these speakers answer neither.

The one protocol that made same-VLAN discovery work all along was SSDP — which is link-local multicast and never crosses a VLAN without help.

## How this proxy fixes it

Run it on a small container/VM/host with **one interface in each VLAN**:

1. Listen for multicast `M-SEARCH` on the Trusted-VLAN leg; **filter to `ST: ZonePlayer`** (this also stops Trusted-VLAN clients from enumerating all your other IoT SSDP devices through the proxy).
2. Re-issue the `M-SEARCH` as multicast on the IoT leg.
3. Collect the speakers' unicast `200 OK` replies for ~3 s and forward each back to the original querier **from the Trusted-side leg** (same-VLAN unicast — the firewall never sees it).
4. Also re-announce the speakers' SSDP `NOTIFY`s onto the Trusted VLAN (rate-limited), so rediscovery after idle works passively.

The app reads the `LOCATION: http://<speaker>:1400/...` header from the reply and connects there directly — which only needs the ordinary Trusted → IoT **forward** allow you already have.

> **Why no firewall holes / no privileges:** SSDP clients follow the `LOCATION` header, **not** the reply's source IP. So the proxy needs no source spoofing, no `NET_RAW`, and no inbound (IoT → Trusted) firewall rule.

**Eventing surprise:** the classic UPnP GENA callback path (speaker → controller, TCP 1400/3400/3401) turned out to be unnecessary. With a scoped IoT → Trusted rule for it *disabled* mid-playback, a volume change at the speaker still updated the app slider live — the current app maintains its own connection *to* the speakers over the forward path. **Net firewall delta for fully-working cross-VLAN Sonos: zero.**

---

## Requirements

- Python 3 (standard library only — no dependencies).
- A host with a NIC (or tagged sub-interface) in **both** the controller VLAN and the speaker VLAN, each with a **static IP**.
- Your usual Trusted → IoT forward allow so the app can reach the speakers on `:1400`.
- Multicast permitted within each VLAN (default). No IoT → Trusted rule needed.

## Configure

Edit the two IPs at the top of `ssdp-proxy.py` to your proxy host's addresses:

```python
TRUSTED_IP = "192.168.10.53"   # this host's leg in the controller/phone VLAN
IOT_IP     = "192.168.30.65"   # this host's leg in the speaker VLAN
```

The subnet prefixes used to classify incoming packets are derived from these two
values (assuming `/24`). If your speaker/controller VLANs are not `/24`, adjust
`TRUSTED_PREFIX` / `IOT_PREFIX`.

## Install (systemd)

```bash
git clone https://github.com/bernhard276/sonos-ssdp-proxy.git
cd sonos-ssdp-proxy
# edit the two IPs at the top of ssdp-proxy.py first (see Configure above)
sudo cp ssdp-proxy.py /opt/ssdp-proxy.py
sudo cp ssdp-proxy.service /etc/systemd/system/ssdp-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable --now ssdp-proxy.service
journalctl -u ssdp-proxy -f
```

Open the Sonos app from the Trusted VLAN — the speakers should appear immediately. The log prints one line per relayed search (`M-SEARCH from <ip> -> N replies forwarded`).

## Handy debugging one-liners

- Identify a speaker (model/room/firmware), unauthenticated:
  `curl -s http://<speaker>:1400/xml/device_description.xml`
- Reveal stereo-pair bonding (`ChannelMapSet`, incl. the "invisible" satellite): a `GetZoneGroupState` SOAP call to `/ZoneGroupTopology/Control`.
- Confirm what your app actually searches for (capture the real `ST:` string before trusting any filter):
  `tcpdump -ni <iface> -A 'udp port 1900'`
- **Gotcha:** "works on the same VLAN" tells you nothing about *which protocol* works — same-L2 unicast replies (speaker → phone) are invisible to a capture on the router or any third host. Enumerate discovery flavors explicitly: `{mDNS, SSDP} × {multicast, unicast} × {QM, QU}`. The answer lived in the one cell few people test (multicast `M-SEARCH`), because unicast `M-SEARCH` — the easy test — is *also* ignored.

## Security notes

- **ST filter** (`ST_ALLOW = b"zoneplayer"`): only Sonos searches are relayed, so Trusted-VLAN clients can't enumerate your other IoT SSDP devices through the proxy.
- **Concurrency cap** (`MAX_RELAYS`): bounds in-flight relays; excess `M-SEARCH` is dropped (the app retries).
- **No privileges:** no `NET_RAW`, no source spoofing, no inbound IoT → Trusted rule. The systemd unit adds `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`.

## Scope / caveats

- Verified against **Sonos Era 300, firmware 95.1-78010**, current iOS app (2026), UniFi Cloud Gateway. Behavior on other models/firmware is genuinely unknown — see the open questions below; reports welcome.
- Assumes `/24` speaker/controller VLANs (adjust the prefixes otherwise) and a host that can hold a static leg in each VLAN.
- This bridges **discovery** only; actual media/control traffic uses your existing Trusted → IoT forward path.

## Open questions (reports welcome via Issues)

1. Is the mDNS-query silence specific to Era 300 / fw 95.x, or do Era 100, Move 2, Arc Ultra, older Play:x on S2 also refuse to answer `_sonos._tcp` PTR queries? (`dns-sd -B _sonos._tcp .` on macOS, or `avahi-browse -rt _sonos._tcp` — on the *same* VLAN as the speakers.)
2. Has anyone captured the current app discovering via mDNS **responses** from speakers (not just boot announcements)? Many working "mDNS-reflection-only" cross-VLAN setups are reported; I can't reconcile that with these captures.
3. For anyone running `multicast-relay` / `udp-proxy-2020` for Sonos: is it the **SSDP** relaying (not the mDNS part) that's actually doing the work? The evidence here suggests yes.

## License

MIT — see [LICENSE](LICENSE).

## Author

Bernhard Schneider ([@bernhard276](https://github.com/bernhard276)). Contributions and capture reports welcome — please open an [issue](https://github.com/bernhard276/sonos-ssdp-proxy/issues).
