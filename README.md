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

## Про большие файлы Telegram

Обычный `https://api.telegram.org` дает ботам скачивать файлы только до 20 MB. Поэтому длинные записи будут работать только если они сильно сжаты и меньше лимита.

Для настоящих 4+ часов нужен локальный Telegram Bot API Server. Тогда укажи:

```env
TELEGRAM_API_BASE=http://your-local-bot-api-service:8081
```

Если сервер запущен в `--local` режиме и возвращает абсолютные пути к файлам, бот должен видеть эти пути через общий volume или быть в том же контейнере.

## Качество и скорость

- `WHISPER_MODEL=tiny` - быстрее, хуже качество.
- `WHISPER_MODEL=base` - нормальный баланс для Railway CPU.
- `WHISPER_MODEL=small` - лучше, но может быть очень медленно на CPU.
- `WHISPER_BEAM_SIZE=1` - быстрее.
- `WHISPER_BEAM_SIZE=5` - обычно точнее, но медленнее.

Это не GPT reasoning. Тут нет платного “думать дольше”. Точность зависит от модели, качества звука, языка, шума и скорости CPU.
