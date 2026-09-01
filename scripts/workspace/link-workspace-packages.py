#!/usr/bin/env python3
"""Link all workspace @deepseek-ai packages from .pnpm virtual store to root node_modules.

Root cause: pnpm only links root package.json's direct deps into the top-level
node_modules/@deepseek-ai/. But deepseek-harness workspace packages do dynamic
imports (loader plugin tree) that resolve from the real package path upward,
so every @deepseek-ai/* workspace package must be resolvable from the root
node_modules. This script creates Windows junctions (no admin required) for
the missing ones.
"""
import os
import sys
import subprocess

BASE = r"D:\tmp\deepseek-harness\my-deepseek-harness\deepseek-harness"
SRC = os.path.join(BASE, "node_modules", ".pnpm", "node_modules", "@deepseek-ai")
DST = os.path.join(BASE, "node_modules", "@deepseek-ai")

def make_junction(link, target):
    # cmd mklink /J requires the link to NOT exist; run via subprocess with
    # shell=False to avoid the Git-Bash path mangling.
    r = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", link, target],
        capture_output=True, text=True,
    )
    return r.returncode == 0

def main():
    if not os.path.isdir(SRC):
        print(f"ERROR: virtual store dir not found: {SRC}")
        sys.exit(1)
    os.makedirs(DST, exist_ok=True)
    names = sorted(os.listdir(SRC))
    created, failed, skipped = 0, 0, 0
    for name in names:
        link = os.path.join(DST, name)
        target = os.path.join(SRC, name)
        if os.path.lexists(link):
            skipped += 1
            continue
        if make_junction(link, target):
            created += 1
        else:
            failed += 1
            print(f"  FAILED: {name}")
    print(f"scanned={len(names)} created={created} skipped={skipped} failed={failed}")

if __name__ == "__main__":
    main()
