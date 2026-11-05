#!/usr/bin/env python3
"""Publishes a WHE client to the launcher's file server.

Hashes the files selected in whe-publish.json, zips stable folders into bundles, writes the manifest, and
uploads through WSL rsync. Only changed files are sent, and the manifest goes last, so testers never see a
manifest whose files are not on the server yet.

    python tools/whe_publish.py --dry-run     show what would ship
    python tools/whe_publish.py --no-upload   build the manifest and bundles only
    python tools/whe_publish.py               build and upload
"""

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
CHUNK = 4 * 1024 * 1024


def load_json(path, fallback):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return fallback


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=1), encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def matches(relative, patterns):
    lowered = relative.lower()
    name = lowered.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(lowered, p.lower()) or fnmatch.fnmatch(name, p.lower()) for p in patterns)


def select_files(client, config):
    """Returns {relative path: source path}, using the casing found on disk."""
    selected = {}
    exclude = config.get("exclude", [])

    def add(path):
        relative = path.relative_to(client).as_posix()
        if not matches(relative, exclude):
            selected[relative] = path

    for pattern in config.get("files", []):
        found = sorted(client.glob(pattern))
        if not found:
            raise SystemExit(f"Nothing matches {pattern!r} in {client}")
        for path in found:
            if path.is_file():
                add(path)
    for folder in config.get("folders", []):
        root = client / folder
        if not root.is_dir():
            raise SystemExit(f"Missing folder: {root}")
        for current, _, names in os.walk(root):
            for name in names:
                add(Path(current) / name)
    for relative, source in config.get("extras", {}).items():
        source = Path(source)
        if not source.is_file():
            raise SystemExit(f"Missing extra file: {source}")
        selected[relative] = source
    return selected


def hash_files(selected, cache_path):
    cache = load_json(cache_path, {})
    fresh = {}
    entries = {}
    done = 0
    for relative, source in sorted(selected.items()):
        stat = source.stat()
        key = str(source)
        known = cache.get(key)
        if known and known["size"] == stat.st_size and known["mtime"] == stat.st_mtime_ns:
            digest = known["sha256"]
        else:
            digest = sha256_file(source)
        fresh[key] = {"size": stat.st_size, "mtime": stat.st_mtime_ns, "sha256": digest}
        entries[relative] = {"path": relative, "sha256": digest, "size": stat.st_size}
        done += 1
        if done % 5000 == 0:
            print(f"  hashed {done}/{len(selected)}")
            write_json(cache_path, fresh)
    write_json(cache_path, fresh)
    return entries


def plan_bundles(entries, config):
    """Splits bundle roots into folders no larger than bundleMaxMB, so one changed file stays a small download."""
    limit = config.get("bundleMaxMB", 150) * 1024 * 1024
    minimum = config.get("bundleMinFiles", 16)
    loose = [PurePosixPath(p.lower()) for p in config.get("loose", [])]

    def is_under(path, folder):
        return folder == path or folder in PurePosixPath(path).parents

    def plan(folder, members):
        lowered = PurePosixPath(folder.lower())
        if any(is_under(lowered, skip) for skip in loose):
            return []
        contains_loose = any(lowered in skip.parents for skip in loose)
        total = sum(entry["size"] for entry in members)
        if not contains_loose and len(members) >= minimum and total <= limit:
            return [folder]
        if len(members) < minimum and not contains_loose:
            return []
        children = {}
        prefix_length = len(folder) + 1
        for entry in members:
            rest = entry["path"][prefix_length:]
            if "/" in rest:
                children.setdefault(rest.split("/", 1)[0], []).append(entry)
        result = []
        for name, child_members in sorted(children.items()):
            result.extend(plan(f"{folder}/{name}", child_members))
        return result

    bundles = []
    for root in config.get("bundleRoots", []):
        prefix = root.lower() + "/"
        members = [entry for entry in entries.values() if entry["path"].lower().startswith(prefix)]
        if members:
            actual = members[0]["path"][: len(root)]
            bundles.extend(plan(actual, members))
    return bundles


def build_bundle(folder, members, selected, bundle_dir, known):
    """Rebuilds only when a member changed. Entry paths start at the folder's own name, as the launcher expects."""
    description = "\n".join(f"{m['path']}\0{m['sha256']}" for m in sorted(members, key=lambda m: m["path"]))
    name = hashlib.sha256(description.encode("utf-8")).hexdigest()[:40] + ".zip"
    target = bundle_dir / name
    record = known.get(name)
    if record and target.is_file() and target.stat().st_size == record["size"]:
        return folder, name, record
    parent_length = folder.rfind("/") + 1
    temporary = target.with_suffix(".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=3, strict_timestamps=False) as archive:
        for member in sorted(members, key=lambda m: m["path"]):
            archive.write(selected[member["path"]], member["path"][parent_length:])
    record = {"sha256": sha256_file(temporary), "size": temporary.stat().st_size}
    os.replace(temporary, target)
    return folder, name, record


def wsl_path(path):
    drive, rest = os.path.splitdrive(str(Path(path).resolve()))
    return f"/mnt/{drive[0].lower()}{rest.replace(chr(92), '/')}"


def run(command):
    print("  $ " + " ".join(command))
    subprocess.run(command, check=True)


def rsync(*arguments):
    run(["wsl", "rsync", "-rt", "--chmod=D755,F644", "--info=stats1", *arguments])


def upload(config, client, stage, loose_files, extras_dir):
    remote_host = config["sshHost"]
    root = config["remoteRoot"].rstrip("/")
    live = f"{root}/client/{config.get('channel', 'live')}"
    remote_live = f"{remote_host}:{live}/"

    run(["wsl", "ssh", remote_host, "mkdir", "-p", f"{live}/.bundles", f"{root}/addons", f"{root}/launcher-updates"])
    # Placeholders for files that are edited by hand on the server, so they are never overwritten.
    rsync("--ignore-existing", f"{wsl_path(stage / 'site')}/", f"{remote_host}:{root}/")

    list_file = stage / "loose-files.txt"
    list_file.write_text("\n".join(loose_files) + "\n", encoding="utf-8", newline="\n")
    rsync(f"--files-from={wsl_path(list_file)}", f"{wsl_path(client)}/", remote_live)
    if any(extras_dir.iterdir()):
        rsync(f"{wsl_path(extras_dir)}/", remote_live)
    rsync(f"{wsl_path(stage / '.bundles')}/", f"{remote_live}.bundles/")
    # rsync renames into place, so this switch is atomic. Old bundles go only after it.
    rsync(wsl_path(stage / "manifest.json"), f"{remote_live}manifest.json")
    rsync("--delete", f"{wsl_path(stage / '.bundles')}/", f"{remote_live}.bundles/")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(HERE / "whe-publish.json"))
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    parser.add_argument("--no-upload", action="store_true", help="build the manifest and bundles only")
    parser.add_argument("--version", default=time.strftime("%Y.%m.%d.%H%M"))
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    client = Path(config["client"])
    stage = Path(config["stage"])
    stage.mkdir(parents=True, exist_ok=True)

    selected = select_files(client, config)
    print(f"Selected {len(selected)} files. Hashing...")
    entries = hash_files(selected, stage / "hash-cache.json")
    folders = plan_bundles(entries, config)

    members_by_folder = {
        folder: [e for e in entries.values() if e["path"].lower().startswith(folder.lower() + "/")] for folder in folders
    }
    in_bundles = {e["path"] for members in members_by_folder.values() for e in members}
    loose = sorted(set(entries) - in_bundles)
    total = sum(e["size"] for e in entries.values())
    bundled = sum(entries[p]["size"] for p in in_bundles)
    largest = max((sum(e["size"] for e in m) for m in members_by_folder.values()), default=0)
    print(f"Total {total / 2**30:.1f} GiB in {len(entries)} files.")
    print(f"{len(folders)} bundles hold {len(in_bundles)} files ({bundled / 2**30:.1f} GiB, largest {largest / 2**20:.0f} MiB).")
    print(f"{len(loose)} loose files ({(total - bundled) / 2**30:.1f} GiB).")
    if args.dry_run:
        return

    bundle_dir = stage / ".bundles"
    bundle_dir.mkdir(exist_ok=True)
    index_path = stage / "bundle-index.json"
    known = load_json(index_path, {})
    print("Building bundles...")
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        built = list(pool.map(
            lambda folder: build_bundle(folder, members_by_folder[folder], selected, bundle_dir, known), folders))
    bundles = []
    keep = {}
    for folder, name, record in built:
        keep[name] = record
        bundles.append({
            "path": folder, "archive": f".bundles/{name}", "sha256": record["sha256"],
            "size": record["size"], "url": f".bundles/{name}",
        })
    for path in bundle_dir.iterdir():
        if path.name not in keep:
            path.unlink()
    write_json(index_path, keep)

    extras_dir = stage / "extras"
    shutil.rmtree(extras_dir, ignore_errors=True)
    extras_dir.mkdir()
    for relative, source in config.get("extras", {}).items():
        destination = extras_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    files = []
    for relative in sorted(entries):
        entry = dict(entries[relative])
        entry["url"] = urllib.parse.quote(relative)
        files.append(entry)
    write_json(stage / "manifest.json", {
        "schemaVersion": 2,
        "channel": config.get("channel", "live"),
        "version": args.version,
        "files": files,
        "bundles": bundles,
        "prune": config.get("prune", []),
    })

    site = stage / "site"
    (site / "addons").mkdir(parents=True, exist_ok=True)
    (site / "news.json").write_text('{ "schemaVersion": 1, "items": [] }\n', encoding="utf-8", newline="\n")
    (site / "patchnotes.md").write_text("<!-- Newest first. ## Title | YYYY-MM-DD | Tag -->\n", encoding="utf-8", newline="\n")
    (site / "addons" / "catalog.json").write_text('{ "schemaVersion": 1, "addons": [] }\n', encoding="utf-8", newline="\n")

    print(f"Manifest {args.version} written to {stage / 'manifest.json'}")
    if args.no_upload:
        return

    extra_names = set(config.get("extras", {}))
    upload(config, client, stage, [p for p in loose if p not in extra_names], extras_dir)
    print(f"Published {args.version}.")


if __name__ == "__main__":
    sys.exit(main())
