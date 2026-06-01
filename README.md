# OpenClaw ↔ MAX Messenger Bridge

Подключи своего OpenClaw-ассистента к мессенджеру MAX.
Два варианта: **Python-мост** (рекомендуемый — проще и надёжнее) и **n8n workflow** (для фанатов визуального редактора).

---

## Что нужно

- OpenClaw Gateway (установлен и запущен)
- Бот в MAX (создан через business.max.ru)
- Python 3 + `requests`

---

# Часть 1: Создать бота в MAX

1. Зайти на [business.max.ru](https://business.max.ru/self)
2. Раздел «Чат-боты» → «Создать бота»
3. Заполнить имя, описание, аватар → отправить на модерацию
4. После модерации (статус «Создан») → «Интеграция» → «Получить токен»
5. **Скопировать токен** — он понадобится для моста

Бот доступен в MAX:
- Поиск по нику `@username_бота` в приложении
- Прямая ссылка: `https://max.ru/@username_бота`

---

# Часть 2: Включить HTTP API в OpenClaw

В файле `~/.openclaw/openclaw.json` добавить блок `gateway.http`:

```json5
{
  "gateway": {
    // ... остальной конфиг ...
    "http": {
      "endpoints": {
        "chatCompletions": { "enabled": true }
      }
    }
  }
}
```

Перезапустить Gateway:
```bash
openclaw gateway restart
```

Запомнить Gateway-токен из того же конфига — поле `gateway.auth.token`.

Проверить что API работает:
```bash
curl -s http://localhost:18789/v1/models \
  -H "Authorization: Bearer ВАШ_ТОКЕН"
```

---

# Часть 3: Python-мост (быстрый и надёжный)

## 3.1 Скопировать и настроить

```bash
mkdir -p /opt/max-bridge
cp max-bridge.py /opt/max-bridge/
```

Отредактировать токены в начале скрипта:

```python
MAX_TOKEN = "f9L…то…акса"
OC_TOKEN  = "c48…ga…claw"
```

### ⚡ Важно: выбор модели

По умолчанию бридж использует **DeepSeek v4 Flash** — быстро (5-15 сек). Если нужен более умный, но медленный ответ — замени в скрипте:

```python
# Быстро (рекомендуется для чат-бота):
headers["x-openclaw-model"] = "deepseek/deepseek-v4-flash"

# Умно, но медленно (~60 сек):
headers["x-openclaw-model"] = "deepseek/deepseek-v4-pro"
```

## 3.2 systemd-сервис (автозапуск и защита от падений)

Создать `/etc/systemd/system/max-bridge.service`:

```ini
[Unit]
Description=MAX ↔ OpenClaw Bridge
After=network.target

[Service]
Type=simple
User=aurum
WorkingDirectory=/opt/max-bridge
ExecStart=/usr/bin/python3 -u /opt/max-bridge/max-bridge.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now max-bridge
```

## 3.3 Управление

```bash
sudo systemctl status max-bridge      # состояние
sudo journalctl -u max-bridge -f      # логи в реальном времени
sudo systemctl restart max-bridge     # перезапуск
sudo systemctl stop max-bridge        # остановить
```

## 3.4 Что внутри (защита от падений)

- **Retry до 3 раз** — OpenClaw API и MAX API
- **Exponential backoff** — при ошибках сети ждёт дольше
- **systemd Restart=always** — упал → через 10 сек поднялся
- **Маркер-файл** — не теряет сообщения при перезапуске
- **Индикатор «печатает»** — бот показывает typing пока думает
- **Graceful degradation** — если OpenClaw упал, бот отвечает «попробуй позже»

---

# Часть 4: Вариант с n8n (альтернатива)

## 4.1 Установить n8n

```bash
npm install -g n8n
```

## 4.2 Первый запуск и настройка владельца

```bash
n8n start
```

Открыть `http://localhost:5678` → создать owner-аккаунт (email + пароль).

⚠️ **Без этого шага API-ключи не работают и workflow не активируются через REST.**

## 4.3 Создать API-ключ

В n8n UI: Settings → API Keys → Create API Key.

Или через базу (если UI недоступен):
```bash
sqlite3 ~/.n8n/database.sqlite "
  INSERT INTO user_api_keys (id, userId, label, apiKey, createdAt, updatedAt, type)
  SELECT '$(uuidgen)', id, 'bridge', 'n8n__' || lower(hex(randomblob(32))),
         datetime('now'), datetime('now'), 'public-api'
  FROM user WHERE role = 'global:owner';
"
```

⚠️ **После вставки в базу нужно перезапустить n8n!**

## 4.4 Импортировать workflow

```bash
n8n import:workflow \
  --input=max-bridge-workflow.json \
  --userId=ID_OWNER_ПОЛЬЗОВАТЕЛЯ
```

ID владельца:
```bash
sqlite3 ~/.n8n/database.sqlite "SELECT id, email FROM user;"
```

## 4.5 Заменить токены в workflow

В `max-bridge-workflow.json` найти по `"name": "Authorization"` в HTTP Request нодах и заменить:

- `f9LHod…yfL6` → свой MAX-токен (2 места)
- `c48cbd…f970` → свой Gateway-токен

## 4.6 Активировать

**Через REST API:**
```bash
curl -s -X PATCH http://localhost:5678/rest/workflows/ID \
  -H "X-N8N-API-KEY: ***" \
  -H "Content-Type: application/json" \
  -d '{"active": true}'
```

⚠️ **Активация через базу (UPDATE workflow_entity SET active=1) НЕ РАБОТАЕТ** — n8n не запускает scheduler.

## 4.7 n8n systemd-сервис

```ini
[Unit]
Description=n8n workflow automation
After=network.target

[Service]
Type=simple
User=aurum
Environment=N8N_PORT=5678
ExecStart=/home/aurum/.npm-global/bin/n8n start
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## 4.8 Диагностика

```bash
curl http://localhost:5678/healthz

curl -s http://localhost:5678/rest/workflows \
  -H "X-N8N-API-KEY: ***" | jq '.data[] | {id, name, active}'

sqlite3 ~/.n8n/database.sqlite \
  "SELECT status, startedAt FROM execution_entity ORDER BY startedAt DESC LIMIT 5;"
```

⚠️ **Если active:true, но запусков нет:**
1. n8n не перезапущен после активации
2. Workflow активирован через базу (не работает!)
3. Ошибка имён нод в connections

---

# MAX API — шпаргалка

| Метод | Путь | Описание |
|---|---|---|
| GET | `/me` | Информация о боте |
| GET | `/updates?marker=` | События (long polling, timeout до 90с) |
| POST | `/messages?user_id=` | Отправить сообщение |
| POST | `/chats/actions` | Действие: typing, смена статуса |
| POST | `/subscriptions` | Webhook-подписка (production) |

База: `https://platform-api.max.ru`
Auth: заголовок `Authorization: <токен_бота>`
Документация: [dev.max.ru/docs-api](https://dev.max.ru/docs-api)

## Формат /updates

```json
{
  "updates": [
    {
      "update_type": "message_created",
      "message": {
        "sender": { "user_id": 123, "is_bot": false },
        "body": { "text": "Привет!" }
      }
    }
  ],
  "marker": 1817
}
```

Маркер нужно сохранять и передавать в следующем запросе.

---

# Файлы

| Файл | Для чего |
|---|---|
| `max-bridge.py` | Python-мост (основной, рекомендуемый) |
| `max-bridge-workflow.json` | n8n workflow (альтернатива) |
| `README.md` | Этот гайд |

---

# Чеклист «бот не тупит»

- [ ] MAX-токен работает → `curl https://platform-api.max.ru/me -H "Authorization: …"`
- [ ] OpenClaw HTTP API включён → `curl localhost:18789/v1/models -H "Authorization: Bearer …"`
- [ ] `max-bridge.service` active → `sudo systemctl status max-bridge`
- [ ] В логах есть «Message from …» → `sudo journalctl -u max-bridge -f`
- [ ] В логах есть «Reply: …» и «Sent to MAX ✓»
- [ ] Модель Flash (быстро), а не Pro (долго)

## Если бот молчит

1. `sudo journalctl -u max-bridge -n 20` — смотреть ошибки
2. `sudo systemctl restart max-bridge` — перезапустить мост
3. Проверить что OpenClaw Gateway жив: `curl localhost:18789/health`
4. Проверить что MAX API принимает: `curl https://platform-api.max.ru/me -H "Authorization: …"`
