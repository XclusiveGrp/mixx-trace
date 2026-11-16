# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP)
"""Time and polarity alignment of two systems (subs to mains, fills to mains) measured one at a time.

Each capture is a transfer function taken with one system soloed, against the same reference and
with the mic in the same place. The delay and polarity that make the two add best are found where
both systems are actually producing sound: the overlap, or crossover, region.
"""
import math

import numpy as np

from . import dsp

C_SOUND = 343.0
SPAN = 0.030                # search +/- this many seconds around the measured arrival difference


def to_complex(mag_db, phase_deg):
    h = 10.0 ** (np.asarray(mag_db, dtype=np.float64) / 20.0) * np.exp(1j * np.radians(np.asarray(phase_deg, dtype=np.float64)))
    return np.where(np.isfinite(h), h, 0.0)


def overlap_mask(a, b, coh_a, coh_b, window_db=12.0, min_coh=0.6):
    ma, mb = np.abs(a), np.abs(b)
    ok = (ma > 0) & (mb > 0) & (np.nan_to_num(coh_a) >= min_coh) & (np.nan_to_num(coh_b) >= min_coh)
    with np.errstate(divide="ignore", invalid="ignore"):
        diff = np.abs(20.0 * np.log10(np.maximum(ma, 1e-12) / np.maximum(mb, 1e-12)))
    ok &= diff <= window_db
    # ignore stray single points; keep the widest continuous run plus runs at least a sixth of an octave wide
    runs, start = [], None
    for i, v in enumerate(ok):
        if v and start is None:
            start = i
        if (not v or i == len(ok) - 1) and start is not None:
            end = i if not v else i + 1
            runs.append((start, end))
            start = None
    keep = np.zeros(len(ok), dtype=bool)
    for s, e in runs:
        if e - s >= dsp.PPO // 6:
            keep[s:e] = True
    return keep


def _db(x):
    return 20.0 * np.log10(np.maximum(np.abs(x), 1e-9))


def solve(cap_a, cap_b):
    """cap = {mag, phase, coh (on dsp.GRID), delay (samples of reference delay used), sr}.

    Returns the delay to add to one system and whether to flip B, with predicted traces.
    """
    g = dsp.GRID
    sr = float(cap_a["sr"])
    a = to_complex(cap_a["mag"], cap_a["phase"])
    b = to_complex(cap_b["mag"], cap_b["phase"])
    coh_a, coh_b = np.asarray(cap_a["coh"], dtype=np.float64), np.asarray(cap_b["coh"], dtype=np.float64)
    # put B on A's time base: the analyzer removed a different delay from each measurement
    base = (cap_b["delay"] - cap_a["delay"]) / sr
    w = 2.0 * np.pi * g
    b_now = b * np.exp(-1j * w * base)
    m = overlap_mask(a, b, coh_a, coh_b)
    n_pts = int(m.sum())
    if n_pts < dsp.PPO // 4:
        return {"ok": False, "msg": "The two systems do not overlap enough to align by phase. Check that both captures "
                                    "are clean (good coherence) and that each system was soloed for its own capture."}
    f_lo, f_hi = float(g[m][0]), float(g[m][-1])
    cross = a[m] * np.conj(b[m])                     # per frequency, how A and B (as measured) relate
    step = 1.0 / (2.0 * sr)
    lo, hi = -SPAN, SPAN                             # extra delay on B, on the analyzer's time base
    if not (cap_a.get("locked", True) and cap_b.get("locked", True)):
        # one capture could not lock its own arrival (typical for subs), so its travel time is still in the data
        lo, hi = max(min(0.0, base) - SPAN, -0.25), min(max(0.0, base) + SPAN, 0.25)
    y = np.arange(lo, hi + step / 2, step)
    z = np.empty(len(y), dtype=np.complex128)        # complex correlation over the overlap band
    for k in range(0, len(y), 1024):
        z[k:k + 1024] = np.exp(1j * np.outer(y[k:k + 1024], w[m])) @ cross
    c = z.real
    env = np.abs(z)
    i_env = int(np.argmax(env))

    def peak_near(sign):
        """Nearest local maximum of sign*c to the envelope peak."""
        s = sign * c
        best = None
        for d in range(0, len(y)):
            for i in (i_env - d, i_env + d):
                if 1 <= i < len(y) - 1 and s[i] >= s[i - 1] and s[i] >= s[i + 1] and s[i] > 0:
                    best = i
                    break
            if best is not None:
                break
        return best

    energy = float(np.sum(np.abs(a[m]) ** 2 + np.abs(b[m]) ** 2))
    cands = []
    for sign in (1, -1):
        i = peak_near(sign)
        if i is not None:
            level = 10.0 * math.log10(max(energy + 2 * sign * c[i], 1e-18))      # summed level over the band, dB
            cands.append({"pol": sign, "i": i, "level": level, "far": abs(i - i_env)})
    if not cands:
        return {"ok": False, "msg": "Could not find a clear alignment. Capture both systems again with more signal."}
    cands.sort(key=lambda cd: -cd["level"])
    pick, other = cands[0], (cands[1] if len(cands) > 1 else None)
    close = other is not None and pick["level"] - other["level"] < 0.5
    if close:
        # Across a narrow crossover a polarity flip and half a cycle of delay add up almost the same.
        # Take the one whose timing also matches (phase slopes parallel); if that is a toss-up too, leave polarity alone.
        if other["far"] < pick["far"] or (other["far"] == pick["far"] and other["pol"] > 0):
            pick, other = other, pick
    pol, i = pick["pol"], pick["i"]
    s = pol * c
    den = s[i - 1] - 2 * s[i] + s[i + 1]
    frac = 0.5 * (s[i - 1] - s[i + 1]) / den if den < 0 else 0.0
    y_best = float(y[i] + frac * step)

    x = y_best - base                                # delay to add to B in the real world (negative: delay A)
    alt = None
    if close:
        xo = float(y[other["i"]]) - base
        alt = {"who": "b" if xo >= 0 else "a", "ms": round(1000.0 * abs(xo), 2), "invert": other["pol"] < 0,
               "worse": round(max(pick["level"] - other["level"], 0.0), 2)}
    who = "b" if x >= 0 else "a"
    amount = abs(x)
    b_fix = pol * b * np.exp(-1j * w * y_best)
    sum_now, sum_fix = a + b_now, a + b_fix
    ideal = np.abs(a) + np.abs(b)

    def band_db(v):
        return 10.0 * math.log10(max(float(np.mean(np.abs(v[m]) ** 2)), 1e-18))

    lvl_now, lvl_fix, lvl_ideal = band_db(sum_now), band_db(sum_fix), band_db(ideal)
    valid = (np.abs(a) > 0) | (np.abs(b) > 0)

    def trace(v):
        return np.where(valid, _db(v), np.nan)

    def ph(v, coh):
        return np.where((np.abs(v) > 0) & (np.nan_to_num(coh) >= 0.3), np.degrees(np.angle(v)), np.nan)

    # phase is drawn with the common travel time removed, so the eye can compare slopes
    return {
        "ok": True, "who": who, "ms": 1000.0 * amount, "m": C_SOUND * amount, "ft": C_SOUND * amount / 0.3048,
        "samples": amount * sr, "invert": pol < 0, "f_lo": f_lo, "f_hi": f_hi,
        "gain": lvl_fix - lvl_now, "short": lvl_ideal - lvl_fix, "now_short": lvl_ideal - lvl_now,
        "offset_ms": 1000.0 * x, "alt": alt,
        "a": trace(a), "b": trace(b), "sum_now": trace(sum_now), "sum_fix": trace(sum_fix),
        "ph_a": ph(a, coh_a), "ph_b": ph(b_now, coh_b), "ph_b_fix": ph(b_fix, coh_b),
        "coh_a": coh_a, "coh_b": coh_b,
    }
