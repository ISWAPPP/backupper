#!/bin/bash
set -euo pipefail
# ponytail: find + delete old archives. Skipped: flags, validation, orphan detection, box art. Add when the schedule changes (edit crontab).
find "${1:-/var/www/backups}" -maxdepth 1 -type f \( -name '*.tar.gz' -o -name '*.status' -o -name '*.size' \) -mtime +3 -delete -print 2>/dev/null
# ponytail: cleanup orphan incoming/ dirs — client lost Internet, crashed, or was cancelled
# but the request never reached /cancel_session. 1 day = generous upper bound for any rsync.
find "/home/backupuser/incoming" -maxdepth 1 -mindepth 1 -type d -mtime +1 -exec rm -rf {} + -prune 2>/dev/null
# Session logs in /home/backupuser/tmp-logs are retained until manually removed.
