# Publishing WSDLC releases

Once `.github/workflows/release.yml` is committed and pushed, publishing a
GitHub release automatically builds the arm64 and amd64 macOS downloads.
The workflow uses GitHub's built-in `GITHUB_TOKEN`; no personal token is needed.

## Normal publication

1. Set the same version in `pyproject.toml` and `civ5_dlc_packer.py` (`VERSION`).
2. Commit and push the source, workflow, and release-tool pins.
3. Create a tag named `v` plus that version, such as `v0.6.0` for version `0.6.0`,
   pointing to the intended source commit.
4. Publish a GitHub release for that tag. Published prereleases also trigger it.

The release appears in the repository's **Releases** section. Once both native
jobs succeed, its Assets list receives:

- `Wir-Schaffen-DLC-VERSION-macos-15-arm64.zip` for Apple Silicon;
- `Wir-Schaffen-DLC-VERSION-macos-15-amd64.zip` for Intel (`x86_64`);
- a Python wheel and source distribution;
- one build-information JSON file per architecture;
- `release-manifest.json` with the source commit, toolchains, hashes, and
  download URLs containing the exact version tag;
- one `SHA256SUMS.txt` covering all payload files and release metadata.

The macOS number in the ZIP name identifies the build OS, not the oldest
supported OS. Hosted jobs use `macos-15` (arm64) and `macos-15-intel` (x86_64),
with native Python on each. GitHub controls the sidebar's Latest selection;
the workflow attaches assets to your exact release and leaves its title,
notes, tag, and Latest/prerelease setting as you published them.

## Version and integrity checks

The workflow resolves the release tag, verifies its version, and passes the
exact commit to both architecture jobs. A moved tag, dirty checkout, or
version mismatch fails before building. Each job runs the test suite, checks
the frozen binary's architecture, runs its `--version` and `--help`, and
verifies its ad-hoc signature.

Python is pinned in `.github/release-python-version`. All release dependencies
and their wheel hashes are pinned in `requirements-release.txt`. Official
builds verify the actual Python and PyInstaller versions. Actions use full
verified commit SHAs, with release versions in comments.

The upload job runs only after both builds succeed. It checks artifact hashes,
versions, architectures, source commits, and the dependency lock from the tag,
then creates one combined manifest and checksum inventory. Only this job has
repository write permission.

Existing downloads are never overwritten automatically. A rerun with conflicting
asset names fails; inspect existing assets before deciding whether to remove a
partial upload or publish a new version. Manual workflow dispatch accepts an
existing published tag for recovery.

This on-publication flow requires releases that allow assets to be attached
following publication. If immutable releases are enabled, assets are sealed
at publication and a draft-build-upload-publish flow is required instead.
The workflow detects immutable releases and gives an explicit error.

The tag must include the workflow and helper files. Publishing an older tag
does not pick up files committed after it. GitHub suppresses most workflow
triggers for events created using another workflow's GITHUB_TOKEN; publish
through GitHub's release UI or as an authenticated user.

## Local builds

Use macOS and a Python interpreter for the target architecture. The exact
release interpreter version is recorded in `.github/release-python-version`.

```sh
python3 -m venv /private/tmp/wsdlc-build-env
/private/tmp/wsdlc-build-env/bin/python -m pip install --require-hashes -r requirements-release.txt
/private/tmp/wsdlc-build-env/bin/python scripts/build_release.py --architectures arm64 --output-dir /private/tmp/wsdlc-artifacts --work-dir /private/tmp/wsdlc-build-work
```

For Intel use `--architectures amd64 --x86-python /path/to/intel/python`.
`x86_64` remains accepted as an alias. Both architectures may be requested with
their respective `--arm-python` and `--x86-python` interpreters. Official local
builds also use `--release-tag vVERSION` from a clean tagged checkout with the
pinned Python/PyInstaller versions.

Output and work directories must be new or empty. Existing releases and build
outputs are preserved. Builds without `--release-tag` have no official release
identity and cannot pass the combined publication checks.

The executable is ad-hoc signed. Developer ID signing and Apple notarization
remain outside this workflow.

References: [GitHub release events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#release),
[hosted runner architectures](https://docs.github.com/en/actions/reference/runners/github-hosted-runners),
and [release immutability](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases).
