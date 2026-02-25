#!/usr/bin/env python3
"""
takeout_process.py (group-based rewrite)

Goal:
- Process media files using Takeout JSON sidecars where one JSON may apply to multiple media variants
  (e.g., original + -edited + different extensions in same folder).
- Avoid the old failure mode where JSON gets moved away after the first media is processed.

Core model:
1) Build a folder-local index of JSON sidecars.
2) For each media, find its "best" JSON sidecar using:
   - exact prefix match: "<media filename>*.json" in same folder
   - if not found, derivative fallback: strip "-edited" (and other suffixes) and try again
   - optional: stem-only match within same folder (disabled by default; risky for IMG_1234 vs IMG_12345)
3) Group by chosen JSON: json_path -> list[media files]
4) For each group:
   - parse JSON once
   - apply metadata to each media in the group
   - then move ALL media + related JSON(s) to archive together

Defaults:
- EXIF-first: if media already has a plausible in-file date, do NOT overwrite dates from JSON.
  (Use --force-json-dates to always write dates from JSON.)
- Still writes GPS/caption only if present in JSON; by default it WILL overwrite those tags too
  (keep simple; adjust if you want "only fill missing").

Windows note:
- Install exiftool and ensure it's in PATH. Example:
  winget install PhilHarvey.ExifTool
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff",
    ".mp4", ".mov", ".m4v", ".avi", ".3gp"
}

# Derivative suffixes that should inherit the base media's JSON
DERIVATIVE_SUFFIXES = ["-edited"]

# Date sanity bounds (you can widen if you truly have older media)
MIN_YEAR = 1980
MAX_YEAR = 2035

# Tags to check for existing in-file timestamps (EXIF/TIFF/QuickTime)
EXIF_TAGS_TO_CHECK = [
    "DateTimeOriginal",
    "CreateDate",
    "TIFF:DateTime",
    "MediaCreateDate",
    "TrackCreateDate",
]


@dataclass
class GroupTask:
    folder: Path
    json_path: Path
    media_paths: List[Path]
    related_jsons_to_move: List[Path]  # all jsons that should move with group


# ---------- exiftool helpers ----------

def check_exiftool() -> bool:
    try:
        p = subprocess.run(["exiftool", "-ver"], capture_output=True, text=True)
        return p.returncode == 0
    except FileNotFoundError:
        return False


def exiftool_read_tags(path: Path, tags: List[str]) -> Dict[str, str]:
    """
    Read selected tags using exiftool JSON output.
    """
    cmd = ["exiftool", "-j", "-n"]
    for t in tags:
        cmd.append(f"-{t}")
    cmd.append(str(path))

    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0 and not p.stdout.strip():
        return {}
    try:
        data = json.loads(p.stdout)
        if not data:
            return {}
        d0 = data[0]
        return {k: str(d0.get(k, "")) for k in tags if d0.get(k) not in (None, "")}
    except Exception:
        return {}


def extract_year(s: str) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\b(\d{4})\b", s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def has_valid_infile_date(media: Path) -> bool:
    tags = exiftool_read_tags(media, EXIF_TAGS_TO_CHECK)
    for t in EXIF_TAGS_TO_CHECK:
        y = extract_year(tags.get(t, ""))
        if y is None:
            continue
        if y in (0, 1970):
            continue
        if MIN_YEAR <= y <= MAX_YEAR:
            return True
    return False


# ---------- Takeout JSON parsing helpers ----------

def read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def get_timestamp(meta: dict) -> Optional[int]:
    # Common Takeout fields for photos/videos
    for k in ("photoTakenTime", "creationTime"):
        v = meta.get(k, {}) or {}
        ts = v.get("timestamp")
        if ts is None:
            continue
        try:
            t = int(ts)
            if 0 < t < 4102444800:  # up to year 2100
                return t
        except Exception:
            pass

    # Some exports also include ISO timestamps in mediaMetadata.creationTime
    mm = meta.get("mediaMetadata")
    if isinstance(mm, dict):
        iso = mm.get("creationTime")
        if isinstance(iso, str) and iso:
            y = extract_year(iso)
            if y and (MIN_YEAR <= y <= MAX_YEAR):
                # We only have year; don't invent full timestamp.
                # Return None here to avoid making up a date.
                return None

    return None


def get_gps(meta: dict):
    for k in ("geoDataExif", "geoData"):
        g = meta.get(k, {}) or {}
        lat = g.get("latitude")
        lon = g.get("longitude")
        alt = g.get("altitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            if abs(lat) > 1e-6 or abs(lon) > 1e-6:
                a = float(alt) if isinstance(alt, (int, float)) else None
                return float(lat), float(lon), a
    return None


def get_caption(meta: dict) -> Optional[str]:
    d = meta.get("description")
    if isinstance(d, str) and d.strip():
        return d.strip()
    return None


# ---------- Matching logic (one JSON can serve many media variants) ----------

def normalize_base_name(filename: str) -> str:
    """
    Remove derivative suffixes from stem: IMG_1234-edited.jpg -> IMG_1234.jpg
    Also handles "-edited (1)" pattern.
    """
    p = Path(filename)
    stem = p.stem
    for suf in DERIVATIVE_SUFFIXES:
        if stem.endswith(suf):
            stem = stem[: -len(suf)]
            break
        m = re.match(rf"^(.*){re.escape(suf)}\s*\(\d+\)$", stem)
        if m:
            stem = m.group(1)
            break
    return stem + p.suffix


def build_folder_json_index(folder: Path) -> Tuple[Dict[str, List[Path]], List[Path]]:
    """
    Returns:
    - json_by_prefix: maps a prefix string (e.g. "foo.jpg") to json files whose names start with that prefix
    - all_jsons: list of all *.json in folder
    """
    json_by_prefix: Dict[str, List[Path]] = {}
    all_jsons: List[Path] = []
    for p in folder.iterdir():
        if p.is_file() and p.suffix.lower() == ".json":
            all_jsons.append(p)
    # We'll match by "starts with <media filename>"
    # So index by the part before the first ".json" isn't helpful; just keep list and filter quickly.
    return json_by_prefix, all_jsons


def pick_best_json_for_media(media: Path, all_jsons: List[Path]) -> Tuple[Optional[Path], List[Path]]:
    """
    Pick a best JSON candidate for media in the same folder.
    Returns (best_json, related_jsons_for_move)

    related_jsons_for_move: all json files that clearly attach to this media filename OR its base name
    so they can be moved along together.
    """
    name = media.name
    base_name = normalize_base_name(name)

    # Candidates: any JSON whose filename starts with exact media filename (incl extension)
    exact = [j for j in all_jsons if j.name.startswith(name)]
    # Fallback: any JSON whose filename starts with base (un-edited) filename
    base = [j for j in all_jsons if j.name.startswith(base_name)] if base_name != name else []

    related = sorted(set(exact + base), key=lambda p: p.name)

    # Choose "best" with a stable preference order:
    # 1) exact match + ".supplemental-metadata.json"
    # 2) exact match + ".s.json" (video sidecars)
    # 3) exact match + anything .json
    # 4) base match + ".supplemental-metadata.json"
    # 5) base match + ".s.json"
    # 6) base match + anything .json
    def score(j: Path) -> int:
        n = j.name
        if n == f"{name}.supplemental-metadata.json":
            return 0
        if n == f"{name}.s.json":
            return 1
        if n.startswith(name):
            return 2
        if n == f"{base_name}.supplemental-metadata.json":
            return 3
        if n == f"{base_name}.s.json":
            return 4
        if n.startswith(base_name):
            return 5
        return 999

    candidates = sorted(related, key=score)
    best = candidates[0] if candidates else None
    return best, related


# ---------- Writing & moving ----------

def build_exiftool_cmd(media: Path, ts: int, gps, caption: Optional[str], write_dates: bool) -> List[str]:
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    dt = dt_utc.strftime("%Y:%m:%d %H:%M:%S")
    dtz = dt + "Z"
    ext = media.suffix.lower()

    cmd = ["exiftool", "-overwrite_original", "-P", "-m", "-api", "QuickTimeUTC=1"]

    if write_dates:
        cmd += [
            f"-DateTimeOriginal={dt}",
            f"-CreateDate={dt}",
            f"-ModifyDate={dt}",
            "-OffsetTimeOriginal=+00:00",
            "-OffsetTime=+00:00",
            "-OffsetTimeDigitized=+00:00",
        ]
        if ext in {".mp4", ".mov", ".m4v", ".avi", ".3gp"}:
            cmd += [
                f"-QuickTime:CreateDate={dtz}",
                f"-QuickTime:ModifyDate={dtz}",
                f"-QuickTime:TrackCreateDate={dtz}",
                f"-QuickTime:TrackModifyDate={dtz}",
                f"-QuickTime:MediaCreateDate={dtz}",
                f"-QuickTime:MediaModifyDate={dtz}",
                f"-XMP:CreateDate={dtz}",
                f"-XMP:ModifyDate={dtz}",
            ]

    if gps:
        lat, lon, alt = gps
        cmd += [f"-GPSLatitude={lat}", f"-GPSLongitude={lon}"]
        if alt is not None:
            cmd += [f"-GPSAltitude={alt}"]

    if caption:
        cmd += [
            f"-Description={caption}",
            f"-ImageDescription={caption}",
            f"-Caption-Abstract={caption}",
        ]

    cmd.append(str(media))
    return cmd


def run_exiftool_cmd(cmd: List[str], dry_run: bool) -> Tuple[bool, str]:
    if dry_run:
        return True, "DRYRUN " + " ".join(cmd)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        return True, (p.stdout.strip() or "ok")
    return False, (p.stderr.strip() or p.stdout.strip() or f"ExifTool failed ({p.returncode})")


def safe_move(src: Path, dst: Path, dry_run: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        return
    if dst.exists():
        # avoid overwrite
        stem, suf = dst.stem, dst.suffix
        parent = dst.parent
        i = 1
        while True:
            cand = parent / f"{stem}__DUP{i}{suf}"
            if not cand.exists():
                dst = cand
                break
            i += 1
    shutil.move(str(src), str(dst))


def move_group_files(group: GroupTask, root: Path, archive_root: Path, dry_run: bool) -> str:
    # Move all media in group + all related jsons (dedup), preserving rel paths.
    moved = []
    all_to_move = list(dict.fromkeys(group.media_paths + group.related_jsons_to_move))
    for p in all_to_move:
        rel = p.relative_to(root)
        dest = archive_root / rel
        if dry_run:
            moved.append(f"{rel}")
        else:
            safe_move(p, dest, dry_run=False)
            moved.append(f"{rel}")
    return f"MOVED {len(all_to_move)} files"


def process_group(group: GroupTask, root: Path, archive_root: Path, dry_run: bool, force_json_dates: bool) -> Tuple[bool, str]:
    meta = read_json(group.json_path)
    if not meta:
        return False, f"SKIP bad-json {group.json_path}"

    ts = get_timestamp(meta)
    if ts is None:
        return False, f"SKIP no-timestamp {group.json_path}"

    gps = get_gps(meta)
    caption = get_caption(meta)

    # Apply to each media
    for m in group.media_paths:
        write_dates = force_json_dates or (not has_valid_infile_date(m))
        cmd = build_exiftool_cmd(m, ts, gps, caption, write_dates=write_dates)
        ok, msg = run_exiftool_cmd(cmd, dry_run=dry_run)
        if not ok:
            return False, f"FAIL exif {m}: {msg}"

    moved_msg = move_group_files(group, root, archive_root, dry_run=dry_run)
    return True, f"OK group json={group.json_path.name} media={len(group.media_paths)} {moved_msg}"


# ---------- Main walk & grouping ----------

def iter_folders(root: Path, archive_root: Path):
    archive_root_resolved = archive_root.resolve()
    for dirpath, _, _ in os.walk(root):
        d = Path(dirpath)
        try:
            dr = d.resolve()
            if dr == archive_root_resolved or archive_root_resolved in dr.parents:
                continue
        except Exception:
            pass
        yield d


def list_media_in_folder(folder: Path) -> List[Path]:
    out = []
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() in MEDIA_EXTS and not p.name.lower().endswith(".json"):
            out.append(p)
    return out


def build_tasks(root: Path, archive_root: Path) -> List[GroupTask]:
    tasks: Dict[Path, GroupTask] = {}
    # key tasks by json_path

    for folder in iter_folders(root, archive_root):
        _, all_jsons = build_folder_json_index(folder)
        if not all_jsons:
            continue

        medias = list_media_in_folder(folder)
        if not medias:
            continue

        # For each media, choose best json; group by json_path
        for m in medias:
            best, related_jsons = pick_best_json_for_media(m, all_jsons)
            if not best:
                continue  # no json for this media; skip (valid bank should minimize this)

            if best not in tasks:
                tasks[best] = GroupTask(
                    folder=folder,
                    json_path=best,
                    media_paths=[],
                    related_jsons_to_move=[],
                )

            tasks[best].media_paths.append(m)

            # Collect JSONs to move with this group (dedup later)
            tasks[best].related_jsons_to_move.extend(related_jsons)

    # Dedup per task
    out_tasks: List[GroupTask] = []
    for t in tasks.values():
        t.media_paths = sorted(set(t.media_paths), key=lambda p: p.name)
        t.related_jsons_to_move = sorted(set(t.related_jsons_to_move), key=lambda p: p.name)
        out_tasks.append(t)
    return out_tasks


def main():
    ap = argparse.ArgumentParser(
        description="Embed Takeout sidecar JSON metadata into media; supports one JSON applying to multiple media variants."
    )
    ap.add_argument("path", help="Path to process (folder), ideally your 'valid bank' root")
    ap.add_argument("--archive-name", default="_processed_archive", help="Archive folder name inside input path")
    ap.add_argument("--workers", type=int, default=4, help="Parallel workers (default 4)")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without modifying files")
    ap.add_argument("--log", default="takeout_process.log", help="Log file name created in input path")
    ap.add_argument("--force-json-dates", action="store_true",
                    help="Overwrite dates from JSON even if media already has a valid in-file date")
    args = ap.parse_args()

    root = Path(args.path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Path is not a directory: {root}")

    if not check_exiftool():
        raise SystemExit("exiftool not found. Install ExifTool and ensure it's in PATH (Windows: winget install PhilHarvey.ExifTool)")

    archive_root = root / args.archive_name
    if not args.dry_run:
        archive_root.mkdir(parents=True, exist_ok=True)

    tasks = build_tasks(root, archive_root)
    log_path = root / args.log

    ok = 0
    fail = 0
    skipped_no_json = 0  # implicit skips (media with no json mapping)

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"root={root}\narchive={archive_root}\nworkers={args.workers}\ndry_run={args.dry_run}\n")
        log.write(f"force_json_dates={args.force_json_dates}\n")
        log.write(f"groups={len(tasks)}\n\n")

        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [
                ex.submit(process_group, t, root, archive_root, args.dry_run, args.force_json_dates)
                for t in tasks
            ]
            for fut in cf.as_completed(futures):
                success, msg = fut.result()
                if success:
                    ok += 1
                else:
                    fail += 1
                log.write(msg + "\n")

        log.write(f"\nSUMMARY ok_groups={ok} fail_groups={fail}\n")

    print(f"Done. ok_groups={ok} fail_groups={fail} log={log_path}")
    print(f"Archive: {archive_root}")


if __name__ == "__main__":
    main()