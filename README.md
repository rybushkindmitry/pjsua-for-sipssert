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

## Режимы работы

| Режим | SIP-роль | TLS-роль | Описание |
|---|---|---|---|
| `--mode=uac` | UAC (звонит) | Client | Стандартный: инициирует звонок и TLS-соединение |
| `--mode=uas` | UAS (отвечает) | Server | Стандартный: слушает порт, принимает звонки |
| `--mode=uas-tls-client` | UAS (отвечает) | Client | Нестандартный: сам подключается по TLS, но ждёт входящий INVITE |
| `--mode=uac-tls-server` | UAC (звонит) | Server | Нестандартный: слушает TLS-порт, после подключения клиента отправляет INVITE |

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
- name: sipp-uas
  type: sipp
  config_file: uas.xml
  args:
    - "-t l1"

- name: pjsua-uac
  image: pjsua-test
  require: sipp-uas
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
     | exit 0 (match >= 90%)                  |
     | exit 1 (match < 90%)                   |
```

**sipssert scenario.yml:**

```yaml
- name: sipp-uas-echo
  type: sipp
  config_file: uas_srtp_echo.xml
  args:
    - "-t l1"
    - "-rtp_echo"

- name: pjsua-uac-srtp
  image: pjsua-test
  require: sipp-uas-echo
  args:
    - "--mode=uac"
    - "--proxy=sipp-uas-echo:5061"
    - "--tls"
    - "--tls-ca-file=/home/certs/ca.pem"
    - "--srtp=mandatory"
    - "--duration=5"
```

### Сценарий 3: SIP UAS + TLS Client (развязка ролей)

Ключевой сценарий, невозможный в SIPp. pjsua-test сам инициирует TLS-соединение
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
- name: sipp-uac-echo
  type: sipp
  config_file: uac_srtp_echo.xml
  args:
    - "-t l1"
    - "-rtp_echo"

- name: pjsua-uas-tls-client
  image: pjsua-test
  args:
    - "--mode=uas-tls-client"
    - "--proxy=sipp-uac-echo:5061"
    - "--tls-ca-file=/home/certs/ca.pem"
    - "--tls-cert-file=/home/certs/client.pem"
    - "--tls-privkey-file=/home/certs/client-key.pem"
    - "--srtp=mandatory"
    - "--duration=5"
    - "--tolerance=85"

- name: sipp-uac-echo
  require: pjsua-uas-tls-client
```

### Сценарий 4: SIP UAC + TLS Server (развязка ролей, обратная)

Зеркальный к сценарию 3. pjsua-test слушает TLS-порт (TLS server), ждёт
входящее TLS-подключение от удалённой стороны, после чего отправляет INVITE (SIP UAC).

Типичный use case: тестирование устройства, которое само подключается по TLS,
а мы должны инициировать звонок после установки соединения.

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
- name: pjsua-uac-tls-server
  image: pjsua-test
  args:
    - "--mode=uac-tls-server"
    - "--proxy=sipp-uas:5060"
    - "--port=5061"
    - "--tls-cert-file=/home/certs/server.pem"
    - "--tls-privkey-file=/home/certs/server-key.pem"
    - "--srtp=mandatory"
    - "--duration=5"
    - "--tolerance=85"

- name: sipp-uas
  type: sipp
  config_file: uas_srtp_echo.xml
  require: pjsua-uac-tls-server
  args:
    - "-t l1"
    - "-rtp_echo"
```

### Сценарий 5: Тестирование SBC/Proxy (сквозной)

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
- name: pjsua-uas
  image: pjsua-test
  args:
    - "--mode=uas"
    - "--tls"
    - "--tls-cert-file=/home/certs/server.pem"
    - "--tls-privkey-file=/home/certs/server-key.pem"
    - "--srtp=mandatory"
    - "--duration=10"

- name: proxy
  image: my-sbc
  require: pjsua-uas

- name: pjsua-uac
  image: pjsua-test
  require: proxy
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
| `--proxy HOST:PORT` | Адрес удалённой стороны | - |
| `--port PORT` | Локальный SIP-порт | 5060 / 5061 |
| `--ip ADDR` | Привязать к конкретному IP / интерфейсу | 0.0.0.0 |
| `--duration SEC` | Длительность звонка | 10 |
| `--dest-uri URI` | Полный SIP URI (вместо --proxy) | - |

### TLS

| Параметр | Описание |
|---|---|
| `--tls` | Включить TLS-транспорт |
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

### Echo-валидация (режимы uas-tls-client, uac-tls-server)

| Параметр | Описание | По умолчанию |
|---|---|---|
| `--tolerance` | Минимальный % совпадений для PASS | 90 |
| `--wait-timeout` | Таймаут ожидания звонка (сек) | 30 |

### Прочее

| Параметр | Описание |
|---|---|
| `--pjsua2-script PATH` | Запустить произвольный Python-скрипт вместо pjsua CLI |
| `--extra "ARGS"` | Дополнительные аргументы pjsua |
| `PJSUA_EXTRA_ARGS` | То же через переменную окружения |

## Структура проекта

```
.
├── Dockerfile              # Alpine 3.20 + pjproject/pjsua/py3-pjsua пакеты
├── entrypoint.sh           # CLI-обёртка над pjsua / PJSUA2 скриптами
├── scripts/
│   ├── uas_tls_client.py   # SIP UAS + TLS Client + echo-валидация (PJSUA2)
│   └── uac_tls_server.py   # SIP UAC + TLS Server + echo-валидация (PJSUA2)
└── README.md
```
