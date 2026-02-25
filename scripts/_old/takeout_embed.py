{\rtf1\ansi\ansicpg1252\cocoartf2761
\cocoatextscaling0\cocoaplatform0{\fonttbl\f0\fswiss\fcharset0 Helvetica;}
{\colortbl;\red255\green255\blue255;}
{\*\expandedcolortbl;;}
\margl1440\margr1440\vieww11520\viewh8400\viewkind0
\pard\tx566\tx1133\tx1700\tx2267\tx2834\tx3401\tx3968\tx4535\tx5102\tx5669\tx6236\tx6803\pardirnatural\partightenfactor0

\f0\fs24 \cf0 #!/usr/bin/env python3\
import argparse\
import concurrent.futures as cf\
import json\
import os\
import subprocess\
from datetime import datetime, timezone\
from pathlib import Path\
\
MEDIA_EXTS = \{".jpg", ".jpeg", ".png", ".mp4", ".mov"\}\
# Takeout sometimes uses uppercase extensions\
MEDIA_EXTS |= \{e.upper() for e in MEDIA_EXTS\}\
\
def iso_like_exif(dt_utc: datetime) -> str:\
    # EXIF date format: YYYY:MM:DD HH:MM:SS\
    return dt_utc.strftime("%Y:%m:%d %H:%M:%S")\
\
def read_takeout_json(jpath: Path) -> dict:\
    with jpath.open("r", encoding="utf-8") as f:\
        return json.load(f)\
\
def get_timestamp_seconds(meta: dict) -> int | None:\
    # Prefer photoTakenTime; fallback to creationTime\
    for k in ("photoTakenTime", "creationTime"):\
        v = meta.get(k, \{\})\
        ts = v.get("timestamp")\
        if ts:\
            try:\
                return int(ts)\
            except ValueError:\
                pass\
    return None\
\
def get_gps(meta: dict):\
    # Prefer geoDataExif (what was in EXIF at export time), then geoData\
    for k in ("geoDataExif", "geoData"):\
        g = meta.get(k, \{\}) or \{\}\
        lat = g.get("latitude")\
        lon = g.get("longitude")\
        alt = g.get("altitude")\
        # Takeout often uses 0,0 when missing\
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):\
            if abs(lat) > 0.000001 or abs(lon) > 0.000001:\
                return float(lat), float(lon), (float(alt) if isinstance(alt, (int, float)) else None)\
    return None\
\
def get_caption(meta: dict) -> str | None:\
    # Often present as "description"\
    desc = meta.get("description")\
    if isinstance(desc, str) and desc.strip():\
        return desc.strip()\
    return None\
\
def run_exiftool(media_path: Path, dt_utc: datetime, gps, caption: str | None, dry_run: bool) -> tuple[bool, str]:\
    dt_str = iso_like_exif(dt_utc)\
\
    # We write UTC time and also write offset tags as +00:00 where supported.\
    # This reduces \'93day shift\'94 surprises vs writing UTC without an offset.\
    args = [\
        "exiftool",\
        "-overwrite_original",\
        "-P",  # preserve filesystem timestamps as much as possible\
        "-m",  # ignore minor warnings\
        "-api", "QuickTimeUTC=1",  # treat QuickTime times as UTC\
        f"-DateTimeOriginal=\{dt_str\}",\
        f"-CreateDate=\{dt_str\}",\
        f"-ModifyDate=\{dt_str\}",\
        "-OffsetTimeOriginal=+00:00",\
        "-OffsetTime=+00:00",\
        "-OffsetTimeDigitized=+00:00",\
    ]\
\
    # MP4/MOV: also set multiple QuickTime time atoms for better compatibility\
    ext = media_path.suffix\
    if ext.lower() in (".mp4", ".mov"):\
        args += [\
            f"-TrackCreateDate=\{dt_str\}",\
            f"-TrackModifyDate=\{dt_str\}",\
            f"-MediaCreateDate=\{dt_str\}",\
            f"-MediaModifyDate=\{dt_str\}",\
        ]\
\
    if gps:\
        lat, lon, alt = gps\
        # Signed decimal degrees is fine for ExifTool\
        args += [f"-GPSLatitude=\{lat\}", f"-GPSLongitude=\{lon\}"]\
        if alt is not None:\
            args += [f"-GPSAltitude=\{alt\}"]\
\
    if caption:\
        # Write caption into common fields; Photos may pick up one of these depending on file type\
        args += [f"-Description=\{caption\}", f"-ImageDescription=\{caption\}", f"-Caption-Abstract=\{caption\}"]\
\
    args.append(str(media_path))\
\
    if dry_run:\
        return True, "DRYRUN " + " ".join(args)\
\
    p = subprocess.run(args, capture_output=True, text=True)\
    if p.returncode == 0:\
        return True, p.stdout.strip()\
    return False, (p.stderr.strip() or p.stdout.strip() or f"ExifTool failed with code \{p.returncode\}")\
\
def process_one(media_path: Path, dry_run: bool) -> tuple[str, bool, str]:\
    jpath = Path(str(media_path) + ".json")  # e.g. IMG_1234.JPG.json\
    if not jpath.exists():\
        return (str(media_path), False, "No sidecar JSON found")\
\
    try:\
        meta = read_takeout_json(jpath)\
    except Exception as e:\
        return (str(media_path), False, f"JSON read error: \{e\}")\
\
    ts = get_timestamp_seconds(meta)\
    if ts is None:\
        return (str(media_path), False, "No usable timestamp in JSON (photoTakenTime/creationTime)")\
\
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)\
    gps = get_gps(meta)\
    caption = get_caption(meta)\
\
    ok, msg = run_exiftool(media_path, dt_utc, gps, caption, dry_run)\
    return (str(media_path), ok, msg)\
\
def iter_media(root: Path):\
    for dirpath, _, filenames in os.walk(root):\
        for fn in filenames:\
            p = Path(dirpath) / fn\
            if p.suffix in MEDIA_EXTS:\
                # Skip files that are actually the sidecar json\
                if p.name.lower().endswith(".json"):\
                    continue\
                yield p\
\
def main():\
    ap = argparse.ArgumentParser(description="Embed Google Takeout JSON metadata into media files using ExifTool.")\
    ap.add_argument("root", help="Root folder containing unzipped Google Takeout exports")\
    ap.add_argument("--workers", type=int, default=4, help="Parallel workers (default: 4)")\
    ap.add_argument("--dry-run", action="store_true", help="Print exiftool commands without modifying files")\
    ap.add_argument("--log", default="takeout_embed.log", help="Log file path (default: takeout_embed.log)")\
    args = ap.parse_args()\
\
    root = Path(args.root).expanduser().resolve()\
    media_files = list(iter_media(root))\
\
    with open(args.log, "w", encoding="utf-8") as log:\
        log.write(f"Root: \{root\}\\nFiles: \{len(media_files)\}\\nDry-run: \{args.dry_run\}\\nWorkers: \{args.workers\}\\n\\n")\
\
        ok_count = 0\
        fail_count = 0\
\
        with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:\
            futures = [ex.submit(process_one, p, args.dry_run) for p in media_files]\
            for fut in cf.as_completed(futures):\
                path, ok, msg = fut.result()\
                if ok:\
                    ok_count += 1\
                    log.write(f"OK   \{path\}\\n\{msg\}\\n\\n")\
                else:\
                    fail_count += 1\
                    log.write(f"FAIL \{path\}\\n\{msg\}\\n\\n")\
\
        log.write(f"Summary: OK=\{ok_count\} FAIL=\{fail_count\}\\n")\
        print(f"Done. OK=\{ok_count\} FAIL=\{fail_count\}. See log: \{args.log\}")\
\
if __name__ == "__main__":\
    main()\
}