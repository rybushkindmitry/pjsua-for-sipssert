# pjsua-test

Docker-образ на базе PJSIP/PJSUA для тестирования SIP over TLS с валидацией RTP/SRTP.
Предназначен для использования с [SIPssert](https://github.com/OpenSIPS/SIPssert) как замена SIPp
в сценариях, где SIPp имеет архитектурные ограничения.

## Зачем это нужно

SIPp (v3.7) имеет ряд проблем, которые не решаются без патчей исходного кода:

| Проблема | Причина в SIPp | Решение в pjsua-test |
|---|---|---|
| Низкая производительность при SRTP-валидации | Кастомная реализация JLSRTP: множественные аллокации `std::vector`, глобальные mutex, нет zero-copy | PJSIP использует libsrtp2 с in-place шифрованием |
| TLS-роль привязана к SIP-роли | UAC = TLS client, UAS = TLS server, зашито в `sslsocket.cpp` | PJSUA2 API позволяет произвольно комбинировать роли |
| SRTP-контексты глобальные | Один набор ключей на все вызовы | Per-call SRTP-контексты |
| Нет DTLS-SRTP | Только SDES | PJSIP поддерживает SDES и DTLS-SRTP |

## Сборка

```bash
docker build -t pjsua-test .
```

Образ основан на Alpine 3.20 с готовыми пакетами `pjproject` 2.14.1 и `py3-yaml` — сборка занимает ~5 секунд.

## Режимы работы

Все 4 комбинации SIP/TLS ролей:

| Режим | SIP-роль | TLS-роль | Описание |
|---|---|---|---|
| `--mode=uac` | UAC (звонит) | Client | Стандартный: инициирует звонок и TLS-соединение |
| `--mode=uas` | UAS (отвечает) | Server | Стандартный: слушает порт, принимает звонки |
| `--mode=uas-tls-client` | UAS (отвечает) | Client | Сам подключается по TLS, ждёт входящий INVITE |
| `--mode=uac-tls-server` | UAC (звонит) | Server | Слушает TLS-порт, после подключения клиента отправляет INVITE |

Режимы `uas-tls-client` и `uac-tls-server` невозможны в SIPp — это ключевое преимущество.

Все 4 режима реализованы на PJSUA2 Python API (скрипты `scripts/*.py`).

## Конфигурационный файл

Параметры можно передавать через YAML-файл: `--config=/home/config.yml`.
CLI-аргументы имеют приоритет над конфигом; списки заголовков (`headers:`) объединяются.

**Формат YAML-конфига (все поддерживаемые ключи):**

```yaml
mode: uac             # uac | uas | uas-tls-client | uac-tls-server
proxy: 127.0.0.1:5061
port: 5061
ip: 0.0.0.0
rtp_port: 16000
dest_uri: ""          # полный SIP URI (вместо proxy)
duration: 10
tolerance: 90
wait_timeout: 30
tls_wait: 10
srtp: mandatory       # off | optional | mandatory
srtp_secure: 0        # 0 | 1 | 2
log_level: 3

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
    - "Via: 2"    # ровно 2
    - "Via: 2+"   # не менее 2
    - "Via: 1-3"  # от 1 до 3
```

**Использование в sipssert:**

```yaml
args:
  - "--config=/home/config.yml"
  - "--duration=5"          # CLI переопределяет config
  - "--expect-header=X-Extra"  # списки заголовков дополняются
```

## Тестовые сценарии

### Сценарий 1: Базовый SIP over TLS

Проверка прохождения SIP-сигнализации через TLS.

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

### Сценарий 2: SIP over TLS + SRTP с echo-валидацией

Проверка: TLS-сигнализация + SRTP-медиа проходят корректно. На стороне SIPp работает
`rtp_echo`, pjsua-test отправляет паттерн и сравнивает вернувшиеся данные побайтово.

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
     | [расшифровка + сравнение payload]      |
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

### Сценарий 3: SIP UAS + TLS Client (развязка ролей)

Невозможный в SIPp сценарий. pjsua-test сам инициирует TLS-соединение
к удалённому серверу (TLS client), но при этом ждёт входящий INVITE (SIP UAS).

Типичный use case: тестирование SBC или proxy, где клиентская сторона должна
подключиться по TLS, но при этом принимать звонки.

```
pjsua-test (SIP UAS, TLS client)    SIPp/SBC (SIP UAC, TLS server)
     |                                        |
     |-------- TLS ClientHello ------------->|  pjsua-test инициирует TLS
     |<------- TLS ServerHello --------------|
     |<------- TLS Handshake done -----------|
     |                                        |
     |<------- INVITE -----------------------|  SIPp отправляет звонок
     |-------- 200 OK ---------------------->|  pjsua-test отвечает
     |<------- ACK --------------------------|
     |                                        |
     |== SRTP (pattern) ===================>|
     |<= SRTP (echo) =======================|
     |                                        |
     | [валидация echo]                       |
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

### Сценарий 4: SIP UAC + TLS Server (развязка ролей, обратная)

Зеркальный к сценарию 3. pjsua-test слушает TLS-порт (TLS server), ждёт
входящее TLS-подключение от удалённой стороны, после чего отправляет INVITE (SIP UAC).

Детекция TLS-подключения: удалённая сторона отправляет probe INVITE, который
uac-tls-server использует как сигнал готовности соединения.

```
pjsua-test (SIP UAC, TLS server)    SIPp/Device (SIP UAS, TLS client)
     |                                        |
     |<------- TLS ClientHello --------------|  Удалённая сторона подключается
     |-------- TLS ServerHello ------------->|
     |-------- TLS Handshake done ---------->|
     |                                        |
     |-------- INVITE ---------------------->|  pjsua-test отправляет звонок
     |<------- 200 OK -----------------------|
     |-------- ACK ------------------------->|
     |                                        |
     |== SRTP (pattern) ===================>|
     |<= SRTP (echo) =======================|
     |                                        |
     | [валидация echo]                       |
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

### Сценарий 5: Два pjsua-test с развязкой ролей (интеграционный)

Проверенный интеграционный тест — два pjsua-test контейнера общаются друг с другом
с полной развязкой SIP/TLS ролей. Тест находится в `tests/pjsua-tls-roles/`.

```
pjsua-test (SIP UAC, TLS server)    pjsua-test (SIP UAS, TLS client)
     |                                        |
     | [слушает TLS:15061]                    |
     |<------- TLS connect ------------------|  UAS подключается как TLS client
     |<------- probe INVITE (отклоняется) ----|  Сигнал готовности
     |                                        |
     |-------- INVITE ---------------------->|  UAC отправляет звонок
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

Запуск:

```bash
pip install git+https://github.com/OpenSIPS/SIPssert.git
sipssert tests/
```

### Сценарий 6: Тестирование SBC/Proxy (сквозной)

Полный тест: два pjsua-test на обоих концах, SBC/proxy посередине.
Проверяется, что SBC корректно проксирует TLS-сигнализацию и SRTP-медиа.

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
     | [валидация]        |                    |
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

## Echo-валидация RTP/SRTP

При использовании с `rtp_echo` на удалённой стороне, pjsua-test выполняет побайтовую
проверку медиапотока:

1. `EchoValidatorPort` генерирует фреймы с детерминистичным паттерном (4-байтовый счётчик)
2. PJSIP шифрует фрейм (SRTP) и отправляет по сети
3. Удалённая сторона (`rtp_echo`) возвращает зашифрованный пакет as-is
4. PJSIP расшифровывает полученный пакет своим ключом
5. `EchoValidatorPort` сравнивает расшифрованный payload с ring buffer отправленных фреймов
6. Учитывается задержка echo: поиск среди последних 64 отправленных фреймов

Результат:

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

Exit code: `0` = PASS, `1` = FAIL. Совместимо с sipssert (exit code определяет результат теста).

## Параметры

Поддерживаются оба формата: `--key=value` и `--key value`.

### Основные

| Параметр | Описание | По умолчанию |
|---|---|---|
| `--mode` | `uac`, `uas`, `uas-tls-client`, `uac-tls-server` | `uac` |
| `--config PATH` | YAML-файл с параметрами (CLI-аргументы имеют приоритет) | - |
| `--proxy HOST:PORT` | Адрес удалённой стороны | - |
| `--port PORT` | Локальный SIP/TLS-порт | 5060 / 5061 |
| `--ip ADDR` | Привязать к конкретному IP / интерфейсу | 0.0.0.0 |
| `--rtp-port PORT` | Локальный RTP-порт (для host-сети) | auto |
| `--duration SEC` | Длительность звонка | 10 |
| `--dest-uri URI` | Полный SIP URI (вместо --proxy) | - |

### TLS

| Параметр | Описание |
|---|---|
| `--tls` | Включить TLS-транспорт (режимы uac/uas) |
| `--tls-ca-file PATH` | CA-сертификат |
| `--tls-cert-file PATH` | Сертификат клиента/сервера |
| `--tls-privkey-file PATH` | Приватный ключ |
| `--tls-verify-server` | Проверять сертификат сервера |
| `--tls-verify-client` | Проверять сертификат клиента |

### SRTP

| Параметр | Описание | По умолчанию |
|---|---|---|
| `--srtp` | `off`, `optional`, `mandatory` | `off` |
| `--srtp-secure` | 0 = нет требований, 1 = требуется TLS, 2 = end-to-end | 0 |

### Echo-валидация и таймауты

| Параметр | Описание | По умолчанию |
|---|---|---|
| `--tolerance` | Минимальный % совпадений для PASS | 90 |
| `--wait-timeout` | Таймаут ожидания входящего звонка, сек (uas-tls-client) | 30 |
| `--tls-wait` | Таймаут ожидания TLS-подключения, сек (uac-tls-server) | 10 |

### Кастомные SIP-хедеры

| Параметр | Формат значения | Описание |
|---|---|---|
| `--set-header` | `Name: Value` | Добавить заголовок в исходящие запросы (повторяемый) |
| `--expect-header` | `Name` | Проверить наличие заголовка в ответе (повторяемый) |
| `--expect-no-header` | `Name` | Проверить отсутствие заголовка (повторяемый) |
| `--expect-header-regex` | `REGEX` | Хотя бы одно имя заголовка соответствует regex (повторяемый) |
| `--expect-no-header-regex` | `REGEX` | Ни одно имя заголовка не соответствует regex (повторяемый) |
| `--expect-header-value` | `Name[N]: Value` | Точное совпадение значения заголовка (повторяемый) |
| `--expect-header-value-regex` | `Name[N]: REGEX` | Значение заголовка соответствует regex (повторяемый) |
| `--expect-header-count` | `Name: N\|N+\|N-M` | Количество вхождений заголовка (повторяемый) |

**Индексирование**: `Name[0]` — первый, `Name[-1]` — последний заголовок с таким именем.

**Диапазоны для count**: `2` — ровно 2, `2+` — не менее 2, `1-3` — от 1 до 3.

**Пример:**

```bash
--set-header="X-Request-Id: abc123"
--expect-header-value="X-Session: abc123"
--expect-header-value-regex="Via[0]: SIP/2.0/TLS .*"
--expect-header-count="Via: 2+"
--expect-no-header-regex="^X-Debug-.*"
```

Все проверки заголовков применяются к ответу на INVITE (200 OK). Провальная проверка
даёт exit code 1.

### Прочее

| Параметр | Описание |
|---|---|
| `--log-level N` | Уровень логирования PJSIP 0-6 (по умолчанию 3) |
| `PJSUA_EXTRA_ARGS` | Дополнительные аргументы через переменную окружения (legacy) |

## Структура проекта

```
.
├── Dockerfile              # Alpine 3.20 + pjproject/pjsua/py3-pjsua/py3-yaml пакеты
├── entrypoint.sh           # CLI-обёртка: разбирает аргументы, запускает нужный скрипт
├── scripts/
│   ├── common.py           # Общий модуль: EchoValidatorPort, HeaderManager, ConfigLoader,
│   │                       # add_common_args, configure_srtp/tls, safe_shutdown
│   ├── uac.py              # SIP UAC + TLS Client + echo-валидация (PJSUA2)
│   ├── uas.py              # SIP UAS + TLS Server + echo-валидация (PJSUA2)
│   ├── uas_tls_client.py   # SIP UAS + TLS Client + echo-валидация (PJSUA2)
│   └── uac_tls_server.py   # SIP UAC + TLS Server + echo-валидация (PJSUA2)
├── tests/
│   ├── config.yml              # sipssert test set config
│   ├── pjsua-standard-roles/   # uac + uas (стандартные роли)
│   ├── pjsua-tls-roles/        # uac-tls-server + uas-tls-client
│   ├── pjsua-config-file/      # тест YAML-конфига (--config)
│   ├── pjsua-headers-set-check/    # --set-header + --expect-header-value
│   ├── pjsua-headers-expect-not/   # --expect-no-header
│   ├── pjsua-headers-regex/        # --expect-header-regex + --expect-header-count
│   └── pjsua-headers-tls-roles/    # заголовки с развязкой TLS-ролей
└── README.md
```

## Известные особенности

- **JACK/ALSA сообщения**: подавлены через `JACK_NO_START_SERVER=1` и null ALSA-конфиг.
  При использовании старого кэша Docker могут проявиться — пересоберите с `--no-cache`.
- **Segfault при выходе**: PJSUA2 Python bindings иногда крашатся при `libDestroy()`.
  Обходится через `os._exit()` — exit code корректен.
- **Probe INVITE** (режим `uac-tls-server`): uas-tls-client отправляет probe INVITE
  для сигнализации о готовности TLS-соединения. uac-tls-server отклоняет его (486 Busy)
  и затем отправляет настоящий INVITE.
