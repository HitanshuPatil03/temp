#!/usr/bin/env bash
#
# MRIP — one command to a running, populated system.
#
#     ./run.sh
#
# What this replaces: thirteen commands across five terminals, each of which can
# fail in a way that looks like a different problem than it is. A port already
# bound by another PostgreSQL reads as "the API won't start". A missing admin
# account reads as "login is broken". This script's job is to make those
# distinguishable, so the failure tells you what to do instead of what went
# wrong somewhere.
#
# Every refusal below names a remedy, for the same reason `mrip-admin verify`
# does: whoever runs this is usually trying to get to a demo, not to debug a
# container runtime.
#
#     ./run.sh            bring the stack up and seed it
#     ./run.sh stop       stop it, keep the data
#     ./run.sh reset      stop it and destroy the data (asks first)
#     ./run.sh logs       follow the logs
#     ./run.sh status     what is running, on which ports
#
set -euo pipefail

# The script lives at the repository root and is run from anywhere.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Ports this run settled on. Not `.env`: that file is yours, and a script that
# edits your configuration behind you is a script you stop trusting. Compose
# takes shell environment over `.env`, so exporting is enough.
PORTS_FILE="$ROOT/.mrip-run.env"

# ---------------------------------------------------------------------- output

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'; GREEN=$'\033[32m'
    YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RESET=$'\033[0m'
else
    BOLD=''; DIM=''; RED=''; GREEN=''; YELLOW=''; CYAN=''; RESET=''
fi

step()  { printf '\n%s==>%s %s%s%s\n' "$CYAN" "$RESET" "$BOLD" "$1" "$RESET"; }
info()  { printf '    %s\n' "$1"; }
muted() { printf '    %s%s%s\n' "$DIM" "$1" "$RESET"; }
warn()  { printf '    %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
good()  { printf '    %s*%s %s\n' "$GREEN" "$RESET" "$1"; }

# Every failure prints what to do about it. The second argument is the remedy,
# and it is not optional — a bare "failed to start" has made someone's evening
# worse rather than better.
die() {
    printf '\n%sCannot continue.%s %s\n\n' "$RED$BOLD" "$RESET" "$1" >&2
    shift
    while [ "$#" -gt 0 ]; do
        printf '    %s\n' "$1" >&2
        shift
    done
    printf '\n' >&2
    exit 1
}

have() { command -v "$1" >/dev/null 2>&1; }

# ------------------------------------------------------------------ preflight

# Compose moved from a separate binary to a docker subcommand. Both still exist
# in the wild; pick whichever is here rather than assuming.
detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif have docker-compose; then
        COMPOSE="docker-compose"
    else
        die "Docker Compose is not available." \
            "Docker Desktop ships it. If you installed the Docker engine on its own," \
            "install the compose plugin:" \
            "" \
            "    https://docs.docker.com/compose/install/"
    fi
}

preflight() {
    if ! have docker; then
        die "Docker is not installed, or is not on PATH." \
            "Install Docker Desktop, start it, and run this script again:" \
            "" \
            "    https://docs.docker.com/get-started/get-docker/" \
            "" \
            "No Docker on a managed laptop? The same PostgreSQL runs inside WSL2" \
            "with no administrator rights — see 'No Docker?' in README.md."
    fi

    # `docker info` talks to the daemon; `docker --version` does not. This
    # distinction is the single most common confusing failure, because the
    # binary being present looks like Docker being up.
    if ! docker info >/dev/null 2>&1; then
        die "Docker is installed but the daemon is not responding." \
            "Start Docker Desktop and wait for the whale icon to stop animating," \
            "then run this script again." \
            "" \
            "On Linux:  sudo systemctl start docker"
    fi

    detect_compose

    [ -f "$ROOT/docker-compose.yml" ] || die \
        "docker-compose.yml is not next to this script." \
        "Run ./run.sh from inside the repository, not from a copy of the file."
}

# ---------------------------------------------------------------------- ports

# Is anything listening on this port? Four probes because no one tool is present
# everywhere: ss on modern Linux, lsof on macOS, netstat in Git Bash on Windows.
# If none of them exist we return "free" and let Docker produce the real error —
# guessing "busy" would block a working machine.
port_busy() {
    port="$1"
    if have ss; then
        ss -ltnH 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${port}\$" && return 0
        return 1
    fi
    if have lsof; then
        lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
        return 1
    fi
    if have netstat; then
        netstat -an 2>/dev/null \
            | grep -iE 'LISTEN' \
            | grep -qE "[:.]${port}[^0-9]" && return 0
        return 1
    fi
    return 1
}

# Walk upward from the preferred port. Bounded, so a pathological machine fails
# with a message rather than looping.
free_port_from() {
    start="$1"; label="$2"
    candidate="$start"
    limit=$((start + 40))
    while [ "$candidate" -le "$limit" ]; do
        if ! port_busy "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
        candidate=$((candidate + 1))
    done
    die "Every port from $start to $limit is in use, so $label has nowhere to listen." \
        "Something is unusual about this machine's networking. Free a port, or set" \
        "the port explicitly and re-run:" \
        "" \
        "    $label=<port> ./run.sh"
}

choose_ports() {
    # Our own containers holding a port is not a conflict — it is this stack
    # already running. Probing first would see postgres on 5432, call it taken,
    # and march every port one along on each run, recreating containers for no
    # reason. So when the stack is up, adopt the ports it is already on, asked
    # of Docker rather than inferred: that is the one source that cannot be out
    # of date.
    if stack_running; then
        [ -n "${WEB_PORT:-}" ]      || WEB_PORT="$(published_port web 3000)"
        [ -n "${API_PORT:-}" ]      || API_PORT="$(published_port api 8000)"
        [ -n "${POSTGRES_PORT:-}" ] || POSTGRES_PORT="$(published_port postgres 5432)"
        ADOPTED=1
    fi

    # Anything still unknown — a partially-up stack, or a service that has never
    # started — falls back to the last run's choice before probing afresh.
    if [ -f "$PORTS_FILE" ]; then
        # shellcheck disable=SC1090
        . "$PORTS_FILE"
    fi

    # An explicit value from the environment is a decision, not a preference:
    # honour it and let Docker complain if it is taken.
    [ -n "${WEB_PORT:-}" ]      || WEB_PORT="$(free_port_from 3000 WEB_PORT)"
    [ -n "${API_PORT:-}" ]      || API_PORT="$(free_port_from 8000 API_PORT)"
    [ -n "${POSTGRES_PORT:-}" ] || POSTGRES_PORT="$(free_port_from 5432 POSTGRES_PORT)"
    export WEB_PORT API_PORT POSTGRES_PORT

    cat >"$PORTS_FILE" <<EOF
# Written by ./run.sh — the ports this deployment settled on.
# Delete this file to let the script choose again.
WEB_PORT=$WEB_PORT
API_PORT=$API_PORT
POSTGRES_PORT=$POSTGRES_PORT
EOF
}

report_ports() {
    if [ -n "${ADOPTED:-}" ]; then
        good "Already running; kept the ports it is published on."
        return
    fi
    moved=0
    [ "$WEB_PORT" = "3000" ] || moved=1
    [ "$API_PORT" = "8000" ] || moved=1
    [ "$POSTGRES_PORT" = "5432" ] || moved=1

    if [ "$moved" -eq 1 ]; then
        warn "Some default ports were already in use, so this run moved:"
        [ "$WEB_PORT" = "3000" ]      || info "  web         3000 -> $WEB_PORT"
        [ "$API_PORT" = "8000" ]      || info "  api         8000 -> $API_PORT"
        [ "$POSTGRES_PORT" = "5432" ] || info "  postgres    5432 -> $POSTGRES_PORT"
        muted "Another PostgreSQL on 5432 is the usual cause, and is fine to leave running."
    else
        good "Ports 3000, 8000 and 5432 are free."
    fi
}

# ----------------------------------------------------------------------- model

# The narrative half of the product talks to a local Ollama. Everything
# evidence-backed — figures, tables, charts, search, conflicts, reports — works
# without it, so this reports rather than refuses.
check_model() {
    # `.env` ships with MRIP_LLM_BASE_URL=http://127.0.0.1:11434, which is right
    # for running the API on the host and silently wrong inside a container,
    # where loopback is the container itself. Compose reads `.env` for
    # substitution, so that value wins over the compose default and every
    # narrative answer fails with "model unavailable" while Ollama answers
    # perfectly well on the host.
    #
    # Translating only loopback keeps a deliberate choice intact: someone who
    # pointed this at another machine meant it, and that address already works
    # from inside a container.
    configured="${MRIP_LLM_BASE_URL:-}"
    if [ -z "$configured" ] && [ -f "$ROOT/.env" ]; then
        configured="$(sed -n 's/^[[:space:]]*MRIP_LLM_BASE_URL=//p' "$ROOT/.env" | tail -n1)"
    fi
    [ -n "$configured" ] || configured="http://127.0.0.1:11434"

    model_port="$(printf '%s' "$configured" | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p')"
    [ -n "$model_port" ] || model_port=11434

    case "$configured" in
        *127.0.0.1*|*localhost*|*[::1]*)
            MRIP_LLM_BASE_URL="http://host.docker.internal:${model_port}"
            export MRIP_LLM_BASE_URL
            ;;
        *)
            export MRIP_LLM_BASE_URL="$configured"
            ;;
    esac

    if port_busy "$model_port"; then
        good "A model runtime is answering on $model_port; narrative answers will work."
        muted "Containers reach it at $MRIP_LLM_BASE_URL — loopback would mean the container itself."
    else
        warn "No model runtime on port $model_port."
        muted "Every figure, table, chart, search result, conflict and report still works —"
        muted "the exact-figure path reaches no model at all. Only narrative prose needs it."
        muted "To enable it:  ollama pull qwen3:8b"
    fi
}

# ------------------------------------------------------------------ the stack

stack_running() {
    $COMPOSE ps --status running -q 2>/dev/null | grep -q .
}

# Which host port a running service is published on, empty if it is not running.
published_port() {
    service="$1"; internal="$2"
    mapping="$($COMPOSE port "$service" "$internal" 2>/dev/null | tail -n1)"
    # "0.0.0.0:5432" -> "5432", and the same for the IPv6 form.
    printf '%s' "${mapping##*:}"
}

compose_up() {
    step "Building and starting the stack"
    muted "First run pulls PostgreSQL and builds two images; several minutes is normal."
    muted "Later runs are seconds. The output below is the build — it is not an error."
    echo

    if ! $COMPOSE up -d --build ${REBUILD:+--force-recreate}; then
        # Two different failures arrive here and they have different remedies:
        # an image that would not build, and an image that built and then
        # exited on startup. Compose names the service in the second case, so
        # say which one happened rather than blaming the build for both.
        failed="$($COMPOSE ps --status exited --format '{{.Service}}' 2>/dev/null | head -n3 | tr '\n' ' ')"
        if [ -n "$failed" ]; then
            die "The stack built, but a service did not start: ${failed}" \
                "It exited rather than failing to build, so the reason is in its log:" \
                "" \
                "    ./run.sh logs ${failed%% *}" \
                "" \
                "A failing 'migrate' means the database schema could not be applied;" \
                "nothing else starts until it succeeds, which is deliberate — no" \
                "process should ever serve against a stale schema."
        fi
        die "The images did not build." \
            "The build output above has the reason. The usual causes:" \
            "" \
            "  * no network, so a base image or package could not be pulled" \
            "  * Docker is out of disk — reclaim it with:  docker system prune -a" \
            "  * a half-built image from an interrupted run — retry with:  ./run.sh --rebuild"
    fi
}

# Poll the API's own health endpoint from the host. Compose's healthcheck would
# also tell us, but it has a 20 s start period and a 15 s interval, so it
# reports "starting" long after the API is serving.
http_ok() {
    url="$1"
    if have curl; then
        curl -fsS --max-time 3 "$url" >/dev/null 2>&1
    elif have wget; then
        wget -q -T 3 -O /dev/null "$url" >/dev/null 2>&1
    else
        # No HTTP client on the host: ask the container, which has Python.
        $COMPOSE exec -T api python -c \
            "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)" \
            >/dev/null 2>&1
    fi
}

wait_for_api() {
    step "Waiting for the API to answer"
    deadline=180
    waited=0
    while [ "$waited" -lt "$deadline" ]; do
        if http_ok "http://127.0.0.1:${API_PORT}/api/health"; then
            echo
            good "API healthy on port $API_PORT after ${waited}s."
            return 0
        fi
        # A container that has already exited will never become healthy, so stop
        # waiting three minutes to discover it.
        if [ "$($COMPOSE ps -q api | wc -l)" -eq 0 ]; then
            echo
            die "The api container is not running." \
                "It exited during startup. Its last words:" \
                "" \
                "    ./run.sh logs api"
        fi
        printf '.'
        sleep 2
        waited=$((waited + 2))
    done
    echo
    die "The API did not become healthy within ${deadline}s." \
        "Migrations run before the API starts, so a slow first boot is usually them." \
        "Check what it is waiting on:" \
        "" \
        "    ./run.sh logs api" \
        "    ./run.sh logs migrate"
}

seed() {
    step "Seeding the demonstration corpus"
    muted "Four documents through the real pipeline — intake, digitize, extract,"
    muted "normalize, validate, index. Idempotent: a second run changes nothing."
    echo
    if ! $COMPOSE exec -T api mrip-admin seed-demo; then
        die "Seeding failed." \
            "The stack is up, so this is not a startup problem — sign in and work with" \
            "an empty corpus, or read the error above. To retry just this step:" \
            "" \
            "    $COMPOSE exec api mrip-admin seed-demo"
    fi
}

credentials() {
    printf '\n%s' "$BOLD"
    printf '  MRIP is running\n'
    printf '%s\n' "$RESET"
    printf '  %sOpen%s   %shttp://localhost:%s%s\n' \
        "$DIM" "$RESET" "$CYAN$BOLD" "$WEB_PORT" "$RESET"
    printf '  %sAPI%s    %shttp://localhost:%s/docs%s   %s(interactive OpenAPI)%s\n' \
        "$DIM" "$RESET" "$CYAN" "$API_PORT" "$RESET" "$DIM" "$RESET"
    printf '\n'
    printf '  %sSign in with any of these — the password is the same for all five:%s\n' \
        "$DIM" "$RESET"
    printf '\n'
    printf '      %sPassword%s   %ssih-demo-2026-mrip%s\n' \
        "$DIM" "$RESET" "$BOLD" "$RESET"
    printf '\n'
    printf '      %s%-16s %-9s %-13s %s%s\n' \
        "$DIM" "USERNAME" "ROLE" "SEES" "WHY YOU'D USE IT" "$RESET"
    printf '      %-16s %-9s %-13s %s\n' \
        "admin"           "admin"    "all entities" "Deployment administration, audit log"
    printf '      %-16s %-9s %-13s %s\n' \
        "hq.officer"      "officer"  "all entities" "Upload, generate reports, CIL-wide"
    printf '      %-16s %-9s %-13s %s\n' \
        "secl.officer"    "officer"  "secl only"    "Row-level scope — a shorter document list"
    printf '      %-16s %-9s %-13s %s\n' \
        "cmpdi.reviewer"  "reviewer" "all entities" "Adjudicates conflicting figures"
    printf '      %-16s %-9s %-13s %s\n' \
        "ministry.viewer" "viewer"   "all entities" "Read only — every write is refused"
    printf '\n'
    printf '  %sStart with hq.officer. Sign in as secl.officer in a second browser%s\n' "$DIM" "$RESET"
    printf '  %sprofile to see the same corpus with one subsidiary'"'"'s access.%s\n' "$DIM" "$RESET"
    printf '\n'
    printf '  %sStop it:%s  ./run.sh stop      %sLogs:%s  ./run.sh logs\n' \
        "$DIM" "$RESET" "$DIM" "$RESET"
    printf '\n'
}

# ------------------------------------------------------------------- commands

cmd_up() {
    preflight
    step "Checking the host"
    choose_ports
    report_ports
    check_model
    compose_up
    wait_for_api
    [ -n "${NO_SEED:-}" ] || seed
    credentials
}

cmd_stop() {
    preflight
    step "Stopping the stack"
    $COMPOSE stop
    good "Stopped. Data is kept — ./run.sh brings it back as it was."
}

cmd_reset() {
    preflight
    printf '\n%sThis destroys the database and every uploaded document.%s\n' "$YELLOW$BOLD" "$RESET"
    printf 'The blob store holds the evidence behind every figure, and it is not recoverable.\n\n'
    if [ -z "${ASSUME_YES:-}" ]; then
        # No terminal to ask on — in CI, or piped. Refuse rather than block
        # forever on a read, and rather than assume consent to destroy evidence.
        [ -t 0 ] || die \
            "reset needs confirmation, and there is no terminal to ask on." \
            "If you are certain, say so explicitly:" \
            "" \
            "    ./run.sh reset --yes"
        printf 'Type %sdestroy%s to confirm: ' "$BOLD" "$RESET"
        read -r reply
        [ "$reply" = "destroy" ] || { printf '\nLeft alone.\n\n'; exit 0; }
    fi
    step "Removing containers and volumes"
    $COMPOSE down -v
    rm -f "$PORTS_FILE"
    good "Gone. ./run.sh starts a fresh system."
}

cmd_logs() {
    preflight
    $COMPOSE logs -f --tail=200 "$@"
}

cmd_status() {
    preflight
    [ -f "$PORTS_FILE" ] && . "$PORTS_FILE"
    step "Containers"
    $COMPOSE ps
    if stack_running; then
        step "Addresses"
        info "web    http://localhost:${WEB_PORT:-3000}"
        info "api    http://localhost:${API_PORT:-8000}/docs"
        info "db     127.0.0.1:${POSTGRES_PORT:-5432}"
    fi
}

usage() {
    cat <<'EOF'

  MRIP — one command to a running, populated system.

    ./run.sh                bring the stack up, seed it, print the logins
    ./run.sh stop           stop it, keep the data
    ./run.sh reset          stop it and destroy the data (asks first)
    ./run.sh logs [service] follow the logs
    ./run.sh status         what is running, on which ports

  Options for the default command:

    --rebuild               recreate the containers (after changing code)
    --no-seed               do not create the demonstration corpus
    --yes                   do not ask for confirmation on reset

  Ports are chosen automatically when the defaults are taken. To pin them:

    WEB_PORT=4000 API_PORT=9000 ./run.sh

EOF
}

main() {
    command=""
    # Flags are accepted on either side of the command, because `./run.sh up
    # --rebuild` is what people type and silently ignoring it would be worse
    # than refusing it. Everything after a recognised command that is not a
    # known flag is passed through — that is how `./run.sh logs api` works.
    passthrough=""
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --rebuild)     REBUILD=1 ;;
            --no-seed)     NO_SEED=1 ;;
            --yes|-y)      ASSUME_YES=1 ;;
            -h|--help)     usage; exit 0 ;;
            up|stop|reset|logs|status)
                [ -z "$command" ] || die \
                    "Two commands given: $command and $1." \
                    "Run one at a time. ./run.sh --help lists them."
                command="$1" ;;
            -*) die "Unrecognised option: $1" \
                    "Run ./run.sh --help for what this accepts." ;;
            *)
                [ "$command" = "logs" ] || die \
                    "Unrecognised argument: $1" \
                    "Run ./run.sh --help for what this accepts."
                passthrough="$passthrough $1" ;;
        esac
        shift
    done

    case "${command:-up}" in
        up)     cmd_up ;;
        stop)   cmd_stop ;;
        reset)  cmd_reset ;;
        # Deliberately unquoted: the service names collected above are split
        # back into separate arguments for compose.
        logs)   cmd_logs $passthrough ;;
        status) cmd_status ;;
    esac
}

main "$@"
