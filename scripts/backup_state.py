"""Create and restore local LinkBound state snapshots."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath

MANIFEST = "manifest.json"
DATABASE = "database.sqlite"
FORMAT_VERSION = 1


def _private(path: Path, *, directory: bool = False) -> None:
    """Best-effort owner-only permissions on platforms that support chmod."""
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        pass


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(raw: str) -> PurePosixPath:
    rel = PurePosixPath(raw)
    if (not raw or rel.is_absolute() or "\\" in raw or
            any(part in ("", ".", "..") for part in rel.parts) or
            (rel.parts and ":" in rel.parts[0])):
        raise ValueError(f"Unsafe path in manifest: {raw!r}")
    return rel


def _scan_files(root: Path) -> list[str]:
    result: list[str] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in list(dirs):
            item = base / name
            if item.is_symlink():
                raise ValueError(f"Symlink is not allowed in snapshot: {item}")
        for name in files:
            item = base / name
            if not _regular_file(item):
                raise ValueError(f"Non-regular file is not allowed: {item}")
            result.append(item.relative_to(root).as_posix())
    return sorted(result)


def _write_manifest(root: Path) -> None:
    entries = [{"path": rel, "sha256": _hash(root / Path(*PurePosixPath(rel).parts))}
               for rel in _scan_files(root)]
    manifest = {"format_version": FORMAT_VERSION, "files": entries}
    path = root / MANIFEST
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _private(path)


def verify_snapshot(snapshot: Path) -> None:
    if snapshot.is_symlink():
        raise ValueError("Snapshot must not be a symlink")
    snapshot = snapshot.resolve()
    if not snapshot.is_dir():
        raise ValueError("Snapshot must be a directory")
    manifest_path = snapshot / MANIFEST
    if not _regular_file(manifest_path):
        raise ValueError("Snapshot manifest is missing or not a regular file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Snapshot manifest is invalid JSON") from exc
    if (not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION or
            not isinstance(manifest.get("files"), list)):
        raise ValueError("Unsupported or invalid snapshot manifest")

    expected: dict[str, str] = {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ValueError("Invalid file entry in snapshot manifest")
        rel = _relative_path(entry["path"]).as_posix()
        digest = entry.get("sha256")
        if (rel == MANIFEST or rel in expected or not isinstance(digest, str) or len(digest) != 64 or
                any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError(f"Invalid or duplicate manifest entry: {rel}")
        expected[rel] = digest

    actual = set(_scan_files(snapshot)) - {MANIFEST}
    if actual != set(expected):
        missing, extra = sorted(set(expected) - actual), sorted(actual - set(expected))
        raise ValueError(f"Snapshot file set differs from manifest (missing={missing}, extra={extra})")
    for rel, digest in expected.items():
        file_path = snapshot.joinpath(*PurePosixPath(rel).parts)
        # Resolve parents and require the path to remain under this snapshot.
        if not file_path.resolve().is_relative_to(snapshot) or not _regular_file(file_path):
            raise ValueError(f"Unsafe or missing snapshot file: {rel}")
        if _hash(file_path) != digest:
            raise ValueError(f"SHA256 mismatch: {rel}")
    if DATABASE not in expected:
        raise ValueError("Snapshot does not contain the database")
    _check_database(snapshot / DATABASE)


def _check_database(path: Path) -> None:
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Snapshot database failed SQLite integrity_check")


def snapshot_state(
    database: Path,
    output: Path,
    attachments: Path | None = None,
    screenshots: Path | None = None,
    uploads: Path | None = None,
    inbound_files: Path | None = None,
) -> None:
    if database.is_symlink():
        raise ValueError(f"Database must not be a symlink: {database}")
    database = database.resolve()
    if not _regular_file(database):
        raise ValueError(f"Database is missing or not a regular file: {database}")
    output = output.resolve()
    if output.exists():
        raise FileExistsError(f"Snapshot destination already exists: {output}")
    file_dirs = {
        name: path for name, path in {
            "attachments": attachments,
            "screenshots": screenshots,
            "uploads": uploads,
            "inbound_files": inbound_files,
        }.items() if path is not None
    }
    for name, path in file_dirs.items():
        if path.is_symlink():
            raise ValueError(f"{name} must not be a symlink: {path}")
        resolved = path.resolve()
        if not resolved.is_dir():
            raise ValueError(f"{name} must be an existing directory: {path}")
        if resolved == database.parent or database.is_relative_to(resolved):
            raise ValueError(f"Database must not be inside {name}")
        if output == resolved or output.is_relative_to(resolved):
            raise ValueError(f"Snapshot destination must be outside {name}")
        file_dirs[name] = resolved

    output.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        db_copy = temp / DATABASE
        with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(db_copy)) as dest:
                source.backup(dest)
        _check_database(db_copy)
        _private(db_copy)
        for name, source_dir in file_dirs.items():
            target_dir = temp / name
            target_dir.mkdir()
            _private(target_dir, directory=True)
            for rel in _scan_files(source_dir):
                src = source_dir.joinpath(*PurePosixPath(rel).parts)
                dst = target_dir.joinpath(*PurePosixPath(rel).parts)
                dst.parent.mkdir(parents=True, exist_ok=True)
                _private(dst.parent, directory=True)
                shutil.copyfile(src, dst)
                _private(dst)
        _write_manifest(temp)
        temp.rename(output)
        _private(output, directory=True)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def restore_snapshot(snapshot: Path, target: Path) -> None:
    verify_snapshot(snapshot)
    target = target.resolve()
    if target == snapshot.resolve() or target.is_relative_to(snapshot.resolve()):
        raise ValueError("Restore target must be outside the snapshot")
    if target.exists():
        raise FileExistsError(f"Restore target must not already exist: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(mode=0o700)
    _private(target, directory=True)
    try:
        for rel in _scan_files(snapshot):
            if rel == MANIFEST:
                continue
            src = snapshot.joinpath(*PurePosixPath(rel).parts)
            dst = target.joinpath(*PurePosixPath(rel).parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            _private(dst.parent, directory=True)
            shutil.copyfile(src, dst)
            _private(dst)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Snapshot, verify, or restore LinkBound local state")
    commands = parser.add_subparsers(dest="command", required=True)
    snap = commands.add_parser("snapshot", help="Create an SQLite-consistent local snapshot")
    snap.add_argument("--database", required=True, type=Path)
    snap.add_argument("--output", required=True, type=Path)
    snap.add_argument("--attachments", type=Path, help="Optional attachments directory")
    snap.add_argument("--screenshots", type=Path, help="Optional screenshots directory")
    snap.add_argument("--uploads", type=Path, help="Optional uploads directory")
    snap.add_argument("--inbound-files", type=Path, help="Optional saved inbox files directory")
    verify = commands.add_parser("verify", help="Verify snapshot file set and SHA256 manifest")
    verify.add_argument("snapshot", type=Path)
    restore = commands.add_parser("restore", help="Restore to a new, nonexistent target directory")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("target", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            snapshot_state(args.database, args.output, args.attachments, args.screenshots,
                           args.uploads, args.inbound_files)
            print(f"Snapshot created: {args.output}")
        elif args.command == "verify":
            verify_snapshot(args.snapshot)
            print(f"Snapshot verified: {args.snapshot}")
        else:
            restore_snapshot(args.snapshot, args.target)
            print(f"Snapshot restored: {args.target}")
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"backup_state: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
