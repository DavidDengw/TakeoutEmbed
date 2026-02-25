#!/usr/bin/env python3
"""
move_valid_by_date_sources.py

Definition of VALID (your process):
A media file is VALID if it has a plausible date from either:
  A) in-file metadata (EXIF/TIFF/QuickTime tags via exiftool), OR
  B) a matching JSON sidecar that contains a plausible timestamp.

Otherwise it's INVALID.

Diagnostic intent:
- Move VALID media + its derivatives/related JSONs into a 'valid' folder (preserve structure).
- Leave INVALID behind and produce a CSV explaining why.

Matching rules (folder-local, lenient):
- For a given media file 'name.ext', consider JSON candidates in SAME FOLDER that:
    - start with 'name.ext' (e.g., name.ext.supplemental-metadata.json, name.ext.s.json, name.ext.anything.json)
  PLUS for edited derivatives:
    - if media ends with '-edited', also allow base name without '-edited' for JSON lookup.

No cross-folder/global matching is performed.

Requires: exiftool in PATH.
"""

from __future__ import annotations
import argparse, csv, json, os, re, shutil, subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

MEDIA_EXTS_DEFAULT = {
    ".jpg", ".jpeg", ".png", ".heic",
    ".mp4", ".mov", ".m4v", ".avi", ".3gp", ".gif"
}

# Your year window
MIN_YEAR = 2000
MAX_YEAR = 2025

# Tags to check for in-file dates
EXIF_TAGS_TO_CHECK = [
    "DateTimeOriginal",
    "CreateDate",
    "TIFF:DateTime",
    "MediaCreateDate",
    "TrackCreateDate",
]

DERIVATIVE_SUFFIXES = ["-edited"]
BOUNDARY_CHARS = set(".-_ (")  # used for safe prefix-ish matching of base stems


@dataclass
class ValidDecision:
    is_valid: bool
    source: str                 # "exif" | "json" | ""
    reason: str                 # why invalid or notes
    exif_year: Optional[int] = None
    json_year: Optional[int] = None
    chosen_json: Optional[str] = None


def run_exiftool_json(path: Path, tags: List[str]) -> Dict[str, str]:
    cmd = ["exiftool", "-j", "-n"]
    for t in tags:
        cmd.append(f"-{t}")
    cmd.append(str(path))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise SystemExit("ExifTool not found on PATH. Install ExifTool and ensure 'exiftool' is available.")

    if proc.returncode != 0 and not proc.stdout.strip():
        return {}
    try:
        data = json.loads(proc.stdout)
        if not data:
            return {}
        return {k: str(v) for k, v in data[0].items() if k in tags}
    except json.JSONDecodeError:
        return {}


def extract_year(value: str) -> Optional[int]:
    if not value:
        return None
    m = re.search(r"\b(\d{4})\b", value)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def year_in_range(y: Optional[int]) -> bool:
    return y is not None and MIN_YEAR <= y <= MAX_YEAR


def normalize_base_key(stem: str) -> str:
    s = stem
    for suf in DERIVATIVE_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
        m = re.match(rf"^(.*){re.escape(suf)}\s*\(\d+\)$", s)
        if m:
            s = m.group(1)
            break
    return s


def stem_boundary_match(filename: str, base_key: str) -> bool:
    if not filename.startswith(base_key):
        return False
    if len(filename) == len(base_key):
        return True
    return filename[len(base_key)] in BOUNDARY_CHARS


def json_candidates_for_media(folder_files: List[Path], media_name: str) -> List[Path]:
    """
    Folder-local, lenient:
    - any *.json whose name starts with the full media filename (incl extension).
      e.g. IMG.mp4.s.json starts with IMG.mp4
    """
    prefix = media_name
    return [p for p in folder_files if p.suffix.lower() == ".json" and p.name.startswith(prefix)]


def json_candidates_with_edited_fallback(folder_files: List[Path], media: Path) -> List[Path]:
    """
    If media is *-edited.ext, also consider JSON candidates for base (without -edited).
    """
    cands = json_candidates_for_media(folder_files, media.name)
    if cands:
        return cands

    base_key = normalize_base_key(media.stem)
    if base_key != media.stem:
        base_name = base_key + media.suffix  # e.g. IMG_1234.jpg
        cands2 = json_candidates_for_media(folder_files, base_name)
        return cands2

    return []


def parse_takeout_json_timestamp_year(json_path: Path) -> Optional[int]:
    """
    Try common Google Takeout schemas.
    We only need a YEAR for validity testing.

    Common patterns:
    - photoTakenTime.timestamp (string epoch seconds)
    - creationTime.timestamp (epoch seconds) for videos in some exports
    - mediaMetadata.creationTime (ISO8601)
    """
    try:
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    # Helper to extract epoch seconds
    def epoch_to_year(ts: str) -> Optional[int]:
        try:
            # epoch seconds
            sec = int(ts)
        except Exception:
            return None
        # convert to year without importing heavy libs: use datetime
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(sec, tz=timezone.utc).year
        except Exception:
            return None

    # 1) photoTakenTime.timestamp
    pt = data.get("photoTakenTime")
    if isinstance(pt, dict):
        ts = pt.get("timestamp")
        if ts:
            y = epoch_to_year(str(ts))
            if y:
                return y

    # 2) creationTime.timestamp
    ct = data.get("creationTime")
    if isinstance(ct, dict):
        ts = ct.get("timestamp")
        if ts:
            y = epoch_to_year(str(ts))
            if y:
                return y

    # 3) mediaMetadata.creationTime (ISO string)
    mm = data.get("mediaMetadata")
    if isinstance(mm, dict):
        iso = mm.get("creationTime")
        if isinstance(iso, str) and iso:
            y = extract_year(iso)
            if y:
                return y

    # 4) fallback: search common keys shallowly for "timestamp"
    # (keep it conservative; we only want year)
    for k, v in data.items():
        if k.lower().endswith("time") and isinstance(v, dict) and "timestamp" in v:
            y = epoch_to_year(str(v.get("timestamp")))
            if y:
                return y

    return None


def decide_validity(media: Path, folder_files: List[Path]) -> ValidDecision:
    # A) EXIF/TIFF/QuickTime date check
    tags = run_exiftool_json(media, EXIF_TAGS_TO_CHECK)
    exif_years = [extract_year(tags.get(t, "")) for t in EXIF_TAGS_TO_CHECK]
    exif_years = [y for y in exif_years if y is not None]

    if exif_years:
        # if any in range => valid
        for y in exif_years:
            if year_in_range(y):
                return ValidDecision(True, "exif", "valid_exif_in_range", exif_year=y)
        # has exif dates but out of range
        return ValidDecision(False, "", "exif_date_out_of_range", exif_year=exif_years[0])

    # B) JSON fallback (folder-local)
    cands = json_candidates_with_edited_fallback(folder_files, media)
    if not cands:
        return ValidDecision(False, "", "no_exif_date_and_no_json_candidate")

    # Try candidates; accept first that yields in-range year
    best_year = None
    for jp in sorted(cands, key=lambda p: p.name):
        y = parse_takeout_json_timestamp_year(jp)
        if y is None:
            continue
        best_year = y
        if year_in_range(y):
            return ValidDecision(True, "json", "valid_json_in_range", json_year=y, chosen_json=jp.name)

    if best_year is None:
        return ValidDecision(False, "", "json_present_but_no_timestamp", chosen_json=";".join(p.name for p in cands))
    return ValidDecision(False, "", "json_timestamp_out_of_range", json_year=best_year, chosen_json=";".join(p.name for p in cands))


def collect_attachment_set(folder_files: List[Path], media: Path) -> Set[Path]:
    """
    If media is deemed valid, move:
    - the media itself
    - any file starting with full media filename (incl extension)
    - any file whose name matches the base key boundary (captures -edited variants and their json)
    """
    base_key = normalize_base_key(media.stem)
    full_prefix = media.name

    attach: Set[Path] = set()
    for p in folder_files:
        if p.name.startswith(full_prefix):
            attach.add(p)
            continue
        if stem_boundary_match(p.name, base_key):
            attach.add(p)
            continue
    # Always include the media itself
    attach.add(media)
    return attach


def safe_move(src: Path, dst: Path, dry_run: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        print(f"[DRY] MOVE: {src} -> {dst}")
        return
    final_dst = dst
    if final_dst.exists():
        stem = final_dst.stem
        suf = final_dst.suffix
        parent = final_dst.parent
        i = 1
        while True:
            cand = parent / f"{stem}__DUP{i}{suf}"
            if not cand.exists():
                final_dst = cand
                break
            i += 1
    shutil.move(str(src), str(final_dst))


def write_csv(path: Path, header: List[str], rows: List[List[str]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help=r"Root folder (e.g. F:\takeout\...\Takeout\Google Photos)")
    ap.add_argument("--target", default="valid", help="Folder name or absolute path for moved valid files")
    ap.add_argument("--report", default="invalid_report.csv", help="CSV report filename (written under root)")
    ap.add_argument("--media-exts", default=",".join(sorted(MEDIA_EXTS_DEFAULT)))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        raise SystemExit(f"Root path does not exist: {root}")

    media_exts: Set[str] = {e.strip().lower() for e in args.media_exts.split(",") if e.strip()}
    target_root = Path(args.target).resolve() if Path(args.target).is_absolute() else (root / args.target).resolve()

    invalid_rows: List[List[str]] = []
    valid_count = 0
    invalid_count = 0
    moved_files = 0

    # Walk folders; folder-local decisions
    for dirpath, _, filenames in os.walk(root):
        d = Path(dirpath)
        rel_dir = d.relative_to(root)

        # skip target folder
        if rel_dir.parts and (root / rel_dir.parts[0]).resolve() == (target_root / target_root.parts[-1]).resolve():
            # simple skip; ok if target is root/valid
            pass

        folder_files = [d / fn for fn in filenames]
        media_files = [p for p in folder_files if p.suffix.lower() in media_exts]

        moved_in_folder: Set[Path] = set()

        for mf in media_files:
            if mf in moved_in_folder:
                continue

            decision = decide_validity(mf, folder_files)
            if decision.is_valid:
                valid_count += 1
                attach = collect_attachment_set(folder_files, mf)
                for src in attach:
                    if src in moved_in_folder:
                        continue
                    dst = target_root / src.relative_to(root)
                    safe_move(src, dst, args.dry_run)
                    moved_in_folder.add(src)
                    moved_files += 1
            else:
                invalid_count += 1
                invalid_rows.append([
                    str(mf),
                    mf.name,
                    decision.reason,
                    str(decision.exif_year) if decision.exif_year is not None else "",
                    str(decision.json_year) if decision.json_year is not None else "",
                    decision.chosen_json or ""
                ])

    report_path = root / args.report
    write_csv(
        report_path,
        ["media_path", "media_filename", "invalid_reason", "exif_year", "json_year", "json_candidates_or_chosen"],
        invalid_rows
    )

    print("\nDone.")
    print(f"Valid media count (by date sources): {valid_count}")
    print(f"Invalid media count:                {invalid_count}")
    print(f"Files moved into valid/:            {moved_files}")
    print(f"Invalid report written to:          {report_path}")
    print("\nNow open invalid_report.csv and sort by invalid_reason to see the real causes.")


if __name__ == "__main__":
    main()