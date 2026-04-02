# Конфигурационный файл и кастомные SIP-хедеры

## Цель

Добавить:
1. Универсальный YAML конфиг-файл (`--config`) для всех параметров pjsua-test
2. Установку кастомных SIP-хедеров в исходящие сообщения (INVITE / 200 OK)
3. Проверку хедеров во входящих сообщениях (наличие, отсутствие, точное значение, regex)
4. Перевод всех 4 режимов на PJSUA2 Python для единообразной функциональности

## Конфиг-файл

### Формат

YAML-файл, подключается через `--config=PATH`. Все параметры опциональны.

```yaml
mode: uac-tls-server
proxy: 10.0.0.1:5061
port: 5061
ip: 198.51.100.8
rtp_port: 16000
duration: 5
tolerance: 85
wait_timeout: 30
tls_wait: 10

srtp: mandatory
srtp_secure: 0

tls:
  cert_file: /home/certs/server.pem
  privkey_file: /home/certs/server-key.pem
  ca_file: /home/certs/ca.pem
  verify_server: false
  verify_client: false

headers:
  set:
    - "X-Session-Id: test-123"
    - "X-Custom: value"
  expect:
    - "X-Session-Id"
  expect_not:
    - "X-Removed"
  expect_value:
    - "X-Custom: value"
  expect_value_regex:
    - "X-Session-Id: ^test-.*"
  expect_name_regex:
    - "^X-Custom-.*"
  expect_not_regex:
    - "^X-Internal-.*"
```

### Приоритет

CLI-аргументы имеют приоритет над конфиг-файлом. Списковые параметры (headers) объединяются: конфиг + CLI.

### Путь

В sipssert директория сценария монтируется в `/home` (read-only). Конфиг лежит рядом со scenario.yml:

```yaml
tasks:
  - name: pjsua-uac
    image: pjsua-test
    args:
      - "--config=/home/pjsua-uac.yml"
      - "--set-header=X-Extra: override"
```

## Кастомные SIP-хедеры

### Установка (set)

- **UAC** добавляет хедеры в **INVITE**
- **UAS** добавляет хедеры в **200 OK**

Механизм: `CallOpParam.txOption.headers` (PJSUA2 API, `SipHeaderVector`).

CLI:
```
--set-header="X-Custom: value"    # повторяемый
```

### Проверка

- **UAC** проверяет хедеры в **200 OK** (ответ на INVITE)
- **UAS** проверяет хедеры во **входящем INVITE**

Типы проверок:

| CLI-флаг | Конфиг-ключ | Что проверяет |
|---|---|---|
| `--expect-header="Name"` | `headers.expect` | Хедер с таким именем присутствует |
| `--expect-header-regex="Pattern"` | `headers.expect_name_regex` | Есть хедер, имя которого матчит regex |
| `--expect-no-header="Name"` | `headers.expect_not` | Хедер с таким именем отсутствует |
| `--expect-no-header-regex="Pattern"` | `headers.expect_not_regex` | Нет хедеров, имя которых матчит regex |
| `--expect-header-value="Name: value"` | `headers.expect_value` | Точное совпадение значения |
| `--expect-header-value-regex="Name: pattern"` | `headers.expect_value_regex` | Regex-совпадение значения |

Примеры проверки по шаблону имени:
```
--expect-header-regex="^X-Custom-.*"          # есть хотя бы один хедер X-Custom-*
--expect-no-header-regex="^X-Internal-.*"     # нет ни одного хедера X-Internal-*
```

Все флаги повторяемые (`action="append"` в argparse).

### Отчёт

```
==================================================
Header Validation Results:
  [PASS] expect: X-Session-Id — found
  [PASS] expect_name_regex: ^X-Custom-.* — matched X-Custom-Foo
  [PASS] expect_not: X-Removed — not found
  [PASS] expect_not_regex: ^X-Internal-.* — no matching headers
  [FAIL] expect_value: X-Custom — expected "exact", got "other"
  [PASS] expect_value_regex: X-Session-Id — "test-123" matches ^test-.*
  RESULT: FAIL (1/6 checks failed)
==================================================
```

Если хотя бы одна проверка не прошла — exit code 1.

## Архитектура

### Все режимы на PJSUA2

Режимы `uac` и `uas` переписываются с pjsua CLI на PJSUA2 Python-скрипты:
- `scripts/uac.py` — стандартный UAC (TLS client, SIP UAC)
- `scripts/uas.py` — стандартный UAS (TLS server, SIP UAS)

Это обеспечивает единообразную поддержку конфига и хедеров во всех 4 режимах.

### Общий модуль `scripts/common.py`

Выносим дублирующийся код из всех скриптов:

- **`EchoValidatorPort`** — кастомный AudioMediaPort (сейчас дублирован в 2 файлах)
- **`HeaderManager`** — установка и проверка SIP-хедеров
- **`ConfigLoader`** — парсинг YAML-конфига + merge с CLI-аргументами
- **`parse_common_args()`** — общие argparse-аргументы
- **`safe_shutdown()`** — `hangupAllCalls()` + `os._exit()`

### entrypoint.sh

Упрощается — все 4 режима маршрутизируются на Python-скрипты:

```
--mode=uac           → python3 /scripts/uac.py
--mode=uas           → python3 /scripts/uas.py
--mode=uas-tls-client → python3 /scripts/uas_tls_client.py
--mode=uac-tls-server → python3 /scripts/uac_tls_server.py
```

Парсинг в entrypoint сводится к: определить mode, передать `--config` и все CLI-аргументы в скрипт.

### HeaderManager

```python
class HeaderManager:
    def __init__(self, config):
        self.set_headers = config.get("set", [])
        self.expect = config.get("expect", [])
        self.expect_not = config.get("expect_not", [])
        self.expect_value = config.get("expect_value", [])
        self.expect_regex = config.get("expect_regex", [])

    def build_sip_headers(self) -> pj.SipHeaderVector:
        """Build headers for outgoing INVITE / 200 OK."""

    def check_headers(self, msg: pj.SipRxData) -> list[CheckResult]:
        """Validate headers in incoming message."""

    def print_report(self, results) -> bool:
        """Print report, return True if all passed."""
```

## sipssert-тесты

### Тест 1: `pjsua-headers-set-check`

UAC устанавливает хедер → UAS проверяет наличие и значение.

```
UAC (set X-Test: hello)  →  INVITE с X-Test: hello  →  UAS (expect X-Test, expect_value X-Test: hello)
UAS (set X-Reply: world) →  200 OK с X-Reply: world →  UAC (expect X-Reply, expect_value X-Reply: world)
```

### Тест 2: `pjsua-headers-expect-not`

Проверка отсутствия хедера.

```
UAC (без X-Secret)  →  INVITE  →  UAS (expect_not X-Secret)
```

### Тест 3: `pjsua-headers-regex`

Проверка regex-паттерна.

```
UAC (set X-Id: session-42-abc)  →  INVITE  →  UAS (expect_regex X-Id: ^session-\d+-[a-z]+$)
```

### Тест 4: `pjsua-config-file`

Все параметры через конфиг-файл (без CLI-аргументов кроме `--config`).

### Тест 5: `pjsua-headers-tls-roles`

Хедеры + развязка ролей (uac-tls-server + uas-tls-client) с проверкой хедеров на обоих концах.

## Изменения в файлах

### Новые файлы
- `scripts/common.py` — общий модуль
- `scripts/uac.py` — стандартный UAC на PJSUA2
- `scripts/uas.py` — стандартный UAS на PJSUA2

### Модифицируемые файлы
- `scripts/uas_tls_client.py` — импорт из common, поддержка хедеров и конфига
- `scripts/uac_tls_server.py` — импорт из common, поддержка хедеров и конфига
- `entrypoint.sh` — упрощение, маршрутизация всех режимов на Python
- `Dockerfile` — добавить `py3-yaml` пакет

### Новые тесты
- `tests/pjsua-headers-set-check/`
- `tests/pjsua-headers-expect-not/`
- `tests/pjsua-headers-regex/`
- `tests/pjsua-config-file/`
- `tests/pjsua-headers-tls-roles/`
