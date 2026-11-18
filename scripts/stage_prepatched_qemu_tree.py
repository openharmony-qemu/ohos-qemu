#!/usr/bin/env python3
"""Validate pre-patched openharmony-qemu manifest checkouts and stage runtime/signing/binary inputs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


PRODUCT_ARCH = {
    "armv7a_virt": "arm",
    "arm64_virt": "arm64",
    "x86_64_virt": "x86_64",
}

LIVE_CAPTIONS_ABI = {
    "arm64_virt": "arm64-v8a",
    "armv7a_virt": "armeabi-v7a",
    "x86_64_virt": "x86_64",
}

ALL_PRODUCTS = ("arm64_virt", "x86_64_virt", "armv7a_virt")
PRODUCT_CONFIG = {
    "arm64_virt": "vendor/ohemu/qemu_arm64_linux_full/config.json",
    "x86_64_virt": "vendor/ohemu/qemu_x86_64_linux_full/config.json",
    "armv7a_virt": "vendor/ohemu/qemu_armv7a_linux_full/config.json",
}

AVC_LICENSES = (
    "openh264-LICENSE",
    "libc++-LICENSE.TXT",
    "libc++abi-LICENSE.TXT",
    "compiler-rt-LICENSE.TXT",
)

GIT_SHIM_SOURCE = r"""#!/usr/bin/env python3
import json, os, sys

real_git = os.environ["OHOS_QEMU_REAL_GIT"]
mapping = json.loads(open(os.environ["OHOS_QEMU_GIT_SHIM_MAP"], encoding="utf-8").read())
args = sys.argv[1:]

target_dir = os.getcwd()
rest = []
i = 0
parsing_globals = True
while i < len(args):
    a = args[i]
    if parsing_globals and a == "-C" and i + 1 < len(args):
        target_dir = args[i + 1]
        i += 2
        continue
    if parsing_globals and a in ("-c", "--git-dir", "--work-tree") and i + 1 < len(args):
        if a == "--work-tree":
            target_dir = args[i + 1]
        i += 2
        continue
    if parsing_globals and a.startswith("-c") and len(a) > 2:
        i += 1
        continue
    if parsing_globals and a == "--no-optional-locks":
        i += 1
        continue
    parsing_globals = False
    rest.append(a)
    i += 1

real_dir = os.path.realpath(target_dir)
upstream_rev = mapping.get(real_dir)
if upstream_rev:
    if rest in (["rev-parse", "HEAD"], ["rev-parse", "--verify", "HEAD"], ["rev-parse", "--verify", "HEAD^{commit}"]):
        print(upstream_rev)
        sys.exit(0)
    if len(rest) >= 2 and rest[0] == "show" and rest[1].startswith("HEAD:"):
        new_args = []
        replaced = False
        for a in args:
            if not replaced and a.startswith("HEAD:"):
                new_args.append(upstream_rev + ":" + a[5:])
                replaced = True
            else:
                new_args.append(a)
        os.execv(real_git, [real_git, *new_args])

os.execv(real_git, [real_git, *args])
"""


def run(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, env=env, cwd=cwd, text=True, check=check)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_manifest_projects(source_root: Path) -> dict[str, dict[str, str]]:
    """Map relative checkout path -> {'name': ..., 'remote': ...} from resolved repo manifest or .repo/manifests XML."""
    repo_dir = source_root / ".repo"
    if not repo_dir.is_dir():
        raise SystemExit(f"missing .repo directory under {source_root}")
    proc = subprocess.run(["repo", "manifest", "-o", "-"], cwd=source_root, capture_output=True, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        root = ET.fromstring(proc.stdout)
        default_el = root.find("default")
        default_remote = default_el.attrib.get("remote", "") if default_el is not None else ""
        result = {}
        for proj in root.findall("project"):
            path = proj.attrib.get("path", proj.attrib["name"])
            remote = proj.attrib.get("remote", default_remote)
            result[path] = {"name": proj.attrib["name"], "remote": remote}
        return result

    manifests_dir = repo_dir / "manifests"
    root_xml = ET.parse(repo_dir / "manifest.xml").getroot()
    projects: dict[str, dict[str, str]] = {}

    def visit(xml_el: ET.Element, inherited_remote: str) -> None:
        default_el = xml_el.find("default")
        default_remote = default_el.attrib.get("remote", inherited_remote) if default_el is not None else inherited_remote
        for inc in xml_el.findall("include"):
            inc_path = manifests_dir / inc.attrib["name"]
            if inc_path.is_file():
                visit(ET.parse(inc_path).getroot(), default_remote)
        for proj in xml_el.findall("project"):
            path = proj.attrib.get("path", proj.attrib["name"])
            projects[path] = {"name": proj.attrib["name"], "remote": proj.attrib.get("remote", default_remote)}

    visit(root_xml, "")
    return projects


def validate_checkout(source_root: Path, component_lock_path: Path, release_lock_path: Path) -> dict[str, str]:
    """Validate pre-patched qemu HEADs and unpatched upstream HEADs; return checkout path -> upstream revision map."""
    comp_lock = json.loads(component_lock_path.read_text(encoding="utf-8"))
    comp_map = {r["name"]: r for r in comp_lock["repositories"]}
    rel_lock = json.loads(release_lock_path.read_text(encoding="utf-8"))
    manifest_projects = parse_manifest_projects(source_root)

    path_to_upstream: dict[str, str] = {}
    for rel_path, info in manifest_projects.items():
        if info["remote"] != "qemu":
            continue
        checkout = source_root / rel_path
        if not checkout.exists():
            continue
        name = info["name"]
        if name not in comp_map:
            raise SystemExit(f"remote='qemu' checkout missing from components.lock.json: {name} ({rel_path})")
        entry = comp_map[name]
        actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        if actual != entry["qemu_revision"]:
            raise SystemExit(
                f"pre-patched checkout HEAD mismatch for {name} ({rel_path}): expected {entry['qemu_revision']}, got {actual}"
            )
        upstream_rev = entry["upstream_revisions"][0]
        has_upstream = subprocess.run(
            ["git", "-C", str(checkout), "cat-file", "-e", f"{upstream_rev}^{{commit}}"],
            capture_output=True,
        )
        if has_upstream.returncode != 0:
            run(["git", "-C", str(checkout), "fetch", "--no-tags", "--depth=1", "origin", upstream_rev])
        path_to_upstream[os.path.realpath(checkout)] = upstream_rev

    # Verify unpatched locked checkouts match release lock revisions.
    for proj in rel_lock["projects"]:
        rel_path = proj["path"]
        name = proj["name"]
        checkout = source_root / rel_path
        if not checkout.exists():
            continue
        if name in comp_map:
            continue
        actual = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        if actual != proj["revision"]:
            raise SystemExit(
                f"unpatched checkout HEAD mismatch for {name} ({rel_path}): expected {proj['revision']}, got {actual}"
            )
    print(f"Verified pre-patched checkout: {len(path_to_upstream)} qemu components + unpatched release lock")
    return path_to_upstream


def atomic_copy(src: Path, dst: Path, mode: int = 0o644) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".stage-", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_bytes(data: bytes, dst: Path, mode: int = 0o644) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".stage-", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def stage_mesa_llvm(source_root: Path, script_dir: Path, artifacts_root: Path, products: list[str]) -> None:
    sys.path.insert(0, str(script_dir))
    import qemu_mesa_llvm_artifacts as qml

    identity = qml.expected_identity(script_dir)
    recipe = identity["recipe_sha256"]
    for rel in ("device/qemu/common/virt_full/hardware/gpu/BUILD.gn", "third_party/mesa3d/ohos/BUILD.gn"):
        content = (source_root / rel).read_bytes()
        if b"build_qemu_mesa_llvm.py" not in content or recipe.encode() not in content:
            raise SystemExit(f"pre-patched Mesa GN hook or recipe hash missing in {rel} (expected {recipe})")
    mesa = source_root / "third_party/mesa3d"
    for product in products:
        qml.validate(artifacts_root / product, product, identity)
        dest = mesa / "qemu_llvm" / recipe / product
        if dest.exists():
            qml.validate(dest, product, identity)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
            stage = Path(tmp) / product
            stage.mkdir()
            shutil.copytree(artifacts_root / product / "include", stage / "include")
            shutil.copytree(artifacts_root / product / "lib", stage / "lib")
            shutil.copyfile(artifacts_root / product / "manifest.json", stage / "manifest.json")
            qml.validate(stage, product, identity)
            os.replace(stage, dest)
    print(f"Staged Mesa LLVM {recipe} for {products}")


def stage_arkweb_core(source_root: Path, script_dir: Path, patch_root: Path, artifacts_root: Path, products: list[str]) -> None:
    sys.path.insert(0, str(script_dir))
    import m144_core_artifacts as mca

    repo_root = patch_root.parent
    webview = source_root / "base/web/webview"
    dest_root = webview / "ohos_nweb/qemu-core"
    for product in products:
        arch = PRODUCT_ARCH[product]
        record = mca.validate(artifacts_root / arch, arch, repo_root)
        target = dest_root / arch / "ArkWebCore.hap"
        atomic_copy(artifacts_root / arch / "ArkWebCore.hap", target, 0o644)
        receipt = {
            "scope": "source_core_installation_NOT_guest_acceptance",
            "arch": arch,
            "hap_sha256": record["hap_sha256"],
            "core_artifact_sha256": record["core_artifact_sha256"],
            "release_source_lock_sha256": record["release_source_lock_sha256"],
            "first_artifact_behavior_gate_passed": False,
        }
        atomic_write_bytes((json.dumps(receipt, indent=2) + "\n").encode("utf-8"), target.parent / "installed-source-core.json")
    print(f"Staged ArkWeb Core HAPs for {products}")


def stage_device_profile(source_root: Path, patch_root: Path, device_type: str, products: list[str], env: dict[str, str]) -> str:
    all_profile_args = []
    for prod in ALL_PRODUCTS:
        if (source_root / PRODUCT_CONFIG[prod]).is_file():
            all_profile_args.extend(["--product", prod])
    prod_args = []
    for prod in products:
        prod_args.extend(["--product", prod])

    s_2in1 = patch_root / "2in1/product_profile/apply.sh"
    s_phone = patch_root / "phone/product_profile/apply.sh"
    run(["bash", str(s_2in1), "--source-root", str(source_root), "--disable", *all_profile_args], env=env)
    run(["bash", str(s_phone), "--source-root", str(source_root), "--disable", *all_profile_args], env=env)

    p_2in1 = env.get("QEMU_2IN1_PROFILE_COMPONENT", "auto")
    p_phone = env.get("QEMU_PHONE_PROFILE_COMPONENT", "auto")
    if device_type == "2in1" and p_2in1 != "0":
        run(["bash", str(s_2in1), "--source-root", str(source_root), *prod_args], env=env)
        return "qemu_2in1_full_source"
    if device_type == "phone" and p_phone != "0":
        run(["bash", str(s_phone), "--source-root", str(source_root), *prod_args], env=env)
        return "qemu_phone_full_source"
    return "param_only" if device_type else "default"


def stage_sceneboard(source_root: Path, patch_root: Path, device_type: str, env: dict[str, str]) -> None:
    p_2in1 = env.get("QEMU_2IN1_PROFILE_COMPONENT", "auto")
    p_phone = env.get("QEMU_PHONE_PROFILE_COMPONENT", "auto")
    if device_type == "2in1" and p_2in1 == "0":
        return
    if device_type == "phone" and p_phone == "0":
        return
    if device_type not in ("phone", "2in1"):
        return

    cem_file = source_root / "base/notification/common_event_service/tools/cem/src/common_event_command.cpp"
    if not cem_file.is_file():
        raise SystemExit(f"missing SceneBoard CEM prerequisite: {cem_file}")

    sb_mod = load_module("sceneboard_assets", patch_root / "lib/sceneboard_assets.py")
    asset_root = Path(env["SCENEBOARD_RUNTIME_ASSET_ROOT"]).resolve()
    native_rev = env["SCENEBOARD_RUNTIME_NATIVE_REVISION"]
    if not re.fullmatch(r"[0-9a-f]{40}", native_rev):
        raise SystemExit("SCENEBOARD_RUNTIME_NATIVE_REVISION must be a 40-char git SHA")
    jar = source_root / "prebuilts/ohos-sdk/linux/26.0.0/toolchains/lib/hap-sign-tool.jar"
    staged = source_root / "applications/standard/hap/sceneboard"
    staged.mkdir(parents=True, exist_ok=True)
    hashes = {}
    expiration = None
    for filename, module in sb_mod.MODULES.items():
        asset = asset_root / filename
        if not asset.is_file():
            raise SystemExit(f"missing renewed SceneBoard HAP: {asset}")
        digest, not_after = sb_mod.verify_hap(asset, module)
        sb_mod.verify_signature(asset, jar)
        if expiration is not None and expiration != not_after:
            raise SystemExit("SceneBoard modules have different profile expiry dates")
        expiration = not_after
        hashes[filename] = digest
        atomic_copy(asset, staged / filename, 0o644)

    meta_path = source_root / f"vendor/ohemu/virt/virt_{device_type}_full.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["sceneboard_runtime"] = {
        "hap_sha256": hashes,
        "profile_valid_until": expiration,
        "native_source_revision": native_rev,
    }
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Verified and staged renewed SceneBoard runtime HAPs for {device_type}")


def stage_jsvm(source_root: Path, patch_root: Path, device_type: str, products: list[str], env: dict[str, str]) -> str:
    toggle = env.get("QEMU_JSVM_COMPONENT", "auto")
    artifacts = Path(env["JSVM_ENGINE_ARTIFACTS"])
    enabled = toggle == "1" or (toggle == "auto" and (artifacts / "manifest.json").is_file())
    if toggle == "1" and not (artifacts / "manifest.json").is_file():
        raise SystemExit(f"QEMU_JSVM_COMPONENT=1 but artifacts missing: {artifacts}")

    jsvm_dir = patch_root / "common/arkcompiler/jsvm"
    arches = sorted({PRODUCT_ARCH[p] for p in products})
    if enabled:
        run([sys.executable, str(jsvm_dir / "validate_artifacts.py"), "--artifact-root", str(artifacts), "--arch", *arches], env=env)
        target = source_root / "vendor/default/binary/artifacts/js_engine_url"
        (target / "v8").mkdir(parents=True, exist_ok=True)
        (target / "v8-include").mkdir(parents=True, exist_ok=True)
        atomic_copy(artifacts / "manifest.json", target / "manifest.json", 0o644)
        inc_dst = target / "v8-include/v8-include"
        if inc_dst.exists():
            shutil.rmtree(inc_dst)
        shutil.copytree(artifacts / "v8-include/v8-include", inc_dst)
        atomic_copy(jsvm_dir / "files/source_location", inc_dst / "source_location", 0o644)
        for arch in arches:
            v8_arch = target / "v8" / arch
            if v8_arch.exists():
                shutil.rmtree(v8_arch)
            shutil.copytree(artifacts / "v8" / arch, v8_arch)

    meta = {"phone": "vendor/ohemu/virt/virt_phone_full.meta.json", "2in1": "vendor/ohemu/virt/virt_2in1_full.meta.json"}.get(device_type)
    for prod in products:
        args = [sys.executable, str(jsvm_dir / "profile.py"), "--profile", str(source_root / PRODUCT_CONFIG[prod])]
        if meta:
            args.extend(["--metadata", str(source_root / meta)])
        args.append("enable" if enabled else "disable")
        run(args, env=env)
    return "true" if enabled else "false"


def stage_vulkan(source_root: Path, script_dir: Path, patch_root: Path, products: list[str], env: dict[str, str]) -> None:
    toggle = env.get("QEMU_VULKAN_COMPONENT", "auto")
    if toggle == "0":
        return
    artifacts = Path(env["QEMU_VULKAN_ARTIFACTS"])
    arches = [PRODUCT_ARCH[p] for p in products]
    missing = [a for a in arches if not (artifacts / a / "libvk_swiftshader.so").is_file() or (artifacts / a / "libvk_swiftshader.so").stat().st_size == 0]
    if missing:
        if toggle == "1":
            raise SystemExit(f"QEMU_VULKAN_COMPONENT=1 but SwiftShader artifacts incomplete: {artifacts}")
        print("SwiftShader artifacts incomplete; skip optional Vulkan ICD")
        return

    run([sys.executable, str(script_dir / "verify_m144_swiftshader_artifacts.py"), "--artifact-root", str(artifacts), "--arch", *arches], env=env)
    vulkan_files = patch_root / "common/standard_vpn/components/qemu_vulkan/files"
    target = source_root / "device/qemu/common/virt_full/hardware/vulkan"
    atomic_copy(vulkan_files / "BUILD.gn", target / "BUILD.gn", 0o644)
    for arch in arches:
        so_src = artifacts / arch / "libvk_swiftshader.so"
        with so_src.open("rb") as f:
            hdr = f.read(20)
        exp = {"arm": (1, 40), "arm64": (2, 183), "x86_64": (2, 62)}[arch]
        if len(hdr) != 20 or hdr[:4] != b"\x7fELF" or hdr[4] != exp[0] or hdr[5] != 1 or struct.unpack_from("<H", hdr, 18)[0] != exp[1]:
            raise SystemExit(f"SwiftShader ELF mismatch: {so_src} ({arch})")
        atomic_copy(so_src, target / "artifacts" / arch / "libvk_swiftshader.so", 0o644)
        atomic_copy(vulkan_files / arch / "swiftshader.json", target / "etc/vulkan/icd.d" / arch / "swiftshader.json", 0o644)
    print(f"Staged SwiftShader Vulkan ICDs for {arches}")


def stage_software_avc(source_root: Path, patch_root: Path, products: list[str], env: dict[str, str]) -> None:
    artifacts = Path(env["QEMU_AVC_ARTIFACTS"])
    arches = [PRODUCT_ARCH[p] for p in products]
    run([sys.executable, str(patch_root / "lib/avc_artifacts.py"), "--artifact-root", str(artifacts), "--arch", *arches], env=env)
    comp_dir = patch_root / "common/foundation/multimedia/av_codec/software_avc"
    dest = source_root / "foundation/multimedia/av_codec/qemu_avc"
    atomic_copy(comp_dir / "files/BUILD.gn", dest / "BUILD.gn", 0o644)
    atomic_copy(artifacts / "manifest.json", dest / "provenance.json", 0o644)
    for arch in arches:
        atomic_copy(artifacts / arch / "libavcenc_ohos.z.so", dest / "artifacts" / arch / "libavcenc_ohos.z.so", 0o644)
    for notice in AVC_LICENSES:
        atomic_copy(artifacts / "licenses" / notice, dest / "licenses" / notice, 0o644)
    print(f"Staged Software AVC binaries for {arches}")


def stage_openvpn_backend(source_root: Path, script_dir: Path, env: dict[str, str]) -> None:
    archive = Path(env["QEMU_OPENVPN_SOURCE_ARCHIVE"])
    run([sys.executable, str(script_dir / "openvpn_source_archive.py"), "--output", str(archive)], env=env)
    net_build = source_root / "foundation/communication/netmanager_ext/BUILD.gn"
    if "qemu_openvpn_backend" not in net_build.read_text(encoding="utf-8"):
        raise SystemExit(f"pre-patched netmanager_ext BUILD.gn missing qemu_openvpn_backend: {net_build}")
    sys.path.insert(0, str(script_dir))
    import prepare_qemu_openvpn_backend as pqo

    planned = pqo.payloads(archive)
    for rel, raw in planned.items():
        if not rel.startswith("foundation/communication/netmanager_ext/qemu_openvpn/"):
            continue
        mode = 0o755 if rel == pqo.EXECUTABLE_ENTRYPOINT else 0o644
        atomic_write_bytes(raw, source_root / rel, mode)
    print("Staged OpenVPN archive and recipe inputs")


def stage_intelligent_voice_cpu_assets(source_root: Path, patch_root: Path) -> None:
    cpu_dir = patch_root / "common/drivers/peripheral/intelligent_voice/cpu_provider"
    vs = load_module("cpu_provider_verify_source", cpu_dir / "verify_source.py")
    edits, index, _ = vs.metadata()
    state = vs.complete_state(vs.vectors(source_root, edits), edits)
    if state != "final":
        raise SystemExit(f"pre-patched intelligent_voice CPU source must be 'final', found '{state}'")
    assets = vs.payloads(index)
    for rel, raw in assets.items():
        target = source_root / rel
        if target.is_file() and not target.is_symlink() and target.read_bytes() == raw:
            continue
        atomic_write_bytes(raw, target, 0o644)
    print(f"Staged {len(assets)} intelligent_voice CPU assets")


def configure_2in1_syscaps(source_root: Path, patch_root: Path, products: list[str]) -> None:
    sys.path.insert(0, str(patch_root / "lib"))
    from device_contract import contract_overlay

    eff_path = source_root / "vendor/ohemu/virt/virt_2in1_full.json"
    meta_path = source_root / "vendor/ohemu/virt/virt_2in1_full.meta.json"
    eff = json.loads(eff_path.read_text(encoding="utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    enables = {
        "SystemCapability.Communication.NetManager.Eap": (
            "communication", "netmanager_ext",
            "Official 2in1 capability; enable upstream EAP NAPI/IPC implementation and matching declaration.",
        ),
        "SystemCapability.Multimedia.Audio.DeviceEnhance": (
            "multimedia", "audio_framework",
            "Official 2in1 capability; enable upstream AudioDeviceEnhanceManager and matching declaration.",
        ),
    }
    camera_features = {
        "drivers_peripheral_camera_feature_usb": False,
        "drivers_peripheral_camera_v4l2_emulator": False,
    }
    for sub in eff.get("subsystems", []):
        for comp in sub.get("components", []):
            if sub.get("subsystem") == "hdf" and comp.get("component") == "drivers_peripheral_camera":
                fvals = [v for v in comp.get("features", []) if v.split("=", 1)[0].strip() not in camera_features]
                comp["features"] = fvals + [f"{k} = {str(v).lower()}" for k, v in sorted(camera_features.items())]
            for cap, (s_name, c_name, _reason) in enables.items():
                if sub.get("subsystem") == s_name and comp.get("component") == c_name:
                    vals = [v for v in comp.get("syscap", []) if v.split("=", 1)[0].strip() != cap]
                    comp["syscap"] = vals + [f"{cap} = true"]
    meta.setdefault("contract_feature_overrides", {})["hdf:drivers_peripheral_camera"] = camera_features
    meta.setdefault("enabled_required_syscaps", {}).update({
        cap: {"part": f"{s_name}:{c_name}", "reason": reason}
        for cap, (s_name, c_name, reason) in sorted(enables.items())
    })
    eff_path.write_text(json.dumps(eff, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for prod in products:
        cfg_path = source_root / PRODUCT_CONFIG[prod]
        if not cfg_path.is_file():
            continue
        overlay_rel = f"vendor/ohemu/virt/virt_2in1_contract_{prod}.json"
        cfg_doc = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_for_visit = dict(cfg_doc, inherit=[i for i in cfg_doc.get("inherit", []) if i != overlay_rel])
        overlay = contract_overlay(source_root, cfg_for_visit, meta)
        (source_root / overlay_rel).write_text(json.dumps(overlay, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def stage_inputs(args: argparse.Namespace) -> None:
    source_root = args.source_root.resolve()
    cache_root = args.cache_root.resolve()
    patch_root = args.patch_root.resolve()
    script_dir = args.script_dir.resolve()
    products = args.product
    device_type = args.device_type or ""

    path_map = validate_checkout(source_root, args.component_lock, args.release_lock)
    stage_mesa_llvm(source_root, script_dir, Path(os.environ["QEMU_MESA_LLVM_ARTIFACTS"]), products)
    stage_arkweb_core(source_root, script_dir, patch_root, Path(os.environ["QEMU_ARKWEB_CORE_ARTIFACTS"]), products)

    with tempfile.TemporaryDirectory(prefix="ohos-qemu-git-shim-") as tmpdir:
        tmp = Path(tmpdir)
        map_file = tmp / "git_map.json"
        map_file.write_text(json.dumps(path_map), encoding="utf-8")
        shim_dir = tmp / "bin"
        shim_dir.mkdir()
        shim_git = shim_dir / "git"
        shim_git.write_text(GIT_SHIM_SOURCE, encoding="utf-8")
        shim_git.chmod(0o755)
        real_git = shutil.which("git") or "/usr/bin/git"
        env = dict(
            os.environ,
            PATH=f"{shim_dir}:{os.environ['PATH']}",
            OHOS_QEMU_REAL_GIT=real_git,
            OHOS_QEMU_GIT_SHIM_MAP=str(map_file),
        )

        profile = stage_device_profile(source_root, patch_root, device_type, products, env)
        stage_sceneboard(source_root, patch_root, device_type, env)
        if device_type in ("phone", "2in1"):
            rev = os.environ.get("QEMU_RECIPE_REVISION", "")
            if not re.fullmatch(r"[0-9a-f]{40}", rev) and os.environ.get("QEMU_BUILD_INPUT_MANIFEST"):
                rev = json.loads(Path(os.environ["QEMU_BUILD_INPUT_MANIFEST"]).read_text(encoding="utf-8")).get("source_git_revision", "")
            if not re.fullmatch(r"[0-9a-f]{40}", rev):
                rev = subprocess.check_output(
                    ["git", "-C", str(patch_root.parent), "rev-parse", "HEAD"], text=True
                ).strip()
            pc_script = patch_root / "common/applications/standard/hap/privacycenter_provider/apply.py"
            asset_root = os.environ["SCENEBOARD_RUNTIME_ASSET_ROOT"]
            run([sys.executable, str(pc_script), "--source-root", str(source_root), "--asset-root", asset_root, "--native-source-revision", rev, "--device", device_type], env=env)
            run([sys.executable, str(pc_script), "--verify-only", "--source-root", str(source_root), "--native-source-revision", rev, "--device", device_type], env=env)

        jsvm_enabled = stage_jsvm(source_root, patch_root, device_type, products, env)
        stage_vulkan(source_root, script_dir, patch_root, products, env)
        stage_software_avc(source_root, patch_root, products, env)
        stage_openvpn_backend(source_root, script_dir, env)

        # HAP/UI signing and .har/.tgz/SDK26/ASR dependencies
        run([sys.executable, str(patch_root / "common/applications/standard/photos/source_provider/prepare_dependencies.py"),
             "--source-root", str(source_root), "--cache-root", str(cache_root / "photos-source-dependencies"),
             "--evidence", str(cache_root / "logs/photos-source-preparation.json")], env=env)
        run([sys.executable, str(patch_root / "common/vendor/ohemu/virt/photos_provider/prepare.py"),
             "--source-root", str(source_root), "--require-patched"], env=env)
        run([sys.executable, str(patch_root / "common/applications/standard/settings/source_provider/prepare_dependencies.py"),
             "--source-root", str(source_root), "--cache-root", str(cache_root / "settings-source-dependencies"),
             "--evidence", str(cache_root / "logs/settings-source-preparation.json")], env=env)
        run([sys.executable, str(patch_root / "common/vendor/ohemu/virt/settings_provider/prepare.py"),
             "--source-root", str(source_root), "--require-patched"], env=env)
        run([sys.executable, str(patch_root / "common/vendor/ohemu/virt/calendar_provider/prepare.py"),
             "--source-root", str(source_root)], env=env)
        run([sys.executable, str(patch_root / "lib/admin_provider.py"), "--source-root", str(source_root), "--prepare"], env=env)
        run([sys.executable, str(patch_root / "common/applications/standard/mms/source_provider/prepare_dependencies.py"),
             "--source-root", str(source_root), "--cache-root", str(cache_root / "mms-source-dependencies")], env=env)
        run([sys.executable, str(patch_root / "lib/mms_provider.py"), "--source-root", str(source_root)], env=env)
        run([sys.executable, str(patch_root / "lib/contacts_theme_providers.py"), "--source-root", str(source_root)], env=env)
        run([sys.executable, str(patch_root / "common/applications/standard/systemui/source_provider/prepare_dependencies.py"),
             "--source-root", str(source_root), "--cache-root", str(cache_root / "systemui-source-dependencies"),
             "--evidence", str(cache_root / "logs/systemui-source-preparation.json")], env=env)

        if device_type == "phone" and env.get("QEMU_PHONE_PROFILE_COMPONENT", "auto") != "0":
            run([sys.executable, str(patch_root / "lib/number_identity_provider.py"), "--source-root", str(source_root), "--prepare"], env=env)
            run([sys.executable, str(patch_root / "common/vendor/ohemu/virt/number_identity_local/configure.py"), "--source-root", str(source_root)], env=env)

        run([sys.executable, str(patch_root / "common/foundation/ability/ability_runtime/service_idle_startid/prepare_sdk26.py"),
             "--source-root", str(source_root), "--sdk-ets-root", str(source_root / "prebuilts/ohos-sdk/linux/26.0.0/ets")], env=env)
        run([sys.executable, str(patch_root / "lib/qemu_ui_providers.py"), "--source-root", str(source_root),
             "--cache-root", str(cache_root / "ui-provider-inputs")], env=env)
        run([sys.executable, str(patch_root / "lib/communicationsetting_provider.py"), "--source-root", str(source_root),
             "--cache-root", str(cache_root / "communicationsetting-source-profile"), "--prepare-profile"], env=env)

        if len(products) == 1:
            prod = products[0]
            run([sys.executable, str(script_dir / "prepare_livecaptions_lottie_cache.py"), "--cache-root", str(cache_root)], env=env)
            if prod == "armv7a_virt":
                run([sys.executable, str(script_dir / "prepare_armv7a_sherpa_inputs.py"), "--source-root", str(source_root), "--cache-root", str(cache_root)], env=env)
                lc_inputs = env.get("QEMU_LIVE_CAPTIONS_INPUTS") or str(cache_root / "artifacts/livecaptions-lottie-7.0/arm32-prebuilt/livecaptions/product-assets-arm32.json")
            else:
                lc_inputs = env.get("QEMU_LIVE_CAPTIONS_INPUTS") or str(cache_root / "artifacts/livecaptions-lottie-7.0/r66-asr-product-assets579-05/product-assets.json")
            lc_sha = env.get("QEMU_LIVE_CAPTIONS_INPUTS_SHA256") or hashlib.sha256(Path(lc_inputs).read_bytes()).hexdigest()
            lottie = env["QEMU_LOTTIE_INPUT"]
            abi = env.get("QEMU_LIVE_CAPTIONS_ABI") or LIVE_CAPTIONS_ABI[prod]
            run([sys.executable, str(patch_root / "lib/dialog_providers579.py"), "--provider", "callsetting",
                 "--source-root", str(source_root), "--cache-root", str(cache_root / "callsetting-source-profile"),
                 "--lottie-input", lottie, "--prepare-profile"], env=env)
            run([sys.executable, str(patch_root / "lib/dialog_providers579.py"), "--provider", "certmanager",
                 "--source-root", str(source_root), "--cache-root", str(cache_root / "certmanager-source-profile"),
                 "--prepare-profile"], env=env)
            run([sys.executable, str(patch_root / "lib/livecaptions_provider.py"), "--source-root", str(source_root),
                 "--cache-root", str(cache_root / "livecaptions-source-profile"), "--inputs", lc_inputs,
                 "--inputs-sha256", lc_sha, "--abi", abi, "--product", prod, "--device", device_type], env=env)

        if device_type in ("phone", "2in1"):
            kargs = [sys.executable, str(script_dir / "prepare_qemu_knowledge_resource.py"), "--source-root", str(source_root)]
            if env.get("QEMU_KNOWLEDGE_ICS"):
                kargs.extend(["--asset", env["QEMU_KNOWLEDGE_ICS"]])
            run(kargs, env=env)

        stage_intelligent_voice_cpu_assets(source_root, patch_root)

        usb_profile = {"qemu_phone_full_source": "phone", "qemu_2in1_full_source": "2in1"}.get(profile, "none")
        run(["bash", str(patch_root / "common/device/qemu/usb_midi_shared_device/apply.sh"),
             "--source-root", str(source_root), "--product", *products, "--profile", usb_profile], env=env)
        if profile == "qemu_2in1_full_source":
            configure_2in1_syscaps(source_root, patch_root, products)
        run(["bash", str(patch_root / "common/base/security/selinux_adapter/qemu_sh_baseline/apply.sh"),
             "--source-root", str(source_root), "--profile", usb_profile], env=env)
        run([sys.executable, str(source_root / "device/qemu/common/virt_full/kernel/kernel_input_evidence.py"),
             "prepare", "--source-root", str(source_root)], env=env)

        if args.env_out:
            args.env_out.write_text(
                f"DEVICE_TYPE_BUILD_PROFILE={profile}\nQEMU_JSVM_ENABLED={jsvm_enabled}\n",
                encoding="utf-8",
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    val = sub.add_parser("validate-checkout", help="Verify pre-patched component HEADs against components.lock.json")
    val.add_argument("--source-root", type=Path, required=True)
    val.add_argument("--component-lock", type=Path, required=True)
    val.add_argument("--release-lock", type=Path, required=True)

    stg = sub.add_parser("stage-inputs", help="Stage omitted binary/signing/profile inputs onto pre-patched checkouts")
    stg.add_argument("--source-root", type=Path, required=True)
    stg.add_argument("--cache-root", type=Path, required=True)
    stg.add_argument("--patch-root", type=Path, required=True)
    stg.add_argument("--script-dir", type=Path, required=True)
    stg.add_argument("--component-lock", type=Path, required=True)
    stg.add_argument("--release-lock", type=Path, required=True)
    stg.add_argument("--device-type", default="")
    stg.add_argument("--product", nargs="+", choices=ALL_PRODUCTS, required=True)
    stg.add_argument("--env-out", type=Path)

    args = parser.parse_args()
    if args.cmd == "validate-checkout":
        validate_checkout(args.source_root.resolve(), args.component_lock.resolve(), args.release_lock.resolve())
    else:
        stage_inputs(args)


if __name__ == "__main__":
    main()
