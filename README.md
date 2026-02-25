# TakeoutEmbed

Process Google Takeout media folders and embed JSON sidecar metadata into media files, then move valid results into `_UPLOAD_READY`.

## What it does

For each `Photos from YYYY` folder (or a single year folder):

1. **Pass 1 (apply)**
   - Reads sidecar `.json` files
   - Fuzzy-matches JSON to media filenames
   - Writes timestamp + location metadata into media using `exiftool`

2. **Pass 2 (validate + move)**
   - Reads effective media timestamp from metadata
   - If date is valid (default: `2000-01-01` to `2025-12-31`), moves media to `_UPLOAD_READY`
   - Moves matching JSON files alongside moved media

3. **Resumable logs**
   - `apply_log.log`
   - `move_log.log`

## Requirements

- Python 3.9+
- `exiftool` installed and available in PATH

macOS install:

```bash
brew install exiftool
```

## Usage

### Process one year folder

```bash
python3 takeout_metadata_embed.py "/path/to/Google Photos/Photos from 2014"
```

### Process a parent folder containing many `Photos from YYYY` folders

```bash
python3 takeout_metadata_embed.py "/path/to/Google Photos"
```

### Useful flags

```bash
python3 takeout_metadata_embed.py "/path/to/Google Photos" \
  --workers 4 \
  --valid-start 2000-01-01 \
  --valid-end 2025-12-31 \
  --preserve-subpaths
```

Optional:
- `--flatten` (instead of preserving subpaths in `_UPLOAD_READY`)
- `--review-invalid` (move invalid files to `_REVIEW_INVALID`)
- `--suffix-tokens '["-edited", "copy", "duplicate"]'`

## Output

Inside each processed `Photos from YYYY` folder:

- `_UPLOAD_READY/`
- `apply_log.log`
- `move_log.log`
- optionally `_REVIEW_INVALID/`

## Notes

- Script uses only Python standard library + external `exiftool`.
- No cloud/API/LLM dependency at runtime.
