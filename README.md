# TuneTrace

Feedback finder, system-tuning trace and channel advice for live sound. It runs on your own
computer, listens through any audio interface, and shows its interface in your web browser.

| Tab | What it does |
| --- | --- |
| Feedback | Live spectrum of the measurement mic. A sustained, narrow, steady tone is flagged as **ringing** (watch it) or **feedback** (act now), with its frequency, note, the cut to make, and the input channel it was found on. |
| System tune | Dual-channel transfer function (magnitude, phase, coherence) of mic vs console output. Finds the delay, captures mic positions, builds the **best trace** from them, and fits a parametric EQ to your target curve. |
| Channels | Scans every input the interface delivers and lists what needs attention on each one (clipping, level, rumble, hum, build-ups, sibilance, feedback suspects), laid out for the console's input strip. |
| Setup | Interface, sample rate, which channels are the mic and the reference, console profile, mic calibration file, signal generator, session export. |

## Install and start

You need Python 3.9 or newer ([python.org/downloads](https://www.python.org/downloads/)). The first
start downloads two packages (numpy and sounddevice), so it needs internet once. After that
TuneTrace works offline.

**Mac**

1. Unzip the folder somewhere permanent (for example Documents).
2. Open Terminal, type `cd ` (with a space), drag the TuneTrace folder onto the window, press Return.
3. Run `bash start-mac.command`
4. The interface opens at http://127.0.0.1:8765/. The first time you start an input, macOS asks
   whether Terminal may use the microphone: allow it.

Double-clicking `start-mac.command` also works once macOS trusts it (right-click, Open, Open).

**Windows**

Double-click `start-windows.bat`. It turns on ASIO support, so ASIO drivers show up in the
interface list.

**By hand (any system)**

```
python3 -m pip install -r requirements.txt
python3 tunetrace.py            # add --demo to start on the built-in simulated show
python3 tunetrace.py --list-devices
```

Leave the Terminal window open while you work. Close it, or press Ctrl+C, to quit.

## Hooking up a DiGiCo Quantum 326

The console's built-in UB MADI port (USB Type B) is an audio interface with up to
48 channels each way. On macOS Catalina or later no driver is needed; on Windows install DiGiCo's
UB MADI driver and pick its ASIO entry.

1. **USB.** UB MADI port to the computer. In Setup choose the UB MADI interface, sample rate
   "Device default", and how many channels to open.
2. **Reference.** Copy what feeds the PA (the master or matrix you are tuning) to a UB MADI
   output socket. Select that channel as *Reference*.
3. **Measurement mic.** Plug it into a console preamp, send that input pre-processing to another
   UB MADI socket, keep it out of the mix. Select it as *Measurement mic*.
4. **Channels to scan.** Send the inputs you want advice on to the remaining sockets (the same
   patch you would use for a multitrack recording). Name them on the Channels tab and pick each
   source type, so the advice fits the instrument.
5. **Apply.** System EQ goes on the output or matrix EQ (8 bands). Channel suggestions are written
   for the input strip: gain trim, HPF, the four EQ bands (any of which can run dynamic), and the
   dynamics section.

TuneTrace does not change anything on the console. You enter the values, listen, and decide.

Any other interface works the same way. The minimum is two inputs: one for the mic, one for a
copy of the signal going to the PA. With one input you still get the Feedback tab.

## Tuning workflow

1. Play pink noise or music through the system (console oscillator, playback, or the generator
   in Setup). The same signal must reach the reference input.
2. **Find delay.** Lines the reference up with the mic. Check the polarity note.
3. **Check the measurement.** Coherence should be high (green). Low coherence means the mic is
   hearing something other than the system: raise the level, find the delay again, or move the mic.
4. **Capture three or more mic positions** across the audience area, finding the delay again at
   each. The best trace is their coherence-weighted average, each level-matched first, so one
   seat's reflections do not steer the EQ. Untick "Use" to leave a bad capture out.
5. **Choose a target** (flat, concert with a sub contour and soft top, warm, speech, or your
   own contour and tilt).
6. **Suggest EQ.** Up to eight bands, cuts preferred, boosts capped (3 dB by default) and kept
   broad, and no filter is placed where coherence is poor. The green trace is the predicted result.
7. Enter the bands on the console, restart the average and measure again.

## Feedback detection

A tone is scored on how far it stands above the spectrum on both sides, how long it has lasted,
how steady its level is, whether it has harmonics (feedback rarely does, instruments do) and
whether it is growing. Sensitivity (Low / Normal / High) sets how far a tone must stand out.

- **Feedback**: suggested cut 6 dB at Q 12. **Ringing**: 3 dB at Q 8. A frequency that comes back
  three times gets 3 dB more.
- When the same tone is on an input channel, the cut is recommended on that channel first, so the
  rest of the mix keeps its tone.

## What it can and cannot tell you

- Levels are dBFS at the interface, not SPL. There is no SPL calibration.
- The detector cannot know intent. A long, pure, steady synth or organ note looks like ringing,
  and can be flagged as such at Normal or High sensitivity. It is listed, not acted on.
- Channel advice is rule-based and conservative. It reads level, spectrum balance and steady
  tones; it does not hear the song. Treat every suggestion as a starting point and confirm by ear.
- An EQ fitted to a single mic position chases that seat's reflections. Use several positions.
- Automatic EQ fixes tonal balance. It does not fix time alignment, coverage or room decay.
- Without a mic calibration file, the mic's own response is part of the trace.

## How it was checked

`python3 -m unittest` runs 19 checks against simulated signals with a known answer: no alarms on
60 s of noise at every sensitivity; growing tones at 125 Hz to 8 kHz flagged at the right
frequency; held musical notes never called feedback; the transfer function, delay and polarity
matched against a simulated PA; 96 kHz input; EQ limits; channel advice on a simulated 12-channel
show; and the multichannel input path through a stand-in audio driver.

It has **not** yet been run against real audio hardware, a real console or a real room. Do a
soundcheck with it before relying on it at a show.

## Files

```
tunetrace.py        start here
tt/                 analysis (analyzer, eq, advisor), audio I/O (engine), app state, web server
web/                the interface
tests/              the checks described above
~/.tunetrace/       your saved session (traces, EQ, channel names, settings)
```

The server only listens on this computer (127.0.0.1). Nothing is sent anywhere.
