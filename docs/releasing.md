# Building a release

Release builds require Python 3.9 or newer plus `build` and `PyInstaller`:

```sh
python3 -m pip install build pyinstaller
python3 scripts/build_release.py
```

The build script runs the complete test suite, creates the Python wheel and
source distribution, freezes the terminal installer as a standalone native
executable, assembles the double-clickable macOS ZIP, and writes SHA-256
checksums beneath `release/`.

PyInstaller binaries inherit the architecture and minimum macOS compatibility
of the Python runtime used for the build. Never rename a native archive to
claim architectures or macOS versions that were not actually built and tested.

The generated executable receives an ad-hoc signature. Public distribution
without Gatekeeper warnings requires an Apple Developer ID certificate and
notarization, which are intentionally outside this local build process.
