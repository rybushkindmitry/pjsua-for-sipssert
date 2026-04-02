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
Dockerfile              — Alpine 3.20 + пакеты pjproject 2.14.1
entrypoint.sh           — CLI-обёртка (режимы uac, uas, uas-tls-client, uac-tls-server)
scripts/
  uas_tls_client.py     — PJSUA2: SIP UAS + TLS client + echo-валидация
  uac_tls_server.py     — PJSUA2: SIP UAC + TLS server + echo-валидация
tests/                  — sipssert интеграционные тесты
```

## Ключевые решения

- **entrypoint.sh** — простые режимы (uac, uas) через pjsua CLI
- **scripts/*.py** — сложные режимы (развязка TLS/SIP ролей) через PJSUA2 Python API
- **EchoValidatorPort** — кастомный `AudioMediaPort` для побайтового сравнения RTP/SRTP фреймов
- **Probe INVITE** — uas-tls-client отправляет probe для сигнализации о TLS-готовности uac-tls-server
- **transportId** — аккаунты привязаны к конкретным TLS-транспортам для корректной маршрутизации
- **os._exit()** — обход segfault в PJSUA2 Python bindings при libDestroy()
- Exit code `0`/`1` для совместимости с sipssert

## Интеграция с sipssert

- Образ: `pjsua-test`
- sipssert монтирует директорию сценария в `/home` (read-only)
- Аргументы: `--key=value` формат (YAML list в scenario.yml)
- При host-сети: использовать `--rtp-port` для разных RTP-портов на каждом контейнере
- Результат теста определяется exit code контейнера
- `daemon: true` для серверной стороны, `require: [{started: ...}]` для клиента
