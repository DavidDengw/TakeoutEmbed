#!/usr/bin/env python3
import argparse
import csv
import shutil
from pathlib import Path


def read_targets(csv_path: Path):
    targets = []
    with csv_path.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            name = (row[0] or '').strip()
            if not name:
                continue
            targets.append(name)

    seen = set()
    out = []
    for t in targets:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def build_index(root: Path):
    idx = {}
    for p in root.rglob('*'):
        if p.is_file():
            idx.setdefault(p.name.lower(), []).append(p)
    return idx


def unique_dest(path: Path):
    if not path.exists():
        return path
    stem, suf = path.stem, path.suffix
    i = 1
    while True:
        cand = path.with_name(f'{stem}__{i}{suf}')
        if not cand.exists():
            return cand
        i += 1


def main():
    ap = argparse.ArgumentParser(description='Find files named in CSV and copy them from source tree.')
    ap.add_argument('--csv', required=True, help='Path to CSV file containing file names in first column')
    ap.add_argument('--source', required=True, help='Source root folder to search recursively')
    args = ap.parse_args()

    csv_path = Path(args.csv).expanduser().resolve()
    source_root = Path(args.source).expanduser().resolve()

    if not csv_path.exists() or not csv_path.is_file():
        raise SystemExit(f'CSV not found: {csv_path}')
    if not source_root.exists() or not source_root.is_dir():
        raise SystemExit(f'Source folder not found: {source_root}')

    csv_stem = csv_path.stem
    dest_root = csv_path.parent / csv_stem
    dest_root.mkdir(parents=True, exist_ok=True)

    targets = read_targets(csv_path)
    index = build_index(source_root)

    found = 0
    missing = 0

    for name in targets:
        matches = index.get(name.lower(), [])
        if not matches:
            print(f'MISSING: {name}')
            missing += 1
            continue

        for src in matches:
            dest = unique_dest(dest_root / src.name)
            shutil.copy2(src, dest)
            print(f'COPIED: {src} -> {dest}')
            found += 1

    print('\nDone')
    print(f'CSV: {csv_path}')
    print(f'Source: {source_root}')
    print(f'Destination: {dest_root}')
    print(f'Target names: {len(targets)}')
    print(f'Files copied: {found}')
    print(f'Names missing: {missing}')


if __name__ == '__main__':
    main()
