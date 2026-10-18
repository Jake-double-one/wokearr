#!/bin/sh
# Starts the built image the way users run it and checks who Wokearr runs as:
# an update from a version that ran as root, a restart, PUID=0, user: and a
# read-only mount (fallback to root + web UI notice). Run by CI after the
# image build:
#
#   sh tests/container_test.sh wokearr:ci
set -eu

IMAGE="${1:?usage: container_test.sh IMAGE}"
WORK="$(mktemp -d)"
NAME="wokearr-container-test"
SUDO=""
[ "$(id -u)" = "0" ] || SUDO="sudo"
FAILED=0

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    $SUDO rm -rf "$WORK"
}
trap cleanup EXIT

fail() {
    echo "FAIL: $1"
    docker logs "$NAME" 2>&1 | sed 's/^/    /'
    FAILED=1
}

# start NAME_SUFFIX DOCKER_RUN_ARGS... - runs the container, waits for /healthz
start() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --security-opt no-new-privileges:true "$@" "$IMAGE" >/dev/null
    i=0
    until docker exec "$NAME" python -c "import urllib.request; urllib.request.urlopen('http://localhost:5005/healthz')" >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -ge 30 ]; then
            fail "container did not become healthy"
            return 1
        fi
        sleep 1
    done
}

# Real user of the app process (PID 1 after the entrypoint's exec)
app_uid() {
    docker exec "$NAME" awk '/^Uid:/ {print $2}' /proc/1/status
}

expect_uid() {
    uid="$(app_uid)"
    [ "$uid" = "$1" ] || fail "$2: app runs as uid $uid, expected $1"
}

expect_log() {
    docker logs "$NAME" 2>&1 | grep -q -- "$1" || fail "$2: log lacks '$1'"
}

expect_no_log() {
    if docker logs "$NAME" 2>&1 | grep -q -- "$1"; then fail "$2: log unexpectedly has '$1'"; fi
}

page() {
    docker exec "$NAME" python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:5005/').read().decode())"
}

echo "== update from a version that ran as root"
DATA="$WORK/data"
$SUDO mkdir -p "$DATA/originals" "$DATA/branded"
echo '{}' | $SUDO tee "$DATA/score_cache.json" >/dev/null
echo 'secret' | $SUDO tee "$DATA/session_secret" >/dev/null
$SUDO chmod 600 "$DATA/session_secret"
$SUDO chown -R 0:0 "$DATA"
if start -v "$DATA:/data"; then
    expect_uid 1000 "update"
    expect_log "CHANGE: Wokearr no longer runs as root" "update"
    owner="$($SUDO stat -c %u:%g "$DATA/session_secret")"
    [ "$owner" = "1000:1000" ] || fail "update: session_secret belongs to $owner"
    page | grep -q "system-notice" && fail "update: web UI shows a notice although nothing is to do"
fi

echo "== restart"
if start -v "$DATA:/data"; then
    expect_uid 1000 "restart"
    expect_no_log "CHANGE:" "restart"
fi

echo "== fresh named volume"
docker volume rm -f wokearr-container-test >/dev/null 2>&1 || true
if start -v wokearr-container-test:/data; then
    expect_uid 1000 "fresh"
    expect_no_log "CHANGE:" "fresh"
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker volume rm -f wokearr-container-test >/dev/null 2>&1 || true

echo "== PUID=0"
if start -e PUID=0 -v "$DATA:/data"; then
    expect_uid 0 "PUID=0"
fi

echo "== user: 1000:1000"
if start --user 1000:1000 -v "$DATA:/data"; then
    expect_uid 1000 "user:"
fi

echo "== read-only mount that can't be handed over"
$SUDO chown -R 0:0 "$DATA"
if start -v "$DATA:/data:ro"; then
    expect_uid 0 "read-only"
    expect_log "could not" "read-only"
    page | grep -q "system-notice" || fail "read-only: web UI shows no notice"
fi

[ "$FAILED" = "0" ] && echo "All container checks passed."
exit "$FAILED"
