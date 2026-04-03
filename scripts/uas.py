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
    print_options_results,
    EchoValidatorPort,
    HeaderManager,
    OptionsPingManager,
    apply_bye_default,
    schedule_bye,
    schedule_reinvites,
    reconnect_media,
    wait_for_completion,
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
        self.options_mgr = None
        self.reinvite_timers = []
        self.active_call = None


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
        app.active_call = call

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
            self.timer = schedule_bye(self, self.app, "uas")
            self.app.reinvite_timers = schedule_reinvites(
                self, self.app, "uas")
            if self.app.options_mgr:
                self.app.options_mgr.start()

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"Call disconnected (status {ci.lastStatusCode}).",
                  file=sys.stderr)
            if self.timer:
                self.timer.cancel()
            for t in self.app.reinvite_timers:
                t.cancel()
            if self.app.options_mgr:
                self.app.options_mgr.stop()
            self.app.call_completed.set()

    def onCallTsxState(self, prm):
        """Track OPTIONS responses for OptionsPingManager."""
        if not self.app.options_mgr:
            return
        try:
            whole_msg = prm.e.body.tsxState.src.rdata.wholeMsg
            if whole_msg.startswith("SIP/2.0 2"):
                tsx = prm.e.body.tsxState.tsx
                if tsx.method == "OPTIONS":
                    self.app.options_mgr.on_options_response(tsx.statusCode)
        except Exception:
            pass

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for mi_idx, mi in enumerate(ci.media):
            if mi.type != pj.PJMEDIA_TYPE_AUDIO:
                continue
            if mi.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                continue

            self.media_active = True
            try:
                reconnect_media(self, self.app, mi_idx)
            except Exception as e:
                print(f"Media setup error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _app

    args, header_mgr = load_config_and_args("Standard SIP UAS (TLS server)")

    app = AppState(args, header_mgr)
    _app = app

    apply_bye_default(args, "uas")

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
    from_user = getattr(args, "from_user", None) or "uas"
    acfg.idUri = f"sip:{from_user}@{bind_addr}:{port}{tp_param}"
    acfg.regConfig.registrarUri = ""
    acfg.regConfig.registerOnAdd = False
    acfg.sipConfig.transportId = transport_id

    configure_srtp(acfg, args.srtp, args.srtp_secure)

    if getattr(args, "rtp_port", 0):
        acfg.mediaConfig.transportConfig.port = args.rtp_port

    account = UasAccount(app)
    account.create(acfg)
    app.account = account

    # Init OPTIONS ping manager
    if getattr(args, "options_ping", None) or getattr(args, "options_auto_reply", False):
        options_mgr = OptionsPingManager(
            interval=getattr(args, "options_ping", None),
            call_getter=lambda: app.active_call,
            ep=ep,
        )
        app.options_mgr = options_mgr

    # Wait for incoming call
    wait_timeout = getattr(args, "wait_timeout", 30)
    print(f"Waiting for incoming call (timeout: {wait_timeout}s)...",
          file=sys.stderr)

    got_call = app.call_completed.wait(timeout=wait_timeout)

    if not got_call and app.active_call is None:
        print("Timeout: no incoming call received.", file=sys.stderr)
    elif not got_call:
        # Call arrived but hasn't completed yet — keep waiting
        completed = wait_for_completion(app, "uas")
        got_call = completed
    else:
        got_call = True

    # --- Results ---
    echo_passed = print_echo_results(app.validator, args.tolerance)

    header_passed = True
    if app.header_results:
        header_passed = HeaderManager.print_report(app.header_results)
    elif header_mgr.has_checks():
        print("Header check results:", file=sys.stderr)
        print("  No call received — headers could not be checked.", file=sys.stderr)
        header_passed = False

    options_passed = print_options_results(
        app.options_mgr, getattr(args, "options_tolerance", 90.0))

    rc = 0 if (echo_passed and header_passed and options_passed and got_call) else 1

    safe_shutdown(ep, validator=app.validator, account=app.account)
    safe_exit(rc)


if __name__ == "__main__":
    main()
