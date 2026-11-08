#!/usr/bin/env python3
"""Дайджест выгодных авто в Telegram: не больше N штук в день, из разных площадок.

python main.py          боевой режим
python main.py --dry    показать выбор в логе, ничего не отправлять и не сохранять
python main.py --demo   отправить тестовые «ДЕМО»-машины (площадки не нужны, состояние не пишется)
"""
import html
import json
import os
import statistics
import sys
import time
from datetime import date

import requests

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")
APIFY = os.environ.get("APIFY_TOKEN", "")
DEMO, DRY = "--demo" in sys.argv, "--dry" in sys.argv

with open("config.json", encoding="utf-8") as f:
    CFG = json.load(f)
STATE_FILE = "state.json"

DEMO_CARS = [
    {"src": "Encar", "id": "demo:e1", "title": "ДЕМО Hyundai Sonata", "model": "Hyundai Sonata", "price": 11800, "year": 2021, "km": 42000, "url": "https://example.com/1", "photo": None},
    {"src": "Encar", "id": "demo:e2", "title": "ДЕМО Kia K5", "model": "Kia K5", "price": 13900, "year": 2022, "km": 31000, "url": "https://example.com/2", "photo": None},
    {"src": "Copart", "id": "demo:c1", "title": "ДЕМО Toyota Camry", "model": "Toyota Camry", "price": 6200, "year": 2020, "km": 68000, "url": "https://example.com/3", "photo": None},
    {"src": "Copart", "id": "demo:c2", "title": "ДЕМО Honda Accord", "model": "Honda Accord", "price": 7400, "year": 2019, "km": 90000, "url": "https://example.com/4", "photo": None},
    {"src": "mashina.kg", "id": "demo:m1", "title": "ДЕМО Toyota RAV4", "model": "Toyota RAV4", "price": 17500, "year": 2020, "km": 75000, "url": "https://example.com/5", "photo": None},
    {"src": "mashina.kg", "id": "demo:m2", "title": "ДЕМО Lexus RX 350", "model": "Lexus RX 350", "price": 19800, "year": 2019, "km": 82000, "url": "https://example.com/6", "photo": None},
]


# ---------- состояние ----------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"seen": [], "sent": {}, "prices": {}}


def save_state(state):
    state["sent"] = dict(sorted(state["sent"].items())[-14:])
    state["prices"] = {k: dict(list(v.items())[-300:]) for k, v in state["prices"].items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------- получение и нормализация данных ----------
def pick(obj, path):
    """Достаёт значение по пути вида 'a.b.0.c'."""
    if not path:
        return None
    for part in str(path).split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = "".join(ch for ch in str(v) if ch.isdigit() or ch == ".")
    return float(s) if s else None


def normalize(raw, src):
    f = src["fields"]
    get = lambda k: pick(raw, f.get(k))
    missing = [k for k in ("id", "title", "price", "year", "url") if get(k) in (None, "")]
    if missing:
        raise KeyError(f"в config.json для «{src['name']}» не настроены поля {missing}; "
                       f"в ответе площадки есть поля: {list(raw)[:30]}")
    price, year = num(get("price")), num(get("year"))
    if not price or price <= 0 or not year:
        raise ValueError("цена или год не распознаны")
    title = str(get("title"))[:120]
    photo = get("photo")
    if isinstance(photo, list):
        photo = photo[0] if photo else None
    return {
        "src": src["name"],
        "id": f"{src['name']}:{get('id')}",
        "title": title,
        "model": str(get("model") or title),
        "price": price * src.get("price_mult", 1),
        "year": int(year),
        "km": num(get("km")),
        "url": str(get("url")),
        "photo": photo if isinstance(photo, str) and photo.startswith("http") else None,
    }


def fetch(src):
    if src["type"] == "apify":
        r = requests.post(f"https://api.apify.com/v2/acts/{src['actor']}/run-sync-get-dataset-items",
                          params={"token": APIFY}, json=src.get("input", {}), timeout=300)
    else:
        r = requests.get(src["url"], headers=src.get("headers", {}), timeout=60)
    r.raise_for_status()
    data = r.json()
    items = pick(data, src["items_path"]) if src.get("items_path") else data
    cars, first_err = [], None
    for raw in items or []:
        try:
            cars.append(normalize(raw, src))
        except (KeyError, TypeError, ValueError) as e:
            first_err = first_err or e
    if not cars and first_err:
        raise first_err
    return cars


# ---------- фильтр, оценка, отбор ----------
def good(c):
    f = CFG["filters"]
    return (c["year"] >= f.get("year_min", 0)
            and c["price"] <= f.get("price_usd_max", 10 ** 12)
            and (c["km"] is None or c["km"] <= f.get("km_max", 10 ** 12)))


def key(c):
    return f"{c['src']}|{' '.join(c['model'].lower().split()[:2])}|{c['year']}"


def score(c, prices):
    """Чем выше, тем выгоднее. Со статистикой: скидка к медиане по площадке/модели/году.
    Пока статистики мало (<5 объявлений): близость к бюджету."""
    hist = [p for i, p in prices.get(key(c), {}).items() if i != c["id"]]
    if len(hist) >= 5:
        med = statistics.median(hist)
        d = (med - c["price"]) / med
        return d, round(d * 100)
    top = CFG["filters"].get("price_usd_max") or c["price"]
    return (top - c["price"]) / top, None


def select(cands, n, per_src):
    """По кругу берём лучшее с каждой площадки, пока не наберём n (не более per_src с одной)."""
    by = {}
    for c in sorted(cands, key=lambda c: -c["score"]):
        by.setdefault(c["src"], []).append(c)
    picked, cnt = [], {}
    while len(picked) < n:
        moved = False
        for s, lst in by.items():
            if lst and cnt.get(s, 0) < per_src and len(picked) < n:
                picked.append(lst.pop(0))
                cnt[s] = cnt.get(s, 0) + 1
                moved = True
        if not moved:
            break
    return picked


# ---------- Telegram ----------
def fmt(x):
    return f"{x:,.0f}".replace(",", " ")


def caption(c):
    lines = [f"🚗 <b>{html.escape(c['title'])}</b>",
             f"📅 {c['year']}  ·  📏 {fmt(c['km']) + ' км' if c['km'] is not None else '—'}",
             f"💰 ${fmt(c['price'])}"]
    if c.get("disc") and c["disc"] > 0:
        lines.append(f"📉 ниже средней по площадке на {c['disc']}%")
    lines.append(f"🏷 {html.escape(c['src'])}")
    lines.append(f"🔗 <a href=\"{html.escape(c['url'], quote=True)}\">Открыть объявление</a>")
    return "\n".join(lines)


def tg(method, **data):
    r = None
    for _ in range(2):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                          data={"chat_id": CHAT, **data}, timeout=60)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        break
    return r


def send(c):
    text = caption(c)
    if c.get("photo"):
        if tg("sendPhoto", photo=c["photo"], caption=text, parse_mode="HTML").ok:
            return True
    return tg("sendMessage", text=text, parse_mode="HTML").ok  # без фото или если Telegram не смог его скачать


# ---------- основной сценарий ----------
def main():
    if not DRY and not (TOKEN and CHAT):
        sys.exit("Не заданы TG_TOKEN и TG_CHAT")
    state = load_state()
    today = date.today().isoformat()
    left = CFG["daily_limit"] - (0 if DEMO else state["sent"].get(today, 0))
    if left <= 0:
        print(f"Дневной лимит ({CFG['daily_limit']}) уже исчерпан")
        return

    fetched = {}
    if DEMO:
        fetched = {c["id"]: c for c in DEMO_CARS}
    else:
        for src in CFG["sources"]:
            if not src.get("enabled"):
                continue
            try:
                cars = fetch(src)
                fetched.update({c["id"]: c for c in cars})
                print(f"{src['name']}: получено {len(cars)}")
            except Exception as e:  # одна сломанная площадка не должна остановить остальные
                print(f"⚠️ {src['name']}: {e}")
    if not fetched:
        print("Нет данных. Проверьте, что площадки включены в config.json (\"enabled\": true)")
        return

    for c in fetched.values():
        c["score"], c["disc"] = score(c, state["prices"])
    seen = set(state["seen"])
    cands = [c for c in fetched.values() if c["id"] not in seen and good(c)]
    for c in fetched.values():  # копим цены для будущих сравнений
        state["prices"].setdefault(key(c), {})[c["id"]] = c["price"]

    picked = select(cands, left, CFG["per_source_max"])
    print(f"Подходит под фильтры: {len(cands)}, к отправке: {len(picked)}")
    sent = []
    for c in picked:
        if DRY:
            print(f"— [{c['src']}] {c['title']} · ${fmt(c['price'])} · {c['year']} · скидка: {c['disc']}")
        elif send(c):
            sent.append(c["id"])
            time.sleep(3)
        else:
            print("⚠️ Не отправилось:", c["title"])
    if DRY or DEMO:
        return
    state["seen"] = (state["seen"] + sent)[-5000:]
    state["sent"][today] = state["sent"].get(today, 0) + len(sent)
    save_state(state)


if __name__ == "__main__":
    main()
