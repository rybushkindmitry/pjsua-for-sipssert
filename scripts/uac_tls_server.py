#!/usr/bin/env python3
"""
SIP UAC + TLS Server with RTP/SRTP echo validation and header support.

Сценарий:
  1. Слушает TLS-порт, ожидает входящее TLS-соединение (TLS server role)
  2. После установки TLS отправляет INVITE (SIP UAC role)
  3. Отправляет RTP/SRTP-поток известного паттерна
  4. Принимает echo-ответ и побайтово сравнивает
  5. Выход: 0 если match >= tolerance, иначе 1

Использование:
  python3 uac_tls_server.py \
    --remote-host 192.168.1.100 \
    --remote-port 5060 \
    --listen-port 5061 \
    --tls-cert-file /home/certs/server.pem \
    --tls-privkey-file /home/certs/server-key.pem \
    --srtp mandatory \
    --duration 10
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
    EchoValidatorPort,
    HeaderManager,
    ConfigLoader,
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
        self.call = None
        self.validator = None
        self.header_results = []
        self.call_completed = threading.Event()
        self.tls_ready = threading.Event()
        self.call_success = False


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class UacAccount(pj.Account):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def onIncomingCall(self, prm):
        """Incoming call = TLS connection is established.

        Before _make_call: reject it (probe from uas-tls-client).
        After _make_call: should not happen in this mode.
        """
        if not self.app.tls_ready.is_set():
            print("Received probe call — TLS connection confirmed.", file=sys.stderr)
            self.app.tls_ready.set()
            # Reject the probe
            call = pj.Call(self, prm.callId)
            reject = pj.CallOpParam()
            reject.statusCode = pj.PJSIP_SC_BUSY_HERE
            try:
                call.hangup(reject)
            except pj.Error:
                pass


# ---------------------------------------------------------------------------
# Call
# ---------------------------------------------------------------------------

class UacCall(pj.Call):
    def __init__(self, app, account):
        super().__init__(account)
        self.app = app
        self.media_active = False
        self.timer = None

    def onCallState(self, prm):
        try:
            ci = self.getInfo()
        except Exception:
            return

        print(f"Call state: {ci.stateText}", file=sys.stderr)

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            duration = self.app.args.duration
            print(f"Call connected. Duration: {duration}s.", file=sys.stderr)
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

            self.media_active = True
            print(f"Audio media active (stream {mi_idx}). "
                  f"Connecting echo validator...", file=sys.stderr)

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
        except pj.Error as e:
            print(f"Hangup: {e}", file=sys.stderr)
            self.app.call_completed.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="SIP UAC + TLS Server with RTP/SRTP echo validation")
    add_common_args(p)
    # Mode-specific args (--tls-wait and --dest-uri already added by add_common_args)
    p.add_argument("--remote-host", default="",
                   help="Remote host to send INVITE to")
    p.add_argument("--remote-port", type=int, default=5060,
                   help="Remote SIP port (default: 5060)")
    p.add_argument("--listen-port", type=int, default=5061,
                   help="Local TLS listen port (default: 5061)")
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

    # Map --port to listen-port if listen-port is default and port given
    if args.port and args.listen_port == 5061:
        args.listen_port = args.port

    header_cfg = ConfigLoader.merge_headers(config, args)
    header_mgr = HeaderManager(header_cfg)

    app = App(args, header_mgr)

    rc = 1
    try:
        # Init endpoint
        ep = init_endpoint(args)
        app.ep = ep

        # Create TLS transport (TLS server role — listen on listen_port)
        # TLS is mandatory for this mode (TLS role decoupling)
        args.transport = "tls"
        transport_id = create_transport(ep, args, port=args.listen_port)
        app.transport_id = transport_id
        print(f"TLS server: listening on port {args.listen_port}", file=sys.stderr)

        # Create account bound to our TLS listener transport
        acfg = pj.AccountConfig()
        bind_addr = getattr(args, "bind_ip", "") or "0.0.0.0"
        acfg.idUri = f"sip:uac@{bind_addr}:{args.listen_port};transport=tls"
        acfg.regConfig.registrarUri = ""
        acfg.regConfig.registerOnAdd = False
        # Force account to use our TLS listener transport —
        # this ensures INVITE goes over the incoming TLS connection
        acfg.sipConfig.transportId = transport_id

        configure_srtp(acfg, args.srtp, args.srtp_secure)

        if getattr(args, "rtp_port", 0):
            acfg.mediaConfig.transportConfig.port = args.rtp_port

        account = UacAccount(app)
        account.create(acfg)
        app.account = account

        # Wait for remote TLS client to connect
        tls_wait = getattr(args, "tls_wait", 10)
        print(f"Waiting up to {tls_wait}s for remote TLS client to connect...",
              file=sys.stderr)

        if not app.tls_ready.wait(timeout=tls_wait):
            raise RuntimeError(
                f"No incoming TLS connection within {tls_wait}s. "
                f"Check that the remote side connects to "
                f"{getattr(args, 'bind_ip', '') or '0.0.0.0'}:{args.listen_port}")

        print("TLS client connected (incoming SIP request detected).", file=sys.stderr)
        time.sleep(0.5)

        # Build destination URI
        dest_uri = getattr(args, "dest_uri", "")
        if not dest_uri:
            remote_host = args.remote_host
            remote_port = args.remote_port
            dest_uri = f"sip:test@{remote_host}:{remote_port}"

        print(f"Making call to {dest_uri}...", file=sys.stderr)

        # Make outgoing call with custom headers
        call = UacCall(app, account)
        app.call = call

        try:
            prm = pj.CallOpParam(True)
            prm.txOption.headers = header_mgr.build_sip_headers()
            call.makeCall(dest_uri, prm)
        except pj.Error as e:
            print(f"makeCall failed: {e}", file=sys.stderr)
            app.call_completed.set()

        # Wait for call to finish
        timeout = args.duration + 30
        if app.call_completed.wait(timeout=timeout):
            print("Call completed.", file=sys.stderr)
        else:
            print("Timeout waiting for call to complete.", file=sys.stderr)

        # Evaluate results
        echo_ok = print_echo_results(app.validator, args.tolerance)

        header_ok = True
        if app.header_results:
            header_ok = HeaderManager.print_report(app.header_results)
        elif header_mgr.has_checks():
            print("WARNING: header checks configured but no 2xx response captured.",
                  file=sys.stderr)
            header_ok = False

        rc = 0 if (echo_ok and header_ok) else 1

    except RuntimeError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        rc = 1
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        rc = 1
    finally:
        safe_shutdown(app.ep, validator=app.validator, account=app.account)

    safe_exit(rc)


if __name__ == "__main__":
    main()
