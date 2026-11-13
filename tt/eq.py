"""Target curves and automatic parametric EQ fitting for system tuning."""
import math
import numpy as np

from . import dsp

TARGET_PRESETS = {
    "flat": {"label": "Flat", "low": 0.0, "tilt": 0.0},
    "concert": {"label": "Concert (sub contour, soft top)", "low": 8.0, "tilt": -0.75},
    "warm": {"label": "Warm", "low": 4.0, "tilt": -1.0},
    "speech": {"label": "Speech / corporate", "low": 0.0, "tilt": -0.5},
}


def target_curve(low_db=0.0, tilt=0.0, low_corner=100.0, tilt_corner=2000.0):
    """Low-frequency contour (dB below low_corner) plus HF tilt (dB/oct above tilt_corner)."""
    g = dsp.GRID
    shelf = low_db * 0.5 * (1.0 - np.tanh(1.6 * np.log2(g / low_corner)))
    octs = np.log2(g / tilt_corner)
    knee = 0.5 * (octs + np.sqrt(octs * octs + 0.25))      # smooth max(0, octs)
    return shelf + tilt * knee


def _cost(resid, w):
    asym = np.where(resid < 0, 0.4, 1.0)                    # go easy on dips: often cancellations
    return float(np.sum(w * asym * resid * resid))


def usable_range(mag, coh=None, coh_min=0.5, drop=6.0):
    """Where the system is actually producing output: stop fitting once it rolls off."""
    g = dsp.GRID
    sm = dsp.smooth_db(mag, 1.0 / 3.0)
    core = (g >= 200) & (g <= 4000) & np.isfinite(sm)
    if not core.any():
        return 40.0, 16000.0
    ref = float(np.median(sm[core]))
    good = np.isfinite(sm) & (sm > ref - drop)
    if coh is not None:
        good &= np.where(np.isfinite(coh), coh, 0.0) >= coh_min * 0.6
    mid = int(np.argmin(np.abs(g - 1000.0)))
    lo = mid
    while lo > 0 and (good[lo - 1] or (lo > 8 and good[lo - 8:lo].any() and g[lo] > 150)):
        lo -= 1
    hi = mid
    while hi < len(g) - 1 and (good[hi + 1] or (hi < len(g) - 9 and good[hi + 1:hi + 9].any() and g[hi] < 6000)):
        hi += 1
    f_lo = float(min(max(g[lo] * 1.2, 25.0), 200.0))
    f_hi = float(max(min(g[hi] / 1.1, 18000.0), 4000.0))
    return f_lo, f_hi


def auto_eq(mag, coh, target, f_lo=40.0, f_hi=16000.0, max_bands=8, max_boost=3.0, max_cut=12.0,
            q_min=0.5, q_max=10.0, coh_min=0.5):
    """Fit up to max_bands bell filters so mag + EQ follows target.

    Returns dict with bands, eq curve, predicted response, level offset and error stats.
    """
    g = dsp.GRID
    mag = np.asarray(mag, dtype=np.float64)
    ok = np.isfinite(mag) & (g >= f_lo) & (g <= f_hi)
    if coh is not None:
        c = np.asarray(coh, dtype=np.float64)
        ok &= np.where(np.isfinite(c), c, 0.0) >= coh_min
    if ok.sum() < 24:
        return {"ok": False, "msg": "Not enough trustworthy data in range. Improve coherence or widen the range."}
    w = ok.astype(np.float64)
    err0 = np.where(ok, mag - target, 0.0)
    offset = float(np.median(err0[ok]))
    err0 = np.where(ok, err0 - offset, 0.0)
    sm = dsp.smooth_db(np.where(ok, err0, np.nan), 1.0 / 6.0)
    sm = np.where(np.isfinite(sm), sm, 0.0)
    lg = np.log2(g)

    def curve(bands):
        out = np.zeros(len(g))
        for f0, gn, q in bands:
            out += dsp.bell_db(g, f0, gn, q)
        return out

    def clamp(f0, gn, q):
        gn = min(max(gn, -max_cut), max_boost)
        top_q = q_max if gn < 0 else min(q_max, 4.0)        # boosts stay broad: never fill a narrow hole
        return (min(max(f0, f_lo), f_hi), gn, min(max(q, q_min), top_q))

    def trusted(f0, q):
        """At least half of the band's own width must sit on data we believe."""
        bw = 2.0 * math.asinh(1.0 / (2.0 * q)) / math.log(2)
        inside = np.abs(lg - math.log2(f0)) <= bw / 2.0
        return bool(inside.any()) and float(ok[inside].mean()) >= 0.5

    def refine(bands, bi, rounds=14):
        steps = [1.0 / 12.0, 1.0, 1.35]                     # octaves, dB, Q ratio
        best = _cost(sm + curve(bands), w)
        for _ in range(rounds):
            improved = False
            for p in range(3):
                for sgn in (1, -1):
                    f0, gn, q = bands[bi]
                    if p == 0:
                        f0 *= 2.0 ** (sgn * steps[0])
                    elif p == 1:
                        gn += sgn * steps[1]
                    else:
                        q *= steps[2] ** sgn
                    trial = list(bands)
                    trial[bi] = clamp(f0, gn, q)
                    if not trusted(trial[bi][0], trial[bi][2]):
                        continue
                    cst = _cost(sm + curve(trial), w)
                    if cst < best - 1e-9:
                        best, bands, improved = cst, trial, True
            if not improved:
                steps = [steps[0] / 2, steps[1] / 2, 1 + (steps[2] - 1) / 2]
                if steps[1] < 0.1:
                    break
        return bands

    bands = []
    blocked = np.zeros(len(g), dtype=bool)
    for _ in range(max_bands * 2):
        if len(bands) >= max_bands:
            break
        r = sm + curve(bands)
        score = np.abs(r) * np.where(r < 0, 0.5, 1.0) * w
        score[blocked] = 0
        i = int(np.argmax(score))
        if abs(r[i]) < 1.0 or score[i] <= 0:
            break
        gn = min(max(-r[i], -max_cut), max_boost)
        half = r[i] / 2.0
        il = i
        while il > 0 and (r[il] - half) * np.sign(r[i]) > 0:
            il -= 1
        ir = i
        while ir < len(g) - 1 and (r[ir] - half) * np.sign(r[i]) > 0:
            ir += 1
        bw = max((ir - il) / dsp.PPO, 0.1)
        q = 1.0 / (2.0 * math.sinh(math.log(2) / 2.0 * bw))
        cand = clamp(float(g[i]), gn, q)
        before = _cost(r, w)
        trial = refine(bands + [cand], len(bands))
        after = _cost(sm + curve(trial), w)
        near = np.abs(lg - lg[i]) < 1.0 / 6.0
        crowd = any(abs(math.log2(trial[-1][0] / b[0])) < 1.0 / 3.0 for b in bands)
        if abs(trial[-1][1]) < 0.75 or after > before * 0.97 or crowd or not trusted(trial[-1][0], trial[-1][2]):
            blocked |= near
            continue
        bands = trial
    for _ in range(2):
        for bi in range(len(bands)):
            bands = refine(bands, bi, rounds=8)
    bands = [b for b in bands if abs(b[1]) >= 0.5]
    bands.sort(key=lambda b: b[0])
    eq = curve(bands)
    resid_before = err0[ok]
    resid_after = (err0 + eq)[ok]
    return {
        "ok": True,
        "bands": [{"f": round(b[0], 1), "gain": round(b[1] * 2) / 2.0, "q": round(b[2], 2)} for b in bands],
        "eq": eq,
        "predicted": mag + eq,
        "offset": offset,
        "rms_before": float(np.sqrt(np.mean(resid_before ** 2))),
        "rms_after": float(np.sqrt(np.mean(resid_after ** 2))),
        "f_lo": f_lo, "f_hi": f_hi,
    }
