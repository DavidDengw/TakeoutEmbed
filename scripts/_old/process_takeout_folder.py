#!/usr/bin/env python3
import argparse
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

MEDIA_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif', '.tif', '.tiff', '.mp4', '.mov', '.m4v', '.avi', '.3gp'}
IMG_EXT = {'.jpg', '.jpeg', '.png', '.heic', '.heif', '.webp', '.gif', '.tif', '.tiff'}
VID_EXT = {'.mp4', '.mov', '.m4v', '.avi', '.3gp'}


def get_media_date(path: Path):
    try:
        out = subprocess.check_output(
            ['mdls', '-raw', '-name', 'kMDItemContentCreationDate', str(path)],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None
    if not out or out == '(null)':
        return None
    try:
        return datetime.strptime(out, '%Y-%m-%d %H:%M:%S %z')
    except Exception:
        return None


def find_sidecar(media: Path):
    candidates = [
        Path(str(media) + '.supplemental-metadata.json'),
        Path(str(media) + '.supplementa.json'),
        Path(str(media) + '.suppl.json'),
        media.with_suffix(media.suffix + '.json'),
        media.with_suffix('.json'),
    ]
    for c in candidates:
        if c.exists():
            return c
    for c in media.parent.glob(media.name + '*json'):
        return c
    return None


def dt_from_filename(name: str):
    patterns = [
        r'.*?(20\d{2})-(\d{2})-(\d{2})[-_](\d{2})-(\d{2})-(\d{2}).*',
        r'.*?(20\d{2})(\d{2})(\d{2})[-_](\d{2})(\d{2})(\d{2}).*',
        r'.*?(20\d{2})(\d{2})(\d{2}).*',
    ]
    for i, pat in enumerate(patterns):
        m = re.match(pat, name)
        if not m:
            continue
        g = m.groups()
        if i < 2:
            y, mo, d, h, mi, s = g
        else:
            y, mo, d = g
            h = mi = s = '00'
        try:
            dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
            if 2000 <= dt.year <= 2025:
                return int(dt.timestamp()), 'filename_pattern'
        except Exception:
            pass

    for token in re.findall(r'\d{10,13}', name):
        try:
            v = int(token)
            if len(token) == 13:
                v //= 1000
            dt = datetime.utcfromtimestamp(v)
            if 2000 <= dt.year <= 2025:
                return v, 'filename_epoch'
        except Exception:
            pass

    return None, None


def exiftool_write(media: Path, ts: int):
    dt = datetime.utcfromtimestamp(ts).strftime('%Y:%m:%d %H:%M:%S')
    dtu = dt + 'Z'
    ext = media.suffix.lower()
    cmd = ['exiftool', '-overwrite_original', '-P']

    if ext in IMG_EXT:
        cmd += [
            f'-DateTimeOriginal={dt}',
            f'-CreateDate={dt}',
            f'-ModifyDate={dt}',
            f'-XMP:DateTimeOriginal={dtu}',
            f'-XMP:CreateDate={dtu}',
            f'-XMP:ModifyDate={dtu}',
        ]
        if ext == '.png':
            cmd += [f'-PNG:CreationTime={dt}']
    elif ext in VID_EXT:
        cmd += [
            f'-QuickTime:CreateDate={dtu}',
            f'-QuickTime:ModifyDate={dtu}',
            f'-QuickTime:TrackCreateDate={dtu}',
            f'-QuickTime:TrackModifyDate={dtu}',
            f'-QuickTime:MediaCreateDate={dtu}',
            f'-QuickTime:MediaModifyDate={dtu}',
            f'-XMP:CreateDate={dtu}',
            f'-XMP:ModifyDate={dtu}',
        ]
    else:
        return False

    return subprocess.run(cmd + [str(media)], capture_output=True).returncode == 0


def process_folder(root: Path, folder_name: str, unfixed_dir: Path = None):
    sub = root / folder_name
    arch = root / '_processed_archive' / folder_name
    arch.mkdir(parents=True, exist_ok=True)

    stats = {
        'folder': folder_name,
        'total_seen': 0,
        'fixed_from_json': 0,
        'fixed_from_filename': 0,
        'already_valid_moved': 0,
        'moved_total': 0,
        'left_unfixable': 0,
    }
    unfix = []

    for p in sorted(sub.rglob('*')):
        if not p.is_file() or p.suffix.lower() not in MEDIA_EXT:
            continue

        stats['total_seen'] += 1
        d = get_media_date(p)
        invalid = (d is None) or (d.year < 2000) or (d.year > 2025)
        side = find_sidecar(p)
        fixed = False

        if invalid:
            ts = None
            source = None
            if side:
                try:
                    ts_raw = json.loads(side.read_text()).get('photoTakenTime', {}).get('timestamp')
                    if ts_raw:
                        t = int(ts_raw)
                        y = datetime.utcfromtimestamp(t).year
                        if 2000 <= y <= 2025:
                            ts = t
                            source = 'json'
                except Exception:
                    pass

            if ts is None:
                ts, source = dt_from_filename(p.name)

            if ts is not None and exiftool_write(p, ts):
                fixed = True
                if source == 'json':
                    stats['fixed_from_json'] += 1
                else:
                    stats['fixed_from_filename'] += 1

        if invalid and not fixed:
            stats['left_unfixable'] += 1
            unfix.append(str(p.relative_to(sub)))
            continue

        dest = arch / p.relative_to(sub)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(p), str(dest))
        stats['moved_total'] += 1
        if not invalid:
            stats['already_valid_moved'] += 1

        if side and side.exists():
            sdest = arch / side.relative_to(sub)
            sdest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(side), str(sdest))

    # After processing, optionally move any leftovers (unfixable media + sidecars/other residual files)
    moved_leftover_files = 0
    if unfixed_dir is not None:
        target_root = unfixed_dir / folder_name
        target_root.mkdir(parents=True, exist_ok=True)
        for lf in sorted(sub.rglob('*')):
            if not lf.is_file():
                continue
            rel = lf.relative_to(sub)
            dest = target_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(lf), str(dest))
            moved_leftover_files += 1

    stats['unfixable_samples'] = unfix[:20]
    stats['leftover_files_moved_to_unfixed'] = moved_leftover_files
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', help='Takeout Google Photos root, e.g. /Users/.../Takeout-2/Google Photos')
    parser.add_argument('folder', help='Subfolder name to process, e.g. Photos from 2022')
    parser.add_argument('--unfixed-dir', help='Optional directory to move leftover files after processing (e.g. /Users/.../Downloads/unfixed media)')
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        raise SystemExit(f'Root not found: {root}')
    if not (root / args.folder).exists():
        raise SystemExit(f'Folder not found: {root / args.folder}')

    unfixed_dir = Path(args.unfixed_dir) if args.unfixed_dir else None
    print(json.dumps(process_folder(root, args.folder, unfixed_dir=unfixed_dir), ensure_ascii=False))


if __name__ == '__main__':
    main()
