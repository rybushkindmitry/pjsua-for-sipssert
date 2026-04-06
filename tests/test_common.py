#!/usr/bin/env python3
"""Unit tests for common.py — edge cases, None handling, defaults."""

import argparse
import struct
import sys
import threading
import unittest

sys.path.insert(0, "/scripts")
# Allow running outside Docker (pjsua2 not available)
try:
    import pjsua2 as pj
    _PJ_AVAILABLE = True
except ImportError:
    _PJ_AVAILABLE = False

from common import (
    _parse_bool,
    _apply_arg_defaults,
    _build_ulaw_quantize_table,
    _build_stable_values,
    _STABLE_VALUES,
    configure_srtp,
    ConfigLoader,
    HeaderManager,
)


# -----------------------------------------------------------------------
# _parse_bool
# -----------------------------------------------------------------------

class TestParseBool(unittest.TestCase):

    def test_true_variants(self):
        for v in ("true", "True", "TRUE", "yes", "Yes", "1"):
            self.assertTrue(_parse_bool(v), f"Expected True for {v!r}")

    def test_false_variants(self):
        for v in ("false", "False", "FALSE", "no", "No", "0"):
            self.assertFalse(_parse_bool(v), f"Expected False for {v!r}")

    def test_bool_passthrough(self):
        self.assertTrue(_parse_bool(True))
        self.assertFalse(_parse_bool(False))

    def test_invalid_string_raises(self):
        for v in ("maybe", "on", "off", "2", "yes!", ""):
            with self.assertRaises(argparse.ArgumentTypeError, msg=f"Should raise for {v!r}"):
                _parse_bool(v)

    def test_none_raises(self):
        with self.assertRaises((argparse.ArgumentTypeError, AttributeError)):
            _parse_bool(None)


# -----------------------------------------------------------------------
# _apply_arg_defaults
# -----------------------------------------------------------------------

class TestApplyArgDefaults(unittest.TestCase):

    def _make_args(self, **kwargs):
        args = argparse.Namespace(
            tls=False, transport=None, srtp=None, srtp_secure=None,
            duration=None, tolerance=None, wait_timeout=None,
            tls_wait=None, wait_bye=None, options_tolerance=None,
            reinvite_delay=None, reinvite_by=None,
        )
        for k, v in kwargs.items():
            setattr(args, k, v)
        return args

    def test_fills_none_with_defaults(self):
        args = self._make_args()
        _apply_arg_defaults(args)
        self.assertEqual(args.duration, 10)
        self.assertEqual(args.tolerance, 90.0)
        self.assertEqual(args.wait_timeout, 30)
        self.assertEqual(args.tls_wait, 10)
        self.assertEqual(args.wait_bye, 30)
        self.assertEqual(args.srtp, "off")
        self.assertEqual(args.srtp_secure, 0)
        self.assertEqual(args.transport, "tls")

    def test_explicit_values_not_overridden(self):
        args = self._make_args(duration=5, transport="udp", srtp="mandatory")
        _apply_arg_defaults(args)
        self.assertEqual(args.duration, 5)
        self.assertEqual(args.transport, "udp")
        self.assertEqual(args.srtp, "mandatory")

    def test_tls_flag_overrides_transport(self):
        args = self._make_args(tls=True, transport=None)
        _apply_arg_defaults(args)
        self.assertEqual(args.transport, "tls")

    def test_tls_flag_no_override_explicit_transport(self):
        args = self._make_args(tls=True, transport="tcp")
        _apply_arg_defaults(args)
        self.assertEqual(args.transport, "tcp")

    def test_reinvite_delay_parsing(self):
        args = self._make_args(reinvite_delay="3,7,12", reinvite_by="uac")
        _apply_arg_defaults(args)
        self.assertEqual(args.reinvite_delays, [3.0, 7.0, 12.0])

    def test_reinvite_delay_none(self):
        args = self._make_args(reinvite_delay=None)
        _apply_arg_defaults(args)
        self.assertEqual(args.reinvite_delays, [])

    def test_reinvite_delay_single(self):
        args = self._make_args(reinvite_delay="5", reinvite_by="uac")
        _apply_arg_defaults(args)
        self.assertEqual(args.reinvite_delays, [5.0])

    def test_zero_duration_preserved(self):
        """duration=0 is falsy but should NOT be replaced with default."""
        args = self._make_args(duration=0)
        _apply_arg_defaults(args)
        # 0 is not None, so _apply_arg_defaults should keep it
        self.assertEqual(args.duration, 0)


# -----------------------------------------------------------------------
# configure_srtp
# -----------------------------------------------------------------------

@unittest.skipUnless(_PJ_AVAILABLE, "pjsua2 not available")
class TestConfigureSrtp(unittest.TestCase):

    def test_none_srtp(self):
        acfg = pj.AccountConfig()
        configure_srtp(acfg, None, None)
        self.assertEqual(acfg.mediaConfig.srtpUse, pj.PJMEDIA_SRTP_DISABLED)
        self.assertEqual(acfg.mediaConfig.srtpSecureSignaling, 0)

    def test_empty_string_srtp(self):
        acfg = pj.AccountConfig()
        configure_srtp(acfg, "", 0)
        self.assertEqual(acfg.mediaConfig.srtpUse, pj.PJMEDIA_SRTP_DISABLED)

    def test_mandatory_srtp(self):
        acfg = pj.AccountConfig()
        configure_srtp(acfg, "mandatory", 1)
        self.assertEqual(acfg.mediaConfig.srtpUse, pj.PJMEDIA_SRTP_MANDATORY)
        self.assertEqual(acfg.mediaConfig.srtpSecureSignaling, 1)

    def test_unknown_srtp_value(self):
        acfg = pj.AccountConfig()
        configure_srtp(acfg, "invalid_value", 0)
        self.assertEqual(acfg.mediaConfig.srtpUse, pj.PJMEDIA_SRTP_DISABLED)

    def test_srtp_secure_zero_not_replaced(self):
        """srtp_secure=0 is valid and should not be treated as None."""
        acfg = pj.AccountConfig()
        configure_srtp(acfg, "off", 0)
        self.assertEqual(acfg.mediaConfig.srtpSecureSignaling, 0)


# -----------------------------------------------------------------------
# ConfigLoader.merge
# -----------------------------------------------------------------------

class TestConfigLoaderMerge(unittest.TestCase):

    def _make_args(self, **kwargs):
        args = argparse.Namespace(
            proxy=None, port=None, bind_ip=None, duration=None,
            srtp=None, srtp_secure=None, transport=None,
            tls_cert_file=None, tls_privkey_file=None,
            tls_ca_file=None, tls_verify_server=None,
            tls_verify_client=None, tolerance=None,
            wait_timeout=None, tls_wait=None, log_level=None,
            bye=None, wait_bye=None, dest_uri=None,
            rtp_port=None, options_ping=None,
            options_auto_reply=None, options_tolerance=None,
            reinvite_by=None, reinvite_delay=None,
            ruri_user=None, from_user=None,
        )
        for k, v in kwargs.items():
            setattr(args, k, v)
        return args

    def test_empty_config(self):
        args = self._make_args(duration=5)
        ConfigLoader.merge({}, args)
        self.assertEqual(args.duration, 5)

    def test_config_fills_none(self):
        args = self._make_args()
        ConfigLoader.merge({"duration": 20, "proxy": "1.2.3.4:5060"}, args)
        self.assertEqual(args.duration, 20)
        self.assertEqual(args.proxy, "1.2.3.4:5060")

    def test_cli_takes_precedence(self):
        args = self._make_args(duration=5)
        ConfigLoader.merge({"duration": 20}, args)
        self.assertEqual(args.duration, 5)

    def test_tls_config_section(self):
        args = self._make_args()
        config = {"tls": {"cert_file": "/cert.pem", "verify_server": True}}
        ConfigLoader.merge(config, args)
        self.assertEqual(args.tls_cert_file, "/cert.pem")
        self.assertEqual(args.tls_verify_server, True)

    def test_tls_config_none(self):
        args = self._make_args()
        config = {"tls": None}
        ConfigLoader.merge(config, args)
        # Should not crash

    def test_config_none_value(self):
        args = self._make_args()
        ConfigLoader.merge({"duration": None}, args)
        # None from config should not override None in args (both None)
        self.assertIsNone(args.duration)


# -----------------------------------------------------------------------
# HeaderManager
# -----------------------------------------------------------------------

class TestHeaderManager(unittest.TestCase):

    def test_empty_config(self):
        hm = HeaderManager({})
        self.assertFalse(hm.has_checks())

    def test_none_config(self):
        hm = HeaderManager(None)
        self.assertFalse(hm.has_checks())

    def test_check_headers_empty_message(self):
        hm = HeaderManager({"expect": ["Via"]})
        results = hm.check_headers("")
        self.assertTrue(len(results) > 0)
        self.assertFalse(results[0].passed)

    def test_expect_header_present(self):
        msg = "SIP/2.0 200 OK\r\nVia: SIP/2.0/TLS 1.2.3.4\r\nContent-Length: 0\r\n\r\n"
        hm = HeaderManager({"expect": ["Via"]})
        results = hm.check_headers(msg)
        self.assertTrue(results[0].passed)

    def test_expect_header_absent(self):
        msg = "SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n"
        hm = HeaderManager({"expect": ["X-Custom"]})
        results = hm.check_headers(msg)
        self.assertFalse(results[0].passed)

    def test_expect_no_header(self):
        msg = "SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n"
        hm = HeaderManager({"expect_not": ["X-Forbidden"]})
        results = hm.check_headers(msg)
        self.assertTrue(results[0].passed)

    def test_expect_no_header_but_present(self):
        msg = "SIP/2.0 200 OK\r\nX-Forbidden: yes\r\nContent-Length: 0\r\n\r\n"
        hm = HeaderManager({"expect_not": ["X-Forbidden"]})
        results = hm.check_headers(msg)
        self.assertFalse(results[0].passed)

    def test_expect_value_with_index(self):
        msg = "SIP/2.0 200 OK\r\nVia: first\r\nVia: second\r\n\r\n"
        hm = HeaderManager({"expect_value": ["Via[1]: second"]})
        results = hm.check_headers(msg)
        self.assertTrue(results[0].passed)

    def test_expect_value_negative_index(self):
        msg = "SIP/2.0 200 OK\r\nVia: first\r\nVia: second\r\n\r\n"
        hm = HeaderManager({"expect_value": ["Via[-1]: second"]})
        results = hm.check_headers(msg)
        self.assertTrue(results[0].passed)

    def test_expect_count_exact(self):
        msg = "SIP/2.0 200 OK\r\nVia: a\r\nVia: b\r\n\r\n"
        hm = HeaderManager({"expect_count": ["Via: 2"]})
        results = hm.check_headers(msg)
        self.assertTrue(results[0].passed)

    def test_expect_count_min(self):
        msg = "SIP/2.0 200 OK\r\nVia: a\r\nVia: b\r\nVia: c\r\n\r\n"
        hm = HeaderManager({"expect_count": ["Via: 2+"]})
        results = hm.check_headers(msg)
        self.assertTrue(results[0].passed)

    def test_expect_count_range(self):
        msg = "SIP/2.0 200 OK\r\nVia: a\r\nVia: b\r\n\r\n"
        hm = HeaderManager({"expect_count": ["Via: 1-3"]})
        results = hm.check_headers(msg)
        self.assertTrue(results[0].passed)

    def test_expect_count_fail(self):
        msg = "SIP/2.0 200 OK\r\nVia: a\r\n\r\n"
        hm = HeaderManager({"expect_count": ["Via: 2"]})
        results = hm.check_headers(msg)
        self.assertFalse(results[0].passed)


# -----------------------------------------------------------------------
# μ-law stable values
# -----------------------------------------------------------------------

class TestStableValues(unittest.TestCase):

    def test_stable_values_not_empty(self):
        self.assertTrue(len(_STABLE_VALUES) > 0)

    def test_no_zero_in_stable_values(self):
        self.assertNotIn(0, _STABLE_VALUES)

    def test_stable_values_in_range(self):
        for v in _STABLE_VALUES:
            self.assertGreaterEqual(abs(v), 500)
            self.assertLessEqual(abs(v), 20000)

    def test_stable_values_are_ulaw_fixed_points(self):
        """Each value should survive μ-law encode→decode unchanged."""
        table = _build_ulaw_quantize_table()
        for v in _STABLE_VALUES:
            unsigned = v & 0xFFFF
            stable = table[unsigned]
            # Convert back to signed for comparison
            stable_signed = stable if stable < 32768 else stable - 65536
            self.assertEqual(v, stable_signed,
                             f"Value {v} is not a μ-law fixed point")

    def test_stable_values_are_unique(self):
        self.assertEqual(len(_STABLE_VALUES), len(set(_STABLE_VALUES)))

    def test_has_positive_and_negative(self):
        pos = [v for v in _STABLE_VALUES if v > 0]
        neg = [v for v in _STABLE_VALUES if v < 0]
        self.assertTrue(len(pos) > 10)
        self.assertTrue(len(neg) > 10)


# -----------------------------------------------------------------------
# EchoValidatorPort (without pjsua2 runtime)
# -----------------------------------------------------------------------

@unittest.skipUnless(_PJ_AVAILABLE, "pjsua2 not available")
class TestEchoValidatorPort(unittest.TestCase):

    _ep = None

    @classmethod
    def setUpClass(cls):
        """Initialize PJSUA2 endpoint once for all tests."""
        ep = pj.Endpoint()
        ep_cfg = pj.EpConfig()
        ep_cfg.logConfig.level = 0
        ep_cfg.logConfig.consoleLevel = 0
        ep.libCreate()
        ep.libInit(ep_cfg)
        ep.audDevManager().setNullDev()
        ep.libStart()
        cls._ep = ep

    @classmethod
    def tearDownClass(cls):
        if cls._ep:
            try:
                cls._ep.libDestroy()
            except Exception:
                pass

    def _make_frame(self, size=320):
        class FakeFrame:
            pass
        f = FakeFrame()
        f.size = size
        f.buf = None
        f.type = 0
        return f

    def test_frame_generation_deterministic(self):
        from common import EchoValidatorPort
        v = EchoValidatorPort()
        v.register("test")

        f1 = self._make_frame()
        v.onFrameRequested(f1)
        data1 = bytes(f1.buf)

        f2 = self._make_frame()
        v.onFrameRequested(f2)
        data2 = bytes(f2.buf)

        self.assertEqual(len(data1), 320)
        self.assertEqual(len(data2), 320)
        self.assertNotEqual(data1, data2)

    def test_frame_constant_fill(self):
        """Each frame should be filled with a single constant value."""
        from common import EchoValidatorPort
        v = EchoValidatorPort()
        v.register("test2")

        f = self._make_frame()
        v.onFrameRequested(f)
        data = bytes(f.buf)
        samples = struct.unpack("<160h", data)
        self.assertEqual(len(set(samples)), 1, "Frame should have single constant value")

    def test_default_frame_size(self):
        """frame.size <= 0 should default to 320."""
        from common import EchoValidatorPort
        v = EchoValidatorPort()
        v.register("test3")

        f = self._make_frame(size=0)
        v.onFrameRequested(f)
        self.assertEqual(f.size, 320)


if __name__ == "__main__":
    unittest.main()
