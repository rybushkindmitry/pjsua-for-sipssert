#!/usr/bin/env python3
"""
uac.py — Standard SIP UAC (TLS client) with RTP/SRTP echo validation and header support.

What this script does:
  1. Creates TLS transport (client role)
  2. Creates account bound to transport
  3. Makes outgoing call to --proxy address with custom headers in INVITE
  4. Connects EchoValidatorPort to media for echo validation
  5. Checks headers in 200 OK response (via onCallTsxState)
  6. Hangs up after --duration seconds
  7. Prints echo + header results
  8. Exits with code 0 (pass) or 1 (fail)
"""

import sys
import threading

sys.path.insert(0, "/scripts")

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

import pjsua2 as pj


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

class App:
    def __init__(self):
        self.ep = None
        self.account = None
        self.call = None
        self.validator = None
        self.call_completed = threading.Event()
        self.header_results = []
        self.call_success = False
        self.options_mgr = None
        self.reinvite_timers = []


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class UacAccount(pj.Account):
    def __init__(self, app):
        super().__init__()
        self.app = app


# ---------------------------------------------------------------------------
# Call
# ---------------------------------------------------------------------------

class UacCall(pj.Call):
    def __init__(self, app, account):
        super().__init__(account)
        self.app = app
        self.timer = None

    def onCallState(self, prm):
        try:
            ci = self.getInfo()
        except Exception:
            return

        print(f"Call state: {ci.stateText}", file=sys.stderr)

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            self.timer = schedule_bye(self, self.app, "uac")
            self.app.reinvite_timers = schedule_reinvites(
                self, self.app, "uac")
            if self.app.options_mgr:
                self.app.options_mgr.start()

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"Call disconnected (status {ci.lastStatusCode}).", file=sys.stderr)
            if self.timer:
                self.timer.cancel()
            for t in self.app.reinvite_timers:
                t.cancel()
            if self.app.options_mgr:
                self.app.options_mgr.stop()
            self.app.call_completed.set()

    def onCallTsxState(self, prm):
        """Check headers in 200 OK responses and track OPTIONS responses."""
        try:
            whole_msg = prm.e.body.tsxState.src.rdata.wholeMsg
            if whole_msg.startswith("SIP/2.0 2"):
                tsx = prm.e.body.tsxState.tsx
                if tsx.method == "OPTIONS" and self.app.options_mgr:
                    self.app.options_mgr.on_options_response(tsx.statusCode)
                else:
                    print("Received 2xx response — checking headers...", file=sys.stderr)
                    results = self.app.header_mgr.check_headers(whole_msg)
                    self.app.header_results.extend(results)
        except Exception:
            pass

    def onCallMediaState(self, prm):
        try:
            ci = self.getInfo()
        except Exception:
            return

        for mi_idx, mi in enumerate(ci.media):
            if mi.type != pj.PJMEDIA_TYPE_AUDIO:
                continue
            if mi.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                continue

            try:
                reconnect_media(self, self.app, mi_idx)
            except Exception as e:
                print(f"Media setup error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args, header_mgr = load_config_and_args("Standard SIP UAC (TLS client)")

    app = App()
    app.args = args
    app.header_mgr = header_mgr

    apply_bye_default(args, "uac")

    rc = 1
    try:
        # Init endpoint
        ep = init_endpoint(args)
        app.ep = ep

        # Create transport (client role — port 0 means ephemeral)
        transport_id = create_transport(ep, args, port=getattr(args, "port", 0))
        t = get_transport(args)
        print(f"{t.upper()} transport created (client role).", file=sys.stderr)

        # Create account
        acfg = pj.AccountConfig()
        proxy = getattr(args, "proxy", "") or f"127.0.0.1:{get_default_port(args)}"
        bind_addr = getattr(args, "bind_ip", "") or "0.0.0.0"
        tp_param = get_transport_param(args)
        from_user = getattr(args, "from_user", None) or "uac"
        acfg.idUri = f"sip:{from_user}@{bind_addr}{tp_param}"
        acfg.regConfig.registrarUri = ""
        acfg.regConfig.registerOnAdd = False
        acfg.sipConfig.transportId = transport_id

        configure_srtp(acfg, getattr(args, "srtp", "off"), getattr(args, "srtp_secure", 0))

        rtp_port = getattr(args, "rtp_port", 0)
        if rtp_port:
            acfg.mediaConfig.transportConfig.port = rtp_port

        account = UacAccount(app)
        account.create(acfg)
        app.account = account

        # Build destination URI
        dest_uri = getattr(args, "dest_uri", "")
        if not dest_uri:
            ruri_user = getattr(args, "ruri_user", "test")
            dest_uri = f"sip:{ruri_user}@{proxy}{tp_param}"

        print(f"Making call to {dest_uri}...", file=sys.stderr)

        # Make outgoing call with custom headers
        call = UacCall(app, account)
        app.call = call

        # Init OPTIONS ping manager
        if getattr(args, "options_ping", None) or getattr(args, "options_auto_reply", False):
            options_mgr = OptionsPingManager(
                interval=getattr(args, "options_ping", None),
                call_getter=lambda: app.call,
                ep=ep,
            )
            app.options_mgr = options_mgr

        call_prm = pj.CallOpParam(True)
        call_prm.txOption.headers = header_mgr.build_sip_headers()

        try:
            call.makeCall(dest_uri, call_prm)
        except pj.Error as e:
            print(f"makeCall failed: {e}", file=sys.stderr)
            app.call_completed.set()

        completed = wait_for_completion(app, "uac")

        echo_ok = print_echo_results(app.validator, getattr(args, "tolerance", 90.0))

        header_ok = True
        if app.header_results:
            header_ok = HeaderManager.print_report(app.header_results)
        elif header_mgr.has_checks():
            print("WARNING: header checks configured but no 2xx response captured.",
                  file=sys.stderr)
            header_ok = False

        options_ok = print_options_results(
            app.options_mgr, getattr(args, "options_tolerance", 90.0))

        rc = 0 if (echo_ok and header_ok and options_ok and completed) else 1

    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        rc = 1
    finally:
        safe_shutdown(app.ep, validator=app.validator, account=app.account)

    safe_exit(rc)


if __name__ == "__main__":
    main()
