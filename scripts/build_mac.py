"""Builds the macOS disk image, build/LimitSwitcher-<arch>.dmg: a self-contained LimitSwitcher.app
(its own Python, packages and code inside the bundle) next to a shortcut to Applications.

    python3.13 scripts/build_mac.py --arch arm64     (Apple silicon)
    python3.13 scripts/build_mac.py --arch x86_64    (Intel)

Runs on a Mac with Python 3.13 (the bundled Python's version, for pip and the .pyc files) and
clang. Either architecture builds on either Mac. Used by .github/workflows/macos.yml.

- Python: python-build-standalone's relocatable CPython 3.13 (as uv uses), in
  Contents/Resources/runtime.
- Packages: requirements-native.txt, wheels for the target architecture only.
- Code: Contents/Resources/app, compiled ahead of time; the app writes nothing inside the
  bundle (LimitSwitcher.pyw), so its ad-hoc signature stays valid.
- Executable: scripts/mac_launcher.c, running that Python in-process (menu bar space on macOS
  26+; Activity Monitor shows LimitSwitcher), with bundle-relative paths in launcher.conf.
"""
import argparse
import compileall
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import time
import sys
import tarfile
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from account_switcher.version import VERSION  # noqa: E402

PYTHON = "3.13"
STANDALONE = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
TRIPLES = {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}
WHEELS = {"arm64": "macosx_11_0_arm64", "x86_64": "macosx_11_0_x86_64"}
# Not used on macOS (Tk is only for Linux's full view), removed to keep the download small.
UNUSED = ["include", "share", "lib/pkgconfig", "lib/itcl*", "lib/thread*", "lib/tcl*", "lib/tk*",
          "lib/libtcl*", "lib/libtk*"]
UNUSED_STDLIB = ["test", "idlelib", "ensurepip", "turtledemo", "tkinter", "lib2to3", "pydoc_data",
                 "config-*", "lib-dynload/_tkinter*"]


def run(*command, **options):
    print("+", " ".join(map(str, command)), flush=True)
    subprocess.run([str(c) for c in command], check=True, **options)


def fetch_python(arch, runtime, work):
    """python-build-standalone's install_only_stripped build of CPython 3.13 for `arch`."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "LimitSwitcher-build"}
    if os.environ.get("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    with urlopen(Request(STANDALONE, headers=headers), timeout=60) as response:
        assets = json.load(response)["assets"]
    pattern = re.compile(rf"cpython-{re.escape(PYTHON)}\.\d+\+\d+-{TRIPLES[arch]}-install_only_stripped\.tar\.gz$")
    asset = next(a for a in assets if pattern.search(a["name"]))
    archive = work / asset["name"]
    print("Python:", asset["name"], flush=True)
    with urlopen(Request(asset["browser_download_url"], headers={"User-Agent": "LimitSwitcher-build"}),
                 timeout=300) as response, open(archive, "wb") as out:
        shutil.copyfileobj(response, out)
    with tarfile.open(archive) as tar:
        tar.extractall(work, filter="tar")
    shutil.move(str(work / "python"), str(runtime))
    stdlib = runtime / "lib" / f"python{PYTHON}"
    for folder, patterns in ((runtime, UNUSED), (stdlib, UNUSED_STDLIB)):
        for pattern in patterns:
            for path in folder.glob(pattern):
                shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
    return stdlib


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", choices=sorted(TRIPLES), default=os.uname().machine)
    parser.add_argument("--out", default=str(ROOT / "build"))
    args = parser.parse_args()
    if "%d.%d" % sys.version_info[:2] != PYTHON:
        sys.exit(f"Python {PYTHON} is needed to build (this is {sys.version.split()[0]})")
    out = Path(args.out).resolve()
    work = out / f"mac-{args.arch}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    app = work / "dmg" / "LimitSwitcher.app"
    contents = app / "Contents"
    resources = contents / "Resources"
    (contents / "MacOS").mkdir(parents=True)
    resources.mkdir(parents=True)

    # 1. Python and the app's packages (wheels for the target architecture, universal2 included).
    runtime = resources / "runtime"
    stdlib = fetch_python(args.arch, runtime, work)
    run(sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-compile",
        "--only-binary=:all:", "--platform", WHEELS[args.arch], "--python-version", PYTHON, "--implementation", "cp",
        "--target", stdlib / "site-packages", "-r", ROOT / "requirements-native.txt")

    # 2. The app itself, and .pyc files for everything (checked by hash, not by file time).
    code = resources / "app"
    shutil.copytree(ROOT / "account_switcher", code / "account_switcher",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in ("LimitSwitcher.pyw", "README.md", "LICENSE", "THIRD-PARTY-NOTICES.txt"):
        shutil.copy2(ROOT / name, code / name)
    for folder in (code, stdlib):
        ok = compileall.compile_dir(str(folder), quiet=1, workers=0,
                                    invalidation_mode=compileall.py_compile.PycInvalidationMode.UNCHECKED_HASH)
        if not ok and folder == code:
            sys.exit("compiling the app failed")

    # 3. The executable and what it runs (paths inside Contents/Resources; the log in the user's folder).
    run("clang", "-arch", args.arch, "-mmacosx-version-min=11.0", "-O2", "-Wall", "-o", contents / "MacOS" / "LimitSwitcher",
        ROOT / "scripts" / "mac_launcher.c")
    (resources / "launcher.conf").write_text("\n".join([
        f"runtime/lib/libpython{PYTHON}.dylib", "runtime/bin/python3", "app/LimitSwitcher.pyw",
        "~/Library/Application Support/LimitSwitcher/app.log"]) + "\n")
    plist = {"CFBundleName": "LimitSwitcher", "CFBundleDisplayName": "LimitSwitcher",
             "CFBundleIdentifier": "com.accountswitcher.app", "CFBundleExecutable": "LimitSwitcher",
             "CFBundleIconFile": "AppIcon", "CFBundlePackageType": "APPL",
             "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION, "LSUIElement": True,
             "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
             "LSArchitecturePriority": [args.arch], "LSRequiresNativeExecution": True,
             "NSHumanReadableCopyright": "MIT License"}
    (contents / "Info.plist").write_bytes(plistlib.dumps(plist))
    from PIL import Image
    Image.open(ROOT / "account_switcher" / "static" / "assets" / "appicon-mac.png").save(
        resources / "AppIcon.icns", sizes=[(s, s) for s in (16, 32, 64, 128, 256, 512, 1024)])

    # 4. One ad-hoc signature for the whole bundle (Apple silicon runs only signed code).
    run("codesign", "--force", "--deep", "--sign", "-", app)
    run("codesign", "--verify", "--deep", "--strict", app)

    # 5. The disk image: the app and a shortcut to Applications, to drag it onto.
    os.symlink("/Applications", work / "dmg" / "Applications")
    dmg = out / f"LimitSwitcher-{'AppleSilicon' if args.arch == 'arm64' else 'Intel'}.dmg"
    dmg.unlink(missing_ok=True)
    # hdiutil fails now and then when the disk image service is busy ("Resource busy"): a few tries.
    for attempt in range(1, 5):
        try:
            run("hdiutil", "create", "-volname", "LimitSwitcher", "-srcfolder", work / "dmg", "-fs", "HFS+",
                "-format", "UDZO", "-imagekey", "zlib-level=9", "-ov", dmg)
            break
        except subprocess.CalledProcessError:
            if attempt == 4:
                raise
            print(f"hdiutil failed (try {attempt}); trying again", flush=True)
            dmg.unlink(missing_ok=True)
            time.sleep(5 * attempt)
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file() and not f.is_symlink())
    print(f"Built {dmg.name} ({dmg.stat().st_size / 2**20:.1f} MB; the app {size / 2**20:.0f} MB), "
          f"LimitSwitcher {VERSION}", flush=True)


if __name__ == "__main__":
    main()
