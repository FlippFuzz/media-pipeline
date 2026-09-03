#!/usr/bin/env bash

# Resolve paths dynamically based on script location within the media-pipeline monorepo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$SCRIPT_DIR")"

INPUT_DIR="$SCRIPT_DIR/02_transcode_queue"
OUTPUT_DIR="$SCRIPT_DIR/04_transcode_finished"
STAGING_DIR="$SCRIPT_DIR/03_transcode_staging"
UPLOAD_STAGING_DIR="$SCRIPT_DIR/01_upload_staging"
LOCK_FILE="/tmp/transcode_process.lock"
LOG_FILE="$SCRIPT_DIR/transcode.log"
FFMPEG_BIN="$SCRIPT_DIR/ffmpeg"
MAX_LOG_SIZE=5242880 # 5MB
UPDATE_INTERVAL=3600 # Check for updates once per hour (3600 seconds)
UPDATE_STAMP="/tmp/.transcode_update_stamp"

# Detect CPU architecture to determine the correct static FFmpeg build
ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]]; then
    ARCH_SUFFIX="linuxarm64"
else
    ARCH_SUFFIX="linux64"
fi
FFMPEG_URL="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-${ARCH_SUFFIX}-gpl.tar.xz"

# Detect available CPU cores, capped at 12 (defaults to 4 if detection fails)
CORES=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
if [ "$CORES" -gt 12 ]; then
    CORES=12
fi

# Ensure all pipeline staging directories exist
mkdir -p "$INPUT_DIR" "$OUTPUT_DIR" "$STAGING_DIR" "$UPLOAD_STAGING_DIR"

# 2C: Acquire process lock BEFORE inspecting or rotating logs
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "Transcode process already running. Exiting." >> "$LOG_FILE"; exit 1; }

# 2C: Log rotation: If log file exceeds MAX_LOG_SIZE, rotate it up to 3 archives
if [ -f "$LOG_FILE" ] && [ $(stat -c%s "$LOG_FILE") -gt $MAX_LOG_SIZE ]; then
    rm -f "${LOG_FILE}.3"
    [ -f "${LOG_FILE}.2" ] && mv "${LOG_FILE}.2" "${LOG_FILE}.3"
    [ -f "${LOG_FILE}.1" ] && mv "${LOG_FILE}.1" "${LOG_FILE}.2"
    mv "$LOG_FILE" "${LOG_FILE}.1"
    echo "--- Log rotated: $(date) ---" > "$LOG_FILE"
fi

# --- Self-Update Logic ---
GIT_UPDATE_LOCK_FILE="/tmp/media_pipeline_git_update.lock"
GIT_UPDATE_LOCK_TIMEOUT=60

if [ ! -f "$FFMPEG_BIN" ] || [ $(( $(date +%s) - $(stat -c %Y "$UPDATE_STAMP" 2>/dev/null || echo 0) )) -gt $UPDATE_INTERVAL ]; then
    touch "$UPDATE_STAMP"

    # 1. Update ffmpeg (BtbN builds include frequent SVT-AV1 optimizations)
    ARCHIVE_PATH="$SCRIPT_DIR/ffmpeg-master-latest-${ARCH_SUFFIX}-gpl.tar.xz"
    
    if wget -qN "$FFMPEG_URL" -P "$SCRIPT_DIR"; then
        if [ "$ARCHIVE_PATH" -nt "$FFMPEG_BIN" ]; then
            echo "--- New ffmpeg version detected. Updating binary... ---" >> "$LOG_FILE"
            tar -xf "$ARCHIVE_PATH" -C "$SCRIPT_DIR"
            mv "$SCRIPT_DIR/ffmpeg-master-latest-${ARCH_SUFFIX}-gpl/bin/ffmpeg" "$FFMPEG_BIN"
            rm -rf "$SCRIPT_DIR/ffmpeg-master-latest-${ARCH_SUFFIX}-gpl"
        fi
    fi

    # 2. Update the repository itself from Git (protected by 2B shared git update lock)
    exec 201>"$GIT_UPDATE_LOCK_FILE"
    if flock -w "$GIT_UPDATE_LOCK_TIMEOUT" 201; then
        cd "$REPO_DIR"
        if [ -d .git ]; then
            OLD_HASH=$(git rev-parse HEAD 2>/dev/null)
            git reset --hard HEAD >> "$LOG_FILE" 2>&1
            if git pull >> "$LOG_FILE" 2>&1; then
                NEW_HASH=$(git rev-parse HEAD 2>/dev/null)
                if [ "$OLD_HASH" != "$NEW_HASH" ]; then
                    chmod +x "$SCRIPT_DIR/transcode.sh" >> "$LOG_FILE" 2>&1
                    echo "--- Script updated from $OLD_HASH to $NEW_HASH. Restarting... ---" >> "$LOG_FILE"
                    flock -u 201
                    exec 201>&-
                    exec "$SCRIPT_DIR/transcode.sh" "$@"
                fi
            fi
        fi
        flock -u 201
        exec 201>&-
    else
        echo "Could not acquire git update lock within ${GIT_UPDATE_LOCK_TIMEOUT}s; skipping self-update this run." >> "$LOG_FILE"
    fi
fi

# Safety check: If ffmpeg is still missing after update attempt, exit
if [ ! -f "$FFMPEG_BIN" ]; then
    echo "ERROR: ffmpeg binary not found at $FFMPEG_BIN and update failed. Exiting." >> "$LOG_FILE"
    exit 1
fi

echo "--- Starting transcode session: $(date) ---" >> "$LOG_FILE"

for filepath in "$INPUT_DIR"/*; do
    [ -e "$filepath" ] || continue

    filename=$(basename "$filepath")

    # Ignore hidden files (rsync) and WinSCP temporary files (.filepart)
    if [[ "$filename" == .* ]] || [[ "$filename" == *.filepart ]]; then
        continue
    fi

    # Prevent processing files modified less than 10 seconds ago (active upload safeguard)
    last_mod=$(stat -c %Y "$filepath")
    if [ $(( $(date +%s) - last_mod )) -lt 10 ]; then
        continue
    fi

    echo "Processing: $filename" >> "$LOG_FILE"

    staging_path="$STAGING_DIR/${filename%.*}.mkv"
    final_path="$OUTPUT_DIR/${filename%.*}.mkv"
    echo "Using CPU threads limit: $CORES" >> "$LOG_FILE"

    input_size=$(stat -c%s "$filepath")

    # Transcode using SVT-AV1 at low process priority (nice) - removed invalid time keyword
    nice -n 18 "$FFMPEG_BIN" -stats_period 60 \
        -threads "$CORES" -i "$filepath" -c:v libsvtav1 -preset 3 -crf 28 \
        -pix_fmt yuv420p10le -svtav1-params "tune=0:scd=1:lp=$CORES:keyint=10s" -c:a libopus -b:a 128k \
        -y "$staging_path" 2>&1 | tr '\r' '\n' >> "$LOG_FILE"

    if [ ${PIPESTATUS[0]} -eq 0 ]; then
        if [ ! -s "$staging_path" ]; then
            echo "ERROR: Transcode finished but output file is missing or empty: $staging_path" >> "$LOG_FILE"
            rm -f "$staging_path"
            continue
        fi

        output_size=$(stat -c%s "$staging_path")

        # Optimization check: Discard transcode if larger than original input file
        if [ "$output_size" -ge "$input_size" ]; then
            echo "Optimizing: Transcoded file ($output_size bytes) is not smaller than original ($input_size bytes). Discarding transcode and keeping original." >> "$LOG_FILE"
            rm -f "$staging_path"
            mv "$filepath" "$OUTPUT_DIR/$filename"
            echo "Successfully moved original: $filename to output" >> "$LOG_FILE"
        else
            mv "$staging_path" "$final_path"
            echo "Successfully transcoded: $filename (Transcoded size: $output_size bytes, original: $input_size bytes)" >> "$LOG_FILE"
            rm "$filepath"
        fi
    else
        echo "ERROR: Failed to transcode: $filename. See logs for details." >> "$LOG_FILE"
    fi
done

echo "--- Session finished: $(date) ---" >> "$LOG_FILE"