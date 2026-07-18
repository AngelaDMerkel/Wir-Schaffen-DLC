# Local mod compatibility matrix

Tested against the 15 ModBuddy mods installed on the development Mac. Each
successful database package was parsed and replayed against the local Brave New
World `Civ5CoreDatabase.db`; all generated package references and XML files were
also validated.

These are structural packer tests, not claims that every mod has completed an
in-game multiplayer session yet.

| Mod | Result | Coverage or blocker |
| --- | --- | --- |
| Barbarians - Unlimited Exp v2 | Pass | XML database update |
| Brotherhood of Steel v2 | Pass | 12 database files, 33 art/audio assets, imported leader scene |
| Corporations (Brave New World) v1 | Pass | Dedicated compiler moves 16 core-table columns into eight sidecar tables and rewrites affected Lua lookups |
| Fantastical Map Script v31 | Blocked | Map script is recognized, but its SQL icon-atlas inserts need translation |
| Faster Aircraft Animations v3 | Blocked | SQL expression updates need materialization as XML |
| Future Worlds v6 | Pass | Dedicated SQL and Lua compatibility compiler; 541 imported virtual files |
| No Unit Limit v1 | Pass | XML database update |
| One City Civs v5 | Pass | Gameplay and localization XML |
| PerfectWorld3 - Updated v5 | Pass | Lua map script through DLC `MapDirectory` |
| Really Advanced Setup v15 | Blocked | Source `GTAS_HelpText.xml` is malformed due to unescaped ampersands; source also declares multiplayer unsupported |
| Really Raging Barbarians v1 | Pass with warning | XML is structurally valid, but its source manifest declares multiplayer unsupported |
| Tatooine v17 | Pass | `.Civ5Map` through DLC `MapDirectory` |
| Tatooine - Star Wars v1 | Pass | `.Civ5Map` through DLC `MapDirectory` |
| Units - Airship and Land Ironclad v3 | Pass | Gameplay and localization XML |
| Workable Mountains v2 | Pass | Gameplay/localization XML plus companion Lua entry-point translation |

Summary: 12 of 15 installed mods produced structurally valid DLC packages. The
three rejected mods failed explicitly rather than producing incomplete DLCs.

Generated packages are placed under `build/packer-matrix/` during local matrix
testing. That directory is ignored by Git because it contains local build
artifacts and copied mod content.
