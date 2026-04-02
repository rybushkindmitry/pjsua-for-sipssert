#!/usr/bin/env python3
"""
SIP UAS + TLS Client with RTP/SRTP echo validation.

Сценарий:
  1. Устанавливает TLS-соединение к удалённому серверу (TLS client role)
  2. Ожидает входящий INVITE по этому соединению (SIP UAS role)
  3. Отвечает 200 OK
  4. Отправляет RTP/SRTP-поток известного паттерна
  5. Принимает echo-ответ от удалённой стороны (rtp_echo)
  6. Побайтово сравнивает отправленные и полученные фреймы
  7. Выход: 0 если процент совпадений >= tolerance, иначе 1

На удалённой стороне предполагается rtp_echo (SIPp или аналог),
который возвращает полученные пакеты as-is.

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

import argparse
import collections
import struct
import sys
import threading
import time

import pjsua2 as pj


# ---------------------------------------------------------------------------
# Audio media port: generates a known pattern and captures echo for comparison
# ---------------------------------------------------------------------------

class EchoValidatorPort(pj.AudioMediaPort):
    """
    Custom media port that:
      - onFrameRequested: generates frames with a deterministic pattern
        (sequential counter bytes) and stores a copy in sent_ring
      - onFrameReceived: receives the echoed frames and compares payload
        against sent_ring

    Сравнение учитывает задержку echo: мы ищем полученный фрейм
    среди последних N отправленных (ring buffer).
    """

    RING_SIZE = 64  # хранить последние 64 отправленных фрейма

    def __init__(self, clock_rate=8000, channel_count=1,
                 samples_per_frame=160, bits_per_sample=16):
        super().__init__()

        self.lock = threading.Lock()
        self.seq = 0  # frame sequence counter for pattern generation
        self.sent_ring = collections.deque(maxlen=self.RING_SIZE)

        self.frames_sent = 0
        self.frames_received = 0
        self.frames_matched = 0
        self.frames_mismatched = 0

        # PJSUA2 needs format configured before addMedia
        fmt = pj.MediaFormatAudio()
        fmt.type = pj.PJMEDIA_TYPE_AUDIO
        fmt.clockRate = clock_rate
        fmt.channelCount = channel_count
        fmt.frameTimeUsec = (samples_per_frame * 1000000) // clock_rate
        fmt.bitsPerSample = bits_per_sample
        self.fmt = fmt

    def register(self, name):
        """Register this port with the conference bridge."""
        super().createPort(name, self.fmt)

    # -- callbacks ----------------------------------------------------------

    def onFrameRequested(self, frame):
        """Generate a deterministic audio frame (known pattern)."""
        # Размер фрейма в байтах: samples_per_frame * (bits/8) * channels
        size = frame.size
        if size <= 0:
            size = 320  # 160 samples * 16bit

        # Паттерн: 4-байтовый little-endian sequence counter, повторённый
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
        """Receive echoed frame and compare against sent ring buffer."""
        if frame.type != pj.PJMEDIA_FRAME_TYPE_AUDIO:
            return
        if frame.size <= 0:
            return

        received = bytes(frame.buf)

        with self.lock:
            self.frames_received += 1
            # Ищем точное совпадение среди последних отправленных фреймов
            matched = False
            for sent in self.sent_ring:
                if len(sent) == len(received) and sent == received:
                    matched = True
                    break

            if matched:
                self.frames_matched += 1
            else:
                self.frames_mismatched += 1

    # -- results ------------------------------------------------------------

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

class TlsClientUas:
    def __init__(self, args):
        self.args = args
        self.ep = pj.Endpoint()
        self.account = None
        self.transport_id = None
        self.call_completed = threading.Event()
        self.call_success = False
        self.validator = None

    def run(self):
        try:
            self._init_endpoint()
            self._create_tls_transport()
            self._create_account()
            self._wait_for_call()
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
        """Create TLS transport that CONNECTS to remote host (TLS client role)."""
        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self.args.local_port
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

        tls.verifyServer = self.args.tls_verify_server
        tls.verifyClient = False

        self.transport_id = self.ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, tp_cfg)
        print(f"TLS client: connecting to {self.args.remote_host}:{self.args.remote_port}",
              file=sys.stderr)

    def _create_account(self):
        """Create account configured to auto-answer via TLS."""
        acfg = pj.AccountConfig()

        bind_addr = self.args.bind_ip or "127.0.0.1"
        local_port = self.args.local_port or 5061
        acfg.idUri = f"sip:uas@{bind_addr}:{local_port};transport=tls"
        acfg.regConfig.registrarUri = ""
        acfg.regConfig.registerOnAdd = False

        # Bind account to our TLS transport so Contact header can be generated
        acfg.sipConfig.transportId = self.transport_id

        proxy_uri = (f"sip:{self.args.remote_host}:{self.args.remote_port}"
                     f";transport=tls;lr")
        acfg.sipConfig.proxies = pj.StringVector([proxy_uri])

        # SRTP
        srtp_map = {
            "off": pj.PJMEDIA_SRTP_DISABLED,
            "optional": pj.PJMEDIA_SRTP_OPTIONAL,
            "mandatory": pj.PJMEDIA_SRTP_MANDATORY,
        }
        acfg.mediaConfig.srtpUse = srtp_map.get(self.args.srtp,
                                                  pj.PJMEDIA_SRTP_DISABLED)
        acfg.mediaConfig.srtpSecureSignaling = self.args.srtp_secure

        if self.args.rtp_port:
            acfg.mediaConfig.transportConfig.port = self.args.rtp_port

        self.account = UasAccount(self)
        self.account.create(acfg)

        # Force TLS handshake
        self._establish_tls_connection()

    def _establish_tls_connection(self):
        """Make a dummy call attempt to force TLS client handshake."""
        remote_uri = (f"sip:ping@{self.args.remote_host}:{self.args.remote_port}"
                      f";transport=tls")
        print(f"Establishing TLS connection to {remote_uri}...", file=sys.stderr)

        # Use a short-lived call to force PJSIP to open TLS connection.
        # The call will fail (no one answers), but the TLS transport stays.
        try:
            dummy = pj.Call(self.account)
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

    def _wait_for_call(self):
        timeout = self.args.wait_timeout
        print(f"Waiting for incoming call (timeout: {timeout}s)...", file=sys.stderr)

        if self.call_completed.wait(timeout=timeout):
            print("Call completed.", file=sys.stderr)
        else:
            print("Timeout waiting for call.", file=sys.stderr)
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
        try:
            self.ep.hangupAllCalls()
            time.sleep(0.5)
        except pj.Error:
            pass
        self.validator = None
        self.account = None
        try:
            self.ep.libDestroy()
        except pj.Error:
            pass


class UasAccount(pj.Account):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.active_call = None

    def onIncomingCall(self, prm):
        call = UasCall(self.app, self, prm.callId)
        self.active_call = call

        call_prm = pj.CallOpParam(True)
        call_prm.statusCode = pj.PJSIP_SC_OK
        call.answer(call_prm)
        print("Incoming call answered with 200 OK.", file=sys.stderr)


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
            if mi.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                continue

            self.media_active = True
            print(f"Audio media active (stream {mi_idx}). "
                  f"Connecting echo validator...", file=sys.stderr)

            aud_med = self.getAudioMedia(mi_idx)

            # Create validator port and connect it bidirectionally
            # to the call's audio media via the conference bridge:
            #   validator.TX -> call (we send known pattern)
            #   call -> validator.RX (we receive echo and compare)
            validator = EchoValidatorPort()
            validator.register("echo-validator")
            self.app.validator = validator

            # validator -> call (send pattern to remote)
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
            print(f"Hangup: {e}", file=sys.stderr)
            self.app.call_completed.set()


def parse_args():
    p = argparse.ArgumentParser(
        description="SIP UAS + TLS Client with RTP/SRTP echo validation")
    p.add_argument("--remote-host", required=True,
                   help="Remote TLS server host to connect to")
    p.add_argument("--remote-port", type=int, default=5061,
                   help="Remote TLS server port (default: 5061)")
    p.add_argument("--local-port", type=int, default=0,
                   help="Local TLS port (0 = ephemeral)")
    p.add_argument("--rtp-port", type=int, default=0,
                   help="Local RTP port (0 = auto, default: 0)")
    p.add_argument("--bind-ip", default="",
                   help="Bind to specific IP address (default: all interfaces)")
    p.add_argument("--tls-ca-file", default="",
                   help="CA certificate file")
    p.add_argument("--tls-cert-file", default="",
                   help="Client certificate file")
    p.add_argument("--tls-privkey-file", default="",
                   help="Client private key file")
    p.add_argument("--tls-verify-server", action="store_true",
                   help="Verify server certificate")
    p.add_argument("--srtp", choices=["off", "optional", "mandatory"],
                   default="off", help="SRTP mode")
    p.add_argument("--srtp-secure", type=int, choices=[0, 1, 2], default=0,
                   help="SRTP secure signaling requirement")
    p.add_argument("--duration", type=int, default=10,
                   help="Call duration in seconds")
    p.add_argument("--wait-timeout", type=int, default=30,
                   help="Max seconds to wait for incoming call")
    p.add_argument("--tolerance", type=float, default=90.0,
                   help="Minimum match percentage to pass (default: 90%%)")
    p.add_argument("--log-level", type=int, default=3,
                   help="PJSIP log level (0-6)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    app = TlsClientUas(args)
    rc = app.run()
    # Use os._exit to avoid segfault in PJSUA2 Python bindings cleanup
    import os
    os._exit(rc)
