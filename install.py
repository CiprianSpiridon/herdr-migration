#!/usr/bin/env python3
"""Install the mod without replacing existing mods, layouts, or key bindings."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def install(app_home):
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or newer is required.")
    source = ROOT / "mods/herdr-migration"
    destination = Path(app_home).expanduser() / "mods/herdr-migration"
    if not shutil.which("herdr-app"):
        raise RuntimeError("Install Herdr desktop and open it once before installing this mod.")
    subprocess.run(["herdr-app", "check", str(source)], check=True, timeout=30)
    if destination.exists():
        supplied = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        if not all((destination / name).is_file() and (destination / name).read_bytes() == contents for name, contents in supplied.items()):
            raise RuntimeError("A different herdr-migration mod already exists. Keep it or rename it before installing; nothing was replaced.")
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=".herdr-migration-", dir=destination.parent))
        try:
            shutil.copytree(source, temporary, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            temporary.rename(destination)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    key_path = destination.parent.parent / "keys.json"
    existing_text = key_path.read_text() if key_path.exists() else None
    keys = json.loads(existing_text) if existing_text is not None else {}
    snippet = json.loads((ROOT / "keys.json").read_text())
    conflicts = []
    for section, bindings in snippet.items():
        for key, action in bindings.items():
            old = keys.setdefault(section, {}).get(key)
            if old and old != action:
                conflicts.append(key)
            else:
                keys[section][key] = action
    # The app watches keys. Refuse a concurrent edit rather than replace it.
    current = key_path.read_text() if key_path.exists() else None
    if current != existing_text:
        raise RuntimeError("Your key bindings changed during installation. The mod is installed; merge keys.json separately.")
    if json.dumps(keys, sort_keys=True) != json.dumps(json.loads(existing_text) if existing_text is not None else {}, sort_keys=True):
        if existing_text is not None:
            backup = key_path.with_name("keys.json.before-herdr-migration")
            if not backup.exists():
                backup.write_text(existing_text)
        temporary = key_path.with_name(".keys-herdr-migration.tmp")
        temporary.write_text(json.dumps(keys, indent=2) + "\n")
        os.replace(temporary, key_path)
    print(json.dumps({"installed": str(destination), "open": "herdr-app do mod.open --name herdr-migration",
                      "preserved_shortcut_conflicts": conflicts}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-home", default=os.environ.get("HERDR_APP_HOME", str(Path.home() / ".config/herdr-app")))
    args = parser.parse_args()
    try:
        install(args.app_home)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
