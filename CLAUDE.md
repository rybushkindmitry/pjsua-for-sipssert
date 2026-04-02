# pjsua-test

Docker-образ на базе PJSIP для тестирования SIP over TLS с валидацией RTP/SRTP.
Используется с [SIPssert](https://github.com/OpenSIPS/SIPssert) как замена SIPp.

## Контекст проекта

Проект создан для решения архитектурных ограничений SIPp v3.7:
- TLS-роль жёстко привязана к SIP-роли (UAC=TLS client, UAS=TLS server)
- Низкая производительность SRTP-валидации (кастомная JLSRTP вместо libsrtp)
- Глобальные SRTP-контексты (не per-call)

Подробности: [README.md](README.md)

## Структура

```
Dockerfile              — Alpine 3.20 + пакеты pjproject 2.14.1 + py3-yaml
entrypoint.sh           — CLI-обёртка (режимы uac, uas, uas-tls-client, uac-tls-server)
scripts/
  common.py             — общий модуль: EchoValidatorPort, HeaderManager, ConfigLoader,
                          add_common_args, configure_srtp/tls, safe_shutdown, safe_exit
  uac.py                — PJSUA2: SIP UAC + TLS client + echo-валидация + заголовки
  uas.py                — PJSUA2: SIP UAS + TLS server + echo-валидация + заголовки
  uas_tls_client.py     — PJSUA2: SIP UAS + TLS client + echo-валидация + заголовки
  uac_tls_server.py     — PJSUA2: SIP UAC + TLS server + echo-валидация + заголовки
tests/                  — sipssert интеграционные тесты (7 сценариев)
  pjsua-standard-roles/ — стандартные роли uac + uas
  pjsua-tls-roles/      — развязка TLS-ролей: uac-tls-server + uas-tls-client
  pjsua-config-file/    — YAML-конфиг (--config)
  pjsua-headers-set-check/    — set-header + expect-header-value
  pjsua-headers-expect-not/   — expect-no-header
  pjsua-headers-regex/        — expect-header-regex + expect-header-count
  pjsua-headers-tls-roles/    — заголовки с развязкой TLS-ролей
```

## Ключевые решения

- **Все 4 режима на PJSUA2** — uac, uas, uas-tls-client, uac-tls-server реализованы Python-скриптами через PJSUA2 API (не pjsua CLI)
- **scripts/common.py** — единый shared-модуль для всех скриптов; содержит EchoValidatorPort, HeaderManager, ConfigLoader, add_common_args и вспомогательные функции
- **ConfigLoader** — загрузка YAML-конфига (`--config=FILE`); CLI-аргументы имеют приоритет; списки заголовков (`headers:`) объединяются (merge)
- **HeaderManager** — управляет 8 типами проверок SIP-заголовков (set, expect, expect_not, expect_name_regex, expect_not_regex, expect_value, expect_value_regex, expect_count); поддерживает индексирование (Name[0], Name[-1]) и диапазоны count (N, N+, N-M)
- **EchoValidatorPort** — кастомный `AudioMediaPort` для побайтового сравнения RTP/SRTP фреймов
- **Probe INVITE** — uas-tls-client отправляет probe для сигнализации о TLS-готовности uac-tls-server
- **transportId** — аккаунты привязаны к конкретным TLS-транспортам для корректной маршрутизации
- **os._exit()** — обход segfault в PJSUA2 Python bindings при libDestroy()
- Exit code `0`/`1` для совместимости с sipssert

## Интеграция с sipssert

- Образ: `pjsua-test`
- sipssert монтирует директорию сценария в `/home` (read-only)
- Аргументы: `--key=value` формат (YAML list в scenario.yml)
- YAML-конфиг: `--config=/home/config.yml`; конфиг-файл удобно класть рядом со scenario.yml
- Аргументы заголовков: `--set-header`, `--expect-header`, `--expect-header-value` и др. (8 типов); дополняют списки из `--config`
- При host-сети: использовать `--rtp-port` для разных RTP-портов на каждом контейнере
- Результат теста определяется exit code контейнера
- `daemon: true` для серверной стороны, `require: [{started: ...}]` для клиента
