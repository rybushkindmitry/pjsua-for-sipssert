#!/usr/bin/env python3
"""
SIP UAC + TLS Server with RTP/SRTP echo validation.

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

import argparse
import collections
import struct
import sys
import threading
import time

import pjsua2 as pj


# ---------------------------------------------------------------------------
# Echo validator (shared with uas_tls_client.py)
# ---------------------------------------------------------------------------

class EchoValidatorPort(pj.AudioMediaPort):
    """
    Generates deterministic audio frames, captures echo, compares payloads.
    """

    RING_SIZE = 64

    def __init__(self, clock_rate=8000, channel_count=1,
                 samples_per_frame=160, bits_per_sample=16):
        super().__init__()

        self.lock = threading.Lock()
        self.seq = 0
        self.sent_ring = collections.deque(maxlen=self.RING_SIZE)

        self.frames_sent = 0
        self.frames_received = 0
        self.frames_matched = 0
        self.frames_mismatched = 0

        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = clock_rate
        fmt.channelCount = channel_count
        fmt.frameTimeUsec = (samples_per_frame * 1000000) // clock_rate
        fmt.bitsPerSample = bits_per_sample
        self.fmt = fmt

    def createPort(self, name, pool_endpoint):
        self.createMediaPort(name, self.fmt)

    def onFrameRequested(self, frame):
        size = frame.size
        if size <= 0:
            size = 320

        pattern = struct.pack("<I", self.seq)
        data = (pattern * ((size // len(pattern)) + 1))[:size]

        frame.buf = pj.ByteVector(data)
        frame.type = pj.PJMEDIA_FRAME_TYPE_AUDIO
        frame.size = size

        with self.lock:
            self.sent_ring.append(bytes(data))
            self.seq += 1
            self.frames_sent += 1

    def onFrameReceived(self, frame):
        if frame.type != pj.PJMEDIA_FRAME_TYPE_AUDIO:
            return
        if frame.size <= 0:
            return

        received = bytes(frame.buf)

        with self.lock:
            self.frames_received += 1
            matched = False
            for sent in self.sent_ring:
                if len(sent) == len(received) and sent == received:
                    matched = True
                    break

            if matched:
                self.frames_matched += 1
            else:
                self.frames_mismatched += 1

    def get_stats(self):
        with self.lock:
            total_checked = self.frames_matched + self.frames_mismatched
            if total_checked == 0:
                match_pct = 0.0
            else:
                match_pct = (self.frames_matched / total_checked) * 100.0
            return {
                "sent": self.frames_sent,
                "received": self.frames_received,
                "matched": self.frames_matched,
                "mismatched": self.frames_mismatched,
                "match_pct": match_pct,
            }


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class TlsServerUac:
    def __init__(self, args):
        self.args = args
        self.ep = pj.Endpoint()
        self.account = None
        self.transport_id = None
        self.call = None
        self.call_completed = threading.Event()
        self.call_success = False
        self.validator = None

    def run(self):
        try:
            self._init_endpoint()
            self._create_tls_transport()
            self._create_account()
            self._wait_for_tls_connection()
            self._make_call()
            self._wait_for_call_end()
        except RuntimeError as e:
            print(f"FATAL: {e}", file=sys.stderr)
            self.call_success = False
        finally:
            self._print_results()
            self._shutdown()

        return 0 if self.call_success else 1

    def _init_endpoint(self):
        ep_cfg = pj.EpConfig()
        ep_cfg.logConfig.level = self.args.log_level
        ep_cfg.logConfig.consoleLevel = self.args.log_level
        ep_cfg.medConfig.noVad = True

        self.ep.libCreate()
        self.ep.libInit(ep_cfg)
        self.ep.audDevManager().setNullDev()
        self.ep.libStart()

    def _create_tls_transport(self):
        """Create TLS transport that LISTENS for incoming connections (TLS server role)."""
        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self.args.listen_port
        if self.args.bind_ip:
            tp_cfg.boundAddress = self.args.bind_ip

        tls = tp_cfg.tlsConfig
        tls.method = pj.PJSIP_TLSV1_2_METHOD

        if self.args.tls_ca_file:
            tls.CaListFile = self.args.tls_ca_file
        if self.args.tls_cert_file:
            tls.certFile = self.args.tls_cert_file
        if self.args.tls_privkey_file:
            tls.privKeyFile = self.args.tls_privkey_file

        tls.verifyServer = False
        tls.verifyClient = self.args.tls_verify_client

        self.transport_id = self.ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, tp_cfg)
        print(f"TLS server: listening on port {self.args.listen_port}", file=sys.stderr)

    def _create_account(self):
        """Create account for making outgoing calls via our TLS transport."""
        acfg = pj.AccountConfig()

        bind_addr = self.args.bind_ip or "0.0.0.0"
        acfg.idUri = f"sip:uac@{bind_addr}:{self.args.listen_port};transport=tls"
        acfg.regConfig.registrarUri = ""
        acfg.regConfig.registerOnAdd = False

        # Force account to use our TLS listener transport —
        # this ensures INVITE goes over the incoming TLS connection
        # rather than opening a new outgoing one
        acfg.sipConfig.transportId = self.transport_id

        # SRTP
        srtp_map = {
            "off": pj.PJMEDIA_SRTP_DISABLED,
            "optional": pj.PJMEDIA_SRTP_OPTIONAL,
            "mandatory": pj.PJMEDIA_SRTP_MANDATORY,
        }
        acfg.mediaConfig.srtpUse = srtp_map.get(self.args.srtp,
                                                  pj.PJMEDIA_SRTP_DISABLED)
        acfg.mediaConfig.srtpSecureSignaling = self.args.srtp_secure

        self.account = UacAccount(self)
        self.account.create(acfg)

    def _wait_for_tls_connection(self):
        """Wait for remote side to connect via TLS before sending INVITE."""
        timeout = self.args.tls_wait
        print(f"Waiting up to {timeout}s for remote TLS client to connect...",
              file=sys.stderr)

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ti = self.ep.transportGetInfo(self.transport_id)
                # usageCount > 1 means at least one connection established
                # (1 = the listener itself)
                if ti.usageCount > 1:
                    print(f"TLS client connected (usage count: {ti.usageCount}).",
                          file=sys.stderr)
                    # Small delay for handshake to fully complete
                    time.sleep(0.5)
                    return
            except pj.Error:
                pass
            time.sleep(0.2)

        raise RuntimeError(
            f"No incoming TLS connection within {timeout}s. "
            f"Check that the remote side connects to {self.args.bind_ip or '0.0.0.0'}"
            f":{self.args.listen_port}")

    def _make_call(self):
        """Send INVITE to remote host over the established TLS connection."""
        dest_uri = self.args.dest_uri
        if not dest_uri:
            # No ;transport=tls here — transport is forced via account's transportId
            dest_uri = f"sip:test@{self.args.remote_host}:{self.args.remote_port}"

        print(f"Making call to {dest_uri}...", file=sys.stderr)

        call = UacCall(self, self.account)
        self.call = call

        try:
            prm = pj.CallOpParam(True)
            call.makeCall(dest_uri, prm)
        except pj.Error as e:
            print(f"makeCall failed: {e}", file=sys.stderr)
            self.call_completed.set()

    def _wait_for_call_end(self):
        timeout = self.args.duration + 30  # duration + grace period
        if self.call_completed.wait(timeout=timeout):
            print("Call completed.", file=sys.stderr)
        else:
            print("Timeout waiting for call to complete.", file=sys.stderr)
            self.call_success = False

    def _print_results(self):
        if not self.validator:
            print("NO MEDIA — validator was never connected.", file=sys.stderr)
            return

        stats = self.validator.get_stats()
        print(f"\n{'='*50}", file=sys.stderr)
        print(f"RTP/SRTP Echo Validation Results:", file=sys.stderr)
        print(f"  Frames sent:       {stats['sent']}", file=sys.stderr)
        print(f"  Frames received:   {stats['received']}", file=sys.stderr)
        print(f"  Frames matched:    {stats['matched']}", file=sys.stderr)
        print(f"  Frames mismatched: {stats['mismatched']}", file=sys.stderr)
        print(f"  Match rate:        {stats['match_pct']:.1f}%", file=sys.stderr)
        print(f"  Tolerance:         {self.args.tolerance}%", file=sys.stderr)

        if stats['match_pct'] >= self.args.tolerance:
            print(f"  RESULT: PASS", file=sys.stderr)
            self.call_success = True
        else:
            print(f"  RESULT: FAIL", file=sys.stderr)
            self.call_success = False
        print(f"{'='*50}\n", file=sys.stderr)

    def _shutdown(self):
        self.call = None
        self.account = None
        self.ep.libDestroy()


class UacAccount(pj.Account):
    def __init__(self, app):
        super().__init__()
        self.app = app


class UacCall(pj.Call):
    def __init__(self, app, account):
        super().__init__(account)
        self.app = app
        self.media_active = False
        self.timer = None

    def onCallState(self, prm):
        ci = self.getInfo()
        print(f"Call state: {ci.stateText}", file=sys.stderr)

        if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
            print(f"Call connected. Duration: {self.app.args.duration}s.", file=sys.stderr)
            self.timer = threading.Timer(self.app.args.duration, self._hangup)
            self.timer.start()

        elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
            print(f"Call disconnected (status {ci.lastStatusCode}).", file=sys.stderr)
            if self.timer:
                self.timer.cancel()
            self.app.call_completed.set()

    def onCallMediaState(self, prm):
        ci = self.getInfo()
        for mi_idx, mi in enumerate(ci.media):
            if mi.type != pj.PJMEDIA_TYPE_AUDIO:
                continue
            if mi.status != pj.PJSIP_INV_STATE_CONFIRMED:
                continue

            self.media_active = True
            print(f"Audio media active (stream {mi_idx}). "
                  f"Connecting echo validator...", file=sys.stderr)

            aud_med = self.getAudioMedia(mi_idx)

            validator = EchoValidatorPort()
            validator.createPort("echo-validator", self.app.ep)
            self.app.validator = validator

            # validator -> call (send pattern)
            validator.startTransmit(aud_med)
            # call -> validator (receive echo)
            aud_med.startTransmit(validator)

            print("Echo validator connected.", file=sys.stderr)

    def _hangup(self):
        try:
            prm = pj.CallOpParam()
            prm.statusCode = pj.PJSIP_SC_OK
            self.hangup(prm)
        except pj.Error as e:
            print(f"Hangup: {e}", file=sys.stderr)
            self.app.call_completed.set()


def parse_args():
    p = argparse.ArgumentParser(
        description="SIP UAC + TLS Server with RTP/SRTP echo validation")
    p.add_argument("--remote-host", required=True,
                   help="Remote host to send INVITE to")
    p.add_argument("--remote-port", type=int, default=5060,
                   help="Remote SIP port (default: 5060)")
    p.add_argument("--listen-port", type=int, default=5061,
                   help="Local TLS listen port (default: 5061)")
    p.add_argument("--bind-ip", default="",
                   help="Bind to specific IP address (default: all interfaces)")
    p.add_argument("--dest-uri", default="",
                   help="Full destination SIP URI (overrides --remote-host/port)")
    p.add_argument("--tls-ca-file", default="",
                   help="CA certificate file")
    p.add_argument("--tls-cert-file", default="",
                   help="Server certificate file")
    p.add_argument("--tls-privkey-file", default="",
                   help="Server private key file")
    p.add_argument("--tls-verify-client", action="store_true",
                   help="Verify client certificate (mTLS)")
    p.add_argument("--tls-wait", type=int, default=10,
                   help="Max seconds to wait for TLS client to connect (default: 10)")
    p.add_argument("--srtp", choices=["off", "optional", "mandatory"],
                   default="off", help="SRTP mode")
    p.add_argument("--srtp-secure", type=int, choices=[0, 1, 2], default=0,
                   help="SRTP secure signaling requirement")
    p.add_argument("--duration", type=int, default=10,
                   help="Call duration in seconds")
    p.add_argument("--tolerance", type=float, default=90.0,
                   help="Minimum match percentage to pass (default: 90%%)")
    p.add_argument("--log-level", type=int, default=3,
                   help="PJSIP log level (0-6)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = TlsServerUac(args)
    sys.exit(app.run())
