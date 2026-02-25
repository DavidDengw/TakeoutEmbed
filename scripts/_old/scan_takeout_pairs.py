#!/usr/bin/env python3
"""
scan_takeout_pairs.py

Scan a Google Takeout "Google Photos" directory tree and determine:
- Exact media <-> JSON sidecar pairs (strict)
- Media files missing a matching sidecar
- JSON sidecars with no matching media (orphans)
- JSON files that are not sidecars (other_json)
- Ambiguous cases (multiple sidecars for the same media key)

Default strict sidecar rule (exact):
  <media filename>.supplemental-metadata.json

Example:
  IMG20141230211557.jpg  <->  IMG20141230211557.jpg.supplemental-metadata.json

Outputs:
- Console summary
- CSV reports in output directory
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from collections import defaultdict, Counter

MEDIA_EXTS_DEFAULT = {".jpg", ".jpeg", ".png", ".heic", ".mp4", ".mov", ".m4v", ".avi", ".gif"}


def is_sidecar_name(name: str) -> bool:
    return name.endswith(".supplemental-metadata.json")


def sidecar_to_media_name(sidecar_name: str) -> str:
    # "X.jpg.supplemental-metadata.json" -> "X.jpg"
    return sidecar_name[: -len(".supplemental-metadata.json")]


def walk_files(root: Path):
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
            yield p


def write_csv(path: Path, header: list[str], rows: list[list[str]]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="Root folder to scan (e.g., ...\\Takeout\\Google Photos)")
    ap.add_argument("--out", default="pair_scan_output", help="Output folder for CSVs (relative or absolute)")
    ap.add_argument(
        "--media-exts",
        default=",".join(sorted(MEDIA_EXTS_DEFAULT)),
        help="Comma-separated media extensions to treat as media files",
    )
    args = ap.parse_args()

    root = Path(args.root).resolve()
    out_dir = Path(args.out).resolve() if Path(args.out).is_absolute() else (Path.cwd() / args.out).resolve()

    media_exts = {e.strip().lower() for e in args.media_exts.split(",") if e.strip()}
    if not root.exists():
        raise SystemExit(f"Root path does not exist: {root}")

    # Indexes
    # "media_key" is the exact media filename including extension (e.g., IMG_1234.jpg)
    media_by_dir: dict[Path, dict[str, Path]] = defaultdict(dict)
    sidecars_by_dir: dict[Path, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    other_json: list[Path] = []

    # Collect
    for p in walk_files(root):
        name = p.name
        ext = p.suffix.lower()
        d = p.parent

        if ext in media_exts:
            # exact key: filename with extension
            # If duplicates in same directory with same name, keep first but record ambiguity later
            if name not in media_by_dir[d]:
                media_by_dir[d][name] = p
            else:
                # duplicate filename in same dir (rare, but possible)
                # store as a "multi" by converting to list via a hack: keep first, add others via sidecars_by_dir sentinel
                sidecars_by_dir[d]["__DUPLICATE_MEDIA_FILENAMES__"].append(p)

        elif ext == ".json":
            if is_sidecar_name(name):
                media_name = sidecar_to_media_name(name)
                sidecars_by_dir[d][media_name].append(p)
            else:
                other_json.append(p)

    paired_rows: list[list[str]] = []
    missing_rows: list[list[str]] = []
    orphan_rows: list[list[str]] = []
    ambiguous_rows: list[list[str]] = []

    # Summary counters
    total_media = 0
    total_sidecars = 0

    missing_by_ext = Counter()
    paired_by_ext = Counter()
    orphan_sidecars_by_media_ext = Counter()
    other_json_by_name = Counter()

    # Per-directory analysis
    for d in sorted(set(media_by_dir.keys()) | set(sidecars_by_dir.keys())):
        medias = media_by_dir.get(d, {})
        sidecars_map = sidecars_by_dir.get(d, {})

        # Count sidecars in dir
        for k, lst in sidecars_map.items():
            if k == "__DUPLICATE_MEDIA_FILENAMES__":
                continue
            total_sidecars += len(lst)

        # Media -> sidecar
        for media_name, media_path in medias.items():
            total_media += 1
            ext = Path(media_name).suffix.lower()

            candidates = sidecars_map.get(media_name, [])
            if len(candidates) == 1:
                sc = candidates[0]
                paired_rows.append([str(media_path), str(sc), str(d)])
                paired_by_ext[ext] += 1
            elif len(candidates) == 0:
                missing_rows.append([str(media_path), media_name, str(d)])
                missing_by_ext[ext] += 1
            else:
                # multiple sidecars for same media filename in same folder
                ambiguous_rows.append([str(media_path), media_name, str(d), ";".join(str(x) for x in candidates)])
                # treat as "missing" for actionability (don’t auto-apply)
                missing_by_ext[ext] += 1

        # Sidecar -> media (orphans)
        for media_name, candidates in sidecars_map.items():
            if media_name == "__DUPLICATE_MEDIA_FILENAMES__":
                continue
            if media_name not in medias:
                for sc in candidates:
                    orphan_rows.append([str(sc), media_name, str(d)])
                    # estimate media ext by the media_name suffix
                    orphan_sidecars_by_media_ext[Path(media_name).suffix.lower()] += 1

    # Other JSON rollup
    for p in other_json:
        other_json_by_name[p.name] += 1

    # Write CSVs
    write_csv(out_dir / "paired.csv", ["media_path", "sidecar_path", "folder"], paired_rows)
    write_csv(out_dir / "missing_sidecar.csv", ["media_path", "media_filename", "folder"], missing_rows)
    write_csv(out_dir / "orphan_sidecar.csv", ["sidecar_path", "expected_media_filename", "folder"], orphan_rows)
    write_csv(out_dir / "ambiguous_multiple_sidecars.csv", ["media_path", "media_filename", "folder", "sidecar_candidates"], ambiguous_rows)
    write_csv(
        out_dir / "other_json.csv",
        ["json_path", "folder"],
        [[str(p), str(p.parent)] for p in other_json],
    )
    write_csv(
        out_dir / "other_json_name_counts.csv",
        ["json_filename", "count"],
        [[name, str(cnt)] for name, cnt in other_json_by_name.most_common()],
    )

    # Console summary
    print(f"\nScanned root: {root}")
    print(f"Reports written to: {out_dir}\n")

    print("Counts")
    print(f"  Media files:              {total_media}")
    print(f"  Sidecar JSON files:       {total_sidecars}")
    print(f"  Paired (exact, same dir): {len(paired_rows)}")
    print(f"  Missing sidecar:          {len(missing_rows)}")
    print(f"  Ambiguous (multi sidecar):{len(ambiguous_rows)}")
    print(f"  Orphan sidecar:           {len(orphan_rows)}")
    print(f"  Other JSON (non-sidecar): {len(other_json)}")

    def print_top(counter: Counter, title: str, n: int = 10):
        if not counter:
            return
        print(f"\n{title}")
        for k, v in counter.most_common(n):
            print(f"  {k or '(no ext)'}: {v}")

    print_top(paired_by_ext, "Paired by media extension")
    print_top(missing_by_ext, "Missing sidecar by media extension")
    print_top(orphan_sidecars_by_media_ext, "Orphan sidecars (expected media ext)")

    # Special warning: duplicate filenames in same directory
    dup_hits = 0
    for d, m in sidecars_by_dir.items():
        dup_hits += len(m.get("__DUPLICATE_MEDIA_FILENAMES__", []))
    if dup_hits:
        print(f"\nWarning: found {dup_hits} duplicate media filenames in the same folder (risk of overwrites if flattened).")

    print("\nNext step: open missing_sidecar.csv and check whether misses are mostly screenshots/PNGs/etc.")
    print("If you want, paste 5-10 rows from missing_sidecar.csv and orphan_sidecar.csv and we’ll interpret patterns.")


if __name__ == "__main__":
    main()