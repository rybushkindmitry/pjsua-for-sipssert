#!/usr/bin/env python3
"""
SIP UAS + TLS Client with RTP/SRTP echo validation and header support.

Сценарий:
  1. Устанавливает TLS-соединение к удалённому серверу (TLS client role)
  2. Ожидает входящий INVITE по этому соединению (SIP UAS role)
  3. Отвечает 200 OK с custom headers
  4. Отправляет RTP/SRTP-поток известного паттерна
  5. Принимает echo-ответ от удалённой стороны (rtp_echo)
  6. Побайтово сравнивает отправленные и полученные фреймы
  7. Проверяет заголовки входящего INVITE
  8. Выход: 0 если процент совпадений >= tolerance и все checks прошли, иначе 1

Использование:
  python3 uas_tls_client.py \
    --remote-host 192.168.1.100 \
    --remote-port 5061 \
    --tls-ca-file /home/certs/ca.pem \
    --tls-cert-file /home/certs/client.pem \
    --tls-privkey-file /home/certs/client-key.pem \
    --srtp mandatory \
    --duration 10 \
    --tolerance 90
"""

import sys
import threading
import time

sys.path.insert(0, "/scripts")

from common import (
    add_common_args,
    init_endpoint,
    configure_srtp,
    create_transport,
    safe_shutdown,
    safe_exit,
    print_echo_results,
    print_options_results,
    EchoValidatorPort,
    HeaderManager,
    ConfigLoader,
    OptionsPingManager,
    apply_bye_default,
    schedule_bye,
    schedule_reinvites,
    reconnect_media,
    wait_for_completion,
)

import argparse
import pjsua2 as pj


# ---------------------------------------------------------------------------
# Application state
# ---------------------------------------------------------------------------

class App:
    def __init__(self, args, header_mgr):
        self.args = args
        self.header_mgr = header_mgr
        self.ep = None
        self.account = None
        self.transport_id = None
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
            self.app.reinvite_timers = schedule_reinvites(self, self.app, "uas")
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

def parse_args():
    p = argparse.ArgumentParser(
        description="SIP UAS + TLS Client with RTP/SRTP echo validation")
    add_common_args(p)
    # Mode-specific args
    p.add_argument("--remote-host", default="",
                   help="Remote TLS server host to connect to")
    p.add_argument("--remote-port", type=int, default=5061,
                   help="Remote TLS server port (default: 5061)")
    p.add_argument("--local-port", type=int, default=0,
                   help="Local TLS port (0 = ephemeral)")
    return p.parse_args()


def main():
    args = parse_args()

    config = {}
    if getattr(args, "config", ""):
        config = ConfigLoader.load(args.config)
        ConfigLoader.merge(config, args)

    # Map --proxy to remote-host/port if remote-host not given explicitly
    if not args.remote_host and args.proxy:
        proxy = args.proxy
        # Strip sip: prefix and ;transport=... params
        proxy = proxy.replace("sip:", "").split(";")[0]
        if ":" in proxy:
            host, port_str = proxy.rsplit(":", 1)
            args.remote_host = host.strip()
            try:
                args.remote_port = int(port_str.strip())
            except ValueError:
                pass
        else:
            args.remote_host = proxy.strip()

    # Map --port to local-port if local-port is 0 and port given
    if args.port and args.local_port == 0:
        args.local_port = args.port

    header_cfg = ConfigLoader.merge_headers(config, args)
    header_mgr = HeaderManager(header_cfg)

    app = App(args, header_mgr)
    apply_bye_default(args, "uas")

    rc = 1
    try:
        # Init endpoint
        ep = init_endpoint(args)
        app.ep = ep

        # Create TLS transport (TLS client role)
        # TLS is mandatory for this mode (TLS role decoupling)
        args.transport = "tls"
        transport_id = create_transport(ep, args, port=args.local_port)
        app.transport_id = transport_id
        print(f"TLS client: connecting to {args.remote_host}:{args.remote_port}",
              file=sys.stderr)

        # Create account configured to auto-answer via TLS
        acfg = pj.AccountConfig()
        bind_addr = getattr(args, "bind_ip", "") or "127.0.0.1"
        local_port = args.local_port or 5061
        acfg.idUri = f"sip:uas@{bind_addr}:{local_port};transport=tls"
        acfg.regConfig.registrarUri = ""
        acfg.regConfig.registerOnAdd = False
        acfg.sipConfig.transportId = transport_id

        proxy_uri = (f"sip:{args.remote_host}:{args.remote_port}"
                     f";transport=tls;lr")
        acfg.sipConfig.proxies = pj.StringVector([proxy_uri])

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

        # Force TLS handshake via probe INVITE
        _establish_tls_connection(account, args)

        # Wait for incoming call
        wait_timeout = getattr(args, "wait_timeout", 30)
        print(f"Waiting for incoming call (timeout: {wait_timeout}s)...",
              file=sys.stderr)

        got_call = app.call_completed.wait(timeout=wait_timeout)

        if not got_call and app.active_call is None:
            print("Timeout: no incoming call received.", file=sys.stderr)
        elif not got_call:
            completed = wait_for_completion(app, "uas")
            got_call = completed
        else:
            got_call = True

        # Evaluate results
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

    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        rc = 1
    finally:
        safe_shutdown(app.ep, validator=app.validator, account=app.account)

    safe_exit(rc)


def _establish_tls_connection(account, args):
    """Make a probe call to force TLS client handshake."""
    remote_uri = (f"sip:ping@{args.remote_host}:{args.remote_port}"
                  f";transport=tls")
    print(f"Establishing TLS connection to {remote_uri}...", file=sys.stderr)

    # Use a short-lived call to force PJSIP to open TLS connection.
    # The call will fail (no one answers), but the TLS transport stays.
    try:
        dummy = pj.Call(account)
        prm = pj.CallOpParam(True)
        dummy.makeCall(remote_uri, prm)
        # Give time for TLS handshake
        time.sleep(2)
        # Hang up the probe call
        try:
            hup = pj.CallOpParam()
            hup.statusCode = pj.PJSIP_SC_REQUEST_TERMINATED
            dummy.hangup(hup)
        except pj.Error:
            pass
    except pj.Error as e:
        # Even if the call fails, TLS connection may be established
        print(f"TLS probe: {e}", file=sys.stderr)
        time.sleep(1)


if __name__ == "__main__":
    main()
