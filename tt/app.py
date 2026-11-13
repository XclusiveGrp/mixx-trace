"""Application state: ties the audio engine, analyzer, EQ fitting and channel advice together."""
import json
import math
import os
import threading
import time

import numpy as np

from . import __version__, advisor, dsp, eq
from .analyzer import Analyzer
from .engine import DemoSource, Engine, Generator

AVG = {"fast": 1.0, "med": 4.0, "slow": 10.0, "inf": None}
SMOOTH = (3, 6, 12, 24, 48)
HOLD_S = 8.0                     # seconds a channel finding stays listed after it was last seen
SESSION_DIR = os.path.join(os.path.expanduser("~"), ".tunetrace")
SESSION_FILE = os.path.join(SESSION_DIR, "session.json")


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
    def __init__(self, demo_only=False, persist=True):
        self.lock = threading.RLock()
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
        }
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

    def bump(self):
        self.ver += 1
        self._save()

    def _session(self):
        return {"app": "TuneTrace", "version": __version__, "settings": self.s, "traces": self.traces,
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
            with open(tmp, "w") as fh:
                json.dump(self._session(), fh)
            os.replace(tmp, SESSION_FILE)
        except OSError:
            pass

    def _load(self):
        try:
            with open(SESSION_FILE) as fh:
                self._apply_session(json.load(fh))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _apply_session(self, data):
        if not isinstance(data, dict) or data.get("app") != "TuneTrace":
            raise ValueError("This is not a TuneTrace session file.")
        n = len(dsp.GRID)
        st = data.get("settings") or {}
        for k, v in st.items():
            if k in ("target", "eq") and isinstance(v, dict):
                self.s[k].update({kk: vv for kk, vv in v.items() if kk in self.s[k]})
            elif k in self.s and not isinstance(self.s[k], dict):
                self.s[k] = v
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
        self.idle_stopped = False
        self.ver += 1
        return {"ok": True}

    def start_live(self, device=None, sr=None, channels=None):
        if self.demo_only:
            return {"ok": False, "msg": "Live input is only available when TuneTrace runs on your own computer."}
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
                "demo_pos": (e.demo.position + 1) if (e.mode == "demo" and e.demo) else None}

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
            if view == "feedback":
                r = an.rta_snapshot(int(self.s["rta_smooth"]))
                out["rta"] = {"avg": arr(r["avg"]), "peak": arr(r["peak"])}
                out["events"] = evs
                out["buildups"] = [{"f": round(b["f"], 1), "excess": round(b["excess"], 1), "iso": dsp.iso_band(b["f"]),
                                    "cut": -min(6.0, max(2.0, round(b["excess"] * 0.6 * 2) / 2))} for b in an.buildups]
            elif view == "tune":
                tf = an.tf_snapshot(int(self.s["tf_smooth"]))
                have = tf["frames"] >= 1 and out["tf_ok"]
                out["tf"] = {"mag": arr(tf["mag"]) if have else None, "phase": arr(tf["phase"], 0) if have else None,
                             "coh": arr(tf["coh"], 2) if have else None, "quality": round(tf["quality"], 3),
                             "frames": tf["frames"]}
                dr = an.delay_result
                out["delay"] = {"samples": int(an.delay), "ms": round(1000.0 * an.delay / an.sr, 2),
                                "m": round(343.0 * an.delay / an.sr, 2), "pending": bool(an.delay_pending),
                                "result": ({k: (round(v, 2) if isinstance(v, float) else v) for k, v in dr.items()}
                                           if dr else None)}
                fc = self._focus(an, tf if have else None) if have or self.traces else None
                off = self._target_offset(fc[1], fc[2]) if fc else None
                out["target_off"] = round(off, 2) if off is not None else None
                out["focus"] = fc[0] if fc else None
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
                "gen": dict(self.gen.cfg), "demo_only": self.demo_only,
            }

    # -------------------------------------------------------------- commands
    def command(self, d):
        cmd = d.get("cmd")
        fn = getattr(self, "c_" + str(cmd), None)
        if fn is None:
            return {"ok": False, "msg": "Unknown command."}
        try:
            return fn(d) or {"ok": True}
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
            return {"ok": False, "msg": "The signal generator needs TuneTrace running on your own computer."}
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

    # ---------------------------------------------------------------- report
    def report(self):
        con = self.console()
        L = ["TuneTrace report  -  %s" % time.strftime("%Y-%m-%d %H:%M"), "Console profile: %s" % con["label"], ""]
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
            else:
                L.append("  No EQ fitted yet.")
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
