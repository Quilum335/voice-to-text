# Telegram Audio Transcriber

Бот принимает voice/audio/video/document из Telegram и делает `.txt`-транскрипт локальной Whisper-совместимой моделью через `faster-whisper`. OpenAI API и GPT-токены не используются.

## Что внутри

- Python 3.12
- `aiohttp` + прямой Telegram Bot API polling
- `faster-whisper` для бесплатного локального STT
- `ffmpeg` для нарезки длинных записей
- SQLite для языковых настроек и статусов задач
- In-process очередь без Redis

## Команды бота

- `/start` или `/help` - справка
- `/lang ru` - русский
- `/lang en` - английский
- `/lang auto` - автоопределение

## Railway

1. Залей проект в GitHub.
2. На Railway выбери `GitHub Repository` и подключи репозиторий.
3. В Variables добавь минимум:
   - `BOT_TOKEN`
   - `DEFAULT_LANGUAGE=ru`
   - `WHISPER_MODEL=base`
   - `WHISPER_DEVICE=cpu`
   - `WHISPER_COMPUTE_TYPE=int8`
   - `WHISPER_BEAM_SIZE=1`
   - `MAX_CONCURRENT_JOBS=1`
4. Рекомендую добавить Volume и смонтировать его в `/data`, затем выставить:
   - `WORK_DIR=/data/bot-transcriber`
   - `DB_PATH=/data/bot-transcriber/bot.sqlite3`
   - `HF_HOME=/data/huggingface`

Первый запуск скачает модель с Hugging Face. Это бесплатно, но может занять несколько минут.

## Локальный SSL на Windows

Если при локальном запуске падает `SSLCertVerificationError: self-signed certificate in certificate chain`, значит HTTPS до `api.telegram.org` перехватывает антивирус или прокси. Для быстрого локального теста можно добавить в `.env`:

```env
TELEGRAM_SSL_VERIFY=0
```

На Railway оставляй `TELEGRAM_SSL_VERIFY=1`. Более правильный локальный вариант - экспортировать root certificate прокси/антивируса в `.pem` и указать путь:

```env
TELEGRAM_CA_FILE=C:\path\to\root-ca.pem
```

## Про большие файлы Telegram

Обычный `https://api.telegram.org` дает ботам скачивать файлы только до 20 MB. Поэтому длинные записи будут работать только если они сильно сжаты и меньше лимита.

Для настоящих 4+ часов включи локальный Telegram Bot API Server в этом же контейнере:

```env
USE_LOCAL_TELEGRAM_API=1
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
AUTO_TELEGRAM_LOGOUT=1
TELEGRAM_WORK_DIR=/data/telegram-bot-api
TELEGRAM_TEMP_DIR=/tmp/telegram-bot-api
```

`TELEGRAM_API_ID` и `TELEGRAM_API_HASH` берутся на https://my.telegram.org/apps. При `USE_LOCAL_TELEGRAM_API=1` переменную `TELEGRAM_API_BASE` руками ставить не нужно: `start.sh` сам переключит бота на `http://127.0.0.1:8081`.

## Качество и скорость

- `WHISPER_MODEL=tiny` - быстрее, хуже качество.
- `WHISPER_MODEL=base` - нормальный баланс для Railway CPU.
- `WHISPER_MODEL=small` - лучше, но может быть очень медленно на CPU.
- `WHISPER_BEAM_SIZE=1` - быстрее.
- `WHISPER_BEAM_SIZE=5` - обычно точнее, но медленнее.

Это не GPT reasoning. Тут нет платного “думать дольше”. Точность зависит от модели, качества звука, языка, шума и скорости CPU.
