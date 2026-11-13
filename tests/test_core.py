"""Checks of the analysis core against simulated signals with a known answer.

Run from the project folder:   python3 -m unittest -v
No audio hardware is needed: the live-input path is exercised through a stand-in driver.
"""
import json
import math
import threading
import time
import unittest
import urllib.error
import urllib.request

import numpy as np

from tt import advisor, dsp, engine, eq
from tt.analyzer import Analyzer
from tt.app import App
from tt.engine import DemoSource
from tt.server import serve

SR = 48000
BLOCK = 2048


def pink(n, seed, rms=0.1):
    rng = np.random.default_rng(seed)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    x = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) / np.sqrt(np.maximum(f, 20.0)), n)
    return (x * (rms / np.std(x))).astype(np.float32)


def through_pa(x, delay=600):
    """Reference -> coloured, delayed 'PA + room' signal."""
    n = len(x)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    h = dsp.chain_response(DemoSource.PA_FILTERS, f, SR) * np.exp(-2j * np.pi * f * delay / SR)
    return np.fft.irfft(np.fft.rfft(x) * h, n).astype(np.float32)


def feed(an, ref, mic, each=None):
    for i in range(0, len(ref) - BLOCK + 1, BLOCK):
        an.ingest(np.stack((ref[i:i + BLOCK], mic[i:i + BLOCK]), axis=1))
        if each:
            each(i + BLOCK)


def run_demo(seconds, an=None, demo=None, each=None):
    demo = demo or DemoSource()
    an = an or Analyzer(SR, len(DemoSource.CHANNELS))
    for i in range(int(seconds * SR / BLOCK)):
        an.ingest(demo.next_block())
        if each:
            each(i, an)
    return an, demo


class Detector(unittest.TestCase):
    def test_noise_raises_no_alarm_at_any_sensitivity(self):
        ref = pink(SR * 60, 1)
        mic = through_pa(ref)
        for sens in ("low", "normal", "high"):
            an = Analyzer(SR, 2)
            an.sens = sens
            feed(an, ref, mic)
            self.assertEqual(len(an.events), 0, "false alarm on pink noise at %s sensitivity: %s" % (
                sens, [(round(e["f"]), e["status"]) for e in an.events.values()]))

    def test_growing_tone_is_flagged_at_its_frequency(self):
        for f0 in (125.0, 630.0, 3150.0, 8000.0):
            n = SR * 12
            ref = pink(n, 2)
            mic = through_pa(ref)
            t = np.arange(n) / SR
            level_db = np.clip(-70.0 + (t - 4.0) * 12.0, -200.0, -18.0)      # starts at 4 s, +12 dB/s
            mic = mic + (np.where(t >= 4.0, 10 ** (level_db / 20.0), 0.0) * np.sin(2 * np.pi * f0 * t)).astype(np.float32)
            an = Analyzer(SR, 2)
            first = {}

            def note(pos, an=an, first=first):
                if an.events and "t" not in first:
                    first["t"] = pos / SR
            feed(an, ref, mic, note)
            fb = [e for e in an.events.values() if e["status"] == "feedback"]
            self.assertEqual(len(fb), 1, "expected one feedback event for %g Hz, got %s" % (f0, list(an.events.values())))
            cents = 1200 * abs(math.log2(fb[0]["f"] / f0))
            self.assertLess(cents, 25, "reported %.1f Hz for a %g Hz tone" % (fb[0]["f"], f0))
            self.assertGreater(first["t"], 4.0)
            self.assertLess(first["t"], 9.0, "took too long to flag %g Hz" % f0)

    def test_sustained_musical_note_is_not_called_feedback(self):
        """A held note with harmonics may be watched as 'ringing', but must not raise the feedback alarm."""
        n = SR * 12
        ref = pink(n, 3)
        mic = through_pa(ref)
        t = np.arange(n) / SR
        env = np.clip((t - 3) / 0.05, 0, 1) * np.clip((9 - t) / 0.3, 0, 1)
        for vibrato in (0.0, 0.35):
            note = sum((0.05 / k) * np.sin(2 * np.pi * 220 * k * t + k * vibrato * np.sin(2 * np.pi * 5.5 * t))
                       for k in range(1, 7))
            for sens in ("low", "normal", "high"):
                an = Analyzer(SR, 2)
                an.sens = sens
                feed(an, ref, (mic + note * env).astype(np.float32))
                self.assertFalse([e for e in an.events.values() if e["status"] == "feedback"], (vibrato, sens))

    def test_demo_feedback_is_traced_to_the_right_input(self):
        an, _ = run_demo(24)
        got = {}
        for e in an.events.values():
            if e["status"] == "feedback":
                got[round(e["f"])] = max(e["src"], key=e["src"].get) if e["src"] else None
        near = lambda f: next((v for k, v in got.items() if abs(k - f) < f * 0.02), "missing")
        self.assertEqual(near(2480), 7, got)     # Lead Vox
        self.assertEqual(near(196), 6, got)      # Ac Gtr
        self.assertEqual(len(got), 2, got)


class TransferFunction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.an = Analyzer(SR, len(DemoSource.CHANNELS))
        cls.demo = DemoSource()

        def each(i, an):
            if i == int(2.9 * SR / BLOCK):
                an.delay_pending = True
        run_demo(30, cls.an, cls.demo, each)

    def test_delay_finder(self):
        r = self.an.delay_result
        self.assertTrue(r and r["ok"], r)
        self.assertLessEqual(abs(r["samples"] - DemoSource.PA_DELAY), 2, r)
        self.assertEqual(r["polarity"], 1)
        self.assertGreater(r["conf"], 30)

    def test_magnitude_matches_the_simulated_system(self):
        tf = self.an.tf_snapshot(48)
        g = dsp.GRID
        delay, r_delay, r_gain, hf_db, level = DemoSource.POSITIONS[0]
        truth = 20 * np.log10(np.abs(level * dsp.chain_response(DemoSource.PA_FILTERS, g, SR)
                                     * (1 + r_gain * np.exp(-2j * np.pi * g / SR * r_delay))))
        # the two demo feedback tones are not in the reference, so skip a third-octave around each
        sel = (g >= 50) & (g <= 16000)
        for f0 in (196.0, 2480.0):
            sel &= np.abs(np.log2(g / f0)) > 1 / 6
        err = tf["mag"][sel] - truth[sel]
        self.assertLess(float(np.sqrt(np.mean(err ** 2))), 0.6)
        self.assertLess(float(np.max(np.abs(err))), 2.5)
        self.assertGreater(tf["quality"], 0.85)

    def test_coherence_drops_where_the_mic_hears_something_else(self):
        # run until the 2.48 kHz demo tone is sounding: it is in the mic but not in the reference
        an = Analyzer(SR, len(DemoSource.CHANNELS))
        an.delay = DemoSource.PA_DELAY
        run_demo(8.5, an)
        tf = an.tf_snapshot(48)
        k = int(np.argmin(np.abs(dsp.GRID - 2480.0)))
        self.assertLess(tf["coh"][k], 0.6)
        self.assertGreater(float(np.nanmedian(tf["coh"])), 0.9)

    def test_polarity_flip_is_reported(self):
        ref = pink(SR * 8, 5)
        an = Analyzer(SR, 2)
        an.delay_pending = True
        feed(an, ref, -through_pa(ref, 480))
        self.assertTrue(an.delay_result["ok"])
        self.assertEqual(an.delay_result["polarity"], -1)
        self.assertLessEqual(abs(an.delay_result["samples"] - 480), 2)

    def test_96k_input(self):
        n = 96000 * 10
        rng = np.random.default_rng(9)
        ref = (0.1 * rng.standard_normal(n)).astype(np.float32)
        mic = np.concatenate((np.zeros(1000, np.float32), 0.5 * ref[:-1000]))
        an = Analyzer(96000, 2)
        an.delay_pending = True
        for i in range(0, n - 4095, 4096):
            an.ingest(np.stack((ref[i:i + 4096], mic[i:i + 4096]), axis=1))
        self.assertEqual(an.delay_result["samples"], 1000)
        tf = an.tf_snapshot(48)
        sel = (dsp.GRID >= 50) & (dsp.GRID <= 18000)
        self.assertLess(float(np.max(np.abs(tf["mag"][sel] + 6.02))), 0.5)


class AutoEq(unittest.TestCase):
    def test_recovers_known_filters(self):
        g = dsp.GRID
        mag = dsp.bell_db(g, 250.0, 6.0, 2.0) + dsp.bell_db(g, 3000.0, -5.0, 3.0)
        r = eq.auto_eq(mag, np.ones(len(g)), np.zeros(len(g)), 40, 16000, max_bands=8, max_boost=6.0)
        self.assertTrue(r["ok"])
        self.assertLess(r["rms_after"], 0.5)
        self.assertLess(r["rms_after"], r["rms_before"] / 3)
        cut = min(r["bands"], key=lambda b: b["gain"])
        self.assertLess(abs(math.log2(cut["f"] / 250.0)), 0.12)
        self.assertLess(abs(cut["gain"] + 6.0), 1.0)

    def test_respects_console_limits(self):
        g = dsp.GRID
        mag = dsp.bell_db(g, 100.0, -15.0, 1.0) + dsp.bell_db(g, 1000.0, 14.0, 4.0)
        r = eq.auto_eq(mag, np.ones(len(g)), np.zeros(len(g)), 40, 16000, max_bands=4, max_boost=3.0, max_cut=9.0)
        self.assertLessEqual(len(r["bands"]), 4)
        for b in r["bands"]:
            self.assertLessEqual(b["gain"], 3.0 + 1e-6)
            self.assertGreaterEqual(b["gain"], -9.0 - 1e-6)
            self.assertTrue(0.5 <= b["q"] <= 10.0)
            self.assertTrue(40 <= b["f"] <= 16000)

    def test_ignores_low_coherence_regions(self):
        g = dsp.GRID
        mag = dsp.bell_db(g, 500.0, 10.0, 6.0)
        coh = np.where(np.abs(np.log2(g / 500.0)) < 0.3, 0.2, 1.0)
        r = eq.auto_eq(mag, coh, np.zeros(len(g)), 40, 16000)
        self.assertTrue(all(abs(math.log2(b["f"] / 500.0)) > 0.2 or abs(b["gain"]) < 1.5 for b in r["bands"]), r["bands"])


class BestTrace(unittest.TestCase):
    def test_average_of_positions_beats_any_single_seat(self):
        """The EQ fitted to the best trace must fix the loudspeaker, not one seat's reflections."""
        app = App(demo_only=True)
        g = dsp.GRID
        sel = (g >= 60) & (g <= 12000)
        truth = 20 * np.log10(np.abs(dsp.chain_response(DemoSource.PA_FILTERS, g, SR)))
        truth_s = dsp.smooth_db(truth, 1 / 12)

        def resid_after(eq_curve):
            r = (truth_s + eq_curve)[sel]
            return float(np.std(r - np.median(r)))

        singles = []
        demo = DemoSource()
        for pos in range(len(DemoSource.POSITIONS)):
            demo.position = pos
            an = Analyzer(SR, len(DemoSource.CHANNELS))
            an.delay = DemoSource.POSITIONS[pos][0]
            # 2.5 s windows that avoid the demo feedback tones would be too short; measure with the
            # tones present, as on a real stage with open mics
            run_demo(24, an, demo)
            tf = an.tf_snapshot(48)
            app.an, app.engine.mode = an, "demo"
            self.assertTrue(app.capture()["ok"])
            ms = dsp.smooth_db(tf["mag"], 1 / 12)
            r = eq.auto_eq(ms, tf["coh"], np.zeros(len(g)), 60, 12000, max_boost=6.0)
            singles.append((resid_after(r["eq"]), len(r["bands"])))
        best = app.best_trace()
        self.assertEqual(best["n"], 3)
        r = eq.auto_eq(dsp.smooth_db(best["mag"], 1 / 12), best["coh"], np.zeros(len(g)), 60, 12000, max_boost=6.0)
        got = resid_after(r["eq"])
        before = float(np.std(truth_s[sel] - np.median(truth_s[sel])))
        self.assertLess(got, before * 0.5, "EQ from the best trace should at least halve the true deviation")
        self.assertLessEqual(got, np.mean([s[0] for s in singles]) + 0.05, (got, singles))
        type(self).summary = {"untreated": before, "best": got, "single": singles, "bands": len(r["bands"])}


class ChannelAdvice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.an, _ = run_demo(40)
        cls.con = advisor.CONSOLES["q326"]
        cls.res = {}
        for c, (name, ctype) in enumerate(DemoSource.CHANNELS):
            findings, info = advisor.advise_channel(cls.an, c, ctype, cls.con)
            cls.res[name] = (findings, advisor.build_strip(findings, cls.con))

    def kinds(self, name):
        return {f["kind"] for f in self.res[name][0]}

    def test_expected_problems_are_found(self):
        self.assertIn("clip", self.kinds("E-Gtr"))
        self.assertIn("hum", self.kinds("Bass DI"))
        self.assertIn("tone", self.kinds("Lead Vox"))
        self.assertIn("tone", self.kinds("Ac Gtr"))
        self.assertIn("none", self.kinds("Tracks"))
        self.assertIn("low", self.kinds("Keys"))

    def test_healthy_channels_stay_quiet(self):
        for name in ("Snare", "BV"):
            sev = {f["sev"] for f in self.res[name][0]}
            self.assertFalse(sev & {"crit", "warn"}, (name, self.res[name][0]))

    def test_strip_fits_the_console(self):
        for name, (findings, strip) in self.res.items():
            self.assertLessEqual(len(strip["bands"]), self.con["in_bands"], name)
            self.assertEqual(len({b["band"] for b in strip["bands"]}), len(strip["bands"]), name)
            for b in strip["bands"]:
                self.assertTrue(self.con["f_min"] <= b["f"] <= self.con["f_max"], (name, b))
                self.assertLessEqual(abs(b["gain"]), self.con["gain"])
                self.assertTrue(self.con["q_min"] <= b["q"] <= self.con["q_max"])

    def test_vocal_notch_lands_on_the_feedback_frequency(self):
        bands = self.res["Lead Vox"][1]["bands"]
        notch = [b for b in bands if abs(b["f"] - 2480) < 40]
        self.assertTrue(notch, bands)
        self.assertLessEqual(notch[0]["gain"], -6.0)
        self.assertGreaterEqual(notch[0]["q"], 8.0)


class FakeStream:
    """Stands in for a PortAudio input stream: 12 channels of the demo show, faster than real time."""
    def __init__(self, device=None, samplerate=None, channels=None, dtype=None, blocksize=None, callback=None):
        self.samplerate, self.channels, self.cb, self.bs = samplerate, channels, callback, blocksize
        self.run = False

    def start(self):
        self.run = True
        demo = DemoSource()

        def loop():
            while self.run:
                blk = demo.next_block()[:, :self.channels]
                for i in range(0, len(blk), self.bs):
                    self.cb(blk[i:i + self.bs], self.bs, None, None)
                time.sleep(0.004)
        threading.Thread(target=loop, daemon=True).start()

    def stop(self):
        self.run = False

    def close(self):
        pass


class FakeSD:
    DEV = {"name": "UB MADI (stand-in)", "hostapi": 0, "max_input_channels": 48, "max_output_channels": 48,
           "default_samplerate": 48000.0}
    InputStream = FakeStream

    class default:
        device = (0, 0)

    def query_devices(self, device=None, kind=None):
        return dict(self.DEV) if (device is not None or kind) else [dict(self.DEV)]

    def query_hostapis(self):
        return [{"name": "Core Audio"}]

    def _terminate(self):
        pass

    def _initialize(self):
        pass


class LivePathAndServer(unittest.TestCase):
    def setUp(self):
        self._sd = engine.sd
        engine.sd = FakeSD()
        self.app = App(persist=False)
        self.httpd = serve(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.app.stop()
        self.httpd.shutdown()
        self.httpd.server_close()
        engine.sd = self._sd

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return json.loads(r.read())

    def cmd(self, **d):
        req = urllib.request.Request(self.base + "/api/cmd", json.dumps(d).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())

    def test_multichannel_interface_end_to_end(self):
        dev = self.get("/api/devices")
        self.assertEqual(dev["inputs"][0]["ch"], 48)
        self.assertTrue(self.cmd(cmd="start", device=0, channels=12)["ok"])
        fr = self.get("/api/frame?view=channels")
        self.assertEqual(fr["status"]["mode"], "live")
        self.assertEqual(fr["status"]["nch"], 12)
        self.assertTrue(self.cmd(cmd="roles", meas=1, ref=0)["ok"])
        self.assertTrue(self.cmd(cmd="find_delay")["ok"])
        deadline = time.time() + 20
        while time.time() < deadline:
            d = self.get("/api/frame?view=tune")["delay"]
            if d["result"]:
                break
            time.sleep(0.1)
        self.assertTrue(d["result"] and d["result"]["ok"], d)
        self.assertLessEqual(abs(d["samples"] - DemoSource.PA_DELAY), 2)
        deadline = time.time() + 20
        while time.time() < deadline and self.get("/api/frame?view=tune")["tf"]["frames"] < 12:
            time.sleep(0.1)
        self.assertTrue(self.cmd(cmd="capture")["ok"])
        self.assertTrue(self.cmd(cmd="auto_eq")["ok"])
        st = self.get("/api/static")
        self.assertTrue(st["eq"]["bands"])
        self.assertLess(st["eq"]["after"], st["eq"]["before"])
        self.assertEqual(self.app.engine.dropped, 0)
        with urllib.request.urlopen(self.base + "/api/report.txt", timeout=5) as r:
            self.assertIn("SYSTEM EQ", r.read().decode())

    def test_bad_requests_are_rejected_cleanly(self):
        self.assertFalse(self.cmd(cmd="nope")["ok"])
        self.assertFalse(self.cmd(cmd="capture")["ok"])            # nothing running
        bad = urllib.request.Request(self.base + "/api/cmd", b"{not json", {"Content-Type": "application/json"})
        self.assertEqual(self.status(bad), 400)
        self.assertEqual(self.status(urllib.request.Request(self.base + "/api/frame", headers={"Host": "evil.example"})), 403)
        self.assertIn(self.status(self.base + "/../tunetrace.py"), (403, 404))
        self.assertEqual(self.status(self.base + "/"), 200)

    def status(self, req):
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code


if __name__ == "__main__":
    unittest.main()
