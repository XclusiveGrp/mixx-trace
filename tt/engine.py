"""Audio I/O: live capture through PortAudio (any interface), demo source, signal generator."""
import math
import os
import queue
import threading
import time

import numpy as np

from . import dsp

os.environ.setdefault("SD_ENABLE_ASIO", "1")
try:
    import sounddevice as sd
    SD_ERROR = None
except Exception as exc:  # missing module or PortAudio library
    sd = None
    SD_ERROR = str(exc)

MAX_CH = 64


class DemoSource:
    """Simulated show: reference, measurement mic through a coloured PA, and ten input channels."""
    SR = 48000
    BLOCK = 2048
    CHANNELS = [
        ("Ref (console out)", "auto"), ("Meas mic", "auto"), ("Kick", "kick"), ("Snare", "snare"),
        ("Bass DI", "bass"), ("E-Gtr", "egtr"), ("Ac Gtr", "agtr"), ("Lead Vox", "vocal"),
        ("BV", "vocal"), ("OH", "overhead"), ("Keys", "keys"), ("Tracks", "playback"),
    ]
    PA_DELAY = 1234
    # what the simulated PA does to the signal (the "truth" the tuning view should uncover)
    PA_FILTERS = [("hp", 45, 0.54, 0), ("hp", 45, 1.31, 0), ("peak", 125, 2.0, 6.0), ("peak", 800, 3.0, -4.0),
                  ("peak", 2500, 4.0, 5.0), ("hs", 9000, 0.707, -5.0), ("lp", 17000, 0.707, 0)]
    # mic positions: (delay samples, reflection delay, reflection gain, HF shelf dB, level)
    POSITIONS = [(1234, 149, 0.22, 0.0, 0.70), (1310, 211, 0.25, -1.5, 0.62), (1175, 97, 0.20, 1.0, 0.75)]

    def __init__(self, seed=7):
        sr, n = self.SR, self.SR * 8
        rng = np.random.default_rng(seed)
        f = np.fft.rfftfreq(n, 1.0 / sr)
        fs = np.maximum(f, 1.0)
        t = np.arange(n) / sr

        def white():
            return rng.standard_normal(n)

        def shape(x, h):
            return np.fft.irfft(np.fft.rfft(x) * h, n)

        def norm(x, peak):
            return x * (peak / np.max(np.abs(x)))

        def bp(f0, q):
            return 1.0 / np.sqrt(1.0 + (q * (fs / f0 - f0 / fs)) ** 2)

        def hp(fc, order=4):
            return 1.0 / np.sqrt(1.0 + (fc / fs) ** (2 * order))

        def lp(fc, order=4):
            return 1.0 / np.sqrt(1.0 + (fs / fc) ** (2 * order))

        def pink(rms):
            x = shape(white(), np.where(f >= 15, 1.0 / np.sqrt(np.maximum(f, 15.0)), 0.0))
            return x * (rms / np.std(x))

        ch = np.zeros((n, len(self.CHANNELS)))
        ref = pink(0.1)
        ch[:, 0] = ref
        w = 2 * np.pi * f / sr
        h_sys = dsp.chain_response(self.PA_FILTERS, f, sr)
        self.meas = []
        for delay, r_delay, r_gain, hf_db, level in self.POSITIONS:
            h = level * h_sys * dsp.chain_response([("hs", 6000, 0.707, hf_db)], f, sr)
            h = h * (1.0 + r_gain * np.exp(-1j * w * r_delay)) * np.exp(-1j * w * delay)
            self.meas.append((shape(ref, h) + pink(0.0025)).astype(np.float32))
        ch[:, 1] = self.meas[0]

        tau = t % 0.5
        kick_tone = np.sin(2 * np.pi * (48 * tau + 40 * 0.03 * (1 - np.exp(-tau / 0.03)))) * np.exp(-tau / 0.09)
        box = shape(white() * np.exp(-tau / 0.15), bp(400, 5.0))
        kick = 0.6 * kick_tone + 0.55 * box / np.max(np.abs(box))
        ch[:, 2] = norm(kick, 0.3)

        tau2 = (t + 0.25) % 0.5
        sn = 0.5 * np.sin(2 * np.pi * 190 * tau2) * np.exp(-tau2 / 0.05)
        sn_noise = shape(white() * np.exp(-tau2 / 0.07), hp(700, 2) * lp(9000, 2))
        ch[:, 3] = norm(sn + 0.6 * sn_noise / np.max(np.abs(sn_noise)), 0.25)

        bass = np.zeros(n)
        for i, note in enumerate([41.2, 55.0, 73.4, 82.4, None, None, None, None] * 2):
            if note is None:
                continue
            seg = slice(i * sr // 2, (i + 1) * sr // 2)
            ts = t[seg] - t[seg][0]
            env = np.exp(-ts / 0.12) * (1 - np.exp(-ts / 0.004))
            bass[seg] = env * sum(a * np.sin(2 * np.pi * note * k * ts) for k, a in ((1, 1.0), (2, 0.5), (3, 0.25), (4, 0.12)))
        bass = norm(bass, 0.3)
        for hf, amp in ((60, 0.012), (120, 0.008), (180, 0.005)):
            bass += amp * np.sin(2 * np.pi * hf * t)
        ch[:, 4] = bass

        strum = 0.35 + 0.65 * np.exp(-(t % 0.25) / 0.12)
        eg = shape(white(), hp(110, 2) * lp(4500, 2) / np.sqrt(fs)) * strum
        ch[:, 5] = np.clip(eg * (1.0 / np.quantile(np.abs(eg), 0.997)), -1.0, 1.0)

        ag = shape(white(), hp(100, 3) * lp(7000, 2) / np.sqrt(fs)) * np.exp(-(t % 0.25) / 0.1)
        ch[:, 6] = norm(ag, 0.2)

        def voice(rms, lowcut):
            form = (bp(500, 2.0) + 0.7 * bp(1500, 3.0) + 0.5 * bp(2600, 3.0)) * hp(lowcut, 3) * lp(5000, 2)
            syl = (0.5 + 0.5 * np.sin(2 * np.pi * 3.1 * t)) ** 2 * ((t % 4.0) < 3.0)
            x = shape(white(), form) * syl
            return x * (rms / np.std(x))

        vox = voice(0.06, 90)
        ess = shape(white(), hp(5000, 4) * lp(9000, 4)) * (((t * 1.5) % 1.0) < 0.12) * ((t % 4.0) < 3.0)
        vox += ess * (0.055 / np.std(ess))
        rumble = shape(white(), hp(30, 4) * lp(75, 4))
        vox += rumble * (0.02 / np.std(rumble))
        ch[:, 7] = vox
        ch[:, 8] = voice(0.03, 130)

        wash = shape(white(), hp(3000, 2)) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t) ** 2)
        ch[:, 9] = norm(wash, 0.12) + 0.3 * ch[:, 2]

        keys = sum(np.sin(2 * np.pi * fr * t) + 0.3 * np.sin(2 * np.pi * 2 * fr * t)
                   for fr in (220.0, 277.2, 329.6, 440.0, 554.4))
        ch[:, 10] = norm(keys * (0.6 + 0.4 * np.sin(2 * np.pi * 0.25 * t)), 0.006)

        self.loop = ch.astype(np.float32)
        self.position = 0
        self.pos = 0
        self.total = 0
        self.phase = [0.0, 0.0]

    @staticmethod
    def _fb_db(t, start, peak_db):
        """Level of a feedback tone that builds, holds, then gets pulled."""
        x = t - start
        if x < 0 or x > 6.5:
            return None
        if x < 3.5:
            return -62.0 + x / 3.5 * (peak_db + 62.0)
        if x < 5.5:
            return peak_db
        return peak_db - (x - 5.5) * 60.0

    def next_block(self):
        n = self.BLOCK
        idx = (self.pos + np.arange(n)) % len(self.loop)
        out = self.loop[idx].copy()
        if self.position:
            out[:, 1] = self.meas[self.position][idx]
        t = (self.total / self.SR) % 24.0
        for j, (start, freq, peak_db, src) in enumerate(((3.0, 2480.0, -16.0, 7), (14.0, 196.0, -18.0, 6))):
            db = self._fb_db(t, start, peak_db)
            if db is None:
                continue
            ph = self.phase[j] + 2 * np.pi * freq * np.arange(n) / self.SR
            self.phase[j] = (self.phase[j] + 2 * np.pi * freq * n / self.SR) % (2 * np.pi)
            s = (10 ** (db / 20.0)) * np.sin(ph)
            out[:, 1] += s.astype(np.float32)
            out[:, src] += (1.5 * s).astype(np.float32)
        self.pos = (self.pos + n) % len(self.loop)
        self.total += n
        return out


class Generator:
    """Pink noise / sine out of any output device and channel."""

    def __init__(self):
        self.stream = None
        self.cfg = {"on": False, "type": "pink", "freq": 1000.0, "level": -24.0, "device": None, "channels": [1]}
        self.error = None
        self._pink = None
        self._pos = 0
        self._phase = 0.0

    def _make_pink(self, sr):
        n = 1 << 19
        rng = np.random.default_rng(3)
        f = np.fft.rfftfreq(n, 1.0 / sr)
        x = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * np.where(f >= 15, 1 / np.sqrt(np.maximum(f, 15)), 0), n)
        return (x / np.std(x)).astype(np.float32)

    def start(self, cfg):
        self.stop()
        self.cfg.update(cfg)
        self.cfg["on"] = False
        self.error = None
        if sd is None:
            self.error = "Audio output is unavailable: " + (SD_ERROR or "")
            return
        try:
            dev = self.cfg["device"]
            info = sd.query_devices(dev, "output")
            nch = int(min(info["max_output_channels"], MAX_CH))
            sr = float(info["default_samplerate"])
            self._pink = self._make_pink(sr)
            chans = [c - 1 for c in self.cfg["channels"] if 1 <= c <= nch] or [0]

            def cb(outdata, frames, time_info, status):
                outdata.fill(0)
                amp = 10 ** (self.cfg["level"] / 20.0)
                if self.cfg["type"] == "sine":
                    ph = self._phase + 2 * np.pi * self.cfg["freq"] * np.arange(frames) / sr
                    self._phase = (self._phase + 2 * np.pi * self.cfg["freq"] * frames / sr) % (2 * np.pi)
                    sig = amp * np.sin(ph)
                else:
                    idx = (self._pos + np.arange(frames)) % len(self._pink)
                    self._pos = (self._pos + frames) % len(self._pink)
                    sig = amp * self._pink[idx]          # level = RMS for noise
                for c in chans:
                    outdata[:, c] = sig

            self.stream = sd.OutputStream(device=dev, samplerate=sr, channels=nch, dtype="float32", callback=cb)
            self.stream.start()
            self.cfg["on"] = True
        except Exception as exc:
            self.error = str(exc)
            self.stream = None

    def stop(self):
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
        self.stream = None
        self.cfg["on"] = False


class Engine:
    def __init__(self, on_block):
        self.on_block = on_block
        self.mode = "stopped"
        self.sr = 48000.0
        self.nch = 0
        self.device_name = ""
        self.error = None
        self.dropped = 0
        self.q = queue.Queue(maxsize=600)
        self._stream = None
        self._run = False
        self._threads = []
        self.demo = None

    # -- devices
    @staticmethod
    def live_available():
        return sd is not None

    @staticmethod
    def list_devices():
        if sd is None:
            return {"inputs": [], "outputs": [], "error": SD_ERROR}
        try:
            sd._terminate()
            sd._initialize()            # refresh hot-plugged interfaces
        except Exception:
            pass
        apis = sd.query_hostapis()
        ins, outs = [], []
        try:
            d_in, d_out = sd.default.device
        except Exception:
            d_in, d_out = -1, -1
        for i, d in enumerate(sd.query_devices()):
            item = {"id": i, "name": d["name"], "api": apis[d["hostapi"]]["name"],
                    "sr": d["default_samplerate"]}
            if d["max_input_channels"] > 0:
                ins.append(dict(item, ch=int(d["max_input_channels"]), default=(i == d_in)))
            if d["max_output_channels"] > 0:
                outs.append(dict(item, ch=int(d["max_output_channels"]), default=(i == d_out)))
        return {"inputs": ins, "outputs": outs, "error": None}

    # -- lifecycle
    def _worker(self):
        while self._run:
            try:
                block = self.q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.on_block(block)
            except Exception as exc:        # keep the audio thread alive, surface the problem
                self.error = "Analysis error: %s" % exc

    def _begin(self):
        self._run = True
        self.error = None
        self.dropped = 0
        th = threading.Thread(target=self._worker, daemon=True)
        th.start()
        self._threads = [th]

    def start_demo(self):
        self.stop()
        if self.demo is None:
            self.demo = DemoSource()
        self.sr, self.nch = float(DemoSource.SR), len(DemoSource.CHANNELS)
        self.device_name = "Demo signal"
        self.mode = "demo"
        self._begin()

        def feed():
            t0 = time.monotonic()
            made = 0
            while self._run and self.mode == "demo":
                due = (time.monotonic() - t0) * self.sr
                if due - made > self.sr:            # machine was asleep: do not burst
                    t0 = time.monotonic() - made / self.sr
                    due = made
                while made + DemoSource.BLOCK <= due:
                    self._push(self.demo.next_block())
                    made += DemoSource.BLOCK
                time.sleep(0.01)

        th = threading.Thread(target=feed, daemon=True)
        th.start()
        self._threads.append(th)

    def start_live(self, device=None, sr=None, channels=None):
        self.stop()
        if sd is None:
            self.error = "Live input needs the sounddevice package (PortAudio): %s" % SD_ERROR
            return False
        try:
            info = sd.query_devices(device, "input")
            nch = int(min(info["max_input_channels"], MAX_CH))
            if channels:
                nch = max(1, min(nch, int(channels)))
            rate = float(sr or info["default_samplerate"])

            def cb(indata, frames, time_info, status):
                self._push(indata.copy())

            self._stream = sd.InputStream(device=device, samplerate=rate, channels=nch, dtype="float32",
                                          blocksize=1024, callback=cb)
            self.sr, self.nch = float(self._stream.samplerate), nch
            self.device_name = info["name"]
            self.mode = "live"
            self._begin()
            self._stream.start()
            return True
        except Exception as exc:
            self.error = "Could not open the input device: %s" % exc
            self.mode = "stopped"
            self._stream = None
            return False

    def _push(self, block):
        try:
            self.q.put_nowait(block)
        except queue.Full:
            self.dropped += 1

    def stop(self):
        self._run = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        for th in self._threads:
            th.join(timeout=1.0)
        self._threads = []
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self.mode = "stopped"
