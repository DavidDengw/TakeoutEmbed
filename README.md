# TakeoutEmbed

Toolkit for recovering fragmented Google Takeout exports and preparing clean uploads to Apple Photos/iCloud.

## What this project solves

- Media and JSON sidecars split across many takeout archives
- Metadata embed failures (`no_match`, missing sidecars, junk sidecars)
- Staging chaos (`_UPLOAD_READY` at multiple levels)
- macOS import issues (AppleEvent timeout, disk pressure)
- Codec incompatibility (VP9 videos fail in QuickTime/Photos)

## Main workflow

1. Run metadata embed (`scripts/embed/takeout_metadata_embed.py`)
2. (Optional) run cross-folder recovery and rerun embed
3. Clean staging structure (single canonical `_UPLOAD_READY` per year)
4. Import to Photos with disk-space gate (`scripts/import/upload_icloud_photos_range.sh`)
5. Convert VP9 to H.264 when needed (`scripts/convert/...`)

See `docs/WORKFLOW.md`.

## Requirements

- Python 3.9+
- `exiftool`
- `ffmpeg` (for video conversion)
- macOS Photos automation uses `osascript`

## Quick start

```bash
# 1) Embed local sidecars
python3 scripts/embed/takeout_metadata_embed.py "/path/to/root" --workers 4

# 2) Optional cross-folder recovery + rerun
python3 scripts/embed/takeout_metadata_embed.py "/path/to/root" --crossfolder --crossfolder-root "/path/to/root"

# 3) macOS batch import
bash scripts/import/upload_icloud_photos_range.sh
```

## Scripts

- `scripts/embed/takeout_metadata_embed.py` — core embed/validate/move pipeline
- `scripts/import/upload_icloud_photos_range.sh` — range import into Photos with free-space gate
- `scripts/convert/convert_vp9_to_h264_crossplatform.py` — safe VP9->H.264 conversion
- `scripts/utilities/find_and_copy_from_csv.py` — CSV file finder/copy helper
- `scripts/windows/*.ps1` — Windows helpers

## Project status

Pipeline was used to process large multi-part Takeout sets, recover many cross-folder matches, and isolate true hard-failure remainders for focused diagnostics.
