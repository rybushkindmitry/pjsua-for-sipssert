#!/bin/bash
set -e

usage() {
    cat <<'USAGE'
pjsua-test — PJSUA wrapper for sipssert SIP/TLS/SRTP testing

Usage: entrypoint.sh [OPTIONS]

Modes:
  --mode uac              Make an outgoing call (default)
  --mode uas              Wait for an incoming call
  --mode uas-tls-client   SIP UAS + TLS client (connect to remote, wait for INVITE)

SIP options:
  --proxy HOST:PORT       SIP proxy / destination (uac mode)
  --port PORT             Local SIP port (default: 5060, or 5061 for TLS)
  --username USER         SIP auth username
  --password PASS         SIP auth password
  --dest-uri URI          Full destination SIP URI (overrides --proxy)

TLS options:
  --tls                   Enable TLS transport
  --tls-ca-file PATH      CA certificate file
  --tls-cert-file PATH    Client/server certificate
  --tls-privkey-file PATH Private key file
  --tls-verify-server     Verify server certificate
  --tls-verify-client     Verify client certificate
  --tls-port PORT         TLS listen port (default: 5061)

SRTP options:
  --srtp off              Disable SRTP (default)
  --srtp optional         SRTP optional
  --srtp mandatory        SRTP mandatory
  --srtp-secure LEVEL     0=no requirement, 1=require TLS, 2=end-to-end

Media options:
  --play-file PATH        WAV file to play during call
  --rec-file PATH         WAV file to record into
  --duration SEC          Call duration in seconds (default: 10)
  --auto-loop             Loop audio playback

Extra:
  --extra "ARGS"          Pass extra arguments directly to pjsua
  --pjsua2-script PATH   Run a PJSUA2 Python script instead of pjsua CLI

Environment variables:
  PJSUA_EXTRA_ARGS        Extra arguments appended to pjsua command
USAGE
    exit 0
}

# Defaults
MODE="uac"
PROXY=""
PORT=""
TLS_PORT=""
USERNAME=""
PASSWORD=""
DEST_URI=""
TLS_ENABLED=0
TLS_CA_FILE=""
TLS_CERT_FILE=""
TLS_PRIVKEY_FILE=""
TLS_VERIFY_SERVER=0
TLS_VERIFY_CLIENT=0
SRTP="off"
SRTP_SECURE="0"
PLAY_FILE=""
REC_FILE=""
DURATION=10
AUTO_LOOP=0
EXTRA_ARGS=""
PJSUA2_SCRIPT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mode)           MODE="$2";             shift 2 ;;
        --proxy)          PROXY="$2";            shift 2 ;;
        --port)           PORT="$2";             shift 2 ;;
        --username)       USERNAME="$2";         shift 2 ;;
        --password)       PASSWORD="$2";         shift 2 ;;
        --dest-uri)       DEST_URI="$2";         shift 2 ;;
        --tls)            TLS_ENABLED=1;         shift   ;;
        --tls-ca-file)    TLS_CA_FILE="$2";      shift 2 ;;
        --tls-cert-file)  TLS_CERT_FILE="$2";    shift 2 ;;
        --tls-privkey-file) TLS_PRIVKEY_FILE="$2"; shift 2 ;;
        --tls-verify-server) TLS_VERIFY_SERVER=1; shift  ;;
        --tls-verify-client) TLS_VERIFY_CLIENT=1; shift  ;;
        --tls-port)       TLS_PORT="$2";         shift 2 ;;
        --srtp)           SRTP="$2";             shift 2 ;;
        --srtp-secure)    SRTP_SECURE="$2";      shift 2 ;;
        --play-file)      PLAY_FILE="$2";        shift 2 ;;
        --rec-file)       REC_FILE="$2";         shift 2 ;;
        --duration)       DURATION="$2";         shift 2 ;;
        --auto-loop)      AUTO_LOOP=1;           shift   ;;
        --extra)          EXTRA_ARGS="$2";       shift 2 ;;
        --pjsua2-script)  PJSUA2_SCRIPT="$2";   shift 2 ;;
        --help|-h)        usage ;;
        *)
            echo "Unknown option: $1" >&2
            usage
            ;;
    esac
done

# If a Python script is provided, run it directly
if [[ -n "$PJSUA2_SCRIPT" ]]; then
    exec python3 "$PJSUA2_SCRIPT"
fi

# uas-tls-client: SIP UAS that initiates TLS connection (TLS client role)
# This mode requires PJSUA2 Python script — pjsua CLI cannot decouple roles
if [[ "$MODE" == "uas-tls-client" ]]; then
    SCRIPT_ARGS=(python3 /scripts/uas_tls_client.py)
    [[ -n "$PROXY" ]]             && SCRIPT_ARGS+=(--remote-host="${PROXY%%:*}")
    # Extract port from PROXY if present (HOST:PORT format)
    if [[ "$PROXY" == *:* ]]; then
        SCRIPT_ARGS+=(--remote-port="${PROXY##*:}")
    fi
    [[ -n "$PORT" ]]              && SCRIPT_ARGS+=(--local-port="$PORT")
    [[ -n "$TLS_CA_FILE" ]]       && SCRIPT_ARGS+=(--tls-ca-file="$TLS_CA_FILE")
    [[ -n "$TLS_CERT_FILE" ]]     && SCRIPT_ARGS+=(--tls-cert-file="$TLS_CERT_FILE")
    [[ -n "$TLS_PRIVKEY_FILE" ]]  && SCRIPT_ARGS+=(--tls-privkey-file="$TLS_PRIVKEY_FILE")
    [[ "$TLS_VERIFY_SERVER" -eq 1 ]] && SCRIPT_ARGS+=(--tls-verify-server)
    SCRIPT_ARGS+=(--srtp="$SRTP")
    SCRIPT_ARGS+=(--srtp-secure="$SRTP_SECURE")
    SCRIPT_ARGS+=(--duration="$DURATION")

    echo "=== pjsua-test: uas-tls-client mode ===" >&2
    echo "CMD: ${SCRIPT_ARGS[*]}" >&2
    exec "${SCRIPT_ARGS[@]}"
fi

# Build pjsua command
CMD=(pjsua --null-audio)

# SRTP
case "$SRTP" in
    off)        CMD+=(--use-srtp=0) ;;
    optional)   CMD+=(--use-srtp=1) ;;
    mandatory)  CMD+=(--use-srtp=2) ;;
    *)          echo "Invalid --srtp value: $SRTP" >&2; exit 1 ;;
esac
CMD+=(--srtp-secure="$SRTP_SECURE")

# TLS
if [[ "$TLS_ENABLED" -eq 1 ]]; then
    CMD+=(--use-tls)
    [[ -n "$TLS_CA_FILE" ]]      && CMD+=(--tls-ca-file="$TLS_CA_FILE")
    [[ -n "$TLS_CERT_FILE" ]]    && CMD+=(--tls-cert-file="$TLS_CERT_FILE")
    [[ -n "$TLS_PRIVKEY_FILE" ]] && CMD+=(--tls-privkey-file="$TLS_PRIVKEY_FILE")
    [[ "$TLS_VERIFY_SERVER" -eq 1 ]] && CMD+=(--tls-verify-server)
    [[ "$TLS_VERIFY_CLIENT" -eq 1 ]] && CMD+=(--tls-verify-client)
fi

# Port
if [[ -n "$PORT" ]]; then
    CMD+=(--local-port="$PORT")
elif [[ "$TLS_ENABLED" -eq 1 ]]; then
    CMD+=(--local-port="${TLS_PORT:-5061}")
fi

# Auth
[[ -n "$USERNAME" ]] && CMD+=(--id="sip:${USERNAME}@localhost" --registrar="" --realm=* --username="$USERNAME")
[[ -n "$PASSWORD" ]] && CMD+=(--password="$PASSWORD")

# Media
[[ -n "$PLAY_FILE" ]]     && CMD+=(--play-file="$PLAY_FILE" --auto-play)
[[ -n "$REC_FILE" ]]      && CMD+=(--rec-file="$REC_FILE")
[[ "$AUTO_LOOP" -eq 1 ]]  && CMD+=(--auto-loop)

# Duration
CMD+=(--duration="$DURATION")

# Mode-specific options
case "$MODE" in
    uas)
        CMD+=(--auto-answer=200)
        ;;
    uac)
        if [[ -z "$DEST_URI" ]]; then
            if [[ -n "$PROXY" ]]; then
                if [[ "$TLS_ENABLED" -eq 1 ]]; then
                    DEST_URI="sip:test@${PROXY};transport=tls"
                else
                    DEST_URI="sip:test@${PROXY}"
                fi
            else
                echo "Error: --proxy or --dest-uri required in uac mode" >&2
                exit 1
            fi
        fi
        ;;
    *)
        echo "Invalid mode: $MODE (must be uac or uas)" >&2
        exit 1
        ;;
esac

# Extra args from flag and environment
[[ -n "$EXTRA_ARGS" ]]      && read -ra EA <<< "$EXTRA_ARGS" && CMD+=("${EA[@]}")
[[ -n "$PJSUA_EXTRA_ARGS" ]] && read -ra EA <<< "$PJSUA_EXTRA_ARGS" && CMD+=("${EA[@]}")

# In UAC mode, append destination URI — pjsua dials it on startup
[[ "$MODE" == "uac" ]] && CMD+=("$DEST_URI")

echo "=== pjsua-test: ${MODE} mode ===" >&2
echo "CMD: ${CMD[*]}" >&2

exec "${CMD[@]}"
