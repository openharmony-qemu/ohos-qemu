# QEMU source repository migration

Source and packaging migration to [openharmony-qemu](https://github.com/openharmony-qemu)
uses a minimal patched-only footprint: only the `108` repositories carrying QEMU
source patches or component-owned recipes remain hosted under `openharmony-qemu`
(`100` OpenHarmony OS, `7` ArkWeb Chromium M144, `1` external `third_party_openvpn`),
alongside [`manifest`](https://github.com/openharmony-qemu/manifest),
[`mirror-sync`](https://github.com/openharmony-qemu/mirror-sync), and
[`ohos-qemu`](https://github.com/openharmony-qemu/ohos-qemu). Unpatched
components (including unpatched Git LFS repositories) are referenced directly from
their official GitHub, GitCode, or upstream remotes with pinned commits.
Package builds and runtime validation remain deferred.

## Ownership and branches

- Organization: <https://github.com/openharmony-qemu>
- Packaging & workflow repository: <https://github.com/openharmony-qemu/ohos-qemu> (`main`)
- Manifest: <https://github.com/openharmony-qemu/manifest/tree/OpenHarmony-7.0-Release-qemu> (`48ac299eb434f436f12e921585a397b02688704e`)
- Central synchronization: <https://github.com/openharmony-qemu/mirror-sync>
- Patched component repositories keep their upstream branches plus `OpenHarmony-7.0-Release-qemu`. Future major baselines use the corresponding release name.
- Component repositories own their source changes; `./patches/` is gitignored in `ohos-qemu` and materialized on demand by `scripts/ensure_component_inputs.sh` (`103` owners, `2,184` files).

In [`arkweb.xml`](https://github.com/openharmony-qemu/manifest/blob/OpenHarmony-7.0-Release-qemu/arkweb.xml),
only the `7` patched ArkWeb repositories stay on `openharmony-qemu`, `third_party_bounds_checking_function`
points directly to `https://github.com/openharmony/third_party_bounds_checking_function.git`,
and the remaining `147` unique unpatched repositories point directly to `https://gitcode.com/openharmony-tpc/`.
Similarly, in [`ohos.xml`](https://github.com/openharmony-qemu/manifest/blob/OpenHarmony-7.0-Release-qemu/ohos.xml)
(`100` `qemu`, `414` `ohos-github`, `6` `ohos-gitcode`) and [`external.xml`](https://github.com/openharmony-qemu/manifest/blob/OpenHarmony-7.0-Release-qemu/external.xml)
(`1` `qemu`, `2` `ohos-github`, `16` `github`, `1` `gitlab`), unpatched dependencies
(such as `third_party_icu`, `third_party_libphonenumber`, `third_party_llvm-project`,
`third_party_mesa3d` 21.3.3, and audio/caption upstreams) fetch directly from upstream without organization forks or mirrors.

## Central synchronization

Synchronization scripts, upstream configuration and conflict issues belong to
`openharmony-qemu/mirror-sync`. Its `repositories.json` tracks only the `15`
patched component repositories whose upstream is not already mirrored under
`https://github.com/openharmony/` (daily scheduled sync at 19:23 UTC / 03:23 Asia/Shanghai;
fast-forward only, never modifying `OpenHarmony-*-Release-qemu`).

Component repositories have GitHub Actions disabled so upstream branch sync pushes
do not launch upstream CI matrices. Synchronization remains enabled in `mirror-sync`,
and packaging verification/CI runs in `openharmony-qemu/ohos-qemu`.

The scheduled job runs daily at 19:23 UTC / 03:23 Asia/Shanghai. It fetches every
upstream branch, preserves full ancestry, updates the same branch names with
normal fast-forward pushes, and never deletes destination refs. Non-fast-forward
updates or concurrent changes create a deduplicated issue in `mirror-sync`, while
preserving the conflicting destination ref. QEMU development branches are excluded.

An upstream branch rewrite is recorded separately from the fixed QEMU baseline.
Migration evidence verifies the preserved destination SHA and the matching open
issue in `mirror-sync`. Such a reported upstream conflict can coexist with a
complete QEMU source migration; conflicts on the fixed baseline or QEMU branch
still prevent completion. Outstanding upstream conflicts remain explicit in
`migration-status.json`.

The former sync installations in `global_i18n`, `developtools_smartperf_host` and
`device_soc_rockchip` are retired through signed deletion commits affecting only
the four migration-owned sync/configuration files. Source tree contents are
preserved. The removal commits and original-to-hosted baseline mappings are
recorded in the manifest repository's `migration-evidence/mirror-sync/` directory.

GitHub rejected deploy-key creation because deploy keys are disabled for these
repositories. Central CI therefore supports a GitHub App (recommended) or a
fine-grained PAT restricted to target repositories. Configure `MIRROR_APP_ID`
and `MIRROR_APP_PRIVATE_KEY`, or `MIRROR_TOKEN`, in the central repository as
documented there. App authorization is now configured, and the first production
sync of all 326 `global_i18n` upstream branches was verified against remote SHAs.
Production verification records are archived with the manifest. A `prepare_only`
diagnostic run never publishes branches.

An actual scheduled run has also been observed and its completed results archived.
The latest verified OpenVPN `renovate/vcpkg-digest` rewrite is tracked in
[mirror-sync issue 2](https://github.com/openharmony-qemu/mirror-sync/issues/2).
The destination history was preserved; the fixed QEMU baseline is unaffected.

Long uploads use repository-scoped App tokens renewed before expiry. Git LFS
credential caching is disabled so a long-running transfer can obtain a fresh
token. The current token is revoked when the job finishes.

GitHub's large-blob limit requires LFS history conversion in some independent
mirrors. Original upstream SHA and hosted SHA are distinct identities and must
be retained in source provenance; do not silently substitute one for the other.

## Local migration utilities

These scripts are administrative tools, not build-time dependencies:

- `create_repositories.py`: create reviewed forks or independent mirrors,
  recording an append-only journal and preserving existing repositories.
- `publish_baselines.py`: establish missing QEMU branches from source locks.
- `setup_mirror_ci.py`: generate the **central** mirror registry; it never adds
  workflows to component repositories.
- `import_mirror_baseline.py`: create a missing QEMU branch from a centrally
  synchronized baseline, without adding automation to the source tree.
- `retire_component_sync.py`: remove the three reviewed legacy installations.
- `configure_source_automation.py`: disable component Actions and cancel automatic
  CI triggered by this migration, preserving unrelated manual workflow runs.
- `inventory_sources.py`, `export_source_changes.py`, `publish_source_changes.py`:
  inventory, review and publish component changes separately from build artifacts.
- `generate_manifest.py`: reproduce manifest selection and the six-product contract.
- `verify_remote_state.py`: audit repository defaults and QEMU branch SHAs through
  read-only API queries without altering source history.
- `stage_component_recipes.py`, `stage_source_helpers.py`: assign tracked patches
  and source preparation helpers to their owning repositories. Cross-component
  patches are split at file boundaries and retain exact reconstruction hashes.
- `verify_component_payloads.py`: verify published component bytes and modes
  against the manifest ownership index and publication lineage.
- `harvest_completed_mirrors.py`, `publish_ready_components.py`: collect completed
  synchronization evidence and resume explicitly reviewed source publications.
- `snapshot_publication.py`: archive full journals and derive the manifest lock.
  Its completion checks require matching remote refs/defaults, current export
  fingerprints, component payloads, production mirror evidence and input cutover.
  When later local edits exist, `--verified-inputs` checks an independent exact
  reconstruction and the preserved original files. It also verifies the active
  compatibility links, backs up changed local inputs, and records them as
  unpublished work. It never replaces those edits or includes them in the lock.
- `scripts/prepare_component_inputs.py`: retrieve locked component inputs and
  invoke the component-owned materializer (`build:.qemu/materialize_adaptations.py`).
- `scripts/ensure_component_inputs.sh`: fetch the locked `openharmony-qemu/manifest`
  commit into `.qemu/manifest` and materialize regular files into `./patches`
  without overwriting tracked scripts in `openharmony-qemu/ohos-qemu`.
- `scripts/stage_prepatched_qemu_tree.py`: validate `components.lock.json`
  pre-patched checkouts and stage omitted binary/signing/profile assets when
  `QEMU_MANIFEST_PREPATCHED=1`.

The manifest's `adaptations.json` contains ownership and checksums only (`103`
component owners, `2,184` files). Editable recipes live under `.qemu/adaptations/`
in their component repositories; the generic materializer belongs to
`openharmony-qemu/build`.

Operation journals and source candidates remain under the ignored
`.tmp/organization-migration/` directory. Candidates are not published source
changes. Published journals, full-history mappings and verification evidence are
archived with the manifest. Its status is
`source_migration_complete_with_reported_conflicts`; every completion gate passes.

All 695 catalog repositories have been created and their uniform QEMU branches
and defaults verified. All 105 reviewed component source changes are published,
and all 2,160 original adaptation inputs have been verified against 104 owning
repositories. All 186 mirrors have complete production verification records,
including the reported upstream conflict above. The system and ArkWeb locks
require different `web_webview` and bounds-checking revisions. Separate ArkWeb mirror repositories
preserve both locked source identities under the same QEMU branch convention.
No package/runtime acceptance is claimed.

The manifest's `dependencies.json` records cross-tree prerequisites and the actual
checkout locations of source dependencies, including Serial SIG in the system
tree. Per-component build dependencies remain in `bundle.json`, GN targets and
the product profiles. `products.json` preserves the six artifact contracts.

## Retrieve historical component inputs

The committed `components.lock.json` is complete. A fresh checkout can retrieve
the component-owned compatibility inputs with:

```sh
git clone --single-branch --branch OpenHarmony-7.0-Release-qemu \
  https://github.com/openharmony-qemu/manifest.git .qemu/manifest
python3 scripts/prepare_component_inputs.py --manifest .qemu/manifest
```

The retriever requires committed manifest documents and the exact locked component
SHAs. It verifies the component-owned materializer and all reconstructed hashes
and file modes. Generated files live under ignored `.qemu/component-inputs/` (or a custom
`--output` directory), while the repository root retains the tracked `patches/`
tree and `scripts/` source helpers directly.

The retrieval command is integrated via `scripts/ensure_component_inputs.sh`
against the committed complete lock in `openharmony-qemu/manifest`
(`48ac299eb434f436f12e921585a397b02688704e`), binding `108` locked component
repositories (`103` adaptation owners, `2,184` files) with verified SHA-256
hashes and executable file modes materialized into gitignored `./patches`.
Package builds and runtime acceptance remain deferred.
