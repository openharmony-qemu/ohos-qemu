#!/usr/bin/env python3
"""Safely delete unpatched/backup repositories in openharmony-qemu from a verified plan."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def gh_delete(org: str, name: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["gh", "api", "--method", "DELETE", f"repos/{org}/{name}"],
        capture_output=True,
        text=True,
    )
    return proc.returncode, (proc.stderr or proc.stdout).strip()


def list_org_repos(org: str) -> list[str]:
    out = subprocess.check_output(
        ["gh", "api", "--paginate", f"orgs/{org}/repos?per_page=100", "--jq", ".[].name"],
        text=True,
    )
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> int:
    default_plan = Path(__file__).resolve().parents[1] / "docs/unpatched-repos-cleanup.json"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, default=default_plan)
    parser.add_argument("--execute", action="store_true", help="Actually invoke DELETE on GitHub; default is dry-run")
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    org = plan["organization"]
    keep = set(plan["keep"])
    delete_list = plan["delete_priority_first"] + plan["delete_remaining"]
    assert len(keep) == 111, len(keep)
    assert len(set(delete_list)) == len(delete_list), "Duplicate delete entries"
    assert not (keep & set(delete_list)), f"Overlap with keep set: {keep & set(delete_list)}"

    if not args.execute:
        print(f"DRY RUN: {len(keep)} kept, {len(delete_list)} queued ({len(plan['delete_priority_first'])} LFS/backup first)")
        return 0

    existing = set(list_org_repos(org))
    assert keep.issubset(existing), f"Missing kept repos on GitHub: {keep - existing}"
    targets = [n for n in delete_list if n in existing]
    print(f"Deleting {len(targets)} repositories from {org} (skipping {len(delete_list) - len(targets)} already absent)...")
    for idx, name in enumerate(targets, 1):
        rc, msg = gh_delete(org, name)
        if rc != 0:
            print(f"FAILED [{idx}/{len(targets)}] {org}/{name}: {msg}")
            return 1
        if idx <= 25 or idx % 50 == 0 or idx == len(targets):
            print(f"Deleted [{idx}/{len(targets)}] {org}/{name}", flush=True)

    remaining = set(list_org_repos(org))
    print(f"Remaining repositories in {org}: {len(remaining)}")
    assert remaining == keep, f"Mismatch after deletion: extra={remaining - keep} missing={keep - remaining}"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
