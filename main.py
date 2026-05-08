import asyncio
import json
import logging
import math
import os
import shutil
import signal
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv


load_dotenv()


SUPPORTED_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
OFFICIAL_TELEGRAM_DOWNLOAD_LIMIT = 20 * 1024 * 1024
MODEL = None


@dataclass(frozen=True)
class Settings:
    bot_token: str
    telegram_api_base: str
    work_dir: Path
    db_path: Path
    default_language: str
    whisper_model: str
    whisper_device: str
    whisper_compute_type: str
    whisper_beam_size: int
    whisper_cpu_threads: int
    whisper_chunk_seconds: int
    whisper_prompt: str
    max_concurrent_jobs: int

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN is required")

        work_dir = Path(os.getenv("WORK_DIR", "/tmp/bot-transcriber")).resolve()
        db_path = Path(os.getenv("DB_PATH", str(work_dir / "bot.sqlite3"))).resolve()

        default_language = os.getenv("DEFAULT_LANGUAGE", "ru").strip().lower()
        if default_language not in {"ru", "en", "auto"}:
            default_language = "ru"

        return cls(
            bot_token=token,
            telegram_api_base=os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
            work_dir=work_dir,
            db_path=db_path,
            default_language=default_language,
            whisper_model=os.getenv("WHISPER_MODEL", "base").strip() or "base",
            whisper_device=os.getenv("WHISPER_DEVICE", "cpu").strip() or "cpu",
            whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "int8").strip() or "int8",
            whisper_beam_size=int(os.getenv("WHISPER_BEAM_SIZE", "1")),
            whisper_cpu_threads=int(os.getenv("WHISPER_CPU_THREADS", "0")),
            whisper_chunk_seconds=int(os.getenv("WHISPER_CHUNK_SECONDS", "600")),
            whisper_prompt=os.getenv("WHISPER_PROMPT", "").strip(),
            max_concurrent_jobs=max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1"))),
        )


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self.settings = settings
        self.session = session

    @property
    def is_official_api(self) -> bool:
        return self.settings.telegram_api_base == "https://api.telegram.org"

    def method_url(self, method: str) -> str:
        return f"{self.settings.telegram_api_base}/bot{self.settings.bot_token}/{method}"

    def file_url(self, file_path: str) -> str:
        return f"{self.settings.telegram_api_base}/file/bot{self.settings.bot_token}/{file_path.lstrip('/')}"

    async def request(self, method: str, data: Any | None = None) -> Any:
        url = self.method_url(method)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        async with self.session.post(url, data=data, timeout=timeout) as response:
            raw = await response.text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise TelegramError(f"Telegram returned non-JSON response: {raw[:300]}") from exc

        if not payload.get("ok"):
            description = payload.get("description", "unknown Telegram error")
            raise TelegramError(description)
        return payload.get("result")

    async def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        data: dict[str, Any] = {
            "timeout": 30,
            "allowed_updates": json.dumps(["message"]),
        }
        if offset is not None:
            data["offset"] = offset
        return await self.request("getUpdates", data=data)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        last_result = None
        for index, chunk in enumerate(split_text(text, 3900)):
            data: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": "true",
            }
            if reply_to_message_id is not None and index == 0:
                data["reply_to_message_id"] = reply_to_message_id
                data["allow_sending_without_reply"] = "true"
            if reply_markup is not None and index == 0:
                data["reply_markup"] = json.dumps(reply_markup)
            last_result = await self.request("sendMessage", data=data)
        return last_result

    async def send_document(
        self,
        chat_id: int,
        file_path: Path,
        caption: str,
        reply_to_message_id: int | None = None,
    ) -> Any:
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("caption", caption[:1024])
        if reply_to_message_id is not None:
            form.add_field("reply_to_message_id", str(reply_to_message_id))
            form.add_field("allow_sending_without_reply", "true")

        with file_path.open("rb") as file_handle:
            form.add_field(
                "document",
                file_handle,
                filename=file_path.name,
                content_type="text/plain",
            )
            return await self.request("sendDocument", data=form)

    async def get_file(self, file_id: str) -> dict[str, Any]:
        return await self.request("getFile", data={"file_id": file_id})

    async def download_file(self, file_path: str, destination: Path) -> None:
        source = Path(file_path)
        if source.is_absolute():
            if not source.exists():
                raise TelegramError(
                    "Локальный Telegram Bot API вернул абсолютный путь, но бот его не видит. "
                    "Запусти bot и telegram-bot-api в одном контейнере или подключи общий volume."
                )
            await asyncio.to_thread(shutil.copyfile, source, destination)
            return

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        async with self.session.get(self.file_url(file_path), timeout=timeout) as response:
            if response.status != 200:
                details = await response.text()
                raise TelegramError(f"Не удалось скачать файл из Telegram: HTTP {response.status}: {details[:300]}")
            with destination.open("wb") as output:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    output.write(chunk)


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER,
                duration INTEGER,
                language TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                transcript_path TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def get_language(self, user_id: int, default_language: str) -> str:
        row = self.conn.execute("SELECT language FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["language"] if row else default_language

    def set_language(self, user_id: int, language: str) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO users (user_id, language, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET language = excluded.language, updated_at = excluded.updated_at
            """,
            (user_id, language, now, now),
        )
        self.conn.commit()

    def create_job(self, job: dict[str, Any]) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO jobs (
                id, user_id, chat_id, message_id, file_id, file_name, file_size,
                duration, language, status, error, transcript_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job["user_id"],
                job["chat_id"],
                job["message_id"],
                job["file_id"],
                job["file_name"],
                job.get("file_size"),
                job.get("duration"),
                job["language"],
                job["status"],
                None,
                None,
                now,
                now,
            ),
        )
        self.conn.commit()

    def get_job(self, job_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"Job not found: {job_id}")
        return row

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values())
        values.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
        self.conn.commit()

    def mark_unfinished_interrupted(self) -> None:
        now = int(time.time())
        self.conn.execute(
            """
            UPDATE jobs
            SET status = 'failed',
                error = 'Процесс был перезапущен до завершения задачи.',
                updated_at = ?
            WHERE status IN ('queued', 'processing')
            """,
            (now,),
        )
        self.conn.commit()


def split_text(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) <= limit:
            current += line
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def language_label(language: str) -> str:
    return {"ru": "русский", "en": "английский", "auto": "авто"}[language]


def language_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            [{"text": "/lang ru"}, {"text": "/lang en"}, {"text": "/lang auto"}],
        ],
        "resize_keyboard": True,
        "is_persistent": False,
    }


def safe_filename(name: str) -> str:
    clean = "".join(char if char.isalnum() or char in "._- " else "_" for char in name).strip()
    return clean[:80] or "audio"


def guess_extension(file_name: str | None, mime_type: str | None) -> str:
    if file_name:
        suffix = Path(file_name).suffix.lower()
        if suffix:
            return suffix
    if not mime_type:
        return ".bin"
    return {
        "audio/ogg": ".ogg",
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/wav": ".wav",
        "audio/webm": ".webm",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
    }.get(mime_type, ".bin")


def extract_media(message: dict[str, Any]) -> dict[str, Any] | None:
    if "voice" in message:
        voice = message["voice"]
        return {
            "file_id": voice["file_id"],
            "file_name": f"voice_{message['message_id']}.oga",
            "file_size": voice.get("file_size"),
            "duration": voice.get("duration"),
        }

    if "audio" in message:
        audio = message["audio"]
        ext = guess_extension(audio.get("file_name"), audio.get("mime_type"))
        return {
            "file_id": audio["file_id"],
            "file_name": safe_filename(audio.get("file_name") or f"audio_{message['message_id']}{ext}"),
            "file_size": audio.get("file_size"),
            "duration": audio.get("duration"),
        }

    if "document" in message:
        document = message["document"]
        file_name = document.get("file_name") or f"document_{message['message_id']}"
        mime_type = document.get("mime_type", "")
        suffix = Path(file_name).suffix.lower()
        if not (mime_type.startswith("audio/") or mime_type.startswith("video/") or suffix in SUPPORTED_EXTENSIONS):
            return None
        ext = suffix or guess_extension(file_name, mime_type)
        return {
            "file_id": document["file_id"],
            "file_name": safe_filename(file_name if suffix else f"{file_name}{ext}"),
            "file_size": document.get("file_size"),
            "duration": None,
        }

    if "video" in message:
        video = message["video"]
        return {
            "file_id": video["file_id"],
            "file_name": f"video_{message['message_id']}.mp4",
            "file_size": video.get("file_size"),
            "duration": video.get("duration"),
        }

    if "video_note" in message:
        note = message["video_note"]
        return {
            "file_id": note["file_id"],
            "file_name": f"video_note_{message['message_id']}.mp4",
            "file_size": note.get("file_size"),
            "duration": note.get("duration"),
        }

    return None


def format_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "неизвестно"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, sec = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def format_timestamp(seconds: float) -> str:
    milliseconds = int((seconds - int(seconds)) * 1000)
    whole = int(seconds)
    hours, remainder = divmod(whole, 3600)
    minutes, sec = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}.{milliseconds:03d}"


def probe_duration_sync(path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logging.warning("ffprobe failed: %s", result.stderr.strip())
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def extract_chunk_sync(source: Path, destination: Path, start: int, length: int | None) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start),
    ]
    if length is not None:
        command.extend(["-t", str(length)])
    command.extend(
        [
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(destination),
        ]
    )
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")


def get_model(settings: Settings) -> Any:
    global MODEL
    if MODEL is None:
        from faster_whisper import WhisperModel

        logging.info(
            "Loading Whisper model=%s device=%s compute_type=%s",
            settings.whisper_model,
            settings.whisper_device,
            settings.whisper_compute_type,
        )
        MODEL = WhisperModel(
            settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
            cpu_threads=settings.whisper_cpu_threads,
        )
    return MODEL


def transcribe_chunk_sync(
    settings: Settings,
    chunk_path: Path,
    language: str,
    offset_seconds: int,
    previous_tail: str,
) -> tuple[str, str]:
    model = get_model(settings)
    language_code = None if language == "auto" else language
    prompt_parts = [settings.whisper_prompt, previous_tail]
    initial_prompt = "\n".join(part for part in prompt_parts if part).strip() or None

    segments, _info = model.transcribe(
        str(chunk_path),
        language=language_code,
        task="transcribe",
        beam_size=settings.whisper_beam_size,
        vad_filter=True,
        temperature=0.0,
        condition_on_previous_text=True,
        initial_prompt=initial_prompt,
    )

    lines: list[str] = []
    tail_parts: list[str] = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        start = format_timestamp(offset_seconds + segment.start)
        end = format_timestamp(offset_seconds + segment.end)
        lines.append(f"[{start} - {end}] {text}")
        tail_parts.append(text)

    tail = " ".join(tail_parts)[-1000:]
    return "\n".join(lines), tail


class TranscriptionBot:
    def __init__(self, settings: Settings, telegram: TelegramClient, store: Store) -> None:
        self.settings = settings
        self.telegram = telegram
        self.store = store
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.stopping = asyncio.Event()

    async def run(self) -> None:
        workers = [
            asyncio.create_task(self.worker_loop(worker_id))
            for worker_id in range(self.settings.max_concurrent_jobs)
        ]
        poller = asyncio.create_task(self.polling_loop())

        await self.stopping.wait()
        poller.cancel()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(poller, *workers, return_exceptions=True)

    async def polling_loop(self) -> None:
        offset: int | None = None
        while not self.stopping.is_set():
            try:
                updates = await self.telegram.get_updates(offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    await self.handle_update(update)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Polling failed")
                await asyncio.sleep(3)

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not message:
            return

        text = (message.get("text") or "").strip()
        if text.startswith("/"):
            await self.handle_command(message, text)
            return

        media = extract_media(message)
        if media is None:
            await self.telegram.send_message(
                message["chat"]["id"],
                "Пришли voice, audio, video или документ с аудио/видео. Я верну .txt с таймкодами.",
                reply_to_message_id=message["message_id"],
                reply_markup=language_keyboard(),
            )
            return

        await self.enqueue_media(message, media)

    async def handle_command(self, message: dict[str, Any], text: str) -> None:
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        command, *args = text.split(maxsplit=1)
        command = command.split("@", maxsplit=1)[0].lower()

        if command in {"/start", "/help"}:
            language = self.store.get_language(user_id, self.settings.default_language)
            await self.telegram.send_message(
                chat_id,
                (
                    "Привет. Пришли голосовое, аудио, видео или аудио-файл документом, "
                    "а я сделаю транскрипт локальной Whisper-моделью без GPT-токенов.\n\n"
                    f"Текущий язык: {language_label(language)}.\n"
                    "Команды: /lang ru, /lang en, /lang auto."
                ),
                reply_to_message_id=message["message_id"],
                reply_markup=language_keyboard(),
            )
            return

        if command == "/lang":
            language = args[0].strip().lower() if args else ""
            if language not in {"ru", "en", "auto"}:
                current = self.store.get_language(user_id, self.settings.default_language)
                await self.telegram.send_message(
                    chat_id,
                    (
                        f"Сейчас выбран язык: {language_label(current)}.\n"
                        "Поставить можно так: /lang ru, /lang en или /lang auto."
                    ),
                    reply_to_message_id=message["message_id"],
                    reply_markup=language_keyboard(),
                )
                return

            self.store.set_language(user_id, language)
            await self.telegram.send_message(
                chat_id,
                f"Готово, теперь язык: {language_label(language)}.",
                reply_to_message_id=message["message_id"],
                reply_markup=language_keyboard(),
            )
            return

        await self.telegram.send_message(
            chat_id,
            "Не знаю такую команду. Используй /help.",
            reply_to_message_id=message["message_id"],
            reply_markup=language_keyboard(),
        )

    async def enqueue_media(self, message: dict[str, Any], media: dict[str, Any]) -> None:
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]
        file_size = media.get("file_size")

        if self.telegram.is_official_api and file_size and file_size > OFFICIAL_TELEGRAM_DOWNLOAD_LIMIT:
            await self.telegram.send_message(
                chat_id,
                (
                    "Файл больше 20 MB. Обычный api.telegram.org такие файлы ботам не отдает.\n\n"
                    "Для длинных записей на Railway нужно добавить локальный Telegram Bot API Server "
                    "и указать TELEGRAM_API_BASE. Для маленьких голосовых можно слать как сейчас."
                ),
                reply_to_message_id=message["message_id"],
            )
            return

        language = self.store.get_language(user_id, self.settings.default_language)
        job_id = uuid.uuid4().hex[:12]
        self.store.create_job(
            {
                "id": job_id,
                "user_id": user_id,
                "chat_id": chat_id,
                "message_id": message["message_id"],
                "file_id": media["file_id"],
                "file_name": media["file_name"],
                "file_size": file_size,
                "duration": media.get("duration"),
                "language": language,
                "status": "queued",
            }
        )
        await self.queue.put(job_id)

        position = self.queue.qsize()
        await self.telegram.send_message(
            chat_id,
            (
                f"Задача #{job_id} в очереди. Позиция: {position}.\n"
                f"Язык: {language_label(language)}. Модель: {self.settings.whisper_model}."
            ),
            reply_to_message_id=message["message_id"],
        )

    async def worker_loop(self, worker_id: int) -> None:
        logging.info("Worker %s started", worker_id)
        while True:
            job_id = await self.queue.get()
            try:
                await self.process_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Job %s failed", job_id)
            finally:
                self.queue.task_done()

    async def process_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        chat_id = int(job["chat_id"])
        reply_to = int(job["message_id"])
        job_dir = self.settings.work_dir / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        self.store.update_job(job_id, status="processing")
        await self.telegram.send_message(chat_id, f"Задача #{job_id}: скачиваю файл.", reply_to_message_id=reply_to)

        try:
            tg_file = await self.telegram.get_file(job["file_id"])
            tg_path = tg_file.get("file_path")
            if not tg_path:
                raise TelegramError("Telegram не вернул file_path для скачивания файла.")

            input_path = job_dir / safe_filename(job["file_name"])
            await self.telegram.download_file(tg_path, input_path)

            duration = await asyncio.to_thread(probe_duration_sync, input_path)
            if duration is not None:
                self.store.update_job(job_id, duration=int(duration))
            await self.telegram.send_message(
                chat_id,
                (
                    f"Задача #{job_id}: файл получен, длина {format_seconds(duration)}. "
                    "Начинаю распознавание."
                ),
                reply_to_message_id=reply_to,
            )

            transcript_path = await self.transcribe_file(job_id, input_path, duration)
            self.store.update_job(job_id, status="done", transcript_path=str(transcript_path))

            caption = (
                f"Готово: #{job_id}\n"
                f"Язык: {language_label(job['language'])}\n"
                f"Длина: {format_seconds(duration)}"
            )
            if transcript_path.stat().st_size <= 3500:
                await self.telegram.send_message(
                    chat_id,
                    transcript_path.read_text(encoding="utf-8"),
                    reply_to_message_id=reply_to,
                )
            await self.telegram.send_document(chat_id, transcript_path, caption, reply_to_message_id=reply_to)
        except Exception as exc:
            error = str(exc)
            self.store.update_job(job_id, status="failed", error=error)
            await self.telegram.send_message(
                chat_id,
                f"Задача #{job_id} упала: {error[:1500]}",
                reply_to_message_id=reply_to,
            )
        finally:
            if os.getenv("KEEP_JOB_FILES", "0") != "1":
                for child in job_dir.iterdir():
                    if child.name.startswith("chunk_") or child.name == safe_filename(job["file_name"]):
                        child.unlink(missing_ok=True)

    async def transcribe_file(self, job_id: str, input_path: Path, duration: float | None) -> Path:
        job = self.store.get_job(job_id)
        chat_id = int(job["chat_id"])
        reply_to = int(job["message_id"])
        language = str(job["language"])
        job_dir = input_path.parent
        transcript_path = job_dir / f"{Path(job['file_name']).stem}_transcript.txt"
        transcript_path.write_text(
            (
                f"Job: {job_id}\n"
                f"File: {job['file_name']}\n"
                f"Language: {language_label(language)}\n"
                f"Model: {self.settings.whisper_model}\n"
                f"Duration: {format_seconds(duration)}\n\n"
            ),
            encoding="utf-8",
        )

        chunk_seconds = max(60, self.settings.whisper_chunk_seconds)
        total_chunks = 1 if duration is None else max(1, math.ceil(duration / chunk_seconds))
        previous_tail = ""

        for chunk_index in range(total_chunks):
            start = chunk_index * chunk_seconds
            length = None if duration is None else chunk_seconds
            chunk_path = job_dir / f"chunk_{chunk_index:04d}.wav"

            await asyncio.to_thread(extract_chunk_sync, input_path, chunk_path, start, length)
            text, previous_tail = await asyncio.to_thread(
                transcribe_chunk_sync,
                self.settings,
                chunk_path,
                language,
                start,
                previous_tail,
            )
            with transcript_path.open("a", encoding="utf-8") as output:
                if text:
                    output.write(text)
                    output.write("\n\n")

            chunk_path.unlink(missing_ok=True)

            completed = chunk_index + 1
            if total_chunks == 1 or completed == total_chunks or completed % 3 == 0:
                await self.telegram.send_message(
                    chat_id,
                    f"Задача #{job_id}: распознано {completed}/{total_chunks} частей.",
                    reply_to_message_id=reply_to,
                )

        return transcript_path


def install_signal_handlers(bot: TranscriptionBot) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bot.stopping.set)
        except NotImplementedError:
            signal.signal(sig, lambda _signum, _frame: bot.stopping.set())


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    settings = Settings.from_env()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.db_path)
    store.mark_unfinished_interrupted()

    async with aiohttp.ClientSession() as session:
        telegram = TelegramClient(settings, session)
        bot = TranscriptionBot(settings, telegram, store)
        install_signal_handlers(bot)
        try:
            await bot.run()
        finally:
            store.close()


if __name__ == "__main__":
    asyncio.run(main())
