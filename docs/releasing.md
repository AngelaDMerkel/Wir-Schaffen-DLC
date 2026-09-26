# Automatic WSDLC releases

**Push your changes to `main` or `codex/shared-macos-gamecore`.** GitHub Actions
then assigns a version, builds arm64 and amd64, and publishes the complete
release automatically. There is no separate tag, version-edit, or Publish
Release step. Commit and push this workflow update once to enable that behavior.

The built-in `GITHUB_TOKEN` handles publication and the automatic source-version
update afterward. No personal token is required. Pull requests, other
branches, tag pushes, and branch deletions do not publish releases.

## Automatic versioning

The workflow selects the next patch version above the highest existing
`vMAJOR.MINOR.PATCH` tag. For example, if `v1.0.0` exists, the next successful
release is `v1.0.1`, followed by `v1.0.2`. A first release uses the source's
base version when no equal or higher version has been tagged. You may optionally
set both source version declarations to a higher major/minor version to start
a new series; ordinary pushes require no version edits.

Planning occurs in the disposable CI checkout. It stamps the chosen version
into `pyproject.toml` and `civ5_dlc_packer.py`, then creates a release commit
whose sole parent is the exact commit you pushed. It also regenerates
`assets/wir-schaffen-dlc-main.svg` from that version of the installer. Only the
two version declarations and the generated README image may differ from the
pushed source. An annotated tag records the original source commit, release
commit, and workflow run identity.

Both architecture jobs restore that same versioned commit from a checksummed
Git bundle. The native CLI version, installer screens, Python package metadata,
README image, filenames, tag, and release manifest must agree. A stale README
image fails validation before publication.

After successful publication, the workflow synchronizes both publishing branches
with that release version and regenerates each branch's README image from its
own installer code. It commits only the two version declarations and the image;
other source content stays on its existing branch. The source version is the
latest published baseline, while the release tag identifies the exact shipped
code. Pull the branch updates to receive the new version in a local checkout.
An older release retry never lowers a newer source version.

## Build, upload, and publication

1. Queue the source push and allocate its version.
2. Build and test on native `macos-15` arm64 and `macos-15-intel` x86_64 runners.
3. Verify both binaries, versions, signatures, manifests, and SHA-256 hashes.
4. Push the exact release tag and create a draft release.
5. Upload the two executable bundles and verify GitHub's reported sizes and
   SHA-256 digests.
6. Publish the completed draft and show the new release in GitHub's Releases
   section. It becomes Latest unless a higher stable version has already been
   published. Workflow run numbers and queue order do not determine Latest;
   unpublished tags, drafts, and prereleases do not block it.
7. Synchronize the source versions and README images on `main` and
   `codex/shared-macos-gamecore` with the published version.

This draft-first flow also supports GitHub's immutable releases: assets are
complete before publication seals the release. Failed builds create no public
release. Upload failures leave a recoverable workflow-owned draft.

Each release uploads only:

- `Wir-Schaffen-DLC-VERSION-macos-15-arm64.zip` for Apple Silicon;
- `Wir-Schaffen-DLC-VERSION-macos-15-amd64.zip` for Intel (`x86_64`).

Each ZIP includes the standalone installer and its double-clickable launcher.
The automatic workflow does not build or upload Python wheels or source
distributions. GitHub also displays its own
[source-code archive links](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
for the release tag.

Per-architecture build-information JSON, `release-manifest.json` (including
the exact release commit and version-pinned download URLs), and `SHA256SUMS.txt`
are retained for 30 days in the workflow's `release-verification` artifact.
All build records and checksums are verified before publication; they are not
uploaded to the public release.

The macOS number identifies the build OS, not the oldest supported OS. Each
native build runs the test suite, checks Mach-O architecture, runs `--version`
and `--help`, and verifies the ad-hoc signature. Python is pinned in
`.github/release-python-version`; dependencies and wheel hashes are pinned in
`requirements-release.txt`. Actions use full commit pins.

## Queuing and retry behavior

The two publishing branches share a serial workflow queue, so simultaneous
pushes cannot allocate the same version. `queue: max` retains up to GitHub's
100 pending-run limit instead of replacing older pending pushes. A rapid burst
of more than that limit is subject to GitHub's queue capacity.

Re-running a workflow whose tag already exists reuses its recorded version.
An incomplete draft owned by that same run can have its partial assets
replaced automatically. A complete published release is left untouched;
published downloads are never clobbered. Unrelated user-created releases,
conflicting tags, changed source, and checksum failures are rejected.

Only the final publishing job has repository write permission. It never
force-pushes tags or branches. If a user pushes while version synchronization
is running, the workflow fetches the newer source, regenerates its image, and
retries up to three times. Permission failures or repeated races leave a clear
failed synchronization step; rerunning the workflow reuses the existing release.
Published binaries are not overwritten.

The synchronization commit uses `GITHUB_TOKEN` and includes `[skip ci]`, so it
does not start another release. Manual workflow dispatch remains available for
maintenance, but is not part of normal publication. See GitHub's
[workflow-trigger rules](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

`actionlint` 1.7.12 does not yet recognize GitHub's documented `queue` property.
For that version, lint with this one narrow schema exception:

```sh
actionlint -ignore 'unexpected key "queue" for "concurrency" section' .github/workflows/release.yml
```

## Local builds

Local development builds still work without creating a release:

```sh
python3 -m venv /private/tmp/wsdlc-build-env
/private/tmp/wsdlc-build-env/bin/python -m pip install --require-hashes -r requirements-release.txt
/private/tmp/wsdlc-build-env/bin/python scripts/build_release.py --architectures arm64 --skip-python-distributions --output-dir /private/tmp/wsdlc-artifacts --work-dir /private/tmp/wsdlc-build-work
```

For Intel use `--architectures amd64 --x86-python /path/to/intel/python`.
Omit `--skip-python-distributions` when you also need developer Python packages.
`x86_64` remains accepted as an alias. Output and work directories must be new
or empty; existing artifacts are preserved. Official builds use the generated
tagged checkout and the pinned Python/PyInstaller versions. Local untagged
builds do not carry official release identity.

Developer ID signing and Apple notarization remain outside this workflow.

When changing the installer layout or branding, regenerate the README image
with `python3 scripts/render_main_menu.py`. Verify it without changing files
with `python3 scripts/render_main_menu.py --check`. CI and the release planner
also check the image against the installer that produced it.

References: [GitHub workflow queues](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency),
[immutable release publication](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases),
and [hosted runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
