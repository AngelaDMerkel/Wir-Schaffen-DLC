# macOS installer and DLC packer

## Interactive installer: Wir Schaffen DLC

The easiest workflow is the branded terminal interface. Quit Civilization V,
then run this from the repository directory:

```sh
./wir-schaffen-dlc.command
```

It automatically looks for the normal Steam installation at:

```text
~/Library/Application Support/Steam/steamapps/common/Sid Meier's Civilization V/Civilization V.app
```

It reads mods from:

```text
~/Library/Application Support/Sid Meier's Civilization 5/MODS/
```

Select mods with numbers, comma-separated numbers, ranges, or `all`. For
example, `1,3-5` selects entries 1, 3, 4, and 5. The installer packages and
validates every selection in a temporary directory before touching the game.
If any build fails, nothing is installed.

The generated folders are installed beneath the application bundle's
`Contents/Assets/Assets/DLC/` directory. Matching packages are not overwritten
without confirmation, and a failed multi-package copy is rolled back.

For an unusual Steam library or user-data location, pass the paths explicitly:

```sh
./wir-schaffen-dlc.command \
  --game-app "/Volumes/Games/steamapps/common/Sid Meier's Civilization V/Civilization V.app" \
  --user-data "$HOME/Library/Application Support/Sid Meier's Civilization 5"
```

Useful automation and diagnostic options are:

```text
--select 1,3-5   preselect mod numbers
--select all     select every discovered mod
--dry-run        package and validate without installing
--replace        permit replacement of matching tool-installed packages
--yes            skip the final install confirmation
```

`--yes` does not imply `--replace`; updating an existing package requires both
options. The script does not request administrator privileges.

## Direct packer

`civ5_dlc_packer.py` converts a ModBuddy mod directory into a
Civilization V DLC directory. This is useful on the native macOS build, where
the MPPatch native runtime hook is not available.

The result is deliberately a folder, not an installer. It is inspectable,
easy to remove, and can be copied unchanged to every multiplayer participant.

## Future Worlds v6 trial

The packer recognizes Future Worlds v6 and translates its six SQL actions into
DLC-compatible XML. It also:

- materializes the SQL-generated mutant art and fungal-growth resource rows;
- moves the Future Tech node and inserts the Future Worlds era in the intended
  order;
- removes the custom `Eras.SplashScreen` database column and rewrites the era
  popup to use a static mapping;
- injects `FutureLua.lua` through the active Brave New World `InGame.lua`;
- detects duplicate virtual filenames, deduplicates identical files, and
  renames/relinks different files safely.

Run from the repository root:

```sh
python3 civ5_dlc_packer.py \
  "$HOME/Library/Application Support/Sid Meier's Civilization 5/MODS/Future Worlds (v 6)" \
  "build/packed-dlc/Future Worlds (v 6)" \
  --base-db "$HOME/Library/Application Support/Sid Meier's Civilization 5/cache/Civ5CoreDatabase.db" \
  --game-assets "$HOME/Library/Application Support/Steam/steamapps/common/Sid Meier's Civilization V/Civilization V.app/Contents/Assets/Assets" \
  --ui-set Expansion2
```

The output includes a `.Civ5Pkg`, `Files` and `UI` directories, translated
database XML, and `pack-report.json`. The report records the source identity,
the deterministic DLC UUID, all translations, and known omissions.

## Corporations v1 trial

Corporations adds 16 columns to eight core game tables through SQL. A DLC
cannot safely perform those late schema changes, so its compatibility compiler:

- creates eight `Corporation*Settings` sidecar tables;
- moves the SQL values and custom XML fields into keyed sidecar rows;
- retains the mod's native `Corporations` and `CorporationSettings` tables;
- rewrites the affected Lua configuration lookups;
- preserves the original art, Lua entry point, and Brave New World UI
  overrides.

Build it with:

```sh
python3 civ5_dlc_packer.py \
  "$HOME/Library/Application Support/Sid Meier's Civilization 5/MODS/Corporations (Brave New World) (v 1)" \
  "build/packed-dlc/Corporations (Brave New World) (v 1)" \
  --base-db "$HOME/Library/Application Support/Sid Meier's Civilization 5/cache/Civ5CoreDatabase.db" \
  --game-assets "$HOME/Library/Application Support/Steam/steamapps/common/Sid Meier's Civilization V/Civilization V.app/Contents/Assets/Assets" \
  --ui-set Expansion2
```

The generated package is structurally validated, but Corporations persists
state through `Modding.OpenSaveData()`. A multiplayer host/client session and a
save/reload cycle are required before calling its runtime behavior verified.

To install the trial package, quit Civilization V and copy the complete output
directory into the app's DLC directory:

```sh
ditto \
  "build/packed-dlc/Future Worlds (v 6)" \
  "$HOME/Library/Application Support/Steam/steamapps/common/Sid Meier's Civilization V/Civilization V.app/Contents/Assets/Assets/DLC/Future Worlds (v 6)"
```

Remove that exact `DLC/Future Worlds (v 6)` directory to uninstall it. Do not
enable the original Future Worlds copy in the Mods menu while testing the DLC.

## Multiplayer requirements

Every player needs a byte-identical packed DLC folder and the same Civ V DLC/
expansion set. The Future Worlds compiler uses the supplied core database to
materialize SQL `SELECT` results, so differing game data can cause database
checksums or gameplay to diverge.

Start with a new multiplayer save. A DLC changes the game database at startup,
so adding or removing it from an existing save is unsafe.

## Current boundaries

The generic path supports database XML, imported assets, localization XML,
imported leader scenes, `.Civ5Map` files, Lua map scripts, and
`InGameUIAddin` Lua entry points. A GameData XML entry point with a same-name
Lua companion is also recognized. Arbitrary SQL is rejected unless an explicit
compatibility compiler exists; silently dropping SQL would produce a corrupt
or incomplete mod. Other ModBuddy action and entry-point types are rejected
with a clear error.

Future Worlds' runtime SQL triggers are not retained. Their effects for the
current Brave New World database are materialized in XML instead. The
Enlightenment Era integration is included only when those records are present
in the database passed with `--base-db`.

Run the packer tests with:

```sh
python3 -m unittest discover -s tests -v
```

The [local compatibility matrix](compatibility.md) records the
results from packing all 15 mods installed on the development Mac.
