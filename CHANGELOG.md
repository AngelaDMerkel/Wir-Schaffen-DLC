# Changelog

## 0.5.0 — 2026-08-31

- Adds **Restore stock Civilization V** as program 04 and `--restore-stock`.
  It transactionally removes only authenticated Wir Schaffen DLC folders,
  restores and hashes the original executable, invalidates rebuildable game
  database caches, and removes the now-unneeded verified engine backup.
- Stock restoration preserves Firaxis DLC, saves, user MODS and Maps,
  preferences, screenshots, and saved-game/history databases. Ambiguous,
  unauthenticated, symlinked, or unsupported paths stop the operation before
  the game is changed.
- Keeps the alternate-screen TUI active for the complete interactive run.
  Discovery, downloads, packaging progress, confirmations, cancellation,
  completion and errors now render inside the same themed terminal surface.
- Adds a dedicated four-phase progress panel for Download, Verify, Package and
  Install. Web transfers report the current source, byte percentage, received
  and total size, and transfer speed; later phases report item counts and
  transactional completion.
- Adds **Install AngelaDMerkel's map patch** as program 03 in the terminal UI
  and as the non-interactive `--excogitare-patch` command.
- Installs authenticated DLC metadata for Excogitare's exact Extreme
  (`180x94`, 20 major/20 minor) and Colossal (`170x110`, 22 major/22 minor)
  world sizes without requiring an individual map to be packaged first.
- Applies the signature-checked, reversible macOS engine guard as an explicit
  part of the Excogitare patch. Tall, Wide and Square retain ordinary Civ5Map
  dimensions; Needle, Ribbon, Pin and String use the guarded extreme-span path.
- Treats Excogitare's compatibility DLC as infrastructure, allowing it to
  coexist with the otherwise-exclusive Very Best Mods collection.
- Detects Excogitare custom-size identifiers and 8:1-or-greater aspect ratios
  when packaging standalone maps and automatically requires the same guard.
- Keeps the unsafe sizes and geometries explicitly experimental: the patch
  addresses known database, divisor and projection-loop failures but does not
  promise away Civ V's memory, minimap, pathfinding or late-game limits.

## 0.4.0 — 2026-08-31

- Reworks the terminal landing screen as the Bauhaus Launchpad, with a compact
  30-row layout, full ASCII wordmark, bright ANSI hierarchy, a solid yellow
  program-01 selection block, a no-color fallback, and the approved
  “Fine, I'll do it for you.” tagline.
- Makes **AngelaDMerkel's Very Best Mods** program 01 and the default action;
  local installed-mod/map packaging is program 02.
- Uses a real alternate-screen TUI for interactive mode selection, including
  raw ↑/↓ and J/K navigation, Enter, numeric shortcuts, Q/Escape cancellation,
  cursor restoration, and responsive redraw after terminal resizing.
- Paints every row and column of the alternate screen with the approved
  gentle-yellow mockup surface and its exact 24-bit Bauhaus foreground,
  selection, cyan, red, and muted colors, independent of the user's Terminal
  profile; the composition is centered as one responsive panel.
- Eliminates Terminal.app background tiling by painting literal cells instead
  of erase-to-end regions and temporarily matching the terminal's default
  background (including its inset margin) to the mockup surface.
- Removes the decorative SOURCE / VERIFY / PACKAGE / INSTALL legend from the
  launch screen so only actionable controls remain.
- Adds the mutually exclusive **AngelaDMerkel's Very Best Mods** installer
  mode, downloading and validating seven authoritative Civ V Workshop sources
  before packaging any of them.
- Verifies Valve Workshop identity, Civ V app ownership, ModBuddy GUID/version,
  advertised byte length, archive paths, and SHA-256 provenance for every
  collection member.
- Corrects the supplied Civilization VI Workable Mountains link to the
  original author's Civilization V item (`233614126`).
- Marks every generated report with the complete collection manifest and
  refuses both partial collections and coexistence with other Wir Schaffen DLC
  packages. Official Firaxis DLC is unaffected.
- Adds a Mass Effect Civilizations v7 compiler that replaces its late
  `Buildings.IsVisible`/`PediaVisible` columns with equivalent Lua filters,
  repairs its malformed Rachni XML, and translates its legacy trait-yield table.
- Translates legacy `PromotionPrereqOrN` fields to BNW's stock
  `UnitPromotions_PromotionPrereqOrs` relation, allowing Future Worlds to
  validate against a clean Brave New World database.

## 0.3.2 — 2026-08-15

- Generates the 16-byte authentication key required by the native macOS DLC
  loader from each package's GUID, Steam app IDs, and protected manifest tags.
- Validates the generated key before installation and includes a Firaxis
  package test vector to prevent regressions in the key algorithm.
- Uses ASCII-only installation folder names because Civ V's legacy macOS DLC
  scanner silently skips packages behind Unicode path components.
- Gives standalone-map database and localization files map-specific virtual
  names, preventing one selected custom world from shadowing another.
- Validates cross-package database filenames as part of the combined virtual
  namespace before changing the game installation.
- Applies a signature-checked, reversible x86-64 instruction-order patch for
  the native macOS engine's over-255-axis divide-by-zero, preserving the
  nonzero path and taking the routine's existing zero-divisor return.
- Replaces the adjacent projection-normalization add/subtract loops with an
  equivalent bounded calculation. Extreme-aspect maps can otherwise remain
  there forever at full CPU even when their geography is pre-rendered.
- Stores and verifies the exact original executable before ad-hoc signing the
  patched copy; `--restore-engine-patch` restores it byte-for-byte.
- Fixes the common cause of missing packed civilizations and custom world-size
  rows: unsigned packages were parsed but excluded from `DownloadableContent`.

## 0.3.1 — 2026-08-15

- Emits normal DLC `GameData` and `TextData` entries at the package root,
  matching Firaxis's civilization and map DLC manifests.
- Makes custom world-size, civilization, unit, and other gameplay rows
  available to the frontend database before setup and map selection.
- Keeps asset and map directories in the gameplay section while validating
  that database entries cannot regress to the inactive nested layout.

## 0.3.0 — 2026-08-15

- Coordinates all installed and newly selected `InGameUIAddin` scripts through
  one deterministic, priority shared UI bridge.
- Loads every add-in with `ContextPtr:LoadNewContext` so mods retain separate
  Lua globals and event state instead of competing through multiple
  `UI/InGame.lua` overrides.
- Defers per-mod hooks when using Wir Schaffen DLC and validates the effective
  cross-package `Files`, `UI`, and `Maps` namespaces before installation.
- Refuses combined packages containing different payloads with the same
  virtual filename instead of accepting load-order-dependent behavior.

## 0.2.2 — 2026-08-15

- Translates Really Advanced Setup v15's `GetActivatedModVersion` calls to its
  manifest version when packaging it as always-on DLC.
- Prevents its user-data initialization from receiving `nil` outside the Mods
  activation context, which previously broke the standard Advanced Setup UI.
- Validates that no activated-version dependency remains in generated Lua.

## 0.2.1 — 2026-08-15

- Repairs illegal bare ampersands in source database and imported UI XML while
  packaging, without modifying the installed ModBuddy source.
- Allows Really Advanced Setup v15 to pass structural packaging despite its
  malformed `Gods & Kings` localization and UI XML.
- Makes an aborted batch explicitly state that the Civ V DLC folder was not
  changed.

## 0.2.0 — 2026-08-15

- Adds standalone `.Civ5Map` packaging to the terminal installer and direct
  packer, including maps supplied with the repeatable `--map` option.
- Parses and validates v11/v12 map headers, world identifiers, plot counts,
  geography records, and the 32,768-plot file-format limit before packaging.
- Registers missing custom `Worlds` rows such as `WORLDSIZE_COLOSSAL` and
  `WORLDSIZE_EXTREME` through reversible DLC database XML.
- Preserves the source map byte-for-byte and records its dimensions, aspect
  ratio, geography offset, and SHA-256 in `pack-report.json`.
- Extends Corporations' world settings for Colossal and Extreme maps.
- Adds test-session map inventory to the macOS diagnostics kit so a runtime
  failure can be tied to the exact installed map and dimensions.

## 0.1.1 — 2026-08-15

Runtime compatibility maintenance release based on a two-hour instrumented
macOS game session.

- Adds missing `Help` keys to newly inserted unit rows, preventing Brave New
  World's production screens from passing `nil` to `Locale.ConvertTextKey`.
- Makes Future Worlds v6 resource/improvement lookups safe when another mod
  adds a resource without a corresponding improvement mapping.
- Embeds Corporations v1 configuration values into every affected Lua context;
  the compatibility layer no longer depends on dynamic `GameInfo` sidecars.
- Loads the Corporations compatibility helper independently in each Civ V UI
  context that uses its rewritten accessors.
- Recognizes and transactionally replaces packages generated under the legacy
  0.1.0 folder naming scheme.
- Warns clearly that enabling the original ModBuddy copy alongside its packed
  DLC copy loads the mod twice and is unsupported.

## 0.1.0 — 2026-07-17

First official release.

- Converts supported Civilization V ModBuddy mods into deterministic DLC
  packages for native macOS multiplayer.
- Adds the interactive **AngelaDMerkel's: Wir Schaffen DLC** terminal installer
  with automatic game and mod discovery.
- Packages all selected mods before installation and validates their generated
  database, virtual-file, map, and UI references.
- Refuses silent replacement and rolls back partial multi-package installs.
- Includes compatibility compilers for Future Worlds v6 and Corporations
  (Brave New World) v1.
- Verified dry-run packaging for Brotherhood of Steel v2, Corporations v1,
  Future Worlds v6, and Workable Mountains v2.

The native release artifact is architecture and macOS-version labeled. The
wheel and source distribution remain available for other systems with Python
3.9 or newer.
