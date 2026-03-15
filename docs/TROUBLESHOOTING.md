# Troubleshooting

## `no_match` / missing JSON
- Many failures were due to media/JSON split across takeout folders.
- Use cross-folder staging/rehome before rerun.

## Junk sidecars
- Ignore/remove `._*` and `.filtered-*` JSON artifacts.
- They are not valid Takeout sidecars.

## macOS Photos import timeout
- Use AppleScript `with timeout` wrapper.
- Import in smaller batches if needed.

## Disk pressure during iCloud sync
- Gate imports by minimum free space.
- Trigger reclaim cycle (temp file create/remove), re-check, then continue.

## Videos play on Windows but not on macOS
- Check `CompressorID=vp09` via `exiftool`.
- Convert to H.264 (`libx264` + `aac`) before import.

## Windows subprocess decode errors
- Use explicit UTF-8 decode with replacement in Python subprocess calls.
