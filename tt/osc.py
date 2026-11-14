"""DiGiCo console remote control over OSC ("DiGiCo Pad" external control). Standard library only.

Nothing in here sends anything by itself. The app builds a plan (a list of writes), shows it to
the operator, and only then hands it to `execute`. Every plan is read back from the console
first, so there is always a record to undo with and proof that the console is really listening.

Addresses follow the DiGiCo iPad command set as documented by the Bitfocus Companion module.
"""
import ipaddress
import os
import socket
import struct
import time

OUT_TYPES = {"Group": "Group_Outputs", "Aux": "Aux_Outputs", "Matrix": "Matrix_Outputs"}
F_MIN, F_MAX = 20.0, 20000.0          # Hz
G_MAX = 18.0                          # dB
Q_MIN, Q_MAX = 0.1, 20.0
DELAY_MAX = 1.3                       # seconds
BELL = 1.0                            # eq_curve_n value for a bell
CURVES = {1: "Bell", 2: "curve 2", 3: "Low shelf", 4: "High shelf"}
# One message per 100 ms is the only rate with field evidence of being safe on a DiGiCo desk.
PACE = 0.1
TIMEOUT = 0.5
_last_sent = [0.0]                    # when the last message left for a real console


# --------------------------------------------------------------------------- codec
def _pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def encode(addr, args=()):
    """OSC message. Numbers go out as float32, which is what the console expects."""
    tags = ","
    body = b""
    for a in args:
        if isinstance(a, str):
            tags += "s"
            body += _pad(a.encode("utf-8"))
        elif isinstance(a, bool) or isinstance(a, (int, float)):
            tags += "f"
            body += struct.pack(">f", float(a))
        else:
            raise TypeError("Unsupported OSC argument: %r" % (a,))
    return _pad(addr.encode("ascii")) + _pad(tags.encode("ascii")) + body


def _read_str(p, i):
    end = p.index(b"\0", i)
    return p[i:end].decode("utf-8", "replace"), (end + 4) & ~3


def decode(packet):
    """-> list of (address, [args]). Understands bundles; ignores what it cannot parse."""
    out = []
    try:
        if packet[:8] == b"#bundle\0":
            i = 16
            while i + 4 <= len(packet):
                (n,) = struct.unpack(">I", packet[i:i + 4])
                i += 4
                if n <= 0 or i + n > len(packet):
                    break
                out += decode(packet[i:i + n])
                i += n
            return out
        addr, i = _read_str(packet, 0)
        if not addr.startswith("/"):
            return out
        if i >= len(packet):
            return [(addr, [])]
        tags, i = _read_str(packet, i)
        args = []
        for t in tags.lstrip(","):
            if t == "f":
                args.append(struct.unpack(">f", packet[i:i + 4])[0])
                i += 4
            elif t == "i":
                args.append(struct.unpack(">i", packet[i:i + 4])[0])
                i += 4
            elif t == "d":
                args.append(struct.unpack(">d", packet[i:i + 8])[0])
                i += 8
            elif t == "h":
                args.append(struct.unpack(">q", packet[i:i + 8])[0])
                i += 8
            elif t in ("s", "S"):
                s, i = _read_str(packet, i)
                args.append(s)
            elif t == "T":
                args.append(1.0)
            elif t == "F":
                args.append(0.0)
            elif t == "b":
                (n,) = struct.unpack(">I", packet[i:i + 4])
                i += 4 + ((n + 3) & ~3)
            elif t in ("N", "I"):
                args.append(None)
            else:
                break
        out.append((addr, args))
    except (ValueError, struct.error, IndexError):
        pass
    return out


# ------------------------------------------------------------------- stand-in console
class SimConsole:
    """In-memory console that answers the same addresses. Used for the demo, practice and tests."""

    def __init__(self, name="Simulated Quantum 326", bands=8):
        self.name = name
        self.bands = bands
        self.v = {}
        self.log = []                 # every message received, in order: (addr, args)
        self.on_change = None

    def _default(self, addr):
        p = addr.strip("/").split("/")
        if p[0] == "Console":
            return {"Name": self.name, "Channels": 128.0, "Group_Outputs": 64.0, "Aux_Outputs": 64.0,
                    "Matrix_Outputs": 24.0}.get("/".join(p[1:]))
        if len(p) != 4 or p[0] not in OUT_TYPES.values() or not p[1].isdigit():
            return None
        sec, par = p[2], p[3]
        if sec == "EQ":
            if par == "eq_in":
                return 1.0
            name, _, n = par.rpartition("_")
            if not n.isdigit() or not 1 <= int(n) <= self.bands:
                return None
            n = int(n)
            if name == "eq_on":
                return 1.0
            if name == "eq_gain":
                return 0.0
            if name == "eq_freq":
                return (8000.0, 2000.0, 500.0, 100.0, 12000.0, 4000.0, 1000.0, 250.0)[n - 1]
            if name == "eq_Q":
                return 0.71
            if name == "eq_curve":      # outer bands sit on shelves until someone changes them
                return {1: 4.0, 4: 3.0}.get(n, 1.0)
            if name == "dynamic_eq_on":
                return 0.0
            return None
        if sec == "Channel_Delay":
            return {"delay": 0.0, "delay_on": 0.0}.get(par)
        if sec == "Buss_Trim":
            return {"phase": 0.0}.get(par)
        return None

    def get(self, addr):
        return self.v[addr] if addr in self.v else self._default(addr)

    def handle(self, addr, args):
        """Apply one incoming message; return the replies the console would send."""
        self.log.append((addr, list(args)))
        if addr.endswith("/?"):
            key = addr[:-2]
            val = self.get(key)
            return [] if val is None else [(key, [val])]
        if not args or self._default(addr) is None or addr.startswith("/Console/"):
            return []
        self.v[addr] = float(args[0]) if not isinstance(args[0], str) else args[0]
        if self.on_change:
            self.on_change()
        return []

    def output(self, typ, n):
        """Processing state of one output, for the demo PA to follow."""
        base = "/%s/%d/" % (OUT_TYPES[typ], int(n))
        g = lambda k: self.get(base + k)
        eq = []
        if g("EQ/eq_in") >= 0.5:
            for b in range(1, self.bands + 1):
                if g("EQ/eq_on_%d" % b) >= 0.5 and abs(g("EQ/eq_gain_%d" % b)) > 1e-6:
                    eq.append((g("EQ/eq_freq_%d" % b), g("EQ/eq_gain_%d" % b), g("EQ/eq_Q_%d" % b),
                               int(round(g("EQ/eq_curve_%d" % b)))))
        return {"eq": eq, "delay": g("Channel_Delay/delay") if g("Channel_Delay/delay_on") >= 0.5 else 0.0,
                "inv": g("Buss_Trim/phase") >= 0.5}


# ------------------------------------------------------------------------------ links
class SimLink:
    pace = 0.0
    label = "simulated console"

    def __init__(self, console):
        self.console = console
        self.inbox = []

    def open(self):
        self.inbox = []

    def close(self):
        pass

    def send(self, addr, args=()):
        self.inbox += self.console.handle(addr, [a for m in decode(encode(addr, args)) for a in m[1]])

    def recv(self, timeout):
        return [self.inbox.pop(0)] if self.inbox else []


class UdpLink:
    pace = PACE

    def __init__(self, host, send_port, listen_port):
        self.host, self.send_port, self.listen_port = host, int(send_port), int(listen_port)
        self.label = "%s:%d" % (host, self.send_port)
        self.sock = None

    def open(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if os.name != "nt":          # on Windows this option would let two programs share the port
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", self.listen_port))
        except OSError as exc:
            s.close()
            raise OscError("Could not listen on port %d (%s). Another console app may be using it."
                           % (self.listen_port, exc.strerror or exc))
        self.sock = s

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def send(self, addr, args=()):
        try:
            self.sock.sendto(encode(addr, args), (self.host, self.send_port))
        except OSError as exc:
            raise OscError("Could not send to %s (%s). Check the network connection." % (self.label, exc.strerror or exc))

    def recv(self, timeout):
        self.sock.settimeout(max(timeout, 0.001))
        try:
            data, src = self.sock.recvfrom(65535)
        except (socket.timeout, BlockingIOError):
            return []
        except OSError:
            return []
        if src[0] != self.host:          # only the console we were pointed at may answer
            return []
        return decode(data)


class OscError(Exception):
    pass


def check_host(host):
    """The console lives on the local network. Refuse anything else."""
    try:
        ip = ipaddress.ip_address(str(host).strip())
    except ValueError:
        raise OscError("Enter the console's IP address, for example 192.168.1.10.")
    if ip.version != 4 or ip.is_multicast or ip.is_unspecified or not (ip.is_private or ip.is_loopback or ip.is_link_local):
        raise OscError("That is not a local network address. Use the console's Local IP Address from its External Control page.")
    return str(ip)


def check_port(p):
    p = int(p)
    if not 1024 <= p <= 65535:
        raise OscError("Ports must be between 1024 and 65535.")
    return p


# ----------------------------------------------------------------------------- client
class Client:
    """One conversation with the console: paced sends, one query at a time."""

    def __init__(self, link, progress=None):
        self.link = link
        self.sent = 0
        self.progress = progress

    def __enter__(self):
        self.link.open()
        return self

    def __exit__(self, *exc):
        self.link.close()

    def _send(self, addr, args=()):
        if self.link.pace:               # the gap is kept across conversations too, not just inside one
            wait = _last_sent[0] + self.link.pace - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self.link.send(addr, args)
        if self.link.pace:
            _last_sent[0] = time.monotonic()
        self.sent += 1
        if self.progress:
            self.progress(self.sent)

    def write(self, addr, value):
        self._send(addr, [value])

    def query(self, addr, tries=2):
        """Ask for the current value. None when the console does not answer."""
        for _ in range(tries):
            self._send(addr + "/?")
            end = time.monotonic() + (TIMEOUT if self.link.pace else 0.0)
            while True:
                msgs = self.link.recv(end - time.monotonic())
                for a, args in msgs:
                    if a == addr and args:
                        return args[0]
                if not msgs and time.monotonic() >= end:
                    break
        return None


# ------------------------------------------------------------------------------ plans
def base(typ, n):
    if typ not in OUT_TYPES:
        raise OscError("Choose Group, Aux or Matrix as the output type.")
    n = int(n)
    if not 1 <= n <= 128:
        raise OscError("Output number must be between 1 and 128.")
    return "/%s/%d/" % (OUT_TYPES[typ], n)


def clamp(v, lo, hi):
    return min(max(float(v), lo), hi)


def _same(kind, a, b):
    if a is None or b is None or isinstance(a, str) or isinstance(b, str):
        return False
    tol = {"freq": max(1.0, 0.012 * abs(b)), "gain": 0.15, "q": max(0.02, 0.05 * abs(b)), "delay": 0.00006}.get(kind, 0.01)
    return abs(float(a) - float(b)) <= tol


def _fmt(kind, v):
    if v is None:
        return "no answer"
    if isinstance(v, str):
        return v
    if kind == "freq":
        return "%.0f Hz" % v if v >= 100 else "%.1f Hz" % v
    if kind == "gain":
        return "%+.1f dB" % v
    if kind == "q":
        return "%.2f" % v
    if kind == "delay":
        return "%.2f ms" % (1000.0 * v)
    if kind == "curve":
        return CURVES.get(int(round(v)), "curve %d" % round(v))
    if kind == "pol":
        return "inverted" if v >= 0.5 else "normal"
    return "on" if v >= 0.5 else "off"


def _op(addr, kind, label, value, now):
    return {"addr": addr, "kind": kind, "label": label, "value": value, "now": now,
            "now_txt": _fmt(kind, now), "new_txt": _fmt(kind, value), "skip": _same(kind, now, value)}


def probe(cli):
    """Proof of life. Returns the console's name, or raises."""
    name = cli.query("/Console/Name")
    if name is None:
        if cli.query("/Console/Channels") is None:
            raise OscError("The console did not answer. Check its IP address and ports, that External Control is "
                           "enabled with a DiGiCo Pad device pointing at this computer, and that the command file is loaded.")
        name = "DiGiCo console"
    return str(name)


def plan_eq(cli, typ, n, bands):
    """Read the output EQ, then lay out the writes that put `bands` (bells) on it."""
    b0 = base(typ, n) + "EQ/"
    want = sorted(bands, key=lambda b: -b["f"])       # band 1 is the top band on the console
    warnings = []
    avail = 4
    if len(want) > 4:
        avail = 8 if cli.query(b0 + "eq_gain_5", tries=1) is not None else 4
        if avail < len(want):
            return {"ok": False, "refit": 4,
                    "msg": "The console only answered for EQ bands 1 to 4 on this output, and this fit uses %d. "
                           "Re-fit with 4 bands, then send." % len(want)}
    if len(want) > avail:
        return {"ok": False, "refit": avail, "msg": "This fit uses %d bands; the output EQ has %d." % (len(want), avail)}
    ops = []
    eq_in = cli.query(b0 + "eq_in")
    if eq_in is None:
        raise OscError("The console answered, but not for %s %d. Check the output type and number." % (typ, int(n)))
    replaced = []
    for i, b in enumerate(want):
        k = i + 1
        f, g, q = clamp(b["f"], F_MIN, F_MAX), clamp(b["gain"], -G_MAX, G_MAX), clamp(b["q"], Q_MIN, Q_MAX)
        f = round(f, 0 if f >= 100 else 1)
        g, q = round(g, 1), round(q, 2)
        cur = {p: cli.query(b0 + "%s_%d" % (p, k)) for p in ("eq_freq", "eq_gain", "eq_Q", "eq_curve", "eq_on")}
        if cur["eq_freq"] is None or cur["eq_gain"] is None or cur["eq_Q"] is None:
            raise OscError("The console did not answer for EQ band %d on %s %d, so nothing was prepared." % (k, typ, int(n)))
        dyn = cli.query(b0 + "dynamic_eq_on_%d" % k, tries=1)
        if dyn is not None and dyn >= 0.5:
            warnings.append("Band %d is in dynamic mode on the console, so it will only act above its threshold. "
                            "Switch it to static on the console." % k)
        tag = "Band %d " % k
        live = abs(cur["eq_gain"]) > 0.05 and (cur["eq_on"] is None or cur["eq_on"] >= 0.5)
        moves = not (_same("freq", cur["eq_freq"], f) and _same("q", cur["eq_Q"], q))
        if live:
            replaced.append("band %d (%s at %s)" % (k, _fmt("gain", cur["eq_gain"]), _fmt("freq", cur["eq_freq"])))
            if moves:                       # flatten first so nothing sweeps through the mix while it moves
                ops.append(_op(b0 + "eq_gain_%d" % k, "gain", tag + "gain (flatten before moving)", 0.0, cur["eq_gain"]))
        if cur["eq_curve"] is not None:
            ops.append(_op(b0 + "eq_curve_%d" % k, "curve", tag + "shape", BELL, cur["eq_curve"]))
        ops.append(_op(b0 + "eq_freq_%d" % k, "freq", tag + "frequency", f, cur["eq_freq"]))
        ops.append(_op(b0 + "eq_Q_%d" % k, "q", tag + "Q", q, cur["eq_Q"]))
        ops.append(_op(b0 + "eq_gain_%d" % k, "gain", tag + "gain", g, 0.0 if (live and moves) else cur["eq_gain"]))
        if cur["eq_on"] is not None:
            ops.append(_op(b0 + "eq_on_%d" % k, "sw", tag + "on", 1.0, cur["eq_on"]))
    ops.append(_op(b0 + "eq_in", "sw", "Output EQ in", 1.0, eq_in))
    others = []
    for k in range(len(want) + 1, avail + 1):
        g = cli.query(b0 + "eq_gain_%d" % k, tries=1)
        on = cli.query(b0 + "eq_on_%d" % k, tries=1)
        if g is not None and abs(g) > 0.05 and (on is None or on >= 0.5):
            others.append("band %d (%s)" % (k, _fmt("gain", g)))
    if replaced:
        warnings.append("This output already has EQ on %s. It will be replaced. The fit assumes the output EQ was "
                        "flat when you measured; if it was not, the result will differ from the prediction." % ", ".join(replaced))
    if others:
        warnings.append("Not touched: %s stays as it is on the console and is not part of the prediction." % ", ".join(others))
    return {"ok": True, "ops": ops, "warnings": warnings, "target": "%s %d" % (typ, int(n))}


def plan_delay(cli, steps):
    """steps: [{typ, n, name, add_s, flip}] -> writes that add delay and/or flip polarity."""
    ops, warnings = [], []
    for s in steps:
        b0 = base(s["typ"], s["n"])
        who = "%s (%s %d) " % (s["name"], s["typ"], int(s["n"]))
        if s.get("add_s"):
            cur = cli.query(b0 + "Channel_Delay/delay")
            on = cli.query(b0 + "Channel_Delay/delay_on")
            if cur is None or on is None:
                raise OscError("The console did not answer for the delay on %s %d, so nothing was prepared." % (s["typ"], int(s["n"])))
            have = cur if on >= 0.5 else 0.0
            new = have + float(s["add_s"])
            if new > DELAY_MAX:
                raise OscError("That needs %.0f ms of delay on %s %d, more than the console's %.0f ms."
                               % (1000 * new, s["typ"], int(s["n"]), 1000 * DELAY_MAX))
            if have > 0:
                warnings.append("%salready has %.2f ms of delay. The correction is added to it." % (who, 1000 * have))
            ops.append(_op(b0 + "Channel_Delay/delay", "delay", who + "delay", round(new, 5), cur))
            ops.append(_op(b0 + "Channel_Delay/delay_on", "sw", who + "delay on", 1.0, on))
        if s.get("flip"):
            cur = cli.query(b0 + "Buss_Trim/phase")
            if cur is None:
                raise OscError("The console did not answer for the polarity on %s %d, so nothing was prepared." % (s["typ"], int(s["n"])))
            ops.append(_op(b0 + "Buss_Trim/phase", "pol", who + "polarity", 0.0 if cur >= 0.5 else 1.0, cur))
    return {"ok": True, "ops": ops, "warnings": warnings,
            "target": ", ".join("%s %d" % (s["typ"], int(s["n"])) for s in steps)}


def plan_restore(cli, saved):
    """saved: [{addr, kind, label, value}] -> writes that put those values back."""
    now = {}
    for s in saved:
        now[s["addr"]] = cli.query(s["addr"])
        if now[s["addr"]] is None:
            raise OscError("The console did not answer for %s, so nothing was prepared." % s["addr"])
    gains = [s for s in saved if s["kind"] == "gain"]
    switches = [s for s in saved if s["kind"] in ("sw", "pol")]
    rest = [s for s in saved if s["kind"] not in ("gain", "sw", "pol")]
    ops, flat = [], set()
    if rest:
        for s in gains:                 # flatten first so nothing sweeps through the mix while it moves back
            if abs(now[s["addr"]]) > 0.05 and not _same("gain", now[s["addr"]], s["value"]):
                ops.append(_op(s["addr"], "gain", s["label"] + " (flatten before moving)", 0.0, now[s["addr"]]))
                flat.add(s["addr"])
    for s in rest + gains + switches:
        ops.append(_op(s["addr"], s["kind"], s["label"], s["value"], 0.0 if s["addr"] in flat else now[s["addr"]]))
    return {"ok": True, "ops": ops, "warnings": [], "target": "previous values"}


def execute(cli, ops):
    """Send the plan, then read every written value back. Returns (written, mismatches)."""
    todo = [o for o in ops if not o["skip"]]
    for o in todo:
        cli.write(o["addr"], o["value"])
    final = {}
    for o in todo:                      # a later write to the same address wins
        final[o["addr"]] = o
    bad = []
    for o in final.values():
        got = cli.query(o["addr"])
        if not _same(o["kind"], got, o["value"]):
            bad.append({"label": o["label"], "want": o["new_txt"], "got": _fmt(o["kind"], got)})
    return len(todo), bad


def count_messages(ops):
    todo = [o for o in ops if not o["skip"]]
    return len(todo) + len({o["addr"] for o in todo})
