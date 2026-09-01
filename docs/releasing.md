# Building a release

Release builds require Python 3.9 or newer plus `build` and `PyInstaller`.
Create one environment per target architecture; on Apple Silicon, Apple's
universal Python can create the Intel environment under Rosetta:

```sh
python3 -m pip install build pyinstaller
arch -x86_64 /usr/bin/python3 -m venv /private/tmp/wir-schaffen-x86
arch -x86_64 /private/tmp/wir-schaffen-x86/bin/python -m pip install \
  'setuptools>=77' wheel build 'pyinstaller==6.15.0'

python3 scripts/build_release.py \
  --architectures arm64 x86_64 \
  --arm-python .venv/bin/python \
  --x86-python /private/tmp/wir-schaffen-x86/bin/python
```

The build script runs the complete test suite, creates the Python wheel and
source distribution, freezes the terminal installer as a standalone native
executables, asserts that each is a thin binary of the requested architecture,
smoke-tests each frozen CLI, assembles both double-clickable macOS ZIPs, and
writes SHA-256 checksums beneath `release/`.

PyInstaller binaries inherit the architecture and minimum macOS compatibility
of the Python runtime used for the build. Never rename a native archive to
claim architectures or macOS versions that were not actually built and tested.

The generated executable receives an ad-hoc signature. Public distribution
without Gatekeeper warnings requires an Apple Developer ID certificate and
notarization, which are intentionally outside this local build process.
