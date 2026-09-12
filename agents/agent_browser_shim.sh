#!/bin/bash
# AGENT_BROWSER_SHIM_MARKER — do not remove; the shim uses it to recognise
# itself when resolving the real binary.
#
# agent-browser front door for agent sessions.
#
# Installed ahead of the upstream binary on PATH (/usr/local/bin in the
# session image). Every invocation passes through here and, except for the
# verbs below, is handed unchanged to the real agent-browser. What it adds:
#
#   * an idle limit by default — AGENT_BROWSER_IDLE_TIMEOUT_MS is exported
#     (15 min unless already set) so the upstream daemon closes Chrome and
#     exits on its own once the session stops issuing commands;
#   * an open guard — `open` refuses to start a second browser while another
#     session name is still alive. Reuse it with --session <name>, run
#     alongside with --new, or close the others first with --replace.
#     AGENT_BROWSER_ALLOW_MANY=1 disables the guard (test suites that
#     legitimately run one browser per worker set it);
#   * a sentinel per daemon — a detached watcher that notices when the daemon
#     exits without an explicit `close` (idle limit or crash), reaps any
#     Chrome tree left behind, and posts one task notification to the
#     session through the dashboard so the agent knows the browser is gone;
#   * `agent-browser ps`   — live sessions, Chrome process counts, RSS, idle
#                            time, plus orphaned Chrome trees no daemon owns;
#   * `agent-browser reap` — kill orphaned Chrome trees (default), sessions
#                            idle longer than --idle <minutes>, or --all.
#
# Why a daemon can leave Chrome behind: the daemon is Chrome's parent. When
# the daemon dies without closing (OOM, kill -9, harness teardown) the whole
# Chrome tree reparents to PID 1 and keeps its memory and inotify instances,
# while `session list` and `doctor` both report nothing. Measured 2026-09-12:
# one about:blank browser is ~1.2 GB RSS across 15 processes.
#
# State lives beside the upstream sidecar files (<name>.pid / .sock):
#   <name>.last      mtime = last command through the shim (idle clock)
#   <name>.closed    marker: the session was closed on purpose; stay quiet
#   <name>.sentinel  "<sentinel pid> <daemon pid>" of the running watcher
#   janitor.log      what the sentinel and reap did, with timestamps
set -u

: "${AGENT_BROWSER_IDLE_TIMEOUT_MS:=900000}"
export AGENT_BROWSER_IDLE_TIMEOUT_MS
SENTINEL_POLL_S="${AGENT_BROWSER_SENTINEL_POLL_S:-5}"
PROFILE_PREFIX="/tmp/agent-browser-chrome-"

STATE_DIR="${AGENT_BROWSER_SOCKET_DIR:-}"
if [ -z "$STATE_DIR" ] && [ -n "${XDG_RUNTIME_DIR:-}" ]; then
    STATE_DIR="$XDG_RUNTIME_DIR/agent-browser"
fi
STATE_DIR="${STATE_DIR:-$HOME/.agent-browser}"

_self="$(readlink -f "${BASH_SOURCE[0]}")"

# ── Real binary ────────────────────────────────────────────────────────
REAL="${AGENT_BROWSER_REAL:-}"
if [ -z "$REAL" ]; then
    while IFS= read -r cand; do
        [ "$(readlink -f "$cand" 2>/dev/null)" = "$_self" ] && continue
        head -c 400 "$cand" 2>/dev/null | grep -q AGENT_BROWSER_SHIM_MARKER && continue
        REAL="$cand"
        break
    done < <(type -aP agent-browser 2>/dev/null)
fi
if [ -z "$REAL" ]; then
    echo "agent-browser shim: the real agent-browser binary is not on PATH" >&2
    exit 127
fi

# ── Helpers ────────────────────────────────────────────────────────────
log() {
    mkdir -p "$STATE_DIR" 2>/dev/null
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$STATE_DIR/janitor.log" 2>/dev/null
}

is_daemon_pid() {  # is $1 a live agent-browser daemon process?
    local pid="$1" exe comm
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    exe="$(readlink -f "/proc/$pid/exe" 2>/dev/null)"
    case "$exe" in *agent-browser*) return 0 ;; esac
    comm="$(cat "/proc/$pid/comm" 2>/dev/null)"
    case "$comm" in agent-browser*) return 0 ;; esac
    return 1
}

live_sessions() {  # "<name> <daemon pid>" per live daemon under STATE_DIR
    local f name pid
    for f in "$STATE_DIR"/*.pid; do
        [ -e "$f" ] || continue
        name="${f##*/}"; name="${name%.pid}"
        pid="$(tr -dc '0-9' < "$f" 2>/dev/null)"
        is_daemon_pid "$pid" && echo "$name $pid"
    done
}

# Every Chrome process launched by agent-browser: "pid ppid rss_kb etimes profile_uuid is_root"
chrome_procs() {
    ps -eww -o pid=,ppid=,rss=,etimes=,args= 2>/dev/null | awk -v pfx="$PROFILE_PREFIX" '
        index($0, pfx) && $5 ~ /(^|\/)chrome(_crashpad_handler)?$/ {
            uuid = substr($0, index($0, pfx) + length(pfx)); sub(/[ \/].*/, "", uuid)
            if (uuid !~ /^[0-9a-fA-F-]+$/ || length(uuid) < 8) next
            root = (index($0, " --type=") == 0) ? 1 : 0
            print $1, $2, $3, $4, uuid, root
        }'
}

# Chrome roots whose parent is not a live daemon: "pid rss_kb_tree nprocs etimes uuid"
orphan_trees() {
    local procs line pid ppid rss et uuid root
    procs="$(chrome_procs)"
    [ -n "$procs" ] || return 0
    while read -r pid ppid rss et uuid root; do
        [ "$root" = 1 ] || continue
        is_daemon_pid "$ppid" && continue
        awk -v u="$uuid" -v p="$pid" -v e="$et" '$5 == u { n++; r += $3 } END { print p, r + 0, n + 0, e, u }' <<< "$procs"
    done <<< "$procs"
}

tree_stats() {  # $1 = daemon pid → "nprocs rss_kb" of the Chrome it owns
    chrome_procs | awk -v d="$1" '
        $6 == 1 && $2 == d { owned[$5] = 1 }
        { rows[NR] = $0 }
        END { n = 0; r = 0; for (i in rows) { split(rows[i], f, " "); if (f[5] in owned) { n++; r += f[3] } } print n, r }'
}

kill_tree() {  # $1 = profile uuid → kills every process referencing it, removes the profile dir
    local uuid="$1" pids
    pids="$(chrome_procs | awk -v u="$uuid" '$5 == u { print $1 }')"
    [ -n "$pids" ] && kill -TERM $pids 2>/dev/null
    local i=0
    while [ $i -lt 20 ]; do
        pids="$(chrome_procs | awk -v u="$uuid" '$5 == u { print $1 }')"
        [ -z "$pids" ] && break
        sleep 0.25; i=$((i + 1))
    done
    [ -n "$pids" ] && kill -KILL $pids 2>/dev/null
    case "$uuid" in
        *[!0-9a-fA-F-]*|"") ;;
        *) rm -rf "${PROFILE_PREFIX}${uuid}" 2>/dev/null ;;
    esac
}

sweep_orphans() {  # kills every orphan tree; prints "ntrees nprocs rss_kb"
    local trees pid rss n et uuid T=0 P=0 R=0
    trees="$(orphan_trees)"
    [ -n "$trees" ] || { echo "0 0 0"; return; }
    while read -r pid rss n et uuid; do
        [ -n "$uuid" ] || continue
        log "reaping orphaned Chrome tree root=$pid procs=$n rss_kb=$rss age_s=$et profile=$uuid"
        kill_tree "$uuid"
        T=$((T + 1)); P=$((P + n)); R=$((R + rss))
    done <<< "$trees"
    echo "$T $P $R"
}

fmt_dur() {  # seconds → 4m / 2h13m / 3d
    local s="${1:-0}"
    if [ "$s" -lt 60 ]; then echo "${s}s"
    elif [ "$s" -lt 3600 ]; then echo "$((s / 60))m"
    elif [ "$s" -lt 86400 ]; then echo "$((s / 3600))h$(( (s % 3600) / 60 ))m"
    else echo "$((s / 86400))d$(( (s % 86400) / 3600 ))h"; fi
}

idle_seconds() {  # $1 = session name; empty when the shim never saw it
    local f m
    f="$STATE_DIR/$1.last"
    [ -e "$f" ] || { echo ""; return; }
    m="$(stat -c %Y "$f" 2>/dev/null)"
    [ -n "$m" ] && echo $(( $(date +%s) - m ))
}

mark_closed() { mkdir -p "$STATE_DIR"; : > "$STATE_DIR/$1.closed"; }

real_close() {  # $1 = session name; closes it through the real binary, quietly
    mark_closed "$1"
    "$REAL" --session "$1" close >/dev/null 2>&1
}

notify() {  # $1 = status, $2 = notification id suffix, $3 = summary
    local status="$1" nid="agent-browser:$2" text="$3" base="${GRAPH_API:-}" session="${AUTONOMY_SESSION:-}"
    log "notify status=$status: $text"
    [ -n "$session" ] || return 0
    if [ -n "$base" ] && command -v curl >/dev/null 2>&1; then
        local payload
        payload="$(printf '%s' "$text" | python3 -c 'import json,sys; print(json.dumps({"tmux_session": sys.argv[1], "notification_id": sys.argv[2], "kind": "agent-browser", "status": sys.argv[3], "summary": sys.stdin.read()}))' "$session" "$nid" "$status" 2>/dev/null)"
        if [ -n "$payload" ]; then
            local auth=()
            [ -n "${CROSSTALK_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $CROSSTALK_TOKEN")
            if curl -sk --max-time 10 -o /dev/null -f -H 'Content-Type: application/json' "${auth[@]}" \
                    -X POST "$base/api/session/notify" --data-binary "$payload"; then
                log "notify delivered via $base"
                return 0
            fi
            log "notify via $base failed; falling back to crosstalk"
        fi
    fi
    if command -v graph >/dev/null 2>&1; then
        graph crosstalk send "$session" "$text" >/dev/null 2>&1 && log "notify delivered via crosstalk"
    fi
    return 0
}

live_summary() {  # one line naming what is still running, for notifications
    local names="" name pid
    while read -r name pid; do [ -n "$name" ] && names="${names:+$names, }$name"; done <<< "$(live_sessions)"
    echo "${names:-none}"
}

ensure_sentinel() {  # $1 = session name; spawn a watcher for its daemon if none is running
    local name="$1" pidf dpid reg spid rdpid
    pidf="$STATE_DIR/$name.pid"
    [ -e "$pidf" ] || return 0
    dpid="$(tr -dc '0-9' < "$pidf" 2>/dev/null)"
    is_daemon_pid "$dpid" || return 0
    reg="$STATE_DIR/$name.sentinel"
    if [ -e "$reg" ]; then
        read -r spid rdpid < "$reg"
        [ "$rdpid" = "$dpid" ] && kill -0 "$spid" 2>/dev/null && return 0
    fi
    rm -f "$STATE_DIR/$name.closed"
    setsid bash "$_self" __sentinel "$name" "$dpid" >/dev/null 2>&1 < /dev/null &
    echo "$! $dpid" > "$reg"
}

# ── Sentinel (internal) ───────────────────────────────────────────────
if [ "${1:-}" = "__sentinel" ]; then
    name="$2"; dpid="$3"
    idle_limit_s=$(( AGENT_BROWSER_IDLE_TIMEOUT_MS / 1000 ))
    log "sentinel start session=$name daemon=$dpid idle_limit_s=$idle_limit_s"
    while is_daemon_pid "$dpid"; do sleep "$SENTINEL_POLL_S"; done
    idle="$(idle_seconds "$name")"
    read -r T P R <<< "$(sweep_orphans)"
    rm -f "$STATE_DIR/$name.sentinel"
    if [ -e "$STATE_DIR/$name.closed" ]; then
        rm -f "$STATE_DIR/$name.closed" "$STATE_DIR/$name.last"
        log "sentinel exit session=$name: closed on purpose"
        if [ "$T" -gt 0 ]; then
            notify reaped "$name:$dpid:orphans" "agent-browser: after closing session '$name', reaped $T orphaned Chrome tree(s) ($P processes, ~$((R / 1024)) MB) that no daemon owned. Live browser sessions now: $(live_summary)."
        fi
        exit 0
    fi
    rm -f "$STATE_DIR/$name.last"
    extra=""
    [ "$T" -gt 0 ] && extra=" Also reaped $T orphaned Chrome tree(s) ($P processes, ~$((R / 1024)) MB)."
    threshold=$(( idle_limit_s - SENTINEL_POLL_S - 1 )); [ "$threshold" -lt 0 ] && threshold=0
    if [ -n "$idle" ] && [ "$idle" -ge "$threshold" ]; then
        notify idle-closed "$name:$dpid" "agent-browser: browser session '$name' was closed automatically after $(fmt_dur "$idle") without commands (idle limit $(fmt_dur "$idle_limit_s")); its Chrome is gone and its memory is back. Run \`agent-browser open <url>\` again when you next need it.$extra Live browser sessions now: $(live_summary)."
    else
        notify exited "$name:$dpid" "agent-browser: the daemon for browser session '$name' exited without being closed (idle ${idle:-unknown}s, so not the idle limit); page state is lost.$extra Live browser sessions now: $(live_summary)."
    fi
    exit 0
fi

# ── Argument scan: session name, verb, our own flags ─────────────────
VALUE_OPTS=" --session --profile --state --executable-path --extension --init-script --enable --args --user-agent --proxy --proxy-bypass --device --screenshot-dir --screenshot-quality --screenshot-format --cdp --color-scheme --download-path --max-output --allowed-domains --action-policy --confirm-actions --engine --model --config --headers --session-name -p --provider "
SESSION="${AGENT_BROWSER_SESSION:-default}"
VERB=""; NEW=0; REPLACE=0; CLOSE_ALL=0
expect=""
for a in "$@"; do
    if [ -n "$expect" ]; then
        [ "$expect" = "--session" ] && SESSION="$a"
        expect=""; continue
    fi
    case "$a" in
        --session=*) SESSION="${a#--session=}"; continue ;;
        --new) NEW=1; continue ;;
        --replace) REPLACE=1; continue ;;
        --all) [ "$VERB" = close ] && CLOSE_ALL=1; continue ;;
    esac
    if [[ " $VALUE_OPTS " == *" $a "* ]]; then expect="$a"; continue; fi
    [[ "$a" == -* ]] && continue
    [ -z "$VERB" ] && VERB="$a"
done

# ── Our verbs ─────────────────────────────────────────────────────────
case "$VERB" in
    ps)
        count_only=0; for a in "$@"; do [ "$a" = "--count" ] && count_only=1; done
        NS=0; NO=0; RS=0
        rows=""
        while read -r name pid; do
            [ -n "$name" ] || continue
            read -r n r <<< "$(tree_stats "$pid")"
            idle="$(idle_seconds "$name")"; age="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')"
            idle_s="?"; [ -n "$idle" ] && idle_s="$(fmt_dur "$idle")"
            rows+="$(printf '%-24s %-8s %-7s %-8s %-7s %-7s' "$name" "$pid" "$n" "$((r / 1024))" "$idle_s" "$(fmt_dur "${age:-0}")")"$'\n'
            NS=$((NS + 1)); RS=$((RS + r))
        done <<< "$(live_sessions)"
        orows=""
        while read -r pid rss n et uuid; do
            [ -n "$uuid" ] || continue
            orows+="$(printf '  root pid %-8s %-3s procs  ~%-6s MB  age %-7s %s' "$pid" "$n" "$((rss / 1024))" "$(fmt_dur "$et")" "${PROFILE_PREFIX}${uuid}")"$'\n'
            NO=$((NO + 1)); RS=$((RS + rss))
        done <<< "$(orphan_trees)"
        if [ $count_only = 1 ]; then
            echo "sessions=$NS orphans=$NO rss_mb=$((RS / 1024))"
            exit 0
        fi
        printf '%-24s %-8s %-7s %-8s %-7s %-7s\n' SESSION DAEMON CHROME RSS_MB IDLE AGE
        [ -n "$rows" ] && printf '%s' "$rows" || echo "(no live browser sessions)"
        if [ -n "$orows" ]; then
            echo "Orphaned Chrome trees (no daemon owns them; \`agent-browser reap\` removes them):"
            printf '%s' "$orows"
        fi
        echo "Total: $NS session(s), $NO orphan tree(s), ~$((RS / 1024)) MB. Idle limit $(fmt_dur $((AGENT_BROWSER_IDLE_TIMEOUT_MS / 1000))). State: $STATE_DIR"
        exit 0
        ;;
    reap)
        mode=orphans; idle_min=""
        # Options after the verb itself (a --session prefix may precede it).
        while [ $# -gt 0 ] && [ "$1" != reap ]; do shift; done
        shift
        while [ $# -gt 0 ]; do
            case "$1" in
                --all) mode=all ;;
                --orphans) mode=orphans ;;
                --idle) mode=idle; idle_min="${2:-}"; shift ;;
                --idle=*) mode=idle; idle_min="${1#--idle=}" ;;
                *) echo "agent-browser reap: unknown option $1 (use --orphans, --idle <minutes>, --all)" >&2; exit 2 ;;
            esac
            shift
        done
        if [ "$mode" = idle ] && ! [[ "$idle_min" =~ ^[0-9]+$ ]]; then
            echo "agent-browser reap --idle needs a whole number of minutes" >&2; exit 2
        fi
        closed=0
        if [ "$mode" != orphans ]; then
            while read -r name pid; do
                [ -n "$name" ] || continue
                if [ "$mode" = idle ]; then
                    idle="$(idle_seconds "$name")"
                    [ -n "$idle" ] || idle="$(ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' ')"
                    [ "${idle:-0}" -ge $((idle_min * 60)) ] || continue
                fi
                log "reap: closing session=$name daemon=$pid mode=$mode"
                real_close "$name"
                echo "closed session '$name' (daemon $pid)"
                closed=$((closed + 1))
            done <<< "$(live_sessions)"
            sleep 1
        fi
        read -r T P R <<< "$(sweep_orphans)"
        echo "reaped $T orphaned Chrome tree(s), $P process(es), ~$((R / 1024)) MB; closed $closed session(s). Live now: $(live_summary)."
        exit 0
        ;;
esac

# ── Pass-through with guard, stamps, and sentinel ────────────────────
mkdir -p "$STATE_DIR" 2>/dev/null

if [ "$VERB" = open ]; then
    if [ $REPLACE = 1 ]; then
        while read -r name pid; do
            [ -n "$name" ] || continue
            log "open --replace: closing session=$name daemon=$pid"
            real_close "$name"
        done <<< "$(live_sessions)"
    elif [ $NEW = 0 ] && [ "${AGENT_BROWSER_ALLOW_MANY:-0}" != 1 ]; then
        others=""
        while read -r name pid; do
            [ -n "$name" ] && [ "$name" != "$SESSION" ] || continue
            read -r n r <<< "$(tree_stats "$pid")"
            idle="$(idle_seconds "$name")"
            idle_s="?"; [ -n "$idle" ] && idle_s="$(fmt_dur "$idle")"
            others+="$(printf '  %-24s pid %-7s idle %-6s ~%s MB' "$name" "$pid" "$idle_s" "$((r / 1024))")"$'\n'
        done <<< "$(live_sessions)"
        if [ -n "$others" ]; then
            {
                echo "agent-browser: refusing to open browser session '$SESSION' while other browser session(s) are already running (each is ~1 GB):"
                printf '%s' "$others"
                echo "Reuse one:                agent-browser --session <name> open <url>"
                echo "Close the others first:   agent-browser open --replace <url>"
                echo "Run alongside on purpose: agent-browser open --new <url>"
                echo "See them any time:        agent-browser ps"
            } >&2
            exit 2
        fi
    fi
fi

# Rebuild the argument list without our own flags.
PASS=()
for a in "$@"; do
    case "$a" in
        --new|--replace) [ "$VERB" = open ] && continue ;;
    esac
    PASS+=("$a")
done

if [ "$VERB" = close ]; then
    if [ $CLOSE_ALL = 1 ]; then
        while read -r name pid; do [ -n "$name" ] && mark_closed "$name"; done <<< "$(live_sessions)"
    else
        mark_closed "$SESSION"
    fi
fi

case "$VERB" in
    ""|--help|-h|--version|-V|doctor|skills|install|upgrade|profiles|session|dashboard) ;;
    *) touch "$STATE_DIR/$SESSION.last" 2>/dev/null ;;
esac

"$REAL" "${PASS[@]}"
rc=$?

case "$VERB" in
    close|""|--help|-h|--version|-V|doctor|skills|install|upgrade|profiles|session|dashboard) ;;
    *) ensure_sentinel "$SESSION" ;;
esac
exit $rc
