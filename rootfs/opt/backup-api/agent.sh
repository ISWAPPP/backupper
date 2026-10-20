#!/bin/bash
# backup-agent — rsync + archive on server, then cleanup

set -eE
set -o pipefail

# ponytail: reset terminal in case previous run left it raw
TTY_SAVED=$(stty -g 2>/dev/null || true)
stty sane 2>/dev/null || true

_pick_directory() {
    local dir
    read -e -r -p "Directory to back up: " dir
    PICKED_DIR="$dir"
}

VPS_IP="@@PUBLIC_HOST@@"
# ponytail: no DNS/domain dependency — same base the script itself is fetched from
API_BASE="https://${VPS_IP}"
TMP_DIR=$(mktemp -d -t backup-XXXXXXXX)
chmod 700 "$TMP_DIR"
LOG_FILE="${TMP_DIR}/agent.log"
# Pin the backup VPS SSH host key, rather than trusting whichever host answers.
printf '%s\n' '@@PUBLIC_HOST@@ @@SSH_HOST_KEY@@' > "$TMP_DIR/known_hosts"
START_EPOCH=$(date +%s)

log() {
    local level="$1" msg="$2"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [$(hostname)] [${level}] ${msg}" >> "$LOG_FILE"
}

cleanup() {
    local rc=$?
    stty "$TTY_SAVED" 2>/dev/null || true
    rm -rf "$TMP_DIR"
    exit $rc
}
trap cleanup EXIT

log "INFO" "Start, host: $(hostname), user: ${USER}"

# 1. directory — interactive picker with live suggestions (arrow up/down)
_pick_directory || exit 1
SRC_DIR="$PICKED_DIR"
SRC_DIR="${SRC_DIR/#\~/$HOME}"
SRC_DIR=$(realpath -m "$SRC_DIR" 2>/dev/null || echo "$SRC_DIR")

if [ ! -d "$SRC_DIR" ]; then
    echo "Not a directory"
    log "ERROR" "Bad dir: ${SRC_DIR}"
    exit 1
fi

echo "  Calculating directory size (large trees may take several minutes)..."
DIR_BYTES=$(du -sb "$SRC_DIR" 2>/dev/null | cut -f1) || {
    echo "Could not calculate directory size" >&2
    exit 1
}
DIR_SIZE=$(numfmt --to=iec --suffix=B "$DIR_BYTES")
echo "  $(basename "$SRC_DIR"): ${DIR_SIZE}"
log "INFO" "Source: ${SRC_DIR} (${DIR_BYTES} bytes)"

# 1.5 check disk space on VPS
SPACE_RESP=$(curl -sf --connect-timeout 10 "${API_BASE}/disk_space" 2>/dev/null) || {
    echo "  WARN: cannot reach VPS, skipping space check"
    log "WARN" "disk_space endpoint unreachable"
}
if [ -n "$SPACE_RESP" ]; then
    total= used= free=
    while IFS= read -r line; do
        if [[ "$line" =~ ^total=[0-9]+$ ]]; then total=${line#total=}
        elif [[ "$line" =~ ^used=[0-9]+$ ]]; then used=${line#used=}
        elif [[ "$line" =~ ^free=[0-9]+$ ]]; then free=${line#free=}
        else echo "Invalid disk response" >&2; exit 1
        fi
    done <<< "$SPACE_RESP"
    if [[ -z "$total" || -z "$used" || -z "$free" || "$total" == 0 ]]; then
        echo "Incomplete disk response" >&2; exit 1
    fi
    FREE_GB=$(awk "BEGIN { printf \"%.1f\", $free / 1073741824 }" 2>/dev/null)
    TOTAL_GB=$(awk "BEGIN { printf \"%.1f\", $total / 1073741824 }" 2>/dev/null)
    PCT=$(( (total - free) * 100 / total ))
    NEED=$(( DIR_BYTES * 2 ))

    # disk bar: █=used, ▒=backup overlay, ░=free
    W=30
    FILL=$(( PCT * W / 100 ))
    OVERLAY=$(( NEED * W / total ))
    [ "$OVERLAY" -lt 1 ] && OVERLAY=1
    OVERLAY_END=$(( FILL + OVERLAY ))
    [ "$OVERLAY_END" -gt "$W" ] && OVERLAY_END=$W
    BAR=
    for ((i=0; i<FILL; i++)); do BAR="${BAR}█"; done
    for ((i=FILL; i<OVERLAY_END; i++)); do BAR="${BAR}▒"; done
    for ((i=OVERLAY_END; i<W; i++)); do BAR="${BAR}░"; done

    NEED_GB=$(awk "BEGIN { printf \"%.1f\", $NEED / 1073741824 }" 2>/dev/null)
    echo "  VPS: [${BAR}] ${PCT}% used  ${FREE_GB}G free / ${TOTAL_GB}G"
    echo "        ─ ${NEED_GB}G (files + archive)"

    # ponytail: peak = 2x source (incoming + archive before incoming cleanup)
    # refuse if used + 2x_source >= 90% of total
    AFTER=$(( used + NEED ))
    AFTER_PCT=$(( AFTER * 100 / total ))
    if [ "$AFTER_PCT" -ge 90 ]; then
        echo "  ERROR: insufficient VPS disk space"
        echo "  Need ${NEED_GB}G, only ${FREE_GB}G free — would use ${AFTER_PCT}% (limit 90%)"
        log "ERROR" "Disk space: ${PCT}% used, need ${NEED_GB}G, free ${FREE_GB}G, would exceed 90%"
        exit 1
    fi
fi

# 2. auth
ssh-keygen -t ed25519 -N "" -f "$TMP_DIR/id_ed25519" -q
SESSION_ID=$(curl -sf --connect-timeout 10 -X POST "${API_BASE}/register_key" \
    --data-urlencode "key=$(cat "$TMP_DIR/id_ed25519.pub")")

if [ -z "$SESSION_ID" ] || [[ "$SESSION_ID" == *"detail"* ]]; then
    echo "Auth failed"
    log "ERROR" "Key reg fail: ${SESSION_ID}"
    exit 1
fi

log "INFO" "Session: ${SESSION_ID}"

# 3. sync with live window
rsync_tail_window() {
    local line count=0 percent='' last_shown='' status
    local interactive=0
    [ -t 1 ] && interactive=1
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == "sending incremental file list" ]] && continue
        if [[ "$line" =~ ^[[:space:]]*[0-9,]+[[:space:]]+([0-9]+)% ]]; then
            percent=${BASH_REMATCH[1]}
            status="Sync: ${percent}%"
            # Print only when the percentage changes; no rapid frame redraws.
            if [[ "$status" != "$last_shown" ]]; then
                if (( interactive )); then
                    printf '\r\033[2K%s' "$status"
                else
                    printf '%s\n' "$status"
                fi
                last_shown=$status
            fi
        else
            count=$((count + 1))
        fi
    done
    if (( interactive )); then printf '\r\033[2KSync transfer ended\n'; fi
    echo "$count" > "$TMP_DIR/.rsync_count"
}

RSYNC_START=$(date +%s)
set +e
rsync -avz --info=progress2 \
    -e "ssh -i $TMP_DIR/id_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$TMP_DIR/known_hosts -o LogLevel=ERROR" \
    "$SRC_DIR/" "backupuser@${VPS_IP}:." 2>&1 | tr '\r' '\n' | rsync_tail_window
RSYNC_STATUS=("${PIPESTATUS[@]}")
set -e
RSYNC_EXIT=${RSYNC_STATUS[0]}
if [ "$RSYNC_EXIT" -eq 0 ] && { [ "${RSYNC_STATUS[1]}" -ne 0 ] || [ "${RSYNC_STATUS[2]}" -ne 0 ]; }; then RSYNC_EXIT=1; fi
RSYNC_END=$(date +%s)
RSYNC_COUNT=$(cat "$TMP_DIR/.rsync_count" 2>/dev/null || echo 0)

if [ "$RSYNC_EXIT" -ne 0 ]; then
    echo "rsync error ($RSYNC_EXIT)"
    log "ERROR" "rsync fail, exit $RSYNC_EXIT, $((RSYNC_END - RSYNC_START))s"
    curl -sf --connect-timeout 5 -X POST "${API_BASE}/cancel_session" \
        --data-urlencode "session_id=${SESSION_ID}" >/dev/null 2>&1 || true
    log "INFO" "Session ${SESSION_ID} cancelled — leftover files removed"
    exit 1
fi

log "INFO" "rsync: ${RSYNC_COUNT} files, $((RSYNC_END - RSYNC_START))s"

# 5. Submit the temporary log to the backup VPS; EXIT trap removes local copy.
log "INFO" "rsync completed; requesting archive"
DOWNLOAD_URL=$(curl -sf --connect-timeout 10 -X POST "${API_BASE}/create_archive" \
    --data-urlencode "session_id=${SESSION_ID}" \
    --data-urlencode "log@${LOG_FILE}")
echo "Download: ${DOWNLOAD_URL}"
