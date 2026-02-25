#!/usr/bin/env python3
import argparse
import json
import os
import re
import unicodedata
import uuid
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import subprocess

MEDIA_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".gif", ".tif", ".tiff",
    ".mp4", ".mov", ".m4v", ".avi", ".3gp",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".3gp"}
JSON_EXT = ".json"

DEFAULT_SUFFIX_TOKENS = [
    "-edited", ".edited", "(1)", "(2)",
    ".supplemental-metadata", "supplemental-metadata",
    ".supplemental-metadat", "supplemental-metadat",
    ".metadata", "-collage", "-effects", "-motion", "-hdr",
    " copy", " copy 1",
]

EXCLUDED_DIRS = {"_UPLOAD_READY", "_REVIEW_INVALID", "_REVIEW", "_logs"}


@dataclass
class Config:
    root: Path
    valid_start: datetime
    valid_end: datetime
    workers: int
    preserve_subpaths: bool
    review_invalid: bool
    suffix_tokens: List[str]


def run(cmd: List[str]) -> Tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def check_exiftool() -> None:
    rc, out, err = run(["exiftool", "-ver"])
    if rc != 0:
        raise SystemExit(f"exiftool not found. Install with: brew install exiftool\n{err or out}")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> List[dict]:
    out = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def load_successful_apply_pairs(apply_log: Path) -> Set[Tuple[str, str]]:
    pairs = set()
    for row in load_jsonl(apply_log):
        if row.get("action") == "apply" and row.get("status") == "ok":
            j = row.get("json_path")
            m = row.get("media_path")
            if j and m:
                pairs.add((j, m))
    return pairs


def load_moved_media(move_log: Path) -> Set[str]:
    moved = set()
    for row in load_jsonl(move_log):
        if row.get("action") == "move" and row.get("status") == "ok":
            m = row.get("media_path")
            if m:
                moved.add(m)
    return moved


def _looks_camera_name(stem_key: str) -> bool:
    s = stem_key.strip().lower()
    return bool(
        re.fullmatch(r"img\s*\d{3,}", s)
        or re.fullmatch(r"dsc\s*\d{3,}", s)
        or re.fullmatch(r"vid\s*\d{3,}", s)
        or re.fullmatch(r"pxl\s*\d{8}\s*\d{6}.*", s)
    )


def normalize_stem(stem: str, suffix_tokens: List[str]) -> Tuple[str, str]:
    # Rule: split on FIRST '.' and keep only left side.
    s = unicodedata.normalize("NFKC", stem)
    s = s.split('.', 1)[0]

    # Normalize separators/spaces; keep digits.
    s = s.lower().replace('_', ' ')
    s = re.sub(r"\s+", " ", s).strip()

    variation_words = r"(?:edited|edit|enhanced|cropped|export|converted|final|version)"

    changed = True
    while changed:
        changed = False

        # Remove variation markers at END (repeatable)
        ns = re.sub(rf"(?:[\s-]+{variation_words})$", "", s).strip()
        if ns != s:
            s = ns
            changed = True
            continue

        # Remove bracketed counters at END: (1), [2], {3}
        ns = re.sub(r"\s*[\(\[\{]\d+[\)\]\}]$", "", s).strip()
        if ns != s:
            s = ns
            changed = True
            continue

        # Remove duplicate markers at END: copy, copy 2, duplicate, dup
        ns = re.sub(r"\s*(?:copy(?:\s+\d+)?|duplicate|dup)$", "", s).strip()
        if ns != s:
            s = ns
            changed = True
            continue

        # Remove trailing -1/_2 only for camera-style base names.
        m = re.search(r"([_-])(\d+)$", s)
        if m:
            base = s[:m.start()].strip()
            camera_probe = re.sub(r"[^a-z0-9]", "", base)
            if _looks_camera_name(camera_probe):
                s = base
                changed = True
                continue

    s = re.sub(r"\s+", " ", s).strip()
    strict = s
    loose = re.sub(r"[^a-z0-9]+", "", strict)
    return strict, loose


def _tokenize_key(k: str) -> Set[str]:
    return {t for t in re.split(r"\s+", k.strip()) if t}


def _numbers_in_key(k: str) -> Set[str]:
    return set(re.findall(r"\d+", k))


def _match_score(query_key: str, candidate_key: str) -> float:
    q = _tokenize_key(query_key)
    c = _tokenize_key(candidate_key)
    if not q or not c:
        return 0.0
    inter = len(q & c)
    union = len(q | c)
    score = inter / union if union else 0.0

    qn = _numbers_in_key(query_key)
    cn = _numbers_in_key(candidate_key)
    if qn and cn and qn != cn:
        score -= 0.4
    return score


def iter_in_scope(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in EXCLUDED_DIRS for part in p.parts):
            continue
        yield p


def rel(p: Path, root: Path) -> str:
    return str(p.relative_to(root))


def build_indexes(root: Path, suffix_tokens: List[str]):
    media_files: List[Path] = []
    json_files: List[Path] = []
    media_index: Dict[Tuple[str, str], Set[Path]] = defaultdict(set)
    json_index: Dict[Tuple[str, str], Set[Path]] = defaultdict(set)

    for p in iter_in_scope(root):
        ext = p.suffix.lower()
        if ext in MEDIA_EXTS:
            media_files.append(p)
            strict, loose = normalize_stem(p.stem, suffix_tokens)
            media_index[("strict", strict)].add(p)
            media_index[("loose", loose)].add(p)
        elif ext == JSON_EXT:
            json_files.append(p)
            strict, loose = normalize_stem(p.stem, suffix_tokens)
            json_index[("strict", strict)].add(p)
            json_index[("loose", loose)].add(p)

    return media_files, json_files, media_index, json_index


def parse_takeout_json(jpath: Path):
    try:
        meta = json.loads(jpath.read_text(encoding="utf-8"))
    except Exception as e:
        return None, None, None, f"json_parse_error:{e}"

    ts = None
    pt = meta.get("photoTakenTime") or {}
    ct = meta.get("creationTime") or {}

    if pt.get("timestamp"):
        try:
            ts = int(pt["timestamp"])
        except Exception:
            ts = None

    if ts is None and pt.get("formatted"):
        try:
            dt = datetime.fromisoformat(str(pt["formatted"]).replace("Z", "+00:00"))
            ts = int(dt.timestamp())
        except Exception:
            ts = None

    if ts is None and ct.get("timestamp"):
        try:
            ts = int(ct["timestamp"])
        except Exception:
            ts = None

    gps = None
    for k in ("geoDataExif", "geoData"):
        g = meta.get(k) or {}
        lat = g.get("latitude")
        lon = g.get("longitude")
        alt = g.get("altitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (abs(lat) > 1e-8 or abs(lon) > 1e-8):
            gps = (float(lat), float(lon), float(alt) if isinstance(alt, (int, float)) else None)
            break

    return meta, ts, gps, None


def exif_dt(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")


def detect_file_type(media: Path) -> str:
    rc, out, _ = run(["exiftool", "-s3", "-FileType", str(media)])
    if rc == 0 and out:
        return out.strip().lower()
    return media.suffix.lower().lstrip('.')


def write_metadata(media: Path, ts: int, gps) -> Tuple[bool, str, Path]:
    dt = exif_dt(ts)
    dtz = dt + "Z"
    ext = media.suffix.lower()
    ftype = detect_file_type(media)

    cmd = ["exiftool", "-overwrite_original", "-P", "-m", "-api", "QuickTimeUTC=1"]

    if ext in VIDEO_EXTS or ftype in {"mp4", "mov", "m4v", "avi", "3gp"}:
        cmd += [
            f"-CreateDate={dt}", f"-ModifyDate={dt}",
            f"-TrackCreateDate={dt}", f"-TrackModifyDate={dt}",
            f"-MediaCreateDate={dt}", f"-MediaModifyDate={dt}",
        ]
    else:
        cmd += [
            f"-DateTimeOriginal={dt}", f"-CreateDate={dt}", f"-ModifyDate={dt}",
            f"-XMP:DateTimeOriginal={dtz}", f"-XMP:CreateDate={dtz}", f"-XMP:ModifyDate={dtz}",
        ]

    if gps:
        lat, lon, alt = gps
        cmd += [f"-GPSLatitude={lat}", f"-GPSLongitude={lon}"]
        if alt is not None:
            cmd += [f"-GPSAltitude={alt}"]

    cmd.append(str(media))
    rc, out, err = run(cmd)
    if rc == 0:
        return True, (out or err), media

    # Fallback for stubborn image formats: XMP-only date write.
    if ext not in VIDEO_EXTS:
        cmd2 = [
            "exiftool", "-overwrite_original", "-P", "-m",
            f"-XMP:DateTimeOriginal={dtz}", f"-XMP:CreateDate={dtz}", f"-XMP:ModifyDate={dtz}",
        ]
        if gps:
            lat, lon, alt = gps
            cmd2 += [f"-GPSLatitude={lat}", f"-GPSLongitude={lon}"]
            if alt is not None:
                cmd2 += [f"-GPSAltitude={alt}"]
        cmd2.append(str(media))
        rc2, out2, err2 = run(cmd2)
        if rc2 == 0:
            return True, (out2 or err2), media


    # Fallback: mislabeled PNG files that are actually JPEG data.
    emsg = (err or out or "")
    if media.suffix.lower() == ".png" and "looks more like a JPEG" in emsg:
        target = media.with_suffix(".jpg")
        i = 1
        while target.exists():
            target = media.with_name(f"{media.stem}__{i}.jpg")
            i += 1
        os.rename(media, target)
        cmd3 = [
            "exiftool", "-overwrite_original", "-P", "-m",
            f"-DateTimeOriginal={dt}", f"-CreateDate={dt}", f"-ModifyDate={dt}",
            f"-XMP:DateTimeOriginal={dtz}", f"-XMP:CreateDate={dtz}", f"-XMP:ModifyDate={dtz}",
        ]
        if gps:
            lat, lon, alt = gps
            cmd3 += [f"-GPSLatitude={lat}", f"-GPSLongitude={lon}"]
            if alt is not None:
                cmd3 += [f"-GPSAltitude={alt}"]
        cmd3.append(str(target))
        rc3, out3, err3 = run(cmd3)
        if rc3 == 0:
            return True, f"renamed_to:{target.name}; " + (out3 or err3), target
        return False, f"rename_retry_failed:{err3 or out3}", target

    return False, (err or out), media


def parse_exif_date(s: str) -> Optional[datetime]:
    s = s.strip()
    s = re.sub(r"([+-]\d\d):(\d\d)$", r"\1\2", s)
    fmts = [
        "%Y:%m:%d %H:%M:%S%z",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def read_effective_timestamp(media: Path) -> Tuple[Optional[datetime], str]:
    cmd = [
        "exiftool", "-j",
        "-DateTimeOriginal", "-CreateDate", "-ModifyDate", "-MediaCreateDate", "-TrackCreateDate",
        "-XMP:DateTimeOriginal", "-XMP:CreateDate", "-PNG:CreationTime",
        str(media),
    ]
    rc, out, err = run(cmd)
    if rc != 0:
        return None, f"read_error:{err or out}"

    try:
        rows = json.loads(out)
        row = rows[0] if rows else {}
    except Exception as e:
        return None, f"read_json_error:{e}"

    for k in ("DateTimeOriginal", "CreateDate", "ModifyDate", "MediaCreateDate", "TrackCreateDate", "CreationTime"):
        v = row.get(k)
        if isinstance(v, str) and v.strip():
            dt = parse_exif_date(v)
            if dt is not None:
                return dt, "ok"
            return None, f"parse_fail:{k}:{v}"

    return None, "missing_tags"


def choose_matches_for_json(jpath: Path, media_index, suffix_tokens: List[str]) -> Tuple[List[Path], str]:
    strict, loose = normalize_stem(jpath.stem, suffix_tokens)
    candidates = sorted(media_index.get(("strict", strict), []))
    mode = "strict"
    query_key = strict
    if not candidates:
        candidates = sorted(media_index.get(("loose", loose), []))
        mode = "loose"
        query_key = loose
    if not candidates:
        return [], "none"
    if len(candidates) == 1:
        return candidates, mode

    scored = []
    for c in candidates:
        c_strict, c_loose = normalize_stem(c.stem, suffix_tokens)
        cand_key = c_strict if mode == "strict" else c_loose
        scored.append((_match_score(query_key, cand_key), c))

    best = max(scored, key=lambda x: x[0])[0]
    if best <= 0:
        return [], "none"
    picked = sorted([c for sc, c in scored if sc == best])
    return picked, mode


def choose_jsons_for_media(mpath: Path, json_index, suffix_tokens: List[str]) -> List[Path]:
    strict, loose = normalize_stem(mpath.stem, suffix_tokens)
    candidates = set()
    candidates.update(json_index.get(("strict", strict), set()))
    candidates.update(json_index.get(("loose", loose), set()))
    candidates = sorted(candidates)
    if len(candidates) <= 1:
        return candidates

    scored = []
    for j in candidates:
        j_strict, _ = normalize_stem(j.stem, suffix_tokens)
        scored.append((_match_score(strict, j_strict), j))
    best = max(scored, key=lambda x: x[0])[0]
    if best <= 0:
        return []
    return sorted([j for sc, j in scored if sc == best])


def unique_destination(dst: Path) -> Path:
    if not dst.exists():
        return dst
    base = dst.stem
    ext = dst.suffix
    i = 1
    while True:
        cand = dst.with_name(f"{base}__{i}{ext}")
        if not cand.exists():
            return cand
        i += 1


def atomic_move_no_overwrite(src: Path, dst: Path) -> Path:
    dst = unique_destination(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # atomic on same filesystem
    return dst


def pass1_apply(cfg: Config, run_id: str, json_files: List[Path], media_index, apply_log: Path,
                applied_ok_pairs: Set[Tuple[str, str]]) -> Counter:
    stats = Counter()

    def task(jpath: Path):
        local_rows = []
        local_stats = Counter()
        json_rel = rel(jpath, cfg.root)

        _, ts, gps, err = parse_takeout_json(jpath)
        if err:
            local_stats["json_parse_fail"] += 1
            local_rows.append({
                "run_id": run_id, "timestamp": now_iso(), "action": "apply", "status": "fail",
                "json_path": json_rel, "media_path": None, "details": err,
            })
            return local_rows, local_stats

        if ts is None:
            local_stats["json_no_timestamp"] += 1
            local_rows.append({
                "run_id": run_id, "timestamp": now_iso(), "action": "apply", "status": "fail",
                "json_path": json_rel, "media_path": None, "details": "no_timestamp",
            })
            return local_rows, local_stats

        matches, mode = choose_matches_for_json(jpath, media_index, cfg.suffix_tokens)
        if not matches:
            local_stats["json_no_match"] += 1
            local_rows.append({
                "run_id": run_id, "timestamp": now_iso(), "action": "apply", "status": "no_match",
                "json_path": json_rel, "media_path": None, "details": {"match_mode": mode},
            })
            return local_rows, local_stats

        for media in matches:
            media_rel = rel(media, cfg.root)
            if (json_rel, media_rel) in applied_ok_pairs:
                local_stats["pairs_skipped_already_applied"] += 1
                local_rows.append({
                    "run_id": run_id, "timestamp": now_iso(), "action": "apply", "status": "skip",
                    "json_path": json_rel, "media_path": media_rel, "details": "already_applied",
                })
                continue

            ok, msg, media_after = write_metadata(media, ts, gps)
            if not ok:
                local_stats["pairs_applied_fail"] += 1
                local_rows.append({
                    "run_id": run_id, "timestamp": now_iso(), "action": "apply", "status": "fail",
                    "json_path": json_rel, "media_path": media_rel, "details": f"write_fail:{msg}",
                })
                continue

            dt, reason = read_effective_timestamp(media_after)
            if dt is None:
                local_stats["pairs_applied_fail"] += 1
                local_rows.append({
                    "run_id": run_id, "timestamp": now_iso(), "action": "apply", "status": "fail",
                    "json_path": json_rel, "media_path": media_rel, "details": f"verify_fail:{reason}",
                })
                continue

            local_stats["pairs_applied_ok"] += 1
            media_rel_after = rel(media_after, cfg.root)
            local_rows.append({
                "run_id": run_id, "timestamp": now_iso(), "action": "apply", "status": "ok",
                "json_path": json_rel, "media_path": media_rel_after,
                "details": {"match_mode": mode, "effective_utc": dt.isoformat()},
            })

        return local_rows, local_stats

    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        futs = [ex.submit(task, j) for j in sorted(json_files)]
        total_jobs = len(futs)
        done_jobs = 0
        print_pass_progress("pass1", done_jobs, total_jobs)
        for fut in as_completed(futs):
            rows, st = fut.result()
            stats.update(st)
            for row in rows:
                append_jsonl(apply_log, row)
                if row.get("status") == "ok":
                    applied_ok_pairs.add((row["json_path"], row["media_path"]))
            done_jobs += 1
            print_pass_progress("pass1", done_jobs, total_jobs)

    stats["json_total"] = len(json_files)
    return stats


def pass2_validate_move(cfg: Config, run_id: str, media_files: List[Path], json_index, move_log: Path,
                        moved_media: Set[str]) -> Counter:
    stats = Counter()
    upload_root = cfg.root / "_UPLOAD_READY"
    review_root = cfg.root / "_REVIEW_INVALID"
    upload_root.mkdir(parents=True, exist_ok=True)
    if cfg.review_invalid:
        review_root.mkdir(parents=True, exist_ok=True)

    total_media = len(media_files)
    done_media = 0
    print_pass_progress("pass2", done_media, total_media)
    for media in sorted(media_files):
        media_rel = rel(media, cfg.root)
        stats["media_total"] += 1

        if media_rel in moved_media:
            stats["media_skipped_already_moved"] += 1
            done_media += 1
            print_pass_progress("pass2", done_media, total_media)
            continue

        dt, reason = read_effective_timestamp(media)
        valid = dt is not None and cfg.valid_start <= dt <= cfg.valid_end

        if not valid:
            stats["media_remaining_invalid"] += 1
            row = {
                "run_id": run_id,
                "timestamp": now_iso(),
                "action": "move",
                "status": "invalid",
                "json_path": None,
                "media_path": media_rel,
                "details": {
                    "reason": reason if dt is None else "out_of_range",
                    "effective_utc": None if dt is None else dt.isoformat(),
                },
            }

            if cfg.review_invalid:
                if cfg.preserve_subpaths:
                    dst = review_root / media_rel
                else:
                    dst = review_root / media.name
                newp = atomic_move_no_overwrite(media, dst)
                row["details"]["moved_to"] = rel(newp, cfg.root)

            append_jsonl(move_log, row)
            done_media += 1
            print_pass_progress("pass2", done_media, total_media)
            continue

        if cfg.preserve_subpaths:
            dst_media = upload_root / media_rel
        else:
            dst_media = upload_root / media.name
        new_media = atomic_move_no_overwrite(media, dst_media)

        moved_json = []
        for j in choose_jsons_for_media(media, json_index, cfg.suffix_tokens):
            if not j.exists():
                continue
            j_rel = rel(j, cfg.root)
            if cfg.preserve_subpaths:
                dst_j = upload_root / j_rel
            else:
                dst_j = upload_root / j.name
            new_j = atomic_move_no_overwrite(j, dst_j)
            moved_json.append(rel(new_j, cfg.root))

        append_jsonl(move_log, {
            "run_id": run_id,
            "timestamp": now_iso(),
            "action": "move",
            "status": "ok",
            "json_path": moved_json,
            "media_path": media_rel,
            "details": {
                "effective_utc": dt.isoformat(),
                "moved_media_to": rel(new_media, cfg.root),
            },
        })
        moved_media.add(media_rel)
        stats["media_moved_valid"] += 1
        done_media += 1
        print_pass_progress("pass2", done_media, total_media)

    return stats


def move_json_for_already_uploaded(cfg: Config, run_id: str, move_log: Path) -> Counter:
    stats = Counter()
    upload_root = cfg.root / "_UPLOAD_READY"
    if not upload_root.exists():
        return stats

    _, json_files, _, json_index = build_indexes(cfg.root, cfg.suffix_tokens)
    upload_media = []
    for p in upload_root.rglob("*"):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            upload_media.append(p)

    for media in sorted(upload_media):
        # media path relative to upload root should map to root-relative if preserved;
        # matching is key-based so flatten/preserve both work.
        for j in choose_jsons_for_media(media, json_index, cfg.suffix_tokens):
            if not j.exists():
                continue
            j_rel = rel(j, cfg.root)
            if cfg.preserve_subpaths:
                dst_j = upload_root / j_rel
            else:
                dst_j = upload_root / j.name
            new_j = atomic_move_no_overwrite(j, dst_j)
            append_jsonl(move_log, {
                "run_id": run_id,
                "timestamp": now_iso(),
                "action": "move_json",
                "status": "ok",
                "json_path": j_rel,
                "media_path": str(media.relative_to(cfg.root)) if cfg.root in media.parents else str(media),
                "details": {"moved_json_to": rel(new_j, cfg.root)},
            })
            stats["json_moved_postpass"] += 1

    stats["json_total_remaining_before_postpass"] = len(json_files)
    return stats


def parse_date(s: str, end_of_day: bool = False) -> datetime:
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt


def parse_suffix_tokens(raw: str) -> List[str]:
    if not raw:
        return DEFAULT_SUFFIX_TOKENS
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(x) for x in v]
    except Exception:
        pass
    return [x.strip() for x in raw.split(",") if x.strip()]


def print_summary(summary: Counter, apply_fail_reasons: Counter, move_fail_reasons: Counter):
    print("\nSummary")
    print(f"- json_total: {summary.get('json_total', 0)}")
    print(f"- json_no_match: {summary.get('json_no_match', 0)}")
    print(f"- media_total: {summary.get('media_total', 0)}")
    print(f"- pairs_applied_ok: {summary.get('pairs_applied_ok', 0)}")
    print(f"- pairs_applied_fail: {summary.get('pairs_applied_fail', 0)}")
    print(f"- media_moved_valid: {summary.get('media_moved_valid', 0)}")
    print(f"- media_remaining_invalid: {summary.get('media_remaining_invalid', 0)}")
    print(f"- json_moved_postpass: {summary.get('json_moved_postpass', 0)}")

    def top5(c: Counter):
        return c.most_common(5)

    print("- top_apply_failure_reasons:")
    for k, v in top5(apply_fail_reasons):
        print(f"  - {k}: {v}")

    print("- top_move_failure_reasons:")
    for k, v in top5(move_fail_reasons):
        print(f"  - {k}: {v}")


def collect_failure_reasons(apply_log: Path, move_log: Path, run_id: str):
    apply_reasons = Counter()
    move_reasons = Counter()

    for row in load_jsonl(apply_log):
        if row.get("run_id") != run_id:
            continue
        if row.get("action") == "apply" and row.get("status") in {"fail", "no_match"}:
            d = row.get("details")
            key = d if isinstance(d, str) else json.dumps(d, ensure_ascii=False)
            apply_reasons[key] += 1

    for row in load_jsonl(move_log):
        if row.get("run_id") != run_id:
            continue
        if row.get("action") == "move" and row.get("status") == "invalid":
            d = row.get("details")
            if isinstance(d, dict):
                key = d.get("reason", "invalid")
            else:
                key = str(d)
            move_reasons[key] += 1

    return apply_reasons, move_reasons


def process_one_root(root: Path, args) -> dict:
    cfg = Config(
        root=root,
        valid_start=parse_date(args.valid_start),
        valid_end=parse_date(args.valid_end, end_of_day=True),
        workers=max(1, args.workers),
        preserve_subpaths=not args.flatten,
        review_invalid=args.review_invalid,
        suffix_tokens=parse_suffix_tokens(args.suffix_tokens),
    )

    run_id = str(uuid.uuid4())
    apply_log = cfg.root / "apply_log.log"
    move_log = cfg.root / "move_log.log"

    applied_ok_pairs = load_successful_apply_pairs(apply_log)
    moved_media = load_moved_media(move_log)

    media_files, json_files, media_index, _ = build_indexes(cfg.root, cfg.suffix_tokens)
    apply_stats = pass1_apply(cfg, run_id, json_files, media_index, apply_log, applied_ok_pairs)

    media_files2, _, _, json_index2 = build_indexes(cfg.root, cfg.suffix_tokens)
    move_stats = pass2_validate_move(cfg, run_id, media_files2, json_index2, move_log, moved_media)
    post_json_stats = move_json_for_already_uploaded(cfg, run_id, move_log)

    summary = Counter()
    summary.update(apply_stats)
    summary.update(move_stats)
    summary.update(post_json_stats)

    apply_fail_reasons, move_fail_reasons = collect_failure_reasons(apply_log, move_log, run_id)

    print(json.dumps({
        "run_id": run_id,
        "folder": str(cfg.root),
        "apply_log": str(apply_log),
        "move_log": str(move_log),
        "upload_ready": str(cfg.root / "_UPLOAD_READY"),
        "review_invalid": str(cfg.root / "_REVIEW_INVALID") if cfg.review_invalid else None,
    }, ensure_ascii=False))
    print_summary(summary, apply_fail_reasons, move_fail_reasons)

    return {
        "run_id": run_id,
        "folder": str(cfg.root),
        "summary": dict(summary),
    }




def print_pass_progress(pass_name: str, done: int, total: int, width: int = 20):
    if total <= 0:
        print(f"{pass_name}: [....................] 0% (0/0)")
        return
    filled = int((done / total) * width)
    bar = "o" * filled + "." * (width - filled)
    pct = int((done / total) * 100)
    end = "\n" if done >= total else "\r"
    print(f"{pass_name}: [{bar}] {pct}% ({done}/{total})", end=end, flush=True)

def render_progress(done: int, total: int, width: int = 20) -> str:
    if total <= 0:
        return "[....................] 0%"
    filled = int((done / total) * width)
    bar = "o" * filled + "." * (width - filled)
    pct = int((done / total) * 100)
    return f"[{bar}] {pct}% ({done}/{total})"

def main():
    ap = argparse.ArgumentParser(description="Fix Google Takeout folders: one Photos-from-YEAR folder or a parent containing many.")
    ap.add_argument("folder", help='Path to one "Photos from YEAR" folder OR parent containing multiple such folders')
    ap.add_argument("--valid-start", default="2000-01-01")
    ap.add_argument("--valid-end", default="2025-12-31")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--preserve-subpaths", action="store_true", default=True)
    ap.add_argument("--flatten", action="store_true", help="Move into _UPLOAD_READY flattened instead of preserving subpaths")
    ap.add_argument("--review-invalid", action="store_true", help="Move invalid files to _REVIEW_INVALID")
    ap.add_argument("--suffix-tokens", default="", help="JSON array or comma list")
    args = ap.parse_args()

    root = Path(args.folder).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"Folder not found: {root}")

    check_exiftool()

    # If given a parent, process each "Photos from YEAR" folder one by one.
    # Recursive search so parent can be above the Google Photos folder too.
    children = [d for d in root.rglob("*") if d.is_dir() and re.match(r"(?i)^photos from \d{4}$", d.name)]
    if children:
        targets = sorted(set(children))
    else:
        targets = [root]

    results = []
    total = len(targets)
    print(f"Processing {total} folder(s)")
    for t in targets:
        print(f" - {t}")
    print(render_progress(0, total))

    for i, t in enumerate(targets, start=1):
        print(f"\n=== [{i}/{total}] {t.name} ===")
        results.append(process_one_root(t, args))
        print(render_progress(i, total))

    print("\nDone all folders.")


if __name__ == "__main__":
    main()
