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
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from telegram import Update, User
from telegram.ext import Application, CommandHandler, ContextTypes

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
)


@dataclass
class Target:
    user_id: str
    name: str


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"players": {}, "usage": {}}

    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Failed to load state, creating new state file")
        return {"players": {}, "usage": {}}

    payload.setdefault("players", {})
    payload.setdefault("usage", {})
    return payload


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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


def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN не задан. Пример: export BOT_TOKEN='123:abc'")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("roll", roll))
    application.add_handler(CommandHandler("hp", hp))
    application.add_handler(CommandHandler("dmg", dmg))
    application.add_handler(CommandHandler("heal", heal))
    application.add_handler(CommandHandler("resurrection", resurrection))
    application.add_handler(CommandHandler("Resurrection", resurrection))

    LOGGER.info("Starting Telegram bot polling")
    # Python 3.14+ no longer creates a default event loop for the main thread.
    # python-telegram-bot still expects one to exist when run_polling starts.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
