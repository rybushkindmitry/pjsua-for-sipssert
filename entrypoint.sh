#!/bin/bash
set -e

usage() {
    cat <<'USAGE'
pjsua-test — PJSUA wrapper for sipssert SIP/TLS/SRTP testing

Usage: entrypoint.sh [OPTIONS]

Modes:
  --mode=uac              Make an outgoing call (TLS client)
  --mode=uas              Wait for an incoming call (TLS server)
  --mode=uas-tls-client   SIP UAS + TLS client
  --mode=uac-tls-server   SIP UAC + TLS server

All parameters can be set via --config=FILE (YAML) and/or CLI flags.
CLI flags override config values. See README.md for full parameter list.
USAGE
    exit 0
}

# Pre-parse: split --key=value into --key value
ARGS=()
for arg in "$@"; do
    if [[ "$arg" == --*=* ]]; then
        ARGS+=("${arg%%=*}" "${arg#*=}")
    else
        ARGS+=("$arg")
    fi
done
set -- "${ARGS[@]}"

# Extract --mode (needed to route to correct script)
MODE="uac"
REMAINING=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)   MODE="$2"; shift 2 ;;
        --help|-h) usage ;;
        *)        REMAINING+=("$1"); shift ;;
    esac
done

# Route to Python script
case "$MODE" in
    uac)            SCRIPT=/scripts/uac.py ;;
    uas)            SCRIPT=/scripts/uas.py ;;
    uas-tls-client) SCRIPT=/scripts/uas_tls_client.py ;;
    uac-tls-server) SCRIPT=/scripts/uac_tls_server.py ;;
    *)
        echo "Unknown mode: $MODE" >&2
        usage
        ;;
esac

echo "=== pjsua-test: ${MODE} mode ===" >&2
exec python3 "$SCRIPT" "${REMAINING[@]}"
