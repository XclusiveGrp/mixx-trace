# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP)
# PyInstaller recipe for the Mixx Trace Mac app. Run through build-mac-app.command, not by hand.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
sys.path.insert(0, ROOT)
from tt import __version__  # noqa: E402

EXCLUDES = ["tkinter", "matplotlib", "scipy", "pandas", "PIL", "IPython", "pytest", "numpy.f2py",
            # the Mac window is built into macOS; never drag in another window toolkit
            "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "gi", "cefpython3", "clr",
            "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef",
            "webview.platforms.winforms", "webview.platforms.edgechromium", "webview.platforms.mshtml",
            "webview.platforms.android"]

a = Analysis(
    [os.path.join(ROOT, "mixxtrace_app.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "web"), "web")],
    hiddenimports=["sounddevice", "webview"],          # both are imported lazily, so name them
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Mixx Trace",
    console=False,
    upx=False,
    argv_emulation=False,
    target_arch=None,                                   # build for the Mac it is built on
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name="Mixx Trace")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Mixx Trace.app",
        icon=os.path.join(SPECPATH, "MixxTrace.icns"),
        bundle_identifier="com.xavgroup.mixxtrace",
        info_plist={
            "CFBundleName": "Mixx Trace",
            "CFBundleDisplayName": "Mixx Trace",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.music",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # Without these two, macOS refuses audio input and blocks the link to the console without asking.
            "NSMicrophoneUsageDescription":
                "Mixx Trace listens to your audio interface or measurement microphone to analyze the sound system.",
            "NSLocalNetworkUsageDescription":
                "Mixx Trace sends EQ and delay settings to your mixing console over the local network.",
            "NSHumanReadableCopyright":
                "Copyright (c) 2026 Xclusive Audio Visual Group (XAVGROUP). MIT License.",
        },
    )
