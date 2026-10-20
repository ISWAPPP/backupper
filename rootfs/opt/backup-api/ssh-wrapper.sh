#!/bin/bash
# Forced command for ephemeral rsync upload keys. No shell evaluation of client input.
SESSION_DIR=$1
if [[ ! "$SESSION_DIR" =~ ^/home/backupuser/incoming/[0-9a-f-]{36}$ ]] || [[ ! -d "$SESSION_DIR" ]]; then
    echo "Access Denied" >&2
    exit 1
fi
# Accept only the sender-side invocation used by rsync -avz; reject --sender,
# extra paths, long options and shell syntax. The destination is server-chosen.
if [[ "$SSH_ORIGINAL_COMMAND" =~ ^rsync\ --server\ (-[A-Za-z0-9.,]+)\ \.\ \.$ ]]; then
    flags=${BASH_REMATCH[1]}
    exec /usr/bin/rsync --server "$flags" . "$SESSION_DIR/"
fi
echo "Access Denied" >&2
exit 1
