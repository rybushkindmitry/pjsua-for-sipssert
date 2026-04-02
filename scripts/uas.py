#!/usr/bin/env python3
"""
Standard SIP UAS (TLS server) with RTP/SRTP echo validation and header support.

Scenario:
  1. Creates TLS transport (server/listener role) on --port (default 5061)
  2. Creates account bound to transport
  3. Waits for incoming call (up to --wait-timeout seconds)
  4. On incoming INVITE: checks headers via header_mgr.check_headers()
  5. Answers with 200 OK + custom headers
  6. Connects EchoValidatorPort to media
  7. Hangs up after --duration seconds
  8. Prints echo + header results
  9. Exits with code 0 (pass) or 1 (fail)
"""

import sys
import threading

sys.path.insert(0, "/scripts")

import pjsua2 as pj

from common import (
    load_config_and_args,
    init_endpoint,
    configure_srtp,
    create_transport,
    get_transport,
    get_transport_param,
    get_default_port,
    safe_shutdown,
    safe_exit,
    print_echo_results,
    EchoValidatorPort,
    HeaderManager,
)


# ---------------------------------------------------------------------------
# Global state shared between callbacks and main thread
# ---------------------------------------------------------------------------

_app = None  # filled in main()


class AppState:
    def __init__(self, args, header_mgr):
        self.args = args
        self.header_mgr = header_mgr
        self.ep = None
        self.account = None
        self.validator = None
        self.header_results = []
        self.call_completed = threading.Event()
        self.call_success = False


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class UasAccount(pj.Account):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.active_call = None

    def onIncomingCall(self, prm):
        app = self.app

        # Check incoming INVITE headers
        whole_msg = prm.rdata.wholeMsg
        if app.header_mgr.has_checks():
            app.header_results = app.header_mgr.check_headers(whole_msg)

        # Answer with 200 OK + custom headers
        call = UasCall(app, self, prm.callId)
        self.active_call = call

        call_prm = pj.CallOpParam(True)
        call_prm.statusCode = pj.PJSIP_SC_OK
        call_prm.txOption.headers = app.header_mgr.build_sip_headers()

        call.answer(call_prm)
        print("Incoming call answered with 200 OK.", file=sys.stderr)


# ---------------------------------------------------------------------------
# Call
# ---------------------------------------------------------------------------

class UasCall(pj.Call):
    def __init__(self, app, account, call_id):
        super().__init__(account, call_id)
        self.app = app
        self.media_active = False
        self.timer = None

    def onCallState(self, prm):
        ci = self.getInfo()
        print(f"Call state: {ci.stateText}", file=sys.stderr)

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            print(f"Call connected. Duration: {self.app.args.duration}s.",
                  file=sys.stderr)
            self.timer = threading.Timer(self.app.args.duration, self._hangup)
            self.timer.start()

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"Call disconnected (status {ci.lastStatusCode}).",
                  file=sys.stderr)
            if self.timer:
                self.timer.cancel()
            self.app.call_completed.set()

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for mi_idx, mi in enumerate(ci.media):
            if mi.type != pj.PJMEDIA_TYPE_AUDIO:
                continue
            if mi.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                continue

            self.media_active = True
            print(f"Audio media active (stream {mi_idx}). "
                  f"Connecting echo validator...", file=sys.stderr)

            aud_med = self.getAudioMedia(mi_idx)

            validator = EchoValidatorPort()
            validator.register("echo-validator")
            self.app.validator = validator

            # validator -> call (send known pattern to remote)
            validator.startTransmit(aud_med)
            # call -> validator (receive echo from remote)
            aud_med.startTransmit(validator)

            print("Echo validator connected.", file=sys.stderr)

    def _hangup(self):
        try:
            prm = pj.CallOpParam()
            prm.statusCode = pj.PJSIP_SC_OK
            self.hangup(prm)
        except pj.Error as e:
            print(f"Hangup error: {e}", file=sys.stderr)
            self.app.call_completed.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _app

    args, header_mgr = load_config_and_args("Standard SIP UAS (TLS server)")

    app = AppState(args, header_mgr)
    _app = app

    # Init PJSUA2 endpoint
    ep = init_endpoint(args)
    app.ep = ep

    # Create transport (listener / server role)
    port = args.port or get_default_port(args)
    transport_id = create_transport(ep, args, port=port)
    t = get_transport(args)
    print(f"{t.upper()} server: listening on port {port}", file=sys.stderr)

    # Create account bound to transport
    acfg = pj.AccountConfig()
    bind_addr = getattr(args, "bind_ip", "") or "0.0.0.0"
    tp_param = get_transport_param(args)
    acfg.idUri = f"sip:uas@{bind_addr}:{port}{tp_param}"
    acfg.regConfig.registrarUri = ""
    acfg.regConfig.registerOnAdd = False
    acfg.sipConfig.transportId = transport_id

    configure_srtp(acfg, args.srtp, args.srtp_secure)

    if getattr(args, "rtp_port", 0):
        acfg.mediaConfig.transportConfig.port = args.rtp_port

    account = UasAccount(app)
    account.create(acfg)
    app.account = account

    # Wait for incoming call
    wait_timeout = getattr(args, "wait_timeout", 30)
    print(f"Waiting for incoming call (timeout: {wait_timeout}s)...",
          file=sys.stderr)

    got_call = app.call_completed.wait(timeout=wait_timeout)

    if not got_call:
        print("Timeout: no incoming call received.", file=sys.stderr)

    # --- Results ---
    echo_passed = print_echo_results(app.validator, args.tolerance)

    header_passed = True
    if app.header_results:
        header_passed = HeaderManager.print_report(app.header_results)
    elif header_mgr.has_checks():
        print("Header check results:", file=sys.stderr)
        print("  No call received — headers could not be checked.", file=sys.stderr)
        header_passed = False

    rc = 0 if (echo_passed and header_passed and got_call) else 1

    safe_shutdown(ep, validator=app.validator, account=app.account)
    safe_exit(rc)


if __name__ == "__main__":
    main()
