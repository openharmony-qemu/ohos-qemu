#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: ensure_component_inputs.sh [--manifest-url URL] [--manifest-branch BRANCH] [--manifest-revision REV]

Materializes component-owned patches/ from openharmony-qemu/manifest into
.qemu/component-inputs and copies them as regular files into ./patches without
overwriting tracked scripts in this repository.
USAGE
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

MANIFEST_URL="${MANIFEST_URL:-https://github.com/openharmony-qemu/manifest.git}"
MANIFEST_BRANCH="${OHOS_BRANCH:-OpenHarmony-7.0-Release-qemu}"
MANIFEST_REVISION="${MANIFEST_REVISION:-48ac299eb434f436f12e921585a397b02688704e}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --manifest-url)
      MANIFEST_URL="${2:?missing value for --manifest-url}"
      shift 2
      ;;
    --manifest-branch)
      MANIFEST_BRANCH="${2:?missing value for --manifest-branch}"
      shift 2
      ;;
    --manifest-revision)
      MANIFEST_REVISION="${2:?missing value for --manifest-revision}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

MANIFEST_DIR="${REPO_ROOT}/.qemu/manifest"
CACHE_DIR="${REPO_ROOT}/.qemu/components"
INPUTS_DIR="${REPO_ROOT}/.qemu/component-inputs"
PATCHES_DIR="${REPO_ROOT}/patches"

mkdir -p "${REPO_ROOT}/.qemu"

if [ ! -d "${MANIFEST_DIR}/.git" ]; then
  rm -rf "${MANIFEST_DIR}"
  GIT_LFS_SKIP_SMUDGE=1 GIT_TERMINAL_PROMPT=0 git clone \
    --single-branch --branch "${MANIFEST_BRANCH}" \
    "${MANIFEST_URL}" "${MANIFEST_DIR}"
else
  if [ "$(git -C "${MANIFEST_DIR}" remote get-url origin 2>/dev/null || true)" != "${MANIFEST_URL}" ]; then
    git -C "${MANIFEST_DIR}" remote set-url origin "${MANIFEST_URL}"
  fi
fi

GIT_LFS_SKIP_SMUDGE=1 GIT_TERMINAL_PROMPT=0 git -C "${MANIFEST_DIR}" fetch \
  --no-tags origin "${MANIFEST_REVISION}"
RESOLVED_SHA="$(git -C "${MANIFEST_DIR}" rev-parse --verify "FETCH_HEAD^{commit}")"
if [ "$(git -C "${MANIFEST_DIR}" rev-parse --verify HEAD 2>/dev/null || true)" != "${RESOLVED_SHA}" ]; then
  git -C "${MANIFEST_DIR}" checkout --detach "${RESOLVED_SHA}"
fi

NEED_REFRESH=0
if [ ! -f "${INPUTS_DIR}/.component-inputs.json" ] || [ ! -d "${INPUTS_DIR}/patches" ]; then
  NEED_REFRESH=1
else
  NEED_REFRESH="$(python3 - "${MANIFEST_DIR}" "${INPUTS_DIR}" <<'PY'
import hashlib, json, subprocess, sys
from pathlib import Path

manifest = Path(sys.argv[1])
inputs = Path(sys.argv[2])
try:
    receipt = json.loads((inputs / ".component-inputs.json").read_text(encoding="utf-8"))
    lock_sha = hashlib.sha256((manifest / "components.lock.json").read_bytes()).hexdigest()
    actual_head = subprocess.check_output(["git", "-C", str(manifest), "rev-parse", "HEAD"], text=True).strip()
    if receipt.get("manifest_commit") == actual_head and receipt.get("component_lock_sha256") == lock_sha:
        print("0")
    else:
        print("1")
except Exception:
    print("1")
PY
)"
fi

if [ "${NEED_REFRESH}" = "1" ]; then
  rm -rf "${INPUTS_DIR}"
  python3 "${REPO_ROOT}/scripts/prepare_component_inputs.py" \
    --manifest "${MANIFEST_DIR}" \
    --cache "${CACHE_DIR}" \
    --output "${INPUTS_DIR}"
fi

# Materialize ./patches as regular files (never symlinks) so snapshot_build_inputs.py
# and m144_dependency_recipe.py validate and hash identical patch recipes.
NEED_COPY=1
if [ -f "${PATCHES_DIR}/.component-recipes.json" ] && \
   [ -f "${INPUTS_DIR}/patches/.component-recipes.json" ] && \
   cmp -s "${INPUTS_DIR}/patches/.component-recipes.json" "${PATCHES_DIR}/.component-recipes.json" && \
   [ -z "$(find "${PATCHES_DIR}" -type l 2>/dev/null | head -n 1)" ]; then
  NEED_COPY=0
fi

if [ "${NEED_COPY}" = "1" ]; then
  STAGE_PATCHES="$(mktemp -d "${REPO_ROOT}/.qemu/patches-stage.XXXXXX")"
  cp -a "${INPUTS_DIR}/patches/." "${STAGE_PATCHES}/"
  if find "${STAGE_PATCHES}" -type l | grep -q .; then
    rm -rf "${STAGE_PATCHES}"
    echo "symlink detected in materialized patches; refusing" >&2
    exit 1
  fi
  rm -rf "${PATCHES_DIR}"
  mv "${STAGE_PATCHES}" "${PATCHES_DIR}"
fi

# Ensure any non-tracked materialized helper under scripts/ exists without
# overwriting tracked/adapted packaging scripts in this repository.
python3 - "${INPUTS_DIR}" "${REPO_ROOT}" <<'PY'
import shutil, sys
from pathlib import Path

inputs = Path(sys.argv[1])
repo = Path(sys.argv[2])
scripts_src = inputs / "scripts"
if scripts_src.is_dir():
    for src in scripts_src.rglob("*"):
        if src.is_symlink() or not src.is_file():
            continue
        rel = src.relative_to(inputs)
        dst = repo / rel
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
PY

echo "Component inputs ready from manifest ${RESOLVED_SHA}: ${PATCHES_DIR}"
