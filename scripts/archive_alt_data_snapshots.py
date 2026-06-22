#!/usr/bin/env python3
"""Archive alternative-data snapshots into date-stamped PIT evidence folders.

This is an offline archival layer for provider outputs that are snapshots or
rolling windows. It does not fetch data. It copies existing snapshot files into
`data/alt_archives/<archive_date>/<source>/`, records SHA256 hashes and links
the manifest to the previous archive manifest hash.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

from _paths import (
    ALT_ARCHIVES_DIR,
    ANALYST_EXPECTATIONS_DIR,
    FLOW_SIGNALS_DIR,
    HEDGE_ASSETS_DIR,
    INTRADAY_DIR,
)

ARCHIVABLE_SUFFIXES = {".csv", ".json", ".parquet"}
SOURCE_DIRS = {
    "analyst_expectations": ANALYST_EXPECTATIONS_DIR,
    "flow_signals": FLOW_SIGNALS_DIR,
    "hedge_assets": HEDGE_ASSETS_DIR,
    "intraday": INTRADAY_DIR,
}
DEFAULT_SOURCES = ",".join(sorted(SOURCE_DIRS))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-date",
        default=os.getenv("QUANT_ALT_ARCHIVE_DATE", date.today().strftime("%Y%m%d")),
        help="Archive date in YYYYMMDD format.",
    )
    parser.add_argument(
        "--sources",
        default=os.getenv("QUANT_ALT_ARCHIVE_SOURCES", DEFAULT_SOURCES),
        help=f"Comma-separated sources. Available: {','.join(sorted(SOURCE_DIRS))}",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=os.getenv("QUANT_ALT_ARCHIVE_OVERWRITE", "0") == "1",
        help="Allow replacing archived files when content differs.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        default=os.getenv("QUANT_ALT_ARCHIVE_VERIFY", "0") == "1",
        help="Verify an existing archive manifest instead of copying files.",
    )
    return parser.parse_args()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_text_atomically(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _copy_atomically(src: Path, dest: Path, *, overwrite: bool) -> str:
    src_hash = _sha256_file(src)
    if dest.exists():
        dest_hash = _sha256_file(dest)
        if dest_hash == src_hash:
            return "unchanged"
        if not overwrite:
            raise RuntimeError(
                f"archive target already exists with different hash: {dest}; "
                "set --overwrite only for an audited repair"
            )
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.with_name(f"{dest.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(src, tmp_path)
        tmp_path.replace(dest)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return "archived"


def _iter_source_files(source_dir: Path) -> list[Path]:
    if not source_dir.exists():
        return []
    return sorted(
        path
        for path in source_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in ARCHIVABLE_SUFFIXES
    )


def _planned_file_hashes(requested: list[str]) -> dict[tuple[str, str], str]:
    planned: dict[tuple[str, str], str] = {}
    for source in requested:
        source_dir = SOURCE_DIRS[source]
        for src in _iter_source_files(source_dir):
            relative = src.relative_to(source_dir).as_posix()
            planned[(source, relative)] = _sha256_file(src)
    return planned


def _manifest_file_hashes(manifest: dict[str, Any]) -> dict[tuple[str, str], str]:
    hashes: dict[tuple[str, str], str] = {}
    for row in manifest.get("files", []):
        hashes[(str(row["source"]), str(row["relative_path"]))] = str(row["sha256"])
    return hashes


def _manifest_hash_from_sidecar(manifest_path: Path) -> str:
    hash_path = manifest_path.with_suffix(".sha256")
    if hash_path.exists():
        return hash_path.read_text(encoding="utf-8").strip().split()[0]
    return _sha256_file(manifest_path)


def _previous_manifest_hash(archive_date: str) -> str | None:
    if not ALT_ARCHIVES_DIR.exists():
        return None
    candidates = []
    for manifest in ALT_ARCHIVES_DIR.glob("*/manifest.json"):
        parent = manifest.parent.name
        if parent < archive_date:
            candidates.append(manifest)
    if not candidates:
        return None
    latest = sorted(candidates, key=lambda path: path.parent.name)[-1]
    hash_path = latest.with_suffix(".sha256")
    if hash_path.exists():
        return hash_path.read_text(encoding="utf-8").strip().split()[0]
    return _sha256_file(latest)


def _archive_source(
    source: str,
    source_dir: Path,
    archive_root: Path,
    *,
    overwrite: bool,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for src in _iter_source_files(source_dir):
        relative = src.relative_to(source_dir)
        dest = archive_root / source / relative
        status = _copy_atomically(src, dest, overwrite=overwrite)
        rows.append(
            {
                "source": source,
                "relative_path": str(relative),
                "source_path": str(src),
                "archive_path": str(dest),
                "status": status,
                "size_bytes": int(src.stat().st_size),
                "sha256": _sha256_file(dest),
                "source_mtime_ns": int(src.stat().st_mtime_ns),
            }
        )
    return rows


def _verify_archive(archive_date: str) -> None:
    archive_root = ALT_ARCHIVES_DIR / archive_date
    manifest_path = archive_root / "manifest.json"
    hash_path = archive_root / "manifest.sha256"
    if not manifest_path.exists() or not hash_path.exists():
        raise RuntimeError(f"archive manifest is incomplete: {archive_root}")
    expected_manifest_hash = hash_path.read_text(encoding="utf-8").strip().split()[0]
    actual_manifest_hash = _sha256_file(manifest_path)
    if actual_manifest_hash != expected_manifest_hash:
        raise RuntimeError(
            f"manifest hash mismatch: expected={expected_manifest_hash} actual={actual_manifest_hash}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    failures: list[str] = []
    for row in manifest.get("files", []):
        path = Path(str(row["archive_path"]))
        expected = str(row["sha256"])
        if not path.exists():
            failures.append(f"missing: {path}")
            continue
        actual = _sha256_file(path)
        if actual != expected:
            failures.append(f"hash mismatch: {path} expected={expected} actual={actual}")
    if failures:
        raise RuntimeError("; ".join(failures))
    print(
        f"Verified archive {archive_date}: files={manifest.get('file_count')} "
        f"manifest_sha256={actual_manifest_hash}",
        flush=True,
    )


def main() -> None:
    args = _parse_args()
    archive_date = str(args.archive_date)
    try:
        datetime.strptime(archive_date, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("--archive-date must be YYYYMMDD") from exc
    if args.verify:
        _verify_archive(archive_date)
        return

    requested = [item.strip() for item in str(args.sources).split(",") if item.strip()]
    unknown = sorted(set(requested) - set(SOURCE_DIRS))
    if unknown:
        raise ValueError(f"unknown archive sources: {unknown}; choose from {sorted(SOURCE_DIRS)}")

    archive_root = ALT_ARCHIVES_DIR / archive_date
    manifest_path = archive_root / "manifest.json"
    existing_manifest: dict[str, Any] | None = None
    existing_manifest_hash: str | None = None
    if manifest_path.exists():
        existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        existing_manifest_hash = _manifest_hash_from_sidecar(manifest_path)
        if not args.overwrite:
            planned_hashes = _planned_file_hashes(requested)
            existing_hashes = _manifest_file_hashes(existing_manifest)
            if planned_hashes == existing_hashes:
                print(
                    f"Archive {archive_date} already exists with matching manifest "
                    f"manifest_sha256={existing_manifest_hash}",
                    flush=True,
                )
                return
            raise RuntimeError(
                f"archive manifest already exists with different planned content: {manifest_path}; "
                "use --overwrite only for an audited local repair"
            )

    files: list[dict[str, Any]] = []
    for source in requested:
        files.extend(
            _archive_source(
                source,
                SOURCE_DIRS[source],
                archive_root,
                overwrite=bool(args.overwrite),
            )
        )

    manifest = {
        "timestamp": datetime.now().isoformat(),
        "archive_date": archive_date,
        "research_only": True,
        "sources": requested,
        "previous_manifest_hash": _previous_manifest_hash(archive_date),
        "file_count": len(files),
        "files": files,
        "production_warning": (
            "This local archive is PIT evidence for research. Production still requires "
            "approved external WORM retention and provider entitlements."
        ),
    }
    if existing_manifest is not None and existing_manifest_hash is not None:
        manifest["replaces_manifest_sha256"] = existing_manifest_hash
    _write_text_atomically(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True, default=str),
    )
    manifest_hash = _sha256_file(manifest_path)
    _write_text_atomically(
        archive_root / "manifest.sha256",
        f"{manifest_hash}  manifest.json\n",
    )
    print(
        f"Archived {len(files)} files for {archive_date} "
        f"sources={','.join(requested)} manifest_sha256={manifest_hash}",
        flush=True,
    )


if __name__ == "__main__":
    main()
