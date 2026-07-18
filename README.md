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

## Usage

```sh
python3 civ5_dlc_packer.py \
  "/path/to/MODS/Mod Name (v 1)" \
  "build/packed-dlc/Mod Name (v 1)" \
  --base-db "$HOME/Library/Application Support/Sid Meier's Civilization 5/cache/Civ5CoreDatabase.db" \
  --game-assets "$HOME/Library/Application Support/Steam/steamapps/common/Sid Meier's Civilization V/Civilization V.app/Contents/Assets/Assets" \
  --ui-set Expansion2
```

With the package installed through `pip`, the equivalent command is
`civ5-dlc-packer`.

Install the complete generated folder beneath:

```text
Civilization V.app/Contents/Assets/Assets/DLC/
```

Every multiplayer participant needs a byte-identical package and the same
expansion/DLC configuration. Do not enable the original ModBuddy version at the
same time.

See [the usage guide](docs/usage.md) for detailed macOS paths and known
limitations. The [compatibility matrix](docs/compatibility.md) records results
from the development mod collection.

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
