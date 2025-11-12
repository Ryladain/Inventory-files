# -*- coding: utf-8 -*-
# item_catalog.py — загрузка каталогов и форматированный вывод карточек предметов

from pathlib import Path
import json
import re

# Пути по умолчанию: рядом со скриптом бота
DATA_DIR = Path(__file__).resolve().parent / "data"
NONMAGIC_PATH = DATA_DIR / "nonmagic.json"   # оружие/доспехи/прочее
MAGIC_PATH    = DATA_DIR / "library.json"    # магические предметы

NONMAGIC: list[dict] = []
MAGIC: list[dict] = []

def init_catalogs(data_dir: str | Path | None = None):
    global DATA_DIR, NONMAGIC_PATH, MAGIC_PATH, NONMAGIC, MAGIC
    if data_dir:
        DATA_DIR = Path(data_dir)
        NONMAGIC_PATH = DATA_DIR / "nonmagic.json"
        MAGIC_PATH    = DATA_DIR / "library.json"

    def _load(path: Path) -> list[dict]:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return []

    NONMAGIC = _load(NONMAGIC_PATH)
    MAGIC    = _load(MAGIC_PATH)

    print(f"📚 Загружено: {len(MAGIC)} магических и {len(NONMAGIC)} немагических предметов.")
    return MAGIC, NONMAGIC   # ← вот эта строчка



def _norm(s: str) -> str:
    return (s or "").strip().lower()

def find_nonmagic_item(name: str, category: str | None = None) -> dict | None:
    """Поиск по nonmagic.json: точное, затем частичное совпадение."""
    q = _norm(name)
    # точное
    for it in NONMAGIC:
        if category and it.get("category") != category:
            continue
        if _norm(it.get("name")) == q:
            return it
    # частичное
    for it in NONMAGIC:
        if category and it.get("category") != category:
            continue
        if q and q in _norm(it.get("name")):
            return it
    return None

def find_magic_item(name: str) -> dict | None:
    """Поиск по library.json: точное, затем частичное совпадение."""
    q = _norm(name)
    for it in MAGIC:
        if _norm(it.get("name")) == q:
            return it
    for it in MAGIC:
        if q and q in _norm(it.get("name")):
            return it
    return None

def enrich_item(obj: dict) -> dict | None:
    """
    Принимает {'name','category'} и возвращает полную запись из каталогов.
    Если не нашлось — вернёт исходный объект.
    """
    if not obj:
        return None
    name = obj.get("name") or obj.get("title") or ""
    category = obj.get("category")
    if not name:
        return obj

    if category in ("Оружие","Доспехи","Инструменты","Снаряжение","Наборы","Одежда"):
        found = find_nonmagic_item(name, category if category in ("Оружие","Доспехи") else None) \
                or find_nonmagic_item(name)
    else:
        # всё остальное считаем магией
        found = find_magic_item(name)

    return found or obj

def render_item_card(item: dict) -> str:
    """
    Форматированный вывод карточки предмета.
    Поддерживает оружие, доспехи, магию и прочее.
    Берёт поля из уже готовых JSON (ничего не парсит!).
    """
    if not item:
        return "—"

    cat  = item.get("category") or item.get("type") or "Предмет"
    name = item.get("name", "Безымянный")
    cost = item.get("cost")
    weight = item.get("weight")
    # Если у тебя другое поле с описанием, добавь его сюда через or:
    desc = item.get("desc") or item.get("description") or item.get("описание")
    src  = item.get("source_url")

    lines = [f"*{name}* ({cat})"]

    # ОРУЖИЕ
    if cat == "Оружие" and isinstance(item.get("props"), dict):
        p = item["props"]
        dmg = p.get("damage", {})
        dmg_s = " / ".join([x for x in [dmg.get("dice"), dmg.get("type")] if x])
        if dmg_s:
            lines.append(f"Урон: {dmg_s}")
        props_list = (p.get("properties") or [])[:]
        rng = p.get("ranges") or {}
        if rng.get("ammo"):
            props_list.append(f"боеприпас {rng['ammo']}")
        if rng.get("thrown"):
            props_list.append(f"метательное {rng['thrown']}")
        if p.get("versatile_dice"):
            props_list.append(f"универсальное ({p['versatile_dice']})")
        if props_list:
            lines.append("Свойства: " + ", ".join(props_list))

    # ДОСПЕХИ
    if cat == "Доспехи" and isinstance(item.get("props"), dict):
        p = item["props"]
        if p.get("ac"):      lines.append(f"КД: {p['ac']}")
        if p.get("str_req"): lines.append(f"Требование силы: {p['str_req']}")
        sd = p.get("stealth_disadv")
        if sd is True:  lines.append("Помеха скрытности: да")
        if sd is False: lines.append("Помеха скрытности: нет")

    # МАГИЯ: покажем редкость/настройку, если есть такие ключи
    if cat.lower().startswith("маг"):
        rar = item.get("rarity") or item.get("редкость")
        att = item.get("attunement") or item.get("настройка")
        if rar: lines.append(f"Редкость: {rar}")
        if att: lines.append(f"Настройка: {att}")

    if cost:   lines.append(f"Стоимость: {cost}")
    if weight: lines.append(f"Вес: {weight}")

    if desc:
        short = re.sub(r"\s+", " ", desc.strip())
        if len(short) > 400:
            short = short[:400].rstrip() + "…"
        lines.append("")
        lines.append(short)

    if src:
        lines.append(f"[Источник]({src})")

    return "\n".join(lines)
