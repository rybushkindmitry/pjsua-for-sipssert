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
Dockerfile              — multi-stage сборка PJSIP 2.14.1 + runtime
entrypoint.sh           — CLI-обёртка (режимы uac, uas, uas-tls-client)
scripts/
  uas_tls_client.py     — PJSUA2: SIP UAS + TLS client + echo-валидация
```

## Ключевые решения

- **entrypoint.sh** — простые режимы (uac, uas) через pjsua CLI
- **scripts/*.py** — сложные режимы (развязка TLS/SIP ролей) через PJSUA2 Python API
- **EchoValidatorPort** — кастомный `AudioMediaPort` для побайтового сравнения отправленных и полученных RTP/SRTP фреймов (ring buffer, tolerance)
- Предполагается `rtp_echo` на удалённой стороне (SIPp)
- Exit code `0`/`1` для совместимости с sipssert

## Интеграция с sipssert

- Образ: `pjsua-test`
- sipssert монтирует директорию сценария в `/home` (read-only)
- Сертификаты передаются через `--tls-*` параметры
- Результат теста определяется exit code контейнера
