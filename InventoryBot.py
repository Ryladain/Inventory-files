import os, json, random, re, asyncio, subprocess, datetime, html
from pathlib import Path
from dotenv import load_dotenv
from rapidfuzz import fuzz, process

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, ReplyKeyboardRemove, constants
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, ContextTypes, filters
)

# === библиотека описаний ===
from item_catalog import init_catalogs, enrich_item, render_item_card, MAGIC, NONMAGIC

# ──────────────────────────────────────────────────────────────────────────────
# Конфиг/данные
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).with_name('.env'), override=True)
TOKEN = os.getenv("BOT_TOKEN")
DATA_FILE = Path("inventory_data.json")
DATA_DIR = (Path(__file__).parent / "data").resolve()

# Категории (d20)
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
    "Одежда": ["комплект путешественника","комплект простолюдина","комплект знатного","комплект мага"],
    "Снаряжение": ["факел","верёвка (15 м)","рюкзак","бутылка воды","спальник","фляга","мешочек","фляга масла","зеркальце"],
    "Наборы снаряжения": ["набор путешественника","набор священника","набор вора","набор исследователя подземелий"],
    "Инструменты": ["инструменты кузнеца","инструменты вора","инструменты художника","музыкальный инструмент (лютня)"],
    "Доспехи": ["кожаный доспех","кольчужная рубаха","латы","щит"],
    "Оружие": ["кинжал","короткий меч","длинный меч","лук","топор","посох"],
    "Магический предмет": ["зелье лечения","меч +1","кольцо защиты","плащ защиты","жезл молний","мешок хранения"],
}

RARITY_TABLE = [
    (30, "обычный"),
    (66, "необычный"),
    (81, "редкий"),
    (96, "значимый необычный"),
    (98, "очень редкий"),
    (100,"значимый редкий"),
]

# Состояния разговоров
STATE_REMOVE_CATEGORY   = 20
STATE_SIMULATE_DAYS     = 30
STATE_INVENTORY_CATEGORY= 40
STATE_ADD_CATEGORY      = 10
STATE_ADD_NAME          = 11
STATE_ADD_CONFIRM       = 12

# ──────────────────────────────────────────────────────────────────────────────
# Хранилище
# ──────────────────────────────────────────────────────────────────────────────
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

# ──────────────────────────────────────────────────────────────────────────────
# Утилиты выпадений
# ──────────────────────────────────────────────────────────────────────────────
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

        pool = [i for i in MAGIC if i.get("rarity")==base_rarity and i.get("tier")==tier]
        if pool:
            chosen = random.choice(pool)
            found = chosen["name"]
            info = enrich_item({"name": found, "category": cat})
            desc = (info or {}).get("description","")
            if desc:
                found += f" — {desc[:600].strip()}…"
        else:
            found = f"Не найдено ({base_rarity}, {tier})"

        found = f"{found} ({rarity_label}, d100={r100})"

    inv[cat].append(found)
    return cat, found, r

# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательное: кастомные строки
# ──────────────────────────────────────────────────────────────────────────────
def split_custom(entry):
    """Вернёт (name, desc|None) для записи-строки или словаря."""
    if isinstance(entry, dict):
        name = (entry.get("name") or "").strip().lstrip("⭐").strip()
        desc = (entry.get("description") or "").strip() or None
        return name, desc
    s = str(entry).strip()
    if " — " in s:
        n, d = s.split(" — ", 1)
        return n.strip().lstrip("⭐").strip(), (d.strip() or None)
    if "—" in s:
        n, d = s.split("—", 1)
        return n.strip().lstrip("⭐").strip(), (d.strip() or None)
    return s.lstrip("⭐").strip(), None

# ──────────────────────────────────────────────────────────────────────────────
# Роли и клавиатуры
# ──────────────────────────────────────────────────────────────────────────────
MASTER_ID = 1840976992  # замени на своего мастера

PLAYERS = {
    "Карла":   111111111,
    "Энсо":    558026215,
    "Найт":    1615374911,
    "Гундар":  6141258332,
    "Авитус":  868719266,
}

PLAYER_WITH_SIMULATION = "Найт"

def default_keyboard(user_id=None):
    if user_id == MASTER_ID:
        return ReplyKeyboardMarkup([["📜 Мастер-инвентарь"]], resize_keyboard=True)
    for name, pid in PLAYERS.items():
        if user_id == pid:
            base = [
                ["➕ Добавить предмет", "➖ Удалить предмет"],
                ["📦 Инвентарь"],
                ["📚 Категории"],
            ]
            if name == PLAYER_WITH_SIMULATION:
                base[1].append("🎲 Симулировать день")
            return ReplyKeyboardMarkup(base, resize_keyboard=True)
    return ReplyKeyboardMarkup([["📚 Категории"]], resize_keyboard=True)

def keyboard_for(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == MASTER_ID and context.user_data.get("target_id"):
        target_name = context.user_data.get("target_name","")
        kb = [
            ["➕ Добавить предмет", "➖ Удалить предмет"],
            ["📦 Инвентарь"],
            ["📚 Категории"],
            ["🔙 Назад"],
        ]
        if target_name == PLAYER_WITH_SIMULATION:
            kb[1].append("🎲 Симулировать день")
        return ReplyKeyboardMarkup(kb, resize_keyboard=True)
    return default_keyboard(uid)

def get_category_keyboard():
    rows = [[c] for c in ["Одежда","Снаряжение","Наборы снаряжения","Инструменты","Доспехи","Оружие","Магический предмет"]]
    rows.append(["🔙 Назад"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

# ──────────────────────────────────────────────────────────────────────────────
# Команды
# ──────────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 🧙‍♂️ Я D&D инвентарь-бот.\nВыбери действие из меню ниже:",
        reply_markup=default_keyboard(update.effective_user.id)
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/inventory, /add, /remove, /simulate, /categories")

async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    order = ["Одежда","Снаряжение","Наборы снаряжения","Инструменты","Доспехи","Оружие","Магический предмет"]
    await update.message.reply_text("📚 Категории:\n" + "\n".join(f"• {c}" for c in order))

# ──────────────────────────────────────────────────────────────────────────────
# Показ инвентаря (HTML, с описаниями)
# ──────────────────────────────────────────────────────────────────────────────
async def show_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    inv = get_inventory(uid)

    blocks = ["<b>🎒 Инвентарь:</b>"]
    for cat, lst in inv.items():
        blocks.append(f"<b>{html.escape(cat)}:</b>")
        if not lst:
            blocks.append("<i>пусто</i>")
            continue
        for i, entry in enumerate(lst, 1):
            name, desc = split_custom(entry)
            if not desc:
                lib = enrich_item({"name": name, "category": cat}) or {}
                desc = (lib.get("description") or "").strip() or None
            blocks.append(f"{i}. {html.escape(name)}")
            if desc:
                short = desc if len(desc)<=1000 else (desc[:1000]+"…")
                blocks.append(f"<i>{html.escape(short)}</i>")

    joined = "\n".join(blocks)
    for i in range(0, len(joined), 3900):
        await update.message.reply_text(joined[i:i+3900], parse_mode=constants.ParseMode.HTML)

    await update.message.reply_text("Готово.", reply_markup=keyboard_for(update, context))

# ──────────────────────────────────────────────────────────────────────────────
# Добавление предмета
# ──────────────────────────────────────────────────────────────────────────────
def normalize_text(s: str) -> str:
    return (s or "").strip().lower()

def find_closest_item(name: str, category: str | None = None):
    query = normalize_text(name)
    if category and "маг" in category.lower():
        base = MAGIC
    else:
        base = NONMAGIC

    # ограничиваем библиотеку выбранной категорией, если в данных есть такое поле
    search_space = [i for i in base if normalize_text(i.get("category")) == normalize_text(category)] or base
    names = [normalize_text(i.get("name")) for i in search_space if i.get("name")]

    best = process.extractOne(query, names, scorer=fuzz.WRatio)
    if not best:
        return None
    best_name, score, _ = best
    if score < 60:
        return None
    for it in search_space:
        if normalize_text(it.get("name")) == best_name:
            return it
    return None

async def add_item_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # мастер без выбранного игрока — назад
    if update.effective_user.id == MASTER_ID and "target_id" not in context.user_data:
        await update.message.reply_text("⚠️ Сначала выбери игрока в «Мастер-инвентаре».",
                                        reply_markup=default_keyboard(MASTER_ID))
        return ConversationHandler.END

    kb = [
        ["Одежда","Снаряжение"],
        ["Наборы снаряжения","Инструменты"],
        ["Доспехи","Оружие"],
        ["Магический предмет"],
        ["🔙 Назад"]
    ]
    text = "Выбери категорию:"
    if update.effective_user.id == MASTER_ID:
        tname = context.user_data.get("target_name","неизвестный игрок")
        text = f"📜 Добавление предмета в инвентарь игрока *{tname}*.\nВыбери категорию:"
    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return STATE_ADD_CATEGORY

async def add_item_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if cat.lower() == "назад" or cat == "🔙 Назад":
        await update.message.reply_text("↩️ Возврат в главное меню.", reply_markup=keyboard_for(update, context))
        return ConversationHandler.END
    if cat not in ITEMS:
        await update.message.reply_text("❌ Такой категории нет. Попробуй ещё раз.", reply_markup=get_category_keyboard())
        return STATE_ADD_CATEGORY

    context.user_data["add_cat"] = cat
    await update.message.reply_text(
        f"Введи название предмета для категории [{cat}].\n"
        f"Можно добавить описание через двоеточие, например:\n"
        f"`Гранёный кристалл: сияет в темноте`",
        parse_mode=constants.ParseMode.MARKDOWN
    )
    return STATE_ADD_NAME

async def add_item_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = context.user_data.get("target_id", update.effective_user.id)
    inv = get_inventory(uid)
    cat = context.user_data.get("add_cat")

    raw = (update.message.text or "").strip()
    context.user_data["raw_name"] = raw
    if ":" in raw:
        name, user_desc = [x.strip() for x in raw.split(":", 1)]
    else:
        name, user_desc = raw, None

    found_lib = enrich_item({"name": name, "category": cat}) or {}
    closest = find_closest_item(name, cat) if not found_lib else None

    if closest:
        found_name = closest["name"]
        context.user_data["pending_item"] = (cat, found_name)
        context.user_data["pending_desc"] = user_desc

        found_item = enrich_item({"name": found_name, "category": cat}) or {}
        desc = (found_item.get("description") or found_item.get("desc") or "— нет описания —").strip()
        short = re.sub(r"\s+"," ", desc)
        if len(short) > 350: short = short[:350].rstrip()+"…"

        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да", callback_data="confirm_yes"),
                                    InlineKeyboardButton("❌ Нет", callback_data="confirm_no")]])
        await update.message.reply_text(
            f"🤔 Похоже, вы имели в виду *{found_name}*?\n\n{short}",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=kb
        )
        return STATE_ADD_CONFIRM

    # кастом
    saved = f"⭐ {name} — {(user_desc or '— пользовательское описание —')}"
    inv[cat].append(saved)
    save_inventory(uid, inv)

    await notify_master(context.bot, update.effective_user.first_name, f"добавил предмет: [{cat}] {name}")

    card = render_item_card({"name": name, "description": user_desc or "— пользовательское описание —"})
    await update.message.reply_text(
        f"⚙️ Не найдено в библиотеке. Добавлен как пользовательский предмет.\n\nДобавлено в [{cat}]:\n\n{card}",
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True,
        reply_markup=keyboard_for(update, context)
    )
    return ConversationHandler.END

async def on_add_confirm_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    uid = context.user_data.get("target_id", update.effective_user.id)
    inv = get_inventory(uid)
    data = q.data
    cat, found_name = context.user_data.get("pending_item", (None, None))
    user_desc = context.user_data.get("pending_desc")

    if data == "confirm_yes" and found_name:
        inv[cat].append(found_name)
        save_inventory(uid, inv)
        fi = enrich_item({"name": found_name, "category": cat}) or {}
        desc = (fi.get("description") or fi.get("desc") or "— нет описания —").strip()
        await q.edit_message_text(f"✅ Добавлено в {cat}:\n\n*{found_name}*\n\n{desc}",
                                  parse_mode=constants.ParseMode.MARKDOWN)
        await context.bot.send_message(q.message.chat_id, "↩️ Возврат в главное меню.",
                                       reply_markup=keyboard_for(update, context))
        return ConversationHandler.END

    if data == "confirm_no":
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да", callback_data="add_custom_yes"),
                                    InlineKeyboardButton("❌ Нет", callback_data="add_custom_no")]])
        await q.edit_message_text("⚙️ Не найдено в библиотеке.\nДобавить как пользовательский предмет?",
                                  reply_markup=kb)
        return

    if data == "add_custom_yes":
        raw = context.user_data.get("raw_name","Неизвестный предмет")
        if ":" in raw: name, desc = [x.strip() for x in raw.split(":",1)]
        else: name, desc = raw, (user_desc or "— пользовательское описание —")
        inv[cat].append(f"⭐ {name} — {desc}")
        save_inventory(uid, inv)

        await q.edit_message_text(f"Добавлено в {cat}:\n\n*{name}*\n\n{desc}", parse_mode=constants.ParseMode.MARKDOWN)
        await context.bot.send_message(q.message.chat_id, "↩️ Возврат в главное меню.",
                                       reply_markup=keyboard_for(update, context))
        return ConversationHandler.END

    if data == "add_custom_no":
        await q.edit_message_text("🚫 Добавление отменено.")
        await context.bot.send_message(q.message.chat_id, "↩️ Возврат в главное меню.",
                                       reply_markup=keyboard_for(update, context))
        return ConversationHandler.END

async def add_item_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Добавление отменено.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────────────────────────
# Удаление предмета (весь поток — один ConversationHandler!)
# ──────────────────────────────────────────────────────────────────────────────
async def remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выбери категорию для удаления:", reply_markup=get_category_keyboard())
    return STATE_REMOVE_CATEGORY

async def show_remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if "назад" in cat.lower():
        await update.message.reply_text("↩️ Возврат в главное меню.", reply_markup=keyboard_for(update, context))
        return ConversationHandler.END

    valid = ["Одежда","Снаряжение","Наборы снаряжения","Инструменты","Доспехи","Оружие","Магический предмет"]
    if cat.capitalize() not in valid:
        await update.message.reply_text("❌ Такой категории нет. Попробуй ещё раз.")
        return STATE_REMOVE_CATEGORY

    uid = context.effective_user.id
    inv = get_inventory(uid)
    items = inv.get(cat.capitalize(), [])
    if not items:
        await update.message.reply_text(f"📭 В категории {cat} ничего нет. Выбери другую:",
                                        reply_markup=get_category_keyboard())
        return STATE_REMOVE_CATEGORY

    context.user_data["remove_cat"] = cat.capitalize()
    context.user_data["page"] = 0
    context.user_data["items"] = items
    await send_remove_page(update, context)
    return STATE_REMOVE_CATEGORY

async def send_remove_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data["remove_cat"]
    page = context.user_data.get("page",0)
    items = context.user_data["items"]

    per = 10
    start, end = page*per, page*per+per
    page_items = items[start:end]

    buttons = []
    for i, entry in enumerate(page_items, start=start+1):
        name, _ = split_custom(entry)
        buttons.append([InlineKeyboardButton(f"{i}. {name[:35]}", callback_data=f"rm_{i-1}")])

    nav = []
    if page>0: nav.append(InlineKeyboardButton("⬅️", callback_data="pg_prev"))
    if end < len(items): nav.append(InlineKeyboardButton("➡️", callback_data="pg_next"))
    if nav: buttons.append(nav)

    markup = InlineKeyboardMarkup(buttons)
    text = f"🗑️ *{cat}* — страница {page+1}/{max(1,(len(items)-1)//per+1)}\nВыбери предмет для удаления:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")

async def on_remove_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "pg_prev": context.user_data["page"] -= 1
    elif q.data == "pg_next": context.user_data["page"] += 1
    await send_remove_page(update, context)
    return STATE_REMOVE_CATEGORY

async def on_remove_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cat = context.user_data["remove_cat"]
    items = context.user_data["items"]
    idx = int(q.data.replace("rm_", ""))
    if idx < 0 or idx >= len(items):
        await q.answer("Ошибка!")
        return STATE_REMOVE_CATEGORY

    uid = update.effective_user.id
    inv = get_inventory(uid)
    item = items[idx]
    inv[cat].remove(item)
    save_inventory(uid, inv)

    await notify_master(context.bot, update.effective_user.first_name, f"удалил предмет: [{cat}] {item}")
    await q.edit_message_text(f"❌ Удалено: [{cat}] {item}")

    # после удаления ВСЕГДА выходим в основное меню (чтобы не залипало состояние)
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text="↩️ Возврат в главное меню.",
                                   reply_markup=keyboard_for(update, context))
    return ConversationHandler.END

async def on_remove_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❎ Отменено.", reply_markup=keyboard_for(update, context))
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────────────────────────
# Просмотр инвентаря c пагинацией (один ConversationHandler)
# ──────────────────────────────────────────────────────────────────────────────
async def show_inventory_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [
        ["⚔ Оружие","🛡 Доспехи"],
        ["🧳 Снаряжение","🧰 Инструменты"],
        ["📚 Наборы снаряжения","👕 Одежда"],
        ["✨ Магический предмет"],
        ["📜 Весь инвентарь","🔙 Назад"]
    ]
    await update.message.reply_text("📦 Выбери категорию для просмотра:",
                                    reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return STATE_INVENTORY_CATEGORY

async def show_inventory_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text.strip()
    if cat == "🔙 Назад":
        await update.message.reply_text("↩️ Возврат в главное меню.", reply_markup=keyboard_for(update, context))
        return ConversationHandler.END

    uid = update.effective_user.id
    inv = get_inventory(uid)

    if "Весь инвентарь" in cat:
        all_items = [f"[{c}] {i}" for c, lst in inv.items() for i in lst if lst]
        if not all_items:
            await update.message.reply_text("📭 Инвентарь пуст.")
            return STATE_INVENTORY_CATEGORY
        await update.message.reply_text("🧾 Весь инвентарь:\n\n" + "\n".join(all_items))
        return STATE_INVENTORY_CATEGORY

    # убираем эмодзи
    clean = cat
    for p in ["⚔ ","🛡 ","🧳 ","🧰 ","📚 ","👕 ","✨ ","📜 "]:
        clean = clean.replace(p,"")
    clean = clean.strip()

    items = inv.get(clean, [])
    if not items:
        await update.message.reply_text(f"📭 В категории {clean} нет предметов.")
        return STATE_INVENTORY_CATEGORY

    context.user_data["inv_cat"] = clean
    context.user_data["inv_page"] = 0
    context.user_data["inv_items"] = items
    await send_inventory_page(update, context)
    return STATE_INVENTORY_CATEGORY

async def send_inventory_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = context.user_data["inv_cat"]
    page = context.user_data.get("inv_page",0)
    items= context.user_data["inv_items"]

    per = 10
    start,end = page*per, page*per+per
    page_items = items[start:end]

    buttons=[]
    for i, entry in enumerate(page_items, start=start+1):
        name, _ = split_custom(entry)
        buttons.append([InlineKeyboardButton(f"{i}. {name[:40]}", callback_data=f"inv_{i-1}")])

    nav=[]
    if page>0: nav.append(InlineKeyboardButton("⬅️", callback_data="inv_prev"))
    if end < len(items): nav.append(InlineKeyboardButton("➡️", callback_data="inv_next"))
    if nav: buttons.append(nav)

    markup = InlineKeyboardMarkup(buttons)
    text = f"{cat} — страница {page+1}/{max(1,(len(items)-1)//per+1)}\nВыбери предмет для просмотра:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=markup)

async def on_inventory_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "inv_prev": context.user_data["inv_page"] -= 1
    elif q.data == "inv_next": context.user_data["inv_page"] += 1
    await send_inventory_page(update, context)
    return STATE_INVENTORY_CATEGORY

async def on_inventory_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    items = context.user_data["inv_items"]
    idx   = int(q.data.replace("inv_",""))
    if idx < 0 or idx >= len(items):
        await q.answer("Ошибка!")
        return STATE_INVENTORY_CATEGORY

    item_name = items[idx]
    cat = context.user_data["inv_cat"]

    full = enrich_item({"name": item_name, "category": cat}) or {"name": item_name}
    if isinstance(item_name, str) and ("⭐" in item_name or " — " in item_name or "—" in item_name):
        base = item_name.replace("⭐","").strip()
        if " — " in base: name, desc = [x.strip() for x in base.split(" — ",1)]
        elif "—" in base: name, desc = [x.strip() for x in base.split("—",1)]
        else: name, desc = base, "— пользовательское описание —"
        full = {"name": name, "description": desc, "category": cat}

    await q.message.reply_text(render_item_card(full),
                               parse_mode=constants.ParseMode.MARKDOWN,
                               disable_web_page_preview=True)

    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text="↩️ Возврат в главное меню.",
                                   reply_markup=keyboard_for(update, context))
    return ConversationHandler.END

# ──────────────────────────────────────────────────────────────────────────────
# Симуляция
# ──────────────────────────────────────────────────────────────────────────────
async def ask_simulation_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb=[["1","3","5"],["7","10","📝 Другое"],["🔙 Назад"]]
    await update.message.reply_text("⏳ На сколько дней симулировать приключение?",
                                    reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    return STATE_SIMULATE_DAYS

async def handle_simulation_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if text == "🔙 Назад":
        await update.message.reply_text("↩️ Возврат в главное меню.", reply_markup=keyboard_for(update, context))
        return ConversationHandler.END
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

async def simulate_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    inv = get_inventory(uid)
    if not context.args:
        await update.message.reply_text("Используй: /simulate <число>")
        return
    days = max(1, int(context.args[0]))
    out=[]
    for d in range(1, days+1):
        lost_cat, lost_item, r1 = _lose_item(inv)
        found_cat, found_item, r2 = _find_item(inv)
        lost_full  = enrich_item({"name": lost_item, "category": lost_cat})
        found_full = enrich_item({"name": found_item, "category": found_cat})
        out.append(
            f"\n📅 *День {d}:*\n"
            f"  Потерял ({r1}) [{lost_cat}] — {lost_full['name']}\n"
            f"  {lost_full.get('description','')}\n"
            f"  Нашёл  ({r2}) [{found_cat}] — {found_full['name']}\n"
            f"  {found_full.get('description','')}"
        )
    save_inventory(uid, inv)
    await update.message.reply_text("\n".join(out), parse_mode=constants.ParseMode.MARKDOWN)
    await update.message.reply_text("🏁 Симуляция завершена! Что делаем дальше?",
                                    reply_markup=keyboard_for(update, context))

# ──────────────────────────────────────────────────────────────────────────────
# Мастер-инвентарь
# ──────────────────────────────────────────────────────────────────────────────
async def show_master_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MASTER_ID:
        await update.message.reply_text("🚫 У вас нет доступа к мастер-инвентарю.")
        return
    keyboard = [[name] for name in PLAYERS.keys()]
    keyboard.append(["🔙 Назад"])
    await update.message.reply_text("🎩 Выбери игрока:",
                                    reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return STATE_INVENTORY_CATEGORY

async def master_select_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if name == "🔙 Назад":
        await update.message.reply_text("↩️ Возврат в главное меню.", reply_markup=default_keyboard(MASTER_ID))
        return ConversationHandler.END
    if name not in PLAYERS:
        await update.message.reply_text("⚠️ Неизвестный игрок.")
        return STATE_INVENTORY_CATEGORY

    context.user_data["target_id"]   = PLAYERS[name]
    context.user_data["target_name"] = name

    kb = [
        ["➕ Добавить предмет", "➖ Удалить предмет"],
        ["📦 Инвентарь"],
        ["📚 Категории"],
        ["🔙 Назад"]
    ]
    if name == PLAYER_WITH_SIMULATION:
        kb[1].append("🎲 Симулировать день")

    await update.message.reply_text(
        f"📦 Управляешь инвентарём игрока *{name}*",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )
    return STATE_ADD_CATEGORY

# ──────────────────────────────────────────────────────────────────────────────
# Уведомления/бэкап
# ──────────────────────────────────────────────────────────────────────────────
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

async def backup_inventory_to_github():
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        subprocess.run(["git","config","--global","user.email", os.getenv("GITHUB_EMAIL")], check=True)
        subprocess.run(["git","config","--global","user.name",  os.getenv("GITHUB_NAME")], check=True)
        subprocess.run(["git","add","inventory_data.json"], check=True)
        subprocess.run(["git","commit","-m",f"auto backup {ts}"], check=False)
        subprocess.run(["git","push",f"https://{os.getenv('GITHUB_TOKEN')}@github.com/{os.getenv('GITHUB_REPO')}.git","HEAD:main"], check=False)
        print(f"✅ GitHub backup done at {ts}")
    except Exception as e:
        print(f"⚠️ Backup error: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────────────────────────────────────
async def run_bot():
    global MAGIC, NONMAGIC
    MAGIC, NONMAGIC = init_catalogs(str(DATA_DIR))

    app = ApplicationBuilder().token(TOKEN).build()

    # Удаление
    remove_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➖ Удалить предмет$"), remove_item)],
        states={
            STATE_REMOVE_CATEGORY: [
                MessageHandler(filters.Regex("^🔙 Назад$"), on_remove_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, show_remove_page),
                CallbackQueryHandler(on_remove_nav, pattern="^pg_(prev|next)$"),
                CallbackQueryHandler(on_remove_click, pattern="^rm_"),
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^🔙 Назад$"), on_remove_cancel)],
    )
    app.add_handler(remove_conv)

    # Просмотр инвентаря
    inventory_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📦 Инвентарь$"), show_inventory_menu)],
        states={
            STATE_INVENTORY_CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, show_inventory_list),
                CallbackQueryHandler(on_inventory_nav, pattern="^inv_(prev|next)$"),
                CallbackQueryHandler(on_inventory_item, pattern="^inv_[0-9]+$"),
            ],
        },
        fallbacks=[MessageHandler(filters.Regex("^🔙 Назад$"),
                                  lambda u,c: (u.message.reply_text("↩️ Возврат в главное меню.",
                                                                    reply_markup=keyboard_for(u,c)),
                                               ConversationHandler.END)[1])],
    )
    app.add_handler(inventory_conv)

    # Симуляция
    simulate_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🎲 Симулировать день$"), ask_simulation_days)],
        states={STATE_SIMULATE_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_simulation_days)]},
        fallbacks=[MessageHandler(filters.Regex("^🔙 Назад$"),
                                  lambda u,c: (u.message.reply_text("↩️ Возврат в главное меню.",
                                                                    reply_markup=keyboard_for(u,c)),
                                               ConversationHandler.END)[1])],
    )
    app.add_handler(simulate_conv)

    # Добавление
    add_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^➕ Добавить предмет$"), add_item_start)],
        states={
            STATE_ADD_CATEGORY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_category)],
            STATE_ADD_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_item_name)],
            STATE_ADD_CONFIRM:  [CallbackQueryHandler(on_add_confirm_button, pattern="^(confirm_|add_custom_)")],
        },
        fallbacks=[MessageHandler(filters.Regex("^🔙 Назад$"),
                                  lambda u,c: (u.message.reply_text("↩️ Возврат в главное меню.",
                                                                    reply_markup=keyboard_for(u,c)),
                                               ConversationHandler.END)[1])],
    )
    app.add_handler(add_conv)

    # Мастер
    app.add_handler(MessageHandler(filters.Regex("^📜 Мастер-инвентарь$"), show_master_inventory))
    app.add_handler(MessageHandler(filters.Regex("^(Карла|Энсо|Найт|Гундар|Авитус|🔙 Назад)$"), master_select_player))

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("categories", categories))
    app.add_handler(CommandHandler("inventory", show_inventory))
    app.add_handler(CommandHandler("simulate", simulate_days))

    # Периодический бэкап
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

