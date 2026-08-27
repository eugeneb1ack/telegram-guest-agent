# Установка (русский)

**Telegram Guest Agent** — отдельный sidecar для Telegram Guest Mode. Он работает и с Hermes Runs, и с обычным OpenAI-совместимым API Chat Completions.

## 1. Что потребуется

- Бот, созданный через `@BotFather`, с включённым **Guest Mode** в Mini App BotFather.
- Числовой Telegram ID пользователя, который будет единственным владельцем бота.
- Docker Engine с Docker Compose v2 (рекомендуется) либо Python 3.12 для прямого запуска.
- URL и bearer-ключ вашего harness/агента.

Создайте отдельного Telegram-бота. Тот же токен не должен одновременно опрашивать другой long-polling процесс.

## 2. Настройка

```bash
git clone https://github.com/eugeneb1ack/telegram-guest-agent.git
cd telegram-guest-agent
./init-env.sh
```

Откройте `.env` и заполните обязательные значения:

```dotenv
GUEST_BOT_TOKEN=123456:вставьте-свой-токен
GUEST_OWNER_ID=123456789
GUEST_BOT_USERNAME=username_вашего_guest_бота

HERMES_API_URL=http://host.docker.internal:8643/v1/chat/completions
HERMES_API_KEY=вставьте-ключ-harness
HERMES_MODEL=название-модели
HERMES_USE_RUNS=1
```

`GUEST_OWNER_ID` обязателен, должен быть положительным целым числом и не имеет скрытого значения по умолчанию.

В Docker Desktop `host.docker.internal` указывает на сервис на хост-машине. В приложенном Compose это же имя добавлено для современного Docker на Linux. Если harness — другой сервис того же Compose-проекта, используйте имя сервиса и его порт.

## 3. Выбор режима harness

### Hermes Runs — предпочтительный режим

Укажите `HERMES_USE_RUNS=1`, когда API поддерживает:

```text
POST /v1/runs
GET  /v1/runs/{run_id}
```

Стартовый запрос получает `model`, `instructions`, `input` и стабильный `session_id`. Это правильный режим для долгих задач с инструментами и для контекста, который хранит сам harness.

Для Hermes направьте `HERMES_API_URL` и `HERMES_API_KEY` в API выделенного profile. Персона, инструменты и правила агента остаются в profile; этот проект передаёт только Telegram-контекст и безопасно доставляет ответ.

### Обычный OpenAI-compatible Chat Completions

Укажите `HERMES_USE_RUNS=0`, если harness поддерживает только:

```text
POST /v1/chat/completions
```

Endpoint должен принимать `model`, `messages`, `stream: false`, bearer-авторизацию и отвечать в форме:

```json
{"choices":[{"message":{"content":"текст ответа"}}]}
```

Для этого режима gateway сам хранит в оперативной памяти последние шесть пар «запрос/ответ» для активной reply-сессии. История живёт `GUEST_PENDING_ANCHOR_TTL` (по умолчанию 120 секунд), сбрасывается при рестарте sidecar и не записывается в `state.json`.

## 4. Проверка и запуск

Сначала проверьте подключение:

```bash
docker compose run --rm --no-deps telegram-guest-agent --check
```

Команда проверяет Telegram-бота и пытается выполнить необязательный health-check harness. Затем запустите polling:

```bash
./run-docker.sh
```

Либо напрямую:

```bash
docker compose up --build
```

Сервис настроен на автоматический restart. Логи:

```bash
docker compose logs -f telegram-guest-agent
```

## 5. Как работает контекст

1. Новый явный вызов через `@username_бота` или команду всегда создаёт новую сессию.
2. Ответ на недавнее guest-сообщение может продолжить ту же сессию в пределах TTL.
3. Новый вызов, даже в том же чате, получает другую сессию и не наследует старую историю.

Если Telegram Guest Mode отдаёт обычный reply отдельным update, gateway коротко запоминает этот reply как anchor. Следующий явный вызов в том же reply-контексте использует его и продолжает сессию.

## Медиа и приватность

Входящие Telegram-файлы скачиваются в `GUEST_MEDIA_CACHE_DIR` и ограничиваются `GUEST_MEDIA_MAX_BYTES`. В Docker они остаются в `/sandbox/inbound`.

Чтобы файл, сгенерированный harness, можно было прикрепить в публичный Telegram-ответ, явно задайте разрешённые каталоги:

```dotenv
GUEST_OWNER_MEDIA_ENABLED=1
GUEST_OWNER_MEDIA_ALLOWED_DIRS=/абсолютный/путь/к/разрешённым/файлам
GUEST_MEDIA_HOST_DIR=/абсолютный/путь/к/файлам/который/видит/harness
```

Бот сначала отправляет разрешённый файл в личный чат владельца, получает Telegram `file_id` и использует его в публичном rich-ответе там, где это поддерживает Bot API. Файлы вне allowlist отклоняются; `MEDIA:`, `file://`, системные POSIX-пути и Windows-пути удаляются из публичного текста.

`runtime/state.json` может содержать очередь и payload входящих сообщений. Не коммитьте, не архивируйте и не публикуйте `runtime/`, `.env`, логи и сгенерированные пользовательские файлы. Gateway записывает state атомарно и выставляет owner-only права там, где это поддерживает система.

## Запуск без Docker (только для разработки)

После создания защищённого `.env`:

```bash
python3 guest_gateway.py --check
python3 guest_gateway.py --poll
```

Для production рекомендуем Docker: он отделяет скачанные недоверенные медиа от процесса harness.

## Обновление

```bash
git pull --ff-only
docker compose up -d --build
docker compose logs -f telegram-guest-agent
```

Не удаляйте `runtime/`, пока сервис работает: там может находиться недоставленная очередь.
