#!/bin/sh
# Starts Wokearr as an unprivileged user (PUID/PGID, default 1000:1000)
# instead of root.
#
# Existing installations need no change: data written by older versions
# belongs to root, so /data is handed over to PUID:PGID once on the first
# start. Whenever that is not possible (read-only mount, NFS with root_squash,
# ...), Wokearr keeps running as root as before and says so in the log -
# an automatic update must never leave a container that no longer starts.
#
#   PUID=0                 keep running as root (opt-out)
#   user: "1000:1000"      (compose) - already unprivileged, nothing to do here
set -u

DATA_DIR="${DATA_DIR:-/data}"

# What Wokearr itself keeps in /data. Only these are handed over - never the
# whole mount, so a folder mounted there by mistake (a media drive, a backup)
# keeps its owners.
KNOWN_PATHS="score_cache.json url_state.json rendered_state.json pushed_state.json library_index.json run_history.json session_secret originals branded"

log() {
    # Same layout as Wokearr's own log lines; one call per line
    level="$1"
    shift
    for line in "$@"; do
        printf '%s %-7s [startup] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "$line"
    done
}

RULE="=============================================================================="

as_root() {
    # Fallback: keep running as root as before. $1 tells the web UI why, so
    # it can show what to do (the only case where there is something to do)
    WOKEARR_RUN_AS_NOTICE="$1"
    export WOKEARR_RUN_AS_NOTICE
    shift
    exec setpriv --no-new-privs "$@"
}

run() {
    # --no-new-privs: nothing started from here (a setuid binary, say) can
    # gain privileges, whether or not the compose file says so. The bounding
    # set is emptied as well where the kernel lets us (needs CAP_SETPCAP).
    # HOME: plexapi and gunicorn look for files under ~ - root's home is not
    # readable for the unprivileged user
    bounding=""
    if setpriv --bounding-set=-all true 2>/dev/null; then
        bounding="--bounding-set=-all"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups --no-new-privs $bounding env HOME=/tmp "$@"
}

# True if /data itself or any of Wokearr's own files in it belong to someone
# other than PUID:PGID
needs_handover() {
    [ -n "$(find "$DATA_DIR" -maxdepth 0 \( ! -user "$PUID" -o ! -group "$PGID" \) -print 2>/dev/null)" ] && return 0
    for name in $KNOWN_PATHS; do
        [ -e "$DATA_DIR/$name" ] || continue
        [ -n "$(find "$DATA_DIR/$name" \( ! -user "$PUID" -o ! -group "$PGID" \) -print -quit 2>/dev/null)" ] && return 0
    done
    return 1
}

# chown -h: a symlink is changed itself, never what it points to
hand_over() {
    chown -h "$PUID:$PGID" "$DATA_DIR" 2>/dev/null || return 1
    for name in $KNOWN_PATHS; do
        [ -e "$DATA_DIR/$name" ] || continue
        chown -hR "$PUID:$PGID" "$DATA_DIR/$name" 2>/dev/null || return 1
    done
    return 0
}

# Started with "user:"/--user or under rootless Podman with a mapped user:
# privileges are already dropped, nothing left to do
if [ "$(id -u)" != "0" ]; then
    exec setpriv --no-new-privs "$@"
fi

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

case "$PUID$PGID" in
    *[!0-9]*|"")
        log WARNING "PUID/PGID must be numbers (got PUID=$PUID PGID=$PGID) - running as root."
        as_root invalid_ids "$@"
        ;;
esac

if [ "$PUID" = "0" ]; then
    exec setpriv --no-new-privs "$@"
fi

mkdir -p "$DATA_DIR" 2>/dev/null

# Hand Wokearr's files over if any of them belong to someone else -
# typically the first start after an update, when everything still belongs
# to root
if needs_handover; then
    # Update from a version that ran as root: /data itself still belongs to
    # root and holds data. (Fresh install: empty. Later: only single files,
    # e.g. written by "docker exec" as root - quietly handed back.)
    upgrade=""
    if [ "$(stat -c %u "$DATA_DIR")" = "0" ] && [ -n "$(find "$DATA_DIR" -mindepth 1 -print -quit 2>/dev/null)" ]; then
        upgrade=1
    fi
    if hand_over; then
        if [ -n "$upgrade" ]; then
            log WARNING "$RULE" \
                "CHANGE: Wokearr no longer runs as root, but as user $PUID:$PGID." \
                "Wokearr's data in $DATA_DIR has been handed over to that user (one-time)." \
                "Nothing to do - everything keeps working as before." \
                "Different user wanted? Set PUID/PGID. Keep running as root: PUID=0." \
                "$RULE"
        fi
    else
        log WARNING "$RULE" \
            "Wokearr now runs as user $PUID:$PGID instead of root, but could not" \
            "hand $DATA_DIR over to it (read-only or network mount?)." \
            "Running as root as before - nothing breaks. To fix: give $DATA_DIR to" \
            "$PUID:$PGID on the host, or set PUID/PGID to its owner, or PUID=0 to" \
            "keep root and silence this message." \
            "$RULE"
        as_root chown_failed "$@"
    fi
fi

# Last check before dropping root: can the user actually write there?
if ! setpriv --reuid="$PUID" --regid="$PGID" --clear-groups test -w "$DATA_DIR" 2>/dev/null; then
    log WARNING "$RULE" \
        "User $PUID:$PGID cannot write to $DATA_DIR - running as root as before." \
        "To fix: set PUID/PGID to the owner of $DATA_DIR, or PUID=0 to keep root" \
        "and silence this message." \
        "$RULE"
    as_root not_writable "$@"
fi

log INFO "Running as user $PUID:$PGID (PUID/PGID; PUID=0 runs as root)."
run "$@"
