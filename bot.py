import asyncio
import base64
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from openai import APIError as OpenAIAPIError
    from openai import AsyncOpenAI
    from openai import RateLimitError as OpenAIRateLimitError
except ImportError:  # OpenAI is optional; the bot still stores raw entries.
    OpenAIAPIError = None
    AsyncOpenAI = None
    OpenAIRateLimitError = None

try:
    from gigachat import GigaChat
    from gigachat.exceptions import GigaChatException
except ImportError:  # GigaChat is optional; the bot still stores raw entries.
    GigaChat = None
    GigaChatException = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
EXPORTS_DIR = DATA_DIR / "exports"
BACKUPS_DIR = DATA_DIR / "backups"
DIARY_PATH = DATA_DIR / "diary.jsonl"
QUESTIONS_PATH = BASE_DIR / "questions.json"

DEFAULT_QUESTIONS = [
    "Какой был сон? Ответ: от 0 до 10 баллов, 10 - очень хороший.",
    "Во сколько уснул? Ответ: примерное время.",
    "Какой уровень болевых ощущений? Ответ: от 0 до 10 баллов, 0 - боли нет совсем, 10 - боль очень сильная.",
    "Занимался ли спортом? Ответ: да/нет.",
]

DAILY_CHECKIN_QUESTIONS = [
    {
        "key": "sleep_quality",
        "question": "Какой был сон? Ответьте числом от 0 до 10, где 10 - очень хороший сон.",
    },
    {
        "key": "fell_asleep_at",
        "question": "Во сколько уснули? Можно примерное время, например 23:30 или около полуночи.",
    },
    {
        "key": "pain_level",
        "question": "Какой уровень болевых ощущений? Ответьте числом от 0 до 10, где 0 - боли нет совсем, 10 - боль очень сильная.",
    },
    {
        "key": "did_sport",
        "question": "Занимались ли спортом? Ответьте да или нет.",
    },
]


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    allowed_chat_ids: set[int]
    questions_time: time
    diary_day_start: time
    timezone: str
    openai_api_key: str | None
    gigachat_api_key: str | None
    gigachat_verify_ssl_certs: bool


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before starting the bot.")
    if token == "123456:replace_with_token_from_botfather":
        raise RuntimeError("Replace TELEGRAM_BOT_TOKEN in .env with the real token from BotFather.")

    allowed = {
        int(value.strip())
        for value in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
        if value.strip()
    }

    timezone = os.getenv("TIMEZONE", "Asia/Novosibirsk")
    try:
        tzinfo = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown TIMEZONE: {timezone}") from exc

    hour, minute = parse_hhmm(os.getenv("QUESTIONS_TIME", "20:00"), "QUESTIONS_TIME")
    diary_day_start_hour, diary_day_start_minute = parse_hhmm(
        os.getenv("DIARY_DAY_START", "04:00"),
        "DIARY_DAY_START",
    )
    return Settings(
        telegram_token=token,
        allowed_chat_ids=allowed,
        questions_time=time(hour=hour, minute=minute, tzinfo=tzinfo),
        diary_day_start=time(hour=diary_day_start_hour, minute=diary_day_start_minute),
        timezone=timezone,
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        gigachat_api_key=os.getenv("GIGACHAT_API_KEY") or os.getenv("GIGACHAT_CREDENTIALS") or None,
        gigachat_verify_ssl_certs=parse_bool(os.getenv("GIGACHAT_VERIFY_SSL_CERTS", "true")),
    )


def parse_hhmm(value: str, setting_name: str = "time setting") -> tuple[int, int]:
    try:
        hour_text, minute_text = value.split(":", maxsplit=1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError as exc:
        raise RuntimeError(f"{setting_name} must use HH:MM format, for example 20:00.") from exc

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise RuntimeError(f"{setting_name} must use a valid 24-hour time.")
    return hour, minute


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def get_diary_date(moment: datetime, diary_day_start: time) -> str:
    diary_date = moment.date()
    if (moment.hour, moment.minute) < (diary_day_start.hour, diary_day_start.minute):
        diary_date -= timedelta(days=1)
    return diary_date.isoformat()


def get_entry_diary_date(entry: dict[str, Any], diary_day_start: time) -> str | None:
    if entry.get("diary_date"):
        return str(entry["diary_date"])

    sent_at = entry.get("message_sent_at")
    if sent_at:
        try:
            return get_diary_date(datetime.fromisoformat(sent_at), diary_day_start)
        except ValueError:
            logging.warning("Could not parse message_sent_at for diary date: %s", sent_at)

    message_sent_date = entry.get("message_sent_date")
    return str(message_sent_date) if message_sent_date else None


def parse_diary_date_argument(text: str, settings: Settings) -> tuple[str | None, str | None]:
    normalized = text.strip().lower().replace("ё", "е")
    tzinfo = settings.questions_time.tzinfo
    current_diary_date = datetime.fromisoformat(
        get_diary_date(datetime.now(tzinfo), settings.diary_day_start)
    ).date()

    if not normalized or normalized in {"сегодня", "today"}:
        return current_diary_date.isoformat(), None
    if normalized in {"вчера", "yesterday"}:
        return (current_diary_date - timedelta(days=1)).isoformat(), None

    try:
        return datetime.strptime(normalized, "%Y-%m-%d").date().isoformat(), None
    except ValueError:
        return None, "Укажите дату как `2026-06-05`, `сегодня` или `вчера`."


def read_questions() -> list[str]:
    if not QUESTIONS_PATH.exists():
        return DEFAULT_QUESTIONS

    with QUESTIONS_PATH.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    questions = payload.get("questions", [])
    return [str(question).strip() for question in questions if str(question).strip()]


def ensure_storage() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    UPLOADS_DIR.mkdir(exist_ok=True)
    EXPORTS_DIR.mkdir(exist_ok=True)
    BACKUPS_DIR.mkdir(exist_ok=True)
    if not QUESTIONS_PATH.exists():
        QUESTIONS_PATH.write_text(
            json.dumps({"questions": DEFAULT_QUESTIONS}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def is_allowed(update: Update, settings: Settings) -> bool:
    if not settings.allowed_chat_ids:
        return True
    chat = update.effective_chat
    return chat is not None and chat.id in settings.allowed_chat_ids


async def require_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings: Settings = context.application.bot_data["settings"]
    if is_allowed(update, settings):
        return True

    if update.effective_message:
        await update.effective_message.reply_text("Этот бот ведет личный дневник и не принимает записи из этого чата.")
    return False


def build_entry(
    update: Update,
    settings: Settings,
    entry_type: str,
    summary: str,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    chat = update.effective_chat
    user = update.effective_user
    tzinfo = settings.questions_time.tzinfo
    received_at = datetime.now(tzinfo).astimezone(tzinfo)
    message = update.effective_message
    sent_at = message.date.astimezone(tzinfo) if message and message.date else received_at
    diary_date = get_diary_date(sent_at, settings.diary_day_start)

    return {
        "created_at": received_at.isoformat(timespec="seconds"),
        "message_sent_at": sent_at.isoformat(timespec="seconds"),
        "message_sent_date": sent_at.date().isoformat(),
        "diary_date": diary_date,
        "received_at": received_at.isoformat(timespec="seconds"),
        "received_date": received_at.date().isoformat(),
        "type": entry_type,
        "summary": summary,
        "chat": {"id": chat.id if chat else None, "title": chat.title if chat else None},
        "user": {
            "id": user.id if user else None,
            "username": user.username if user else None,
            "name": user.full_name if user else None,
        },
        "raw": raw or {},
    }


async def save_entry(entry: dict[str, Any]) -> None:
    line = json.dumps(entry, ensure_ascii=False)
    await asyncio.to_thread(append_line, DIARY_PATH, line)


def append_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(line + "\n")


async def read_entries() -> list[dict[str, Any]]:
    if not DIARY_PATH.exists():
        return []
    return await asyncio.to_thread(read_entries_sync)


def read_entries_sync() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    with DIARY_PATH.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                logging.warning("Skipping invalid diary line: %s", line[:120])
    return entries


async def rewrite_entries_with_backup(entries: list[dict[str, Any]]) -> Path:
    return await asyncio.to_thread(rewrite_entries_with_backup_sync, entries)


def rewrite_entries_with_backup_sync(entries: list[dict[str, Any]]) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUPS_DIR / f"diary_{timestamp}.jsonl"
    if DIARY_PATH.exists():
        shutil.copy2(DIARY_PATH, backup_path)
    else:
        backup_path.write_text("", encoding="utf-8")

    lines = [json.dumps(entry, ensure_ascii=False) for entry in entries]
    DIARY_PATH.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return backup_path


def get_entries_for_diary_date(
    entries: list[dict[str, Any]],
    diary_date: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    return [
        entry
        for entry in entries
        if get_entry_diary_date(entry, settings.diary_day_start) == diary_date
    ]


def format_entry(entry: dict[str, Any], index: int | None = None) -> str:
    sent_at = entry.get("message_sent_at") or entry.get("created_at", "")
    received_at = entry.get("received_at")
    diary_date = entry.get("diary_date")
    entry_type = entry.get("type", "entry")
    summary = entry.get("summary", "")
    prefix = f"{index}. " if index is not None else ""
    header = f"{prefix}{sent_at} [{entry_type}]"
    if diary_date and diary_date != entry.get("message_sent_date"):
        header += f"\nДень дневника: {diary_date}"
    if received_at and received_at != sent_at:
        header += f"\nПолучено ботом: {received_at}"
    return f"{header}\n{summary}".strip()


def split_telegram_messages(text: str, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        candidate = block if not current else current + "\n\n" + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = block

    if current:
        chunks.append(current)
    return chunks


def escape_markdown_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def format_export_entry(entry: dict[str, Any], index: int) -> str:
    entry_type = entry.get("type", "entry")
    sent_at = entry.get("message_sent_at") or entry.get("created_at", "")
    received_at = entry.get("received_at")
    summary = escape_markdown_text(str(entry.get("summary") or ""))

    lines = [f"### {index}. {entry_type}"]
    if sent_at:
        lines.append(f"- Отправлено: {sent_at}")
    if received_at and received_at != sent_at:
        lines.append(f"- Получено ботом: {received_at}")

    raw = entry.get("raw") or {}
    if entry_type == "daily_checkin":
        answers = raw.get("answers") or {}
        if answers:
            did_sport = "да" if answers.get("did_sport") is True else "нет"
            lines.extend(
                [
                    "",
                    "- Сон: " + str(answers.get("sleep_quality")) + "/10",
                    "- Уснул: " + str(answers.get("fell_asleep_at")),
                    "- Боль: " + str(answers.get("pain_level")) + "/10",
                    "- Спорт: " + did_sport,
                ]
            )
            return "\n".join(lines)

    if summary:
        lines.extend(["", summary])
    return "\n".join(lines)


def build_markdown_export(entries: list[dict[str, Any]], settings: Settings) -> str:
    generated_at = datetime.now(settings.questions_time.tzinfo).isoformat(timespec="seconds")
    lines = [
        "# Дневник питания и самочувствия",
        "",
        f"Экспорт создан: {generated_at}",
        "",
    ]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        diary_date = get_entry_diary_date(entry, settings.diary_day_start) or "без даты"
        grouped.setdefault(diary_date, []).append(entry)

    for diary_date in sorted(grouped):
        lines.extend([f"## {diary_date}", ""])
        for index, entry in enumerate(grouped[diary_date], 1):
            lines.append(format_export_entry(entry, index))
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def get_daily_checkin_sessions(context: ContextTypes.DEFAULT_TYPE) -> dict[int, dict[str, Any]]:
    return context.application.bot_data.setdefault("daily_checkins", {})


def get_daily_checkin_questions() -> list[dict[str, str]]:
    questions = [dict(question) for question in DAILY_CHECKIN_QUESTIONS]
    configured_questions = read_questions()
    if len(configured_questions) >= len(questions):
        for question, configured_text in zip(questions, configured_questions):
            question["question"] = configured_text
    return questions


def parse_score_answer(text: str) -> tuple[int | None, str | None]:
    match = re.search(r"\b(10|[0-9])\b", text.strip())
    if not match:
        return None, "Ответьте числом от 0 до 10."
    return int(match.group(1)), None


def parse_yes_no_answer(text: str) -> tuple[bool | None, str | None]:
    normalized = text.strip().lower().replace("ё", "е")
    yes_values = {"да", "д", "yes", "y", "+", "ага", "занимался", "занималась"}
    no_values = {"нет", "не", "н", "no", "n", "-", "не занимался", "не занималась"}
    if normalized in yes_values:
        return True, None
    if normalized in no_values:
        return False, None
    return None, "Ответьте да или нет."


def parse_daily_checkin_answer(question_key: str, text: str) -> tuple[Any | None, str | None]:
    if question_key in {"sleep_quality", "pain_level"}:
        return parse_score_answer(text)
    if question_key == "did_sport":
        return parse_yes_no_answer(text)

    answer = text.strip()
    if not answer:
        return None, "Напишите примерное время."
    return answer, None


def format_daily_checkin_summary(answers: dict[str, Any]) -> str:
    did_sport = "да" if answers.get("did_sport") is True else "нет"
    return (
        "Ежедневная проверка самочувствия:\n"
        f"Сон: {answers.get('sleep_quality')}/10\n"
        f"Уснул: {answers.get('fell_asleep_at')}\n"
        f"Боль: {answers.get('pain_level')}/10\n"
        f"Спорт: {did_sport}"
    )


async def format_previous_daily_checkin(chat_id: int, target_date: str, settings: Settings) -> str | None:
    entries = await read_entries()
    checkins = [
        entry
        for entry in entries
        if entry.get("type") == "daily_checkin"
        and get_entry_diary_date(entry, settings.diary_day_start) == target_date
        and (entry.get("chat") or {}).get("id") == chat_id
    ]
    if not checkins:
        return None

    raw = checkins[-1].get("raw") or {}
    answers = raw.get("answers") or {}
    if not answers:
        return checkins[-1].get("summary")

    did_sport = "да" if answers.get("did_sport") is True else "нет"
    return (
        f"Ответы за вчера ({target_date}):\n"
        f"Сон: {answers.get('sleep_quality')}/10\n"
        f"Уснул: {answers.get('fell_asleep_at')}\n"
        f"Боль: {answers.get('pain_level')}/10\n"
        f"Спорт: {did_sport}"
    )


async def start_daily_checkin_for_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    settings: Settings = context.application.bot_data["settings"]
    tzinfo = settings.questions_time.tzinfo
    current_diary_date = datetime.fromisoformat(get_diary_date(datetime.now(tzinfo), settings.diary_day_start)).date()
    yesterday_date = (current_diary_date - timedelta(days=1)).isoformat()
    yesterday_checkin = await format_previous_daily_checkin(chat_id, yesterday_date, settings)
    questions = get_daily_checkin_questions()
    sessions = get_daily_checkin_sessions(context)
    sessions[chat_id] = {
        "current_index": 0,
        "answers": {},
        "questions": questions,
        "prompted_at": datetime.now(tzinfo).isoformat(timespec="seconds"),
    }
    intro = "Ежедневная проверка самочувствия."
    if yesterday_checkin:
        intro += "\n\n" + yesterday_checkin
    else:
        intro += f"\n\nЗа вчера ({yesterday_date}) сохраненного чек-ина не нашел."

    await context.bot.send_message(
        chat_id=chat_id,
        text=intro + "\n\nСегодня:\n" + questions[0]["question"],
    )


async def handle_daily_checkin_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    if not chat or not message:
        return False

    sessions = get_daily_checkin_sessions(context)
    session = sessions.get(chat.id)
    if not session:
        return False

    questions = session.get("questions") or DAILY_CHECKIN_QUESTIONS
    question = questions[session["current_index"]]
    answer, error = parse_daily_checkin_answer(question["key"], message.text or "")
    if error:
        await message.reply_text(error + "\n\n" + question["question"])
        return True

    session["answers"][question["key"]] = answer
    next_index = session["current_index"] + 1
    if next_index < len(questions):
        session["current_index"] = next_index
        await message.reply_text(questions[next_index]["question"])
        return True

    settings: Settings = context.application.bot_data["settings"]
    answers = session["answers"]
    summary = format_daily_checkin_summary(answers)
    await save_entry(
        build_entry(
            update,
            settings,
            "daily_checkin",
            summary,
            {
                "answers": answers,
                "prompted_at": session.get("prompted_at"),
                "questions": questions,
            },
        )
    )
    sessions.pop(chat.id, None)
    await message.reply_text("Записал ежедневную проверку:\n" + summary)
    return True


async def summarize_text(text: str, settings: Settings) -> str:
    if not text.strip():
        return "Пустое текстовое сообщение."

    if settings.gigachat_api_key:
        summary = await summarize_text_with_gigachat(text, settings)
        if summary:
            return summary

    client = make_openai_client(settings)
    if client is None:
        return text.strip()

    try:
        response = await client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "system",
                    "content": (
                        "Ты ведешь личный дневник питания и самочувствия. "
                        "Кратко, аккуратно и без медицинских диагнозов законспектируй запись. "
                        "Выдели еду, время/контекст, симптомы и вопросы, если они есть. "
                        "Не добавляй факты, время, погоду, активность, симптомы или вопросы, которых нет в сообщении. "
                        "Если информации нет, напиши: не указано."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
    except (OpenAIAPIError, OpenAIRateLimitError) as exc:
        logging.warning("OpenAI text summarization failed: %s", exc)
        return text.strip()

    return response.output_text.strip()


async def transcribe_voice(path: Path, settings: Settings) -> str:
    client = make_openai_client(settings)
    if client is None:
        return f"Голосовое сообщение сохранено: {path.name}. Для расшифровки добавьте OPENAI_API_KEY."

    try:
        with path.open("rb") as audio:
            transcript = await client.audio.transcriptions.create(
                model="gpt-4o-mini-transcribe",
                file=audio,
            )
    except (OpenAIAPIError, OpenAIRateLimitError) as exc:
        logging.warning("OpenAI voice transcription failed: %s", exc)
        return f"Голосовое сообщение сохранено: {path.name}. OpenAI API сейчас недоступен: {friendly_openai_error(exc)}"

    return transcript.text.strip()


async def describe_image(path: Path, caption: str | None, settings: Settings) -> str:
    client = make_openai_client(settings)
    if client is None:
        if caption:
            return f"Фото сохранено: {path.name}. Подпись: {caption}"
        return f"Фото сохранено: {path.name}. Для описания изображения добавьте OPENAI_API_KEY."

    image_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    prompt = "Опиши фото как запись дневника питания/самочувствия. Кратко перечисли видимую еду и важные детали."
    if caption:
        prompt += f"\nПодпись пользователя: {caption}"

    try:
        response = await client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}"},
                    ],
                }
            ],
        )
    except (OpenAIAPIError, OpenAIRateLimitError) as exc:
        logging.warning("OpenAI image description failed: %s", exc)
        return f"Фото сохранено: {path.name}. OpenAI API сейчас недоступен: {friendly_openai_error(exc)}"

    return response.output_text.strip()


def friendly_openai_error(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code == "insufficient_quota":
        return "недостаточно квоты или не настроена оплата в OpenAI Platform."
    return "проверьте ключ, оплату и доступность API."


async def summarize_text_with_gigachat(text: str, settings: Settings) -> str | None:
    if GigaChat is None:
        logging.warning("GIGACHAT_API_KEY is set, but gigachat package is not installed.")
        return None

    prompt = (
        "Ты ведешь личный дневник питания и самочувствия. "
        "Кратко и аккуратно законспектируй запись. "
        "Строго используй только факты из текста пользователя. "
        "Не придумывай время, погоду, физическую активность, симптомы, реакции, причины или вопросы. "
        "Если симптомов, вопросов или контекста нет в тексте, напиши: не указано. "
        "Формат ответа:\n"
        "Еда: ...\n"
        "Контекст: ...\n"
        "Самочувствие/симптомы: ...\n"
        "Вопросы пользователя: ...\n\n"
        f"Запись пользователя:\n{text}"
    )

    try:
        return await asyncio.to_thread(call_gigachat, prompt, settings)
    except Exception as exc:
        logging.warning("GigaChat summarization failed: %s", exc)
        return None


def call_gigachat(prompt: str, settings: Settings) -> str:
    with GigaChat(
        credentials=settings.gigachat_api_key,
        verify_ssl_certs=settings.gigachat_verify_ssl_certs,
    ) as client:
        response = client.chat(prompt)
    return response.choices[0].message.content.strip()


def make_openai_client(settings: Settings) -> Any | None:
    if not settings.openai_api_key or AsyncOpenAI is None:
        return None
    return AsyncOpenAI(api_key=settings.openai_api_key)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    chat_id = update.effective_chat.id if update.effective_chat else "неизвестен"
    await update.effective_message.reply_text(
        "Готов вести дневник. Присылайте текст, голосовые и фото еды или самочувствия.\n"
        f"ID этого чата: {chat_id}"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    await update.effective_message.reply_text(
        "Доступные команды:\n"
        "/start - начать работу и показать ID чата\n"
        "/help - показать список команд\n"
        "/last - показать последнюю запись дневника\n"
        "/today - показать записи за текущий дневниковый день\n"
        "/day - показать записи за выбранный дневниковый день\n"
        "/delete_last - удалить последнюю запись текущего чата\n"
        "/checkin - запустить ежедневную проверку самочувствия\n"
        "/fasting - отметить текущий дневниковый день как разгрузочный\n"
        "/export - отправить дневник Markdown-файлом\n\n"
        "Для /day и /fasting можно указать дату:\n"
        "/day вчера\n"
        "/day сегодня\n"
        "/day 2026-06-05\n"
        "/fasting вчера\n"
        "/fasting сегодня\n"
        "/fasting 2026-06-05"
    )


async def last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    entries = await read_entries()
    if not entries:
        await update.effective_message.reply_text("В дневнике пока нет записей.")
        return

    await update.effective_message.reply_text("Последняя запись:\n\n" + format_entry(entries[-1]))


async def today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    settings: Settings = context.application.bot_data["settings"]
    tzinfo = settings.questions_time.tzinfo
    today_date = get_diary_date(datetime.now(tzinfo), settings.diary_day_start)
    entries = get_entries_for_diary_date(await read_entries(), today_date, settings)
    if not entries:
        await update.effective_message.reply_text("За сегодня записей пока нет.")
        return

    text = "Записи за сегодня:\n\n" + "\n\n".join(
        format_entry(entry, index) for index, entry in enumerate(entries, 1)
    )
    for chunk in split_telegram_messages(text):
        await update.effective_message.reply_text(chunk)


async def day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    settings: Settings = context.application.bot_data["settings"]
    argument = " ".join(context.args or [])
    diary_date, error = parse_diary_date_argument(argument, settings)
    if error:
        await update.effective_message.reply_text(error)
        return

    entries = get_entries_for_diary_date(await read_entries(), diary_date, settings)
    if not entries:
        await update.effective_message.reply_text(f"За {diary_date} записей нет.")
        return

    text = f"Записи за {diary_date}:\n\n" + "\n\n".join(
        format_entry(entry, index) for index, entry in enumerate(entries, 1)
    )
    for chunk in split_telegram_messages(text):
        await update.effective_message.reply_text(chunk)


async def delete_last(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    chat = update.effective_chat
    if not chat:
        return

    entries = await read_entries()
    if not entries:
        await update.effective_message.reply_text("В дневнике пока нет записей.")
        return

    delete_index = None
    for index in range(len(entries) - 1, -1, -1):
        if (entries[index].get("chat") or {}).get("id") == chat.id:
            delete_index = index
            break

    if delete_index is None:
        await update.effective_message.reply_text("Не нашел записей для этого чата.")
        return

    deleted_entry = entries.pop(delete_index)
    backup_path = await rewrite_entries_with_backup(entries)
    await update.effective_message.reply_text(
        "Удалил последнюю запись:\n\n"
        + format_entry(deleted_entry)
        + f"\n\nРезервная копия: {backup_path}"
    )


async def checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    chat = update.effective_chat
    if not chat:
        return
    await start_daily_checkin_for_chat(context, chat.id)


async def fasting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    settings: Settings = context.application.bot_data["settings"]
    argument = " ".join(context.args or [])
    diary_date, error = parse_diary_date_argument(argument, settings)
    if error:
        await update.effective_message.reply_text(error)
        return

    summary = f"Разгрузочный день: еды не было. Дата дневника: {diary_date}."
    entry = build_entry(
        update,
        settings,
        "fasting_day",
        summary,
        {
            "marked_diary_date": diary_date,
            "command_argument": argument,
        },
    )
    entry["diary_date"] = diary_date
    await save_entry(entry)
    await update.effective_message.reply_text(f"Записал разгрузочный день за {diary_date}.")


async def export_diary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    entries = await read_entries()
    if not entries:
        await update.effective_message.reply_text("В дневнике пока нет записей для экспорта.")
        return

    settings: Settings = context.application.bot_data["settings"]
    export_text = build_markdown_export(entries, settings)
    timestamp = datetime.now(settings.questions_time.tzinfo).strftime("%Y%m%d_%H%M%S")
    export_path = EXPORTS_DIR / f"diary_export_{timestamp}.md"
    await asyncio.to_thread(export_path.write_text, export_text, "utf-8")

    with export_path.open("rb") as file:
        await update.effective_message.reply_document(
            document=file,
            filename=export_path.name,
            caption="Экспорт дневника в Markdown.",
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    if await handle_daily_checkin_answer(update, context):
        return

    settings: Settings = context.application.bot_data["settings"]
    text = update.effective_message.text or ""
    summary = await summarize_text(text, settings)
    await save_entry(build_entry(update, settings, "text", summary, {"text": text}))
    await update.effective_message.reply_text("Записал:\n" + summary)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    settings: Settings = context.application.bot_data["settings"]
    voice = update.effective_message.voice
    file = await context.bot.get_file(voice.file_id)
    path = UPLOADS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{voice.file_unique_id}.ogg"
    await file.download_to_drive(path)

    transcript = await transcribe_voice(path, settings)
    summary = await summarize_text(transcript, settings)
    await save_entry(
        build_entry(
            update,
            settings,
            "voice",
            summary,
            {"file": str(path), "transcript": transcript, "duration": voice.duration},
        ),
    )
    if settings.openai_api_key:
        await update.effective_message.reply_text("Расшифровал и записал:\n" + summary)
    else:
        await update.effective_message.reply_text("Голосовое сохранил и записал:\n" + summary)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_allowed(update, context):
        return

    settings: Settings = context.application.bot_data["settings"]
    photo = update.effective_message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    path = UPLOADS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{photo.file_unique_id}.jpg"
    await file.download_to_drive(path)

    caption = update.effective_message.caption
    summary = await describe_image(path, caption, settings)
    await save_entry(build_entry(update, settings, "photo", summary, {"file": str(path), "caption": caption}))
    await update.effective_message.reply_text("Фото записал:\n" + summary)


async def send_daily_questions(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not settings.allowed_chat_ids:
        logging.warning("Daily questions need ALLOWED_CHAT_IDS, otherwise the bot does not know where to send them.")
        return

    for chat_id in settings.allowed_chat_ids:
        await start_daily_checkin_for_chat(context, chat_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    ensure_storage()
    settings = load_settings()

    asyncio.set_event_loop(asyncio.new_event_loop())

    application = Application.builder().token(settings.telegram_token).build()
    application.bot_data["settings"] = settings

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("last", last))
    application.add_handler(CommandHandler("today", today))
    application.add_handler(CommandHandler("day", day))
    application.add_handler(CommandHandler("delete_last", delete_last))
    application.add_handler(CommandHandler("checkin", checkin))
    application.add_handler(CommandHandler("fasting", fasting))
    application.add_handler(CommandHandler("export", export_diary))
    application.add_handler(MessageHandler(filters.VOICE, handle_voice))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.job_queue.run_daily(send_daily_questions, settings.questions_time, name="daily_questions")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
