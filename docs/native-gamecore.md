# Native GameCore products

Wir Schaffen DLC is the sole public installer for native Lekmod and Vox Populi.
The products have separate binaries and content. Exactly one product is active.

```sh
wir-schaffen-dlc --gamecore status
wir-schaffen-dlc --gamecore lekmod --gamecore-package /path/to/release.zip --gamecore-sha256 SHA256 --dry-run
wir-schaffen-dlc --gamecore lekmod --gamecore-package /path/to/release.zip --gamecore-sha256 SHA256
wir-schaffen-dlc --gamecore vox-populi --gamecore-package /path/to/vp-release.zip --gamecore-sha256 SHA256
wir-schaffen-dlc --gamecore stock
wir-schaffen-dlc --gamecore recover
```

Replace SHA256 with the independently recorded digest from artifact production
or the trusted release catalog. A checksum inside the downloaded archive is
not an independent trust anchor. `--game-app` and `--user-data` select explicit
installation paths. `--yes` skips the tool's final interactive confirmation.
Quit Civ V before changing products.

Installing the same product with a newer verified artifact updates it.
Installing the other product switches both GameCore and DLC. Restoring stock
removes managed native payloads and restores the verified original library.
Generated database caches are invalidated during switching and restored on
rollback. Other content/engine installation modes require stock GameCore first.

## State and recovery

Canonical backup and state live under the selected user-data directory:
`WirSchaffenDLC/GameCore/<application-identity>/`. There is one `stock.dylib`,
verified against the catalog's stock hash. A modded binary is never accepted
as a stock source. No new `.lekmod-original` or `.vp-original` backups are made.

The recorded legacy Lekmod installation may be migrated only when both its
binary and full DLC tree match the observed catalog hashes and its original
backup has the known stock hash. The historical backup is retained untouched.
Modified or unknown legacy installations require inspection, not blind adoption.

Preflight rejects unknown host/binary hashes, corrupt backups, modified managed
content, unowned conflicting payloads, and symlinked managed paths. Status
distinguishes Steam restoring the stock binary from an unknown binary update.
Read-only status and dry-run create no application or state files.

Before changing any managed path, a transaction records its previous contents.
Ordinary failures roll back. Process interruption leaves a journal; `status`
reports recovery required and `recover` verifies every saved file before
restoring anything. An advisory lock prevents concurrent transactions using
the same application identity and user-data location. This is process-crash
recovery, not a guarantee against disk failure or a machine losing power.

## Artifact trust and publication

Only AngelaDMerkel/Lekmod and AngelaDMerkel/Community-Patch-DLL-macOS are
recognized products. The compatibility source must be
AngelaDMerkel/civ5-macos-gamecore-compat. Full commit identifiers are required.
Each archive's external SHA-256, complete file inventory, SHA256SUMS,
GameCore/payload hashes, ABI, game hashes, conflicts, and fixed destinations
are checked. Traversal, duplicate/case-colliding paths, links, and special
files are rejected before extraction.

The public release catalog is intentionally empty until local build, content,
runtime validation, and publication authorization succeed. A product request
without a local package fails clearly while no public release exists. Future
catalog entries must contain a GitHub release URL and independently pinned
archive digest. No repositories, releases, or branches were published by this
local reorganization.

Release producers ad-hoc sign the GameCore before hashing it. The installer
verifies that signature and copies the signed bytes without re-signing, so
the installed binary continues to match the manifest and state hash. The
independently pinned archive digest remains the product trust anchor.

## Normal Steam launch with the startup correction

Artifacts may request the fixed `sqlite-stat-inode64-v1` startup correction.
WSDLC verifies the signed `libWirCiv5HostStat.dylib` against the archive inventory,
adds its fixed `@executable_path` dependency in unused Mach-O header space and
ad-hoc signs a staged executable. The supported original executable must match
the pinned full SHA-256. No instruction or section is relocated. Steam's Play
button then uses the installed correction without launch options or a separate
launcher. On the current M2 Max/macOS26.5.2 test machine, actual Steam Play and
Aspyr Play reached the menu, loaded an existing Lekmod single-player save and
exited normally with code0, without test UI hooks or injected process libraries.
Evidence is recorded in Lekmod's `docs/macos-host-stat-abi.md` and ignored
`build/macos/steam-play-20260926/`. This is one pinned host/package validation.

The original executable is preserved as `stock-executable` alongside the existing
`stock.dylib`. State records original/installed executable and correction-library
hashes. Both new application files participate in the same recovery transaction
as GameCore and DLC. Switching to stock, or to an artifact without a startup
correction, restores the original executable byte for byte and removes the owned
correction library. Unowned files, changed libraries, corrupt backups and unknown
Steam executable updates are rejected. A Steam integrity check that restores the
known original is reported as `steam-restored`; reinstall the verified artifact
or select stock. Existing unrelated engine patches must be restored first.

The correction itself is supplied and validated by the native product. This
installer feature does not establish complete gameplay or platform support.
