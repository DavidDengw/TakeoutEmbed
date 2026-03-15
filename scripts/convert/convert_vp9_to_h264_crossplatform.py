#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v"}


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def have_tool(name: str) -> bool:
    return shutil.which(name) is not None


def progress_bar(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[....................] 0% (0/0)"
    filled = int((done / total) * width)
    bar = "o" * filled + "." * (width - filled)
    pct = int((done / total) * 100)
    return f"[{bar}] {pct}% ({done}/{total})"


def print_progress(done: int, total: int):
    print("\rprogress " + progress_bar(done, total), end="", flush=True)
    if done >= total:
        print()


def compressor_id(path: Path):
    rc, out, err = run(["exiftool", "-s3", "-CompressorID", str(path)])
    if rc != 0:
        return None, err or out
    return out.strip().lower(), ""


def ffmpeg_convert(src: Path, dst: Path):
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(src),
        "-map_metadata", "0",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(dst),
    ]
    return run(cmd)


def copy_time_tags(src: Path, dst: Path):
    rc, out, err = run([
        "exiftool",
        "-overwrite_original",
        "-TagsFromFile", str(src),
        "-Time:All>Time:All",
        str(dst),
    ])
    return rc, out, err


def preserve_fs_times(src: Path, dst: Path):
    st = src.stat()
    os.utime(dst, (st.st_atime, st.st_mtime))


def find_video_files(root: Path):
    out = []
    checked = 0
    print(f"Scanning under: {root}")
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        out.append(p)
        checked += 1
        if checked % 100 == 0:
            print(f"scan progress: checked {checked} video files...")
    print(f"scan done: checked {checked} video files")
    return out


def in_place_convert_if_vp9(src: Path):
    cid, msg = compressor_id(src)
    if cid != "vp09":
        return "skip", msg or cid or "not_vp09"

    # temp in same folder for atomic replace to work on same filesystem
    tmp = src.with_name(f".{src.stem}.h264tmp.mp4")

    rc, out, err = ffmpeg_convert(src, tmp)
    if rc != 0:
        tmp.unlink(missing_ok=True)
        return "fail", err or out

    rc2, out2, err2 = copy_time_tags(src, tmp)
    if rc2 != 0:
        print(f"WARN time-tags: {src} -> {tmp}\n{err2 or out2}")

    preserve_fs_times(src, tmp)

    backup = src.with_name(f".{src.name}.bak_h264")
    try:
        os.replace(src, backup)
        os.replace(tmp, src)
        backup.unlink(missing_ok=True)
        return "ok", "converted_in_place"
    except Exception as e:
        # rollback best effort
        try:
            if src.exists():
                src.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if backup.exists():
                os.replace(backup, src)
        except Exception:
            pass
        tmp.unlink(missing_ok=True)
        return "fail", f"swap_failed:{e}"


def main():
    ap = argparse.ArgumentParser(description="Cross-platform VP9->H264 in-place converter (Windows/macOS/Linux).")
    ap.add_argument("root", nargs="?", default=".", help="Folder to process recursively")
    ap.add_argument("--dry-run", action="store_true", help="Only report VP9 files; no conversion")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Root not found: {root}")

    if not have_tool("exiftool"):
        raise SystemExit("exiftool not found in PATH")
    if not have_tool("ffmpeg") and not args.dry_run:
        raise SystemExit("ffmpeg not found in PATH")

    videos = find_video_files(root)

    folder_map = {}
    for f in sorted(videos):
        folder_map.setdefault(f.parent, []).append(f)

    vp9_found = 0
    converted = 0
    failed = 0
    skipped = 0

    for folder in sorted(folder_map):
        files = folder_map[folder]
        print(f"\nFolder: {folder}")
        done = 0
        total = len(files)
        print_progress(done, total)

        for src in files:
            if args.dry_run:
                cid, _ = compressor_id(src)
                if cid == "vp09":
                    vp9_found += 1
                    print(f"VP9: {src}")
                else:
                    skipped += 1
                done += 1
                print_progress(done, total)
                continue

            status, msg = in_place_convert_if_vp9(src)
            if status == "ok":
                vp9_found += 1
                converted += 1
                print(f"OK: {src} ({msg})")
            elif status == "skip":
                skipped += 1
            else:
                vp9_found += 1
                failed += 1
                print(f"FAIL: {src}\n{msg}")

            done += 1
            print_progress(done, total)

    print("\nSummary:")
    print(json.dumps({
        "root": str(root),
        "vp9_found": vp9_found,
        "converted_in_place": converted,
        "failed": failed,
        "skipped_non_vp9": skipped,
        "dry_run": args.dry_run,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
