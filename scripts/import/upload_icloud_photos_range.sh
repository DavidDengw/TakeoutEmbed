#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="${BASE_DIR:-}"
PREFIX="${PREFIX:-takeout-YYYYMMDDThhmmssZ-part-}"
START="${START:-1}"
END="${END:-1}"

if [[ -z "$BASE_DIR" ]]; then
  echo "Set BASE_DIR env var (example: BASE_DIR=/path/to/TakeoutFiles)"
  exit 1
fi

# If you want to test without importing, run:
#   DRY_RUN=1 ./upload_icloud_photos_range.sh
DRY_RUN="${DRY_RUN:-0}"
MIN_FREE_GB="${MIN_FREE_GB:-29}"
CHECK_PATH="${CHECK_PATH:-$HOME/Pictures}"
WAIT_SECONDS="${WAIT_SECONDS:-1800}"

pad3() {
  printf "%03d" "$1"
}

free_gb() {
  local path="$1"
  # df -Pk gives 1K blocks; available is column 4
  local avail_kb
  avail_kb=$(df -Pk "$path" | awk 'NR==2 {print $4}')
  awk -v kb="$avail_kb" 'BEGIN {printf "%.1f", kb/1024/1024}'
}

reclaim_space() {
  local path="$1"
  local tmp="$path/.icloud_reclaim_$$.tmp"

  # Try large sparse allocation first; if it fails, try smaller.
  if mkfile 20g "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    return 0
  fi
  if mkfile 10g "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    return 0
  fi
  if mkfile 5g "$tmp" 2>/dev/null; then
    rm -f "$tmp"
    return 0
  fi

  rm -f "$tmp" 2>/dev/null || true
  return 1
}

wait_for_space() {
  local need_gb="$1"
  local path="$2"
  while true; do
    local cur after
    cur=$(free_gb "$path")

    if awk -v cur="$cur" -v need="$need_gb" 'BEGIN {exit !(cur+0 >= need+0)}'; then
      echo "[SPACE] OK: free=${cur} GB (need >= ${need_gb} GB) at ${path}"
      break
    fi

    echo "[SPACE] Low disk: free=${cur} GB < ${need_gb} GB. Triggering reclaim via tempfile..."
    if reclaim_space "$path"; then
      after=$(free_gb "$path")
      if awk -v cur="$after" -v need="$need_gb" 'BEGIN {exit !(cur+0 >= need+0)}'; then
        echo "[SPACE] Reclaim succeeded: free=${after} GB (need >= ${need_gb} GB)"
        break
      fi
      echo "[SPACE] After reclaim free=${after} GB still below ${need_gb} GB. Waiting ${WAIT_SECONDS}s..."
      sleep "$WAIT_SECONDS"
    else
      echo "[SPACE] Reclaim trigger failed; waiting ${WAIT_SECONDS}s..."
      sleep "$WAIT_SECONDS"
    fi
  done
}

import_folder() {
  local folder="$1"
  if [[ "$DRY_RUN" == "1" ]]; then
    echo "[DRY_RUN] Would import: $folder"
    return 0
  fi

  # Photos returns imported media item references; suppress noisy stdout.
  osascript <<APPLESCRIPT >/dev/null
  with timeout of 7200 seconds
    tell application "Photos"
      import POSIX file "$folder"
    end tell
  end timeout
APPLESCRIPT
}

echo "Base: $BASE_DIR"
echo "Range: ${START}-${END}"
echo "Disk check path: $CHECK_PATH"
echo "Min free space required: ${MIN_FREE_GB} GB"

for i in $(seq "$START" "$END"); do
  suffix="$(pad3 "$i")"
  takeout_dir="$BASE_DIR/${PREFIX}${suffix}"

  if [[ ! -d "$takeout_dir" ]]; then
    echo "[SKIP] Missing takeout folder: $takeout_dir"
    continue
  fi

  # Only import folders named _UPLOAD_READY inside this takeout folder
  while IFS= read -r upload_ready; do
    if [[ -d "$upload_ready" ]]; then
      wait_for_space "$MIN_FREE_GB" "$CHECK_PATH"
      echo "[IMPORT] $upload_ready"
      import_folder "$upload_ready"
    fi
  done < <(find "$takeout_dir" -type d -name "_UPLOAD_READY" | sort)
done

echo "Done."
