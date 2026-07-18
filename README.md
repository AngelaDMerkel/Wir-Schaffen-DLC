# Civ5 Mod DLC Packer

Civ5 Mod DLC Packer converts Civilization V ModBuddy mods into DLC directories
that the game loads before multiplayer setup. Its primary target is the native
macOS version of Civilization V, where runtime patching is impractical.

The tool validates the ModBuddy manifest, translates database XML and
localization, flattens and deduplicates the virtual asset namespace, packages
maps and Lua entry points, and writes a deterministic `.Civ5Pkg` manifest.

## Supported content

- Database and localization XML
- Imported art, audio, Lua, and UI overrides
- `.Civ5Map` files and Lua map scripts
- Imported leader scenes
- `InGameUIAddin` Lua entry points
- Future Worlds v6, including its SQL and era-popup compatibility layer
- Corporations (Brave New World) v1, using DLC-safe sidecar configuration
  tables in place of late core-schema changes

Arbitrary SQL is rejected unless the mod has an explicit compatibility
compiler. Producing no package is safer than silently dropping gameplay data.

## Requirements

- Python 3.9 or newer
- Civilization V with Brave New World for the included compatibility adapters
- The unpacked ModBuddy mod directory

No third-party Python packages are required.

## AngelaDMerkel's: Wir Schaffen DLC

The macOS terminal installer discovers Civilization V, reads the mods already
installed in its `MODS` directory, and presents a numbered selection screen.
It packages every selected mod first and only then asks permission to copy the
validated packages into the game's DLC directory.

Quit Civilization V, then double-click `wir-schaffen-dlc.command` in Finder or
run it from Terminal:

```sh
./wir-schaffen-dlc.command
```

The interface is intentionally small: select entries with input such as
`1,3-5` or `all`, review any compatibility warnings, and confirm installation.
Existing packages created by the installer are never replaced without an
additional confirmation.

After a `pip` installation, the same interface is available as
`wir-schaffen-dlc`.

## Release 0.1.0

Release artifacts include a standalone, double-clickable macOS terminal bundle
and portable Python wheel/source packages. Native bundles are labeled with the
architecture and macOS generation they were built for; do not use an
incompatible native archive on a different Mac.

The standalone binary is ad-hoc signed rather than Apple-notarized. On its
first launch, macOS may require Control-clicking `Wir Schaffen DLC.command` and
choosing **Open**. See the [changelog](CHANGELOG.md) and
[release-build instructions](docs/releasing.md) for details.

## Direct packer usage

```sh
python3 civ5_dlc_packer.py \
  "/path/to/MODS/Mod Name (v 1)" \
  "build/packed-dlc/Mod Name (v 1)" \
  --base-db "$HOME/Library/Application Support/Sid Meier's Civilization 5/cache/Civ5CoreDatabase.db" \
  --game-assets "$HOME/Library/Application Support/Steam/steamapps/common/Sid Meier's Civilization V/Civilization V.app/Contents/Assets/Assets" \
  --ui-set Expansion2
```

With the package installed through `pip`, the equivalent direct command is
`civ5-dlc-packer`.

Install the complete generated folder beneath:

```text
Civilization V.app/Contents/Assets/Assets/DLC/
```

Every multiplayer participant needs a byte-identical package and the same
expansion/DLC configuration. Do not enable the original ModBuddy version at the
same time.

See [the usage guide](docs/usage.md) for installer options, detailed macOS
paths, and known limitations. The [compatibility matrix](docs/compatibility.md)
records results from the development mod collection.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The packer also validates generated package references, XML, Lua compatibility
rewrites, and gameplay database actions during a build.

## Project relationship

This project was developed while investigating native macOS support for
[MPPatch](https://github.com/Lymia/MPPatch). MPPatch modifies the game runtime;
this project instead translates mods into DLC packages. They are separate
projects with different architectures.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
