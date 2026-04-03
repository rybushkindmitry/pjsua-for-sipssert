# pjsua-test

[Русская версия](README.ru.md)

A PJSIP/PJSUA2-based Docker image for testing SIP over TLS with RTP/SRTP validation.
Designed to be used with [SIPssert](https://github.com/OpenSIPS/SIPssert) as a replacement for SIPp
in scenarios where SIPp has architectural limitations.

## Motivation

SIPp (v3.7) has several issues that cannot be resolved without patching the source code:

| Problem | Root cause in SIPp | Solution in pjsua-test |
|---|---|---|
| Poor SRTP validation performance | Custom JLSRTP implementation: excessive `std::vector` allocations, global mutexes, no zero-copy | PJSIP uses libsrtp2 with in-place encryption |
| TLS role is tied to SIP role | UAC = TLS client, UAS = TLS server, hardcoded in `sslsocket.cpp` | PJSUA2 API allows arbitrary role combinations |
| Global SRTP contexts | Single key set for all calls | Per-call SRTP contexts |
| No DTLS-SRTP | SDES only | PJSIP supports both SDES and DTLS-SRTP |

## Building

```bash
docker build -t pjsua-test .
```

The image is based on Alpine 3.20 with prebuilt `pjproject` 2.14.1 and `py3-yaml` packages — build takes ~5 seconds.

## Operating Modes

All 4 SIP/TLS role combinations:

| Mode | SIP role | TLS role | Description |
|---|---|---|---|
| `--mode=uac` | UAC (caller) | Client | Standard: initiates call and TLS connection |
| `--mode=uas` | UAS (callee) | Server | Standard: listens on port, accepts calls |
| `--mode=uas-tls-client` | UAS (callee) | Client | Initiates TLS connection, waits for incoming INVITE |
| `--mode=uac-tls-server` | UAC (caller) | Server | Listens on TLS port, sends INVITE after client connects |

The `uas-tls-client` and `uac-tls-server` modes are impossible in SIPp — this is the key advantage.

All 4 modes are implemented using the PJSUA2 Python API (`scripts/*.py`).
The `uac` and `uas` modes support all 3 transports (TLS, TCP, UDP) via the `--transport` parameter.
TLS-role modes (`uas-tls-client`, `uac-tls-server`) always use TLS regardless of `--transport`.

## Configuration File

Parameters can be passed via a YAML file: `--config=/home/config.yml`.
CLI arguments take precedence over the config; header lists (`headers:`) are merged.

**YAML config format (all supported keys):**

```yaml
mode: uac             # uac | uas | uas-tls-client | uac-tls-server
transport: tls        # tls | tcp | udp (for uac/uas; TLS-role modes ignore this)
proxy: 127.0.0.1:5061
port: 5061
ip: 0.0.0.0
rtp_port: 16000
dest_uri: ""          # full SIP URI (instead of proxy)
duration: 10
tolerance: 90
wait_timeout: 30
tls_wait: 10
srtp: mandatory       # off | optional | mandatory
srtp_secure: 0        # 0 | 1 | 2
log_level: 3
bye: uac              # uac | uas | none (which side sends BYE)
wait_bye: 30          # timeout for waiting BYE from remote side
reinvite_by: uac      # uac | uas (which side sends re-INVITE)
reinvite_delay: "3"   # delay(s) in seconds, comma-separated: "3" or "3,7,12"
options_ping: 5       # in-dialog OPTIONS send interval, sec
options_auto_reply: true   # only reply to OPTIONS (don't send)
options_tolerance: 90      # min % of successful OPTIONS responses

tls:
  ca_file: /home/certs/ca.pem
  cert_file: /home/certs/cert.pem
  privkey_file: /home/certs/key.pem
  verify_server: false
  verify_client: false

headers:
  set:
    - "X-My-Header: value"
  expect:
    - "X-Response-Header"
  expect_not:
    - "X-Forbidden-Header"
  expect_name_regex:
    - "^X-Custom-.*"
  expect_not_regex:
    - "^X-Bad-.*"
  expect_value:
    - "X-Response-Header: expected-value"
    - "Via[0]: SIP/2.0/TLS"
  expect_value_regex:
    - "X-Session-Id: [0-9a-f]{8}"
  expect_count:
    - "Via: 2"    # exactly 2
    - "Via: 2+"   # at least 2
    - "Via: 1-3"  # 1 to 3
```

**Usage with sipssert:**

```yaml
args:
  - "--config=/home/config.yml"
  - "--duration=5"          # CLI overrides config
  - "--expect-header=X-Extra"  # header lists are merged
```

## Test Scenarios

### Scenario 1: Basic SIP over TLS

Verifies SIP signaling over TLS.

```
pjsua-test (UAC, TLS client)          SIPp (UAS, TLS server)
     |                                        |
     |-------- TLS ClientHello ------------->|
     |<------- TLS ServerHello --------------|
     |                                        |
     |-------- INVITE ---------------------->|
     |<------- 200 OK -----------------------|
     |-------- ACK ------------------------->|
     |         ... duration ...               |
     |-------- BYE ------------------------->|
```

**sipssert scenario.yml:**

```yaml
tasks:
  - name: sipp-uas
    type: sipp
    config_file: uas.xml
    daemon: true
    args:
      - "-t l1"

  - name: pjsua-uac
    image: pjsua-test
    require:
      - { started: sipp-uas }
    args:
      - "--mode=uac"
      - "--proxy=sipp-uas:5061"
      - "--tls"
      - "--tls-ca-file=/home/certs/ca.pem"
      - "--duration=5"
```

### Scenario 2: SIP over TLS + SRTP with Echo Validation

Verifies TLS signaling + SRTP media integrity. SIPp runs `rtp_echo`, pjsua-test sends
a known pattern and performs byte-by-byte comparison of the returned data.

```
pjsua-test (UAC)                     SIPp (UAS, rtp_echo)
     |                                        |
     |-------- INVITE (crypto in SDP) ------>|
     |<------- 200 OK (crypto in SDP) -------|
     |-------- ACK ------------------------->|
     |                                        |
     |== SRTP (known pattern) ==============>|
     |<= SRTP (echo, same encrypted bytes) ==|
     |                                        |
     | [decrypt + compare payload]            |
     |                                        |
     |-------- BYE ------------------------->|
     |                                        |
     | exit 0 (match >= tolerance)            |
     | exit 1 (match < tolerance)             |
```

**sipssert scenario.yml:**

```yaml
tasks:
  - name: sipp-uas-echo
    type: sipp
    config_file: uas_srtp_echo.xml
    daemon: true
    args:
      - "-t l1"
      - "-rtp_echo"

  - name: pjsua-uac-srtp
    image: pjsua-test
    require:
      - { started: sipp-uas-echo }
    args:
      - "--mode=uac"
      - "--proxy=sipp-uas-echo:5061"
      - "--tls"
      - "--tls-ca-file=/home/certs/ca.pem"
      - "--srtp=mandatory"
      - "--duration=5"
```

### Scenario 3: SIP UAS + TLS Client (Role Decoupling)

A scenario impossible in SIPp. pjsua-test initiates a TLS connection to the remote server
(TLS client) while waiting for an incoming INVITE (SIP UAS).

Typical use case: testing an SBC or proxy where the client side must connect via TLS
but still accept incoming calls.

```
pjsua-test (SIP UAS, TLS client)    SIPp/SBC (SIP UAC, TLS server)
     |                                        |
     |-------- TLS ClientHello ------------->|  pjsua-test initiates TLS
     |<------- TLS ServerHello --------------|
     |<------- TLS Handshake done -----------|
     |                                        |
     |<------- INVITE -----------------------|  SIPp sends call
     |-------- 200 OK ---------------------->|  pjsua-test answers
     |<------- ACK --------------------------|
     |                                        |
     |== SRTP (pattern) ===================>|
     |<= SRTP (echo) =======================|
     |                                        |
     | [echo validation]                      |
     |                                        |
     |-------- BYE (after duration) -------->|
```

**sipssert scenario.yml:**

```yaml
tasks:
  - name: sipp-uac-echo
    type: sipp
    config_file: uac_srtp_echo.xml
    daemon: true
    args:
      - "-t l1"
      - "-rtp_echo"

  - name: pjsua-uas-tls-client
    image: pjsua-test
    require:
      - { started: sipp-uac-echo }
    args:
      - "--mode=uas-tls-client"
      - "--proxy=sipp-uac-echo:5061"
      - "--port=15062"
      - "--rtp-port=17000"
      - "--tls-ca-file=/home/certs/ca.pem"
      - "--tls-cert-file=/home/certs/client.pem"
      - "--tls-privkey-file=/home/certs/client-key.pem"
      - "--srtp=mandatory"
      - "--duration=5"
      - "--tolerance=85"
```

### Scenario 4: SIP UAC + TLS Server (Reverse Role Decoupling)

Mirror of scenario 3. pjsua-test listens on a TLS port (TLS server), waits for an incoming
TLS connection from the remote side, then sends an INVITE (SIP UAC).

TLS connection detection: the remote side sends a probe INVITE, which uac-tls-server
uses as a readiness signal.

```
pjsua-test (SIP UAC, TLS server)    SIPp/Device (SIP UAS, TLS client)
     |                                        |
     |<------- TLS ClientHello --------------|  Remote side connects
     |-------- TLS ServerHello ------------->|
     |-------- TLS Handshake done ---------->|
     |                                        |
     |-------- INVITE ---------------------->|  pjsua-test sends call
     |<------- 200 OK -----------------------|
     |-------- ACK ------------------------->|
     |                                        |
     |== SRTP (pattern) ===================>|
     |<= SRTP (echo) =======================|
     |                                        |
     | [echo validation]                      |
     |                                        |
     |-------- BYE (after duration) -------->|
```

**sipssert scenario.yml:**

```yaml
tasks:
  - name: pjsua-uac-tls-server
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uac-tls-server"
      - "--proxy=sipp-uas:5060"
      - "--port=15061"
      - "--rtp-port=16000"
      - "--tls-cert-file=/home/certs/server.pem"
      - "--tls-privkey-file=/home/certs/server-key.pem"
      - "--srtp=mandatory"
      - "--duration=5"
      - "--tls-wait=10"
      - "--tolerance=85"

  - name: sipp-uas
    type: sipp
    config_file: uas_srtp_echo.xml
    require:
      - { started: pjsua-uac-tls-server }
    args:
      - "-t l1"
      - "-rtp_echo"
```

### Scenario 5: Two pjsua-test Instances with Role Decoupling (Integration)

A proven integration test — two pjsua-test containers communicate with each other
with full SIP/TLS role decoupling. The test is located in `tests/pjsua-tls-roles/`.

```
pjsua-test (SIP UAC, TLS server)    pjsua-test (SIP UAS, TLS client)
     |                                        |
     | [listening TLS:15061]                  |
     |<------- TLS connect ------------------|  UAS connects as TLS client
     |<------- probe INVITE (rejected) ------|  Readiness signal
     |                                        |
     |-------- INVITE ---------------------->|  UAC sends call
     |<------- 200 OK -----------------------|
     |-------- ACK ------------------------->|
     |                                        |
     |<======= SRTP media ==================>|
     |                                        |
     |-------- BYE (after 5s) -------------->|
```

**sipssert scenario.yml:**

```yaml
tasks:
  - name: uac-tls-server
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uac-tls-server"
      - "--proxy=127.0.0.1:15062"
      - "--port=15061"
      - "--rtp-port=16000"
      - "--tls-cert-file=/home/certs/cacert.pem"
      - "--tls-privkey-file=/home/certs/cakey.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=5"
      - "--tls-wait=15"
      - "--tolerance=0"

  - name: uas-tls-client
    image: pjsua-test
    require:
      - { started: uac-tls-server }
    args:
      - "--mode=uas-tls-client"
      - "--proxy=127.0.0.1:15061"
      - "--port=15062"
      - "--rtp-port=17000"
      - "--tls-ca-file=/home/certs/cacert.pem"
      - "--tls-cert-file=/home/certs/cacert.pem"
      - "--tls-privkey-file=/home/certs/cakey.pem"
      - "--srtp=mandatory"
      - "--srtp-secure=0"
      - "--duration=10"
      - "--tolerance=0"
      - "--wait-timeout=20"
```

Running:

```bash
pip install git+https://github.com/OpenSIPS/SIPssert.git
sipssert tests/
```

### Scenario 6: SBC/Proxy Testing (End-to-End)

Full test: two pjsua-test instances on both ends with an SBC/proxy in between.
Verifies that the SBC correctly proxies TLS signaling and SRTP media.

```
pjsua-test (UAC)      SBC/Proxy        pjsua-test (UAS)
     |                    |                    |
     |--- INVITE -------->|--- INVITE -------->|
     |<-- 200 OK ---------|<-- 200 OK ---------|
     |--- ACK ----------->|--- ACK ----------->|
     |                    |                    |
     |== SRTP ==========>|== SRTP ==========>|
     |<= SRTP (echo) ====|<= SRTP (echo) ====|
     |                    |                    |
     | [validation]       |                    |
```

**sipssert scenario.yml:**

```yaml
tasks:
  - name: pjsua-uas
    image: pjsua-test
    daemon: true
    args:
      - "--mode=uas"
      - "--tls"
      - "--tls-cert-file=/home/certs/server.pem"
      - "--tls-privkey-file=/home/certs/server-key.pem"
      - "--srtp=mandatory"
      - "--duration=10"

  - name: proxy
    image: my-sbc
    require:
      - { started: pjsua-uas }

  - name: pjsua-uac
    image: pjsua-test
    require:
      - { started: proxy }
    args:
      - "--mode=uac"
      - "--proxy=proxy:5061"
      - "--tls"
      - "--tls-ca-file=/home/certs/ca.pem"
      - "--srtp=mandatory"
      - "--duration=5"
```

## RTP/SRTP Echo Validation

When used with `rtp_echo` on the remote side, pjsua-test performs byte-by-byte
media stream verification:

1. `EchoValidatorPort` generates frames with a deterministic pattern (4-byte counter)
2. PJSIP encrypts the frame (SRTP) and sends it over the network
3. The remote side (`rtp_echo`) returns the encrypted packet as-is
4. PJSIP decrypts the received packet with its own key
5. `EchoValidatorPort` compares the decrypted payload against a ring buffer of sent frames
6. Echo delay is accounted for: search among the last 64 sent frames

Result:

```
==================================================
RTP/SRTP Echo Validation Results:
  Frames sent:       250
  Frames received:   248
  Frames matched:    247
  Frames mismatched: 1
  Match rate:        99.6%
  Tolerance:         90%
  RESULT: PASS
==================================================
```

Exit code: `0` = PASS, `1` = FAIL. Compatible with sipssert (exit code determines test result).

## Parameters

Both formats are supported: `--key=value` and `--key value`.

### General

| Parameter | Description | Default |
|---|---|---|
| `--mode` | `uac`, `uas`, `uas-tls-client`, `uac-tls-server` | `uac` |
| `--config PATH` | YAML config file (CLI arguments take precedence) | - |
| `--transport` | `tls`, `tcp`, `udp` (for uac/uas; TLS-role modes use TLS) | `tls` |
| `--proxy HOST:PORT` | Remote side address | - |
| `--port PORT` | Local SIP/TLS port | 5060 / 5061 |
| `--ip ADDR` | Bind to a specific IP / interface | 0.0.0.0 |
| `--rtp-port PORT` | Local RTP port (for host networking) | auto |
| `--duration SEC` | Call duration | 10 |
| `--dest-uri URI` | Full SIP URI (instead of --proxy) | - |

### TLS

| Parameter | Description |
|---|---|
| `--tls` | Enable TLS transport (uac/uas modes) |
| `--tls-ca-file PATH` | CA certificate |
| `--tls-cert-file PATH` | Client/server certificate |
| `--tls-privkey-file PATH` | Private key |
| `--tls-verify-server` | Verify server certificate |
| `--tls-verify-client` | Verify client certificate |

### SRTP

| Parameter | Description | Default |
|---|---|---|
| `--srtp` | `off`, `optional`, `mandatory` | `off` |
| `--srtp-secure` | 0 = no requirements, 1 = TLS required, 2 = end-to-end | 0 |

### Echo Validation and Timeouts

| Parameter | Description | Default |
|---|---|---|
| `--tolerance` | Minimum match percentage for PASS | 90 |
| `--wait-timeout` | Timeout for incoming call, sec (uas-tls-client) | 30 |
| `--tls-wait` | Timeout for TLS connection, sec (uac-tls-server) | 10 |

### BYE Control

| Parameter | Description | Default |
|---|---|---|
| `--bye` | `uac`, `uas`, `none` — which side sends BYE | Depends on mode |
| `--wait-bye` | Timeout for waiting BYE from remote side, sec | 30 |

By default, BYE is sent by the side that owns the call (`uac` for uac/uac-tls-server, `uas` for uas/uas-tls-client). With `--bye=none`, nobody sends BYE — the call lives for `--duration` seconds, then the script exits with the echo validation result.

If `--bye` points to the other side, the script waits for BYE from the remote side for up to `--wait-bye` seconds after `--duration` expires. If BYE is not received — exit 1.

### re-INVITE

| Parameter | Description | Default |
|---|---|---|
| `--reinvite-by` | `uac` or `uas` — which side sends re-INVITE | - |
| `--reinvite-delay` | Delay(s) after media establishment, comma-separated (sec) | - |

Both parameters are required together. Example: `--reinvite-by=uac --reinvite-delay=3` sends a re-INVITE after 3 seconds. Multiple re-INVITEs: `--reinvite-delay=3,7,12`.

After a re-INVITE, `EchoValidatorPort` reconnects to the new media object — counters are not reset, validation continues.

Both sides can be initiators (each specifies `--reinvite-by` for its own role). Stagger timers to avoid glare (SIP 491).

### In-dialog OPTIONS Ping

| Parameter | Description | Default |
|---|---|---|
| `--options-ping` | OPTIONS send interval, sec (enables auto-reply) | - |
| `--options-auto-reply` | Only reply 200 OK to incoming OPTIONS | false |
| `--options-tolerance` | Minimum % of successful OPTIONS responses | 90 |

The initiating side (`--options-ping=N`) sends in-dialog OPTIONS every N seconds and checks the percentage of received 200 OK responses. The other side (`--options-auto-reply`) only replies.

OPTIONS validation result affects the exit code along with echo validation and header checks.

### Custom SIP Headers

| Parameter | Value format | Description |
|---|---|---|
| `--set-header` | `Name: Value` | Add header to outgoing requests (repeatable) |
| `--expect-header` | `Name` | Verify header presence in response (repeatable) |
| `--expect-no-header` | `Name` | Verify header absence (repeatable) |
| `--expect-header-regex` | `REGEX` | At least one header name matches regex (repeatable) |
| `--expect-no-header-regex` | `REGEX` | No header name matches regex (repeatable) |
| `--expect-header-value` | `Name[N]: Value` | Exact header value match (repeatable) |
| `--expect-header-value-regex` | `Name[N]: REGEX` | Header value matches regex (repeatable) |
| `--expect-header-count` | `Name: N\|N+\|N-M` | Header occurrence count (repeatable) |

**Indexing**: `Name[0]` — first, `Name[-1]` — last header with that name.

**Count ranges**: `2` — exactly 2, `2+` — at least 2, `1-3` — 1 to 3.

**Example:**

```bash
--set-header="X-Request-Id: abc123"
--expect-header-value="X-Session: abc123"
--expect-header-value-regex="Via[0]: SIP/2.0/TLS .*"
--expect-header-count="Via: 2+"
--expect-no-header-regex="^X-Debug-.*"
```

All header checks are applied to the INVITE response (200 OK). A failed check results in exit code 1.

### Miscellaneous

| Parameter | Description |
|---|---|
| `--log-level N` | PJSIP log level 0-6 (default 3) |
| `PJSUA_EXTRA_ARGS` | Additional arguments via environment variable (legacy) |

## Project Structure

```
.
├── Dockerfile              # Alpine 3.20 + pjproject/pjsua/py3-pjsua/py3-yaml packages
├── entrypoint.sh           # CLI wrapper: parses arguments, launches the appropriate script
├── scripts/
│   ├── common.py           # Shared module: EchoValidatorPort, OptionsPingManager,
│   │                       # HeaderManager, ConfigLoader, BYE/re-INVITE/OPTIONS helpers
│   ├── uac.py              # SIP UAC + TLS Client + echo validation (PJSUA2)
│   ├── uas.py              # SIP UAS + TLS Server + echo validation (PJSUA2)
│   ├── uas_tls_client.py   # SIP UAS + TLS Client + echo validation (PJSUA2)
│   └── uac_tls_server.py   # SIP UAC + TLS Server + echo validation (PJSUA2)
├── tests/
│   ├── config.yml              # sipssert test set config
│   ├── pjsua-standard-roles/   # uac + uas (standard roles)
│   ├── pjsua-tls-roles/        # uac-tls-server + uas-tls-client
│   ├── pjsua-config-file/      # YAML config test (--config)
│   ├── pjsua-headers-set-check/    # --set-header + --expect-header-value
│   ├── pjsua-headers-expect-not/   # --expect-no-header
│   ├── pjsua-headers-regex/        # --expect-header-regex + --expect-header-count
│   ├── pjsua-headers-tls-roles/    # headers with TLS role decoupling
│   ├── pjsua-udp-transport/    # UDP transport (--transport=udp)
│   ├── pjsua-tcp-transport/    # TCP transport (--transport=tcp)
│   ├── pjsua-bye-from-uas/    # BYE from UAS side
│   ├── pjsua-bye-none/        # no BYE (both sides --bye=none)
│   ├── pjsua-reinvite-uac/    # re-INVITE from UAC
│   ├── pjsua-reinvite-uas/    # re-INVITE from UAS
│   └── pjsua-options-ping/    # in-dialog OPTIONS ping
└── README.md
```

## Known Issues

- **JACK/ALSA messages**: suppressed via `JACK_NO_START_SERVER=1` and null ALSA config.
  When using stale Docker cache, they may reappear — rebuild with `--no-cache`.
- **Segfault on exit**: PJSUA2 Python bindings sometimes crash during `libDestroy()`.
  Worked around via `os._exit()` — exit code is correct.
- **Probe INVITE** (`uac-tls-server` mode): uas-tls-client sends a probe INVITE
  to signal TLS connection readiness. uac-tls-server rejects it (486 Busy)
  and then sends the real INVITE.
