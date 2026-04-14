# Report Bot for Telegram

Бот формирует три типа отчётов кнопками:
- План
- Предварительный отчёт
- Итоговый отчёт

У каждого сотрудника свои настройки:
- хештег сотрудника
- хештег города
- упоминание
- плановый трафик

## Файлы
- `bot.py` — основной код бота
- `requirements.txt` — зависимости

## Локальный запуск

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export BOT_TOKEN=твой_токен
python3 bot.py
```

## Railway

### 1. Подготовь репозиторий
Загрузи в GitHub:
- `bot.py`
- `requirements.txt`

### 2. Создай новый проект в Railway
Создай сервис из GitHub-репозитория.

### 3. Добавь переменную окружения
В сервисе Railway открой `Variables` и добавь:
- `BOT_TOKEN=...`

Railway позволяет задавать переменные на вкладке Variables, в том числе через RAW Editor. citeturn322499search5

### 4. Задай Start Command
Если Railway сам не определит запуск, укажи в `Settings` → `Start Command`:

```bash
python bot.py
```

Railway использует Start Command как процесс запуска деплоя и позволяет задать его вручную. citeturn322499search2turn322499search4

### 5. Добавь Volume для хранения настроек
Поскольку бот хранит пользовательские настройки в JSON, без persistent storage они могут пропасть при redeploy.

В Railway добавь Volume и примонтируй его к сервису, например в:
```text
/data
```

Volumes в Railway предназначены для persistent storage и сохраняют данные между деплоями и рестартами. citeturn322499search1turn322499search17turn322499search21

### 6. Ничего дополнительно настраивать в коде не нужно
Бот автоматически использует:
- `APP_DATA_DIR`, если ты задал его сам
- или `RAILWAY_VOLUME_MOUNT_PATH`, если volume примонтирован
- иначе упадёт обратно на локальную папку `./data`

Railway передаёт mount path volume через переменную `RAILWAY_VOLUME_MOUNT_PATH`. citeturn322499search0

### 7. Redeploy
После пуша в GitHub Railway может автоматически задеплоить сервис, если автодеплой включён. Управление GitHub autodeploy есть в настройках деплоя Railway. citeturn322499search22turn322499search23

## Как пользоваться

1. Нажми `/start`
2. Выбери:
   - `План`
   - `Предварительный отчёт`
   - `Итоговый отчёт`
   - `Настройки`
3. В `Настройках` один раз задай:
   - хештег сотрудника
   - хештег города
   - упоминание
   - плановый трафик
4. Дальше бот сам соберёт готовый текст отчёта

## Примечание
Бот рассчитан на один экземпляр сервиса. Если запустить несколько копий с polling, Telegram может вернуть конфликт `getUpdates`.
