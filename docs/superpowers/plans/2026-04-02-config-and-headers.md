# Config File & Custom SIP Headers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add YAML config file support, custom SIP header set/check in all 4 modes, backed by sipssert integration tests.

**Architecture:** Extract shared code into `scripts/common.py` (EchoValidatorPort, HeaderManager, ConfigLoader, shutdown). Rewrite uac/uas modes as PJSUA2 scripts. All 4 mode scripts import from common. entrypoint.sh simplified to mode→script router. YAML config parsed by ConfigLoader, merged with CLI args.

**Tech Stack:** Python 3.12, pjsua2 (PJSIP 2.14.1), PyYAML (`py3-yaml` Alpine package), sipssert for integration tests.

---

### Task 1: Add py3-yaml to Docker image

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Add py3-yaml package**

In `Dockerfile`, add `py3-yaml` to the `apk add` list:

```dockerfile
RUN apk add --no-cache \
        pjproject \
        pjsua \
        py3-pjsua \
        python3 \
        py3-yaml \
        bash \
        ca-certificates \
    && mkdir -p /usr/share/alsa \
    && printf 'pcm.!default { type null }\nctl.!default { type null }\n' \
        > /usr/share/alsa/alsa.conf
```

- [ ] **Step 2: Build and verify**

Run:
```bash
docker build -t pjsua-test .
docker run --rm --entrypoint python3 pjsua-test -c "import yaml; print('yaml', yaml.__version__)"
```
Expected: `yaml 6.0.1` (or similar)

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "Add py3-yaml package for config file support"
```

---

### Task 2: Create `scripts/common.py` — shared module

**Files:**
- Create: `scripts/common.py`

This is the core module. Contains: `EchoValidatorPort`, `HeaderManager`, `ConfigLoader`, `parse_common_args()`, `safe_shutdown()`, SIP message parsing.

- [ ] **Step 1: Create scripts/common.py with EchoValidatorPort**

Move `EchoValidatorPort` from `uac_tls_server.py` into common.py (it's identical in both files):

```python
#!/usr/bin/env python3
"""Shared utilities for pjsua-test scripts."""

import argparse
import collections
import os
import re
import struct
import sys
import threading
import time

import pjsua2 as pj
import yaml


# ---------------------------------------------------------------------------
# EchoValidatorPort
# ---------------------------------------------------------------------------

class EchoValidatorPort(pj.AudioMediaPort):
    """
    Custom media port that generates deterministic audio frames,
    captures echoed frames, and compares payloads.
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

    def register(self, name):
        super().createPort(name, self.fmt)

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
```

- [ ] **Step 2: Add SIP message parser**

Append to `scripts/common.py`:

```python
# ---------------------------------------------------------------------------
# SIP message parsing
# ---------------------------------------------------------------------------

def parse_sip_headers(whole_msg: str) -> list[tuple[str, str]]:
    """Parse SIP message headers from wholeMsg string.

    Returns list of (name, value) tuples preserving order and duplicates.
    Stops at the blank line separating headers from body.
    """
    headers = []
    lines = whole_msg.replace("\r\n", "\n").split("\n")
    # Skip the first line (request/status line)
    for line in lines[1:]:
        if not line:
            break  # end of headers
        if line[0] in (" ", "\t"):
            # Header continuation (folding)
            if headers:
                name, val = headers[-1]
                headers[-1] = (name, val + " " + line.strip())
            continue
        colon = line.find(":")
        if colon > 0:
            name = line[:colon].strip()
            value = line[colon + 1:].strip()
            headers.append((name, value))
    return headers
```

- [ ] **Step 3: Add HeaderManager**

Append to `scripts/common.py`:

```python
# ---------------------------------------------------------------------------
# HeaderManager
# ---------------------------------------------------------------------------

_INDEX_RE = re.compile(r'^(.+?)\[(-?\d+)\]$')
_COUNT_RE = re.compile(r'^(.+?):\s*(\d+)(\+|-(\d+))?$')


class CheckResult:
    def __init__(self, check_type: str, target: str, passed: bool, detail: str):
        self.check_type = check_type
        self.target = target
        self.passed = passed
        self.detail = detail

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"  [{status}] {self.check_type}: {self.target} — {self.detail}"


class HeaderManager:
    def __init__(self, config: dict):
        self.set_headers = config.get("set", [])
        self.expect = config.get("expect", [])
        self.expect_not = config.get("expect_not", [])
        self.expect_name_regex = config.get("expect_name_regex", [])
        self.expect_not_regex = config.get("expect_not_regex", [])
        self.expect_value = config.get("expect_value", [])
        self.expect_value_regex = config.get("expect_value_regex", [])
        self.expect_count = config.get("expect_count", [])

    def has_checks(self) -> bool:
        return bool(self.expect or self.expect_not or self.expect_name_regex
                     or self.expect_not_regex or self.expect_value
                     or self.expect_value_regex or self.expect_count)

    def build_sip_headers(self) -> pj.SipHeaderVector:
        """Build SipHeaderVector for outgoing INVITE / 200 OK."""
        hdr_vec = pj.SipHeaderVector()
        for entry in self.set_headers:
            colon = entry.find(":")
            if colon <= 0:
                continue
            h = pj.SipHeader()
            h.hName = entry[:colon].strip()
            h.hValue = entry[colon + 1:].strip()
            hdr_vec.push_back(h)
        return hdr_vec

    def check_headers(self, whole_msg: str) -> list[CheckResult]:
        """Run all header checks against a SIP message. Returns list of results."""
        parsed = parse_sip_headers(whole_msg)
        results = []

        # expect: header exists
        for name in self.expect:
            found = any(h[0].lower() == name.lower() for h in parsed)
            results.append(CheckResult(
                "expect", name, found,
                "found" if found else "NOT found"))

        # expect_not: header absent
        for name in self.expect_not:
            found = any(h[0].lower() == name.lower() for h in parsed)
            results.append(CheckResult(
                "expect_not", name, not found,
                "not found" if not found else "FOUND (unexpected)"))

        # expect_name_regex: at least one header name matches pattern
        for pattern in self.expect_name_regex:
            regex = re.compile(pattern, re.IGNORECASE)
            matched_names = [h[0] for h in parsed if regex.search(h[0])]
            ok = len(matched_names) > 0
            results.append(CheckResult(
                "expect_name_regex", pattern, ok,
                f"matched {matched_names[0]}" if ok else "no matching headers"))

        # expect_not_regex: no header names match pattern
        for pattern in self.expect_not_regex:
            regex = re.compile(pattern, re.IGNORECASE)
            matched_names = [h[0] for h in parsed if regex.search(h[0])]
            ok = len(matched_names) == 0
            results.append(CheckResult(
                "expect_not_regex", pattern, ok,
                "no matching headers" if ok else f"FOUND {matched_names[0]}"))

        # expect_value: exact value match (with optional index)
        for entry in self.expect_value:
            self._check_value(parsed, entry, exact=True, results=results)

        # expect_value_regex: regex value match (with optional index)
        for entry in self.expect_value_regex:
            self._check_value(parsed, entry, exact=False, results=results)

        # expect_count: count of headers
        for entry in self.expect_count:
            self._check_count(parsed, entry, results=results)

        return results

    def _check_value(self, parsed, entry, exact, results):
        """Check header value — exact or regex, with optional [index]."""
        check_type = "expect_value" if exact else "expect_value_regex"
        colon = entry.find(":")
        if colon <= 0:
            results.append(CheckResult(check_type, entry, False, "invalid format"))
            return

        name_part = entry[:colon].strip()
        expected = entry[colon + 1:].strip()

        # Check for index: Name[N]
        index = None
        m = _INDEX_RE.match(name_part)
        if m:
            name_part = m.group(1)
            index = int(m.group(2))

        # Collect all values for this header
        values = [h[1] for h in parsed if h[0].lower() == name_part.lower()]

        target = f"{name_part}[{index}]" if index is not None else name_part

        if not values:
            results.append(CheckResult(check_type, target, False, "header not found"))
            return

        if index is not None:
            try:
                actual = values[index]
            except IndexError:
                results.append(CheckResult(
                    check_type, target, False,
                    f"index {index} out of range (found {len(values)})"))
                return
            if exact:
                ok = actual == expected
                detail = f'"{actual}" matches' if ok else f'expected "{expected}", got "{actual}"'
            else:
                ok = bool(re.search(expected, actual))
                detail = f'"{actual}" matches {expected}' if ok else f'"{actual}" does not match {expected}'
            results.append(CheckResult(check_type, target, ok, detail))
        else:
            # Any value matches
            if exact:
                ok = any(v == expected for v in values)
                detail = "matched" if ok else f'no value equals "{expected}", got {values}'
            else:
                ok = any(re.search(expected, v) for v in values)
                detail = "matched" if ok else f'no value matches {expected}, got {values}'
            results.append(CheckResult(check_type, target, ok, detail))

    def _check_count(self, parsed, entry, results):
        """Check header count: 'Name: N', 'Name: N+', 'Name: N-M'."""
        m = _COUNT_RE.match(entry)
        if not m:
            results.append(CheckResult("expect_count", entry, False, "invalid format"))
            return

        name = m.group(1).strip()
        min_count = int(m.group(2))
        suffix = m.group(3) or ""

        actual = sum(1 for h in parsed if h[0].lower() == name.lower())

        if suffix == "+":
            ok = actual >= min_count
            expected_str = f"{min_count}+"
        elif suffix.startswith("-"):
            max_count = int(m.group(4))
            ok = min_count <= actual <= max_count
            expected_str = f"{min_count}-{max_count}"
        else:
            ok = actual == min_count
            expected_str = str(min_count)

        results.append(CheckResult(
            "expect_count", name, ok,
            f"found {actual}, expected {expected_str}"))

    @staticmethod
    def print_report(results: list[CheckResult]) -> bool:
        """Print header check report. Returns True if all passed."""
        if not results:
            return True

        failed = sum(1 for r in results if not r.passed)
        total = len(results)

        print(f"\n{'='*50}", file=sys.stderr)
        print("Header Validation Results:", file=sys.stderr)
        for r in results:
            print(str(r), file=sys.stderr)
        status = "PASS" if failed == 0 else f"FAIL ({failed}/{total} checks failed)"
        print(f"  RESULT: {status}", file=sys.stderr)
        print(f"{'='*50}\n", file=sys.stderr)

        return failed == 0
```

- [ ] **Step 4: Add ConfigLoader and arg parsing**

Append to `scripts/common.py`:

```python
# ---------------------------------------------------------------------------
# ConfigLoader
# ---------------------------------------------------------------------------

class ConfigLoader:
    """Load YAML config and merge with CLI args."""

    @staticmethod
    def load(config_path: str) -> dict:
        if not config_path:
            return {}
        with open(config_path) as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def merge(config: dict, args: argparse.Namespace) -> argparse.Namespace:
        """Apply config values to args where args have default/empty values."""
        mapping = {
            "mode": "mode",
            "proxy": "proxy",
            "port": "port",
            "ip": "bind_ip",
            "rtp_port": "rtp_port",
            "duration": "duration",
            "tolerance": "tolerance",
            "wait_timeout": "wait_timeout",
            "tls_wait": "tls_wait",
            "srtp": "srtp",
            "srtp_secure": "srtp_secure",
            "dest_uri": "dest_uri",
            "log_level": "log_level",
        }
        for cfg_key, arg_key in mapping.items():
            if cfg_key in config and not _arg_was_set(args, arg_key):
                setattr(args, arg_key, config[cfg_key])

        # TLS settings
        tls = config.get("tls", {})
        tls_mapping = {
            "cert_file": "tls_cert_file",
            "privkey_file": "tls_privkey_file",
            "ca_file": "tls_ca_file",
            "verify_server": "tls_verify_server",
            "verify_client": "tls_verify_client",
        }
        for cfg_key, arg_key in tls_mapping.items():
            if cfg_key in tls and not _arg_was_set(args, arg_key):
                setattr(args, arg_key, tls[cfg_key])

        return args

    @staticmethod
    def merge_headers(config: dict, args: argparse.Namespace) -> dict:
        """Merge headers from config and CLI args."""
        headers = dict(config.get("headers", {}))

        cli_lists = {
            "set": getattr(args, "set_header", None),
            "expect": getattr(args, "expect_header", None),
            "expect_not": getattr(args, "expect_no_header", None),
            "expect_name_regex": getattr(args, "expect_header_regex", None),
            "expect_not_regex": getattr(args, "expect_no_header_regex", None),
            "expect_value": getattr(args, "expect_header_value", None),
            "expect_value_regex": getattr(args, "expect_header_value_regex", None),
            "expect_count": getattr(args, "expect_header_count", None),
        }

        for key, cli_vals in cli_lists.items():
            if cli_vals:
                existing = headers.get(key, [])
                headers[key] = existing + cli_vals

        return headers


def _arg_was_set(args, key):
    """Check if an arg was explicitly set (not default)."""
    val = getattr(args, key, None)
    if val is None:
        return False
    if isinstance(val, str) and val == "":
        return False
    if isinstance(val, bool) and val is False:
        return False
    if isinstance(val, int) and val == 0:
        return False
    return True


# ---------------------------------------------------------------------------
# Common argparse arguments
# ---------------------------------------------------------------------------

def add_common_args(p: argparse.ArgumentParser):
    """Add arguments shared by all mode scripts."""
    p.add_argument("--config", default="", help="YAML config file path")

    # Connection
    p.add_argument("--proxy", default="", help="Remote HOST:PORT")
    p.add_argument("--port", type=int, default=0, help="Local SIP/TLS port")
    p.add_argument("--bind-ip", default="", help="Bind IP address")
    p.add_argument("--rtp-port", type=int, default=0, help="Local RTP port")
    p.add_argument("--dest-uri", default="", help="Full destination SIP URI")

    # TLS
    p.add_argument("--tls-ca-file", default="", help="CA certificate")
    p.add_argument("--tls-cert-file", default="", help="Certificate file")
    p.add_argument("--tls-privkey-file", default="", help="Private key file")
    p.add_argument("--tls-verify-server", action="store_true")
    p.add_argument("--tls-verify-client", action="store_true")

    # SRTP
    p.add_argument("--srtp", choices=["off", "optional", "mandatory"],
                   default="off")
    p.add_argument("--srtp-secure", type=int, choices=[0, 1, 2], default=0)

    # Call
    p.add_argument("--duration", type=int, default=10, help="Call duration (s)")
    p.add_argument("--tolerance", type=float, default=90.0, help="Echo match %%")
    p.add_argument("--wait-timeout", type=int, default=30, help="Wait for call (s)")
    p.add_argument("--tls-wait", type=int, default=10, help="Wait for TLS (s)")

    # Headers
    p.add_argument("--set-header", action="append", help="Set header: 'Name: Value'")
    p.add_argument("--expect-header", action="append", help="Expect header present")
    p.add_argument("--expect-no-header", action="append", help="Expect header absent")
    p.add_argument("--expect-header-regex", action="append",
                   help="Expect header name matching regex")
    p.add_argument("--expect-no-header-regex", action="append",
                   help="Expect no header name matching regex")
    p.add_argument("--expect-header-value", action="append",
                   help="Expect exact value: 'Name: value' or 'Name[0]: value'")
    p.add_argument("--expect-header-value-regex", action="append",
                   help="Expect value matching regex: 'Name: pattern'")
    p.add_argument("--expect-header-count", action="append",
                   help="Expect header count: 'Name: N', 'Name: N+', 'Name: N-M'")

    # Misc
    p.add_argument("--log-level", type=int, default=3, help="PJSIP log level")


# ---------------------------------------------------------------------------
# PJSUA2 helpers
# ---------------------------------------------------------------------------

SRTP_MAP = {
    "off": pj.PJMEDIA_SRTP_DISABLED,
    "optional": pj.PJMEDIA_SRTP_OPTIONAL,
    "mandatory": pj.PJMEDIA_SRTP_MANDATORY,
}


def configure_srtp(acfg: pj.AccountConfig, srtp: str, srtp_secure: int):
    acfg.mediaConfig.srtpUse = SRTP_MAP.get(srtp, pj.PJMEDIA_SRTP_DISABLED)
    acfg.mediaConfig.srtpSecureSignaling = srtp_secure


def configure_tls(tp_cfg: pj.TransportConfig, args):
    tls = tp_cfg.tlsConfig
    tls.method = pj.PJSIP_TLSV1_2_METHOD
    if getattr(args, "tls_ca_file", ""):
        tls.CaListFile = args.tls_ca_file
    if getattr(args, "tls_cert_file", ""):
        tls.certFile = args.tls_cert_file
    if getattr(args, "tls_privkey_file", ""):
        tls.privKeyFile = args.tls_privkey_file
    tls.verifyServer = getattr(args, "tls_verify_server", False)
    tls.verifyClient = getattr(args, "tls_verify_client", False)


def init_endpoint(args) -> pj.Endpoint:
    ep = pj.Endpoint()
    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = args.log_level
    ep_cfg.logConfig.consoleLevel = args.log_level
    ep_cfg.medConfig.noVad = True
    ep.libCreate()
    ep.libInit(ep_cfg)
    ep.audDevManager().setNullDev()
    ep.libStart()
    return ep


def safe_shutdown(ep, validator=None, account=None):
    """Clean shutdown avoiding PJSUA2 segfault."""
    try:
        ep.hangupAllCalls()
        time.sleep(0.5)
    except pj.Error:
        pass
    # Release Python references before libDestroy
    if validator is not None:
        del validator
    if account is not None:
        del account
    try:
        ep.libDestroy()
    except pj.Error:
        pass


def safe_exit(rc):
    """Exit avoiding PJSUA2 Python bindings cleanup segfault."""
    os._exit(rc)


def print_echo_results(validator, tolerance) -> bool:
    """Print echo validation results. Returns True if passed."""
    if not validator:
        print("NO MEDIA — validator was never connected.", file=sys.stderr)
        return tolerance <= 0

    stats = validator.get_stats()
    print(f"\n{'='*50}", file=sys.stderr)
    print("RTP/SRTP Echo Validation Results:", file=sys.stderr)
    print(f"  Frames sent:       {stats['sent']}", file=sys.stderr)
    print(f"  Frames received:   {stats['received']}", file=sys.stderr)
    print(f"  Frames matched:    {stats['matched']}", file=sys.stderr)
    print(f"  Frames mismatched: {stats['mismatched']}", file=sys.stderr)
    print(f"  Match rate:        {stats['match_pct']:.1f}%", file=sys.stderr)
    print(f"  Tolerance:         {tolerance}%", file=sys.stderr)

    passed = stats["match_pct"] >= tolerance
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}", file=sys.stderr)
    print(f"{'='*50}\n", file=sys.stderr)
    return passed


def load_config_and_args(description: str) -> tuple:
    """Parse CLI args, load config, merge. Returns (args, header_manager)."""
    p = argparse.ArgumentParser(description=description)
    add_common_args(p)
    args = p.parse_args()

    config = ConfigLoader.load(args.config)
    args = ConfigLoader.merge(config, args)
    headers_cfg = ConfigLoader.merge_headers(config, args)
    header_mgr = HeaderManager(headers_cfg)

    return args, header_mgr
```

- [ ] **Step 5: Verify common.py loads inside container**

```bash
docker build -t pjsua-test .
docker run --rm --entrypoint python3 pjsua-test -c "
import sys; sys.path.insert(0, '/scripts')
from common import HeaderManager, ConfigLoader, EchoValidatorPort
print('common.py loaded OK')
"
```

Expected: `common.py loaded OK`

- [ ] **Step 6: Commit**

```bash
git add scripts/common.py
git commit -m "Add scripts/common.py: shared module with HeaderManager, ConfigLoader, EchoValidatorPort"
```

---

### Task 3: Create `scripts/uac.py` — standard UAC mode on PJSUA2

**Files:**
- Create: `scripts/uac.py`

Standard UAC: TLS client, SIP UAC. Makes call, checks headers in 200 OK, sets headers in INVITE.

- [ ] **Step 1: Create scripts/uac.py**

```python
#!/usr/bin/env python3
"""Standard SIP UAC mode (TLS client + SIP UAC) with header support."""

import sys
import threading
import time

sys.path.insert(0, "/scripts")

import pjsua2 as pj
from common import (
    load_config_and_args, init_endpoint, configure_srtp, configure_tls,
    safe_shutdown, safe_exit, print_echo_results,
    EchoValidatorPort, HeaderManager,
)


class UacApp:
    def __init__(self, args, header_mgr):
        self.args = args
        self.header_mgr = header_mgr
        self.ep = None
        self.account = None
        self.call = None
        self.validator = None
        self.call_completed = threading.Event()
        self.header_results = []

    def run(self):
        try:
            self.ep = init_endpoint(self.args)
            self._create_transport()
            self._create_account()
            self._make_call()
            self._wait_for_call_end()
        except Exception as e:
            print(f"FATAL: {e}", file=sys.stderr)
        finally:
            pass

        # Results
        echo_ok = print_echo_results(self.validator, self.args.tolerance)
        headers_ok = HeaderManager.print_report(self.header_results)
        safe_shutdown(self.ep, self.validator, self.account)
        return 0 if (echo_ok and headers_ok) else 1

    def _create_transport(self):
        tp_cfg = pj.TransportConfig()
        if self.args.port:
            tp_cfg.port = self.args.port
        if self.args.bind_ip:
            tp_cfg.boundAddress = self.args.bind_ip
        configure_tls(tp_cfg, self.args)
        self.transport_id = self.ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, tp_cfg)

    def _create_account(self):
        acfg = pj.AccountConfig()
        bind_addr = self.args.bind_ip or "0.0.0.0"
        acfg.idUri = f"sip:uac@{bind_addr};transport=tls"
        acfg.regConfig.registrarUri = ""
        acfg.regConfig.registerOnAdd = False
        acfg.sipConfig.transportId = self.transport_id
        configure_srtp(acfg, self.args.srtp, self.args.srtp_secure)
        if self.args.rtp_port:
            acfg.mediaConfig.transportConfig.port = self.args.rtp_port
        self.account = UacAccount(self)
        self.account.create(acfg)

    def _make_call(self):
        dest = self.args.dest_uri
        if not dest:
            proxy = self.args.proxy
            if not proxy:
                raise RuntimeError("--proxy or --dest-uri required")
            dest = f"sip:test@{proxy};transport=tls"

        print(f"Making call to {dest}...", file=sys.stderr)
        self.call = UacCall(self)
        prm = pj.CallOpParam(True)
        # Set custom headers on INVITE
        if self.header_mgr.set_headers:
            prm.txOption.headers = self.header_mgr.build_sip_headers()
        try:
            self.call.makeCall(dest, prm)
        except pj.Error as e:
            print(f"makeCall failed: {e}", file=sys.stderr)
            self.call_completed.set()

    def _wait_for_call_end(self):
        self.call_completed.wait(timeout=self.args.duration + 30)


class UacAccount(pj.Account):
    def __init__(self, app):
        super().__init__()
        self.app = app


class UacCall(pj.Call):
    def __init__(self, app):
        super().__init__(app.account)
        self.app = app
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

            aud_med = self.getAudioMedia(mi_idx)
            validator = EchoValidatorPort()
            validator.register("echo-validator")
            self.app.validator = validator
            validator.startTransmit(aud_med)
            aud_med.startTransmit(validator)
            print("Echo validator connected.", file=sys.stderr)

    def onCallSdpCreated(self, prm):
        """Check headers in SIP response (200 OK)."""
        # Note: headers in 200 OK are not available via onCallSdpCreated.
        # We use a different approach - see onCallTsxState below.
        pass

    def onCallTsxState(self, prm):
        """Intercept incoming SIP responses to check headers."""
        if not self.app.header_mgr.has_checks():
            return
        # prm.e.body.tsxState.src.rdata gives access to incoming message
        try:
            tsx_info = prm.e
            rdata = tsx_info.body.tsxState.src.rdata
            if rdata and rdata.wholeMsg:
                # Only check 2xx responses to INVITE
                msg = rdata.wholeMsg
                if msg.startswith("SIP/2.0 2"):
                    self.app.header_results = self.app.header_mgr.check_headers(msg)
        except (AttributeError, Exception):
            pass

    def _hangup(self):
        try:
            prm = pj.CallOpParam()
            prm.statusCode = pj.PJSIP_SC_OK
            self.hangup(prm)
        except pj.Error:
            self.app.call_completed.set()


if __name__ == "__main__":
    args, header_mgr = load_config_and_args("Standard SIP UAC (TLS client)")
    app = UacApp(args, header_mgr)
    safe_exit(app.run())
```

- [ ] **Step 2: Build and smoke-test**

```bash
docker build -t pjsua-test .
docker run --rm pjsua-test "--mode=uac" "--proxy=127.0.0.1:5061" "--tls-cert-file=/tmp/a" "--tls-privkey-file=/tmp/b" "--duration=1" 2>&1 | head -5
```

Expected: `=== pjsua-test: uac mode ===` then script starts.

- [ ] **Step 3: Commit**

```bash
git add scripts/uac.py
git commit -m "Add scripts/uac.py: standard UAC mode on PJSUA2 with header support"
```

---

### Task 4: Create `scripts/uas.py` — standard UAS mode on PJSUA2

**Files:**
- Create: `scripts/uas.py`

Standard UAS: TLS server, SIP UAS. Answers calls, checks headers in INVITE, sets headers in 200 OK.

- [ ] **Step 1: Create scripts/uas.py**

```python
#!/usr/bin/env python3
"""Standard SIP UAS mode (TLS server + SIP UAS) with header support."""

import sys
import threading
import time

sys.path.insert(0, "/scripts")

import pjsua2 as pj
from common import (
    load_config_and_args, init_endpoint, configure_srtp, configure_tls,
    safe_shutdown, safe_exit, print_echo_results, parse_sip_headers,
    EchoValidatorPort, HeaderManager,
)


class UasApp:
    def __init__(self, args, header_mgr):
        self.args = args
        self.header_mgr = header_mgr
        self.ep = None
        self.account = None
        self.validator = None
        self.call_completed = threading.Event()
        self.header_results = []

    def run(self):
        try:
            self.ep = init_endpoint(self.args)
            self._create_transport()
            self._create_account()
            self._wait_for_call()
        except Exception as e:
            print(f"FATAL: {e}", file=sys.stderr)
        finally:
            pass

        echo_ok = print_echo_results(self.validator, self.args.tolerance)
        headers_ok = HeaderManager.print_report(self.header_results)
        safe_shutdown(self.ep, self.validator, self.account)
        return 0 if (echo_ok and headers_ok) else 1

    def _create_transport(self):
        tp_cfg = pj.TransportConfig()
        tp_cfg.port = self.args.port or 5061
        if self.args.bind_ip:
            tp_cfg.boundAddress = self.args.bind_ip
        configure_tls(tp_cfg, self.args)
        self.transport_id = self.ep.transportCreate(pj.PJSIP_TRANSPORT_TLS, tp_cfg)
        print(f"TLS server: listening on port {tp_cfg.port}", file=sys.stderr)

    def _create_account(self):
        acfg = pj.AccountConfig()
        bind_addr = self.args.bind_ip or "0.0.0.0"
        port = self.args.port or 5061
        acfg.idUri = f"sip:uas@{bind_addr}:{port};transport=tls"
        acfg.regConfig.registrarUri = ""
        acfg.regConfig.registerOnAdd = False
        acfg.sipConfig.transportId = self.transport_id
        configure_srtp(acfg, self.args.srtp, self.args.srtp_secure)
        if self.args.rtp_port:
            acfg.mediaConfig.transportConfig.port = self.args.rtp_port
        self.account = UasAccount(self)
        self.account.create(acfg)

    def _wait_for_call(self):
        timeout = self.args.wait_timeout
        print(f"Waiting for incoming call (timeout: {timeout}s)...", file=sys.stderr)
        if self.call_completed.wait(timeout=timeout):
            print("Call completed.", file=sys.stderr)
        else:
            print("Timeout waiting for call.", file=sys.stderr)


class UasAccount(pj.Account):
    def __init__(self, app):
        super().__init__()
        self.app = app

    def onIncomingCall(self, prm):
        call = UasCall(self.app, self, prm.callId)

        # Check headers in incoming INVITE
        if self.app.header_mgr.has_checks():
            try:
                msg = prm.rdata.wholeMsg
                if msg:
                    self.app.header_results = self.app.header_mgr.check_headers(msg)
            except (AttributeError, Exception) as e:
                print(f"Header check error: {e}", file=sys.stderr)

        # Answer with 200 OK + custom headers
        call_prm = pj.CallOpParam(True)
        call_prm.statusCode = pj.PJSIP_SC_OK
        if self.app.header_mgr.set_headers:
            call_prm.txOption.headers = self.app.header_mgr.build_sip_headers()
        call.answer(call_prm)
        print("Incoming call answered with 200 OK.", file=sys.stderr)


class UasCall(pj.Call):
    def __init__(self, app, account, call_id):
        super().__init__(account, call_id)
        self.app = app
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

            aud_med = self.getAudioMedia(mi_idx)
            validator = EchoValidatorPort()
            validator.register("echo-validator")
            self.app.validator = validator
            validator.startTransmit(aud_med)
            aud_med.startTransmit(validator)
            print("Echo validator connected.", file=sys.stderr)

    def _hangup(self):
        try:
            prm = pj.CallOpParam()
            prm.statusCode = pj.PJSIP_SC_OK
            self.hangup(prm)
        except pj.Error:
            self.app.call_completed.set()


if __name__ == "__main__":
    args, header_mgr = load_config_and_args("Standard SIP UAS (TLS server)")
    app = UasApp(args, header_mgr)
    safe_exit(app.run())
```

- [ ] **Step 2: Commit**

```bash
git add scripts/uas.py
git commit -m "Add scripts/uas.py: standard UAS mode on PJSUA2 with header support"
```

---

### Task 5: Refactor uac_tls_server.py and uas_tls_client.py to use common.py

**Files:**
- Modify: `scripts/uac_tls_server.py`
- Modify: `scripts/uas_tls_client.py`

Replace duplicated EchoValidatorPort, add header support, use common helpers. Keep mode-specific logic (TLS role decoupling, probe INVITE).

This is a large refactor — import from common.py, remove duplicated classes, add `header_mgr` to both scripts, wire `build_sip_headers()` into `makeCall`/`answer`, wire `check_headers()` into incoming message callbacks.

Key changes per file:
- Remove `EchoValidatorPort` class (use from common)
- Add `sys.path.insert(0, "/scripts")` and import from common
- Add `--config` support via `load_config_and_args()` or `add_common_args()`
- In `makeCall` / `answer`: set `prm.txOption.headers = header_mgr.build_sip_headers()`
- In `onIncomingCall` (UAS): `header_mgr.check_headers(prm.rdata.wholeMsg)`
- In `onCallTsxState` (UAC): check headers in 200 OK response
- Replace `_print_results` / `_shutdown` with common helpers

- [ ] **Step 1: Refactor uac_tls_server.py**

Apply changes: import common, remove EchoValidatorPort, add header support to `_make_call()`, use `load_config_and_args()`.

- [ ] **Step 2: Refactor uas_tls_client.py**

Apply changes: import common, remove EchoValidatorPort, add header support to `onIncomingCall()`, use `load_config_and_args()`.

- [ ] **Step 3: Build and run existing sipssert tests**

```bash
docker build -t pjsua-test .
sipssert tests/
```

Expected: both existing tests PASS.

- [ ] **Step 4: Commit**

```bash
git add scripts/uac_tls_server.py scripts/uas_tls_client.py
git commit -m "Refactor TLS role scripts to use common.py, add header support"
```

---

### Task 6: Simplify entrypoint.sh — route all modes to Python scripts

**Files:**
- Modify: `entrypoint.sh`

- [ ] **Step 1: Rewrite entrypoint.sh**

Replace mode-specific argument building with universal routing. All modes go to Python scripts. Pass `--config` and all CLI args through.

```bash
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

# Route to script
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
```

- [ ] **Step 2: Build and run all sipssert tests**

```bash
docker build -t pjsua-test .
sipssert tests/
```

Expected: all tests PASS.

- [ ] **Step 3: Commit**

```bash
git add entrypoint.sh
git commit -m "Simplify entrypoint.sh: route all modes to Python scripts"
```

---

### Task 7: sipssert test — header set & check

**Files:**
- Create: `tests/pjsua-headers-set-check/scenario.yml`
- Create: `tests/pjsua-headers-set-check/certs/` (symlink or copy from existing)

- [ ] **Step 1: Create test scenario**

```bash
mkdir -p tests/pjsua-headers-set-check/certs
cp tests/pjsua-tls-roles/certs/*.pem tests/pjsua-headers-set-check/certs/
```

Create `tests/pjsua-headers-set-check/scenario.yml`:

```yaml
tasks:
  - name: uas
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uas"
      - "--port=15081"
      - "--rtp-port=18000"
      - "--tls-cert-file=/home/certs/cacert.pem"
      - "--tls-privkey-file=/home/certs/cakey.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=10"
      - "--tolerance=0"
      - "--set-header=X-Reply: world"
      - "--expect-header=X-Test"
      - "--expect-header-value=X-Test: hello"

  - name: uac
    image: pjsua-test
    require:
      - { started: uas }
    args:
      - "--mode=uac"
      - "--proxy=127.0.0.1:15081"
      - "--rtp-port=19000"
      - "--tls-ca-file=/home/certs/cacert.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=5"
      - "--tolerance=0"
      - "--set-header=X-Test: hello"
      - "--expect-header=X-Reply"
      - "--expect-header-value=X-Reply: world"
```

- [ ] **Step 2: Run test**

```bash
sipssert tests/
```

Expected: `pjsua-headers-set-check....PASS`

- [ ] **Step 3: Commit**

```bash
git add tests/pjsua-headers-set-check/
git commit -m "Add sipssert test: header set and check (bidirectional)"
```

---

### Task 8: sipssert test — expect-not

**Files:**
- Create: `tests/pjsua-headers-expect-not/scenario.yml`
- Create: `tests/pjsua-headers-expect-not/certs/`

- [ ] **Step 1: Create test**

```bash
mkdir -p tests/pjsua-headers-expect-not/certs
cp tests/pjsua-tls-roles/certs/*.pem tests/pjsua-headers-expect-not/certs/
```

Create `tests/pjsua-headers-expect-not/scenario.yml`:

```yaml
tasks:
  - name: uas
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uas"
      - "--port=15082"
      - "--rtp-port=18100"
      - "--tls-cert-file=/home/certs/cacert.pem"
      - "--tls-privkey-file=/home/certs/cakey.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=10"
      - "--tolerance=0"
      - "--expect-no-header=X-Secret"
      - "--expect-no-header=X-Internal"

  - name: uac
    image: pjsua-test
    require:
      - { started: uas }
    args:
      - "--mode=uac"
      - "--proxy=127.0.0.1:15082"
      - "--rtp-port=19100"
      - "--tls-ca-file=/home/certs/cacert.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=5"
      - "--tolerance=0"
```

- [ ] **Step 2: Run and verify PASS**

```bash
sipssert tests/
```

- [ ] **Step 3: Commit**

```bash
git add tests/pjsua-headers-expect-not/
git commit -m "Add sipssert test: expect-no-header validation"
```

---

### Task 9: sipssert test — regex and count

**Files:**
- Create: `tests/pjsua-headers-regex/scenario.yml`
- Create: `tests/pjsua-headers-regex/certs/`

- [ ] **Step 1: Create test**

```bash
mkdir -p tests/pjsua-headers-regex/certs
cp tests/pjsua-tls-roles/certs/*.pem tests/pjsua-headers-regex/certs/
```

Create `tests/pjsua-headers-regex/scenario.yml`:

```yaml
tasks:
  - name: uas
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uas"
      - "--port=15083"
      - "--rtp-port=18200"
      - "--tls-cert-file=/home/certs/cacert.pem"
      - "--tls-privkey-file=/home/certs/cakey.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=10"
      - "--tolerance=0"
      - "--expect-header-value-regex=X-Id: ^session-\\d+-[a-z]+$"
      - "--expect-header-regex=^X-Custom-.*"
      - "--expect-no-header-regex=^X-Internal-.*"
      - "--expect-header-count=X-Route: 2"

  - name: uac
    image: pjsua-test
    require:
      - { started: uas }
    args:
      - "--mode=uac"
      - "--proxy=127.0.0.1:15083"
      - "--rtp-port=19200"
      - "--tls-ca-file=/home/certs/cacert.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=5"
      - "--tolerance=0"
      - "--set-header=X-Id: session-42-abc"
      - "--set-header=X-Custom-Foo: bar"
      - "--set-header=X-Route: route-1"
      - "--set-header=X-Route: route-2"
```

- [ ] **Step 2: Run and verify PASS**

- [ ] **Step 3: Commit**

```bash
git add tests/pjsua-headers-regex/
git commit -m "Add sipssert test: regex, name pattern, and count header checks"
```

---

### Task 10: sipssert test — YAML config file

**Files:**
- Create: `tests/pjsua-config-file/scenario.yml`
- Create: `tests/pjsua-config-file/certs/`
- Create: `tests/pjsua-config-file/uac.yml`
- Create: `tests/pjsua-config-file/uas.yml`

- [ ] **Step 1: Create config files and test**

```bash
mkdir -p tests/pjsua-config-file/certs
cp tests/pjsua-tls-roles/certs/*.pem tests/pjsua-config-file/certs/
```

Create `tests/pjsua-config-file/uas.yml`:

```yaml
port: 15084
rtp_port: 18300
duration: 10
tolerance: 0
srtp: mandatory
srtp_secure: 0

tls:
  cert_file: /home/certs/cacert.pem
  privkey_file: /home/certs/cakey.pem

headers:
  expect:
    - "X-From-Config"
  expect_value:
    - "X-From-Config: works"
  set:
    - "X-Reply-Config: ok"
```

Create `tests/pjsua-config-file/uac.yml`:

```yaml
proxy: 127.0.0.1:15084
rtp_port: 19300
duration: 5
tolerance: 0
srtp: mandatory
srtp_secure: 0

tls:
  ca_file: /home/certs/cacert.pem

headers:
  set:
    - "X-From-Config: works"
  expect:
    - "X-Reply-Config"
  expect_value:
    - "X-Reply-Config: ok"
```

Create `tests/pjsua-config-file/scenario.yml`:

```yaml
tasks:
  - name: uas
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uas"
      - "--config=/home/uas.yml"

  - name: uac
    image: pjsua-test
    require:
      - { started: uas }
    args:
      - "--mode=uac"
      - "--config=/home/uac.yml"
```

- [ ] **Step 2: Run and verify PASS**

- [ ] **Step 3: Commit**

```bash
git add tests/pjsua-config-file/
git commit -m "Add sipssert test: all params via YAML config file"
```

---

### Task 11: sipssert test — headers with TLS role decoupling

**Files:**
- Create: `tests/pjsua-headers-tls-roles/scenario.yml`
- Create: `tests/pjsua-headers-tls-roles/certs/`

- [ ] **Step 1: Create test**

```bash
mkdir -p tests/pjsua-headers-tls-roles/certs
cp tests/pjsua-tls-roles/certs/*.pem tests/pjsua-headers-tls-roles/certs/
```

Create `tests/pjsua-headers-tls-roles/scenario.yml`:

```yaml
tasks:
  - name: uac-tls-server
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uac-tls-server"
      - "--proxy=127.0.0.1:15086"
      - "--port=15085"
      - "--rtp-port=18400"
      - "--tls-cert-file=/home/certs/cacert.pem"
      - "--tls-privkey-file=/home/certs/cakey.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=5"
      - "--tls-wait=15"
      - "--tolerance=0"
      - "--set-header=X-From-Server: hello"
      - "--expect-header=X-From-Client"
      - "--expect-header-value=X-From-Client: world"

  - name: uas-tls-client
    image: pjsua-test
    require:
      - { started: uac-tls-server }
    args:
      - "--mode=uas-tls-client"
      - "--proxy=127.0.0.1:15085"
      - "--port=15086"
      - "--rtp-port=19400"
      - "--tls-ca-file=/home/certs/cacert.pem"
      - "--tls-cert-file=/home/certs/cacert.pem"
      - "--tls-privkey-file=/home/certs/cakey.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=10"
      - "--tolerance=0"
      - "--wait-timeout=20"
      - "--set-header=X-From-Client: world"
      - "--expect-header=X-From-Server"
      - "--expect-header-value=X-From-Server: hello"
```

- [ ] **Step 2: Run and verify all tests PASS**

```bash
sipssert tests/
```

Expected: all 7+ tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/pjsua-headers-tls-roles/
git commit -m "Add sipssert test: headers with TLS role decoupling"
```

---

### Task 12: Update documentation

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update README**

Add sections: config file usage, header parameters table, new test descriptions.

- [ ] **Step 2: Update CLAUDE.md**

Add: config file, HeaderManager, all modes on PJSUA2.

- [ ] **Step 3: Commit and push**

```bash
git add README.md CLAUDE.md
git commit -m "Update docs: config file, custom headers, new tests"
git push
```
