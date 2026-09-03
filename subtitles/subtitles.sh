#!/usr/bin/env bash

# Verify that the target model identifier was supplied
if [ -z "$1" ]; then
    echo "Usage: $0 <model_identifier> (e.g., google-gla:gemini-3.5-flash-lite)"
    exit 1
fi

MODEL_NAME="$1"

# Resolve paths dynamically within the media-pipeline monorepo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null || dirname "$SCRIPT_DIR")"

# Create a safe identifier for locking and logging
SAFE_NAME=$(echo "$MODEL_NAME" | tr -c '[:alnum:]_' '_')
LOCK_FILE="/tmp/subtitles_${SAFE_NAME}.lock"
LOG_FILE="$SCRIPT_DIR/subtitles_${SAFE_NAME}.log"
MAX_LOG_SIZE=5242880 # 5MB
UPDATE_INTERVAL=3600 # Check for updates once per hour
UPDATE_STAMP="/tmp/.subtitles_update_stamp"

# 2C: Acquire per-model concurrency isolation via flock BEFORE rotating logs
exec 200>"$LOCK_FILE"
flock -n 200 || { echo "Subtitles runner for $MODEL_NAME already active. Exiting." >> "$LOG_FILE"; exit 1; }

# 2C: Rotate logs if larger than 5MB (only once lock is acquired)
if [ -f "$LOG_FILE" ] && [ $(stat -c%s "$LOG_FILE") -gt $MAX_LOG_SIZE ]; then
    rm -f "${LOG_FILE}.3"
    [ -f "${LOG_FILE}.2" ] && mv "${LOG_FILE}.2" "${LOG_FILE}.3"
    [ -f "${LOG_FILE}.1" ] && mv "${LOG_FILE}.1" "${LOG_FILE}.2"
    mv "$LOG_FILE" "${LOG_FILE}.1"
    echo "--- Log rotated: $(date) ---" > "$LOG_FILE"
fi

# --- Self-Update Logic ---
GIT_UPDATE_LOCK_FILE="/tmp/media_pipeline_git_update.lock"
GIT_UPDATE_LOCK_TIMEOUT=60 # seconds to wait for another model's update to finish

if [ $(( $(date +%s) - $(stat -c %Y "$UPDATE_STAMP" 2>/dev/null || echo 0) )) -gt $UPDATE_INTERVAL ]; then
    exec 201>"$GIT_UPDATE_LOCK_FILE"
    if flock -w "$GIT_UPDATE_LOCK_TIMEOUT" 201; then
        # Re-check the stamp now that we hold the lock: another model may have
        # already refreshed it while we were waiting, in which case skip.
        if [ $(( $(date +%s) - $(stat -c %Y "$UPDATE_STAMP" 2>/dev/null || echo 0) )) -gt $UPDATE_INTERVAL ]; then
            touch "$UPDATE_STAMP"

            cd "$REPO_DIR"
            if [ -d .git ]; then
                OLD_HASH=$(git rev-parse HEAD 2>/dev/null)
                git reset --hard HEAD >> "$LOG_FILE" 2>&1
                if git pull >> "$LOG_FILE" 2>&1; then
                    NEW_HASH=$(git rev-parse HEAD 2>/dev/null)
                    if [ "$OLD_HASH" != "$NEW_HASH" ]; then
                        chmod +x "$SCRIPT_DIR/subtitles.sh" >> "$LOG_FILE" 2>&1
                        echo "--- Subtitles script updated from $OLD_HASH to $NEW_HASH. Restarting... ---" >> "$LOG_FILE"
                        # 2A: Unlock and close descriptor before exec to prevent lock leak
                        flock -u 201
                        exec 201>&-
                        exec "$SCRIPT_DIR/subtitles.sh" "$@"
                    fi
                fi
            fi
        fi
        flock -u 201
        exec 201>&-
    else
        echo "Could not acquire git update lock within ${GIT_UPDATE_LOCK_TIMEOUT}s; skipping self-update this run." >> "$LOG_FILE"
    fi
fi

# 1B: Load environment variables (e.g. AISUB_AI_GOOGLE_KEY) if .env exists
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
elif [ -f "$REPO_DIR/.env" ]; then
    set -a
    source "$REPO_DIR/.env"
    set +a
fi

# Activate local virtual environment if present
if [ -f "$SCRIPT_DIR/venv/bin/activate" ]; then
    source "$SCRIPT_DIR/venv/bin/activate"
elif [ -f "$REPO_DIR/venv/bin/activate" ]; then
    source "$REPO_DIR/venv/bin/activate"
fi

echo "--- Starting subtitles run for $MODEL_NAME: $(date) ---" >> "$LOG_FILE"

# Execute the orchestrator
cd "$SCRIPT_DIR"
python3 "$SCRIPT_DIR/subtitles_runner.py" --model "$MODEL_NAME" >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

echo "--- Subtitles session finished (Exit code: $EXIT_CODE): $(date) ---" >> "$LOG_FILE"
exit $EXIT_CODE