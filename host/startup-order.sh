#!/bin/sh
# Ordered stop/start of Docker containers by their `startup-order` label (ADR-0003).
#
# Registered as TrueNAS Init/Shutdown Scripts (System > Advanced), never as OS config:
#   SHUTDOWN: sh /mnt/luna/homeops/bin/startup-order.sh stop
#   POSTINIT: sh /mnt/luna/homeops/bin/startup-order.sh start
#
#   stop   record running containers, then stop them level 3 -> 0
#   start  detach and run `run` in the background (keeps the POSTINIT task short)
#   run    wait for Docker, then bring targets up level 0 -> 3, waiting for health
#   plan   print what `run` would do without changing anything
#
# Levels: 0 edge, 1 Datastores and monitoring core, 2 Exposed Services, 3 sidecars.
# Unlabeled containers are treated as level 2; `startup-order=skip` is never touched.
set -u

BASE=${STARTUP_ORDER_DIR:-/mnt/luna/homeops/startup}
STATE=$BASE/state
LOG=$BASE/last.log
LOCK=$BASE/lock
LABEL=startup-order
DEFAULT_LEVEL=2
LEVEL_TIMEOUT=${STARTUP_ORDER_LEVEL_TIMEOUT:-420}
DOCKER_WAIT=${STARTUP_ORDER_DOCKER_WAIT:-900}
SETTLE=${STARTUP_ORDER_SETTLE:-20}
SETTLE_MAX=${STARTUP_ORDER_SETTLE_MAX:-180}
DRY=0

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S%z') $*"; }

# name<TAB>level for every container, running or not.
inventory() {
    docker ps -a --format "{{.Names}}	{{.Label \"$LABEL\"}}" 2>/dev/null |
        awk -F'\t' -v d="$DEFAULT_LEVEL" '$2 == "skip" { next } { l = $2; if (l !~ /^[0-3]$/) l = d; print $1 "\t" l }'
}

running() { docker ps --format '{{.Names}}' 2>/dev/null | sort; }

# Names from $1 (newline list) whose level is $2.
at_level() {
    inventory | WANT=$1 awk -F'\t' -v lvl="$2" '
        BEGIN { n = split(ENVIRON["WANT"], w, "\n"); for (i = 1; i <= n; i++) if (w[i] != "") t[w[i]] = 1 }
        $2 == lvl && ($1 in t) { print $1 }'
}

# "ready" once running and (if it has a healthcheck) healthy.
ready() {
    docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null |
        awk '$1 == "running" && ($2 == "healthy" || $2 == "none") { ok = 1 } END { exit !ok }'
}

cmd_stop() {
    mkdir -p "$BASE"
    # Record first: dockerd may be stopping in parallel with this task. Merge with any
    # state left by a boot run that never finished, so a second shutdown loses nothing.
    { running; cat "$STATE" 2>/dev/null; } | sed '/^$/d' | sort -u > "$STATE.tmp"
    if [ -s "$STATE.tmp" ]; then mv "$STATE.tmp" "$STATE"; else rm -f "$STATE.tmp"; fi
    {
        log "stop: recorded $(cat "$STATE" 2>/dev/null | wc -l) containers"
        for lvl in 3 2 1 0; do
            names=$(at_level "$(running)" "$lvl" | tr '\n' ' ')
            [ -n "$names" ] || continue
            log "stop: level $lvl: $names"
            # shellcheck disable=SC2086
            docker stop $names >/dev/null 2>&1 || log "stop: level $lvl: docker stop reported errors (daemon may be stopping)"
        done
        log "stop: done"
    } >> "$BASE/shutdown.log" 2>&1
}

wait_docker() {
    i=0
    until docker info >/dev/null 2>&1; do
        i=$((i + 5)); [ "$i" -ge "$DOCKER_WAIT" ] && return 1; sleep 5
    done
}

# Wait until dockerd's own restart-policy starts stop changing the running set.
wait_settle() {
    prev=$(running); quiet=0; total=0
    while [ "$quiet" -lt "$SETTLE" ] && [ "$total" -lt "$SETTLE_MAX" ]; do
        sleep 5; quiet=$((quiet + 5)); total=$((total + 5))
        cur=$(running)
        [ "$cur" = "$prev" ] || { quiet=0; prev=$cur; }
    done
}

cmd_run() {
    mkdir -p "$BASE"
    if ! mkdir "$LOCK" 2>/dev/null; then log "run: another run holds $LOCK"; return 1; fi
    trap 'rmdir "$LOCK" 2>/dev/null' EXIT INT TERM
    log "run: waiting for Docker"
    wait_docker || { log "run: Docker not ready after ${DOCKER_WAIT}s, giving up"; return 1; }
    [ "$DRY" = 1 ] || wait_settle
    now=$(running)
    saved=$(cat "$STATE" 2>/dev/null)
    # Names recorded at shutdown that no longer exist (e.g. --rm containers) are dropped by at_level.
    targets=$(printf '%s\n%s\n' "$now" "$saved" | sed '/^$/d' | sort -u)
    log "run: $(printf '%s\n' "$targets" | grep -c .) targets ($(printf '%s\n' "$now" | grep -c .) already running, state file: $([ -n "$saved" ] && echo yes || echo no))"

    # Levels 0 and 1 have no upstream dependencies; restart only what sits above them.
    for lvl in 3 2; do
        names=$(at_level "$now" "$lvl" | tr '\n' ' ')
        [ -n "$names" ] || continue
        log "run: stopping level $lvl for reorder: $names"
        # shellcheck disable=SC2086
        [ "$DRY" = 1 ] || docker stop $names >/dev/null 2>&1 || log "run: stop errors at level $lvl"
    done

    for lvl in 0 1 2 3; do
        names=$(at_level "$targets" "$lvl" | tr '\n' ' ')
        [ -n "$names" ] || continue
        log "run: starting level $lvl: $names"
        [ "$DRY" = 1 ] && continue
        # shellcheck disable=SC2086
        docker start $names >/dev/null 2>&1 || log "run: start errors at level $lvl"
        waited=0
        while :; do
            pending=""
            for n in $names; do ready "$n" || pending="$pending $n"; done
            [ -z "$pending" ] && { log "run: level $lvl ready after ${waited}s"; break; }
            if [ "$waited" -ge "$LEVEL_TIMEOUT" ]; then
                log "run: WARNING level $lvl not ready after ${LEVEL_TIMEOUT}s:$pending; continuing"
                break
            fi
            sleep 5; waited=$((waited + 5))
        done
    done

    [ "$DRY" = 1 ] || rm -f "$STATE"
    log "run: done"
}

case "${1:-}" in
    stop) cmd_stop ;;
    start)
        mkdir -p "$BASE"
        setsid sh "$0" run > "$LOG" 2>&1 < /dev/null &
        ;;
    run) cmd_run ;;
    plan) DRY=1; LOCK=$BASE/lock.plan; cmd_run ;;
    *) echo "usage: $0 stop|start|run|plan" >&2; exit 2 ;;
esac
