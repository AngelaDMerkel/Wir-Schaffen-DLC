# Local mod compatibility matrix

Tested against the 15 ModBuddy mods installed on the development Mac. Each
successful database package was parsed and replayed against the local Brave New
World `Civ5CoreDatabase.db`; all generated package references and XML files were
also validated.

These are structural packer tests, not claims that every mod has completed an
in-game multiplayer session yet.

Standalone map validation also passes for the supplied 474x39
`WORLDSIZE_COLOSSAL` map (18,486 plots) and 38x445 `WORLDSIZE_EXTREME` map
(16,910 plots). Both payloads are preserved byte-for-byte and their generated
world rows replay successfully against the local Brave New World database.
Native testing identified and guarded the initial zero-divisor crash and the
following non-converging projection normalization; gameplay beyond initial
loading remains experimental.

The 0.5.0 Excogitare program also installs those two world-size definitions as
a standalone authenticated compatibility DLC, using Excogitare's current
`180x94` Extreme and `170x110` Colossal contract. Tall, Wide and Square alter
only the rectangular file dimensions. Needle, Ribbon, Pin and String enter the
guarded extreme-geometry path. This fixes known registration and native loader
failures, not every resource limit of the game.

Program 04 provides the corresponding stock boundary. Only authenticated
folders created with the `Civ5MP - ` installation prefix are removed. The
supported original executable is restored byte-for-byte and rebuildable core,
debug, mod and merged-localization caches are invalidated. Official DLC and
user-authored content are deliberately outside that operation.

The downloadable seven-mod preset validates as one collection with 10 isolated
`InGameUIAddin` contexts and no differing cross-package virtual-file
collisions. Every gameplay XML file replays against a clean Brave New World
database before installation.

| Mod | Result | Coverage or blocker |
| --- | --- | --- |
| Barbarians - Unlimited Exp v2 | Pass | XML database update |
| Brotherhood of Steel v2 | Pass | 12 database files, 33 art/audio assets, imported leader scene; missing unit Help keys repaired |
| Corporations (Brave New World) v1 | Pass | Dedicated compiler moves 16 core-table columns into eight sidecar tables and embeds settings in each affected Lua context |
| Fantastical Map Script v31 | Blocked | Map script is recognized, but its SQL icon-atlas inserts need translation |
| Faster Aircraft Animations v3 | Blocked | SQL expression updates need materialization as XML |
| Future Worlds v6 | Pass | Dedicated SQL and Lua compatibility compiler; 541 imported virtual files; combined-mod resource lookups are nil-safe |
| Mass Effect Civilizations v7 | Pass | 175 gameplay files and 375 imports; late visibility columns replaced with Lua filters; malformed Rachni XML and legacy trait-yield table translated |
| No Unit Limit v1 | Pass | XML database update |
| One City Civs v5 | Pass | Gameplay and localization XML |
| PerfectWorld3 - Updated v5 | Pass | Lua map script through DLC `MapDirectory` |
| Really Advanced Setup v15 | Pass with warning | Malformed source XML is repaired and activation-version lookup translated; source declares multiplayer unsupported |
| Really Raging Barbarians v1 | Pass with warning | XML is structurally valid, but its source manifest declares multiplayer unsupported |
| Tatooine v17 | Pass | `.Civ5Map` through DLC `MapDirectory` |
| Tatooine - Star Wars v1 | Pass | `.Civ5Map` through DLC `MapDirectory` |
| Units - Airship and Land Ironclad v3 | Pass | Gameplay and localization XML |
| Workable Mountains v2 | Pass | Gameplay/localization XML plus companion Lua entry-point translation |

Summary: 13 of 15 installed mods produced structurally valid DLC packages. The
two rejected mods failed explicitly rather than producing incomplete DLCs. The
separately downloaded Mass Effect package and complete Very Best Mods preset
also pass structural, namespace, and clean-database validation.

Generated packages are placed under `build/packer-matrix/` during local matrix
testing. That directory is ignored by Git because it contains local build
artifacts and copied mod content.
