"""Shared DSP helpers: log-frequency grid, band sums, filters, formatting."""
import math
import numpy as np

PPO = 48                       # grid points per octave
F0 = 20.0
N_GRID = int(math.floor(PPO * math.log2(20000.0 / F0))) + 1
GRID = F0 * 2.0 ** (np.arange(N_GRID) / PPO)

_hann_cache = {}


def hann(n):
    w = _hann_cache.get(n)
    if w is None:
        w = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float64)
        _hann_cache[n] = w
    return w


def cum(s):
    """Cumulative sum with a leading zero; bin k covers [k-0.5, k+0.5)."""
    return np.concatenate(([0.0], np.cumsum(s)))


def band_sum(c, lo, hi):
    """Sum of spectrum between fractional bin edges lo..hi using cum() output."""
    x = np.arange(len(c))
    return np.interp(hi + 0.5, x, c) - np.interp(lo + 0.5, x, c)


def band_edges(freqs, df, frac, nbins, min_bins=1.0):
    """Fractional bin edges for 1/frac-octave bands centred on freqs.

    Returns (lo, hi, scale) where scale corrects power sums when a band had to
    be widened to min_bins.
    """
    hw = 1.0 / (2.0 * frac)
    lo = freqs * 2.0 ** (-hw) / df
    hi = freqs * 2.0 ** (hw) / df
    true_w = hi - lo
    narrow = true_w < min_bins
    mid = 0.5 * (lo + hi)
    lo = np.where(narrow, mid - min_bins / 2, lo)
    hi = np.where(narrow, mid + min_bins / 2, hi)
    lo = np.clip(lo, 0.5, nbins - 1.5)
    hi = np.clip(hi, 0.5, nbins - 1.5)
    used = np.maximum(hi - lo, 1e-9)
    scale = np.minimum(true_w / used, 1.0)
    return lo, hi, scale


def db10(x):
    return 10.0 * np.log10(np.maximum(x, 1e-30))


def bell_db(f, f0, gain_db, q):
    """Magnitude (dB) of an analogue-prototype peaking filter."""
    a = 10.0 ** (gain_db / 40.0)
    x = np.asarray(f, dtype=np.float64) / f0
    d = (1.0 - x * x) ** 2
    num = d + (x * a / q) ** 2
    den = d + (x / (a * q)) ** 2
    return 10.0 * np.log10(num / den)


def smooth_db(arr, octaves):
    """Moving average of a dB curve on GRID over a window `octaves` wide. NaN-aware."""
    arr = np.asarray(arr, dtype=np.float64)
    half = max(0, int(round(octaves * PPO / 2)))
    if half == 0:
        return arr.copy()
    ok = np.isfinite(arr)
    v = np.where(ok, arr, 0.0)
    cv = np.concatenate(([0.0], np.cumsum(v)))
    cn = np.concatenate(([0.0], np.cumsum(ok.astype(np.float64))))
    i = np.arange(len(arr))
    lo = np.maximum(i - half, 0)
    hi = np.minimum(i + half + 1, len(arr))
    n = cn[hi] - cn[lo]
    out = np.where(n > 0, (cv[hi] - cv[lo]) / np.maximum(n, 1), np.nan)
    return out


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(f):
    if f <= 0:
        return ""
    midi = 69 + 12 * math.log2(f / 440.0)
    n = int(round(midi))
    cents = int(round((midi - n) * 100))
    name = NOTE_NAMES[n % 12] + str(n // 12 - 1)
    return "%s %+dc" % (name, cents) if cents else name


ISO_THIRD = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630,
             800, 1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000,
             12500, 16000, 20000]


def iso_band(f):
    return min(ISO_THIRD, key=lambda b: abs(math.log(f / b)))


def fmt_hz(f):
    if f >= 10000:
        return "%.1f kHz" % (f / 1000.0)
    if f >= 1000:
        return "%.2f kHz" % (f / 1000.0)
    if f >= 100:
        return "%.0f Hz" % f
    return "%.1f Hz" % f


# ---- digital biquad responses (RBJ cookbook), evaluated on a frequency vector ----

def _biquad_response(b, a, f, sr):
    w = 2 * np.pi * np.asarray(f, dtype=np.float64) / sr
    z1 = np.exp(-1j * w)
    z2 = z1 * z1
    return (b[0] + b[1] * z1 + b[2] * z2) / (a[0] + a[1] * z1 + a[2] * z2)


def rbj(kind, f0, sr, q=0.707, gain_db=0.0):
    a_ = 10.0 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / sr
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "peak":
        b = [1 + alpha * a_, -2 * cw, 1 - alpha * a_]
        a = [1 + alpha / a_, -2 * cw, 1 - alpha / a_]
    elif kind == "hp":
        b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "lp":
        b = [(1 - cw) / 2, 1 - cw, (1 - cw) / 2]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "bp":
        b = [alpha, 0.0, -alpha]
        a = [1 + alpha, -2 * cw, 1 - alpha]
    elif kind == "hs":
        s = 2 * math.sqrt(a_) * alpha
        b = [a_ * ((a_ + 1) + (a_ - 1) * cw + s), -2 * a_ * ((a_ - 1) + (a_ + 1) * cw),
             a_ * ((a_ + 1) + (a_ - 1) * cw - s)]
        a = [(a_ + 1) - (a_ - 1) * cw + s, 2 * ((a_ - 1) - (a_ + 1) * cw), (a_ + 1) - (a_ - 1) * cw - s]
    elif kind == "ls":
        s = 2 * math.sqrt(a_) * alpha
        b = [a_ * ((a_ + 1) - (a_ - 1) * cw + s), 2 * a_ * ((a_ - 1) - (a_ + 1) * cw),
             a_ * ((a_ + 1) - (a_ - 1) * cw - s)]
        a = [(a_ + 1) + (a_ - 1) * cw + s, -2 * ((a_ - 1) + (a_ + 1) * cw), (a_ + 1) + (a_ - 1) * cw - s]
    else:
        raise ValueError(kind)
    return b, a


def chain_response(specs, f, sr):
    """Complex response of a cascade of ('kind', f0, q, gain_db) biquads."""
    h = np.ones(len(f), dtype=np.complex128)
    for kind, f0, q, g in specs:
        b, a = rbj(kind, f0, sr, q, g)
        h *= _biquad_response(b, a, f, sr)
    return h
