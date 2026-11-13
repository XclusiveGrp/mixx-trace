"""Turns channel measurements into console-ready suggestions."""
import math
import numpy as np

from . import dsp
from .analyzer import spectral_buildups

CONSOLES = {
    "q326": {
        "label": "DiGiCo Quantum 326",
        "in_bands": 4, "out_bands": 8, "gain": 18.0, "q_min": 0.1, "q_max": 20.0,
        "f_min": 20.0, "f_max": 20000.0, "hpf": "HPF, 24 dB/oct", "usb_ch": 48,
        "band_names": ["Band 1 (Low)", "Band 2 (Lo-Mid)", "Band 3 (Hi-Mid)", "Band 4 (High)"],
        "dyn_eq": True, "deesser": "Dyn 1 in De-esser mode",
        "out_note": "Output busses have 8 EQ bands; inputs have 4, each switchable to dynamic.",
    },
    "generic": {
        "label": "Generic 4-band console",
        "in_bands": 4, "out_bands": 6, "gain": 15.0, "q_min": 0.3, "q_max": 12.0,
        "f_min": 20.0, "f_max": 20000.0, "hpf": "HPF", "usb_ch": 32,
        "band_names": ["Band 1", "Band 2", "Band 3", "Band 4"],
        "dyn_eq": False, "deesser": "a de-esser",
        "out_note": "Check how many output EQ bands your console offers.",
    },
}

SOURCE_TYPES = {
    "auto": {"label": "Unassigned", "hpf": 40, "low_skip": 80},
    "kick": {"label": "Kick", "hpf": 30, "low_skip": 150},
    "snare": {"label": "Snare", "hpf": 90, "low_skip": 250},
    "toms": {"label": "Toms", "hpf": 60, "low_skip": 200},
    "overhead": {"label": "Overheads / cymbals", "hpf": 150, "low_skip": 200},
    "bass": {"label": "Bass", "hpf": 30, "low_skip": 150},
    "egtr": {"label": "Electric guitar", "hpf": 90, "low_skip": 120},
    "agtr": {"label": "Acoustic guitar", "hpf": 90, "low_skip": 120},
    "keys": {"label": "Keys / synth", "hpf": 40, "low_skip": 100},
    "vocal": {"label": "Vocal", "hpf": 100, "low_skip": 130},
    "speech": {"label": "Speech / lectern", "hpf": 120, "low_skip": 150},
    "horns": {"label": "Horns / strings", "hpf": 120, "low_skip": 150},
    "playback": {"label": "Playback / tracks", "hpf": 30, "low_skip": 100},
}

_GUESS = [
    ("kick", ("kick", "kik", "bd ", "bass drum")), ("snare", ("snare", "sn ", "snr")),
    ("toms", ("tom", "floor", "rack")), ("overhead", ("oh", "overhead", "hat", "hh", "ride", "cym")),
    ("bass", ("bass", "sub")), ("agtr", ("acoustic", "ac gtr", "agtr", "ac. g")),
    ("egtr", ("gtr", "guitar")), ("keys", ("key", "piano", "synth", "organ", "rhodes")),
    ("speech", ("lectern", "podium", "speech", "pastor", "lav", "mc ")),
    ("vocal", ("vox", "voc", "bv", "sing", "choir")), ("horns", ("horn", "sax", "trumpet", "tpt", "violin", "string")),
    ("playback", ("track", "trk", "playback", "pb", "click", "laptop", "dj")),
]


def guess_type(name):
    n = (name or "").lower() + " "
    for key, words in _GUESS:
        if any(w in n for w in words):
            return key
    return "auto"


def _r(x, step):
    return round(x / step) * step


def nice_freq(f):
    if f < 100:
        return float(round(f))
    if f < 1000:
        return float(_r(f, 5))
    if f < 10000:
        return float(_r(f, 10))
    return float(_r(f, 100))


def notch_for_event(ev, console):
    """Suggested cut for a detected feedback / ringing frequency."""
    fb = ev.get("status") == "feedback"
    gain = -6.0 if fb else -3.0
    if ev.get("hits", 1) >= 3:
        gain -= 3.0
    q = min(console["q_max"], 12.0 if fb else 8.0)
    return {"type": "Bell", "f": round(ev["f"], 1), "gain": max(gain, -console["gain"]), "q": q}


def _band_power(c, df, f1, f2):
    return float(dsp.band_sum(c, np.array([f1 / df]), np.array([f2 / df]))[0])


def advise_channel(an, c, ctype, console):
    """Findings for channel index c of Analyzer an.

    Each finding: sev (crit|warn|info|ok), kind, title, detail, f (Hz or None) and an
    optional fix: {"gain": text} | {"hpf": Hz} | {"band": {...}} | {"dyn": text}.
    Returns (findings, info).
    """
    findings = []

    def add(sev, kind, title, detail, f=None, **fix):
        findings.append({"sev": sev, "kind": kind, "title": title, "detail": detail, "f": f, "fix": fix or None})

    st = SOURCE_TYPES.get(ctype, SOURCE_TYPES["auto"])
    t = an.written / an.sr
    rms_db = 10 * math.log10(max(an.ms[c], 1e-12))
    peak_db = 20 * math.log10(max(an.peak_slow[c], 1e-6))
    active = an.scan_cnt[c] >= 4 and peak_db > -70
    info = {"active": bool(active), "peak_db": peak_db, "rms_db": rms_db}
    if not active:
        add("info", "none", "No signal", "Nothing above -70 dBFS on this input yet.")
        return findings, info

    # --- level
    if t - an.clip_t[c] < 10.0:
        add("crit", "clip", "Clipping at the input",
            "Samples are hitting full scale. Bring the preamp gain down about 6 dB, then make it up at the fader.",
            gain="-6 dB")
    elif peak_db > -6.0:
        cut = int(math.ceil((peak_db + 12.0) / 3.0) * 3)
        add("warn", "hot", "Running hot",
            "Peaks reach %.0f dBFS. Trim about %d dB so peaks sit near -12 dBFS." % (peak_db, cut), gain="-%d dB" % cut)
    elif peak_db < -40.0:
        add("info", "low", "Low level",
            "Peaks only reach %.0f dBFS. If this tap is pre-fader, the preamp has room for roughly %d dB more." % (
                peak_db, min(24, int(-18 - peak_db))))

    lt = an.scan_lt[:, c]
    n = an.scan_n
    df = an.sr / n
    cs = dsp.cum(lt)
    total = max(_band_power(cs, df, 20.0, min(20000.0, an.sr / 2 * 0.95)), 1e-30)

    # --- sustained tones (feedback suspects)
    for tn in sorted(an.scan_tones[c], key=lambda x: (x["status"] != "feedback", -x["level"]))[:2]:
        fb = tn["status"] == "feedback"
        age = t - tn["_t"]
        when = "is sitting on this input now" if age < 1.5 else "was on this input %d s ago" % round(age)
        gain = -6.0 if fb else -3.0
        for ev in list(getattr(an, "events", {}).values()):        # same depth as the notch shown for the mic event
            if abs(math.log2(ev["f"] / tn["f"])) < 0.04:
                gain = min(gain, notch_for_event(ev, console)["gain"])
        add("crit" if fb else "warn", "tone",
            ("Feedback suspect at %s" if fb else "Ringing at %s") % dsp.fmt_hz(tn["f"]),
            "A steady, pure tone %s (%.0f dB above its surroundings, %.0f dBFS). Notch it on this channel before reaching for the system EQ." % (when, tn["prom"], tn["level"]),
            f=float(tn["f"]),
            band={"prio": 0, "f": round(float(tn["f"]), 1), "gain": gain,
                  "q": min(console["q_max"], 12.0), "why": "feedback notch"})

    # --- mains hum: steady lines that survive in the quietest moments of the channel
    fl = an.scan_min[:, c]
    if an.scan_cnt[c] >= 40 and np.isfinite(fl).all():
        line_floor = 10 ** (-85.0 / 10) * n * n / 16.0      # -85 dBFS sine
        for mains in (60.0, 50.0):
            hit = 0
            for h in (1, 2, 3):
                k = int(round(mains * h / df))
                kk = k - 1 + int(np.argmax(fl[k - 1:k + 2]))
                lvl = fl[kk]
                around = np.concatenate((fl[max(kk - 7, 1):kk - 2], fl[kk + 3:kk + 8]))
                if lvl <= line_floor or lvl < 12.0 * float(np.mean(around)):
                    continue
                a, b, cc = (10 * math.log10(max(v, 1e-30)) for v in fl[kk - 1:kk + 2])
                den = a - 2 * b + cc
                f_line = (kk + (0.5 * (a - cc) / den if den < 0 else 0.0)) * df
                if abs(f_line - mains * h) <= 1.5:
                    hit += 1
            if hit >= 2:
                add("warn", "hum", "Mains hum (%d Hz and harmonics)" % mains,
                    "Steady lines at %d / %d Hz stay put when the source is quiet. Fix it at the source first: ground lift on the DI, swap the cable, move away from power. EQ will not remove it cleanly." % (mains, mains * 2))
                break

    # --- low-frequency rumble below the useful range of the source
    hpf = st["hpf"]
    low = _band_power(cs, df, 20.0, hpf * 0.8)
    rel = 10 * math.log10(max(low, 1e-30) / total)
    if rel > -15.0 and ctype not in ("kick", "bass", "playback"):
        add("warn", "rumble", "Low-end rumble",
            "Energy below %d Hz is only %.0f dB under the rest of the signal. It eats headroom and muddies the mix." % (hpf * 0.8, -rel),
            hpf=nice_freq(hpf))
    elif rel > -6.0:
        add("info", "sub", "Sub-heavy",
            "Most of the energy is below %d Hz. Fine if intended; otherwise tighten the HPF." % hpf)

    # --- sibilance on voices
    sibilant = False
    if ctype in ("vocal", "speech"):
        sib = _band_power(cs, df, 5000.0, 9000.0)
        body = _band_power(cs, df, 1000.0, 4000.0)
        ratio = 10 * math.log10(max(sib, 1e-30) / max(body, 1e-30))
        if ratio > -8.0:
            sibilant = True
            fix = {"dyn": "%s around 6-7 kHz, 3-5 dB of reduction on esses" % console["deesser"]}
            if console["dyn_eq"]:
                fix["band"] = {"prio": 2, "f": 6500.0, "gain": -4.0, "q": 2.5, "dyn": True, "why": "sibilance"}
            add("warn", "sib", "Sibilance heavy",
                "The 5-9 kHz region is only %.0f dB under the 1-4 kHz body. Tame it dynamically so the tone stays open between esses." % -ratio,
                **fix)

    # --- resonances / build-up
    b3 = an._band_db(lt, n, 3)
    for bu in spectral_buildups(b3, max(60.0, st["low_skip"]), 12000.0, min_excess=5.0, top=2,
                                skip=[tn["f"] for tn in an.scan_tones[c]]):
        if sibilant and 4000.0 <= bu["f"] <= 11000.0:
            continue
        cut = -min(6.0, max(2.0, _r(bu["excess"] * 0.6, 0.5)))
        add("warn", "buildup", "Build-up near %s" % dsp.fmt_hz(nice_freq(bu["f"])),
            "This region stands %.0f dB above the bands either side of it. Sweep a cut around it and keep it if the source clears up." % bu["excess"],
            f=float(bu["f"]),
            band={"prio": 1, "f": nice_freq(bu["f"]), "gain": cut, "q": 3.0, "why": "build-up"})

    if not findings:
        add("ok", "ok", "Looks healthy", "Level, low end and balance are within normal range.")
    return findings, info


def build_strip(findings, console):
    """Lay the fixes out the way the console's input strip is organised."""
    strip = {"gain": None, "hpf": None, "bands": [], "dyn": None}
    eqs = []
    for f in findings:
        fix = f.get("fix") or {}
        if fix.get("gain") and not strip["gain"]:
            strip["gain"] = fix["gain"]
        if fix.get("hpf"):
            strip["hpf"] = max(strip["hpf"] or 0, fix["hpf"])
        if fix.get("dyn"):
            strip["dyn"] = fix["dyn"]
        if fix.get("band"):
            eqs.append(fix["band"])
    eqs.sort(key=lambda e: e["prio"])
    names = console["band_names"]
    eqs = sorted(eqs[:console["in_bands"]], key=lambda e: e["f"])
    edges = [0, 200, 1000, 4000, 1e9]
    used = set()
    for i, e in enumerate(eqs):
        if len(eqs) == len(names):
            pick = i
        else:                               # the band whose usual range fits best
            pref = next(j for j in range(4) if edges[j] <= e["f"] < edges[j + 1])
            pref = min(pref, len(names) - 1)
            pick = next(j for j in sorted(range(len(names)), key=lambda j: abs(j - pref)) if j not in used)
        used.add(pick)
        strip["bands"].append({"band": names[pick], "n": pick,
                               "f": min(max(e["f"], console["f_min"]), console["f_max"]),
                               "gain": max(e["gain"], -console["gain"]),
                               "q": min(max(e["q"], console["q_min"]), console["q_max"]),
                               "dyn": bool(e.get("dyn")), "why": e["why"]})
    strip["bands"].sort(key=lambda b: b["n"])
    return strip
