#!/usr/bin/env python3
"""
mcscan - find Minecraft servers by pinging random public IPv4 addresses.

For each random address it does a cheap TCP connect on the Minecraft port, and
if that succeeds it speaks the real Server List Ping protocol (the same thing
your client does on the multiplayer screen) to confirm it is actually Minecraft
and to pull the MOTD, version and player counts.

Reserved / private / bogon ranges are skipped. Results are appended to a JSONL
file so a run can be stopped and resumed at any time.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import random
import re
import struct
import sys
import time
from bisect import bisect_right

__version__ = "1.0.0"

# ---------------------------------------------------------------- address pool

# Blocks that must never be probed: private, loopback, doc/test nets, CGNAT,
# multicast, and the reserved 240/4 space.
_RESERVED_CIDRS = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.31.196.0/24", "192.52.193.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "192.175.48.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4",
]


def _build_excluded():
    spans = []
    for cidr in _RESERVED_CIDRS:
        net = ipaddress.ip_network(cidr)
        spans.append([int(net.network_address), int(net.broadcast_address)])
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [s for s, _ in merged], [e for _, e in merged]


_EXC_STARTS, _EXC_ENDS = _build_excluded()


def is_scannable(n):
    """True if the 32-bit int is a routable public unicast address."""
    i = bisect_right(_EXC_STARTS, n) - 1
    return i < 0 or n > _EXC_ENDS[i]


def random_ip(rng):
    while True:
        n = rng.getrandbits(32)
        if is_scannable(n):
            return "%d.%d.%d.%d" % (n >> 24, (n >> 16) & 255, (n >> 8) & 255, n & 255)


# ------------------------------------------------------------ minecraft protocol

class ProtocolError(Exception):
    pass


def varint(value):
    if value < 0:
        value += 1 << 32
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def pack_string(s):
    raw = s.encode("utf-8")
    return varint(len(raw)) + raw


def framed(payload):
    return varint(len(payload)) + payload


async def read_varint(reader):
    num = 0
    for i in range(5):
        b = await reader.readexactly(1)
        num |= (b[0] & 0x7F) << (7 * i)
        if not b[0] & 0x80:
            return num
    raise ProtocolError("varint too long")


MAX_STATUS_BYTES = 2 * 1024 * 1024  # cap so a hostile host cannot balloon memory


async def server_list_ping(host, port, reader, writer, protocol):
    """Modern (1.7+) handshake, status request, JSON response. Returns (dict, latency_ms)."""
    handshake = (
        b"\x00" + varint(protocol) + pack_string(host)
        + struct.pack(">H", port) + varint(1)
    )
    writer.write(framed(handshake) + framed(b"\x00"))
    await writer.drain()

    length = await read_varint(reader)
    if length <= 0 or length > MAX_STATUS_BYTES:
        raise ProtocolError("bad packet length %d" % length)
    packet = await reader.readexactly(length)
    if packet[0] != 0x00:
        raise ProtocolError("unexpected packet id 0x%02x" % packet[0])

    # The body is a length-prefixed UTF-8 string; pull its varint out by hand.
    body = packet[1:]
    n, shift, idx = 0, 0, 0
    while True:
        b = body[idx]
        n |= (b & 0x7F) << shift
        idx += 1
        shift += 7
        if not b & 0x80:
            break
    status = json.loads(body[idx:idx + n].decode("utf-8", "replace"))

    latency = None
    try:
        token = random.getrandbits(63)
        t0 = time.perf_counter()
        writer.write(framed(b"\x01" + struct.pack(">q", token)))
        await writer.drain()
        plen = await read_varint(reader)
        await reader.readexactly(plen)
        latency = round((time.perf_counter() - t0) * 1000, 1)
    except Exception:
        pass
    return status, latency


async def legacy_ping(reader, writer):
    """Pre-1.7 ping (0xFE 0x01) for old servers. Returns a status-shaped dict."""
    writer.write(b"\xfe\x01")
    await writer.drain()
    head = await reader.readexactly(3)
    if head[0] != 0xFF:
        raise ProtocolError("not a legacy response")
    nchars = struct.unpack(">H", head[1:3])[0]
    text = (await reader.readexactly(nchars * 2)).decode("utf-16-be", "replace")

    def num(parts, i):
        return int(parts[i]) if len(parts) > i and parts[i].lstrip("-").isdigit() else -1

    if text.startswith("§1"):
        parts = text.split("\x00")
        return {
            "version": {"name": parts[2] if len(parts) > 2 else "legacy",
                        "protocol": num(parts, 1)},
            "description": parts[3] if len(parts) > 3 else "",
            "players": {"online": num(parts, 4), "max": num(parts, 5)},
            "_legacy": True,
        }
    parts = text.split("§")
    return {
        "version": {"name": "legacy", "protocol": -1},
        "description": parts[0],
        "players": {"online": num(parts, 1), "max": num(parts, 2)},
        "_legacy": True,
    }


# ----------------------------------------------------------------- status shape

_COLOR = re.compile("§.")


def flatten_motd(node):
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_motd(n) for n in node)
    if isinstance(node, dict):
        s = node.get("text", "")
        if not s and "translate" in node:
            s = str(node["translate"])
        for extra in node.get("extra", []) or []:
            s += flatten_motd(extra)
        return s
    return str(node)


def clean_motd(node):
    text = _COLOR.sub("", flatten_motd(node))
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


# ------------------------------------------------------- what you need to join

# Server software that is server-side only: a plain vanilla client can join.
_PLUGIN_SERVERS = ("paper", "purpur", "folia", "spigot", "craftbukkit", "bukkit",
                   "pufferfish", "leaves", "leaf")
_PROXIES = ("velocity", "bungeecord", "waterfall", "travertine", "zenithproxy")
# Server software that runs mods and therefore usually needs them client-side.
_MODDED_SERVERS = ("neoforge", "forge", "fabric", "quilt", "magma", "mohist",
                   "arclight", "sponge", "catserver", "ketting")


def _unpack_forge_blob(text):
    """Undo Forge's packed ping encoding: 15 data bits per UTF-16 char.

    Big modpacks blow past the ping size limit, so FML network version 3 leaves
    `mods` and `channels` empty and packs everything into a `d` string instead.
    The first two chars carry the payload length.
    """
    if len(text) < 3:
        return b""
    size = (ord(text[0]) & 0x7FFF) | ((ord(text[1]) & 0x7FFF) << 15)
    size = min(size, 1 << 20)
    out = bytearray()
    buf = bits = 0
    for char in text[2:]:
        if bits >= 8:
            out.append(buf & 0xFF)
            buf >>= 8
            bits -= 8
        buf |= (ord(char) & 0x7FFF) << bits
        bits += 15
    while len(out) < size and bits > 0:
        out.append(buf & 0xFF)
        buf >>= 8
        bits -= 8
    return bytes(out[:size])


class _Buf:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def byte(self):
        if self.pos >= len(self.data):
            raise ProtocolError("forge blob truncated")
        value = self.data[self.pos]
        self.pos += 1
        return value

    def varint(self):
        num = shift = 0
        for _ in range(5):
            b = self.byte()
            num |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                return num
        raise ProtocolError("forge varint too long")

    def ushort(self):
        return (self.byte() << 8) | self.byte()

    def utf(self):
        length = self.varint()
        if length < 0 or self.pos + length > len(self.data):
            raise ProtocolError("forge string overruns blob")
        raw = self.data[self.pos:self.pos + length]
        self.pos += length
        return raw.decode("utf-8", "replace")


def parse_forge_blob(text):
    """Mod ids out of a packed `d` payload. Best effort: returns [] if it does
    not decode, since the exact layout varies between FML versions."""
    try:
        buf = _Buf(_unpack_forge_blob(text))
        truncated = bool(buf.byte())
        count = buf.ushort()
        if count > 4000:
            return [], False
        mods = []
        for _ in range(count):
            flags = buf.varint()
            channels = flags >> 1
            ignore_server_only = flags & 1
            mod_id = buf.utf()
            if not ignore_server_only:
                buf.utf()                       # mod version
            for _ in range(channels):           # per-mod channels
                buf.utf()
                buf.utf()
                buf.byte()
            if mod_id:
                mods.append(mod_id)
        return mods, truncated
    except Exception:
        return [], False


def detect_platform(status):
    """Pull the mod loader and mod list out of a status response.

    Forge advertises itself in the ping: modern versions send `forgeData` with a
    mod list, and 1.12-and-older send `modinfo` with type FML. Everything else we
    infer from the free-text version string.
    """
    version = str((status.get("version") or {}).get("name", "") or "")
    low = version.lower()
    loader, mods, truncated = None, [], False

    forge = status.get("forgeData")
    modinfo = status.get("modinfo")

    if isinstance(forge, dict):
        for mod in forge.get("mods") or []:
            if isinstance(mod, dict) and mod.get("modId"):
                mods.append(str(mod["modId"]))
        truncated = bool(forge.get("truncated"))
        if not mods and isinstance(forge.get("d"), str):
            mods, packed_truncated = parse_forge_blob(forge["d"])
            truncated = truncated or packed_truncated
        channels = forge.get("channels") or []
        haystack = (low + " " + json.dumps(channels)[:2000] + " "
                    + " ".join(mods[:400])).lower()
        loader = "NeoForge" if "neoforge" in haystack else "Forge"
    elif isinstance(modinfo, dict) and str(modinfo.get("type", "")).upper() == "FML":
        loader = "Forge"
        for mod in modinfo.get("modList") or []:
            if isinstance(mod, dict) and mod.get("modid"):
                mods.append(str(mod["modid"]))
    else:
        for name in _MODDED_SERVERS:
            if name in low:
                loader = {"neoforge": "NeoForge", "forge": "Forge", "fabric": "Fabric",
                          "quilt": "Quilt"}.get(name, name.capitalize())
                break

    software = None
    for name in _PLUGIN_SERVERS + _PROXIES:
        if name in low:
            software = name.capitalize()
            break

    # "forge" inside a modded-server name like Magma should not read as plain Forge.
    return {
        "loader": loader,
        "software": software,
        "mods": mods[:40],
        "mod_count": len(mods) if mods else None,
        "mods_truncated": truncated,
    }


_VERSION_NUM = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def version_family(text):
    """'Paper 1.20.4' -> '1.20.x'. Returns None when there is no version in it.

    Server version strings are free text, so this pulls the first number pair out
    and buckets by it -- which is the grouping a player actually cares about,
    since a 1.20.1 client and a 1.20.4 server are different problems.
    """
    match = _VERSION_NUM.search(str(text or ""))
    if not match:
        return None
    return "%s.%s.x" % (match.group(1), match.group(2))


# Seeds for the version picker so common choices are there before you have found
# one. Anything actually discovered gets merged in on top of this, which is how
# newer version numbering shows up without a code change.
KNOWN_VERSIONS = [
    "1.21.11", "1.21.10", "1.21.9", "1.21.8", "1.21.7", "1.21.6", "1.21.5",
    "1.21.4", "1.21.3", "1.21.2", "1.21.1", "1.21",
    "1.20.6", "1.20.4", "1.20.2", "1.20.1",
    "1.19.4", "1.19.2", "1.18.2", "1.17.1", "1.16.5", "1.16.1", "1.15.2",
    "1.14.4", "1.12.2", "1.8.9", "1.7.10",
]


def version_tuple(text):
    """'Paper 1.21.11' -> (1, 21, 11). Empty tuple when there is no version."""
    match = _VERSION_NUM.search(str(text or ""))
    if not match:
        return ()
    parts = [match.group(1), match.group(2), match.group(3)]
    return tuple(int(p) for p in parts if p is not None)


def version_matches(wanted, server_version):
    """Does a server match a version filter?

    The filter is treated as a prefix, so '1.21' covers 1.21, 1.21.1 and 1.21.11
    while '1.21.11' matches only that one. '1.21.x' means the same as '1.21'.
    """
    wanted = str(wanted or "").strip().lower()
    if not wanted:
        return True
    if wanted.endswith(".x"):
        wanted = wanted[:-2]
    want = version_tuple(wanted)
    if not want:
        # Not a version number at all -- fall back to a plain text match so
        # things like 'Paper' or 'ZenithProxy' still work if typed in.
        return wanted in str(server_version or "").lower()
    have = version_tuple(server_version)
    return have[:len(want)] == want


def family_sort_key(family):
    parts = str(family).replace(".x", "").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return (0,)


def loader_of(hit):
    """The mod loader for a hit, re-deriving it for records saved before
    detection existed. None means 'no loader' only when the record was scanned
    with detection; otherwise it is genuinely unknown."""
    if hit.get("loader"):
        return hit["loader"]
    if "loader" in hit:
        return None
    return detect_platform({"version": {"name": hit.get("version", "")}})["loader"]


def requirements(hit):
    """One line describing what the player needs on their side.

    Returns (text, level) where level is 'need' (client-side install required),
    'maybe' (probably fine), or 'ok' (vanilla client is enough).
    """
    version = str(hit.get("version") or "?")
    # Records saved before mod detection existed carry only the version string.
    # We can still read the software name off it, but absence of a mod list in
    # such a record proves nothing -- so never claim vanilla on that basis.
    scanned_for_mods = "loader" in hit
    if not scanned_for_mods:
        hit = dict(hit, **detect_platform({"version": {"name": version}}))
    loader = hit.get("loader")
    count = hit.get("mod_count")

    if loader in ("Forge", "NeoForge"):
        text = "Needs the %s client" % loader
        if count:
            text += " and its %d mod%s" % (count, "" if count == 1 else "s")
            if hit.get("mods_truncated"):
                text += "+"
        elif hit.get("legacy"):
            text += " (mod list not sent)"
        return text, "need"

    if loader in ("Fabric", "Quilt"):
        return "%s server - a vanilla client usually works" % loader, "maybe"

    if loader:
        return "Modded server (%s) - expect to need its modpack" % loader, "need"

    software = hit.get("software")
    if software and software.lower() in _PROXIES:
        return "Proxy (%s) - the real server is behind it" % software, "maybe"
    if software:
        return "%s - plugins only, vanilla client works" % software, "ok"

    if not scanned_for_mods:
        return "Mod info not recorded - rescan this one to check", "unknown"

    # Scanned and it advertised nothing. Forge and 1.20.1-era NeoForge always
    # announce themselves in the ping, so this really is a plain server -- but
    # say what we actually observed rather than promising vanilla.
    if version and version[0].isdigit():
        return "No mods advertised - a %s client should work" % version, "ok"
    return "No mods advertised - vanilla client should work", "ok"


def summarize(ip, port, status, latency):
    version = status.get("version") or {}
    players = status.get("players") or {}
    sample = [p.get("name") for p in (players.get("sample") or []) if isinstance(p, dict)]
    platform = detect_platform(status)
    return {
        "ip": ip,
        "port": port,
        "found_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": str(version.get("name", "?"))[:64],
        "protocol": version.get("protocol"),
        "online": players.get("online"),
        "max": players.get("max"),
        "motd": clean_motd(status.get("description"))[:200],
        "latency_ms": latency,
        "players_sample": sample[:12],
        "has_icon": bool(status.get("favicon")),
        "enforces_secure_chat": status.get("enforcesSecureChat"),
        "legacy": status.get("_legacy", False),
        "loader": platform["loader"],
        "software": platform["software"],
        "mod_count": platform["mod_count"],
        "mods": platform["mods"],
        "mods_truncated": platform["mods_truncated"],
    }


# ------------------------------------------------------------------- the scanner

class Scanner:
    def __init__(self, args, on_hit=None, quiet=False):
        self.args = args
        self.on_hit = on_hit          # called with each hit dict instead of printing
        self.quiet = quiet            # suppress the console banner and status line
        self.abort = False            # set from another thread to stop the scan
        self.rng = random.Random(args.seed)
        self.stop = asyncio.Event()
        self.tried = 0
        self.open_ports = 0
        self.hits = 0
        self.started = time.time()
        self.out = None
        self.seen = set()

    def budget_spent(self):
        a = self.args
        if self.abort:
            return True
        if a.limit and self.hits >= a.limit:
            return True
        if a.duration and time.time() - self.started >= a.duration:
            return True
        if a.max_ips and self.tried >= a.max_ips:
            return True
        return False

    async def probe(self, ip, port):
        self.tried += 1
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), self.args.timeout)
        except (OSError, asyncio.TimeoutError):
            return None
        self.open_ports += 1
        try:
            try:
                status, latency = await asyncio.wait_for(
                    server_list_ping(ip, port, reader, writer, self.args.protocol),
                    self.args.timeout)
            except Exception:
                if not self.args.legacy:
                    return None
                await self._close(writer)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), self.args.timeout)
                status = await asyncio.wait_for(legacy_ping(reader, writer), self.args.timeout)
                latency = None
            return summarize(ip, port, status, latency)
        except Exception:
            return None
        finally:
            await self._close(writer)

    @staticmethod
    async def _close(writer):
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    def record(self, hit):
        key = (hit["ip"], hit["port"])
        if key in self.seen:
            return
        self.seen.add(key)
        self.hits += 1
        if self.out:
            self.out.write(json.dumps(hit) + "\n")
            self.out.flush()
        if self.on_hit:
            self.on_hit(hit)
            return
        players = "%s/%s" % (hit["online"], hit["max"])
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        need, _level = requirements(hit)
        print("[HIT] %-21s %-22s %9s  %s" % (
            "%s:%d" % (hit["ip"], hit["port"]), hit["version"][:22],
            players, hit["motd"][:52]), flush=True)
        print("      %s" % need, flush=True)

    async def worker(self):
        while not self.stop.is_set() and not self.budget_spent():
            ip = random_ip(self.rng)
            for port in self.args.ports:
                if self.abort or self.stop.is_set():
                    return
                hit = await self.probe(ip, port)
                if hit:
                    self.record(hit)

    async def ticker(self):
        while not self.stop.is_set():
            await asyncio.sleep(1.0)
            if self.quiet:
                continue
            elapsed = max(time.time() - self.started, 1e-9)
            sys.stderr.write(
                "\r\033[K%s probed  |  %.0f ip/s  |  %s open  |  %s servers  |  %.0fs" % (
                    format(self.tried, ","), self.tried / elapsed,
                    format(self.open_ports, ","), format(self.hits, ","), elapsed))
            sys.stderr.flush()

    def load_existing(self):
        if not (self.args.out and os.path.exists(self.args.out)) or self.args.overwrite:
            return
        with open(self.args.out, "r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    self.seen.add((rec["ip"], rec["port"]))
                except Exception:
                    pass

    async def run(self):
        self.load_existing()
        if self.args.out:
            self.out = open(self.args.out, "w" if self.args.overwrite else "a",
                            encoding="utf-8")

        # A dead address burns the full timeout, so throughput is essentially
        # concurrency / timeout. Raise -c or lower -t to go faster.
        rate = self.args.concurrency / max(self.args.timeout, 0.01)
        if not self.quiet:
            print("scanning random public IPv4 on port(s) %s with %d workers, %ss timeout" % (
                ",".join(str(p) for p in self.args.ports),
                self.args.concurrency, self.args.timeout))
            print("~%.0f addresses/sec, so roughly one server every %.0f min "
                  "(about 1 in 150,000 addresses runs Minecraft)" % (rate, 150000 / rate / 60))
            if self.seen:
                print("loaded %s servers already in %s" % (
                    format(len(self.seen), ","), self.args.out))
            print("ctrl-c to stop\n")

        self.started = time.time()
        tick = asyncio.create_task(self.ticker())
        workers = [asyncio.create_task(self.worker()) for _ in range(self.args.concurrency)]
        try:
            await asyncio.gather(*workers)
        finally:
            self.stop.set()
            tick.cancel()
            for w in workers:
                w.cancel()
            await asyncio.gather(tick, *workers, return_exceptions=True)
            if self.out:
                self.out.close()

    def summary(self):
        elapsed = max(time.time() - self.started, 1e-9)
        sys.stderr.write("\r\033[K")
        sys.stderr.flush()
        print("\n--- done in %.1fs ---" % elapsed)
        print("addresses probed : %s (%.0f/s)" % (format(self.tried, ","), self.tried / elapsed))
        print("ports open       : %s" % format(self.open_ports, ","))
        print("minecraft servers: %s" % format(self.hits, ","))
        if self.args.out:
            print("saved to         : %s" % self.args.out)


# ------------------------------------------------------------------------- cli

async def ping_host(host, port, timeout, protocol):
    """One status ping, no scanning machinery. Returns a hit dict or None."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        status, latency = await asyncio.wait_for(
            server_list_ping(host, port, reader, writer, protocol), timeout)
        return summarize(host, port, status, latency)
    except Exception:
        return None
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def recheck_file(args):
    """Re-ping everything already in the results file and refresh each record.

    Older records predate mod detection, so their requirement line can only say
    'unknown'. One pass over the file fixes that without rescanning the internet.
    """
    path = args.out
    if not os.path.exists(path):
        print("nothing to recheck: %s does not exist" % path)
        return 1
    with open(path, "r", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    print("rechecking %d saved server%s in %s\n" % (
        len(records), "" if len(records) == 1 else "s", path))

    sem = asyncio.Semaphore(max(args.concurrency // 40, 8))

    async def one(record):
        async with sem:
            return record, await ping_host(record["ip"], record["port"],
                                           args.timeout, args.protocol)

    updated = gone = 0
    fresh = []
    for coro in asyncio.as_completed([one(r) for r in records]):
        old, new = await coro
        if new is None:
            old["offline_at_recheck"] = True
            fresh.append(old)
            gone += 1
            print("  %-22s offline now" % ("%s:%d" % (old["ip"], old["port"])))
            continue
        new["found_at"] = old.get("found_at", new["found_at"])
        fresh.append(new)
        updated += 1
        need, _level = requirements(new)
        print("  %-22s %-16s %s" % ("%s:%d" % (new["ip"], new["port"]),
                                    new["version"][:16], need))

    order = {(r["ip"], r["port"]): i for i, r in enumerate(records)}
    fresh.sort(key=lambda r: order.get((r["ip"], r["port"]), 0))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for record in fresh:
            fh.write(json.dumps(record) + "\n")
    os.replace(tmp, path)
    print("\n%d refreshed, %d no longer answering - %s rewritten" % (updated, gone, path))
    return 0


async def check_one(args, target):
    host, _, p = target.rpartition(":")
    if not host:
        host, port = target, args.ports[0]
    else:
        port = int(p)
    scanner = Scanner(args)
    hit = await scanner.probe(host, port)
    if not hit:
        print("%s:%d - no Minecraft server responded" % (host, port))
        return 1
    print(json.dumps(hit, indent=2))
    return 0


def parse_ports(text):
    ports = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            lo, hi = chunk.split("-", 1)
            ports.extend(range(int(lo), int(hi) + 1))
        elif chunk:
            ports.append(int(chunk))
    return ports or [25565]


def main():
    ap = argparse.ArgumentParser(
        description="Find Minecraft servers by pinging random public IPv4 addresses.")
    ap.add_argument("--version", action="version", version="mcscan " + __version__)
    ap.add_argument("--check", metavar="HOST[:PORT]",
                    help="ping one specific host instead of scanning (good for testing)")
    ap.add_argument("--recheck", action="store_true",
                    help="re-ping the servers already in the results file and "
                         "refresh them (fills in mod info for older records)")
    ap.add_argument("-c", "--concurrency", type=int, default=400,
                    help="simultaneous probes in flight (default: 400; higher is "
                         "faster but can swamp a home router - see README)")
    ap.add_argument("-t", "--timeout", type=float, default=2.0,
                    help="per-step timeout in seconds (default: 2)")
    ap.add_argument("-p", "--ports", type=parse_ports, default=[25565],
                    help="port list/range, e.g. 25565 or 25565-25567 (default: 25565)")
    ap.add_argument("-o", "--out", default="servers.jsonl",
                    help="JSONL results file, appended and resumed (default: servers.jsonl)")
    ap.add_argument("--overwrite", action="store_true", help="truncate the results file first")
    ap.add_argument("--limit", type=int, default=0, help="stop after N servers found")
    ap.add_argument("--duration", type=float, default=0, help="stop after N seconds")
    ap.add_argument("--max-ips", type=int, default=0, help="stop after N addresses probed")
    ap.add_argument("--protocol", type=int, default=767,
                    help="handshake protocol version (default: 767 / 1.21)")
    ap.add_argument("--legacy", action="store_true",
                    help="also try the pre-1.7 ping when the modern one fails")
    ap.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    args = ap.parse_args()

    if args.check:
        sys.exit(asyncio.run(check_one(args, args.check)))

    if args.recheck:
        sys.exit(asyncio.run(recheck_file(args)))

    scanner = Scanner(args)
    try:
        asyncio.run(scanner.run())
    except KeyboardInterrupt:
        pass
    scanner.summary()


if __name__ == "__main__":
    main()
