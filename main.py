#!/usr/bin/env python3
"""Дайджест выгодных авто в Telegram: до N в день, из разных площадок, фото-альбом + шаблонный текст.
Отбор: цена «под ключ до Минска» сравнивается с ценой рынка Беларуси (av.by).

python main.py          боевой режим
python main.py --dry    показать выбор в логе, ничего не отправлять и не сохранять
python main.py --demo   отправить тестовые «ДЕМО»-машины (площадки не нужны, состояние не пишется)
"""
import html
import json
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import date
from urllib.parse import quote

import requests

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")
APIFY = os.environ.get("APIFY_TOKEN", "")
DEMO, DRY = "--demo" in sys.argv, "--dry" in sys.argv
UA = "Mozilla/5.0 (compatible; cars-bot)"

with open("config.json", encoding="utf-8") as f:
    CFG = json.load(f)
MARKET = CFG.get("market", {})
STATE_FILE = "state.json"


def _demo(src, i, title, make, model, year, km, price, market, engine, notes):
    return {
        "src": src, "id": f"demo:{src}:{i}", "title": title, "make": make, "model": model,
        "year": year, "km": km, "price": price, "market": market, "url": "https://example.com",
        "photos": [f"https://picsum.photos/seed/{src}{i}{n}/800/600" for n in range(3)],
        "engine": engine, "transmission": "автомат", "drive": "передний", "color": "чёрный", "notes": notes,
    }


DEMO_CARS = [
    _demo("Encar", 1, "ДЕМО Palisade 2.2d 2WD Exclusive", "Hyundai", "Palisade", 2022, 51428, 24710, 33000,
          "2.2 дизель (202 л.с.)", ["🎨 окрас правого заднего крыла", "🚗 2 владельца в Корее"]),
    _demo("Encar", 2, "ДЕМО K5 2.0 Prestige", "Kia", "K5", 2021, 42000, 15900, 19000, "2.0 бензин (152 л.с.)", []),
    _demo("Copart", 1, "ДЕМО Camry SE", "Toyota", "Camry", 2020, 68000, 14200, 17000, "2.5 бензин (203 л.с.)",
          ["💵 страховая выплата 550$"]),
    _demo("Copart", 2, "ДЕМО Accord Sport", "Honda", "Accord", 2019, 90000, 12900, 15000, "1.5 бензин (192 л.с.)", []),
    _demo("mashina.kg", 1, "ДЕМО RAV4 Adventure", "Toyota", "RAV4", 2020, 75000, 21500, 25000, "2.5 бензин (199 л.с.)",
          []),
    _demo("mashina.kg", 2, "ДЕМО RX 350", "Lexus", "RX 350", 2019, 82000, 27900, 31500, "3.5 бензин (296 л.с.)", []),
]


# ---------- служебное ----------
def todo(v):
    """Пусто или не заполнено (плейсхолдер «ЗАМЕНИТЬ…»)."""
    return v in (None, "", [], {}) or (isinstance(v, str) and v.strip().startswith("ЗАМЕНИТЬ"))


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
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = "".join(ch for ch in str(v) if ch.isdigit() or ch == ".")
    try:
        return float(s) if s else None
    except ValueError:
        return None


def fmt(x):
    return f"{x:,.0f}".replace(",", " ")


# ---------- состояние ----------
def load_state():
    state = {"seen": [], "sent": {}, "prices": {}, "market": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save_state(state):
    state["sent"] = dict(sorted(state["sent"].items())[-14:])
    state["prices"] = {k: dict(list(v.items())[-300:]) for k, v in state["prices"].items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------- получение и нормализация данных ----------
def subst(x, subs, enc=False):
    if isinstance(x, str):
        for k, v in subs.items():
            x = x.replace("{" + k + "}", quote(str(v)) if enc else str(v))
        return x
    if isinstance(x, list):
        return [subst(i, subs, enc) for i in x]
    if isinstance(x, dict):
        return {k: subst(v, subs, enc) for k, v in x.items()}
    return x


def raw_items(src, subs=None):
    subs = subs or {}
    if src["type"] == "apify":
        r = requests.post(f"https://api.apify.com/v2/acts/{src['actor']}/run-sync-get-dataset-items",
                          params={"token": APIFY}, json=subst(src.get("input", {}), subs), timeout=300)
    else:
        r = requests.get(subst(src["url"], subs, enc=True), headers=src.get("headers", {}), timeout=60)
    r.raise_for_status()
    data = r.json()
    return (pick(data, src["items_path"]) if src.get("items_path") else data) or []


def turnkey_ok(src):
    tk = src.get("turnkey") or {}
    return all(isinstance(tk.get(k), (int, float)) and not isinstance(tk.get(k), bool) for k in ("mult", "add_usd"))


def engine_cc(v):
    """Объём двигателя в см³: принимает 2199, '2 199 cc' или литры (2.2)."""
    n = num(v)
    if not n or n <= 0:
        return None
    return round(n * 1000) if n < 20 else round(n)


def engine_text(cc, fuel):
    return (f"{cc / 1000:.1f} л" + (f", {fuel}" if fuel else "")) if cc else ""


def customs_usd(year, cc, fuel, value_usd):
    """Растаможка физлица в Беларуси, USD: пошлина (с льготой) + таможенный сбор + утильсбор.
    Ставки, сборы и курсы НБРБ берутся из config.json → customs."""
    C = CFG["customs"]
    if any(todo(C.get(k)) for k in ("byn_per_usd", "byn_per_eur")):
        raise ValueError("в config.json → customs не заданы курсы НБРБ")
    eur_usd = C["byn_per_eur"] / C["byn_per_usd"]
    age = date.today().year - year
    young = age < C["young_under_years"]
    fl = (fuel or "").lower()
    if fl == "ev" or "electric" in fl or "электр" in fl:
        if date.today().year > C.get("ev_zero_until_year", 0):
            raise ValueError("правила для электромобилей на этот год не заданы")
        duty_eur = 0.0
    else:
        if not cc:
            raise ValueError("нет объёма двигателя (поле engine_cc), растаможку посчитать нельзя")
        if young:
            v_eur = (value_usd + C.get("value_add_usd", 0)) / eur_usd
            row = next(r for r in C["young"] if r[0] is None or v_eur <= r[0])
            duty_eur = max(v_eur * row[1], cc * row[2])
        else:
            table = C["mid"] if age <= C["mid_max_years"] else C["old"]
            duty_eur = cc * next(r for r in table if r[0] is None or cc <= r[0])[1]
    duty_usd = duty_eur * eur_usd * (1 - C.get("duty_discount", 0))
    fees_byn = C["customs_fee_byn"] + (C["util_fee_young_byn"] if young else C["util_fee_old_byn"])
    return duty_usd + fees_byn / C["byn_per_usd"]


def normalize(raw, src):
    f = src["fields"]
    get = lambda k: pick(raw, f.get(k)) if isinstance(f.get(k), str) else None
    txt = lambda k: "" if todo(get(k)) else str(get(k))
    missing = [k for k in ("id", "title", "price", "year", "url") if todo(get(k))]
    if missing:
        raise KeyError(f"в config.json для «{src['name']}» не настроены поля {missing}; "
                       f"в ответе площадки есть поля: {list(raw)[:30]}")
    price_src, year = num(get("price")), num(get("year"))
    if not price_src or price_src <= 0 or not year:
        raise ValueError("цена или год не распознаны")
    tk = src["turnkey"]
    base = price_src * src.get("price_mult", 1)
    cc, fuel = engine_cc(get("engine_cc")), txt("fuel")
    extra = customs_usd(int(year), cc, fuel, base) if tk.get("customs") else 0
    price = round((base * tk["mult"] + tk["add_usd"] + extra) / 10) * 10
    ph = get("photos") or get("photo")
    if isinstance(ph, str):
        ph = [ph]
    notes = []
    for n in f.get("notes") or []:
        v = pick(raw, n.get("path"))
        if v in (None, "", 0, "0", False, [], {}):
            continue
        notes.append(f"{n.get('icon', '•')} " + str(n.get("text", "")).replace("{}", str(v)))
    return {
        "src": src["name"], "id": f"{src['name']}:{get('id')}", "title": txt("title")[:120],
        "make": txt("make"), "model": txt("model"), "price": price, "year": int(year), "km": num(get("km")),
        "url": txt("url"), "photos": [p for p in (ph or []) if isinstance(p, str) and p.startswith("http")],
        "engine": txt("engine") or engine_text(cc, fuel), "engine_cc": cc, "fuel": fuel,
        "transmission": txt("transmission"), "drive": txt("drive"), "color": txt("color"), "notes": notes,
    }


def fetch(src):
    if not turnkey_ok(src):
        raise ValueError("не заданы turnkey.mult и turnkey.add_usd: цена «под ключ» не посчитана, площадка пропущена")
    cars, errs, first_err = [], Counter(), None
    for raw in raw_items(src):
        try:
            cars.append(normalize(raw, src))
        except (KeyError, TypeError, ValueError) as e:
            errs[str(e)[:160]] += 1
            first_err = first_err or e
    if not cars and first_err:
        raise first_err
    for msg, n in errs.most_common(3):
        print(f"   пропущено {n} шт. ({src['name']}): {msg}")
    return cars


# ---------- фильтр, рынок, отбор ----------
def good(c):
    f = CFG["filters"]  # price_usd_max — предельная цена ПОД КЛЮЧ
    return (c["year"] >= f.get("year_min", 0)
            and c["price"] <= f.get("price_usd_max", 10 ** 12)
            and (c["km"] is None or c["km"] <= f.get("km_max", 10 ** 12)))


def alias(s):
    s = str(s or "").lower()
    for a, b in MARKET.get("aliases", {}).items():
        s = s.replace(a.lower(), str(b).lower())
    return " ".join(s.split())


def ident(c):
    if c.get("make") and c.get("model"):
        return alias(c["make"]), " ".join(alias(c["model"]).split()[:2])
    t = alias(c["title"]).split()
    return (t[0] if t else ""), " ".join(t[1:2])


def mkey(c):
    make, model = ident(c)
    return f"{make} {model}".strip() + f"|{c['year']}"


def key(c):  # группа для запасной оценки по истории цен
    return f"{c['src']}|{mkey(c)}"


def score(c, prices):
    """Запасная оценка (режим market.mode = off): скидка к медиане по площадке, а пока данных мало, близость к бюджету."""
    hist = [p for i, p in prices.get(key(c), {}).items() if i != c["id"]]
    if len(hist) >= 5:
        med = statistics.median(hist)
        return (med - c["price"]) / med, round((med - c["price"]) / med * 100)
    top = CFG["filters"].get("price_usd_max") or c["price"]
    return (top - c["price"]) / top, None


def tokens(s):
    return re.findall(r"[a-zа-я0-9][a-zа-я0-9\-\.]*", str(s).lower())


def table_lookup(c):
    """Ищет цену в market.prices. Название из таблицы (например «bmw 5 series g30») должно целиком
    встретиться словами по порядку в описании машины (марка + модель + заголовок); побеждает самое
    длинное, то есть самое конкретное, совпадение. Сначала точный год, потом «|*»."""
    desc = tokens(alias(f"{c.get('make', '')} {c.get('model', '')} {c['title']}"))
    entries = []
    for k, v in MARKET.get("prices", {}).items():
        name, _, yr = k.partition("|")
        if isinstance(v, (int, float)) and v > 0:
            entries.append((tuple(tokens(name)), yr.strip(), v))
    for want in (str(c["year"]), "*"):
        best = None
        for name, yr, price in entries:
            it = iter(desc)
            if yr == want and name and all(t in it for t in name) and (best is None or len(name) > len(best[0])):
                best = (name, price)
        if best:
            return best[1]
    return None


def market_price(c, state, look):
    """Цена этой машины на рынке Беларуси (av.by): из ручной таблицы или автозапросом с кэшем."""
    mode, k = MARKET.get("mode", "off"), mkey(c)
    if mode == "manual":
        return table_lookup(c)
    if mode == "auto":
        cached = state["market"].get(k)
        if cached and (date.today() - date.fromisoformat(cached["day"])).days < MARKET.get("cache_days", 7):
            return cached["price"]
        if look["n"] >= MARKET.get("max_lookups", 5) or todo(MARKET.get("auto", {}).get("url", MARKET.get("auto", {}).get("actor"))):
            return None
        look["n"] += 1
        make, model = ident(c)
        try:
            raw = raw_items(MARKET["auto"], {"make": make, "model": model, "year": c["year"], "query": f"{make} {model}".strip()})
        except Exception as e:
            print(f"⚠️ рынок для {k}: {e}")
            return None
        fl, mult, ps = MARKET["auto"]["fields"], MARKET["auto"].get("price_mult", 1), []
        for r in raw:
            p, y = num(pick(r, fl.get("price"))), num(pick(r, fl.get("year")))
            if p and p > 0 and y and abs(y - c["year"]) <= 1:
                ps.append(p * mult)
        price = statistics.median(ps) if len(ps) >= MARKET.get("min_samples", 5) else None
        state["market"][k] = {"price": price, "n": len(ps), "day": date.today().isoformat()}
        return price
    return None


def apply_market(cands, state):
    need, require = MARKET.get("min_discount_pct", 10), MARKET.get("require", True)
    look, miss, kept = {"n": 0}, Counter(), []
    for c in sorted(cands, key=lambda c: c["price"]):
        mp = c.get("market") or market_price(c, state, look)
        if not mp:
            miss[mkey(c)] += 1
            if require:
                continue
            c["disc"], c["score"] = None, -1
        else:
            c["market"] = mp
            d = (mp - c["price"]) / mp
            c["disc"], c["score"] = round(d * 100), d
            if require and d * 100 < need:
                continue
        kept.append(c)
    if miss:
        print("Нет цены рынка для:", ", ".join(f"{k} ×{n}" for k, n in miss.most_common(10)))
    return kept


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


# ---------- текст по шаблону ----------
def render(c):
    esc = lambda s: "" if todo(s) else html.escape(str(s))
    notes = ""
    if c.get("notes"):
        notes = "\n".join([CFG.get("notes_header", "По предварительной информации:")] + [html.escape(n) for n in c["notes"]])
    mline = ""
    if c.get("market") and c.get("disc") and c["disc"] > 0:
        mline = (MARKET.get("line", "📊 Средняя цена на av.by: ~{market}$ · выгода {disc}%")
                 .replace("{market}", fmt(c["market"])).replace("{disc}", str(c["disc"])))
    vals = {
        "title": esc(c["title"]), "year": esc(c["year"]), "price_turnkey": fmt(c["price"]),
        "mileage": fmt(c["km"]) if c.get("km") is not None else "", "engine": esc(c.get("engine")),
        "transmission": esc(c.get("transmission")), "drive": esc(c.get("drive")), "color": esc(c.get("color")),
        "notes_block": notes, "market_line": html.escape(mline), "order_link": esc(CFG.get("order_link")),
    }
    out = []
    for line in CFG["template"]:
        names = re.findall(r"\{(\w+)\}", line)
        if any(not vals.get(n) for n in names):  # нет данных для плейсхолдера: строка пропускается
            continue
        for n in names:
            line = line.replace("{" + n + "}", str(vals[n]))
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---------- Telegram ----------
def tg(method, files=None, **data):
    r = None
    for _ in range(2):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                          data={"chat_id": CHAT, **data}, files=files, timeout=120)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        break
    if not r.ok:
        print(f"⚠️ Telegram {method}: {r.status_code} {r.text[:300]}")
    return r


def download(url):
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        if r.ok and r.headers.get("content-type", "").startswith("image") and len(r.content) < 10_000_000:
            return r.content
    except requests.RequestException:
        pass
    return None


def send_photos(photos, caption):
    """Одно фото или альбом (2–10). Сначала по ссылкам, при отказе Telegram скачиваем и загружаем сами."""
    cap = {"caption": caption, "parse_mode": "HTML"} if caption else {}
    if len(photos) == 1:
        if tg("sendPhoto", photo=photos[0], **cap).ok:
            return True
        b = download(photos[0])
        return bool(b) and tg("sendPhoto", files={"photo": ("p.jpg", b)}, **cap).ok
    media = [{"type": "photo", "media": u} for u in photos]
    if cap:
        media[0].update(cap)
    if tg("sendMediaGroup", media=json.dumps(media)).ok:
        return True
    files, media = {}, []
    for i, u in enumerate(photos):
        b = download(u)
        if b:
            files[f"p{i}"] = (f"p{i}.jpg", b)
            media.append({"type": "photo", "media": f"attach://p{i}"})
    if not media:
        return False
    if len(media) == 1:
        return tg("sendPhoto", files={"photo": next(iter(files.values()))}, **cap).ok
    if cap:
        media[0].update(cap)
    return tg("sendMediaGroup", files=files, media=json.dumps(media)).ok


def send(c):
    text = render(c)
    photos = c.get("photos", [])[:CFG.get("max_photos", 10)]
    caption = text if len(text) <= 1024 else None  # длинный текст уходит отдельным сообщением
    posted = bool(photos) and send_photos(photos, caption)
    if posted and caption:
        return True
    return tg("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview="true").ok


# ---------- основной сценарий ----------
def main():
    if not DRY and not (TOKEN and CHAT):
        sys.exit("Не заданы TG_TOKEN и TG_CHAT")
    state = load_state()
    today = date.today().isoformat()
    rd = CFG.get("customs", {}).get("rates_date")
    if rd and (date.today() - date.fromisoformat(rd)).days > 14:
        print(f"⚠️ Курсы НБРБ в config.json от {rd} устарели: обновите customs.byn_per_usd и customs.byn_per_eur")
    left = CFG["daily_limit"] - (0 if DEMO else state["sent"].get(today, 0))
    if left <= 0:
        print(f"Дневной лимит ({CFG['daily_limit']}) уже исчерпан")
        return

    fetched = {}
    if DEMO:
        fetched = {c["id"]: dict(c) for c in DEMO_CARS}
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

    seen = set(state["seen"])
    for c in fetched.values():
        c["score"], c["disc"] = score(c, state["prices"])
    cands = [c for c in fetched.values() if c["id"] not in seen and good(c)]
    for c in fetched.values():  # копим цены для запасной оценки
        state["prices"].setdefault(key(c), {})[c["id"]] = c["price"]
    if MARKET.get("mode", "off") != "off":
        cands = apply_market(cands, state)

    picked = select(cands, left, CFG["per_source_max"])
    print(f"Подходит: {len(cands)}, к отправке: {len(picked)}")
    sent = []
    for c in picked:
        if DRY:
            print(f"— [{c['src']}] {c['title']} · ${fmt(c['price'])} · {c['year']} · рынок: {c.get('market')} · выгода: {c['disc']}%")
        elif send(c):
            sent.append(c["id"])
            time.sleep(5)
        else:
            print("⚠️ Не отправилось:", c["title"])
    if DRY or DEMO:
        return
    state["seen"] = (state["seen"] + sent)[-5000:]
    state["sent"][today] = state["sent"].get(today, 0) + len(sent)
    save_state(state)


if __name__ == "__main__":
    main()
