"""Application state: ties the audio engine, analyzer, EQ fitting and channel advice together."""
import json
import math
import os
import re
import threading
import time

import numpy as np

from . import __version__, advisor, align, dsp, eq, osc
from .analyzer import Analyzer
from .engine import DemoSource, Engine, Generator

AVG = {"fast": 1.0, "med": 4.0, "slow": 10.0, "inf": None}
SMOOTH = (3, 6, 12, 24, 48)
HOLD_S = 8.0                     # seconds a channel finding stays listed after it was last seen
APP_NAME = "Mixx Trace"
SESSION_DIR = os.path.join(os.path.expanduser("~"), ".mixxtrace")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")
OLD_SESSION_FILE = os.path.join(os.path.expanduser("~"), ".tunetrace", "session.json")   # name before 0.2
PLAN_TTL = 120.0                 # seconds a previewed console push stays valid
ALIGN_SETTLE = 3.0               # seconds of fresh audio before an alignment capture starts
ALIGN_FRAMES = 10


def arr(x, nd=1):
    """numpy array -> JSON-safe list (None where there is no data)."""
    out = []
    for v in np.asarray(x, dtype=np.float64).tolist():
        out.append(round(v, nd) if math.isfinite(v) else None)
    return out


def from_list(lst):
    return np.array([np.nan if v is None else v for v in lst], dtype=np.float64)


def db20(x):
    return 20.0 * math.log10(max(float(x), 1e-7))


def parse_cal(text):
    """Mic calibration text (freq, dB per line) -> correction on GRID."""
    pts = []
    for line in text.replace(",", " ").replace(";", " ").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            f, d = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if 1.0 <= f <= 100000.0 and abs(d) < 60:
            pts.append((f, d))
    if len(pts) < 3:
        raise ValueError("No calibration data found. Expected lines of: frequency (Hz), dB.")
    pts.sort()
    f = np.log(np.array([p[0] for p in pts]))
    d = np.array([p[1] for p in pts])
    return np.interp(np.log(dsp.GRID), f, d)


class App:
    def __init__(self, demo_only=False, persist=True, native=False):
        self.lock = threading.RLock()
        self.native = bool(native) and not demo_only     # running inside the desktop app window
        self.downloads = os.path.join(os.path.expanduser("~"), "Downloads")
        self.engine = Engine(self._on_block)
        self.gen = Generator()
        self.an = None
        self.demo_only = demo_only
        self.persist = persist and not demo_only
        self.s = {
            "device": None, "sr": 0, "channels": 0, "meas": 1, "ref": 0,
            "avg": "med", "tf_smooth": 12, "rta_smooth": 6, "sens": "normal", "console": "q326",
            "target": {"preset": "flat", "low": 0.0, "tilt": 0.0},
            "eq": {"bands": 8, "boost": 3.0, "cut": 12.0, "auto_range": True, "f_lo": 40.0, "f_hi": 16000.0,
                   "source": "auto"},
            "track": False,
            "osc": {"mode": "off", "host": "", "send_port": 7001, "listen_port": 7000,
                    "out_type": "Matrix", "out_n": 1},
            "align": {"a_name": "Mains", "a_type": "Matrix", "a_n": 1, "b_name": "Subs", "b_type": "Matrix", "b_n": 2},
        }
        self.sim = osc.SimConsole()
        self.osc_lock = threading.Lock()
        self.osc_plan = None
        self.osc_undo = None
        self.osc_info = None
        self.osc_progress = None
        self.al = {"a": None, "b": None, "result": None, "job": None, "error": None}
        self.demo_roles = {"meas": 1, "ref": 0}
        self.traces = []
        self.trace_seq = 0
        self.eq = None
        self.cal = None
        self.ch_meta = {"live": {}, "demo": {}}
        self.ver = 1
        self.started = 0.0
        self.last_poll = time.time()
        self.idle_stopped = False
        self._adv = (None, 0.0, [])
        self._held = {}
        self._devices = None
        if self.persist:
            self._load()

    # ------------------------------------------------------------- plumbing
    def _on_block(self, block):
        with self.lock:
            if self.an is not None:
                self.an.ingest(block)
                if self.al["job"]:
                    self._align_step()

    def bump(self):
        self.ver += 1
        self._save()

    def _session(self):
        return {"app": APP_NAME, "version": __version__, "settings": self.s, "traces": self.traces,
                "trace_seq": self.trace_seq, "eq": self._eq_light(), "cal": self.cal,
                "channels": self.ch_meta["live"]}

    def _eq_light(self):
        if not self.eq:
            return None
        return {k: v for k, v in self.eq.items()}

    def _save(self):
        if not self.persist:
            return
        try:
            os.makedirs(SESSION_DIR, exist_ok=True)
            tmp = SESSION_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._session(), fh)
            os.replace(tmp, SESSION_FILE)
        except OSError:
            pass

    def _load(self):
        for path in (SESSION_FILE, OLD_SESSION_FILE):
            try:
                with open(path, encoding="utf-8") as fh:
                    self._apply_session(json.load(fh), own=True)
                return
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _apply_session(self, data, own=False):
        """own: this computer's own saved session. A file brought in from elsewhere never arms the console remote."""
        if not isinstance(data, dict) or data.get("app") not in (APP_NAME, "TuneTrace"):
            raise ValueError("This is not a Mixx Trace session file.")
        if not own:
            keep = dict(self.s["osc"])
        n = len(dsp.GRID)
        st = data.get("settings") or {}
        for k, v in st.items():
            if k in ("target", "eq", "osc", "align") and isinstance(v, dict):
                self.s[k].update({kk: vv for kk, vv in v.items()
                                  if kk in self.s[k] and type(vv) is type(self.s[k][kk])})
            elif k in self.s and not isinstance(self.s[k], dict):
                self.s[k] = v
        o = self.s["osc"]
        if not own:
            o.update(mode=keep["mode"], host=keep["host"], send_port=keep["send_port"], listen_port=keep["listen_port"])
        try:
            o["host"] = osc.check_host(o["host"]) if o["host"] else ""
            o["send_port"], o["listen_port"] = osc.check_port(o["send_port"]), osc.check_port(o["listen_port"])
            if o["mode"] not in ("off", "sim", "udp") or o["out_type"] not in osc.OUT_TYPES:
                raise osc.OscError("bad")
        except (osc.OscError, ValueError, TypeError):
            o.update(mode="off", host="", send_port=7001, listen_port=7000, out_type="Matrix")
        self.traces = [t for t in data.get("traces") or []
                       if isinstance(t, dict) and len(t.get("mag", [])) == n and len(t.get("coh", [])) == n]
        self.trace_seq = max([int(data.get("trace_seq") or 0)] + [int(t.get("id", 0)) for t in self.traces])
        e = data.get("eq")
        self.eq = e if isinstance(e, dict) and len(e.get("curve", [])) == n else None
        c = data.get("cal")
        self.cal = c if isinstance(c, dict) and len(c.get("db", [])) == n else None
        ch = data.get("channels")
        if isinstance(ch, dict):
            self.ch_meta["live"] = {str(k): v for k, v in ch.items() if isinstance(v, dict)}

    # --------------------------------------------------------------- helpers
    @property
    def mode(self):
        return self.engine.mode

    def roles(self):
        return self.demo_roles if self.mode == "demo" else self.s

    def console(self):
        return advisor.CONSOLES.get(self.s["console"], advisor.CONSOLES["q326"])

    def meta(self, c):
        m = self.ch_meta["demo" if self.mode == "demo" else "live"].get(str(c))
        if m:
            return m
        if self.mode == "demo" and c < len(DemoSource.CHANNELS):
            return {"name": DemoSource.CHANNELS[c][0], "type": DemoSource.CHANNELS[c][1]}
        return {"name": "Input %d" % (c + 1), "type": "auto"}

    def _apply_to_analyzer(self):
        an = self.an
        if an is None:
            return
        r = self.roles()
        an.meas = int(min(max(r["meas"], 0), an.nch - 1))
        an.ref = int(min(max(r["ref"], 0), an.nch - 1))
        an.avg_tau = AVG.get(self.s["avg"], 4.0)
        an.sens = self.s["sens"]
        an.cal = from_list(self.cal["db"]) if self.cal else None
        an.track = bool(self.s["track"])

    def _new_analyzer(self):
        self.an = Analyzer(self.engine.sr, self.engine.nch)
        self._apply_to_analyzer()
        self.started = time.time()
        self._adv = (None, 0.0, [])
        self._held = {}

    def target(self):
        t = self.s["target"]
        return eq.target_curve(float(t["low"]), float(t["tilt"]))

    # ------------------------------------------------------------- lifecycle
    def start_demo(self):
        with self.lock:
            self.engine.stop()
            self.engine.sr, self.engine.nch = float(DemoSource.SR), len(DemoSource.CHANNELS)
            self.engine.mode = "demo"
            self._new_analyzer()
        self.engine.start_demo()
        if self.engine.demo is not None:
            self.engine.demo.position = 0
            self.engine.demo.system = "full"
            self._sync_demo()
        self.idle_stopped = False
        self.ver += 1
        return {"ok": True}

    def start_live(self, device=None, sr=None, channels=None):
        if self.demo_only:
            return {"ok": False, "msg": "Live input is only available when Mixx Trace runs on your own computer."}
        with self.lock:
            self.an = None
        ok = self.engine.start_live(device, sr or None, channels or None)
        if not ok:
            return {"ok": False, "msg": self.engine.error}
        with self.lock:
            self._new_analyzer()
        self.s["device"], self.s["sr"], self.s["channels"] = device, int(sr or 0), int(channels or 0)
        self.bump()
        return {"ok": True}

    def stop(self):
        self.engine.stop()
        self.gen.stop()
        self.ver += 1
        return {"ok": True}

    def devices(self, refresh=False):
        if self.demo_only:
            return {"inputs": [], "outputs": [], "error": None, "demo_only": True}
        if self._devices is None or (refresh and self.mode != "live" and not self.gen.cfg["on"]):
            self._devices = Engine.list_devices()
        return self._devices

    # ----------------------------------------------------------------- frame
    def _status(self):
        e = self.engine
        return {"mode": e.mode, "sr": e.sr, "nch": e.nch, "device": e.device_name, "error": e.error,
                "dropped": e.dropped, "live": Engine.live_available() and not self.demo_only,
                "demo_only": self.demo_only, "uptime": round(time.time() - self.started, 1) if self.an else 0,
                "gen": {"on": self.gen.cfg["on"], "error": self.gen.error},
                "demo_pos": (e.demo.position + 1) if (e.mode == "demo" and e.demo) else None,
                "demo_system": e.demo.system if (e.mode == "demo" and e.demo) else None}

    def _events(self, an):
        con = self.console()
        now = time.time()
        out = []
        for ev in an.events.values():
            n = advisor.notch_for_event(ev, con)
            src = sorted(ev["src"].items(), key=lambda kv: -kv[1])[:3]
            names = [{"ch": c + 1, "name": self.meta(c)["name"]} for c, _ in src]
            if names:
                where = "Cut it on %s (input %d) first, so the rest of the mix keeps its tone." % (names[0]["name"], names[0]["ch"])
            elif an.nch > 2:
                where = "Not seen on any scanned input. Cut it on the output or bus EQ feeding that speaker."
            else:
                where = "Cut it on the open mic's channel if you know which one, otherwise on the output EQ."
            out.append({"id": ev["id"], "f": round(float(ev["f"]), 1), "status": ev["status"],
                        "active": bool(ev["active"]), "level": round(ev["level"], 1),
                        "max": round(ev["max_level"], 1), "hits": ev["hits"], "age": round(now - ev["last"], 1),
                        "growing": bool(ev["active"] and ev.get("slope", 0) > 3.0),
                        "note": dsp.note_name(ev["f"]), "iso": dsp.iso_band(ev["f"]),
                        "notch": n, "src": names, "where": where})
        out.sort(key=lambda e: (not e["active"], e["status"] != "feedback", e["age"]))
        return out

    def _hold(self, c, findings, now):
        """Keep a finding on screen for a few seconds after it was last seen, so advice does not flicker."""
        held = self._held.setdefault(c, [])
        if findings and findings[0]["kind"] == "none":
            held.clear()
            return findings
        for f in findings:
            if f["kind"] == "ok":
                continue
            for h in held:
                if h["f"]["kind"] == f["kind"] and (f["f"] is None or h["f"]["f"] is None
                                                    or abs(math.log2(f["f"] / h["f"]["f"])) < 0.25):
                    h["f"], h["t"] = f, now
                    break
            else:
                held.append({"f": f, "t": now})
        held[:] = [h for h in held if now - h["t"] < HOLD_S]
        order = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
        out = sorted((h["f"] for h in held), key=lambda f: order[f["sev"]])
        return out or [f for f in findings if f["kind"] == "ok"]

    def _channels(self, an):
        key = (an.scan_ver, self.ver)
        now = time.time()
        if self._adv[0] is not None and (self._adv[0] == key or now - self._adv[1] < 1.0):
            rows = self._adv[2]
        else:
            rows = []
            con = self.console()
            order = {"crit": 0, "warn": 1, "info": 2, "ok": 3}
            for c in range(an.nch):
                m = self.meta(c)
                role = "meas" if c == an.meas else ("ref" if c == an.ref else None)
                findings, info = advisor.advise_channel(an, c, m["type"], con)
                if role:
                    findings = [f for f in findings if f["kind"] in ("clip", "hot", "low", "none")]
                findings = self._hold(c, findings, now)
                if role and not findings:
                    findings = [{"sev": "ok", "kind": "ok", "title": "Level is fine", "f": None, "fix": None,
                                 "detail": "Used for the transfer function, so channel advice is skipped here."}]
                rows.append({"i": c, "name": m["name"], "type": m["type"], "role": role, "active": info["active"],
                             "worst": min((order[f["sev"]] for f in findings), default=3),
                             "findings": [{"sev": f["sev"], "title": f["title"], "detail": f["detail"]} for f in findings],
                             "strip": advisor.build_strip(findings, con)})
            self._adv = (key, now, rows)
        t = an.written / an.sr
        out = []
        for r in rows:
            c = r["i"]
            out.append(dict(r, peak=round(db20(an.peak[c]), 1), rms=round(10 * math.log10(max(an.ms[c], 1e-14)), 1),
                            clip=bool(t - an.clip_t[c] < 3.0)))
        return out

    def _focus(self, an=None, tf=None):
        """Curve the EQ/target work on: (label, mag, coh) or None."""
        src = self.s["eq"]["source"]
        best = self.best_trace()
        if src in ("auto", "best") and best:
            return ("Best trace (%d capture%s)" % (best["n"], "" if best["n"] == 1 else "s"), best["mag"], best["coh"])
        if isinstance(src, int):
            for t in self.traces:
                if t["id"] == src:
                    return (t["name"], from_list(t["mag"]), from_list(t["coh"]))
        if an is not None and an.meas != an.ref:
            tf = tf or an.tf_snapshot(48)
            if tf["frames"] >= 4:
                return ("Live measurement", tf["mag"], tf["coh"])
        return None

    def _target_offset(self, mag, coh):
        g = dsp.GRID
        d = mag - self.target()
        ok = np.isfinite(d) & (g >= 200) & (g <= 4000)
        if coh is not None:
            ok &= np.where(np.isfinite(coh), coh, 0) >= 0.5
        return float(np.median(d[ok])) if ok.sum() > 8 else None

    def frame(self, view):
        self.last_poll = time.time()
        if self.demo_only and self.idle_stopped and self.mode == "stopped":
            self.start_demo()
        with self.lock:
            an = self.an
            out = {"status": self._status(), "ver": self.ver}
            if an is None or self.mode == "stopped":
                return out
            out["levels"] = {k: {"peak": round(db20(an.peak[c]), 1),
                                 "rms": round(10 * math.log10(max(an.ms[c], 1e-14)), 1),
                                 "clip": bool(an.written / an.sr - an.clip_t[c] < 3.0),
                                 "ch": c + 1, "name": self.meta(c)["name"]}
                             for k, c in (("meas", an.meas), ("ref", an.ref))}
            evs = self._events(an)
            act = [e for e in evs if e["active"]]
            out["alert"] = ({"f": act[0]["f"], "status": act[0]["status"], "n": len(act), "notch": act[0]["notch"],
                             "src": act[0]["src"], "growing": act[0]["growing"]} if act else None)
            rows = self._channels(an)
            out["counts"] = {"events": len(evs), "active": len(act),
                             "crit": sum(1 for r in rows if r["worst"] == 0 and r["active"]),
                             "warn": sum(1 for r in rows if r["worst"] == 1 and r["active"])}
            out["tf_ok"] = an.meas != an.ref
            if self.osc_progress:
                out["osc_busy"] = self.osc_progress
            if view == "feedback":
                r = an.rta_snapshot(int(self.s["rta_smooth"]))
                out["rta"] = {"avg": arr(r["avg"]), "peak": arr(r["peak"])}
                out["events"] = evs
                out["buildups"] = [{"f": round(b["f"], 1), "excess": round(b["excess"], 1), "iso": dsp.iso_band(b["f"]),
                                    "cut": -min(6.0, max(2.0, round(b["excess"] * 0.6 * 2) / 2))} for b in an.buildups]
            elif view in ("tune", "align"):
                tf = an.tf_snapshot(int(self.s["tf_smooth"]))
                have = tf["frames"] >= 1 and out["tf_ok"]
                out["tf"] = {"mag": arr(tf["mag"]) if have else None, "phase": arr(tf["phase"], 0) if have else None,
                             "coh": arr(tf["coh"], 2) if have else None, "quality": round(tf["quality"], 3),
                             "frames": tf["frames"]}
                dr = an.delay_result
                out["delay"] = {"samples": int(an.delay), "ms": round(1000.0 * an.delay / an.sr, 2),
                                "m": round(343.0 * an.delay / an.sr, 2), "pending": bool(an.delay_pending),
                                "track": bool(an.track), "moves": an.track_moves,
                                "result": ({k: (round(v, 2) if isinstance(v, float) else v) for k, v in dr.items()}
                                           if dr else None)}
                fc = self._focus(an, tf if have else None) if have or self.traces else None
                off = self._target_offset(fc[1], fc[2]) if fc else None
                out["target_off"] = round(off, 2) if off is not None else None
                out["focus"] = fc[0] if fc else None
                j = self.al["job"]
                out["align_job"] = ({"slot": j["slot"], "stage": j["stage"], "pct": self._align_pct(an, j)} if j else None)
            elif view == "channels":
                out["channels"] = rows
            return out

    # ---------------------------------------------------------------- traces
    def best_trace(self):
        """Coherence-weighted average of the included captures, each level-matched first."""
        use = [t for t in self.traces if t.get("in_avg", True)]
        if not use:
            return None
        g = dsp.GRID
        core = (g >= 200) & (g <= 4000)
        num = np.zeros(len(g))
        den = np.zeros(len(g))
        cnum = np.zeros(len(g))
        offs = []
        for t in use:
            mag, coh = from_list(t["mag"]), from_list(t["coh"])
            ok = np.isfinite(mag) & np.isfinite(coh)
            sel = ok & core & (coh >= 0.5)
            off = float(np.median(mag[sel])) if sel.sum() > 8 else float(np.nanmedian(mag[ok & core])) if (ok & core).any() else 0.0
            offs.append(off)
            w = np.where(ok, np.maximum(coh, 0.0) ** 2 + 1e-4, 0.0)
            num += w * np.where(ok, mag - off, 0.0)
            cnum += w * np.where(ok, coh, 0.0)
            den += w
        mag = np.where(den > 0, num / np.maximum(den, 1e-12), np.nan) + float(np.mean(offs))
        coh = np.where(den > 0, cnum / np.maximum(den, 1e-12), np.nan)
        return {"mag": mag, "coh": coh, "n": len(use)}

    def capture(self, name=None):
        with self.lock:
            an = self.an
            if an is None or self.mode == "stopped":
                return {"ok": False, "msg": "Start the input first."}
            if an.meas == an.ref:
                return {"ok": False, "msg": "Pick different measurement and reference channels first."}
            tf = an.tf_snapshot(48)
            if tf["frames"] < 8:
                return {"ok": False, "msg": "Still averaging. Give it a few more seconds, then capture."}
            self.trace_seq += 1
            tid = self.trace_seq
            self.traces.append({"id": tid, "name": (name or "").strip()[:40] or "Position %d" % tid,
                                "mag": arr(tf["mag"], 2), "coh": arr(tf["coh"], 3), "quality": round(tf["quality"], 3),
                                "delay_ms": round(1000.0 * an.delay / an.sr, 2), "visible": True, "in_avg": True,
                                "color": (tid - 1) % 6, "time": time.strftime("%H:%M")})
        self.bump()
        msg = None
        if tf["quality"] < 0.6:
            msg = "Captured, but coherence is low (%d%%). Check the delay and signal level before trusting it." % round(tf["quality"] * 100)
        return {"ok": True, "id": tid, "msg": msg}

    def run_eq(self):
        with self.lock:
            fc = self._focus(self.an if self.mode != "stopped" else None)
        if fc is None:
            return {"ok": False, "msg": "Nothing to fit yet. Run a measurement or capture a trace first."}
        label, mag, coh = fc
        o = self.s["eq"]
        con = self.console()
        mag_s = dsp.smooth_db(mag, 1.0 / 12.0)
        if o["auto_range"]:
            f_lo, f_hi = eq.usable_range(mag_s, coh)
        else:
            f_lo, f_hi = float(o["f_lo"]), float(o["f_hi"])
        res = eq.auto_eq(mag_s, coh, self.target(), f_lo, f_hi, max_bands=int(o["bands"]),
                         max_boost=min(float(o["boost"]), con["gain"]), max_cut=min(float(o["cut"]), con["gain"]),
                         q_min=max(0.5, con["q_min"]), q_max=min(10.0, con["q_max"]))
        if not res["ok"]:
            return res
        self.eq = {"bands": res["bands"], "curve": arr(res["eq"], 2), "predicted": arr(res["predicted"], 2),
                   "offset": round(res["offset"], 2), "before": round(res["rms_before"], 2),
                   "after": round(res["rms_after"], 2), "f_lo": round(f_lo, 1), "f_hi": round(f_hi, 1),
                   "source": label, "target": dict(self.s["target"]), "time": time.strftime("%H:%M")}
        self.bump()
        return {"ok": True, "n": len(res["bands"])}

    # ---------------------------------------------------------------- static
    def static(self):
        with self.lock:
            an = self.an
            frac = int(self.s["tf_smooth"])
            oct_w = 0.0 if frac >= 48 else 1.0 / frac
            traces = []
            for t in self.traces:
                traces.append({"id": t["id"], "name": t["name"], "visible": t["visible"], "in_avg": t["in_avg"],
                               "color": t["color"], "quality": t["quality"], "delay_ms": t["delay_ms"],
                               "time": t.get("time", ""),
                               "mag": arr(dsp.smooth_db(from_list(t["mag"]), oct_w), 2), "coh": t["coh"]})
            best = self.best_trace()
            r = self.roles()
            nch = an.nch if an is not None and self.mode != "stopped" else 0
            return {
                "ver": self.ver, "version": __version__, "grid": arr(dsp.GRID, 2), "settings": self.s,
                "roles": {"meas": r["meas"], "ref": r["ref"]},
                "channels": [{"i": c, "name": self.meta(c)["name"], "type": self.meta(c)["type"]} for c in range(nch)],
                "traces": traces,
                "best": ({"mag": arr(dsp.smooth_db(best["mag"], oct_w), 2), "coh": arr(best["coh"], 3), "n": best["n"]}
                         if best else None),
                "eq": self.eq, "target": arr(self.target(), 2),
                "presets": eq.TARGET_PRESETS,
                "types": {k: v["label"] for k, v in advisor.SOURCE_TYPES.items()},
                "consoles": {k: {"label": v["label"], "in_bands": v["in_bands"], "out_bands": v["out_bands"],
                                 "gain": v["gain"], "q_max": v["q_max"], "usb_ch": v["usb_ch"], "out_note": v["out_note"]}
                             for k, v in advisor.CONSOLES.items()},
                "cal": {"name": self.cal["name"]} if self.cal else None,
                "gen": dict(self.gen.cfg), "demo_only": self.demo_only, "native": self.native,
                "osc": self._osc_static(), "align": self._align_static(),
                "demo_systems": DemoSource.SYSTEMS,
            }

    # -------------------------------------------------------------- commands
    def command(self, d):
        cmd = d.get("cmd")
        fn = getattr(self, "c_" + str(cmd), None)
        if fn is None:
            return {"ok": False, "msg": "Unknown command."}
        try:
            return fn(d) or {"ok": True}
        except osc.OscError as exc:
            return {"ok": False, "msg": str(exc)}
        except (ValueError, TypeError, KeyError) as exc:
            return {"ok": False, "msg": str(exc) or "Invalid request."}

    def c_start(self, d):
        dev = d.get("device")
        return self.start_live(None if dev in (None, "", "default") else int(dev), int(d.get("sr") or 0),
                               int(d.get("channels") or 0))

    def c_start_demo(self, d):
        return self.start_demo()

    def c_stop(self, d):
        return self.stop()

    def c_set(self, d):
        s = self.s
        if "avg" in d and d["avg"] in AVG:
            s["avg"] = d["avg"]
        if "sens" in d and d["sens"] in ("low", "normal", "high"):
            s["sens"] = d["sens"]
        if "tf_smooth" in d and int(d["tf_smooth"]) in SMOOTH:
            s["tf_smooth"] = int(d["tf_smooth"])
        if "rta_smooth" in d and int(d["rta_smooth"]) in SMOOTH:
            s["rta_smooth"] = int(d["rta_smooth"])
        if "console" in d and d["console"] in advisor.CONSOLES:
            s["console"] = d["console"]
        if "track" in d:
            s["track"] = bool(d["track"])
        if "target" in d:
            t = d["target"]
            if t.get("preset") in eq.TARGET_PRESETS:
                p = eq.TARGET_PRESETS[t["preset"]]
                s["target"] = {"preset": t["preset"], "low": p["low"], "tilt": p["tilt"]}
            else:
                s["target"] = {"preset": "custom",
                               "low": min(max(float(t.get("low", s["target"]["low"])), -6.0), 15.0),
                               "tilt": min(max(float(t.get("tilt", s["target"]["tilt"])), -3.0), 1.0)}
        if "eq" in d:
            e, o = d["eq"], s["eq"]
            if "bands" in e:
                o["bands"] = int(min(max(int(e["bands"]), 1), 12))
            if "boost" in e:
                o["boost"] = min(max(float(e["boost"]), 0.0), 6.0)
            if "cut" in e:
                o["cut"] = min(max(float(e["cut"]), 3.0), 18.0)
            if "auto_range" in e:
                o["auto_range"] = bool(e["auto_range"])
            if "f_lo" in e:
                o["f_lo"] = min(max(float(e["f_lo"]), 20.0), 2000.0)
            if "f_hi" in e:
                o["f_hi"] = min(max(float(e["f_hi"]), 2000.0), 20000.0)
            if "source" in e:
                o["source"] = e["source"] if e["source"] in ("auto", "live", "best") else int(e["source"])
        with self.lock:
            self._apply_to_analyzer()
        self.bump()

    def c_roles(self, d):
        r = self.roles()
        with self.lock:
            nch = self.an.nch if self.an else 64
            for k in ("meas", "ref"):
                if k in d:
                    r[k] = int(min(max(int(d[k]), 0), nch - 1))
            if self.an is not None:
                self._apply_to_analyzer()
                self.an.delay = 0
                self.an.delay_result = None
                self.an.reset_tf()
                self.an.clear_events()
                self.an.rta_cnt = 0
        self.bump()

    def c_find_delay(self, d):
        with self.lock:
            if self.an is None or self.mode == "stopped":
                return {"ok": False, "msg": "Start the input first."}
            if self.an.meas == self.an.ref:
                return {"ok": False, "msg": "Pick different measurement and reference channels first."}
            self.an.delay_result = None
            self.an.delay_pending = True

    def c_set_delay(self, d):
        with self.lock:
            if self.an is None:
                return {"ok": False, "msg": "Start the input first."}
            ms = min(max(float(d["ms"]), -500.0), 2000.0)
            self.an.delay = int(round(ms * self.an.sr / 1000.0))
            self.an.reset_tf()

    def c_reset_avg(self, d):
        with self.lock:
            if self.an is not None:
                self.an.reset_tf()

    def c_capture(self, d):
        return self.capture(d.get("name"))

    def c_trace(self, d):
        for t in self.traces:
            if t["id"] == int(d["id"]):
                if "name" in d:
                    t["name"] = str(d["name"]).strip()[:40] or t["name"]
                for k in ("visible", "in_avg"):
                    if k in d:
                        t[k] = bool(d[k])
        self.bump()

    def c_trace_delete(self, d):
        self.traces = [t for t in self.traces if t["id"] != int(d["id"])]
        if self.s["eq"]["source"] == int(d["id"]):
            self.s["eq"]["source"] = "auto"
        self.bump()

    def c_traces_clear(self, d):
        self.traces = []
        self.s["eq"]["source"] = "auto"
        self.bump()

    def c_auto_eq(self, d):
        return self.run_eq()

    def c_eq_clear(self, d):
        self.eq = None
        self.bump()

    def c_eq_refit(self, d):
        self.s["eq"]["bands"] = int(min(max(int(d.get("bands", 4)), 1), 12))
        return self.run_eq()

    def c_clear_events(self, d):
        with self.lock:
            if self.an is not None:
                self.an.clear_events()

    def c_channel(self, d):
        c = int(d["i"])
        key = "demo" if self.mode == "demo" else "live"
        m = dict(self.meta(c))
        if "name" in d:
            m["name"] = str(d["name"]).strip()[:32] or "Input %d" % (c + 1)
            if "type" not in d and m["type"] == "auto":
                m["type"] = advisor.guess_type(m["name"])
        if "type" in d and d["type"] in advisor.SOURCE_TYPES:
            m["type"] = d["type"]
        self.ch_meta[key][str(c)] = m
        self._held.pop(c, None)
        self.bump()

    def c_gen(self, d):
        if self.demo_only:
            return {"ok": False, "msg": "The signal generator needs Mixx Trace running on your own computer."}
        if d.get("on"):
            cfg = {"type": "sine" if d.get("type") == "sine" else "pink",
                   "freq": min(max(float(d.get("freq", 1000)), 20.0), 20000.0),
                   "level": min(max(float(d.get("level", -24)), -60.0), -6.0),
                   "device": None if d.get("device") in (None, "", "default") else int(d["device"]),
                   "channels": [int(c) for c in d.get("channels", [1])][:8] or [1]}
            self.gen.start(cfg)
            self.ver += 1
            if self.gen.error:
                return {"ok": False, "msg": "Could not open the output: %s" % self.gen.error}
        else:
            if "level" in d:
                self.gen.cfg["level"] = min(max(float(d["level"]), -60.0), -6.0)
            else:
                self.gen.stop()
            self.ver += 1

    def c_gen_level(self, d):
        self.gen.cfg["level"] = min(max(float(d["level"]), -60.0), -6.0)
        self.ver += 1

    def c_cal_load(self, d):
        db = parse_cal(str(d.get("text", "")))
        self.cal = {"name": str(d.get("name") or "Mic calibration")[:60], "db": arr(db, 2)}
        with self.lock:
            self._apply_to_analyzer()
            if self.an is not None:
                self.an.reset_tf()
        self.bump()

    def c_cal_clear(self, d):
        self.cal = None
        with self.lock:
            self._apply_to_analyzer()
        self.bump()

    def c_save_file(self, d):
        """Desktop app only: an app window has no downloads bar, so reports and sessions go straight to Downloads."""
        if not self.native:
            return {"ok": False, "msg": "Not available here."}
        name, text = str(d.get("name") or ""), d.get("text")       # no folders: the pattern allows no slashes
        if not re.fullmatch(r"mixxtrace-[A-Za-z0-9_-]{1,60}\.(txt|json)", name) or not isinstance(text, str):
            return {"ok": False, "msg": "That file cannot be saved."}
        try:
            os.makedirs(self.downloads, exist_ok=True)
            stem, ext = os.path.splitext(name)
            path, n = os.path.join(self.downloads, name), 1
            while os.path.exists(path):                  # never overwrite an earlier report
                n += 1
                path = os.path.join(self.downloads, "%s (%d)%s" % (stem, n, ext))
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
        except OSError as exc:
            return {"ok": False, "msg": "Could not save to the Downloads folder: %s" % (exc.strerror or exc)}
        return {"ok": True, "path": path, "msg": "Saved to your Downloads folder as %s" % os.path.basename(path)}

    def c_session_import(self, d):
        self._apply_session(d.get("data"))
        with self.lock:
            self._apply_to_analyzer()
        self.bump()

    def c_demo_move(self, d):
        e = self.engine
        if self.mode != "demo" or e.demo is None:
            return {"ok": False, "msg": "Only available with the demo signal."}
        with self.lock:
            e.demo.position = (e.demo.position + 1) % len(DemoSource.POSITIONS)
            self.an.delay_result = None
            self.an.reset_tf()
        self.ver += 1
        return {"ok": True, "pos": e.demo.position + 1}

    def c_demo_system(self, d):
        e = self.engine
        if self.mode != "demo" or e.demo is None:
            return {"ok": False, "msg": "Only available with the demo signal."}
        if d.get("system") not in DemoSource.SYSTEMS:
            return {"ok": False, "msg": "Unknown demo system."}
        with self.lock:
            e.demo.configure(system=d["system"])
            self.an.delay_result = None
            self.an.reset_tf()
        self.ver += 1
        return {"ok": True}

    # ------------------------------------------------------- console remote
    def _osc_mode(self):
        return "sim" if self.demo_only else self.s["osc"]["mode"]

    def _link(self):
        o = self.s["osc"]
        mode = self._osc_mode()
        if mode == "sim":
            return osc.SimLink(self.sim)
        if mode != "udp":
            raise osc.OscError("Console remote is off. Turn it on in Setup first.")
        return osc.UdpLink(osc.check_host(o["host"]), osc.check_port(o["send_port"]), osc.check_port(o["listen_port"]))

    def _osc_run(self, fn):
        """Open one paced conversation with the console and run fn(client, console_name) in it."""
        if not self.osc_lock.acquire(blocking=False):
            return {"ok": False, "msg": "Still talking to the console. Wait for it to finish."}
        try:
            self.osc_progress = {"sent": 0}
            cli = osc.Client(self._link(), progress=lambda n: self.osc_progress.update(sent=n))
            with cli:
                name = osc.probe(cli)
                self.osc_info = {"name": name, "time": time.strftime("%H:%M")}
                return fn(cli, name)
        except osc.OscError as exc:
            return {"ok": False, "msg": str(exc)}
        finally:
            self.osc_progress = None
            self.osc_lock.release()

    def _sync_demo(self):
        """The demo PA follows the simulated console, so a push can be heard and measured."""
        d = self.engine.demo
        if d is None:
            return
        proc = {}
        if self._osc_mode() == "sim":
            o, al = self.s["osc"], self.s["align"]
            proc["full"] = self.sim.output(o["out_type"], o["out_n"])
            for k in ("a", "b"):
                if al[k + "_type"]:
                    proc[k] = self.sim.output(al[k + "_type"], al[k + "_n"])
        d.configure(proc=proc)

    def _osc_static(self):
        o = self.s["osc"]
        u = self.osc_undo
        return {"mode": self._osc_mode(), "forced_sim": self.demo_only, "host": o["host"], "send_port": o["send_port"],
                "listen_port": o["listen_port"], "out_type": o["out_type"], "out_n": o["out_n"],
                "types": list(osc.OUT_TYPES), "info": self.osc_info,
                "undo": {"what": u["what"], "target": u["target"], "time": u["time"]} if u else None}

    def c_osc_set(self, d):
        o = self.s["osc"]
        if "mode" in d and d["mode"] in ("off", "sim", "udp"):
            o["mode"] = d["mode"]
        if "host" in d:
            host = str(d["host"]).strip()
            o["host"] = osc.check_host(host) if host else ""
        for k in ("send_port", "listen_port"):
            if k in d:
                o[k] = osc.check_port(d[k])
        if o["send_port"] == o["listen_port"] and o["host"] in ("127.0.0.1",):
            raise osc.OscError("The two ports must differ.")
        if "out_type" in d and d["out_type"] in osc.OUT_TYPES:
            o["out_type"] = d["out_type"]
        if "out_n" in d:
            o["out_n"] = int(min(max(int(d["out_n"]), 1), 128))
        al = self.s["align"]
        for k in ("a", "b"):
            if k + "_name" in d:
                al[k + "_name"] = str(d[k + "_name"]).strip()[:24] or ("Mains" if k == "a" else "Subs")
            if k + "_type" in d and (d[k + "_type"] in osc.OUT_TYPES or d[k + "_type"] == ""):
                al[k + "_type"] = d[k + "_type"]
            if k + "_n" in d:
                al[k + "_n"] = int(min(max(int(d[k + "_n"]), 1), 128))
        self.osc_plan = None
        if any(k in d for k in ("mode", "host", "send_port", "listen_port")):
            self.osc_info = None
        self._sync_demo()
        self.bump()

    def c_osc_test(self, d):
        r = self._osc_run(lambda cli, name: {"ok": True, "msg": "Connected to %s." % name, "name": name})
        self.ver += 1
        return r

    def _plan_for(self, what, cli):
        o, al = self.s["osc"], self.s["align"]
        if what == "eq":
            if not self.eq or not self.eq["bands"]:
                raise osc.OscError("There is no EQ to send yet. Press Suggest EQ first.")
            plan = osc.plan_eq(cli, o["out_type"], o["out_n"], self.eq["bands"])
            if plan["ok"]:
                plan["title"] = "Send system EQ to %s" % plan["target"]
            return plan
        if what == "align":
            r = self.al["result"]
            if not r or not r.get("ok"):
                raise osc.OscError("There is no alignment to send yet. Measure both systems first.")
            if r.get("pushed"):
                raise osc.OscError("This correction was already sent at %s. Sending it again would add the delay twice. "
                                   "Measure both systems again first." % r["pushed"])
            steps = []
            for k in ("a", "b"):
                add = r["ms"] / 1000.0 if (r["who"] == k and r["ms"] >= 0.01) else 0.0
                flip = bool(k == "b" and r["invert"])
                if not add and not flip:
                    continue
                if not al[k + "_type"]:
                    raise osc.OscError("Pick the console output that feeds %s first." % al[k + "_name"])
                steps.append({"typ": al[k + "_type"], "n": al[k + "_n"], "name": al[k + "_name"], "add_s": add, "flip": flip})
            if not steps:
                raise osc.OscError("The two systems are already aligned. Nothing to send.")
            if len(steps) == 2 and (steps[0]["typ"], steps[0]["n"]) == (steps[1]["typ"], steps[1]["n"]):
                raise osc.OscError("Both systems are set to the same console output. Give each its own output.")
            plan = osc.plan_delay(cli, steps)
            plan["title"] = "Send alignment to %s" % plan["target"]
            return plan
        if what == "undo":
            u = self.osc_undo
            if not u:
                raise osc.OscError("Nothing to undo.")
            plan = osc.plan_restore(cli, u["saved"])
            plan["title"] = "Undo: put %s back as it was at %s" % (u["target"], u["time"])
            return plan
        raise osc.OscError("Unknown request.")

    def c_osc_preview(self, d):
        """Read the console and lay out exactly what would be written. Sends no changes."""
        what = str(d.get("what"))
        self.osc_plan = None

        def run(cli, name):
            plan = self._plan_for(what, cli)
            if not plan["ok"]:
                return plan
            n = sum(1 for o in plan["ops"] if not o["skip"])
            if n == 0:
                return {"ok": False, "same": True, "msg": "The console already holds these values. Nothing to send."}
            token = "%x" % int(time.time() * 1000)
            self.osc_plan = dict(plan, what=what, token=token, made=time.time(), console=name)
            sim = self._osc_mode() == "sim"
            return {"ok": True, "token": token, "title": plan["title"], "console": name, "sim": sim,
                    "warnings": plan["warnings"], "n": n,
                    "seconds": 0 if sim else int(math.ceil(osc.count_messages(plan["ops"]) * osc.PACE)),
                    "rows": [{"label": o["label"], "now": o["now_txt"], "new": o["new_txt"], "skip": o["skip"]}
                             for o in plan["ops"]]}

        return self._osc_run(run)

    def c_osc_push(self, d):
        plan = self.osc_plan
        if d.get("confirm") is not True or not plan or d.get("token") != plan["token"]:
            return {"ok": False, "msg": "Nothing was sent. Open the preview and confirm it first."}
        if time.time() - plan["made"] > PLAN_TTL:
            self.osc_plan = None
            return {"ok": False, "msg": "That preview is more than two minutes old. Nothing was sent. Open it again."}
        self.osc_plan = None

        def run(cli, name):
            n, bad = osc.execute(cli, plan["ops"])
            stamp = time.strftime("%H:%M")
            if plan["what"] == "undo":
                self.osc_undo = None
                if self.eq:
                    self.eq.pop("pushed", None)
                if self.al["result"]:
                    self.al["result"].pop("pushed", None)
            else:
                saved, seen = [], set()
                for o in plan["ops"]:
                    if not o["skip"] and o["addr"] not in seen:
                        seen.add(o["addr"])
                        saved.append({"addr": o["addr"], "kind": o["kind"], "value": o["now"],
                                      "label": o["label"].replace(" (flatten before moving)", "")})
                # the flatten step recorded the real starting gain; later steps on that address did not
                self.osc_undo = {"what": plan["what"], "target": plan["target"], "time": stamp, "saved": saved[::-1]}
                if plan["what"] == "eq" and self.eq:
                    self.eq["pushed"] = {"target": plan["target"], "time": stamp, "console": name}
                if plan["what"] == "align" and self.al["result"]:
                    self.al["result"]["pushed"] = stamp
            self._sync_demo()
            if bad:
                return {"ok": True, "n": n, "mismatch": bad,
                        "msg": "Sent, but the console reports a different value for: %s. Check the console screen."
                               % ", ".join("%s (wanted %s, console says %s)" % (b["label"], b["want"], b["got"]) for b in bad[:4])}
            return {"ok": True, "n": n, "mismatch": [],
                    "msg": "%s: %d value%s written and read back from %s." % (
                        "Restored" if plan["what"] == "undo" else "Sent", n, "" if n == 1 else "s", name)}

        r = self._osc_run(run)
        self.bump()
        return r

    def c_osc_eq_in(self, d):
        """Switch the pushed output EQ in or out, for a quick before and after."""
        if not self.eq or not self.eq.get("pushed"):
            return {"ok": False, "msg": "Send the EQ to the console first."}
        o = self.s["osc"]
        on = 1.0 if d.get("on") else 0.0
        addr = osc.base(o["out_type"], o["out_n"]) + "EQ/eq_in"

        def run(cli, name):
            cli.write(addr, on)
            got = cli.query(addr)
            self._sync_demo()
            if got is None or (got >= 0.5) != (on >= 0.5):
                return {"ok": False, "msg": "The console did not confirm the EQ switch."}
            self.eq["pushed"]["bypassed"] = not on
            return {"ok": True, "msg": "Output EQ switched %s on the console." % ("in" if on else "out")}

        r = self._osc_run(run)
        self.bump()
        return r

    # ------------------------------------------------------------- alignment
    def _align_pct(self, an, j):
        if j["stage"] == "settle":
            return int(30 * min(1.0, (an.written - j["t0"]) / (ALIGN_SETTLE * an.sr)))
        if j["stage"] == "delay":
            return 35
        return 40 + int(60 * min(1.0, min(t["cnt"] for t in an.tiers) / float(ALIGN_FRAMES)))

    def _align_fail(self, msg):
        self.al["job"] = None
        self.al["error"] = msg
        if self.an is not None:
            self.an.track_hold = False
        self.ver += 1

    def _align_step(self):
        """Runs on the audio thread: settle, lock the delay, average, capture."""
        an, j = self.an, self.al["job"]
        if an.written - j["t0"] > 45 * an.sr:
            return self._align_fail("That took too long. Check that signal is playing and try again.")
        if j["stage"] == "settle":
            if an.written - j["t0"] >= ALIGN_SETTLE * an.sr:
                an.delay_result = None
                an.delay_pending = True
                j["stage"] = "delay"
        elif j["stage"] == "delay":
            r = an.delay_result
            if r is None:
                return
            j["locked"] = bool(r["ok"])
            if not r["ok"]:
                other = self.al["b" if j["slot"] == "a" else "a"]
                if other is not None:
                    an.delay = int(other["delay"])
                an.reset_tf()
            j["stage"] = "avg"
        elif min(t["cnt"] for t in an.tiers) >= ALIGN_FRAMES:
            tf = an.tf_snapshot(48)
            name = self.s["align"][j["slot"] + "_name"]
            lo = dsp.GRID < 250
            q_lo = float(np.nanmean(np.where(np.isfinite(tf["coh"]), tf["coh"], 0.0)[lo]))
            if max(tf["quality"], q_lo) < 0.3:
                return self._align_fail("No usable signal from %s (coherence %d%%). Check that it is unmuted and that "
                                        "test signal is playing." % (name, round(100 * max(tf["quality"], q_lo))))
            self.al[j["slot"]] = {"mag": arr(tf["mag"], 2), "phase": arr(tf["phase"], 1), "coh": arr(tf["coh"], 3),
                                  "delay": int(an.delay), "sr": an.sr, "locked": j["locked"],
                                  "quality": round(max(tf["quality"], q_lo), 3), "time": time.strftime("%H:%M")}
            self.al["job"] = None
            self.al["error"] = None
            an.track_hold = False
            self._align_solve()
            self.ver += 1

    def _align_solve(self):
        a, b = self.al["a"], self.al["b"]
        self.al["result"] = None
        if not a or not b:
            return
        if a["sr"] != b["sr"]:
            self.al["error"] = "The two captures used different sample rates. Measure both again."
            return
        caps = [{"mag": from_list(c["mag"]), "phase": from_list(c["phase"]), "coh": from_list(c["coh"]),
                 "delay": c["delay"], "sr": c["sr"], "locked": c["locked"]} for c in (a, b)]
        r = align.solve(caps[0], caps[1])
        if not r["ok"]:
            self.al["error"] = r["msg"]
            return
        out = {}
        for k, v in r.items():
            if isinstance(v, np.ndarray):
                out[k] = arr(v, 3 if k.startswith("coh") else 2)
            elif isinstance(v, float):
                out[k] = round(v, 3)
            else:
                out[k] = v
        self.al["result"] = out

    def _align_view(self):
        """Display copies of the captures and the prediction, lightly smoothed. Rebuilt only when they change."""
        al = self.al
        key = (al["a"], al["b"], al["result"])          # held by reference, so a new capture can never look like an old one
        old = al.get("view_key")
        if old is not None and all(x is y for x, y in zip(old, key)):
            return al["view"]
        v = {}
        r = al["result"]
        sm = lambda x: arr(dsp.smooth_db(from_list(x), 1.0 / 12.0), 2)
        if r:
            for k in ("a", "b", "sum_now", "sum_fix"):
                v[k] = sm(r[k])
            for k in ("ph_a", "ph_b", "ph_b_fix", "coh_a", "coh_b"):
                v[k] = r[k]
        else:
            for k in ("a", "b"):
                if al[k]:
                    v[k] = sm(al[k]["mag"])
                    v["coh_" + k] = al[k]["coh"]
                    v["ph_" + k] = [p if (p is not None and (c or 0) >= 0.3) else None
                                    for p, c in zip(al[k]["phase"], al[k]["coh"])]
        al["view"], al["view_key"] = v, key
        return v

    def _align_static(self):
        al = self.al
        meta = lambda c: ({"time": c["time"], "quality": c["quality"], "locked": c["locked"],
                           "delay_ms": round(1000.0 * c["delay"] / c["sr"], 2)} if c else None)
        r = al["result"]
        return dict(self.s["align"], a=meta(al["a"]), b=meta(al["b"]), error=al["error"], view=self._align_view(),
                    result=({k: v for k, v in r.items() if not isinstance(v, list)} if r else None))

    def c_align_measure(self, d):
        slot = d.get("slot")
        if slot not in ("a", "b"):
            return {"ok": False, "msg": "Unknown system."}
        with self.lock:
            an = self.an
            if an is None or self.mode == "stopped":
                return {"ok": False, "msg": "Start the input first."}
            if an.meas == an.ref:
                return {"ok": False, "msg": "Pick different measurement and reference channels first."}
            if self.mode == "demo" and self.engine.demo is not None:
                self.engine.demo.configure(system=slot)
            an.track_hold = True
            self.al["error"] = None
            self.al["job"] = {"slot": slot, "stage": "settle", "t0": an.written, "locked": False}
        self.ver += 1
        return {"ok": True}

    def c_align_cancel(self, d):
        with self.lock:
            self.al["job"] = None
            if self.an is not None:
                self.an.track_hold = False
        self.ver += 1

    def c_align_clear(self, d):
        with self.lock:
            self.al.update(a=None, b=None, result=None, job=None, error=None)
            if self.an is not None:
                self.an.track_hold = False
        self.ver += 1

    # ---------------------------------------------------------------- report
    def report(self):
        con = self.console()
        L = ["Mixx Trace report  -  %s" % time.strftime("%Y-%m-%d %H:%M"), "Console profile: %s" % con["label"], ""]
        with self.lock:
            an = self.an if self.mode != "stopped" else None
            L.append("SYSTEM EQ (output / matrix EQ)")
            if self.eq:
                e = self.eq
                L.append("  Fitted to: %s, %s-%s, deviation %.1f dB -> %.1f dB (predicted)" % (
                    e["source"], dsp.fmt_hz(e["f_lo"]), dsp.fmt_hz(e["f_hi"]), e["before"], e["after"]))
                for i, b in enumerate(e["bands"]):
                    L.append("  Band %d   Bell   %-9s  %+5.1f dB   Q %.1f" % (i + 1, dsp.fmt_hz(b["f"]), b["gain"], b["q"]))
                if not e["bands"]:
                    L.append("  Already within 1 dB of the target. No EQ suggested.")
                if e.get("pushed"):
                    L.append("  Sent to the console: %s at %s" % (e["pushed"]["target"], e["pushed"]["time"]))
            else:
                L.append("  No EQ fitted yet.")
            r = self.al["result"]
            if r and r.get("ok"):
                al = self.s["align"]
                L += ["", "ALIGNMENT (%s to %s)" % (al["b_name"], al["a_name"]),
                      "  Delay %s by %.2f ms (%.2f m / %.1f ft)" % (al[r["who"] + "_name"], r["ms"], r["m"], r["ft"]),
                      "  %s polarity: %s" % (al["b_name"], "invert" if r["invert"] else "leave as it is"),
                      "  Judged over %s-%s: %+.1f dB more summed level than now" % (
                          dsp.fmt_hz(r["f_lo"]), dsp.fmt_hz(r["f_hi"]), r["gain"])]
            L += ["", "PROBLEM FREQUENCIES (measurement mic)"]
            evs = self._events(an) if an else []
            if not evs:
                L.append("  None logged.")
            for ev in evs:
                n = ev["notch"]
                L.append("  %-9s %-8s (%s)  x%d  peak %.0f dBFS   cut: Bell %s  %+.0f dB  Q %.0f%s" % (
                    dsp.fmt_hz(ev["f"]), ev["status"], ev["note"], ev["hits"], ev["max"], dsp.fmt_hz(n["f"]),
                    n["gain"], n["q"], ("   seen on: " + ", ".join(s["name"] for s in ev["src"])) if ev["src"] else ""))
            L += ["", "INPUT CHANNELS"]
            chans = self._channels(an) if an else []
            if not chans:
                L.append("  Input not running.")
            for ch in chans:
                if ch["role"] or not ch["active"]:
                    continue
                L.append("  %2d  %s" % (ch["i"] + 1, ch["name"]))
                for f in ch["findings"]:
                    L.append("        - %s: %s" % (f["title"], f["detail"]))
                st = ch["strip"]
                if st["gain"]:
                    L.append("        Gain trim  %s" % st["gain"])
                if st["hpf"]:
                    L.append("        %s  %s" % (con["hpf"], dsp.fmt_hz(st["hpf"])))
                for b in st["bands"]:
                    L.append("        %-16s Bell %-9s %+5.1f dB  Q %.1f%s  (%s)" % (
                        b["band"], dsp.fmt_hz(b["f"]), b["gain"], b["q"], "  dynamic" if b["dyn"] else "", b["why"]))
                if st["dyn"]:
                    L.append("        Dynamics   %s" % st["dyn"])
        L += ["", "These are starting points from measurement. Confirm every change by ear."]
        return "\n".join(L) + "\n"

    # ------------------------------------------------------------ housekeeping
    def idle_check(self):
        """Hosted demo only: stop simulating when nobody is watching."""
        if self.demo_only and self.mode == "demo" and time.time() - self.last_poll > 90:
            self.engine.stop()
            self.idle_stopped = True
