#!/usr/bin/env python3
"""Seal source files or create a private local checkpoint. Requires Docker Compose.

Does not deploy, commit, restore a database, or send files outside this machine.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
WORKSPACE = BACKEND.parent
REPOSITORIES = (BACKEND, WORKSPACE / "frontend")
MANIFEST = "release/source-manifest.json"


def run(args, **kwargs):
    return subprocess.run(args, check=True, cwd=BACKEND, **kwargs)


def source_files(repository):
    listed = subprocess.check_output(
        ["git", "-C", str(repository), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    ).decode().split("\0")
    files = sorted({name for name in listed if name and name != MANIFEST and (repository / name).is_file()})
    if any(Path(name).name == ".env" or (Path(name).name.startswith(".env.") and Path(name).name != ".env.example") for name in files):
        raise SystemExit("Refusing to archive a tracked environment file; remove it from Git first.")
    return files


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as content:
        for block in iter(lambda: content.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sources(repository):
    return {name: checksum(repository / name) for name in source_files(repository)}


def seal():
    for repository in REPOSITORIES:
        path = repository / MANIFEST
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"release": "2026-10-31-clear-basemap", "sha256": sources(repository)},
                                   indent=2, ensure_ascii=False) + "\n")
    print("Source manifests sealed for backend and frontend.")


def verify():
    for repository in REPOSITORIES:
        expected = json.loads((repository / MANIFEST).read_text())["sha256"]
        actual = sources(repository)
        changed = [name for name in set(expected) | set(actual) if expected.get(name) != actual.get(name)]
        if changed:
            raise SystemExit(f"{repository.name}: source changes: " + ", ".join(sorted(changed)))
    print("Both source manifests verified.")


def private_copy(source, target):
    if source.is_file():
        shutil.copyfile(source, target)
        target.chmod(0o600)


def checkpoint():
    verify()
    os.umask(0o077)
    directory = BACKEND / "storage/backups" / datetime.now(UTC).strftime("release-%Y%m%dT%H%M%SZ")
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    # Capture CMS prompts/rules from the running image, not fresh seed defaults.
    remote = f"/tmp/{directory.name}-configuration.json"
    compose = ["docker", "compose", "exec", "-T"]
    run([*compose, "api", "python", "-m", "app.scripts.release_state", "--output", remote])
    try:
        with (directory / "configuration.json").open("wb") as target:
            run([*compose, "api", "cat", remote], stdout=target)
        with (directory / "database.dump").open("wb") as target:
            run([*compose, "postgres", "sh", "-c",
                 'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --no-owner --no-privileges'], stdout=target)
        # /data includes uploads, reports, satellite evidence and regional snapshots.
        # Exclude backups to prevent recursive copies of historical dumps.
        with (directory / "storage.tar.gz").open("wb") as target:
            run([*compose, "api", "tar", "--exclude=./backups", "-C", "/data", "-czf", "-", "."], stdout=target)
        private_copy(BACKEND / ".env", directory / "backend.env")
        private_copy(WORKSPACE / "frontend/.env", directory / "frontend.env")
        with tarfile.open(directory / "sources.tar.gz", "w:gz") as archive:
            for repository in REPOSITORIES:
                for name in [*source_files(repository), MANIFEST]:
                    archive.add(repository / name, arcname=f"{repository.name}/{name}", recursive=False)
        # Preserve the tested artifacts so production can promote them without rebuilding.
        image_ids = []
        for service in ("api", "worker", "scheduler"):
            container = subprocess.check_output(["docker", "compose", "ps", "-q", service], cwd=BACKEND).decode().strip()
            image_ids.append(subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", container]).decode().strip())
        if len(set(image_ids)) != 1:
            raise SystemExit("API, worker and scheduler must run the same image before checkpointing.")
        (directory / "runtime.json").write_text(json.dumps({"backend_image_id": image_ids[0]}, indent=2) + "\n")
        with gzip.open(directory / "backend-image.tar.gz", "wb", compresslevel=3) as target:
            process = subprocess.Popen(["docker", "image", "save", image_ids[0]], stdout=subprocess.PIPE)
            try:
                shutil.copyfileobj(process.stdout, target, length=1024 * 1024)
                if process.wait() != 0:
                    raise RuntimeError("Could not export the tested image")
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        output = WORKSPACE / "frontend/.output"
        if not (output / "server/index.mjs").is_file():
            raise SystemExit("Build the frontend before checkpointing.")
        with tarfile.open(directory / "frontend-build.tar.gz", "w:gz") as archive:
            archive.add(output, arcname=".output")
        with (directory / "database-contents.txt").open("wb") as target:
            run([*compose, "api", "pg_restore", "--list"], input=(directory / "database.dump").read_bytes(), stdout=target)
        # Detect CMS edits made during capture; an incomplete checkpoint is never marked complete.
        run([*compose, "api", "python", "-m", "app.scripts.release_state", "--verify", remote])
        verify()
        manifest = {p.name: {"sha256": checksum(p), "bytes": p.stat().st_size}
                    for p in directory.iterdir() if p.is_file()}
        (directory / "CHECKSUMS.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (directory / "COMPLETE").write_text("Private checkpoint complete; database restore still requires verification.\n")
    finally:
        run([*compose, "api", "rm", "-f", remote])
    print(f"Private checkpoint created: {directory}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["seal", "verify", "checkpoint"])
    args = parser.parse_args()
    {"seal": seal, "verify": verify, "checkpoint": checkpoint}[args.action]()


if __name__ == "__main__":
    main()
