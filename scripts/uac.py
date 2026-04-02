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
import time

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
    EchoValidatorPort,
    HeaderManager,
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
            duration = self.app.args.duration
            print(f"Call connected. Will hang up in {duration}s.", file=sys.stderr)
            self.timer = threading.Timer(duration, self._hangup)
            self.timer.start()

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"Call disconnected (status {ci.lastStatusCode}).", file=sys.stderr)
            if self.timer:
                self.timer.cancel()
            self.app.call_completed.set()

    def onCallTsxState(self, prm):
        """Check headers in 200 OK responses."""
        try:
            whole_msg = prm.e.body.tsxState.src.rdata.wholeMsg
            if whole_msg.startswith("SIP/2.0 2"):
                print("Received 2xx response — checking headers...", file=sys.stderr)
                results = self.app.header_mgr.check_headers(whole_msg)
                self.app.header_results.extend(results)
        except Exception:
            # Not all PJSUA2 versions expose rdata.wholeMsg — ignore silently
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

            print(f"Audio media active (stream {mi_idx}). Connecting echo validator...",
                  file=sys.stderr)

            try:
                aud_med = self.getAudioMedia(mi_idx)

                validator = EchoValidatorPort()
                validator.register("echo-validator")
                self.app.validator = validator

                # validator -> call (send pattern)
                validator.startTransmit(aud_med)
                # call -> validator (receive echo)
                aud_med.startTransmit(validator)

                print("Echo validator connected.", file=sys.stderr)
            except Exception as e:
                print(f"Media setup error: {e}", file=sys.stderr)

    def _hangup(self):
        try:
            prm = pj.CallOpParam()
            prm.statusCode = pj.PJSIP_SC_OK
            self.hangup(prm)
        except Exception as e:
            print(f"Hangup error: {e}", file=sys.stderr)
            self.app.call_completed.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args, header_mgr = load_config_and_args("Standard SIP UAC (TLS client)")

    app = App()
    app.args = args
    app.header_mgr = header_mgr

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
        acfg.idUri = f"sip:uac@{bind_addr}{tp_param}"
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
            dest_uri = f"sip:test@{proxy}{tp_param}"

        print(f"Making call to {dest_uri}...", file=sys.stderr)

        # Make outgoing call with custom headers
        call = UacCall(app, account)
        app.call = call

        call_prm = pj.CallOpParam(True)
        call_prm.txOption.headers = header_mgr.build_sip_headers()

        try:
            call.makeCall(dest_uri, call_prm)
        except pj.Error as e:
            print(f"makeCall failed: {e}", file=sys.stderr)
            app.call_completed.set()

        # Wait for call to finish
        timeout = getattr(args, "duration", 10) + 30
        if app.call_completed.wait(timeout=timeout):
            print("Call completed.", file=sys.stderr)
        else:
            print("Timeout waiting for call to complete.", file=sys.stderr)

        # Evaluate results
        echo_ok = print_echo_results(app.validator, getattr(args, "tolerance", 90.0))

        header_ok = True
        if app.header_results:
            header_ok = HeaderManager.print_report(app.header_results)
        elif header_mgr.has_checks():
            print("WARNING: header checks configured but no 2xx response captured.",
                  file=sys.stderr)
            header_ok = False

        rc = 0 if (echo_ok and header_ok) else 1

    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        rc = 1
    finally:
        safe_shutdown(app.ep, validator=app.validator, account=app.account)

    safe_exit(rc)


if __name__ == "__main__":
    main()
