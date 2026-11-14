"""Real-time analysis: transfer function, RTA, feedback detection, channel scan."""
import math
import time
from collections import deque

import numpy as np

from . import dsp

SENS = {"low": (12.0, 26.0), "normal": (9.0, 22.0), "high": (7.0, 18.0)}


def find_peaks(p, n_fft, sr, fmin=40.0, fmax=16000.0, min_prom=9.0, floor_db=-85.0, top=8):
    """Narrow-band peak candidates in a power spectrum (rfft of a Hann frame).

    Level is in dBFS of an equivalent sine. Prominence is the height above the
    geometric-mean level of the surrounding third-octave, main lobe excluded.
    """
    df = sr / n_fft
    db = dsp.db10(p) + 10.0 * math.log10(16.0 / (n_fft * n_fft))
    nb = len(db)
    k0 = max(4, int(fmin / df))
    k1 = min(nb - 6, int(fmax / df))
    if k1 <= k0:
        return []
    seg = db[k0:k1 + 1]
    is_max = (seg > db[k0 - 1:k1]) & (seg >= db[k0 + 1:k1 + 2]) & (seg > floor_db)
    ks = np.nonzero(is_max)[0] + k0
    if len(ks) == 0:
        return []
    c = np.concatenate(([0.0], np.cumsum(db)))

    def prom_at(k):
        """Height above the louder of the two flanks (each about a sixth-octave, main lobe excluded)."""
        k = np.asarray(k)
        m = np.maximum(6, np.round(k * 0.12).astype(int))
        l_hi = k - 4
        l_lo = np.maximum(l_hi - m + 1, 1)
        r_lo = k + 4
        r_hi = np.minimum(r_lo + m - 1, nb - 1)
        l_cnt = l_hi - l_lo + 1
        r_cnt = r_hi - r_lo + 1
        left = np.where(l_cnt >= 3, (c[np.maximum(l_hi, 0) + 1] - c[l_lo]) / np.maximum(l_cnt, 1), -1e9)
        right = np.where(r_cnt >= 3, (c[r_hi + 1] - c[np.minimum(r_lo, nb - 1)]) / np.maximum(r_cnt, 1), -1e9)
        return db[k] - np.maximum(left, right)

    prom = prom_at(ks)
    sel = np.nonzero(prom >= min_prom)[0]
    if len(sel) == 0:
        return []
    sel = sel[np.argsort(-prom[sel])][:top]
    out = []
    for i in sel:
        k = int(ks[i])
        a, b, cc = db[k - 1], db[k], db[k + 1]
        den = a - 2 * b + cc
        delta = 0.5 * (a - cc) / den if den < 0 else 0.0
        delta = max(-0.5, min(0.5, delta))
        kf = k + delta
        lvl = b - 0.25 * (a - cc) * delta
        harm = False
        for h in (2, 3):
            kh = int(round(h * kf))
            if kh + 3 < nb - 1:
                kk = kh - 2 + int(np.argmax(db[kh - 2:kh + 3]))
                if db[kk] > lvl - 30.0 and float(prom_at(kk)) >= 8.0:
                    harm = True
        sub = False
        for d in (2, 3):
            kd = int(round(kf / d))
            if kd >= 6:
                kk = kd - 1 + int(np.argmax(db[kd - 1:kd + 2]))
                if db[kk] > lvl - 6.0 and float(prom_at(kk)) >= 8.0:
                    sub = True
        out.append({"f": kf * df, "level": float(lvl), "prom": float(prom[i]),
                    "harm": harm, "sub": sub})
    return out


class ToneTracker:
    """Follows narrow peaks over time and scores how feedback-like they are."""

    def __init__(self, gap=0.3, dur_lo=0.25, dur_hi=1.2, dur_fb=0.5, dur_ring=0.3):
        self.gap, self.dur_lo, self.dur_hi = gap, dur_lo, dur_hi
        self.dur_fb, self.dur_ring = dur_fb, dur_ring
        self.tracks = []

    def update(self, cands, t, df, sens="normal"):
        hits = []
        for cnd in cands:
            best, best_d = None, 1e9
            for tr in self.tracks:
                d = abs(math.log2(cnd["f"] / tr["f"]))
                if (d < 1.0 / 30.0 or abs(cnd["f"] - tr["f"]) < 1.5 * df) and d < best_d:
                    best, best_d = tr, d
            if best is None:
                best = {"f": cnd["f"], "first": t, "last": t, "hist": deque(maxlen=96)}
                self.tracks.append(best)
            else:
                if best["last"] == t:
                    continue
                if t - best["last"] > self.gap:
                    best["first"] = t
                    best["hist"].clear()
                best["f"] += 0.3 * (cnd["f"] - best["f"])
                best["last"] = t
            best["hist"].append((t, cnd["level"], cnd["prom"], cnd["harm"], cnd["sub"]))
            hits.append(best)
        self.tracks = [tr for tr in self.tracks if t - tr["last"] <= 3.0]
        out = []
        for tr in hits:
            m = self._metrics(tr, t)
            m["status"], m["score"] = self._classify(m, sens)
            out.append(m)
        return out

    def _metrics(self, tr, t):
        pts = [h for h in tr["hist"] if t - h[0] <= 1.0]
        recent = [h for h in pts if t - h[0] <= 0.35] or pts[-1:]
        ts = np.array([h[0] for h in pts])
        lv = np.array([h[1] for h in pts])
        slope, std = 0.0, 0.0
        if len(pts) >= 4 and ts[-1] - ts[0] >= 0.2:
            cf = np.polyfit(ts - ts[0], lv, 1)
            slope = float(cf[0])
            std = float(np.std(lv - np.polyval(cf, ts - ts[0])))
        return {"f": tr["f"], "dur": tr["last"] - tr["first"], "level": float(lv[-1]),
                "prom": float(np.mean([h[2] for h in recent])), "slope": slope, "std": std,
                "harm": float(np.mean([h[3] for h in pts])),
                "sub": float(np.mean([h[4] for h in pts]))}

    def _classify(self, m, sens):
        p_lo, p_hi = SENS.get(sens, SENS["normal"])
        s_prom = min(1.0, max(0.0, (m["prom"] - p_lo) / (p_hi - p_lo)))
        s_dur = min(1.0, max(0.0, (m["dur"] - self.dur_lo) / (self.dur_hi - self.dur_lo)))
        steady = min(1.0, max(0.2, 1.0 - (m["std"] - 2.5) / 6.0))     # decaying hits and notes wobble
        pure = 1.0 if m["harm"] < 0.3 else 0.4                         # overtones = an instrument
        if m["sub"] > 0.5:
            pure *= 0.3                                                # it is itself an overtone
        grow = 1.15 if m["slope"] > 3.0 else (0.6 if m["slope"] < -15.0 else 1.0)
        score = min(1.0, (0.55 * s_prom + 0.45 * s_dur) * steady * pure * grow)
        if s_prom <= 0.0:
            return None, score
        if score >= 0.7 and m["dur"] >= self.dur_fb:
            return "feedback", score
        if score >= 0.42 and m["dur"] >= self.dur_ring:
            return "ringing", score
        return None, score


class Analyzer:
    TONE_HOLD = 20.0            # seconds a channel tone stays listed after it stops

    def __init__(self, sr, nch):
        self.sr = float(sr)
        self.nch = int(nch)
        self.scale = 1 if sr <= 50000 else (2 if sr <= 100000 else 4)
        self.ring_len = 262144 * self.scale
        self.buf = np.zeros((self.ring_len, self.nch), dtype=np.float32)
        self.written = 0
        # settings
        self.meas = min(1, self.nch - 1)
        self.ref = 0
        self.delay = 0
        self.avg_tau = 4.0          # None = accumulate
        self.rta_tau = 1.0
        self.sens = "normal"
        self.cal = None             # dB on GRID to subtract
        # levels
        self.peak = np.zeros(self.nch)
        self.peak_slow = np.zeros(self.nch)
        self.ms = np.zeros(self.nch)
        self.clip_t = np.full(self.nch, -1e9)
        self.clip_n = np.zeros(self.nch, dtype=np.int64)
        # transfer function tiers (long windows for lows, short for highs)
        self.tiers = []
        for n, f_lo, f_hi in ((32768, 0.0, 200.0), (8192, 200.0, 1600.0), (2048, 1600.0, 1e9)):
            n *= self.scale
            nb = n // 2 + 1
            self.tiers.append({"n": n, "hop": n // 2, "next": n, "cnt": 0, "f_lo": f_lo, "f_hi": f_hi,
                               "sxx": np.zeros(nb), "syy": np.zeros(nb),
                               "sxy": np.zeros(nb, dtype=np.complex128)})
        self._tf_edges = {}
        # RTA + detector
        self.rta_n = 8192 * self.scale
        self.rta_hop = self.rta_n // 4
        self.rta_next = self.rta_n
        nb = self.rta_n // 2 + 1
        self.rta_avg = np.zeros(nb)
        self.rta_peak = np.zeros(nb)
        self.rta_lt = np.zeros(nb)
        self.rta_det = np.zeros(nb)
        self.rta_cnt = 0
        self.tracker = ToneTracker()
        self.events = {}
        self._event_id = 0
        self.buildups = []
        self._buildup_next = 0.0
        # channel scan
        self.scan_n = 8192 * self.scale
        self.scan_next = self.scan_n
        self.scan_lt = np.zeros((self.scan_n // 2 + 1, self.nch))
        self.scan_min = np.full((self.scan_n // 2 + 1, self.nch), np.inf)
        self.scan_cnt = np.zeros(self.nch, dtype=np.int64)
        self.scan_trackers = [ToneTracker(gap=0.4, dur_lo=0.5, dur_hi=2.0, dur_fb=1.0, dur_ring=0.75)
                              for _ in range(self.nch)]
        self.scan_tones = [[] for _ in range(self.nch)]
        self.scan_ver = 0
        # delay finder
        self.delay_pending = False
        self.delay_result = None
        self.track = False          # keep the delay locked on by re-checking it every few seconds
        self.track_hold = False
        self.track_moves = 0
        self._track_next = 0

    # ------------------------------------------------------------------ ingest
    def ingest(self, block):
        n = block.shape[0]
        if block.shape[1] != self.nch:
            return
        i = self.written % self.ring_len
        if i + n <= self.ring_len:
            self.buf[i:i + n] = block
        else:
            k = self.ring_len - i
            self.buf[i:] = block[:k]
            self.buf[:n - k] = block[k:]
        self.written += n
        dt = n / self.sr
        ab = np.abs(block)
        pk = ab.max(axis=0)
        self.peak = np.maximum(self.peak * 10 ** (-12.0 * dt / 20), pk)
        self.peak_slow = np.maximum(self.peak_slow * 10 ** (-1.5 * dt / 20), pk)
        a = 1.0 - math.exp(-dt / 0.3)
        self.ms += a * ((block.astype(np.float64) ** 2).mean(axis=0) - self.ms)
        clipped = (ab >= 0.999).sum(axis=0)
        t = self.written / self.sr
        self.clip_t = np.where(clipped > 0, t, self.clip_t)
        self.clip_n += clipped
        self._proc_tf()
        self._proc_rta()
        self._proc_scan()
        if self.delay_pending:
            self._find_delay()
        elif self.track and not self.track_hold and self.meas != self.ref and self.written >= self._track_next:
            self._track_next = self.written + int(4.0 * self.sr)
            self._track_delay()

    def _read(self, ch, end, n):
        start = end - n
        if start < 0 or start < self.written - self.ring_len or end > self.written:
            return None
        i = start % self.ring_len
        if i + n <= self.ring_len:
            return self.buf[i:i + n, ch]
        return np.concatenate((self.buf[i:, ch], self.buf[:n - (self.ring_len - i), ch]))

    def _read_all(self, end, n):
        start = end - n
        if start < 0 or start < self.written - self.ring_len or end > self.written:
            return None
        i = start % self.ring_len
        if i + n <= self.ring_len:
            return self.buf[i:i + n]
        return np.concatenate((self.buf[i:], self.buf[:n - (self.ring_len - i)]))

    # ------------------------------------------------------- transfer function
    def reset_tf(self):
        for t in self.tiers:
            t["cnt"] = 0
            t["sxx"][:] = 0
            t["syy"][:] = 0
            t["sxy"][:] = 0
            t["next"] = max(t["n"], self.written)

    def _proc_tf(self):
        d = int(self.delay)
        dm, dr = max(0, -d), max(0, d)
        for t in self.tiers:
            n, hop = t["n"], t["hop"]
            if self.written - t["next"] > 6 * hop:
                t["next"] = self.written - 2 * hop
            it = 0
            while t["next"] <= self.written and it < 6:
                y = self._read(self.meas, t["next"] - dm, n)
                x = self._read(self.ref, t["next"] - dr, n)
                t["next"] += hop
                it += 1
                if x is None or y is None:
                    continue
                w = dsp.hann(n)
                xf = np.fft.rfft(x * w)
                yf = np.fft.rfft(y * w)
                if self.avg_tau is None:
                    a = 1.0 / (t["cnt"] + 1)
                else:
                    a = max(1.0 / (t["cnt"] + 1), 1.0 - math.exp(-(hop / self.sr) / self.avg_tau))
                t["sxx"] += a * (np.abs(xf) ** 2 - t["sxx"])
                t["syy"] += a * (np.abs(yf) ** 2 - t["syy"])
                t["sxy"] += a * (np.conj(xf) * yf - t["sxy"])
                t["cnt"] += 1

    def tf_snapshot(self, frac=48):
        """Composite magnitude (dB), phase (deg) and coherence on dsp.GRID."""
        g = dsp.GRID
        mag = np.full(len(g), np.nan)
        ph = np.full(len(g), np.nan)
        coh = np.full(len(g), np.nan)
        cnt = min(t["cnt"] for t in self.tiers)
        for ti, t in enumerate(self.tiers):
            if t["cnt"] == 0:
                continue
            sel = (g >= t["f_lo"]) & (g < t["f_hi"]) & (g < self.sr / 2 * 0.98)
            if not sel.any():
                continue
            key = (ti, frac)
            if key not in self._tf_edges:
                df = self.sr / t["n"]
                self._tf_edges[key] = dsp.band_edges(g[sel], df, frac, len(t["sxx"]))
            lo, hi, _ = self._tf_edges[key]
            xx = dsp.band_sum(dsp.cum(t["sxx"]), lo, hi)
            yy = dsp.band_sum(dsp.cum(t["syy"]), lo, hi)
            cr = dsp.band_sum(dsp.cum(t["sxy"].real), lo, hi)
            ci = dsp.band_sum(dsp.cum(t["sxy"].imag), lo, hi)
            c2 = cr * cr + ci * ci
            xx = np.maximum(xx, 1e-30)
            mag[sel] = 10.0 * np.log10(np.maximum(c2, 1e-60) / (xx * xx))
            ph[sel] = np.degrees(np.arctan2(ci, cr))
            coh[sel] = np.clip(c2 / (xx * np.maximum(yy, 1e-30)), 0.0, 1.0)
        if self.cal is not None:
            mag = mag - self.cal
        band = (g >= 100) & (g <= 10000) & np.isfinite(coh)
        quality = float(np.mean(coh[band])) if band.any() else 0.0
        return {"mag": mag, "phase": ph, "coh": coh, "quality": quality, "frames": int(cnt)}

    # ------------------------------------------------------------ delay finder
    def _measure_delay(self):
        """Arrival time of the measurement signal relative to the reference, from the last few seconds."""
        n = 65536 * self.scale
        if self.written < 2 * n:
            return None
        sxy = np.zeros(n // 2 + 1, dtype=np.complex128)
        sxx = np.zeros(n // 2 + 1)
        for j in range(3):
            end = self.written - j * (n // 2)
            x = self._read(self.ref, end, n)
            y = self._read(self.meas, end, n)
            if x is None or y is None:
                continue
            xf = np.fft.rfft(x.astype(np.float64))
            yf = np.fft.rfft(y.astype(np.float64))
            sxy += np.conj(xf) * yf
            sxx += np.abs(xf) ** 2
        f = np.fft.rfftfreq(n, 1.0 / self.sr)
        wgt = ((f >= 60) & (f <= 12000)).astype(np.float64)
        med = float(np.median(sxx[wgt > 0])) if sxx.any() else 0.0
        if med <= 0:
            return {"ok": False, "msg": "No reference signal. Check the reference channel."}
        h = np.fft.irfft(wgt * sxy / (sxx + 0.01 * med), n)
        idx = int(np.argmax(np.abs(h)))
        pk = h[idx]
        rms = float(np.sqrt(np.mean(h * h)))
        conf = 20 * math.log10(abs(pk) / max(rms, 1e-30))
        d = idx if idx < n // 2 else idx - n
        ok = conf >= 20.0
        return {"ok": ok, "samples": d, "ms": 1000.0 * d / self.sr,
                "m": 343.0 * d / self.sr, "polarity": 1 if pk >= 0 else -1,
                "conf": conf,
                "msg": "" if ok else "Weak correlation. Raise the signal level or check routing."}

    def _find_delay(self):
        res = self._measure_delay()
        if res is None:
            return
        self.delay_pending = False
        self.delay_result = res
        if res["ok"]:
            self.delay = res["samples"]
            self.reset_tf()

    def _track_delay(self):
        """Follow the delay as the mic moves. Only a confident, real change restarts the average."""
        res = self._measure_delay()
        if res is None or not res["ok"]:
            return
        if abs(res["samples"] - self.delay) >= 3:
            self.delay = res["samples"]
            self.delay_result = res
            self.track_moves += 1
            self.reset_tf()

    # --------------------------------------------------------- RTA + detection
    def _proc_rta(self):
        n, hop = self.rta_n, self.rta_hop
        if self.written - self.rta_next > 8 * hop:
            self.rta_next = self.written - 2 * hop
        it = 0
        while self.rta_next <= self.written and it < 8:
            x = self._read(self.meas, self.rta_next, n)
            self.rta_next += hop
            it += 1
            if x is None:
                continue
            p = np.abs(np.fft.rfft(x * dsp.hann(n))) ** 2
            dt = hop / self.sr
            c = self.rta_cnt
            a = max(1.0 / (c + 1), 1.0 - math.exp(-dt / self.rta_tau))
            self.rta_avg += a * (p - self.rta_avg)
            a = max(1.0 / (c + 1), 1.0 - math.exp(-dt / 8.0))
            self.rta_lt += a * (p - self.rta_lt)
            self.rta_peak = np.maximum(self.rta_peak * 10 ** (-2.0 * dt / 10), p)
            self.rta_det += max(1.0 / (c + 1), 0.35) * (p - self.rta_det)
            self.rta_cnt += 1
            t = self.rta_next / self.sr
            self._detect(t)
            if t >= self._buildup_next:
                self._buildup_next = t + 1.0
                self._find_buildups()

    def _detect(self, t):
        df = self.sr / self.rta_n
        p_lo = SENS.get(self.sens, SENS["normal"])[0]
        cands = find_peaks(self.rta_det, self.rta_n, self.sr, min_prom=p_lo, top=10)
        now = time.time()
        for m in self.tracker.update(cands, t, df, self.sens):
            if m["status"]:
                self._log_event(m, t, now)
        for ev in self.events.values():
            if ev["active"] and t - ev["_t"] > 0.5:
                ev["active"] = False
        if len(self.events) > 40:
            old = sorted((e for e in self.events.items() if not e[1]["active"]), key=lambda e: e[1]["last"])
            for k, _ in old[:len(self.events) - 40]:
                del self.events[k]

    def _log_event(self, m, t, now):
        key = int(round(math.log2(m["f"] / 20.0) * 24))
        ev = None
        for k in (key, key - 1, key + 1):
            if k in self.events:
                ev, key = self.events[k], k
                break
        if ev is None:
            self._event_id += 1
            ev = {"id": self._event_id, "f": m["f"], "status": m["status"], "first": now, "hits": 0,
                  "active": False, "level": m["level"], "max_level": m["level"], "src": {}}
            self.events[key] = ev
        if not ev["active"]:
            ev["hits"] += 1
        if m["status"] == "feedback":
            ev["status"] = "feedback"
        ev.update(f=m["f"], now_status=m["status"], score=m["score"], level=m["level"],
                  prom=m["prom"], slope=m["slope"], last=now, active=True, _t=t)
        ev["max_level"] = max(ev["max_level"], m["level"])

    def clear_events(self):
        self.events = {}
        self.buildups = []

    def _band_db(self, p, n_fft, frac):
        df = self.sr / n_fft
        g = dsp.GRID
        lo, hi, sc = dsp.band_edges(g, df, frac, len(p))
        s = dsp.band_sum(dsp.cum(p), lo, hi) * sc
        out = dsp.db10(s * 32.0 / (3.0 * n_fft * n_fft))
        out[g > self.sr / 2 * 0.98] = np.nan
        return out

    def rta_snapshot(self, frac=24):
        avg = self._band_db(self.rta_avg, self.rta_n, frac)
        peak = self._band_db(self.rta_peak, self.rta_n, frac)
        if self.cal is not None:
            avg, peak = avg - self.cal, peak - self.cal
        return {"avg": avg, "peak": peak}

    def _find_buildups(self):
        self.buildups = []
        if self.rta_cnt < int(3.0 * self.sr / self.rta_hop):
            return
        self.buildups = spectral_buildups(self._band_db(self.rta_lt, self.rta_n, 3), 60.0, 10000.0,
                                          min_excess=3.5, top=3,
                                          skip=[e["f"] for e in self.events.values()])

    # ------------------------------------------------------------ channel scan
    def _proc_scan(self):
        if self.written < self.scan_next:
            return
        n = self.scan_n
        end = self.written
        step = int(0.25 * self.sr)
        dt = (end - (self.scan_next - step)) / self.sr
        self.scan_next = end + step
        x = self._read_all(end, n)
        if x is None:
            return
        frame_ms = (x.astype(np.float64) ** 2).mean(axis=0)
        p = np.abs(np.fft.rfft(x * dsp.hann(n)[:, None], axis=0)) ** 2
        t = end / self.sr
        df = self.sr / n
        for c in range(self.nch):
            if frame_ms[c] < 1e-7:
                self.scan_tones[c] = [tn for tn in self.scan_tones[c] if t - tn["_t"] < self.TONE_HOLD]
                continue
            a = max(1.0 / (self.scan_cnt[c] + 1), min(1.0, dt / 6.0))
            self.scan_lt[:, c] += a * (p[:, c] - self.scan_lt[:, c])
            self.scan_min[:, c] = np.minimum(self.scan_min[:, c] * 1.05, p[:, c])
            self.scan_cnt[c] += 1
            cands = find_peaks(p[:, c], n, self.sr, min_prom=12.0, floor_db=-75.0, top=4)
            tones = []
            for m in self.scan_trackers[c].update(cands, t, df, "low"):
                if m["status"]:
                    m["_t"] = t
                    tones.append(m)
            for old in self.scan_tones[c]:
                for m in tones:                      # once feedback, it stays listed as feedback
                    if abs(math.log2(old["f"] / m["f"])) < 1 / 24 and old["status"] == "feedback":
                        m["status"] = "feedback"
            keep = [tn for tn in self.scan_tones[c] if t - tn["_t"] < self.TONE_HOLD and
                    not any(abs(math.log2(tn["f"] / m["f"])) < 1 / 24 for m in tones)]
            self.scan_tones[c] = tones + keep
        for ev in self.events.values():
            if ev["active"]:
                for c, lvl in self.channels_with_tone(ev["f"]):
                    ev["src"][c] = max(ev["src"].get(c, -200.0), lvl)
        self.scan_ver += 1

    def channels_with_tone(self, f):
        out = []
        t = self.written / self.sr
        for c in range(self.nch):
            if c in (self.meas, self.ref):
                continue
            for tn in self.scan_tones[c]:
                if t - tn["_t"] < 1.5 and abs(math.log2(tn["f"] / f)) < 1.0 / 40.0:
                    out.append((c, tn["level"]))
                    break
        out.sort(key=lambda e: -e[1])
        return out


def spectral_buildups(band_db, f_lo, f_hi, min_excess=5.0, top=3, skip=()):
    """Third-octave regions that stand clear of BOTH flanks (0.4-1 octave away) on GRID."""
    g = dsp.GRID
    ok = np.isfinite(band_db)
    if not ok.any():
        return []
    top_db = float(np.nanmax(band_db))
    lv = np.where(ok, np.maximum(band_db, top_db - 60.0), top_db - 60.0)
    c = np.concatenate(([0.0], np.cumsum(lv)))
    n = len(g)
    i = np.arange(n)
    a, b = int(round(0.4 * dsp.PPO)), int(round(1.0 * dsp.PPO))

    def mean_between(lo, hi):
        lo = np.clip(lo, 0, n)
        hi = np.clip(hi, 0, n)
        cnt = np.maximum(hi - lo, 1)
        return np.where(hi > lo, (c[hi] - c[lo]) / cnt, -1e9)

    left = mean_between(i - b, i - a + 1)
    right = mean_between(i + a, i + b + 1)
    ex = lv - np.maximum(left, right)
    edge = (i - b < 0) | (i + b + 1 > n)
    active = ok & ~edge & (g >= f_lo) & (g <= f_hi) & (band_db > top_db - 40.0)
    out = []
    for k in np.nonzero(active & (ex >= min_excess))[0]:
        if ex[k] < np.max(ex[max(0, k - 16):k + 17]):
            continue
        if any(abs(math.log2(g[k] / s)) < 1.0 / 4.0 for s in skip):
            continue
        out.append({"f": float(g[k]), "excess": float(ex[k]), "level": float(band_db[k])})
    out.sort(key=lambda e: -e["excess"])
    return out[:top]
