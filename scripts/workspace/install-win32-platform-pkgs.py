#!/usr/bin/env python3
"""Install missing win32-x64 optional platform packages into the pnpm virtual store.

pnpm install --no-optional skipped ALL optional dependencies, which includes the
platform-specific binaries (esbuild, sharp, koffi, rolldown, lightningcss, etc).
This script downloads just the win32-x64 packages we need from the npmmirror
registry and places them in the .pnpm virtual-store layout, creating the
hoisted links under node_modules/.pnpm/node_modules.
"""
import io
import json
import os
import sys
import tarfile
import urllib.request

BASE = r"D:\tmp\deepseek-harness\my-deepseek-harness\deepseek-harness"
PNPM = os.path.join(BASE, "node_modules", ".pnpm")
HOIST = os.path.join(PNPM, "node_modules")
REGISTRY = "https://registry.npmmirror.com"
PROXY = "http://127.0.0.1:10809"

# (package_name, version) - all win32-x64 platform packages from lockfile
REQUIRED = [
    ("@esbuild/win32-x64", "0.21.5"),
    ("@esbuild/win32-x64", "0.25.12"),
    ("@esbuild/win32-x64", "0.28.1"),
    ("@img/sharp-win32-x64", "0.35.3"),
    ("@koromix/koffi-win32-x64", "3.1.1"),
    ("@oxc-parser/binding-win32-x64-msvc", "0.133.0"),
    ("@oxc-resolver/binding-win32-x64-msvc", "11.20.0"),
    ("@oxlint/binding-win32-x64-msvc", "1.76.0"),
    ("@oxlint-tsgolint/win32-x64", "7.0.2001"),
    ("@rolldown/binding-win32-x64-msvc", "1.0.3"),
    ("@rolldown/binding-win32-x64-msvc", "1.1.1"),
    ("@rollup/rollup-win32-x64-gnu", "4.62.2"),
    ("@rollup/rollup-win32-x64-msvc", "4.62.2"),
    ("@vscode/ripgrep-win32-x64", "1.18.0"),
    ("lightningcss-win32-x64-msvc", "1.32.0"),
    ("node-addon-require-builtin-win32-x64-msvc", "0.1.4"),
]

def encoded_dirname(name, version):
    """pnpm virtual store dir name: @scope/name@ver -> @scope+name@ver"""
    return f"{name.replace('/', '+')}@{version}"

def fetch(url):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    )
    req = urllib.request.Request(url, headers={"User-Agent": "pnpm-fix"})
    with opener.open(req, timeout=120) as r:
        return r.read()

def install_one(name, version):
    store_dir = os.path.join(PNPM, encoded_dirname(name, version))
    pkg_root = os.path.join(store_dir, "node_modules", name)
    if os.path.exists(pkg_root):
        return "skip-exists"
    # resolve registry metadata -> tarball
    url_name = name.replace("/", "%2F")
    meta = json.loads(fetch(f"{REGISTRY}/{url_name}/{version}"))
    tarball = meta["dist"]["tarball"]
    data = fetch(tarball)
    os.makedirs(pkg_root, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for member in tf.getmembers():
            # tar entries are prefixed with package/
            if member.name.startswith("package/"):
                member.name = member.name[len("package/"):]
                tf.extract(member, pkg_root)
    # hoisted link
    hoist_link = os.path.join(HOIST, *name.split("/"))
    if not os.path.lexists(hoist_link):
        os.makedirs(os.path.dirname(hoist_link), exist_ok=True)
        os.symlink(os.path.join(os.path.relpath(pkg_root, os.path.dirname(hoist_link))), hoist_link)
    return "installed"

def main():
    only = sys.argv[1:] if len(sys.argv) > 1 else None
    ok, fail, skip = 0, 0, 0
    for name, version in REQUIRED:
        if only and name not in only:
            continue
        try:
            r = install_one(name, version)
            if r == "installed":
                ok += 1
                print(f"  OK  {name}@{version}")
            elif r == "skip-exists":
                skip += 1
                print(f"  --  {name}@{version} (exists)")
        except Exception as e:
            fail += 1
            print(f"FAIL  {name}@{version}: {e}")
    print(f"\ndone: installed={ok} skipped={skip} failed={fail}")

if __name__ == "__main__":
    main()
