# Mixx Trace

Feedback finder, system-tuning trace, system alignment and channel advice for live sound. It runs
on your own computer (macOS or Windows), listens through any audio interface, shows its interface
in your web browser (or in its own window as a Mac app), and can send the system EQ and alignment delay to a DiGiCo console.

| Tab | What it does |
| --- | --- |
| Feedback | Live spectrum of the measurement mic. A sustained, narrow, steady tone is flagged as **ringing** (watch it) or **feedback** (act now), with its frequency, note, the cut to make, and the input channel it was found on. |
| System tune | Dual-channel transfer function (magnitude, phase, coherence) of mic vs console output. Finds the delay, captures mic positions, builds the **best trace** from them, and fits a parametric EQ to your target curve. |
| Align | Measures two systems one at a time (mains and subs, mains and fills) and works out the delay and polarity that make them add best where they overlap. Shows the sum now and the sum once aligned. |
| Channels | Scans every input the interface delivers and lists what needs attention on each one (clipping, level, rumble, hum, build-ups, sibilance, feedback suspects), laid out for the console's input strip. |
| Setup | Interface, sample rate, which channels are the mic and the reference, console profile, mic calibration file, signal generator, session export, console remote. |

## Install and start

Mixx Trace runs on macOS and Windows from the same folder. You need Python 3.9 or newer
([python.org/downloads](https://www.python.org/downloads/)). The first start downloads two
packages (numpy and sounddevice), so it needs internet once. After that it works offline.

**Mac**

1. Unzip the folder somewhere permanent (for example Documents).
2. Open Terminal (Command-Space, type Terminal, Return). Type `cd ` (with a space), drag the
   Mixx Trace folder onto the window, press Return, then run `bash start-mac.command`.
   The first run takes about a minute.
3. Double-clicking `start-mac.command` also works, but macOS blocks it the first time because the
   file came from the internet. Click Done, open System Settings, Privacy & Security, scroll to
   Security and press Open Anyway (the button is there for about an hour after the blocked
   attempt). On macOS 14 or earlier you can instead right-click the file and choose Open.
4. The interface opens at http://127.0.0.1:8765/. The first time you start an input, macOS asks
   whether Terminal may use the microphone: allow it.

**Mac app (one icon, no Terminal, no browser)**

You build the app once on your own Mac. A Mac app cannot be built on another kind of computer, and
one built on your own Mac opens without the "downloaded from the internet" block.

1. Install Python from [python.org/downloads](https://www.python.org/downloads/) if you have not already.
   The build also needs Apple's free command line developer tools. If they are missing, the build
   script opens Apple's installer for them: click Install, wait for it to finish, then run
   the script again. You can also start that yourself with `xcode-select --install`.
2. Open Terminal, type `cd ` (with a space), drag the Mixx Trace folder onto the window, press
   Return, then run `bash build-mac-app.command`. It needs internet and takes a few minutes. It
   downloads the build tools into the folder, builds `Mixx Trace.app`, checks it, and offers to put
   it in Applications.
3. Open Mixx Trace from Applications or Launchpad like any other app, and keep it in the Dock if
   you like. Closing the window quits it and stops the audio.
4. The first time you start an input, macOS asks whether Mixx Trace may use the microphone: allow
   it. If the meters stay flat, switch Mixx Trace on in System Settings, Privacy & Security, Microphone.
5. The first time you use Console remote on macOS 15 or later, macOS asks whether Mixx Trace may
   find devices on your local network: allow it (System Settings, Privacy & Security, Local Network).

Good to know:

- Reports and exported sessions are saved straight into your Downloads folder.
- The app is signed locally, not by Apple. Apple notes that the Local Network permission is only
  tracked reliably for apps signed with an Apple-issued identity. If the console will not answer
  from the app, run `bash start-mac.command` instead: programs started from Terminal do not need
  that permission.
- After an update, run `bash build-mac-app.command` again. macOS may ask for the microphone again,
  because a rebuilt app counts as a new one.
- If the app will not open or misbehaves, the details are in `.mixxtrace/app.log` in your home
  folder (in Finder: Go, Go to Folder, `~/.mixxtrace`). If the build fails, they are in
  `build-mac-app.log` in the Mixx Trace folder.
- The app uses the same engine, session and settings as the Terminal version, so you can switch
  between them.

**Windows 10 or 11**

1. Install Python from python.org and tick "Add python.exe to PATH" in the installer.
2. Unzip the folder somewhere permanent, then double-click `start-windows.bat`. If SmartScreen
   shows "Windows protected your PC", choose More info, Run anyway.
3. The interface opens at http://127.0.0.1:8765/. ASIO support is switched on, so ASIO drivers
   appear in the interface list next to the WASAPI ones. If Windows blocks the microphone, turn on
   Settings, Privacy & security, Microphone, "Let desktop apps access your microphone".
4. The first time you use Console remote, Windows Firewall asks whether Python may receive on the
   network: allow it on Private networks, or the console's answers never arrive.

**By hand (any system)**

```
python3 -m pip install -r requirements.txt      (Windows: py -m pip install -r requirements.txt)
python3 mixxtrace.py                            (Windows: py mixxtrace.py)
python3 mixxtrace.py --demo                     start on the built-in simulated show
python3 mixxtrace.py --list-devices
```

Leave the Terminal or Command Prompt window open while you work. Close it, or press Ctrl+C, to quit.

Your session is saved in a `.mixxtrace` folder in your home folder on both systems, and an
exported session file opens on either one.

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
5. **Apply.** System EQ goes on the output or matrix EQ (8 bands), typed in by hand or sent with
   Console remote (below). Channel suggestions are written for the input strip: gain trim, HPF,
   the four EQ bands (any of which can run dynamic), and the dynamics section.

Unless you turn on Console remote and press Send, Mixx Trace changes nothing on the console.
Channel suggestions are never sent: you enter those, listen, and decide.

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

## Aligning two systems

Use the Align tab for mains and subs, mains and front fills, or any two systems that overlap.

1. Name the two systems and, if you want to send the result, pick the console output feeding each.
2. Mute everything except the first system, play pink noise, press **Measure**. Then the same for
   the second. The mic must stay in one place for both, and the reference must not change.
3. Mixx Trace shows which system to delay and by how much (ms, metres, feet, samples), whether to
   flip the polarity of the second system, the frequency range where the two overlap, and how many
   dB the sum gains there. The plot shows each system, the sum as it is now (dashed) and the sum
   once aligned (green). In the phase plot, the dashed line is the second system after the
   correction; it should lie on the first one between the two markers.
4. Set the delay and polarity on the console, or press **Send to console**.
5. Unmute both and press **Check live**. The live trace should follow the green line.

How it decides: over the range where both systems are within 12 dB of each other, it tries every
delay with and without a polarity flip and keeps the one with the highest summed level. Across a
narrow crossover, "flip and delay a little more" and "no flip and delay a little less" can add up
almost the same. When they are within 0.5 dB, it picks the one whose phase traces stay together
over the wider range, and shows the other as a **close call** so you can listen to both.

On System tune, **Keep tracking when the mic moves** re-checks the reference delay every few
seconds and follows it when the mic is carried to a new position. It only moves on a confident
change, so the trace does not jump while you are measuring.

## Console remote (DiGiCo)

Mixx Trace can write the fitted system EQ to one console output, and the alignment delay and
polarity to the outputs you picked on the Align tab. It is off until you turn it on in Setup.

On the console:

1. Put the computer and the console on the same network. Open External Control and enable it.
2. Add a device of type **DiGiCo Pad** (not "Other OSC") with this computer's IP address. The
   console's receive port and send port must match the two port fields in Setup (7001 and 7000
   unless you changed them).
3. Load the Quantum command file (`ipad_q3`) under Commands Allowed. Set Bundles off and Suppress
   OSC Retransmit on.
4. Close any other iPad or remote app. The console talks to one Pad device at a time.

In Mixx Trace: Setup, Console remote, choose "DiGiCo console on the network", enter the console's
IP address, pick the output the system EQ goes to, and press **Test connection**. "Simulated
console (practice)" behaves the same way without a console; with the demo signal, the simulated
PA follows what you send, so you can see the result.

What happens when you press **Send to console**:

- The console is read first. A table shows every setting as it is now and what it would become.
  Nothing has been written at this point.
- Only after you press Send are the values written, one at a time, 100 ms apart. Settings that
  already hold the right value are skipped. Each band's gain is flattened before its frequency
  and Q move, so the output never passes through a loud in-between state.
- Every value is read back. If the console holds something different, you are told which one.
- The values from before the send are kept. **Undo** puts them back, and **EQ in / EQ out**
  switches the sent EQ out and in for an A/B comparison.
- A preview is valid for two minutes and for one send. If the target output changes in between,
  it is refused and you preview again.

Limits you should know:

- Remote EQ and delay work on **Group, Aux and Matrix** outputs. Feed the PA from one of those.
- Gain is kept within ±18 dB, Q within 0.1 to 20, delay within 0 to 1300 ms.
- Bands 1 to 4 are used on every output. Bands 5 to 8 are only used when the console answers for
  them; if it does not, Mixx Trace offers to fit the EQ again with four bands.
- DiGiCo does not publish this message list. The addresses come from two independent open-source
  controllers for these consoles, [Bitfocus Companion's DiGiCo module](https://github.com/bitfocus/companion-module-digico-osc)
  and [JPMixer](https://github.com/JPMixing-inc/jpmixer). They have not been tried on a Quantum 326
  from this program. **Try it on a spare or muted output first**, and check the result on the
  console screen, before you use it on a live PA.

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
- Automatic EQ fixes tonal balance. It does not fix coverage or room decay.
- Alignment is right for the mic position you measured at. Away from it the path lengths change,
  so pick the position that matters most (usually mix position or the centre of the audience).
- Without a mic calibration file, the mic's own response is part of the trace.

## How it was checked

`python3 -m unittest` (Windows: `py -m unittest`) runs 55 checks against simulated signals with a
known answer: no alarms on 60 s of noise at every sensitivity; growing tones at 125 Hz to 8 kHz
flagged at the right frequency; held musical notes never called feedback; the transfer function,
delay and polarity matched against a simulated PA; 96 kHz input; EQ limits; channel advice on a
simulated 12-channel show; the multichannel input path through a stand-in audio driver; alignment
of ideal crossovers with a known answer; and the console remote against a stand-in console over
real network messages (exact messages and order, limits, preview writes nothing, refusal without
confirmation, read-back, undo, a console that does not answer).

The Mac app was checked as far as Linux allows: the same build recipe was run on Linux and the
built program passed its own check (interface files inside, engine answering, demo running, audio
library and window parts loading, with the Linux audio library supplied by hand); the
app's start-up code was run with a real app window (pywebview on its Linux window engine, not the
Mac one), where the interface loaded, the demo ran, and Export session and Report saved into
Downloads, and closing the window shut everything down; and the Mac versions of every package it
needs were confirmed to exist (Python 3.12, 3.14 and 3.15 on Apple silicon, 3.13 on Intel). On a
real Mac (macOS 26, Apple silicon, Python 3.15) the build has so far been seen to get as far as
assembling the program, where it stopped for want of Apple's command line developer tools; the
script now checks for them first. The finished app, the Mac window, the permission prompts and
the icon in Finder have not yet been seen running on a Mac.

It has **not** yet been run against real audio hardware, a real console, a real room, or on a
Windows or Mac machine: it was developed and checked on Linux, with care taken over the parts
that differ between systems. Do a soundcheck with it before relying on it at a show.

## Files

```
mixxtrace.py        start here
LICENSE             MIT License
THIRD-PARTY-NOTICES.md   licences of the software Mixx Trace relies on
CONTRIBUTING.md     how to report problems and send changes
tt/                 analysis (analyzer, eq, align, advisor), audio I/O (engine), console remote (osc),
                    app state, web server
start-mac.command   Terminal launcher for macOS (interface in the browser)
build-mac-app.command   builds Mixx Trace.app on your Mac
mixxtrace_app.py    what the Mac app runs: the same engine in its own window
packaging/mac/      build recipe, icon and extra requirements for the Mac app
start-windows.bat   double-click launcher for Windows
web/                the interface
tests/              the checks described above
~/.mixxtrace/       your saved session (traces, EQ, channel names, settings)
```

The interface is only reachable from this computer (127.0.0.1). The only thing Mixx Trace sends
over the network is console remote messages, to the console address you entered, when you press
Send or Test connection.

## License

Mixx Trace is open source under the [MIT License](LICENSE). You may use, copy, change and share
it, including commercially, as long as the copyright notice and the licence text stay with it.
It comes with no warranty: check its advice by ear and on a muted or spare output before you
rely on it at a show.

Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP).

The licence covers the code. The name "Mixx Trace" and its logo are not licensed with it; if you
publish a changed version, give it a different name. The software Mixx Trace relies on is listed
in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md). To report a problem or send a change, see
[CONTRIBUTING.md](CONTRIBUTING.md).

DiGiCo and Quantum are trademarks of their owner. Mixx Trace is an independent project and is
not affiliated with or endorsed by DiGiCo.

Mixx Trace was specified, directed and reviewed by XAVGROUP; most of its code was written with
an AI assistant.
