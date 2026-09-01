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
- Standalone v11/v12 `.Civ5Map` files with custom world-size registration
- Imported leader scenes
- `InGameUIAddin` Lua entry points
- Future Worlds v6, including its SQL and era-popup compatibility layer
- Corporations (Brave New World) v1, using per-context embedded configuration
  in place of late core-schema changes
- Mass Effect Civilizations v7, with DLC-safe dummy-building visibility

Arbitrary SQL is rejected unless the mod has an explicit compatibility
compiler. Producing no package is safer than silently dropping gameplay data.

## Requirements

- Python 3.9 or newer
- Civilization V with Brave New World for the included compatibility adapters
- The unpacked ModBuddy mod directory

No third-party Python packages are required.

## AngelaDMerkel's: Wir Schaffen DLC

The macOS terminal installer discovers Civilization V, reads the mods and maps
already installed in its `MODS` and `Maps` directories, and presents a numbered selection screen.
It packages every selected mod first and only then asks permission to copy the
validated packages into the game's DLC directory.

In an interactive terminal, the full-screen interface remains active from
program selection through discovery, downloads, packaging, confirmation and
the final success or error screen. Ordinary scrolling terminal output is used
only for redirected/non-interactive execution or an incompatible terminal.
Its progress panel follows Download, Verify, Package and Install as distinct
phases. Workshop transfers display the current mod, percentage, byte totals and
speed; packaging and installation display completed and remaining items.

Quit Civilization V, then double-click `wir-schaffen-dlc.command` in Finder or
run it from Terminal:

```sh
./wir-schaffen-dlc.command
```

The interface is intentionally small: select entries with input such as
`1,3-5` or `all`, review any compatibility warnings, and confirm installation.
Existing packages created by the installer are never replaced without an
additional confirmation.

The first and default top-level option is **AngelaDMerkel's Very Best Mods**. It downloads
seven fixed Civ V sources from Valve's Workshop CDN, verifies their identities
and archives, packages the complete set, and installs it only as an exclusive
all-or-nothing collection. It cannot be mixed with local mod selection or
other Wir Schaffen DLC packages; Firaxis expansion and civilization DLC remain
untouched.

The third program, **Install AngelaDMerkel's map patch**, installs Excogitare
compatibility independently of a particular map. It registers Extreme
`180x94` and Colossal `170x110`, then applies the reversible macOS engine guard
used by Excogitare's extreme rectangular geometries. Run it non-interactively
with `--excogitare-patch`. The compatibility package is infrastructure and may
coexist with the exclusive Very Best Mods collection.

Program 04, **Restore stock Civilization V**, reverses every persistent change
owned by this tool. It removes authenticated `Civ5MP - …` DLC packages,
restores and verifies the original executable, removes the tool's engine
backup, and invalidates only the database caches Civ V can rebuild. Firaxis
DLC, saves, user MODS and Maps, preferences, screenshots, and saved-game
databases are preserved. The equivalent non-interactive option is
`--restore-stock`.

After a `pip` installation, the same interface is available as
`wir-schaffen-dlc`.

## Release 0.5.0

Release artifacts include a standalone, double-clickable macOS terminal bundle
and portable Python wheel/source packages. Native bundles are labeled with the
architecture and macOS generation they were built for; do not use an
incompatible native archive on a different Mac.

Version 0.2.0 added validated standalone-map packaging and reversible database
registration for non-stock identifiers such as `WORLDSIZE_COLOSSAL` and
`WORLDSIZE_EXTREME`. Map dimensions and bytes are not changed. The generated
report makes the remaining native-engine runtime test explicit for axes over
255 tiles and unusually wide or tall aspect ratios. Version 0.2.1 additionally
repairs illegal bare ampersands in source XML during packaging, allowing Really
Advanced Setup v15 to package without altering its installed source files.
Version 0.2.2 also replaces that mod's unavailable ModBuddy activation-version
lookup with its manifest version so its setup data initializes when loaded as DLC.

Version 0.3.0 makes multi-mod UI loading deterministic. The installer creates
one shared bridge from the complete effective packed-mod set, loads each
`InGameUIAddin` in an isolated Lua context, and rejects cross-package virtual
filename collisions before changing the game installation.

Version 0.3.1 corrects the generated DLC manifest layout so database and
localization XML load before the setup screens, as they do in Firaxis's normal
civilization and map DLC. Version 0.3.2 also signs every generated package with
the authentication key required by the native macOS DLC loader. Without that
key, macOS parses the manifest but silently excludes its civilizations, custom
world sizes, and other data from the runtime database. Installation folder
names are also normalized to ASCII for the game's legacy directory scanner.
For maps with an axis over 255 tiles, the installer additionally applies a
signature-checked and reversible pair of native guards to the supported macOS
executable: one for the over-255-axis divide-by-zero and one for the zero-span
or non-converging projection normalization exposed immediately afterward. Run with
`--restore-engine-patch` restores only its verified executable backup. Use
program 04 or `--restore-stock` for the complete stock rollback.

Version 0.4.0 adds the downloadable Very Best Mods collection, source hashes
and exclusive collection enforcement. It also adds Mass Effect Civilizations
v7 compatibility and translates Future Worlds' legacy promotion prerequisite
fields for clean Brave New World databases.

Version 0.5.0 adds the dedicated Excogitare compatibility program. It installs
authenticated world metadata for Excogitare's Extreme and Colossal budgets and
applies the reversible native geometry guard without requiring a map to be
packaged first. Tall, Wide and Square use ordinary Civ5Map dimensions; the
guard covers the known divisor and projection-loop failures reached by Needle,
Ribbon, Pin and String. These game-breaking choices can still exceed Civ V's
practical memory, minimap, pathfinding and late-game limits.

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

Package a standalone map with custom world metadata:

```sh
python3 civ5_dlc_packer.py --map-file \
  "/path/to/extreme-map.Civ5Map" \
  "build/packed-dlc/Extreme Map" \
  --base-db "$HOME/Library/Application Support/Sid Meier's Civilization 5/cache/Civ5CoreDatabase.db"
```

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
