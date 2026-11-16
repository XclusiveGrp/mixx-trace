# Contributing to Mixx Trace

Bug reports, measurements from real rigs and code are all welcome.

## Reporting a problem

Open an issue and include:

- your computer and operating system, and your Python version (`python3 --version`)
- the audio interface and, if the console remote is involved, the console model and software version
- what you did, what you expected, and what happened
- the session export from the Setup tab if the problem is about a measurement

Reports from real consoles are especially useful: the console remote has so far only been checked
against a simulated console.

## Changing the code

1. Install the two requirements: `pip install -r requirements.txt`
2. Run the checks from the project folder: `python3 -m unittest -v`. They need no audio hardware.
3. Try your change with the built-in demo signal: `python3 mixxtrace.py --demo-only`
4. Keep each pull request to one change, and add a check for anything the analysis is expected to get right.

New source files start with the same two lines as the existing ones:

```
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP)
```

## Licence of contributions

By sending a contribution you agree that it is released under the MIT License in `LICENSE`, and
that you have the right to release it that way.
