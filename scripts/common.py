#!/usr/bin/env python3
"""
common.py — shared module for all pjsua-test scripts.

Provides:
  - EchoValidatorPort: AudioMediaPort that generates deterministic frames and validates echoes
  - parse_sip_headers: parse SIP message text into (name, value) tuples
  - CheckResult: result of a single header check
  - HeaderManager: build/check SIP headers based on config
  - ConfigLoader: load YAML config and merge with argparse args
  - add_common_args: add shared argparse arguments to a parser
  - Helper functions: configure_srtp, configure_tls, init_endpoint, safe_shutdown, etc.
"""

import argparse
import collections
import os
import re
import struct
import sys
import threading
import time

import pjsua2 as pj

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# ---------------------------------------------------------------------------
# EchoValidatorPort
# ---------------------------------------------------------------------------

class EchoValidatorPort(pj.AudioMediaPort):
    """
    Generates deterministic audio frames (4-byte LE counter pattern).
    Captures echoed frames and compares against ring buffer of last 64 sent frames.
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
        """Register this port with the conference bridge."""
        # Must call super().createPort to avoid infinite recursion
        super().createPort(name, self.fmt)

    def onFrameRequested(self, frame):
        """Generate a deterministic audio frame (4-byte LE counter pattern)."""
        size = frame.size
        if size <= 0:
            size = 320  # 160 samples * 16-bit

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
        """Return dict with sent/received/matched/mismatched/match_pct."""
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
# parse_sip_headers
# ---------------------------------------------------------------------------

def parse_sip_headers(whole_msg: str) -> list:
    """
    Parse SIP message text into list of (name, value) tuples.

    - Skips the first line (request/status line)
    - Handles header folding (continuation lines starting with space/tab)
    - Stops at blank line (header/body separator)
    """
    # Normalize line endings
    text = whole_msg.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    headers = []
    current_name = None
    current_value = None

    # Skip first line (request/status line)
    for line in lines[1:]:
        # Blank line = end of headers
        if line == "":
            break

        # Continuation line (header folding)
        if line and line[0] in (" ", "\t"):
            if current_name is not None:
                current_value = current_value + " " + line.strip()
            continue

        # Save previous header
        if current_name is not None:
            headers.append((current_name, current_value))

        # Parse new header
        colon_pos = line.find(":")
        if colon_pos > 0:
            current_name = line[:colon_pos].strip()
            current_value = line[colon_pos + 1:].strip()
        else:
            # Malformed line — skip
            current_name = None
            current_value = None

    # Save last header
    if current_name is not None:
        headers.append((current_name, current_value))

    return headers


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------

class CheckResult:
    """Result of a single header check."""

    def __init__(self, check_type: str, target: str, passed: bool, detail: str):
        self.check_type = check_type
        self.target = target
        self.passed = passed
        self.detail = detail

    def __str__(self):
        status = "PASS" if self.passed else "FAIL"
        return f"  [{status}] {self.check_type}: {self.target} — {self.detail}"


# ---------------------------------------------------------------------------
# HeaderManager
# ---------------------------------------------------------------------------

class HeaderManager:
    """
    Manages SIP header checks and building from config dict.

    Config keys:
      set              - list of "Name: Value" strings to add to outgoing requests
      expect           - list of header names that must exist
      expect_not       - list of header names that must NOT exist
      expect_name_regex      - list of regexes, at least one header name must match each
      expect_not_regex       - list of regexes, no header name must match each
      expect_value           - list of "Name: value" or "Name[N]: value" exact matches
      expect_value_regex     - list of "Name: regex" or "Name[N]: regex" value matches
      expect_count           - list of "Name: N", "Name: N+", or "Name: N-M" count checks
    """

    def __init__(self, config: dict):
        self.config = config or {}

    def has_checks(self) -> bool:
        """Return True if any check is configured."""
        check_keys = [
            "expect", "expect_not", "expect_name_regex", "expect_not_regex",
            "expect_value", "expect_value_regex", "expect_count",
        ]
        for key in check_keys:
            val = self.config.get(key)
            if val:
                return True
        return False

    def build_sip_headers(self):
        """
        Build pj.SipHeaderVector from 'set' list.
        Each entry is "Name: Value" string.
        """
        hv = pj.SipHeaderVector()
        set_list = self.config.get("set") or []
        for entry in set_list:
            colon_pos = entry.find(":")
            if colon_pos > 0:
                h = pj.SipHeader()
                h.hName = entry[:colon_pos].strip()
                h.hValue = entry[colon_pos + 1:].strip()
                hv.push_back(h)
        return hv

    def check_headers(self, whole_msg: str) -> list:
        """
        Run all checks against parsed headers.
        Returns list of CheckResult.
        """
        headers = parse_sip_headers(whole_msg)
        results = []

        # expect: header name exists (case-insensitive)
        for name in (self.config.get("expect") or []):
            found = any(h[0].lower() == name.lower() for h in headers)
            results.append(CheckResult(
                "expect", name, found,
                "found" if found else "not found"
            ))

        # expect_not: header name does NOT exist
        for name in (self.config.get("expect_not") or []):
            found = any(h[0].lower() == name.lower() for h in headers)
            results.append(CheckResult(
                "expect_not", name, not found,
                "not found (ok)" if not found else "found (unexpected)"
            ))

        # expect_name_regex: at least one header name matches regex
        for pattern in (self.config.get("expect_name_regex") or []):
            rx = re.compile(pattern)
            matched_names = [h[0] for h in headers if rx.search(h[0])]
            passed = len(matched_names) > 0
            results.append(CheckResult(
                "expect_name_regex", pattern, passed,
                f"matched: {matched_names}" if passed else "no header name matched"
            ))

        # expect_not_regex: NO header name matches regex
        for pattern in (self.config.get("expect_not_regex") or []):
            rx = re.compile(pattern)
            matched_names = [h[0] for h in headers if rx.search(h[0])]
            passed = len(matched_names) == 0
            results.append(CheckResult(
                "expect_not_regex", pattern, passed,
                "no match (ok)" if passed else f"unexpected matches: {matched_names}"
            ))

        # expect_value: exact value match with optional index
        for entry in (self.config.get("expect_value") or []):
            result = self._check_value(headers, entry, regex=False)
            results.append(result)

        # expect_value_regex: regex value match with optional index
        for entry in (self.config.get("expect_value_regex") or []):
            result = self._check_value(headers, entry, regex=True)
            results.append(result)

        # expect_count: count checks
        for entry in (self.config.get("expect_count") or []):
            result = self._check_count(headers, entry)
            results.append(result)

        return results

    def _parse_indexed_entry(self, entry: str):
        """
        Parse "Name[N]: value" or "Name: value" into (name, index_or_None, value).
        Supports negative index [-1].
        """
        colon_pos = entry.find(":")
        if colon_pos <= 0:
            return (entry.strip(), None, "")

        name_part = entry[:colon_pos].strip()
        value_part = entry[colon_pos + 1:].strip()

        # Check for index: Name[N]
        bracket_match = re.match(r'^(.+?)\[(-?\d+)\]$', name_part)
        if bracket_match:
            name = bracket_match.group(1).strip()
            index = int(bracket_match.group(2))
            return (name, index, value_part)

        return (name_part, None, value_part)

    def _check_value(self, headers: list, entry: str, regex: bool) -> CheckResult:
        """Check expect_value or expect_value_regex."""
        check_type = "expect_value_regex" if regex else "expect_value"
        name, index, expected_value = self._parse_indexed_entry(entry)

        # Collect all values for this header name (case-insensitive)
        matching = [h[1] for h in headers if h[0].lower() == name.lower()]

        if index is not None:
            # Indexed access
            try:
                actual_value = matching[index]
            except IndexError:
                return CheckResult(
                    check_type, entry, False,
                    f"index {index} out of range (only {len(matching)} values)"
                )
            if regex:
                passed = bool(re.search(expected_value, actual_value))
                detail = (f"value '{actual_value}' matches '{expected_value}'"
                          if passed else
                          f"value '{actual_value}' does not match '{expected_value}'")
            else:
                passed = actual_value == expected_value
                detail = (f"value matches" if passed else
                          f"expected '{expected_value}', got '{actual_value}'")
        else:
            # Any value matches
            if not matching:
                return CheckResult(check_type, entry, False, f"header '{name}' not found")

            if regex:
                passed = any(bool(re.search(expected_value, v)) for v in matching)
                detail = (f"at least one value matches '{expected_value}'"
                          if passed else
                          f"no value matches '{expected_value}' in {matching}")
            else:
                passed = any(v == expected_value for v in matching)
                detail = (f"value found" if passed else
                          f"value '{expected_value}' not found in {matching}")

        return CheckResult(check_type, entry, passed, detail)

    def _check_count(self, headers: list, entry: str) -> CheckResult:
        """Check expect_count: 'Name: N', 'Name: N+', or 'Name: N-M'."""
        colon_pos = entry.find(":")
        if colon_pos <= 0:
            return CheckResult("expect_count", entry, False, "invalid format")

        name = entry[:colon_pos].strip()
        count_spec = entry[colon_pos + 1:].strip()

        actual_count = sum(1 for h in headers if h[0].lower() == name.lower())

        # Parse count spec
        range_match = re.match(r'^(\d+)-(\d+)$', count_spec)
        min_match = re.match(r'^(\d+)\+$', count_spec)
        exact_match = re.match(r'^(\d+)$', count_spec)

        if range_match:
            lo, hi = int(range_match.group(1)), int(range_match.group(2))
            passed = lo <= actual_count <= hi
            detail = (f"count {actual_count} in [{lo}, {hi}]"
                      if passed else
                      f"count {actual_count} not in [{lo}, {hi}]")
        elif min_match:
            minimum = int(min_match.group(1))
            passed = actual_count >= minimum
            detail = (f"count {actual_count} >= {minimum}"
                      if passed else
                      f"count {actual_count} < {minimum}")
        elif exact_match:
            expected = int(exact_match.group(1))
            passed = actual_count == expected
            detail = (f"count {actual_count} == {expected}"
                      if passed else
                      f"count {actual_count} != {expected}")
        else:
            return CheckResult("expect_count", entry, False,
                               f"invalid count spec '{count_spec}'")

        return CheckResult("expect_count", entry, passed, detail)

    @staticmethod
    def print_report(results: list) -> bool:
        """
        Print check results to stderr.
        Returns True if all checks passed.
        """
        all_passed = True
        print("Header check results:", file=sys.stderr)
        for r in results:
            print(str(r), file=sys.stderr)
            if not r.passed:
                all_passed = False
        if all_passed:
            print("  All checks PASSED.", file=sys.stderr)
        else:
            print("  Some checks FAILED.", file=sys.stderr)
        return all_passed


# ---------------------------------------------------------------------------
# ConfigLoader
# ---------------------------------------------------------------------------

class ConfigLoader:
    """Load YAML config and merge with argparse Namespace."""

    @staticmethod
    def load(path: str) -> dict:
        """Read YAML file, return dict."""
        if not _YAML_AVAILABLE:
            raise ImportError("PyYAML is not available; cannot load config file.")
        with open(path, "r") as f:
            data = yaml.safe_load(f) or {}
        return data

    @staticmethod
    def merge(config: dict, args: argparse.Namespace) -> argparse.Namespace:
        """
        Apply config values to argparse Namespace where args have default/empty values.

        Config keys mapped to args attributes:
          mode            -> args.mode
          proxy           -> args.proxy
          port            -> args.port
          ip              -> args.bind_ip
          rtp_port        -> args.rtp_port
          duration        -> args.duration
          tolerance       -> args.tolerance
          wait_timeout    -> args.wait_timeout
          tls_wait        -> args.tls_wait
          srtp            -> args.srtp
          srtp_secure     -> args.srtp_secure
          dest_uri        -> args.dest_uri
          log_level       -> args.log_level
          tls.cert_file       -> args.tls_cert_file
          tls.privkey_file    -> args.tls_privkey_file
          tls.ca_file         -> args.tls_ca_file
          tls.verify_server   -> args.tls_verify_server
          tls.verify_client   -> args.tls_verify_client
        """
        # Flat key -> args attribute mapping
        flat_map = {
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

        for cfg_key, arg_attr in flat_map.items():
            if cfg_key in config:
                # Only override if the arg is at its default/empty value
                current = getattr(args, arg_attr, None)
                if _is_default(current):
                    setattr(args, arg_attr, config[cfg_key])

        # TLS subkey
        tls_cfg = config.get("tls", {}) or {}
        tls_map = {
            "cert_file": "tls_cert_file",
            "privkey_file": "tls_privkey_file",
            "ca_file": "tls_ca_file",
            "verify_server": "tls_verify_server",
            "verify_client": "tls_verify_client",
        }
        for cfg_key, arg_attr in tls_map.items():
            if cfg_key in tls_cfg:
                current = getattr(args, arg_attr, None)
                if _is_default(current):
                    setattr(args, arg_attr, tls_cfg[cfg_key])

        return args

    @staticmethod
    def merge_headers(config: dict, args: argparse.Namespace) -> dict:
        """
        Merge 'headers:' from config with CLI header args.
        CLI args append to config lists.

        Returns merged header config dict.
        """
        headers_cfg = config.get("headers", {}) or {}
        merged = {}

        # Map from config key -> args attribute
        header_keys = {
            "set": "set_header",
            "expect": "expect_header",
            "expect_not": "expect_no_header",
            "expect_name_regex": "expect_header_regex",
            "expect_not_regex": "expect_no_header_regex",
            "expect_value": "expect_header_value",
            "expect_value_regex": "expect_header_value_regex",
            "expect_count": "expect_header_count",
        }

        for cfg_key, arg_attr in header_keys.items():
            cfg_list = list(headers_cfg.get(cfg_key, None) or [])
            cli_list = list(getattr(args, arg_attr, None) or [])
            combined = cfg_list + cli_list
            merged[cfg_key] = combined if combined else None

        return merged


def _is_default(value) -> bool:
    """Return True if value is considered a default/empty value that config can override."""
    if value is None:
        return True
    if value == "":
        return True
    if value is False:
        return True
    if value == 0:
        return True
    return False


# ---------------------------------------------------------------------------
# add_common_args
# ---------------------------------------------------------------------------

def add_common_args(parser: argparse.ArgumentParser):
    """Add all shared argparse arguments to parser."""

    # Config file
    parser.add_argument("--config", default="",
                        help="YAML config file path")

    # SIP/network
    parser.add_argument("--proxy", default="",
                        help="SIP proxy URI")
    parser.add_argument("--port", type=int, default=0,
                        help="Local SIP port (0 = auto)")
    parser.add_argument("--bind-ip", default="",
                        help="Bind to specific IP address")
    parser.add_argument("--rtp-port", type=int, default=0,
                        help="Local RTP port (0 = auto)")
    parser.add_argument("--dest-uri", default="",
                        help="Destination SIP URI")

    # TLS
    parser.add_argument("--tls", action="store_true",
                        help="Enable TLS transport (no-op: TLS is always used; kept for compatibility)")
    parser.add_argument("--tls-ca-file", default="",
                        help="CA certificate file")
    parser.add_argument("--tls-cert-file", default="",
                        help="TLS certificate file")
    parser.add_argument("--tls-privkey-file", default="",
                        help="TLS private key file")
    parser.add_argument("--tls-verify-server", action="store_true",
                        help="Verify server TLS certificate")
    parser.add_argument("--tls-verify-client", action="store_true",
                        help="Verify client TLS certificate (mTLS)")

    # SRTP
    parser.add_argument("--srtp", choices=["off", "optional", "mandatory"],
                        default=None, help="SRTP mode (default: off)")
    parser.add_argument("--srtp-secure", type=int, choices=[0, 1, 2], default=None,
                        help="SRTP secure signaling requirement (default: 0)")

    # Timing
    parser.add_argument("--duration", type=int, default=None,
                        help="Call duration in seconds (default: 10)")
    parser.add_argument("--tolerance", type=float, default=None,
                        help="Minimum match percentage to pass (default: 90)")
    parser.add_argument("--wait-timeout", type=int, default=None,
                        help="Max seconds to wait for incoming call (default: 30)")
    parser.add_argument("--tls-wait", type=int, default=None,
                        help="Max seconds to wait for TLS connection (default: 10)")

    # Header checks
    parser.add_argument("--set-header", action="append", metavar="NAME: VALUE",
                        help="Add SIP header to outgoing requests (repeatable)")
    parser.add_argument("--expect-header", action="append", metavar="NAME",
                        help="Assert header name exists (repeatable)")
    parser.add_argument("--expect-no-header", action="append", metavar="NAME",
                        help="Assert header name does NOT exist (repeatable)")
    parser.add_argument("--expect-header-regex", action="append", metavar="REGEX",
                        help="Assert at least one header name matches regex (repeatable)")
    parser.add_argument("--expect-no-header-regex", action="append", metavar="REGEX",
                        help="Assert no header name matches regex (repeatable)")
    parser.add_argument("--expect-header-value", action="append",
                        metavar="NAME[N]: VALUE",
                        help="Assert exact header value (repeatable)")
    parser.add_argument("--expect-header-value-regex", action="append",
                        metavar="NAME[N]: REGEX",
                        help="Assert header value matches regex (repeatable)")
    parser.add_argument("--expect-header-count", action="append",
                        metavar="NAME: N|N+|N-M",
                        help="Assert header occurrence count (repeatable)")

    # Logging
    parser.add_argument("--log-level", type=int, default=3,
                        help="PJSIP log level 0-6 (default: 3)")


# ---------------------------------------------------------------------------
# SRTP / TLS helpers
# ---------------------------------------------------------------------------

SRTP_MAP = {
    "off": pj.PJMEDIA_SRTP_DISABLED,
    "optional": pj.PJMEDIA_SRTP_OPTIONAL,
    "mandatory": pj.PJMEDIA_SRTP_MANDATORY,
}


def configure_srtp(acfg: pj.AccountConfig, srtp: str, srtp_secure: int):
    """Set SRTP on AccountConfig."""
    acfg.mediaConfig.srtpUse = SRTP_MAP.get(srtp, pj.PJMEDIA_SRTP_DISABLED)
    acfg.mediaConfig.srtpSecureSignaling = srtp_secure


def configure_tls(tp_cfg: pj.TransportConfig, args: argparse.Namespace):
    """Set TLS on TransportConfig from args."""
    tls = tp_cfg.tlsConfig
    tls.method = pj.PJSIP_TLSV1_2_METHOD

    if getattr(args, "tls_ca_file", ""):
        tls.CaListFile = args.tls_ca_file
    if getattr(args, "tls_cert_file", ""):
        tls.certFile = args.tls_cert_file
    if getattr(args, "tls_privkey_file", ""):
        tls.privKeyFile = args.tls_privkey_file

    tls.verifyServer = bool(getattr(args, "tls_verify_server", False))
    tls.verifyClient = bool(getattr(args, "tls_verify_client", False))


# ---------------------------------------------------------------------------
# Endpoint helpers
# ---------------------------------------------------------------------------

def init_endpoint(args: argparse.Namespace) -> pj.Endpoint:
    """Create Endpoint, init, setNullDev, start. Returns endpoint."""
    ep = pj.Endpoint()
    ep_cfg = pj.EpConfig()
    log_level = getattr(args, "log_level", 3)
    ep_cfg.logConfig.level = log_level
    ep_cfg.logConfig.consoleLevel = log_level
    ep_cfg.medConfig.noVad = True

    ep.libCreate()
    ep.libInit(ep_cfg)
    ep.audDevManager().setNullDev()
    ep.libStart()
    return ep


def safe_shutdown(ep: pj.Endpoint, validator=None, account=None):
    """Hang up all calls, delete refs, destroy library. All errors suppressed."""
    try:
        ep.hangupAllCalls()
        time.sleep(0.5)
    except Exception:
        pass

    # Drop references to prevent use-after-free
    if validator is not None:
        validator = None  # noqa: F841
    if account is not None:
        account = None  # noqa: F841

    try:
        ep.libDestroy()
    except Exception:
        pass


def safe_exit(rc: int):
    """Exit using os._exit to avoid PJSUA2 cleanup segfaults."""
    os._exit(rc)


# ---------------------------------------------------------------------------
# Echo result helpers
# ---------------------------------------------------------------------------

def print_echo_results(validator: EchoValidatorPort, tolerance: float) -> bool:
    """
    Print echo validation stats to stderr.
    Returns True if match_pct >= tolerance.
    """
    if validator is None:
        print("NO MEDIA — validator was never connected.", file=sys.stderr)
        return False

    stats = validator.get_stats()
    print(f"\n{'=' * 50}", file=sys.stderr)
    print("RTP/SRTP Echo Validation Results:", file=sys.stderr)
    print(f"  Frames sent:       {stats['sent']}", file=sys.stderr)
    print(f"  Frames received:   {stats['received']}", file=sys.stderr)
    print(f"  Frames matched:    {stats['matched']}", file=sys.stderr)
    print(f"  Frames mismatched: {stats['mismatched']}", file=sys.stderr)
    print(f"  Match rate:        {stats['match_pct']:.1f}%", file=sys.stderr)
    print(f"  Tolerance:         {tolerance}%", file=sys.stderr)

    passed = stats["match_pct"] >= tolerance
    print(f"  RESULT: {'PASS' if passed else 'FAIL'}", file=sys.stderr)
    print(f"{'=' * 50}\n", file=sys.stderr)
    return passed


# ---------------------------------------------------------------------------
# load_config_and_args
# ---------------------------------------------------------------------------

_ARG_DEFAULTS = {
    "srtp": "off",
    "srtp_secure": 0,
    "duration": 10,
    "tolerance": 90.0,
    "wait_timeout": 30,
    "tls_wait": 10,
}


def _apply_arg_defaults(args: argparse.Namespace):
    """Fill in any args still at None with their real default values."""
    for attr, default in _ARG_DEFAULTS.items():
        if getattr(args, attr, None) is None:
            setattr(args, attr, default)


def load_config_and_args(description: str):
    """
    Parse args, load config, merge, apply defaults, create HeaderManager.
    Returns (args, header_mgr).
    """
    parser = argparse.ArgumentParser(description=description)
    add_common_args(parser)
    args = parser.parse_args()

    config = {}
    if getattr(args, "config", ""):
        config = ConfigLoader.load(args.config)
        ConfigLoader.merge(config, args)

    # Fill in real defaults for any args not set by CLI or config
    _apply_arg_defaults(args)

    header_cfg = ConfigLoader.merge_headers(config, args)
    header_mgr = HeaderManager(header_cfg)

    return args, header_mgr
