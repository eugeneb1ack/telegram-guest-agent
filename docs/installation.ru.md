# Установка (русский)

**Telegram Guest Agent** — отдельный sidecar для Telegram Guest Mode. Он работает и с Hermes Runs, и с обычным OpenAI-совместимым API Chat Completions.

## 1. Что потребуется

- Бот, созданный через `@BotFather`, с включённым **Guest Mode** в Mini App BotFather.
- Числовой Telegram ID пользователя, который будет единственным владельцем бота.
- Git и Bash для готовых installation-скриптов.
- Docker Engine с Docker Compose v2 и плагином Docker Buildx (рекомендуемый production-путь) либо Python 3.12 для прямого запуска.
- Для автоматической настройки Hermes: рабочий CLI `hermes`, `curl` и `openssl`.
- Для другого harness: URL endpoint, название модели и bearer-ключ.

Создайте отдельного Telegram-бота. Тот же токен не должен одновременно опрашивать другой long-polling процесс.

До клонирования проверьте зависимости хоста:

```bash
git --version
docker version
docker compose version
docker buildx version
hermes --version
curl --version
openssl version
```

Для Docker-варианта на хост не устанавливаются Python-пакеты: runtime использует стандартную библиотеку и собирается в приложенный образ.

## 2. Клонирование и локальный `.env`

```bash
git clone https://github.com/eugeneb1ack/telegram-guest-agent.git
cd telegram-guest-agent
./init-env.sh
```

Файл `.env` доступен только владельцу и игнорируется Git. Никогда не
публикуйте его содержимое в issue, коммите, записи терминала или сообщении
поддержке.

## 3. Отдельный Hermes-профиль для Guest Agent

Репозиторий умеет сам создать и подключить профиль, поэтому API и toolsets не
нужно собирать вручную.

Чтобы сохранить ту же персону, авторизацию модели, установленные кастомные
skills, плагины и правила, что у основного Hermes-профиля, клонируйте его
локально:

```bash
./setup-hermes-profile.sh \
  --profile telegram-guest-agent \
  --clone-from default \
  --port 8644
```

Клон остаётся внутри локального Hermes home. В репозиторий и Git из него
ничего не копируется. Скрипт заменяет API-ключ клона отдельным ключом нового
профиля.

Для чистого профиля только с bundled skills и стандартной персоной Hermes:

```bash
./setup-hermes-profile.sh --profile telegram-guest-agent --port 8644
```

Если чистый профиль ещё не авторизован у провайдера модели, завершите
авторизацию именно в нём. Модель можно задать явно:

```bash
./setup-hermes-profile.sh \
  --profile telegram-guest-agent \
  --model название-модели \
  --port 8644
```

Bootstrap делает только ограниченный и проверяемый набор изменений:

- создаёт отдельный профиль и отказывается менять существующий без явного
  `--reuse`;
- включает защищённый API Runs/Chat Completions на выбранном порту;
- даёт платформе `api_server` полный bundle `hermes-cli`, поэтому skills,
  браузер, terminal, web, файлы, memory, плагины и доступные MCP-инструменты
  разрешаются самим Hermes обычным способом;
- генерирует сильный API-ключ, не печатает его и записывает совпадающие
  параметры подключения только в игнорируемый `.env`;
- открывает Telegram-контейнеру только `<profile>/cache/images` через
  read-only mount: созданное изображение можно сначала отправить владельцу в
  личку, а затем встроить его Telegram `file_id` в rich article гостевого
  ответа;
- устанавливает и запускает gateway только этого Hermes-профиля.

Чтобы намеренно перенастроить существующий профиль:

```bash
./setup-hermes-profile.sh --profile telegram-guest-agent --reuse
```

Добавляйте `--rotate-key` только при намеренной замене API-ключа. Используйте
`--no-start`, если Hermes устанавливает и запускает другой supervisor.

### CloakBrowser, VPN и fake-IP DNS

Существующий Chrome/CloakBrowser CDP подключается явно:

```bash
./setup-hermes-profile.sh \
  --profile telegram-guest-agent \
  --clone-from default \
  --cdp-url http://127.0.0.1:9242 \
  --allow-private-urls
```

Флаг `--allow-private-urls` нужен только в доверенной VPN/proxy-среде, где
публичные домены резолвятся в приватные или benchmark-адреса, например
`198.18.0.0/15`. Без него Hermes безопасно закрывает доступ и может принять
такой публичный домен за внутренний. Настройка относится только к отдельному
профилю; cloud metadata/link-local endpoints с учётными данными Hermes всё
равно блокирует безусловно. При этом профиль получит доступ и к другим
приватным адресам — не включайте флаг у недоверенного или публично доступного
harness.

API слушает `0.0.0.0`, потому что Docker-sidecar подключается через
`host.docker.internal`. Ограничьте порт firewall'ом доверенными локальными
сетями и не публикуйте его в интернет, даже несмотря на bearer-авторизацию.

## 4. Настройка Telegram

После bootstrap откройте `.env` и заполните Telegram-поля. URL Hermes,
API-ключ, модель, Runs-режим и каталог сгенерированных файлов уже записаны:

```dotenv
GUEST_BOT_TOKEN=123456:вставьте-свой-токен
GUEST_OWNER_ID=123456789
GUEST_BOT_USERNAME=username_вашего_guest_бота

HERMES_API_URL=http://host.docker.internal:8643/v1/chat/completions
HERMES_API_KEY=вставьте-ключ-harness
HERMES_MODEL=название-модели
HERMES_USE_RUNS=1
HERMES_POLL_INTERVAL=1
GUEST_PROGRESS_ENABLED=1
GUEST_PROGRESS_MIN_INTERVAL=1.0
GUEST_PROGRESS_HEARTBEAT_INTERVAL=4.0
```

`GUEST_OWNER_ID` обязателен, должен быть положительным целым числом и не имеет скрытого значения по умолчанию.

В Docker Desktop `host.docker.internal` указывает на сервис на хост-машине. В приложенном Compose это же имя добавлено для современного Docker на Linux. Если harness — другой сервис того же Compose-проекта, используйте имя сервиса и его порт.

## 5. Выбор режима harness

### Hermes Runs — предпочтительный режим

Укажите `HERMES_USE_RUNS=1`, когда API поддерживает:

```text
POST /v1/runs
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/events  # необязательный SSE-поток статусов
```

Стартовый запрос получает `model`, `instructions`, `input` и стабильный `session_id`. Для валидного reply на ответ гостевого агента он дополнительно получает ограниченный буфер `conversation_history`. Поэтому reply-контекст сохраняется и в тех реализациях Runs, где `session_id` используется как scope выполнения или памяти, но не загружает транскрипт сам. Это правильный режим для долгих задач с инструментами и для контекста, который хранит сам harness.

`HERMES_POLL_INTERVAL` определяет, как быстро завершённый Run будет доставлен в Telegram. Значение по умолчанию `1` секунда подходит для production; gateway не позволит опуститься ниже `0.5` секунды. Увеличивайте значение только если для harness важнее снизить число status-запросов, чем уменьшить задержку доставки.

Если доступен необязательный SSE-endpoint, gateway заменяет «Думаю…» на фиксированные публичные фазы реальной активности. Для поддерживаемых действий есть отдельные статусы начала, работы, завершения и ошибки. Долгая операция меняет безопасные рабочие фразы раз в `GUEST_PROGRESS_HEARTBEAT_INTERVAL` секунд (`4` по умолчанию, диапазон `2–30`). Поле preview команды используется только в памяти, чтобы отличить общую категорию: тесты, сборку, скрипт, репозиторий или поиск в файлах.

В Telegram никогда не передаются исходное имя tool, аргументы, event preview, команда, URL, имя файла, локальный путь, частичный ответ модели или reasoning. `GUEST_PROGRESS_MIN_INTERVAL` ограничивает частоту редактирований: по умолчанию не чаще одного раза в секунду, допустимый диапазон — `0.5–10` секунд. Укажите `GUEST_PROGRESS_ENABLED=0`, чтобы оставить статичный placeholder. При недоступном SSE опрос Run и доставка финального ответа продолжат работать.

Для Hermes используйте `setup-hermes-profile.sh`, чтобы endpoint, полный набор
инструментов, API-ключ и media bridge оставались согласованными. Персона,
skills, инструменты и правила остаются в профиле; этот проект передаёт только
Telegram-контекст и безопасно доставляет ответ.

### Обычный OpenAI-compatible Chat Completions

Укажите `HERMES_USE_RUNS=0`, если harness поддерживает только:

```text
POST /v1/chat/completions
```

Endpoint должен принимать `model`, `messages`, `stream: false`, bearer-авторизацию и отвечать в форме:

```json
{"choices":[{"message":{"content":"текст ответа"}}]}
```

В обоих режимах gateway сам хранит в оперативной памяти последние шесть пар «запрос/ответ» для активной reply-сессии. История живёт `GUEST_PENDING_ANCHOR_TTL` (по умолчанию 120 секунд), сбрасывается при рестарте sidecar и не записывается в `state.json`.

У Chat Completions нет стандартного потока lifecycle-событий инструментов, поэтому в этом режиме gateway оставляет «Думаю…» и не выдумывает несуществующую активность.

Для другого harness пропустите `setup-hermes-profile.sh` и заполните
`HERMES_API_URL`, `HERMES_API_KEY`, `HERMES_MODEL` и `HERMES_USE_RUNS`
вручную. Если harness возвращает локально созданные файлы, задайте
`GUEST_HARNESS_MEDIA_DIR` как один отдельный output-каталог. Compose подключит
только его и только для чтения. В ответе harness должен вернуть
`MEDIA:/абсолютный/путь/к/файлу` либо Markdown-изображение с этим путём;
остальные локальные пути будут скрыты.

## 6. Проверка и запуск

Сначала проверьте подключение:

```bash
./run-docker.sh --check
```

Команда проверяет Telegram-бота и пытается выполнить необязательный health-check harness. Затем запустите polling:

```bash
./run-docker.sh
```

Либо напрямую:

```bash
docker compose up --build
```

`init-env.sh` записывает UID/GID текущего пользователя хоста в `.env`, а `run-docker.sh` подставляет те же значения для старых конфигураций. Поэтому непривилегированный контейнер пишет state в bind mount без ослабления прав доступа.

Сервис настроен на автоматический restart. Логи:

```bash
docker compose logs -f telegram-guest-agent
```

Перед polling дополнительно проверьте Hermes-профиль:

```bash
hermes profile show telegram-guest-agent
hermes -p telegram-guest-agent gateway status
hermes -p telegram-guest-agent config get platform_toolsets.api_server
```

Последняя команда должна содержать `hermes-cli`. После запуска сделайте
реальный end-to-end тест: попросите гостевого агента перечислить и загрузить
один установленный skill, затем открыть безопасную публичную страницу. Для
проверки media попросите создать небольшое изображение и убедитесь в двух
доставках: исходный файл пришёл владельцу в личку, а изображение появилось
media-блоком внутри rich article гостевого ответа.

## 7. Как работает контекст

1. Новый явный вызов через `@username_бота` или команду всегда создаёт новую сессию.
2. Ответ на недавнее guest-сообщение может продолжить ту же сессию в пределах TTL.
3. Новый вызов, даже в том же чате, получает другую сессию и не наследует старую историю.

Если Telegram Guest Mode отдаёт обычный reply отдельным update, gateway коротко запоминает этот reply как anchor. Следующий явный вызов в том же reply-контексте использует его и продолжает сессию.

## Медиа и приватность

Входящие Telegram-файлы скачиваются в `GUEST_MEDIA_CACHE_DIR` и ограничиваются `GUEST_MEDIA_MAX_BYTES`. В Docker они остаются в `/sandbox/inbound`.

Входящие и сгенерированные файлы используют разные мосты. Telegram-загрузки
остаются в `<репозиторий>/runtime/guest-media-cache`. Output-каталог harness
подключается только для чтения как `/sandbox/harness-output`. Hermes-bootstrap
указывает `cache/images` выделенного профиля; для другого harness путь можно
задать вручную:

```dotenv
GUEST_OWNER_MEDIA_ENABLED=1
GUEST_OWNER_MEDIA_ALLOWED_DIRS=/sandbox/inbound
GUEST_MEDIA_HOST_DIR=/абсолютный/путь/к/telegram-guest-agent/runtime/guest-media-cache
GUEST_HARNESS_MEDIA_DIR=/абсолютный/путь/к/output-каталогу-harness
```

Когда harness возвращает путь внутри одного из разрешённых host-root, gateway
сопоставляет его с нужным container mount, проверяет обычный файл и лимит
размера, сначала отправляет его владельцу в личку, получает Telegram `file_id`
и вставляет его в публичный rich-ответ. Остальные пути отклоняются и
замазываются. Generated-output mount не открывает `.env`, персону, skills,
memory или историю профиля. При прямом Python-запуске вручную задайте
`GUEST_HARNESS_MEDIA_HOST_DIR`, `GUEST_HARNESS_MEDIA_CACHE_DIR` и
`GUEST_OWNER_MEDIA_ALLOWED_DIRS`, потому что Compose обычно подставляет
container-side значения сам.

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
./run-docker.sh --check
docker compose up -d --build
docker compose logs -f telegram-guest-agent
```

Не удаляйте `runtime/`, пока сервис работает: там может находиться недоставленная очередь.
