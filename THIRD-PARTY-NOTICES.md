# Third-party notices

Mixx Trace's own code is released under the MIT License (see `LICENSE`). Nothing from another
project is copied into this repository. It relies on the software below, which you install
separately and which stays under its own licence.

## Needed to run

| Software | Used for | Licence |
| --- | --- | --- |
| [Python](https://www.python.org/) | the language it is written in | PSF License |
| [NumPy](https://numpy.org/) | signal analysis | BSD-3-Clause (its packages bundle parts under 0BSD, MIT, Zlib and CC0-1.0) |
| [sounddevice](https://python-sounddevice.readthedocs.io/) | audio input and output | MIT |
| [PortAudio](https://www.portaudio.com/) (comes with sounddevice) | talks to the audio driver | MIT-style PortAudio licence |

## Needed only to build the Mac app

| Software | Used for | Licence |
| --- | --- | --- |
| [pywebview](https://pywebview.flowrl.com/) | the app window | BSD-3-Clause |
| [PyObjC](https://pyobjc.readthedocs.io/) (installed by pywebview) | bridge to macOS | MIT |
| [PyInstaller](https://pyinstaller.org/) | packs the app | GPL-2.0-or-later with a bootloader exception that lets the packed app carry any licence |

A built `Mixx Trace.app` contains copies of Python, NumPy, sounddevice, PortAudio, pywebview and
PyObjC. If you pass a built app on to someone else, include this file and the licence texts of
those projects with it.

## Fonts

The interface asks for two typefaces from their public font services when the computer is
online, and falls back to system fonts when it is not. No font files are in this repository.

| Typeface | Source | Licence |
| --- | --- | --- |
| Switzer | [Fontshare](https://www.fontshare.com/fonts/switzer) | ITF Free Font License |
| JetBrains Mono | [Google Fonts](https://fonts.google.com/specimen/JetBrains+Mono) | SIL Open Font License 1.1 |

## References

The console remote's OSC addresses follow DiGiCo's iPad command set as it is documented by two
open controllers. Their documentation was read; none of their code is used here.

- [Bitfocus Companion DiGiCo module](https://github.com/bitfocus/companion-module-digico-osc) (MIT)
- [JPMixer](https://github.com/JPMixing-inc/jpmixer)

## Trademarks

DiGiCo and Quantum are trademarks of their owner. macOS is a trademark of Apple Inc. and Windows
is a trademark of Microsoft Corporation. Mixx Trace is an independent project and is not
affiliated with, endorsed by or supported by any of them.
