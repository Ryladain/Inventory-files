import json
import random
import re
import asyncio
import subprocess, datetime
import os
import html
from pathlib import Path
from dotenv import load_dotenv
from rapidfuzz import fuzz, process

from telegram import (
    Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove,
    constants,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

BACK_RE = r"^(?:🔙\s*)?Назад$"


async def on_any_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await end_and_main_menu(update, context)

from telegram.ext import ConversationHandler


# === библиотека предметов ===
from item_catalog import init_catalogs, enrich_item, render_item_card, MAGIC, NONMAGIC

load_dotenv(dotenv_path=Path(__file__).with_name('.env'), override=True)
TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = Path("inventory_data.json")
DATA_DIR = (Path(__file__).parent / "data").resolve()

# --------- Таблицы и данные ---------
CATEGORIES_D20 = {
    1: "Одежда",
    range(2, 12): "Снаряжение",
    range(12, 14): "Наборы снаряжения",
    range(14, 16): "Инструменты",
    range(16, 18): "Доспехи",
    range(18, 20): "Оружие",
    20: "Магический предмет",
}

ITEMS = {
    "Одежда": [
        "комплект путешественника", "комплект простолюдина",
        "комплект знатного", "комплект мага",
    ],
    "Снаряжение": [
        "факел", "верёвка (15 м)", "рюкзак", "бутылка воды", "спальник",
        "фляга", "мешочек", "фляга масла", "зеркальце",
    ],
    "Наборы снаряжения": [
        "набор путешественника", "набор священника",
        "набор вора", "набор исследователя подземелий",
    ],
    "Инструменты": [
        "инструменты кузнеца", "инструменты вора",
        "инструменты художника", "музыкальный инструмент (лютня)",
    ],
    "Доспехи": [
        "кожаный доспех", "кольчужная рубаха", "латы", "щит",
    ],
    "Оружие": [
        "кинжал", "короткий меч", "длинный меч", "лук", "топор", "посох",
    ],
    "Магический предмет": [
        "зелье лечения", "меч +1", "кольцо защиты",
        "плащ защиты", "жезл молний", "мешок хранения",
    ],
}

RARITY_TABLE = [
    (30, "обычный"),
    (66, "необычный"),
    (81, "редкий"),
    (96, "значимый необычный"),
    (98, "очень редкий"),
    (100, "значимый редкий"),
]

STATE_REMOVE = 1
STATE_ADD_CATEGORY = 10
STATE_ADD_NAME = 11
STATE_ADD_CONFIRM = 12
STATE_REMOVE_CATEGORY = 20
STATE_SIMULATE_DAYS = 30
STATE_INVENTORY_CATEGORY = 40
STATE_INVENTORY_ITEM = 42

# =======================
#     МАСТЕР / ИГРОКИ
# =======================
MASTER_ID = 1840976992  # поменяешь на свой
PLAYERS = {
    "Карла": 111111111,
    "Энсо": 558026215,
    "Найт": 1615374911,
    "Гундар": 6141258332,
    "Авитус": 868719266,
}
PLAYER_WITH_SIMULATION = "Найт"


# --------- Хранилище инвентаря ---------
def _load_all():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {}

def _save_all(data):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_inventory(user_id: int):
    data = _load_all()
    inv = data.get(str(user_id), {})
    for cat in ITEMS.keys():
        inv.setdefault(cat, [])
    return inv

def save_inventory(user_id: int, inv: dict):
    data = _load_all()
    data[str(user_id)] = inv
    _save_all(data)


# --------- Механика выпадения ---------
def _choose_category_by_d20(roll: int) -> str:
    for k in CATEGORIES_D20:
        if isinstance(k, range) and roll in k:
            return CATEGORIES_D20[k]
        if k == roll:
            return CATEGORIES_D20[k]
    return "Снаряжение"

def _random_item(category: str) -> str:
    return random.choice(ITEMS[category])

def _magic_rarity():
    r = random.randint(1, 100)
    for threshold, rarity in RARITY_TABLE:
        if r <= threshold:
            return rarity, r
    return "обычный", r

def _lose_item(inv: dict):
    while True:
        r = random.randint(1, 20)
        cat = _choose_category_by_d20(r)
        if inv[cat]:
            lost = random.choice(inv[cat])
            inv[cat].remove(lost)
            return cat, lost, r

def _find_item(inv: dict):
    r = random.randint(1, 20)
    cat = _choose_category_by_d20(r)
    found = _random_item(cat)

    if cat == "Магический предмет":
        rarity_label, r100 = _magic_rarity()
        if "значимый" in rarity_label.lower():
            base_rarity = "Необычный" if "необыч" in rarity_label else "Редкий"
            tier = "Значительный"
        else:
            base_rarity = rarity_label.capitalize()
            tier = "Незначительный"

        pool = [i for i in MAGIC if i.get("rarity") == base_rarity and i.get("tier") == tier]
        if pool:
            chosen = random.choice(pool)
            found = chosen["name"]
            full_info = enrich_item({"name": found, "category": cat})
            desc = full_info.get("description", "")
            if desc:
                found += f" — {desc[:600].strip()}…"
        else:
            found = f"Не найдено ({base_rarity}, {tier})"
        found = f"{found} ({rarity_label}, d100={r100})"

    inv[cat].append(found)
    return cat, found, r


# --------- Хелперы отображения / формата ---------
def parse_item_entry(entry):
    """Возвращает (name, desc|None) из строки/словаря."""
    if isinstance(entry, dict):
        return (entry.get("name", "").strip(), (entry.get("description") or entry.get("desc")))
    s = str(entry)
    if "—" in s:
        nm, ds = s.split("—", 1)
        return nm.strip().lstrip("⭐ ").strip(), ds.strip()
    return s.strip().lstrip("⭐ ").strip(), None

def make_custom_string(name: str, desc: str | None):
    desc = (desc or "— пользовательское описание —").strip()
    return f"⭐ {name.strip()} — {desc}"

def normalize_text(s: str) -> str:
    return (s or "").strip().lower()


# ---------- Ролевые клавиатуры и возврат ----------
def _kb_master_root():
    return ReplyKeyboardMarkup([["📜 Мастер-инвентарь"]], resize_keyboard=True)

def _kb_player_base(with_sim=False):
    rows = [
        ["➕ Добавить предмет", "➖ Удалить предмет"],
        ["📦 Инвентарь"],
        ["📚 Категории"],
    ]
    if with_sim:
        rows[1].append("🎲 Симулировать день")
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def _kb_guest():
    return ReplyKeyboardMarkup([["📚 Категории"]], resize_keyboard=True)

def home_kb(update, context):
    """Корректное меню по роли и выбранному игроку (для мастера)."""
    uid = update.effective_user.id
    if uid == MASTER_ID:
        target_name = context.user_data.get("target_name")
        if not target_name:
            return _kb_master_root()
        return _kb_player_base(with_sim=(target_name == PLAYER_WITH_SIMULATION))
    for name, pid in PLAYERS.items():
        if uid == pid:
            return _kb_player_base(with_sim=(name == PLAYER_WITH_SIMULATION))
    return _kb_guest()

async def go_home(update, context, text="↩️ Возврат в главное меню."):
    if update.callback_query:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=home_kb(update, context))
    else:
        await update.message.reply_text(text, reply_markup=home_kb(update, context))

def keyboard_for(update, context):
    # чтобы старые вызовы keyboard_for не падали
    return home_kb(update, context)

async def end_and_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str = "↩️ Возврат в главное меню."):
    """Корректно завершает любой Conversation и показывает актуальное меню."""
    chat_id = update.effective_chat.id if update.effective_chat else update.callback_query.message.chat_id
    for k in ("inv_cat","inv_page","inv_items","remove_cat","page","items","add_cat","pending_item","pending_desc","raw_name","pending"):
        context.user_data.pop(k, None)
    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=home_kb(update, context))
    return ConversationHandler.END


# --------- Команды ---------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 🧙‍♂️ Я D&D инвентарь-бот.\nВыбери действие из меню ниже:",
        reply_markup=home_kb(update, context),
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/inventory, /add, /remove, /simulate, /categories")

async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order = ["Одежда","Снаряжение","Наборы снаряжения","Инструменты","Доспехи","Оружие","Магический предмет"]
    await update.message.reply_text("📚 Категории:\n" + "\n".join(f"• {c}" for c in order))


# --------- Показ инвентаря и предметов ---------
async def show_inventory(update, context):
    uid = update.effective_user.id
    inv = get_inventory(uid)

    def esc(s): return html.escape(str(s)) if s else ""

    blocks = ["<b>🎒 Инвентарь:</b>"]
    for cat, lst in inv.items():
        blocks.append(f"<b>{esc(cat)}:</b>")
        if not lst:
            blocks.append("<i>пусто</i>")
            continue
        for i, entry in enumerate(lst, 1):
            name, desc = parse_item_entry(entry)
            if not desc:
                lib = enrich_item({"name": name, "category": cat}) or {}
                desc = (lib.get("description") or "").strip() or None
            blocks.append(f"{i}. {esc(name)}")
            if desc:
                short = desc if len(desc) <= 1000 else (desc[:1000] + "…")
                blocks.append(f"<i>{esc(short)}</i>")

    joined = "\n".join(blocks)
    for i in range(0, len(joined), 3900):
        await update.message.reply_text(joined[i:i+3900], parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

    await go_home(update, context, "Инвентарь обновлён.")


async def show_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["⚔ Оружие", "🛡 Доспехи"],
        ["🧳 Снаряжение", "🧰 Инструменты"],
        ["📚 Наборы снаряжения", "👕 Одежда"],
        ["✨ Магический предмет"],
        ["📜 Весь инвентарь", "🔙 Назад"],
    ]
    await update.message.reply_text("📦 Выбери категорию для просмотра:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return STATE_INVENTORY_CATEGORY

async def show_inventory_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if "назад" in cat.lower():
        return await end_and_main_menu(update, context)


    uid = update.effective_user.id
    inv = get_inventory(uid)

    if "Весь инвентарь" in cat:
        all_items = [f"[{c}] {i}" for c, lst in inv.items() for i in lst if lst]
        if not all_items:
            await update.message.reply_text("📭 Инвентарь пуст.")
            return STATE_INVENTORY_CATEGORY
        await update.message.reply_text("🧾 Весь инвентарь:\n\n" + "\n".join(all_items))
        return STATE_INVENTORY_CATEGORY

    cat_clean = cat
    for prefix in ["⚔ ", "🛡 ", "🧳 ", "🧰 ", "📚 ", "👕 ", "✨ ", "📜 "]:
        cat_clean = cat_clean.replace(prefix, "")
    cat_clean = cat_clean.strip()

    items = inv.get(cat_clean, [])
    if not items:
        await update.message.reply_text(f"📭 В категории {cat_clean} нет предметов.")
        return STATE_INVENTORY_CATEGORY

    context.user_data["inv_cat"] = cat_clean
    context.user_data["inv_page"] = 0
    context.user_data["inv_items"] = items
    await send_inventory_page(update, context)

async def send_inventory_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data["inv_cat"]
    page = context.user_data.get("inv_page", 0)
    items = context.user_data["inv_items"]

    per_page = 10
    start, end = page * per_page, page * per_page + per_page
    page_items = items[start:end]

    buttons = []
    for i, entry in enumerate(page_items, start=start + 1):
        name, _ = parse_item_entry(entry)
        buttons.append([InlineKeyboardButton(f"{i}. {name[:40]}", callback_data=f"inv_{i-1}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="inv_prev"))
    if end < len(items):
        nav.append(InlineKeyboardButton("➡️", callback_data="inv_next"))
    if nav:
        buttons.append(nav)

    markup = InlineKeyboardMarkup(buttons)
    text = f"{cat} — страница {page+1}/{max(1,(len(items)-1)//per_page+1)}\nВыбери предмет для просмотра:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=markup)

async def on_inventory_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "inv_prev":
        context.user_data["inv_page"] -= 1
    elif q.data == "inv_next":
        context.user_data["inv_page"] += 1
    elif q.data == "inv_exit":
        await show_inventory_menu(update, context)
        return
    await send_inventory_page(update, context)

async def on_inventory_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    items = context.user_data["inv_items"]
    idx = int(q.data.replace("inv_", ""))
    if idx < 0 or idx >= len(items):
        await q.answer("Ошибка!")
        return

    entry = items[idx]
    cat = context.user_data["inv_cat"]
    name, user_desc = parse_item_entry(entry)

    full = enrich_item({"name": name, "category": cat}) or {"name": name, "category": cat}
    if user_desc and not full.get("description"):
        full["description"] = user_desc

    card = render_item_card(full)  # ← ЭТОГО НЕ ХВАТАЛО

    await q.message.reply_text(
        card,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
    return await return_after_inline(update, context)



# --------- Удаление ---------
def get_category_keyboard():
    cats = ["Одежда", "Снаряжение", "Наборы снаряжения",
            "Инструменты", "Доспехи", "Оружие", "Магический предмет"]
    rows = [[c] for c in cats] + [["🔙 Назад"]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

async def remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери категорию для удаления:", reply_markup=get_category_keyboard())
    return STATE_REMOVE_CATEGORY

async def show_remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if "назад" in cat.lower():
        return await end_and_main_menu(update, context)

    valid_cats = list(ITEMS.keys())
    if cat.capitalize() not in valid_cats:
        await update.message.reply_text("❌ Такой категории нет. Попробуй ещё раз.")
        return STATE_REMOVE_CATEGORY

    uid = update.effective_user.id
    inv = get_inventory(uid)
    items = inv.get(cat.capitalize(), [])
    if not items:
        await update.message.reply_text(f"📭 В категории {cat} ничего нет. Выбери другую:", reply_markup=get_category_keyboard())
        return STATE_REMOVE_CATEGORY

    context.user_data["remove_cat"] = cat.capitalize()
    context.user_data["page"] = 0
    context.user_data["items"] = items
    await send_remove_page(update, context)

async def send_remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data["remove_cat"]
    page = context.user_data.get("page", 0)
    items = context.user_data["items"]

    per_page = 10
    start = page * per_page
    end = start + per_page
    page_items = items[start:end]

    buttons = []
    for i, entry in enumerate(page_items, start=start + 1):
        name, _ = parse_item_entry(entry)
        buttons.append([InlineKeyboardButton(f"{i}. {name[:35]}", callback_data=f"rm_{i-1}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data="pg_prev"))
    if end < len(items):
        nav.append(InlineKeyboardButton("➡️", callback_data="pg_next"))
    if nav:
        buttons.append(nav)
    # добавь после блока nav
    if not nav:
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="pg_exit")])

    markup = InlineKeyboardMarkup(buttons)
    text = f"🗑️ *{cat}* — страница {page+1}/{(len(items)-1)//per_page+1}\nВыбери предмет для удаления:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")

async def on_remove_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "pg_prev":
        context.user_data["page"] -= 1
    elif q.data == "pg_next":
        context.user_data["page"] += 1
    elif q.data == "pg_exit":
        await q.edit_message_text("↩️ Возврат в главное меню.")
        context.user_data.clear()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Главное меню:",
            reply_markup=home_kb(update, context)
        )
        return ConversationHandler.END


    await send_remove_page(update, context)

async def on_remove_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cat = context.user_data["remove_cat"]
    items = context.user_data["items"]
    idx = int(q.data.replace("rm_", ""))
    if idx < 0 or idx >= len(items):
        await q.answer("Ошибка!")
        return

    uid = update.effective_user.id
    inv = get_inventory(uid)
    item = items[idx]
    inv[cat].remove(item)
    save_inventory(uid, inv)

    await notify_master(context.bot, update.effective_user.first_name, f"удалил предмет: [{cat}] {item}")

    await q.edit_message_text(f"❌ Удалено: [{cat}] {item}")

    # --- фикс: запоминаем роль до очистки ---
    uid = update.effective_user.id
    is_master = uid == MASTER_ID
    is_controlling = bool(context.user_data.get("target_id"))

    # очищаем контекст, чтобы не залипало
    context.user_data.clear()

    # --- возвращаем нужное меню ---
    chat_id = update.effective_chat.id

    if is_master:
        if is_controlling:
            # мастер управляет игроком — возвращаем меню управления
            await context.bot.send_message(
                chat_id=chat_id,
                text="↩️ Возврат в меню управления игроком.",
                reply_markup=home_kb(update, context)
            )
        else:
            # мастер в своём меню
            await context.bot.send_message(
                chat_id=chat_id,
                text="↩️ Возврат в мастер-инвентарь.",
                reply_markup=_kb_master_root()
            )
    else:
        # игрок — его обычное меню
        await context.bot.send_message(
            chat_id=chat_id,
            text="↩️ Возврат в главное меню.",
            reply_markup=home_kb(update, context)
        )

    return await return_after_inline(update, context)




# --------- Симуляция ---------
async def ask_simulation_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["1", "3", "5"],
        ["7", "10", "📝 Другое"],
        ["🔙 Назад"],
    ]
    await update.message.reply_text("⏳ На сколько дней симулировать приключение?",
                                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return STATE_SIMULATE_DAYS

async def handle_simulation_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if "назад" in text.lower():
        return await end_and_main_menu(update, context)

    if text == "📝 Другое":
        await update.message.reply_text("Введите количество дней числом (например: 12):")
        return STATE_SIMULATE_DAYS

    try:
        days = int(text)
        context.args = [str(days)]
        await simulate_days(update, context)
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("⚠️ Введите число, пожалуйста.")
        return STATE_SIMULATE_DAYS

async def simulate_days(update, context):
    uid = update.effective_user.id
    inv = get_inventory(uid)
    if not context.args:
        await update.message.reply_text("Используй: /simulate <число>")
        return
    days = max(1, int(context.args[0]))
    out = []
    for d in range(1, days + 1):
        lost_cat, lost_entry, r1 = _lose_item(inv)
        found_cat, found_entry, r2 = _find_item(inv)

        ln, _ = parse_item_entry(lost_entry)
        fn, _ = parse_item_entry(found_entry)

        lost_full  = enrich_item({"name": ln, "category": lost_cat})  if ln else None
        found_full = enrich_item({"name": fn, "category": found_cat}) if fn else None

        out.append(
            f"\n📅 *День {d}:*\n"
            f"  Потерял ({r1}) [{lost_cat}] — {(lost_full or {'name': ln}).get('name')}\n"
            f"  {(lost_full or {}).get('description','')}\n"
            f"  Нашёл  ({r2}) [{found_cat}] — {(found_full or {'name': fn}).get('name')}\n"
            f"  {(found_full or {}).get('description','')}"
        )

    save_inventory(uid, inv)
    await update.message.reply_text("\n".join(out), parse_mode=constants.ParseMode.MARKDOWN)
    await go_home(update, context, "🏁 Симуляция завершена! Что делаем дальше?")

async def return_after_inline(update: Update, context: ContextTypes.DEFAULT_TYPE, text="↩️ Возврат в главное меню."):
    """Корректно возвращает меню после inline callback — учитывает, кто нажал кнопку."""
    q = update.callback_query
    chat_id = q.message.chat_id
    uid = update.effective_user.id

    # определяем роль
    is_master = uid == MASTER_ID
    is_controlling = bool(context.user_data.get("target_id"))

    # очищаем временные поля, чтобы не залипали состояния
    for k in ("inv_cat","inv_page","inv_items","remove_cat","page","items","add_cat","pending_item","pending_desc","raw_name","pending"):
        context.user_data.pop(k, None)

    if is_master:
        if is_controlling:
            kb = home_kb(update, context)
            msg = "↩️ Возврат в меню управления игроком."
        else:
            kb = _kb_master_root()
            msg = "↩️ Возврат в мастер-инвентарь."
    else:
        kb = home_kb(update, context)
        msg = text

    await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=kb)
    return ConversationHandler.END


# --------- Добавление предметов ---------
def norm(s): 
    return (s or "").strip().lower()

def find_closest_item(name: str, category: str | None = None):
    query = norm(name)
    cat = norm(category or "")

    # базовый пул
    if "маг" in cat:
        base = MAGIC
    else:
        base = NONMAGIC

    # сузим пул по категории; если вдруг пусто — вернёмся к базовому
    pool = [i for i in base if norm(i.get("category")) == cat] or base

    names = [norm(i.get("name")) for i in pool if i.get("name")]
    best = process.extractOne(query, names, scorer=fuzz.WRatio)
    if not best: 
        return None

    best_name, score, _ = best
    if score < 60:
        return None

    for it in pool:
        if norm(it.get("name")) == best_name:
            return it
    return None


async def add_item_start(update, context):
    if update.effective_user.id == MASTER_ID and "target_id" not in context.user_data:
        await update.message.reply_text("⚠️ Сначала выбери игрока в «Мастер-инвентарь».", reply_markup=home_kb(update, context))
        return ConversationHandler.END

    keyboard = [
        ["Одежда", "Снаряжение"],
        ["Наборы снаряжения", "Инструменты"],
        ["Доспехи", "Оружие"],
        ["Магический предмет"],
        ["🔙 Назад"],
    ]
    await update.message.reply_text("Выбери категорию:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return STATE_ADD_CATEGORY

async def add_item_category(update, context):
    cat = update.message.text.strip()
    if cat.lower() == "назад" or cat == "🔙 Назад":
        await go_home(update, context)
        return ConversationHandler.END

    if cat not in ITEMS:
        await update.message.reply_text("❌ Такой категории нет. Попробуй ещё раз.", reply_markup=get_category_keyboard())
        return STATE_ADD_CATEGORY

    context.user_data["add_cat"] = cat
    await update.message.reply_text(
        f"Введи название предмета для категории [{cat}]:\n"
        f"Можно добавить описание через двоеточие, например:\n"
        f"`Языки пламени: меч с огненным клинком`",
        parse_mode=constants.ParseMode.MARKDOWN
    )
    return STATE_ADD_NAME

async def add_item_name(update, context):
    uid = context.user_data.get("target_id", update.effective_user.id)
    inv = get_inventory(uid)
    cat = context.user_data.get("add_cat")

    raw_text = (update.message.text or "").strip()
    context.user_data["raw_name"] = raw_text
    if ":" in raw_text:
        name, user_desc = [x.strip() for x in raw_text.split(":", 1)]
    else:
        name, user_desc = raw_text, None

    # ищем только в подходящей библиотеке
    closest = find_closest_item(name, cat)
    if closest:
        found_name = closest["name"]
        context.user_data["pending"] = {"uid": uid, "cat": cat, "name": found_name, "desc": user_desc}
        found_item = enrich_item({"name": found_name, "category": cat}) or {}
        short = re.sub(r"\s+", " ", (found_item.get("description") or "— нет описания —")).strip()
        if len(short) > 350: short = short[:350] + "…"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да", callback_data="confirm_yes"),
                                    InlineKeyboardButton("❌ Нет", callback_data="confirm_no")]])
        await update.message.reply_text(
            f"🤔 Похоже, вы имели в виду *{found_name}*?\n\n{short}",
            parse_mode=constants.ParseMode.MARKDOWN,
            disable_web_page_preview=True,
            reply_markup=kb,
        )
        return STATE_ADD_CONFIRM

    # кастом
    inv[cat].append(make_custom_string(name, user_desc))
    save_inventory(uid, inv)
    card = render_item_card({"name": name, "description": user_desc or "— пользовательское описание —", "category": cat})
    await update.message.reply_text(
        f"⚙️ Не найдено в библиотеке. Добавлен как пользовательский предмет.\n\nДобавлено в [{cat}]:\n\n{card}",
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )
    await go_home(update, context)
    return ConversationHandler.END

async def on_add_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    pend = context.user_data.get("pending") or {}
    uid  = pend.get("uid", update.effective_user.id)
    cat  = pend.get("cat")
    found_name = pend.get("name")
    user_desc  = pend.get("desc")

    inv = get_inventory(uid)

    # ✅ подтвердили библиотечный предмет
    if data == "confirm_yes" and found_name:
        inv[cat].append(found_name)
        save_inventory(uid, inv)

        found_item = enrich_item({"name": found_name, "category": cat}) or {}
        desc = (found_item.get("description") or "— нет описания —").strip()

        await q.edit_message_text(
            f"✅ Добавлено в {cat}:\n\n*{found_name}*\n\n{desc}",
            parse_mode=constants.ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )
        return await end_and_main_menu(update, context)

    # ❌ «нет, это не он» → спросим, сохранить как кастом
    if data == "confirm_no":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да", callback_data="add_custom_yes"),
             InlineKeyboardButton("❌ Нет", callback_data="add_custom_no")]
        ])
        await q.edit_message_text("⚙️ Не найдено в библиотеке.\nДобавить как пользовательский предмет?", reply_markup=kb)
        return  # ждём следующее нажатие

    # ✅ добавить как кастом
    if data == "add_custom_yes":
        raw = context.user_data.get("raw_name", found_name or "Неизвестный предмет")
        if ":" in raw:
            base_name, desc = [x.strip() for x in raw.split(":", 1)]
        else:
            base_name, desc = raw.strip(), (user_desc or "— пользовательское описание —")

        inv[cat].append(f"⭐ {base_name} — {desc}")
        save_inventory(uid, inv)

        await q.edit_message_text(
            f"Добавлено в {cat}:\n\n*{base_name}*\n\n{desc}",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return await end_and_main_menu(update, context)

    # 🚫 отменили кастом
    if data == "add_custom_no":
        await q.edit_message_text("🚫 Добавление отменено.")
        return await end_and_main_menu(update, context)




# --------- Мастер-инвентарь ---------
async def show_master_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_ID:
        await update.message.reply_text("🚫 Нет доступа.")
        return
    keyboard = [[name] for name in PLAYERS.keys()] + [["🔙 Назад"]]
    await update.message.reply_text("🎩 Выбери игрока:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return STATE_INVENTORY_CATEGORY

async def master_select_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if "назад" in name.lower():
        await update.message.reply_text("↩️ Возврат в главное меню.", reply_markup=home_kb(update, context))
        return ConversationHandler.END


    if name not in PLAYERS:
        await update.message.reply_text("⚠️ Неизвестный игрок.")
        return STATE_INVENTORY_CATEGORY

    context.user_data["target_id"] = PLAYERS[name]
    context.user_data["target_name"] = name
    await update.message.reply_text(
        f"📦 Управляешь инвентарём игрока: *{name}*",
        parse_mode="Markdown",
        reply_markup=home_kb(update, context),
    )
    return STATE_ADD_CATEGORY


# --------- Уведомления (мягкие) ---------
async def notify_master(bot, player_name, action):
    try:
        await bot.send_message(MASTER_ID, f"🪶 Игрок {player_name} {action}")
    except Exception:
        pass

async def notify_player(bot, player_id, action):
    try:
        await bot.send_message(player_id, f"📜 Мастер изменил ваш инвентарь: {action}")
    except Exception:
        pass


# --------- Бэкап в GitHub ---------
async def backup_inventory_to_github():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        subprocess.run(["git", "config", "--global", "user.email", os.getenv("GITHUB_EMAIL")], check=True)
        subprocess.run(["git", "config", "--global", "user.name", os.getenv("GITHUB_NAME")], check=True)
        subprocess.run(["git", "add", "inventory_data.json"], check=True)
        subprocess.run(["git", "commit", "-m", f"auto backup {ts}"], check=False)
        subprocess.run(["git", "push", f"https://{os.getenv('GITHUB_TOKEN')}@github.com/{os.getenv('GITHUB_REPO')}.git", "HEAD:main"], check=False)
        print(f"✅ GitHub backup done at {ts}")
    except Exception as e:
        print(f"⚠️ Backup error: {e}")


# --------- Запуск ---------
async def run_bot():
    # загрузка каталогов
    global MAGIC, NONMAGIC
    MAGIC, NONMAGIC = init_catalogs(str(DATA_DIR))

    app = ApplicationBuilder().token(TOKEN).build()

    # разговорники
    remove_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Удалить предмет$"), remove_item)],
        states={ STATE_REMOVE_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_remove_page)] },
        fallbacks=[MessageHandler(filters.Regex(BACK_RE), on_any_back)],
    )

    inventory_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Инвентарь$"), show_inventory_menu)],
        states={ STATE_INVENTORY_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, show_inventory_list)] },
        fallbacks=[MessageHandler(filters.Regex(BACK_RE), on_any_back)],
    )

    simulate_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎲 Симулировать день$"), ask_simulation_days)],
        states={ STATE_SIMULATE_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_simulation_days)] },
        fallbacks=[MessageHandler(filters.Regex(BACK_RE), on_any_back)],
    )

    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить предмет$"), add_item_start)],
        states={
            STATE_ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_category)],
            STATE_ADD_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_name)],
            STATE_ADD_CONFIRM:  [CallbackQueryHandler(on_add_confirm_button, pattern="^(confirm_|add_custom_)")],
        },
        fallbacks=[MessageHandler(filters.Regex(BACK_RE), on_any_back)],
    )
      # регистрация
    app.add_handler(inventory_conv)
    app.add_handler(remove_conv)
    app.add_handler(simulate_conv)
    app.add_handler(add_conv)
    app.add_handler(MessageHandler(filters.Regex(BACK_RE), on_any_back))
    app.add_handler(CallbackQueryHandler(on_inventory_nav,  pattern="^inv_(prev|next|exit)$"))
    app.add_handler(CallbackQueryHandler(on_inventory_item, pattern="^inv_[0-9]+$"))
    app.add_handler(CallbackQueryHandler(on_remove_click,   pattern="^rm_"))
    app.add_handler(CallbackQueryHandler(on_remove_nav,     pattern="^pg_"))

    app.add_handler(MessageHandler(filters.Regex("^📜 Мастер-инвентарь$"), show_master_inventory))
    app.add_handler(MessageHandler(filters.Regex("^(Карла|Энсо|Найт|Гундар|Авитус|🔙 Назад)$"), master_select_player))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("inventory", show_inventory))
    app.add_handler(CommandHandler("simulate", simulate_days))  # по желанию

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(backup_inventory_to_github, "interval", hours=24)
    scheduler.start()

    print("✅ Бот запущен!")
    await app.run_polling()


if __name__ == "__main__":
    import nest_asyncio
    nest_asyncio.apply()
    asyncio.run(run_bot())

