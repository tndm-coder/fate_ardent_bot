"""Telegram bot: "Фэйт Ардент, бросающая кубы".

Features:
- /roll [formula] [var=value ...]
- /roll Divination -> d20 prophecy mode
- Persistent HP system for chat participants
- /dmg <target>, /heal <target>, /resurrection <target>, /hp
"""

from __future__ import annotations

import json
import logging
import os
import random
import asyncio
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import error, request
from threading import Thread
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from telegram import Update, User
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from dice_roll import roll_formula

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
LOGGER = logging.getLogger(__name__)

STATE_PATH = Path("telegram_dnd_bot_state.json")
MAX_HP = 100
DAILY_LIMIT = 10
WEEKLY_RESURRECTION_LIMIT = 1
LLM_CHECK_INTERVAL_SECONDS = 1800
LLM_REPLY_PROBABILITY = 0.4
MAX_CONTEXT_MESSAGES = 20
MAX_PENDING_MESSAGES = 60

LLM_MODES: dict[str, dict[str, Any]] = {
    "silent": {
        "reply_probability": 0.05,
        "persona": "молчаливая и наблюдательная",
    },
    "balanced": {
        "reply_probability": 0.4,
        "persona": "загадочная, дерзкая и дружелюбная",
    },
    "chaotic": {
        "reply_probability": 0.8,
        "persona": "эксцентричная, импульсивная и игривая",
    },
}
DEFAULT_LLM_MODE = "balanced"


class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/", "/health", "/healthz"}:
            payload = b"ok"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.info("Keep-alive HTTP: " + format, *args)


def start_keep_alive_server() -> None:
    port_value = os.getenv("PORT")
    if not port_value:
        LOGGER.info("PORT не задан — keep-alive HTTP сервер отключён")
        return

    try:
        port = int(port_value)
    except ValueError:
        LOGGER.warning("Некорректный PORT=%r — keep-alive HTTP сервер отключён", port_value)
        return

    server = ThreadingHTTPServer(("0.0.0.0", port), KeepAliveHandler)
    thread = Thread(target=server.serve_forever, daemon=True, name="keep-alive-http")
    thread.start()
    LOGGER.info("Keep-alive HTTP сервер запущен на порту %s", port)


DIVINATION_LINES: dict[int, str] = {
    1: "не делай этого — ты буквально умрёшь.",
    2: "ох-ох, кажется, сегодня не твой день :)",
    3: "лучше отложи. правда, лучше отложи",
    4: "идея смелая… и опасная. подготовь резервные планы от b до x",
    5: "может сработать, если сначала помолиться всем кубическим богам.",
    6: "шансы скромные, но упрямство иногда творит чудеса.",
    7: "ну в целом, почти. будет близко, но скорее всего не выйдет",
    8: "получится, но с тобой произойдет неприятный казус",
    9: "выпало девять. как думаешь что это значит?",
    10: "в целом получится, но осторожно: лишний шаг — и будет драма.",
    11: "средне-хорошо. не легендарно, но достойно.",
    12: "да, если делать уверенно и без паники.",
    13: "кубы кивают. пахнет успехом. и шампунем",
    14: "очень неплохо: фортуна уже поправляет тебе корону, принцесса",
    15: "да! и красиво. плюс вайб и аура фарминг",
    16: "отличный знак. я смотрю ты неплоха",
    17: "почти триумф. главное — не сглазь.",
    18: "твой момент. делай и сияй. (empty e-hu, e-hu)",
    19: "великолепно. сегодня ты главный герой этого дерьма.",
    20: "БОГИ ВСТАЮТ ПЕРЕД ТОБОЙ НА КОЛЕНИ",
}


HELP_TEXT = (
    "Я — *Фэйт Ардент, бросающая кубы* 🔮🎲\n"
    "Таинственная провидица вашей партии.\n\n"
    "Команды:\n"
    "• `/roll` — бросить d20\n"
    "• `/roll 2d6+3`\n"
    "• `/roll Divination` — пророчество по d20\n"
    "• `/dmg <ник>` — нанести 1d8 урона (10 зарядов/день)\n"
    "• `/heal <ник>` — исцелить на 1d8 (10 зарядов/день)\n"
    "• `/resurrection <ник>` — вернуть к 100 HP (1/неделю)\n"
    "• `/hp` — твои текущие HP"
    "• `/message [текст]` — вручную вызвать ответ LLM (тест)\n"
    "• `/mode [silent|balanced|chaotic]` — режим автоответов LLM\n"
    "• `/llm [on|off]` — включить/выключить автоответы LLM"
)


@dataclass
class Target:
    user_id: str
    name: str


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"players": {}, "usage": {}, "llm": {"chats": {}}}

    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed to load state, creating new state file")
        return {"players": {}, "usage": {}, "llm": {"chats": {}}}

    payload.setdefault("players", {})
    payload.setdefault("usage", {})
    llm_payload = payload.setdefault("llm", {})
    llm_payload.setdefault("chats", {})
    return payload


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def llm_chat_state(state: dict[str, Any], chat_id: int) -> dict[str, Any]:
    chats = state["llm"]["chats"]
    chat = chats.setdefault(
        str(chat_id),
        {
            "pending": [],
            "responded_count": 0,
            "skipped_count": 0,
            "last_activity_at": None,
            "last_reply_at": None,
            "enabled": True,
            "mode": DEFAULT_LLM_MODE,
            "reply_probability": LLM_MODES[DEFAULT_LLM_MODE]["reply_probability"],
            "persona": LLM_MODES[DEFAULT_LLM_MODE]["persona"],
        },
    )
    chat.setdefault("pending", [])
    chat.setdefault("responded_count", 0)
    chat.setdefault("skipped_count", 0)
    chat.setdefault("last_activity_at", None)
    chat.setdefault("last_reply_at", None)
    chat.setdefault("enabled", True)
    chat.setdefault("mode", DEFAULT_LLM_MODE)
    chat.setdefault("reply_probability", LLM_MODES[DEFAULT_LLM_MODE]["reply_probability"])
    chat.setdefault("persona", LLM_MODES[DEFAULT_LLM_MODE]["persona"])
    return chat


def trim_pending(chat: dict[str, Any]) -> None:
    pending = chat.get("pending", [])
    if len(pending) > MAX_PENDING_MESSAGES:
        chat["pending"] = pending[-MAX_PENDING_MESSAGES:]


def build_llm_prompt(messages: list[dict[str, str]], persona: str) -> list[dict[str, str]]:
    context_rows = [f"- {row['author']}: {row['text']}" for row in messages]
    context_block = "\n".join(context_rows)
    return [
        {
            "role": "system",
            "content": (
                "Ты Фэйт Ардент — загадочная, дерзкая и дружелюбная тг-провидица. "
                f"Твой текущий стиль: {persona}. "
                "Пиши коротко и по делу (1-3 предложения), на русском, без токсичности."
            ),
        },
        {
            "role": "user",
            "content": (
                "Вот свежие сообщения из чата. Ответь уместной короткой репликой в стиль ролевого бота:\n"
                f"{context_block}"
            ),
        },
    ]


def call_chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout: float = 15,
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": 120,
        "temperature": 0.9,
    }
    request_payload = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=base_url,
        data=request_payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    with request.urlopen(req, timeout=timeout) as response:
        raw_body = response.read().decode("utf-8")
    body = json.loads(raw_body)
    text = body["choices"][0]["message"]["content"].strip()
    if not text:
        raise ValueError("LLM вернула пустой ответ")
    return text


async def generate_llm_reply(messages: list[dict[str, str]], persona: str) -> tuple[str, str]:
    prompt_messages = build_llm_prompt(messages, persona)

    providers = []
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    groq_key = os.getenv("GROQ_API_KEY")

    if openrouter_key:
        providers.append(
            {
                "name": "openrouter",
                "url": "https://openrouter.ai/api/v1/chat/completions",
                "api_key": openrouter_key,
                "model": os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini"),
            }
        )
    if groq_key:
        providers.append(
            {
                "name": "groq",
                "url": "https://api.groq.com/openai/v1/chat/completions",
                "api_key": groq_key,
                "model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
            }
        )

    if not providers:
        raise RuntimeError("Не заданы OPENROUTER_API_KEY и/или GROQ_API_KEY")

    errors_by_provider: dict[str, str] = {}
    for provider in providers:
        try:
            reply = await asyncio.to_thread(
                call_chat_completion,
                base_url=provider["url"],
                api_key=provider["api_key"],
                model=provider["model"],
                messages=prompt_messages,
            )
            return reply, provider["name"]
        except (error.HTTPError, error.URLError, KeyError, ValueError, json.JSONDecodeError) as exc:
            errors_by_provider[provider["name"]] = str(exc)

    raise RuntimeError(f"Все LLM-провайдеры недоступны: {errors_by_provider}")


def user_display_name(user: User | None) -> str:
    if not user:
        return "Странница"
    if user.full_name:
        return user.full_name
    if user.username:
        return f"@{user.username}"
    return str(user.id)


def ensure_player(state: dict[str, Any], user_id: str, name: str) -> dict[str, Any]:
    players = state["players"]
    player = players.get(user_id)
    if not player:
        player = {"name": name, "hp": MAX_HP}
        players[user_id] = player
    else:
        player["name"] = name
        player.setdefault("hp", MAX_HP)
    return player


def current_day_key() -> str:
    return date.today().isoformat()


def current_week_key() -> str:
    today = datetime.now().isocalendar()
    return f"{today.year}-W{today.week:02d}"


def actor_usage(state: dict[str, Any], actor_id: str) -> dict[str, Any]:
    usage = state["usage"].setdefault(actor_id, {})

    day_key = current_day_key()
    if usage.get("day") != day_key:
        usage["day"] = day_key
        usage["dmg"] = 0
        usage["heal"] = 0

    week_key = current_week_key()
    if usage.get("week") != week_key:
        usage["week"] = week_key
        usage["resurrection"] = 0

    usage.setdefault("dmg", 0)
    usage.setdefault("heal", 0)
    usage.setdefault("resurrection", 0)
    return usage


def find_target_from_arg(state: dict[str, Any], raw_target: str) -> Target | None:
    needle = raw_target.strip().lower().lstrip("@")
    if not needle:
        return None

    for user_id, player in state["players"].items():
        name = str(player.get("name", ""))
        if name.lower().lstrip("@") == needle:
            return Target(user_id=user_id, name=name)
    return None


def resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any]) -> Target | None:
    if update.message and update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user
        target_name = user_display_name(target_user)
        ensure_player(state, str(target_user.id), target_name)
        return Target(user_id=str(target_user.id), name=target_name)

    if context.args:
        matched = find_target_from_arg(state, context.args[0])
        if matched:
            return matched

    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return

    state = load_state()
    current_name = user_display_name(update.effective_user)
    ensure_player(state, str(update.effective_user.id), current_name)
    save_state(state)

    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    state = load_state()
    author_name = user_display_name(update.effective_user)
    ensure_player(state, str(update.effective_user.id), author_name)
    save_state(state)

    if context.args and context.args[0].lower() == "divination":
        value = random.randint(1, 20)
        prophecy = DIVINATION_LINES[value]
        await update.message.reply_text(
            f"🔮 Фэйт Ардент раскручивает нить судьбы... d20 = {value}\n"
            f"{prophecy}"
        )
        return

    formula = context.args[0] if context.args else "1d20"
    vars_payload = parse_vars(context.args[1:]) if context.args else {}

    try:
        result = roll_formula(formula, **vars_payload)
    except ValueError as exc:
        await update.message.reply_text(
            "🌫️ Туман скрывает формулу. Попробуй так:\n"
            "/roll, /roll 1d20+5, /roll 2д6+3, /roll ({str}+1)d20 str=3\n"
            f"Ошибка: {exc}"
        )
        return

    await update.message.reply_text(
        f"🎲 Фэйт Ардент шепчет: {author_name}, твой бросок {formula} = {result}"
    )


def parse_vars(tokens: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for token in tokens:
        if "=" not in token:
            continue
        key, raw_value = token.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            continue

        try:
            result[key] = int(raw_value)
        except ValueError:
            result[key] = raw_value
    return result


async def hp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message:
        return

    state = load_state()
    actor_name = user_display_name(update.effective_user)
    player = ensure_player(state, str(update.effective_user.id), actor_name)
    save_state(state)

    await update.message.reply_text(
        f"💗 {actor_name}, я вижу твою жизненную нить: {player['hp']} HP."
    )


async def dmg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await apply_delta(update, context, mode="dmg")


async def heal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await apply_delta(update, context, mode="heal")


async def apply_delta(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    if not update.message or not update.effective_user:
        return

    state = load_state()
    actor_name = user_display_name(update.effective_user)
    actor_id = str(update.effective_user.id)
    ensure_player(state, actor_id, actor_name)

    target = resolve_target(update, context, state)
    if not target:
        await update.message.reply_text(
            "✨ Укажи цель: `/dmg <ник>` или `/heal <ник>`, "
            "либо ответь командой на сообщение нужного игрока.",
            parse_mode="Markdown",
        )
        save_state(state)
        return

    usage = actor_usage(state, actor_id)
    if mode == "dmg" and usage["dmg"] >= DAILY_LIMIT:
        await update.message.reply_text("🕯️ На сегодня твои заряды урона исчерпаны (10/10).")
        save_state(state)
        return

    if mode == "heal" and usage["heal"] >= DAILY_LIMIT:
        await update.message.reply_text("🕯️ На сегодня твои заряды лечения исчерпаны (10/10).")
        save_state(state)
        return

    amount = random.randint(1, 8)
    player = ensure_player(state, target.user_id, target.name)

    if mode == "dmg":
        usage["dmg"] += 1
        player["hp"] = max(0, int(player["hp"]) - amount)
        if player["hp"] == 0:
            line = (
                f"💥 Фэйт Ардент наносит {amount} урона {player['name']}.\n"
                "прости ты умерла"
            )
        else:
            line = (
                f"💥 Фэйт Ардент наносит {amount} урона {player['name']}.\n"
                f"Осталось: {player['hp']} HP"
            )
    else:
        usage["heal"] += 1
        player["hp"] = min(MAX_HP, int(player["hp"]) + amount)
        line = (
            f"✨ Фэйт Ардент исцеляет {player['name']} на {amount} HP.\n"
            f"Теперь: {player['hp']} HP"
        )

    save_state(state)
    await update.message.reply_text(line)


async def resurrection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_user:
        return

    state = load_state()
    actor_id = str(update.effective_user.id)
    actor_name = user_display_name(update.effective_user)
    ensure_player(state, actor_id, actor_name)

    target = resolve_target(update, context, state)
    if not target:
        await update.message.reply_text(
            "🌙 Укажи, кого воскрешать: `/resurrection <ник>` "
            "или ответь командой на сообщение игрока.",
            parse_mode="Markdown",
        )
        save_state(state)
        return

    usage = actor_usage(state, actor_id)
    if usage["resurrection"] >= WEEKLY_RESURRECTION_LIMIT:
        await update.message.reply_text("⛔ На этой неделе у тебя уже был ритуал воскрешения (1/1).")
        save_state(state)
        return

    usage["resurrection"] += 1
    player = ensure_player(state, target.user_id, target.name)
    player["hp"] = MAX_HP
    save_state(state)

    await update.message.reply_text(
        f"🕊️ Фэйт Ардент возвращает {player['name']} из-за грани.\n"
        f"Жизнь восстановлена: {MAX_HP} HP."
    )


async def collect_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_chat:
        return

    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    state = load_state()
    chat = llm_chat_state(state, update.effective_chat.id)
    chat["pending"].append(
        {
            "author": user_display_name(update.effective_user),
            "text": text,
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    chat["last_activity_at"] = datetime.now().isoformat(timespec="seconds")
    trim_pending(chat)
    save_state(state)


async def llm_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.application:
        return

    state = load_state()
    chats_payload = state.get("llm", {}).get("chats", {})
    changed = False

    for raw_chat_id, chat in chats_payload.items():
        if not bool(chat.get("enabled", True)):
            continue

        pending = list(chat.get("pending", []))
        if not pending:
            continue

        changed = True
        reply_probability = float(chat.get("reply_probability", LLM_REPLY_PROBABILITY))
        if random.random() >= reply_probability:
            chat["skipped_count"] = int(chat.get("skipped_count", 0)) + 1
            chat["pending"] = []
            continue

        context_messages = pending[-MAX_CONTEXT_MESSAGES:]
        try:
            reply, provider = await generate_llm_reply(
                context_messages,
                str(chat.get("persona", LLM_MODES[DEFAULT_LLM_MODE]["persona"])),
            )
        except RuntimeError as exc:
            LOGGER.warning("LLM failed for chat %s: %s", raw_chat_id, exc)
            continue

        try:
            await context.application.bot.send_message(chat_id=int(raw_chat_id), text=reply)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to send LLM reply to chat %s: %s", raw_chat_id, exc)
            continue

        LOGGER.info("LLM replied in chat %s via %s", raw_chat_id, provider)
        chat["responded_count"] = int(chat.get("responded_count", 0)) + 1
        chat["last_reply_at"] = datetime.now().isoformat(timespec="seconds")
        chat["pending"] = []

    if changed:
        save_state(state)


async def force_llm_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    state = load_state()
    chat = llm_chat_state(state, update.effective_chat.id)
    pending = list(chat.get("pending", []))
    manual_text = " ".join(context.args).strip()

    if manual_text:
        pending.append(
            {
                "author": user_display_name(update.effective_user),
                "text": manual_text,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    if not pending:
        pending = [
            {
                "author": user_display_name(update.effective_user),
                "text": "Тест ручного триггера. Дай короткую реплику для чата.",
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        ]

    context_messages = pending[-MAX_CONTEXT_MESSAGES:]
    try:
        reply, provider = await generate_llm_reply(
            context_messages,
            str(chat.get("persona", LLM_MODES[DEFAULT_LLM_MODE]["persona"])),
        )
    except RuntimeError as exc:
        await update.message.reply_text(f"⚠️ Не удалось получить ответ LLM: {exc}")
        return

    chat["pending"] = []
    chat["responded_count"] = int(chat.get("responded_count", 0)) + 1
    chat["last_reply_at"] = datetime.now().isoformat(timespec="seconds")
    save_state(state)

    LOGGER.info("Manual LLM trigger in chat %s via %s", update.effective_chat.id, provider)
    await update.message.reply_text(reply)


async def llm_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    state = load_state()
    chat = llm_chat_state(state, update.effective_chat.id)

    if not context.args:
        current_mode = str(chat.get("mode", DEFAULT_LLM_MODE))
        current_probability = float(chat.get("reply_probability", LLM_REPLY_PROBABILITY))
        enabled = "on" if bool(chat.get("enabled", True)) else "off"
        await update.message.reply_text(
            "🎭 Текущий режим LLM: "
            f"{current_mode} (reply_probability={current_probability:.2f}, llm={enabled}).\n"
            "Доступные режимы: silent, balanced, chaotic"
        )
        return

    mode = context.args[0].strip().lower()
    selected = LLM_MODES.get(mode)
    if not selected:
        await update.message.reply_text(
            "⚠️ Неизвестный режим. Используй: /mode silent, /mode balanced или /mode chaotic"
        )
        return

    chat["mode"] = mode
    chat["reply_probability"] = selected["reply_probability"]
    chat["persona"] = selected["persona"]
    save_state(state)

    await update.message.reply_text(
        "✅ Режим LLM обновлён: "
        f"{mode} (reply_probability={selected['reply_probability']:.2f})."
    )


async def llm_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    state = load_state()
    chat = llm_chat_state(state, update.effective_chat.id)

    if not context.args:
        enabled = bool(chat.get("enabled", True))
        await update.message.reply_text(
            f"🤖 Автоответы LLM сейчас {'включены' if enabled else 'выключены'}. Используй /llm on|off"
        )
        return

    arg = context.args[0].strip().lower()
    if arg not in {"on", "off"}:
        await update.message.reply_text("⚠️ Используй: /llm on или /llm off")
        return

    chat["enabled"] = arg == "on"
    if arg == "off":
        chat["pending"] = []
    save_state(state)
    await update.message.reply_text(
        f"✅ Автоответы LLM {'включены' if chat['enabled'] else 'выключены'}."
    )


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Пример: export BOT_TOKEN='123:abc'")

    start_keep_alive_server()

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("roll", roll))
    application.add_handler(CommandHandler("hp", hp))
    application.add_handler(CommandHandler("dmg", dmg))
    application.add_handler(CommandHandler("heal", heal))
    application.add_handler(CommandHandler("resurrection", resurrection))
    application.add_handler(CommandHandler("Resurrection", resurrection))
    application.add_handler(CommandHandler("message", force_llm_message))
    application.add_handler(CommandHandler("mode", llm_mode))
    application.add_handler(CommandHandler("llm", llm_toggle))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, collect_message))

    if application.job_queue:
        application.job_queue.run_repeating(llm_tick, interval=LLM_CHECK_INTERVAL_SECONDS, first=60)
    else:
        LOGGER.warning("Job queue is unavailable; LLM periodic replies are disabled")

    LOGGER.info("Starting Telegram bot polling")
    # Python 3.14+ no longer creates a default event loop for the main thread.
    # python-telegram-bot still expects one to exist when run_polling starts.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
