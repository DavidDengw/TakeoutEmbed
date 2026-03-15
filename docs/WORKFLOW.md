# Workflow

## A) Embed pass

```bash
python3 scripts/embed/takeout_metadata_embed.py "/path/to/Google Photos" --workers 4
```

Optional cross-folder staging + rerun:

```bash
python3 scripts/embed/takeout_metadata_embed.py "/path/to/root" --crossfolder --crossfolder-root "/path/to/root"
```

## B) Import pass (macOS)

```bash
scripts/import/upload_icloud_photos_range.sh
```

- Imports only `_UPLOAD_READY`
- Per-import timeout set
- Disk free-space gate before each import

## C) VP9 remediation

```bash
python3 scripts/convert/convert_vp9_to_h264_crossplatform.py "/path/to/root" --dry-run
python3 scripts/convert/convert_vp9_to_h264_crossplatform.py "/path/to/root"
```
