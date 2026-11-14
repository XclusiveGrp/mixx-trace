"""Checks of the analysis core against simulated signals with a known answer.

Run from the project folder:   python3 -m unittest -v
No audio hardware is needed: the live-input path is exercised through a stand-in driver.
"""
import json
import math
import os
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import numpy as np

import tt.app as app_mod
from tt import advisor, align, dsp, engine, eq, osc
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
        self.assertIn(self.status(self.base + "/../mixxtrace.py"), (403, 404))
        self.assertEqual(self.status(self.base + "/"), 200)

    def status(self, req):
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code


# ------------------------------------------------------------ console remote and alignment
def demo_app():
    """An app on the demo signal, driven block by block so the tests do not wait for real time."""
    app = App(demo_only=True, persist=False)
    src = DemoSource()
    app.engine.demo, app.engine.mode, app.engine.sr, app.engine.nch = src, "demo", float(SR), len(DemoSource.CHANNELS)
    app._new_analyzer()
    app._sync_demo()
    return app, src


def play(app, src, seconds):
    for _ in range(int(seconds * SR / DemoSource.BLOCK)):
        app._on_block(src.next_block())


def measure(app, src, seconds=8.0):
    """Lock the delay and average a fresh transfer function at full resolution."""
    play(app, src, 3.0)
    app.command({"cmd": "find_delay"})
    play(app, src, seconds)
    return app.an.tf_snapshot(48)


def writes(console):
    return [(a, v) for a, v in console.log if not a.endswith("/?")]


class FakeConsole:
    """A stand-in desk on the loopback network: real UDP, real OSC packets, answers like the console does."""

    def __init__(self, console=None):
        self.sim = console or osc.SimConsole("Test desk")
        self.times = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.port = self.sock.getsockname()[1]
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        self.reply_port = probe.getsockname()[1]
        probe.close()
        self.run = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.run:
            try:
                data, src = self.sock.recvfrom(65535)
            except OSError:
                continue
            self.times.append(time.monotonic())
            for addr, args in osc.decode(data):
                for r_addr, r_args in self.sim.handle(addr, args):
                    self.sock.sendto(osc.encode(r_addr, r_args), (src[0], self.reply_port))

    def close(self):
        self.run = False
        self.thread.join(1.0)
        self.sock.close()


class OscCodec(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(osc.decode(osc.encode("/Aux_Outputs/3/EQ/eq_freq_2", [1250.0])), [("/Aux_Outputs/3/EQ/eq_freq_2", [1250.0])])
        self.assertEqual(osc.decode(osc.encode("/Console/Name/?")), [("/Console/Name/?", [])])
        (addr, args), = osc.decode(osc.encode("/x", [-4.5, "Quantum 326", 7]))
        self.assertEqual((addr, args[1]), ("/x", "Quantum 326"))
        self.assertAlmostEqual(args[0], -4.5)
        self.assertAlmostEqual(args[2], 7.0)

    def test_known_packet_layout(self):
        # address and type tags are null-padded to four bytes; numbers travel as big-endian float32
        self.assertEqual(osc.encode("/a/b", [1.0]), b"/a/b\x00\x00\x00\x00,f\x00\x00\x3f\x80\x00\x00")

    def test_bundles_and_garbage(self):
        one, two = osc.encode("/a", [1.0]), osc.encode("/b", [2.0])
        bundle = b"#bundle\x00" + b"\x00" * 8 + len(one).to_bytes(4, "big") + one + len(two).to_bytes(4, "big") + two
        self.assertEqual(osc.decode(bundle), [("/a", [1.0]), ("/b", [2.0])])
        for junk in (b"", b"\x00\x01", b"hello", b"/a\x00\x00,f\x00\x00\x3f", os.urandom(64)):
            self.assertIsInstance(osc.decode(junk), list)


class ConsoleRemote(unittest.TestCase):
    BANDS = [{"f": 250.0, "gain": -3.5, "q": 2.0}, {"f": 2500.0, "gain": -25.0, "q": 30.0},
             {"f": 63.0, "gain": 2.0, "q": 1.4}, {"f": 6300.0, "gain": 1.5, "q": 0.9}]

    def setUp(self):
        self._pace = osc.UdpLink.pace
        osc.UdpLink.pace = 0.002
        self.desk = FakeConsole()
        self.app = App(persist=False)
        self.cmd = self.app.command
        self.assertTrue(self.cmd({"cmd": "osc_set", "mode": "udp", "host": "127.0.0.1", "send_port": self.desk.port,
                                  "listen_port": self.desk.reply_port, "out_type": "Matrix", "out_n": 3})["ok"])
        self.app.eq = {"bands": [dict(b) for b in self.BANDS]}

    def tearDown(self):
        osc.UdpLink.pace = self._pace
        self.desk.close()

    def push(self, what="eq"):
        pre = self.cmd({"cmd": "osc_preview", "what": what})
        self.assertTrue(pre["ok"], pre)
        return pre, self.cmd({"cmd": "osc_push", "token": pre["token"], "confirm": True})

    def test_connection_test_names_the_console(self):
        r = self.cmd({"cmd": "osc_test"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["name"], "Test desk")
        self.assertEqual(writes(self.desk.sim), [])

    def test_preview_changes_nothing(self):
        pre = self.cmd({"cmd": "osc_preview", "what": "eq"})
        self.assertTrue(pre["ok"], pre)
        self.assertEqual(writes(self.desk.sim), [])
        self.assertFalse(pre["sim"])
        self.assertGreater(pre["n"], 0)

    def test_eq_push_sends_exact_values_in_order_and_reads_them_back(self):
        pre, res = self.push()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["mismatch"], [])
        b = "/Matrix_Outputs/3/EQ/"
        got = [(a, round(v[0], 2)) for a, v in writes(self.desk.sim)]
        self.assertEqual(got, [
            (b + "eq_curve_1", 1.0), (b + "eq_freq_1", 6300.0), (b + "eq_Q_1", 0.9), (b + "eq_gain_1", 1.5),     # top band was a shelf
            (b + "eq_freq_2", 2500.0), (b + "eq_Q_2", 20.0), (b + "eq_gain_2", -18.0),                           # clamped to the console's range
            (b + "eq_freq_3", 250.0), (b + "eq_Q_3", 2.0), (b + "eq_gain_3", -3.5),
            (b + "eq_curve_4", 1.0), (b + "eq_freq_4", 63.0), (b + "eq_Q_4", 1.4), (b + "eq_gain_4", 2.0),
        ])
        self.assertEqual(res["n"], len(got))
        self.assertEqual(pre["n"], len(got))
        for a, _ in self.desk.sim.log:                       # nothing outside the chosen output was touched or even asked
            self.assertTrue(a.startswith(b) or a.startswith("/Console/"), a)
        self.assertEqual(self.app.eq["pushed"]["target"], "Matrix 3")

    def test_existing_eq_is_flattened_before_it_moves_and_undo_puts_it_back(self):
        b = "/Matrix_Outputs/3/EQ/"
        for a, v in ((b + "eq_gain_3", -6.0), (b + "eq_freq_3", 400.0), (b + "eq_Q_3", 4.0), (b + "eq_in", 0.0)):
            self.desk.sim.v[a] = v
        before = dict(self.desk.sim.v)
        state = self.desk.sim.output("Matrix", 3)
        pre, res = self.push()
        self.assertTrue(res["ok"], res)
        self.assertTrue(any("already has EQ" in w for w in pre["warnings"]), pre["warnings"])
        w = writes(self.desk.sim)
        i_flat = w.index((b + "eq_gain_3", [0.0]))
        self.assertLess(i_flat, [a for a, _ in w].index(b + "eq_freq_3"))
        self.assertEqual(self.desk.sim.get(b + "eq_in"), 1.0)
        self.assertTrue(self.app.static()["osc"]["undo"])
        pre, res = self.push("undo")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.desk.sim.output("Matrix", 3), state)
        for a, v in before.items():
            self.assertAlmostEqual(self.desk.sim.get(a), v, places=4, msg=a)
        for k in (1, 2, 4):
            self.assertAlmostEqual(self.desk.sim.get(b + "eq_gain_%d" % k), 0.0)
        self.assertEqual(self.desk.sim.get(b + "eq_curve_1"), 4.0)
        self.assertIsNone(self.app.static()["osc"]["undo"])
        self.assertNotIn("pushed", self.app.eq)

    def test_nothing_is_sent_without_a_confirmed_fresh_preview(self):
        pre = self.cmd({"cmd": "osc_preview", "what": "eq"})
        for bad in ({"token": pre["token"]}, {"token": pre["token"], "confirm": "yes"}, {"token": "abc", "confirm": True},
                    {"confirm": True}):
            self.assertFalse(self.cmd(dict(bad, cmd="osc_push"))["ok"])
        self.app.osc_plan["made"] -= 121
        self.assertFalse(self.cmd({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])
        self.assertFalse(self.cmd({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])   # and it stays dead
        pre = self.cmd({"cmd": "osc_preview", "what": "eq"})
        self.cmd({"cmd": "osc_set", "out_n": 5})                                                        # target changed after the preview
        self.assertFalse(self.cmd({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])
        self.assertEqual(writes(self.desk.sim), [])

    def test_a_token_works_once(self):
        pre, res = self.push()
        self.assertTrue(res["ok"])
        n = len(writes(self.desk.sim))
        self.assertFalse(self.cmd({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])
        self.assertEqual(len(writes(self.desk.sim)), n)

    def test_console_that_does_not_take_a_value_is_reported(self):
        handle = self.desk.sim.handle
        self.desk.sim.handle = lambda a, v: [] if "eq_gain_2" in a else handle(a, v)
        pre = self.cmd({"cmd": "osc_preview", "what": "eq"})
        self.assertFalse(pre["ok"])                         # band 2 never answered, so no plan is made
        self.desk.sim.handle = lambda a, v: [] if (a.endswith("eq_gain_2") and v) else handle(a, v)
        pre, res = self.push()
        self.assertTrue(res["mismatch"], res)
        self.assertIn("Band 2 gain", res["msg"])

    def test_only_local_console_addresses_and_safe_ports(self):
        for host in ("8.8.8.8", "example.com", "224.0.0.1", "0.0.0.0", "::1", "192.168.1.300"):
            self.assertFalse(self.cmd({"cmd": "osc_set", "host": host})["ok"], host)
        self.assertEqual(self.app.s["osc"]["host"], "127.0.0.1")
        for host in ("192.168.1.50", "10.0.0.2", "172.16.4.4", "169.254.10.10"):
            self.assertTrue(self.cmd({"cmd": "osc_set", "host": host})["ok"], host)
        for port in (80, 0, 70000, "x"):
            self.assertFalse(self.cmd({"cmd": "osc_set", "send_port": port})["ok"], port)
        self.assertFalse(self.cmd({"cmd": "osc_set", "out_type": "Matrix", "out_n": 3, "mode": "off"}) is None)
        self.assertFalse(self.cmd({"cmd": "osc_preview", "what": "eq"})["ok"])       # remote is off

    def test_silent_console_fails_cleanly_and_quickly(self):
        self.desk.close()
        t = time.time()
        r = self.cmd({"cmd": "osc_test"})
        self.assertFalse(r["ok"])
        self.assertIn("did not answer", r["msg"])
        self.assertLess(time.time() - t, 5.0)
        self.assertFalse(self.cmd({"cmd": "osc_preview", "what": "eq"})["ok"])
        self.assertIsNone(self.app.osc_plan)

    def test_wrong_output_number_is_caught_before_anything_is_written(self):
        self.cmd({"cmd": "osc_set", "out_n": 99})
        self.desk.sim._default_orig = self.desk.sim._default
        self.desk.sim._default = lambda a: None if "/99/" in a else self.desk.sim._default_orig(a)
        pre = self.cmd({"cmd": "osc_preview", "what": "eq"})
        self.assertFalse(pre["ok"])
        self.assertIn("Matrix 99", pre["msg"])
        self.assertEqual(writes(self.desk.sim), [])

    def test_messages_are_paced(self):
        osc.UdpLink.pace = self._pace
        self.assertGreaterEqual(self._pace, 0.1)
        self.cmd({"cmd": "osc_test"})
        self.cmd({"cmd": "osc_test"})                         # a second conversation straight after the first
        with osc.Client(self.app._link()) as cli:
            for _ in range(3):
                cli.query("/Console/Name")
        gaps = np.diff(self.desk.times)
        self.assertEqual(len(gaps), 4)
        self.assertGreaterEqual(float(np.min(gaps)), 0.09, gaps)


class FourBandConsole(unittest.TestCase):
    def test_fit_is_cut_to_four_bands_when_the_console_only_answers_for_four(self):
        app, src = demo_app()
        app.sim = osc.SimConsole(bands=4)
        measure(app, src)
        app.command({"cmd": "eq_refit", "bands": 8})
        self.assertGreater(len(app.eq["bands"]), 4)
        pre = app.command({"cmd": "osc_preview", "what": "eq"})
        self.assertFalse(pre["ok"])
        self.assertEqual(pre["refit"], 4)
        self.assertEqual(writes(app.sim), [])
        self.assertTrue(app.command({"cmd": "eq_refit", "bands": 4})["ok"])
        pre = app.command({"cmd": "osc_preview", "what": "eq"})
        self.assertTrue(pre["ok"], pre)
        self.assertTrue(app.command({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])
        self.assertFalse(any(a.rsplit("_", 1)[-1] in "5678" for a, _ in writes(app.sim)))


class PushedEqIsWhatWasPredicted(unittest.TestCase):
    def test_measuring_again_after_the_push_matches_the_prediction(self):
        app, src = demo_app()
        tf = measure(app, src)
        self.assertTrue(app.command({"cmd": "auto_eq"})["ok"])
        e = app.eq
        pre = app.command({"cmd": "osc_preview", "what": "eq"})
        self.assertTrue(pre["ok"] and pre["sim"], pre)
        res = app.command({"cmd": "osc_push", "token": pre["token"], "confirm": True})
        self.assertTrue(res["ok"] and not res["mismatch"], res)
        after = measure(app, src)
        self.assertTrue(app.command({"cmd": "osc_eq_in", "on": False})["ok"])
        out = measure(app, src)
        # compare only where every one of the three measurements trusted itself
        sel = ((dsp.GRID >= max(e["f_lo"], 60)) & (dsp.GRID <= min(e["f_hi"], 12000))
               & (np.minimum(np.minimum(tf["coh"], after["coh"]), out["coh"]) > 0.8))
        self.assertGreater(int(sel.sum()), 250)
        change = after["mag"] - tf["mag"]
        want = np.array(e["curve"], dtype=float)
        self.assertLess(float(np.max(np.abs(change[sel] - want[sel]))), 0.5)
        # switching the output EQ out on the console brings the original response back
        self.assertLess(float(np.max(np.abs((out["mag"] - tf["mag"])[sel]))), 0.3)
        self.assertTrue(app.static()["eq"]["pushed"]["bypassed"])


def two_way(order, sub_early_ms, invert_sub=False, main_gain=1.0):
    """Ideal crossover at 100 Hz measured one side at a time: captures with a known right answer."""
    g = dsp.GRID
    q = 0.707 if order == 4 else 0.5
    n = 2 if order == 4 else 1
    hp = dsp.chain_response([("hp", 100, q, 0)] * n, g, SR) * main_gain
    lp = dsp.chain_response([("lp", 100, q, 0)] * n, g, SR) * (-1 if invert_sub else 1)
    lp = lp * np.exp(2j * np.pi * g * sub_early_ms / 1000.0)
    cap = lambda h: {"mag": 20 * np.log10(np.abs(h)), "phase": np.degrees(np.angle(h)), "coh": np.ones(len(g)),
                     "delay": 1200, "sr": float(SR), "locked": True}
    return cap(hp), cap(lp)


class Alignment(unittest.TestCase):
    def test_early_subs_get_exactly_their_lead_as_delay(self):
        for ms in (0.5, 4.0, 11.0):
            r = align.solve(*two_way(4, ms))
            self.assertTrue(r["ok"])
            self.assertEqual((r["who"], r["invert"]), ("b", False))
            self.assertAlmostEqual(r["ms"], ms, delta=0.03)
            self.assertLess(r["short"], 0.05)                 # a fourth-order crossover sums perfectly once in time

    def test_late_subs_put_the_delay_on_the_mains(self):
        r = align.solve(*two_way(4, -6.0))
        self.assertEqual((r["who"], r["invert"]), ("a", False))
        self.assertAlmostEqual(r["ms"], 6.0, delta=0.03)

    def test_polarity_is_flipped_only_when_the_systems_need_it(self):
        r = align.solve(*two_way(2, 4.0))                     # second-order sides are 180 degrees apart by design
        self.assertTrue(r["invert"])
        self.assertAlmostEqual(r["ms"], 4.0, delta=0.05)
        r = align.solve(*two_way(2, 4.0, invert_sub=True))    # already flipped: leave it alone
        self.assertFalse(r["invert"])
        self.assertAlmostEqual(r["ms"], 4.0, delta=0.05)
        self.assertIsNone(r["alt"])
        r = align.solve(*two_way(4, 4.0, invert_sub=True))    # a miswired sub on an in-phase crossover
        self.assertTrue(r["invert"])
        self.assertAlmostEqual(r["ms"], 4.0, delta=0.05)
        # across this narrow band half a cycle of delay does nearly as well, and the result says so
        self.assertFalse(r["alt"]["invert"])
        self.assertIn((r["alt"]["who"], round(r["alt"]["ms"])), (("b", 9), ("a", 1)))
        self.assertLess(r["alt"]["worse"], 0.5)

    def test_different_analyzer_delays_are_accounted_for(self):
        a, b = two_way(4, 0.0)
        b["delay"] = 1200 - 192                               # the analyzer already removed 4 ms less from the subs
        r = align.solve(a, b)
        self.assertEqual(r["who"], "b")
        self.assertAlmostEqual(r["ms"], 4.0, delta=0.03)

    def test_refuses_when_the_systems_do_not_overlap(self):
        a, b = two_way(4, 0.0)
        b["coh"] = np.where(dsp.GRID > 60, 0.1, 1.0)
        self.assertFalse(align.solve(a, b)["ok"])
        a, b = two_way(4, 0.0, main_gain=0.01)
        b["mag"] = np.where(dsp.GRID > 50, -200.0, b["mag"])
        self.assertFalse(align.solve(a, b)["ok"])

    def run_job(self, app, src, slot):
        self.assertTrue(app.command({"cmd": "align_measure", "slot": slot})["ok"])
        for _ in range(60):
            if not app.al["job"]:
                break
            play(app, src, 1.0)
        self.assertIsNone(app.al["job"])
        self.assertIsNone(app.al["error"])

    def test_demo_pa_end_to_end_through_the_console(self):
        app, src = demo_app()
        self.run_job(app, src, "a")
        self.run_job(app, src, "b")
        r = app.al["result"]
        self.assertTrue(r and r["ok"])
        # The demo mains lag their own crossover, so the subs line up best delayed and flipped: with that the two
        # phase traces lie on top of each other through the whole crossover. The plain-delay answer is offered too.
        self.assertEqual((r["who"], r["invert"]), ("b", True))
        self.assertEqual((r["alt"]["who"], r["alt"]["invert"]), ("b", False))
        self.assertLess(r["alt"]["ms"], r["ms"])
        g = dsp.GRID
        pa, pb = app_mod.from_list(r["ph_a"]), app_mod.from_list(r["ph_b_fix"])
        near = (g > 65) & (g < 150)
        self.assertLess(float(np.max(np.abs((pa[near] - pb[near] + 180) % 360 - 180))), 25.0)
        xo = (g > 60) & (g < 160)
        now, fix = app_mod.from_list(r["sum_now"]), app_mod.from_list(r["sum_fix"])
        self.assertLess(float(np.min(now[xo])), float(np.min(fix[xo])) - 6.0)      # there is a hole at the crossover today
        # the answer is the best there is: no other delay or polarity gives more level through the crossover
        f, w = src._f, 2 * np.pi * g
        ha, hb = src._path("a", 0), src._path("b", 0, DemoSource.SUB_EARLY)
        ca = np.interp(g, f, ha.real) + 1j * np.interp(g, f, ha.imag)
        cb = np.interp(g, f, hb.real) + 1j * np.interp(g, f, hb.imag)
        band = (g >= r["f_lo"]) & (g <= r["f_hi"])
        power = lambda x, pol: float(np.mean(np.abs(ca[band] + pol * cb[band] * np.exp(-1j * w[band] * x / 1000.0)) ** 2))
        best = max(power(x, pol) for x in np.arange(-15, 15.01, 0.02) for pol in (1, -1))
        self.assertLess(10 * math.log10(best / power(r["ms"], -1)), 0.1)
        # what the Align tab is given to draw: smoothed copies of the traces, and a result without the raw lists
        st = app.static()["align"]
        self.assertFalse([k for k, v in st["result"].items() if isinstance(v, list)])
        for k in ("a", "b", "sum_now", "sum_fix", "ph_a", "ph_b", "ph_b_fix"):
            self.assertEqual(len(st["view"][k]), len(g), k)
        self.assertEqual(st["result"]["ms"], r["ms"])
        self.assertIsNone(st["result"].get("pushed"))
        json.dumps(st)
        # send it to the simulated console, then measure both systems together
        pre = app.command({"cmd": "osc_preview", "what": "align"})
        self.assertTrue(pre["ok"], pre)
        self.assertEqual([x["label"] for x in pre["rows"]],
                         ["Subs (Matrix 2) delay", "Subs (Matrix 2) delay on", "Subs (Matrix 2) polarity"])
        self.assertTrue(app.command({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])
        self.assertAlmostEqual(app.sim.get("/Matrix_Outputs/2/Channel_Delay/delay"), r["ms"] / 1000.0, places=4)
        self.assertEqual(app.sim.get("/Matrix_Outputs/2/Channel_Delay/delay_on"), 1.0)
        self.assertEqual(app.sim.get("/Matrix_Outputs/2/Buss_Trim/phase"), 1.0)
        self.assertEqual(len(writes(app.sim)), 3)
        self.assertTrue(app.command({"cmd": "demo_system", "system": "ab"})["ok"])
        tf = measure(app, src)
        sel = (g > 40) & (g < 400) & (tf["coh"] > 0.8)
        self.assertLess(float(np.max(np.abs(tf["mag"][sel] - fix[sel]))), 0.6)
        # sending the same correction twice would double the delay
        again = app.command({"cmd": "osc_preview", "what": "align"})
        self.assertFalse(again["ok"])
        self.assertIn("already sent", again["msg"])
        # undo takes the delay off again
        pre = app.command({"cmd": "osc_preview", "what": "undo"})
        self.assertTrue(app.command({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])
        self.assertEqual(app.sim.output("Matrix", 2), {"eq": [], "delay": 0.0, "inv": False})

    def test_order_of_the_two_captures_does_not_matter(self):
        app, src = demo_app()
        self.run_job(app, src, "a")
        self.run_job(app, src, "b")
        first = app.al["result"]
        app.command({"cmd": "align_clear"})
        self.assertIsNone(app.al["result"])
        self.run_job(app, src, "b")
        self.run_job(app, src, "a")
        second = app.al["result"]
        self.assertEqual((second["who"], second["invert"]), (first["who"], first["invert"]))
        self.assertAlmostEqual(second["ms"], first["ms"], delta=0.15)

    def test_existing_console_delay_is_added_to_not_replaced(self):
        app, src = demo_app()
        app.sim.v["/Matrix_Outputs/2/Channel_Delay/delay"] = 0.010
        app.sim.v["/Matrix_Outputs/2/Channel_Delay/delay_on"] = 1.0
        app._sync_demo()
        self.run_job(app, src, "a")
        self.run_job(app, src, "b")
        r = app.al["result"]                                   # the subs are now 10 - 6 = 4 ms late
        pre = app.command({"cmd": "osc_preview", "what": "align"})
        self.assertTrue(pre["ok"], pre)
        self.assertTrue(app.command({"cmd": "osc_push", "token": pre["token"], "confirm": True})["ok"])
        d_a = app.sim.get("/Matrix_Outputs/1/Channel_Delay/delay")
        d_b = app.sim.get("/Matrix_Outputs/2/Channel_Delay/delay")
        self.assertAlmostEqual(d_b - d_a, 0.010 + r["offset_ms"] / 1000.0, places=4)
        self.assertGreaterEqual(d_b, 0.010 - 1e-6)             # never shortened, only added to
        if r["who"] == "b":
            self.assertTrue(any("already has" in w for w in pre["warnings"]), pre["warnings"])
        # and it lands on the same total as the run that started from zero: about 9.4 ms on the subs
        self.assertAlmostEqual(1000 * (d_b - d_a), 9.44, delta=0.3)

    def test_muted_system_is_reported_not_guessed(self):
        app, src = demo_app()
        self.run_job(app, src, "a")
        src._h["b"] = src._h["b"] * 0.0
        self.assertTrue(app.command({"cmd": "align_measure", "slot": "b"})["ok"])
        for _ in range(60):
            if not app.al["job"]:
                break
            play(app, src, 1.0)
        self.assertIsNone(app.al["result"])
        self.assertTrue(app.al["error"])


class DelayTracking(unittest.TestCase):
    def test_delay_follows_the_mic_when_tracking_is_on(self):
        app, src = demo_app()
        measure(app, src, 4.0)
        self.assertLessEqual(abs(app.an.delay - DemoSource.POSITIONS[0][0]), 1)
        app.command({"cmd": "demo_move"})
        play(app, src, 12.0)
        self.assertLessEqual(abs(app.an.delay - DemoSource.POSITIONS[0][0]), 1)      # off: stays where it was put
        self.assertTrue(app.command({"cmd": "set", "track": True})["ok"])
        play(app, src, 12.0)
        self.assertLessEqual(abs(app.an.delay - DemoSource.POSITIONS[1][0]), 1)
        self.assertEqual(app.an.track_moves, 1)
        play(app, src, 20.0)                                                         # and it does not hunt once it is there
        self.assertEqual(app.an.track_moves, 1)
        self.assertGreater(app.an.tf_snapshot(48)["quality"], 0.8)


class Rename(unittest.TestCase):
    def test_sessions_saved_under_the_old_name_still_open(self):
        app = App(persist=False)
        old = dict(app._session(), app="TuneTrace")
        old["settings"] = dict(old["settings"], sens="high")
        for k in ("osc", "align", "track"):                    # a 0.1 session has none of the new settings
            old["settings"].pop(k, None)
        fresh = App(persist=False)
        self.assertTrue(fresh.command({"cmd": "session_import", "data": old})["ok"])
        self.assertEqual(fresh.s["sens"], "high")
        self.assertEqual(fresh.s["osc"]["mode"], "off")
        self.assertEqual(fresh._session()["app"], "Mixx Trace")
        self.assertFalse(fresh.command({"cmd": "session_import", "data": dict(old, app="Something else")})["ok"])

    def test_old_session_folder_is_read_once_then_saved_under_the_new_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            new_dir, old_file = os.path.join(tmp, ".mixxtrace"), os.path.join(tmp, ".tunetrace", "session.json")
            os.makedirs(os.path.dirname(old_file))
            src = App(persist=False)
            src.s["sens"] = "low"
            with open(old_file, "w", encoding="utf-8") as fh:
                json.dump(dict(src._session(), app="TuneTrace"), fh)
            keep = (app_mod.SESSION_DIR, app_mod.SESSION_FILE, app_mod.OLD_SESSION_FILE)
            app_mod.SESSION_DIR, app_mod.SESSION_FILE, app_mod.OLD_SESSION_FILE = new_dir, os.path.join(new_dir, "session.json"), old_file
            try:
                app = App()
                self.assertEqual(app.s["sens"], "low")
                app.command({"cmd": "set", "sens": "high"})
                self.assertTrue(os.path.exists(app_mod.SESSION_FILE))
                self.assertEqual(App().s["sens"], "high")
                with open(old_file, encoding="utf-8") as fh:
                    self.assertEqual(json.load(fh)["settings"]["sens"], "low")      # the old file is left alone
            finally:
                app_mod.SESSION_DIR, app_mod.SESSION_FILE, app_mod.OLD_SESSION_FILE = keep

    def test_console_remote_settings_never_arrive_switched_on_from_a_file(self):
        app = App(persist=False)
        data = app._session()
        data["settings"]["osc"] = dict(data["settings"]["osc"], mode="udp", host="8.8.8.8")
        fresh = App(persist=False)
        fresh.command({"cmd": "session_import", "data": data})
        self.assertEqual((fresh.s["osc"]["mode"], fresh.s["osc"]["host"]), ("off", ""))
        fresh.command({"cmd": "osc_set", "mode": "udp", "host": "192.168.1.20"})
        data["settings"]["osc"]["host"] = "192.168.1.99"      # even a plausible address does not replace the one set here
        fresh.command({"cmd": "session_import", "data": data})
        self.assertEqual((fresh.s["osc"]["mode"], fresh.s["osc"]["host"]), ("udp", "192.168.1.20"))


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------ the desktop (Mac) app
class FakeWebview:
    """Stands in for pywebview: 'opens' a window, lets the test look at the page, then 'closes' it."""

    def __init__(self, while_open=None, fail=False):
        self.settings, self.windows, self.while_open, self.fail = {}, [], while_open, fail

    def create_window(self, title, url, **kw):
        self.windows.append((title, url, kw))

    def start(self):
        if self.fail:
            raise RuntimeError("no display")
        if self.while_open:
            self.while_open(self.windows[0][1])


class DesktopApp(unittest.TestCase):
    def setUp(self):
        import sys
        import mixxtrace_app
        self.sys, self.m = sys, mixxtrace_app
        self.tmp = tempfile.TemporaryDirectory()
        self.keep = (app_mod.SESSION_DIR, app_mod.SESSION_FILE, app_mod.OLD_SESSION_FILE, sys.modules.get("webview"))
        app_mod.SESSION_DIR = os.path.join(self.tmp.name, ".mixxtrace")
        app_mod.SESSION_FILE = os.path.join(app_mod.SESSION_DIR, "session.json")
        app_mod.OLD_SESSION_FILE = os.path.join(self.tmp.name, "none.json")

    def tearDown(self):
        app_mod.SESSION_DIR, app_mod.SESSION_FILE, app_mod.OLD_SESSION_FILE, wv = self.keep
        if wv is None:
            self.sys.modules.pop("webview", None)
        else:
            self.sys.modules["webview"] = wv
        self.tmp.cleanup()

    def test_window_shows_the_running_engine_and_closing_it_shuts_everything_down(self):
        seen = {}

        def while_open(url):
            seen["page"] = urllib.request.urlopen(url, timeout=5).read().decode("utf-8")
            seen["static"] = json.loads(urllib.request.urlopen(url + "api/static", timeout=5).read())
            time.sleep(0.6)
            seen["frame"] = json.loads(urllib.request.urlopen(url + "api/frame?view=feedback", timeout=5).read())
            seen["url"] = url

        fake = FakeWebview(while_open)
        self.sys.modules["webview"] = fake
        self.assertEqual(self.m.main(["--port", "0", "--demo"]), 0)
        title, url, kw = fake.windows[0]
        self.assertEqual(title, "Mixx Trace")
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertTrue(fake.settings["ALLOW_DOWNLOADS"])
        self.assertIn("app.js", seen["page"])
        self.assertTrue(seen["static"]["native"])
        self.assertEqual(seen["frame"]["status"]["mode"], "demo")
        with self.assertRaises(OSError):                       # the window closed, so nothing is left listening
            urllib.request.urlopen(seen["url"], timeout=2)

    def test_a_busy_port_does_not_stop_the_app_from_opening(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        try:
            app = App(persist=False, native=True)
            httpd, url = self.m.start_server(app, blocker.getsockname()[1])
            try:
                self.assertNotEqual(httpd.server_address[1], blocker.getsockname()[1])
                self.assertEqual(json.loads(urllib.request.urlopen(url + "api/static", timeout=5).read())["native"], True)
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            blocker.close()

    def test_no_window_falls_back_to_the_browser_instead_of_running_unseen(self):
        fake, opened, told = FakeWebview(fail=True), [], []
        self.sys.modules["webview"] = fake
        keep = (self.m.webbrowser.open, self.m.tell, self.m.sys.platform)
        self.m.webbrowser.open, self.m.tell = opened.append, lambda msg, button="OK": told.append(msg)
        self.m.sys.platform = "darwin"                         # on a Mac the dialog is what keeps the engine alive
        try:
            self.assertEqual(self.m.main(["--port", "0"]), 0)
        finally:
            self.m.webbrowser.open, self.m.tell, self.m.sys.platform = keep
        self.assertEqual(len(opened), 1)
        self.assertIn("browser", told[0])

    def test_reports_are_saved_into_downloads_and_nowhere_else(self):
        app = App(persist=False, native=True)
        app.downloads = os.path.join(self.tmp.name, "Downloads")
        r = app.command({"cmd": "save_file", "name": "mixxtrace-report-20261113-2101.txt", "text": "Mixx Trace report\n1 kHz"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(os.path.dirname(r["path"]), app.downloads)
        with open(r["path"], encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "Mixx Trace report\n1 kHz")
        again = app.command({"cmd": "save_file", "name": "mixxtrace-report-20261113-2101.txt", "text": "second"})
        self.assertNotEqual(again["path"], r["path"])          # an earlier report is never overwritten
        with open(r["path"], encoding="utf-8") as fh:
            self.assertIn("1 kHz", fh.read())
        for bad in ("../mixxtrace-report-1.txt", "/etc/mixxtrace-x.txt", "mixxtrace-x.command", ".zshrc", "mixxtrace-a/b.txt", ""):
            res = app.command({"cmd": "save_file", "name": bad, "text": "x"})
            self.assertFalse(res["ok"], bad)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["Downloads"])
        self.assertEqual(len(os.listdir(app.downloads)), 2)
        self.assertFalse(app.command({"cmd": "save_file", "name": "mixxtrace-report-1.txt", "text": 5})["ok"])

    def test_saving_files_is_only_offered_inside_the_desktop_app(self):
        for app in (App(persist=False), App(demo_only=True, native=True)):
            self.assertFalse(app.static()["native"])
            app.downloads = os.path.join(self.tmp.name, "Downloads")
            self.assertFalse(app.command({"cmd": "save_file", "name": "mixxtrace-report-1.txt", "text": "x"})["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "Downloads")))

    def test_another_sites_page_cannot_drive_the_engine(self):
        app = App(persist=False, native=True)
        app.downloads = os.path.join(self.tmp.name, "Downloads")
        httpd, url = self.m.start_server(app, 0)

        def post(origin=None, ctype="application/json"):
            body = json.dumps({"cmd": "save_file", "name": "mixxtrace-report-1.txt", "text": "x"}).encode()
            headers = {"Content-Type": ctype}
            if origin:
                headers["Origin"] = origin
            try:
                with urllib.request.urlopen(urllib.request.Request(url + "api/cmd", body, headers), timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                exc.close()
                return exc.code

        try:
            self.assertEqual(post("https://evil.example", "text/plain"), 403)     # a form post from some web page
            self.assertEqual(post("null"), 403)
            self.assertFalse(os.path.exists(app.downloads))
            self.assertEqual(post(url.rstrip("/")), 200)                          # the interface itself
            self.assertEqual(post("http://localhost:8765"), 200)
            self.assertEqual(post(), 200)                                         # no browser involved
            self.assertEqual(len(os.listdir(app.downloads)), 3)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_mac_bundle_recipe_asks_for_microphone_and_network_and_ships_the_interface(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "packaging", "mac", "MixxTrace.spec"), encoding="utf-8") as fh:
            spec = fh.read()
        for needed in ("NSMicrophoneUsageDescription", "NSLocalNetworkUsageDescription", '"web"', "mixxtrace_app.py",
                       "MixxTrace.icns", "console=False"):
            self.assertIn(needed, spec)
        with open(os.path.join(root, "packaging", "mac", "MixxTrace.icns"), "rb") as fh:
            self.assertEqual(fh.read(4), b"icns")
        with open(os.path.join(root, "build-mac-app.command"), "rb") as fh:
            script = fh.read()
        self.assertTrue(script.startswith(b"#!/bin/bash\n"))
        self.assertNotIn(b"\r", script)                        # Windows line endings would stop it running on a Mac
