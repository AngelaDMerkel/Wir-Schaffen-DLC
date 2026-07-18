# Changelog

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
