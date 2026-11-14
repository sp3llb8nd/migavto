#!/usr/bin/env python3
"""Автопостинг выгодных авто в Telegram-канал.

Площадки (Encar, Copart, mashina.kg) → цена «под ключ до Минска» → сравнение с рынком Беларуси (av.by)
→ до N постов в сутки (фото-альбом + текст по шаблону страны), выкладка несколькими порциями за день.

python main.py          боевой режим (порция зависит от времени запуска, см. post_times в config.json)
python main.py --dry    показать выбор в логе, ничего не отправлять и не сохранять
python main.py --demo   отправить тестовые «ДЕМО»-машины (площадки не нужны, состояние не пишется)
"""
import glob
import html
import io
import json
import os
import random
import re
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime
from urllib.parse import quote

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import requests

TOKEN = os.environ.get("TG_TOKEN", "")
CHAT = os.environ.get("TG_CHAT", "")
APIFY = os.environ.get("APIFY_TOKEN", "")
DEMO, DRY = "--demo" in sys.argv, "--dry" in sys.argv
PREFETCH = "--prefetch" in sys.argv
POOL_VERSION = 2  # меняется при изменении расчёта цен: запас дня, собранный старым кодом, пересобирается
UA = "Mozilla/5.0 (compatible; cars-bot)"
HANGUL = r"[\uac00-\ud7a3]+"
UNKNOWN_KO = Counter()

with open("config.json", encoding="utf-8") as f:
    CFG = json.load(f)
MARKET = CFG.get("market", {})
STATE_FILE = "state.json"


DEMO_COUNTRY = {"Encar": "KR", "Copart": "US", "mashina.kg": "KG"}


def _demo(src, i, title, make, model, year, km, price, market, engine, notes):
    return {
        "src": src, "id": f"demo:{src}:{i}", "title": title, "make": make, "model": model, "country": DEMO_COUNTRY[src],
        "price_usd": {"Encar": price - 9000, "Copart": price - 8000, "mashina.kg": price - 2600}[src],
        "price_src": None, "currency_src": "USD", "rate": None,
        "year": year, "km": km, "price": price, "market": market, "url": "https://example.com",
        "photos": [f"https://picsum.photos/seed/{src}{i}{n}/800/600" for n in range(3)],
        "engine": engine, "transmission": "автомат", "drive": "передний", "color": "чёрный", "notes": notes,
    }


DEMO_CARS = [
    _demo("Encar", 1, "ДЕМО Hyundai Palisade 2.2 Exclusive", "Hyundai", "Palisade", 2022, 51428, 24710, 33000,
          "2.2 л, дизель", ["🎨 окрас правого заднего крыла", "🚗 2 владельца в Корее"]),
    _demo("Encar", 2, "ДЕМО Kia K5 2.0 Prestige", "Kia", "K5", 2021, 42000, 15900, 19000, "2.0 л, бензин", []),
    _demo("Copart", 1, "ДЕМО Toyota Camry SE", "Toyota", "Camry", 2020, 68000, 14200, 17000, "2.5 л, бензин",
          ["💥 Повреждение: FRONT END"]),
    _demo("Copart", 2, "ДЕМО Honda Accord Sport", "Honda", "Accord", 2019, 90000, 12900, 15000, "1.5 л, бензин", []),
    _demo("mashina.kg", 1, "ДЕМО Toyota RAV4 Adventure", "Toyota", "RAV4", 2020, 75000, 21500, 25000, "2.5 л", []),
    _demo("mashina.kg", 2, "ДЕМО Lexus RX 350", "Lexus", "RX 350", 2019, 82000, 27900, 31500, "3.5 л", []),
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


NBSP = "\u00a0"  # неразрывный пробел: Telegram не переносит строку внутри «21 000 $»


def fmt(x):
    return f"{x:,.0f}".replace(",", NBSP)


def money(x, cur="$"):
    """Сумма с валютой одним неразрывным блоком: 17 600 $."""
    return f"{fmt(x)}{NBSP}{cur}"


def parse_number(v):
    """Число из ячейки таблицы или поля площадки: 1450, «1 450», «1450,5», «18,900,000», «18 900 000 ₩».
    Возвращает None для пустых и нечисловых значений, вместо ошибки."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = re.sub(r"[\s\u00a0\u202f']", "", str(v))
    t = re.sub(r"[^0-9,.\-]", "", t)
    if not re.search(r"\d", t):
        return None
    if "," in t and "." in t:  # 18,900,000.50 или 18.900.000,50
        t = t.replace(",", "") if t.rfind(".") > t.rfind(",") else t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+", t) else t.replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    try:
        return float(t)
    except ValueError:
        return None


def now_local():
    tz = CFG.get("tz")
    try:
        return datetime.now(ZoneInfo(tz)) if tz and ZoneInfo else datetime.now()
    except Exception:
        return datetime.now()


def src_cfg(name):
    return next((s for s in CFG["sources"] if s["name"] == name), {})


_WARNED = set()


def country_of(c):
    """Страна объявления: по источнику (площадке), откуда оно пришло. Марка машины не учитывается никогда.
    Порядок: страна, записанная в объявлении при разборе → страна площадки в config.json → «неизвестно»."""
    code = (c.get("country") or src_cfg(c.get("src")).get("country") or "").upper()
    if code in CFG.get("countries", {}):
        return code
    if c.get("src") not in _WARNED:
        _WARNED.add(c.get("src"))
        print(f"⚠️ Площадка «{c.get('src')}»: страна не задана или её нет в config.json → countries; флаг не ставится")
    return ""


def country_info(c):
    info = CFG.get("countries", {}).get(country_of(c))
    return info or CFG.get("unknown_country", {"header": "Авто под заказ", "flag": ""})


# ---------- курс корейской воны из Google-таблицы ----------
RATES = {}  # заполняется один раз за запуск: {"KRW": {"working": 1450.0, "real": 1330.0, ...}}


def _norm_label(s):
    return " ".join(str(s or "").lower().replace("ё", "е").split())


def sheet_rows(rc):
    """Таблица как список строк (CSV). Нужен доступ «Все, у кого есть ссылка — Читатель»."""
    import csv
    base = f"https://docs.google.com/spreadsheets/d/{rc['sheet_id']}"
    gid = str(rc.get("gid", "0"))
    errors = []
    for url in (f"{base}/export?format=csv&gid={gid}", f"{base}/gviz/tq?tqx=out:csv&gid={gid}"):
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        except requests.RequestException as e:
            errors.append(str(e)[:120])
            continue
        ctype = r.headers.get("content-type", "")
        if r.ok and "html" not in ctype.lower():
            r.encoding = "utf-8"
            return list(csv.reader(io.StringIO(r.text)))
        errors.append(f"HTTP {r.status_code}, {ctype or 'нет типа'}"
                      + (" (похоже на страницу входа: таблица не открыта по ссылке)" if "html" in ctype.lower() else ""))
    raise ValueError("не удалось прочитать Google-таблицу: " + "; ".join(errors))


def sheet_value(rows, label):
    """Число рядом с подписью: справа в той же строке, иначе ниже в том же столбце (до 5 строк)."""
    want = _norm_label(label)
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            if want and want in _norm_label(cell) and parse_number(cell) is None:
                for v in row[j + 1:]:
                    if parse_number(v) is not None:
                        return parse_number(v)
                for k in range(i + 1, min(i + 6, len(rows))):
                    if j < len(rows[k]) and parse_number(rows[k][j]) is not None:
                        return parse_number(rows[k][j])
    return None


def load_krw_rate(state):
    """Рабочий и реальный курс из таблицы. Если таблица недоступна: последний прочитанный из неё курс,
    но не старше krw_rates.max_cache_days. Иначе None, и машины Encar не публикуются."""
    rc = CFG.get("krw_rates") or {}
    if todo(rc.get("sheet_id")):
        print("⚠️ Курс вон: в config.json не задан krw_rates.sheet_id")
        return None
    lo, hi = rc.get("min", 500), rc.get("max", 3000)
    try:
        rows = sheet_rows(rc)
        work, real = sheet_value(rows, rc["working_label"]), sheet_value(rows, rc.get("real_label", ""))
        if work is None:
            raise ValueError(f"в таблице не найдена подпись «{rc['working_label']}» с числом рядом")
        if not lo <= work <= hi:
            raise ValueError(f"рабочий курс {work} вне допустимого диапазона {lo}–{hi} вон за доллар")
        # самопроверка формулы на примере из самой таблицы: цена для клиента = цена в вонах / рабочий курс
        p, client = sheet_value(rows, rc.get("price_label", "")), sheet_value(rows, rc.get("client_label", ""))
        if p and client:
            if abs(p / work - client) > max(1.0, client * 0.005):
                raise ValueError(f"формула не сходится с таблицей: {p:.0f} / {work} = {p / work:.2f}, а в таблице {client:.2f}")
            check = f"сверка с таблицей: {fmt(p)} ₩ / {work:g} = {p / work:.2f} $ ✓"
        else:
            check = "сверка формулы не выполнена: в таблице не найдены цена в вонах и цена для клиента"
        rate = {"working": work, "real": real, "day": date.today().isoformat(), "source": "таблица"}
        state["krw_rate"] = rate
        print(f"Курс вон из таблицы: рабочий {work:g}" + (f", реальный {real:g}" if real else "") + f" ({check})")
        return rate
    except Exception as e:
        cached = state.get("krw_rate")
        if cached and (date.today() - date.fromisoformat(cached["day"])).days <= rc.get("max_cache_days", 3):
            print(f"⚠️ Курс вон: {e}. Использую последний курс из таблицы от {cached['day']}: {cached['working']:g}")
            return dict(cached, source="кэш")
        print(f"⚠️ Курс вон не получен: {e}. Машины из Кореи в этом запуске не публикуются")
        return None


def to_usd(amount, currency, src):
    """Единственное место перевода валюты площадки в доллары. Вызывается ровно один раз на объявление."""
    cur = (currency or "USD").upper()
    if cur == "USD":
        return amount, None
    if cur == "KRW":
        rate = (RATES.get("KRW") or {}).get("working")
        if not rate:
            raise ValueError("нет рабочего курса вон (Google-таблица недоступна): цена не публикуется")
        return amount / rate, rate
    raise ValueError(f"валюта {cur} у площадки «{src['name']}» не поддерживается")


# ---------- состояние ----------
def load_state():
    state = {"seen": [], "sent": {}, "sent_src": {}, "prices": {}, "market": {}, "pool": {}}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save_state(state):
    state["sent"] = dict(sorted(state["sent"].items())[-14:])
    state["sent_src"] = dict(sorted(state["sent_src"].items())[-14:])
    state["prices"] = {k: dict(list(v.items())[-300:]) for k, v in state["prices"].items()}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ---------- получение и нормализация данных ----------
def subst(x, subs, enc=False):
    if isinstance(x, str):
        if not enc and x.startswith("{") and x.endswith("}") and x[1:-1] in subs:
            return subs[x[1:-1]]  # весь текст = один плейсхолдер: сохраняем тип (год остаётся числом)
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
    if src["type"] in ("apify", "apify_list"):
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


def tr(s):
    """Перевод корейских названий по словарю config.json → translate. Остатки хангыля удаляются."""
    s = str(s or "")
    tmap = CFG.get("translate", {})
    for k in sorted(tmap, key=len, reverse=True):
        s = s.replace(k, tmap[k])
    s = re.sub(r"\d+\s*세대", " ", s)  # «3세대» (поколение)
    for w in re.findall(HANGUL, s):
        UNKNOWN_KO[w] += 1
    return " ".join(re.sub(HANGUL, " ", s).split())


def vmap(v):
    """Перевод значения характеристики целиком (GAS → бензин, AUTOMATIC → автомат) по config.json → value_map.
    Только точное совпадение всего значения: название модели и комплектации не затрагиваются."""
    t = " ".join(str(v or "").split())
    return CFG.get("value_map", {}).get(t.upper(), t)


def nice_case(s):
    return " ".join(w.capitalize() if w.isalpha() and w.isupper() and len(w) > 3 else w for w in s.split())


def parse_year(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if 1900 <= v <= 2100 else None
    m = re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(v))
    return int(m.group(0)) if m else None


def engine_cc(v):
    """Объём двигателя в см³ из числа или текста: 2199, '2 199 cc', 2.2, '2.5L V6', 'Diesel 2.2 2WD'."""
    if isinstance(v, bool) or v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        n = float(v)
    else:
        s = re.sub(r"(?<=\d)[ \u00a0](?=\d{3}(?!\d))", "", str(v))
        m = re.search(r"(?<![\d.])(\d{3,4})\s*(?:cc|см)", s, re.I) or re.search(r"(?<![\d.])(\d\.\d)(?!\d)", s)
        if not m:
            return None
        n = float(m.group(1))
    if n <= 0:
        return None
    return round(n * 1000) if n < 20 else round(n)


def engine_text(cc, fuel):
    return (f"{cc / 1000:.1f} л" + (f", {fuel}" if fuel else "")) if cc else ""


EV_WORDS = {"ev", "electric", "электро", "электричество", "электромобиль", "전기"}


def customs_usd(year, cc, fuel, value_usd):
    """Растаможка физлица в Беларуси, USD: пошлина (с льготой) + таможенный сбор + утильсбор.
    Ставки, сборы и курсы НБРБ берутся из config.json → customs."""
    C = CFG["customs"]
    if any(todo(C.get(k)) for k in ("byn_per_usd", "byn_per_eur")):
        raise ValueError("в config.json → customs не заданы курсы НБРБ")
    eur_usd = C["byn_per_eur"] / C["byn_per_usd"]
    age = date.today().year - year
    young = age < C["young_under_years"]
    if (fuel or "").strip().lower() in EV_WORDS:  # только чистый электромобиль; гибриды считаются по объёму
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

    def get(k):
        p = f.get(k)
        if isinstance(p, list) and p and all(isinstance(x, str) for x in p):  # несколько полей → одна строка
            vals = [str(v) for v in (pick(raw, x) for x in p) if not todo(v)]
            return " ".join(vals) or None
        return pick(raw, p) if isinstance(p, str) else None

    txt = lambda k: "" if todo(get(k)) else str(get(k))
    missing = [k for k in ("id", "title", "year", "url") if todo(get(k))] + (["price"] if todo(f.get("price")) else [])
    if missing:
        raise KeyError(f"в config.json для «{src['name']}» не настроены поля {missing}; "
                       f"в ответе площадки есть поля: {list(raw)[:30]}")
    price_src, year = parse_number(get("price")), parse_year(get("year"))
    if (not price_src or price_src <= 0) and f.get("price_alt"):  # запасное поле, например цена в манвонах
        alt = parse_number(pick(raw, f["price_alt"]))
        price_src = alt * f.get("price_alt_mult", 1) if alt else None
    cur = (src.get("price_currency") or "USD").upper()
    if cur != "USD" and src.get("price_mult", 1) != 1:
        raise ValueError(f"в config.json у «{src['name']}» одновременно price_currency={cur} и price_mult: двойная конвертация")
    if not year:
        raise ValueError("год не распознан")
    if not price_src or price_src <= 0:
        raise ValueError("нет цены (у лотов Copart без ставок цена 0): объявление пропущено")
    if price_src in (src.get("invalid_prices") or []) or price_src < src.get("min_price", 0):
        raise ValueError(f"цена {fmt(price_src)} {cur} похожа на заглушку площадки: объявление пропущено")
    tk = src["turnkey"]
    usd, rate = to_usd(price_src * src.get("price_mult", 1), cur, src)  # перевод в доллары: один раз
    cc, fuel = engine_cc(get("engine_cc")), vmap(tr(txt("fuel")))
    extra = customs_usd(year, cc, fuel, usd) if tk.get("customs") else 0
    price = round((usd * tk["mult"] + tk["add_usd"] + extra) / 10) * 10
    km = num(get("km"))
    ph = get("photos") or get("photo")
    if isinstance(ph, str):
        ph = [ph]
    photos = [p for p in (ph or []) if isinstance(p, str) and p.startswith("http")]
    if src.get("photo_seq") and len(photos) == 1:  # Encar: …_001.jpg → …_002.jpg … до photo_count
        m = re.search(r"_001\.(jpe?g)$", photos[0], re.I)
        n = int(num(get("photo_count")) or 1)
        if m and n > 1:
            photos = [f"{photos[0][:m.start()]}_{i:03d}.{m.group(1)}" for i in range(1, min(n, CFG.get("max_photos", 10)) + 1)]
    notes = []
    for n in f.get("notes") or []:
        v = pick(raw, n.get("path"))
        if v in (None, "", 0, "0", False, [], {}):
            continue
        notes.append(f"{n.get('icon', '•')} " + str(n.get("text", "")).replace("{}", vmap(tr(v)) if isinstance(v, str) else str(v)))
    title = tr(txt("title"))[:120]
    return {
        "src": src["name"], "id": f"{src['name']}:{get('id')}",
        "title": nice_case(title) if src.get("nice_case") else title,
        "make": tr(txt("make")), "model": tr(txt("model")), "price": price, "year": year,
        "price_src": price_src, "currency_src": cur, "rate": rate, "price_usd": round(usd),
        "country": src.get("country", ""),
        "km": None if km is None else km * src.get("km_mult", 1), "url": txt("url"), "photos": photos,
        "engine": tr(txt("engine")) or engine_text(cc, fuel), "engine_cc": cc, "fuel": fuel,
        "transmission": vmap(tr(txt("transmission"))), "drive": vmap(tr(txt("drive"))), "color": vmap(tr(txt("color"))),
        "notes": notes,
    }


def fetch(src):
    if src["type"] == "mashina_web":
        return fetch_mashina(src)
    if src["type"] == "apify_list":
        return fetch_apify_list(src)
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


# ---------- список запросов (Copart, Encar) через Apify, по кругу ----------
ENCAR_REC = re.compile(r"(?<![\w|-])([A-Za-z][A-Za-z\-]*) \| ([^|]+?) \| (\d{4}) \| (\d{4}) \| (\w+) \| ModifiedDate")


def read_entries(src):
    """Запросы из файла. links: строки «ключ таблицы — ссылка» (Copart).
    encar_params: «слитный» список «Марка | Модель | год | год | тип | сортировка» (Encar)."""
    path = src.get("entries_file")
    if not (path and os.path.exists(path)):
        raise ValueError(f"не найден файл {path}: загрузите его в корень репозитория (рядом с main.py)")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    entries = []
    if src.get("entries_format", "links") == "links":
        by_url = {}
        for ln in text.splitlines():
            key, sep, url = ln.strip().partition(" — ")
            if sep and url.startswith("http"):
                by_url.setdefault(url.strip(), []).append(key.strip())
        for url, keys in by_url.items():
            entries.append({"label": keys[0], "brand": keys[0].split()[0], "keys": keys, "subs": {"url": url}})
    else:
        names = src.get("names", {})  # английское название → как его пишет каталог Encar
        groups = {}
        for brand, model, yf, yt, ctype in ENCAR_REC.findall(" ".join(text.split())):
            g = groups.setdefault((brand, model.strip()), {"years": set(), "ctype": ctype})
            g["years"].update(range(int(yf), int(yt) + 1))
        for (brand, model), g in groups.items():
            ys = sorted(g["years"])
            spans = [(ys[0], ys[-1], ys)] if src.get("group_years", True) else [(y, y, [y]) for y in ys]
            for y0, y1, yy in spans:
                entries.append({"label": f"{brand} {model} {y0}" + (f"–{y1}" if y1 != y0 else ""), "brand": brand,
                                "keys": [f"{brand} {model}|{y}".lower() for y in yy],
                                "subs": {"manufacturer": names.get(brand, brand), "model": names.get(model, model),
                                         "yearFrom": y0, "yearTo": y1, "carType": g["ctype"]}})
    ob = {b.lower() for b in src.get("enabled_brands", [])}
    return [e for e in entries if not ob or e["brand"].lower() in ob]


def pick_batch(entries, runs):
    """Сегодняшняя порция запросов: по кругу, в зависимости от даты (без хранения состояния)."""
    n = len(entries)
    start = (date.today().toordinal() * runs) % n if n else 0
    return [entries[(start + i) % n] for i in range(min(runs, n))]


def fetch_apify_list(src):
    if not turnkey_ok(src):
        raise ValueError("не заданы turnkey.mult и turnkey.add_usd: цена «под ключ» не посчитана, площадка пропущена")
    entries = read_entries(src)
    if not entries:
        raise ValueError("в файле нет подходящих запросов (проверьте enabled_brands)")
    batch = pick_batch(entries, int(src.get("runs_per_day", 3)))
    print(f"   {src['name']}: сегодня {len(batch)} из {len(entries)} запросов: " + "; ".join(e["label"] for e in batch))
    cars, errs, failed, t0 = {}, Counter(), 0, time.time()
    for e in batch:
        if time.time() - t0 > src.get("time_budget_sec", 1500):
            print(f"   время на опрос вышло ({src.get('time_budget_sec', 1500)} с): остальные запросы пропущены")
            break
        try:
            raw = raw_items(src, e["subs"])
        except Exception as ex:
            failed += 1
            print(f"   ⚠️ {src['name']} · {e['label']}: {str(ex)[:160]}")
            continue
        got = 0
        for r in raw:
            try:
                c = normalize(r, src)
            except (KeyError, TypeError, ValueError) as ex:
                errs[str(ex)[:140]] += 1
                continue
            c["market_keys"] = [k for k in e["keys"] if k.rpartition("|")[2] == str(c["year"])]
            cars.setdefault(c["id"], c)
            got += 1
        print(f"   {src['name']} · {e['label']}: получено {len(raw)}, принято {got}")
    for msg, n in errs.most_common(3):
        print(f"   пропущено {n} шт. ({src['name']}): {msg}")
    if not cars and failed == len(batch):
        raise ValueError("ни один запрос не отработал: проверьте APIFY_TOKEN, баланс Apify и вход актора")
    return list(cars.values())


# ---------- прямой парсер mashina.kg (без Apify) ----------
def mashina_page_items(page_html):
    """Достаёт объявления со страницы mashina.kg: сайт встраивает их JSON-ом в скрипты Next.js."""
    parts = []
    for m in re.finditer(r'<script[^>]*>self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', page_html, re.S):
        try:
            parts.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue
    payload, dec, pos = "".join(parts), json.JSONDecoder(), 0
    while True:
        j = payload.find('"items":[', pos)
        if j < 0:
            return []
        try:
            items, _ = dec.raw_decode(payload[j + len('"items":'):])
        except ValueError:
            pos = j + 9
            continue
        if isinstance(items, list) and items and isinstance(items[0], dict) and "slug" in items[0]:
            return items
        pos = j + 9


def mashina_car(it, src, keys):
    """Объявление mashina.kg → машина в общем формате. keys — позиции таблицы цен, для которых открыта ссылка."""
    ap, A = it.get("analytics_params") or {}, {a.get("slug"): a for a in it.get("attributes") or []}
    usd = next((p.get("amount") for p in it.get("prices") or [] if p.get("currency") == "USD"), None) or ap.get("price_usd")
    year = parse_year(ap.get("year") or (A.get("year") or {}).get("value_number"))
    if not usd or not year or not it.get("slug"):
        return None
    if (it.get("availability") or "") in src.get("exclude_availability", []) or (ap.get("city") or "") in src.get("exclude_cities", []):
        return None
    tk = src["turnkey"]
    price = round((usd * src.get("price_mult", 1) * tk["mult"] + tk["add_usd"]) / 10) * 10
    title = " ".join(str(it.get("title") or "").split())[:120]
    low = title.lower()
    fuel = ("электро" if "electro" in low else "гибрид" if re.search(r"\dhyb|hybrid", low) else
            "дизель" if re.search(r"\d\.\dd(?![a-z])", low) else "")
    vol = (A.get("engine_volume") or {}).get("value_number")  # у гибридов бывает ёмкость батареи в кВт·ч: отсекаем
    cc = round(vol * 1000) if isinstance(vol, (int, float)) and 0.6 <= vol <= 8.5 else None
    km = (A.get("mileage") or {}).get("value_number")
    steer = (A.get("steering_wheel") or {}).get("value_text") or ""
    notes = [f"🚘 Руль: {steer.lower()}"] if steer and steer.lower() != "слева" else []
    photos = [im.get("big") or im.get("medium") for im in it.get("images") or []]
    return {
        "src": src["name"], "id": f"{src['name']}:{it['id']}", "title": title, "make": "", "model": "",
        "price": price, "year": year, "price_src": usd, "currency_src": "USD", "rate": None, "price_usd": round(usd),
        "country": src.get("country", ""), "km": float(km) if isinstance(km, (int, float)) else None,
        "url": f"https://mashina.kg/details/{it['slug']}", "photos": [p for p in photos if p][:CFG.get("max_photos", 10)],
        "engine": engine_text(cc, fuel) or fuel, "engine_cc": cc, "fuel": fuel,
        "transmission": (A.get("gearbox") or {}).get("value_text") or "", "drive": "", "color": "", "notes": notes,
        "market_keys": [k for k in keys if k.rpartition("|")[2] == str(year)], "upload_photos": True,
    }


def mashina_links(src):
    """Ссылки поиска из файла «ключ таблицы — ссылка» и из списка urls: {ссылка: [ключи таблицы]}."""
    links = {}
    path = src.get("links_file")
    if not (path and os.path.exists(path)):  # файл загружен под другим именем (например, «… (1).txt»)
        found = sorted(glob.glob("*links*.txt"))
        path = found[0] if found else None
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                key, sep, url = ln.strip().partition(" — ")
                if not sep:
                    key, url = "", ln.strip()
                if url.startswith("http"):
                    links.setdefault(url.strip(), [])
                    if key.strip():
                        links[url.strip()].append(key.strip())
    for u in src.get("urls", []):
        links.setdefault(u, [])
    return links


def fetch_mashina(src):
    if not turnkey_ok(src):
        raise ValueError("не заданы turnkey.mult и turnkey.add_usd: цена «под ключ» не посчитана, площадка пропущена")
    links = list(mashina_links(src).items())
    if not links:
        raise ValueError("нет ссылок поиска: загрузите файл mashina_kg_search_links_228.txt в корень репозитория (рядом с main.py) или впишите ссылки в urls в config.json")
    random.Random(date.today().isoformat()).shuffle(links)  # каждый день другой порядок, если не хватит времени на все
    if src.get("max_links"):
        links = links[: int(src["max_links"])]
    cars, st, t0 = {}, Counter(), time.time()
    for n, (url, keys) in enumerate(links, 1):
        if time.time() - t0 > src.get("time_budget_sec", 900):
            print(f"   время на опрос вышло ({src.get('time_budget_sec', 900)} с): обработано {n - 1} из {len(links)} ссылок")
            break
        for page in range(1, int(src.get("pages", 1)) + 1):
            u = re.sub(r"([?&]page=)\d+", rf"\g<1>{page}", url) if page > 1 else url
            try:
                r = requests.get(u, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}, timeout=30)
                if r.status_code in (403, 429):
                    print(f"   ⚠️ сайт ограничил доступ (HTTP {r.status_code}) на ссылке {n} из {len(links)}: остановка, получено {len(cars)}")
                    return list(cars.values())
                r.raise_for_status()
                items = mashina_page_items(r.text)
            except Exception as e:
                st["ошибок"] += 1
                if st["ошибок"] <= 3:
                    print(f"   ⚠️ ссылка {n}: {str(e)[:120]}")
                break
            st["страниц"] += 1
            for it in items:
                c = mashina_car(it, src, keys)
                if c:
                    cars.setdefault(c["id"], c)
                else:
                    st["пропущено (в пути/нет цены)"] += 1
            if len(items) < 20:  # последняя страница
                break
        time.sleep(src.get("delay_sec", 1.0))
    print(f"   mashina.kg: страниц {st['страниц']}, ошибок {st['ошибок']}, пропущено {st['пропущено (в пути/нет цены)']}, уникальных машин {len(cars)}")
    if not cars and st["ошибок"]:
        raise ValueError("не удалось получить ни одной страницы: возможно, сайт блокирует запросы с серверов GitHub")
    return list(cars.values())


# ---------- фильтр, рынок, отбор ----------
def price_ok(c):
    """Можно ли публиковать цену: «под ключ» посчитана, а для аукционных площадок есть цена аукциона."""
    if not c.get("price") or c["price"] <= 0:
        return False
    if src_cfg(c["src"]).get("price_mode") == "auction":
        return bool(c.get("price_usd")) and c["price_usd"] > 0 and (c.get("currency_src") or "USD") == "USD"
    return True


def good(c):
    f = CFG["filters"]  # price_usd_max — предельная цена ПОД КЛЮЧ
    return (price_ok(c) and c["year"] >= f.get("year_min", 0)
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
    # машина найдена по ссылке поиска, привязанной к строкам таблицы: берём их; если строк несколько
    # (поколения), выбираем самую конкретную, чьи слова есть в названии, иначе самую осторожную (низкую) цену
    table = {k.lower(): v for k, v in MARKET.get("prices", {}).items() if isinstance(v, (int, float)) and v > 0}
    cand = sorted(((k, table[k.lower()]) for k in c.get("market_keys", []) if k.lower() in table),
                  key=lambda kv: -len(tokens(kv[0].partition("|")[0])))
    if cand:
        for k, v in cand:
            it = iter(desc)
            if all(t in it for t in tokens(k.partition("|")[0])):
                return v
        return min(v for _, v in cand)
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
        auto = MARKET.get("auto", {})
        if look["n"] >= MARKET.get("max_lookups", 5) or todo(auto.get("url", auto.get("actor"))):
            return None
        look["n"] += 1
        make, model = ident(c)
        try:
            raw = raw_items(auto, {"make": make, "model": model, "year": c["year"], "query": f"{make} {model}".strip()})
        except Exception as e:
            print(f"⚠️ рынок для {k}: {e}")
            return None
        fl, mult, ps = auto["fields"], auto.get("price_mult", 1), []
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


def select(cands, n, per_src, already=None):
    """Выбирает n лучших: каждый раз берёт лучшее с той площадки, которой в этот день выложено меньше всего
    (не более per_src в сутки с одной площадки). already — сколько уже выложено сегодня по площадкам."""
    by = {}
    for c in sorted(cands, key=lambda c: -c["score"]):
        by.setdefault(c["src"], []).append(c)
    cnt, picked = Counter(already or {}), []
    while len(picked) < n:
        avail = [s for s, lst in by.items() if lst and cnt[s] < per_src]
        if not avail:
            break
        s = min(avail, key=lambda s: cnt[s])
        picked.append(by[s].pop(0))
        cnt[s] += 1
    return picked


def batch_quota(left, now):
    """Сколько постов выложить в этом запуске: остаток дня делится поровну на оставшиеся окна post_times."""
    slots = []
    for t in CFG.get("post_times", []):
        h, m = map(int, t.split(":"))
        slots.append(h * 60 + m)
    if not slots:
        return left
    cur = now.hour * 60 + now.minute
    ahead = [s for s in slots if s >= cur - CFG.get("slot_tolerance_min", 45)]
    return min(left, -(-left // max(1, len(ahead))))


# ---------- текст по шаблону ----------
def render(c):
    s = src_cfg(c["src"])
    esc = lambda v: "" if todo(v) else html.escape(str(v))
    notes = ""
    if c.get("notes"):
        notes = "\n".join([CFG.get("notes_header", "По предварительной информации:")] + [html.escape(n) for n in c["notes"]])
    ci = country_info(c)
    mode = s.get("price_mode", "turnkey")
    if mode == "auction":  # Copart: цена аукциона, а не «под ключ»
        if not price_ok(c):
            return None
        price_lines = [f"{s.get('price_label', 'Цена на аукционе:')} ❤️ {money(c['price_usd'])}", s.get("price_note", "")]
    else:
        price_lines = [f"{s.get('price_label', 'Цена:')} ❤️ {money(c['price'])}", s.get("price_note", "под ключ до Минска")]
    if s.get("show_source_price") and c.get("currency_src") not in (None, "USD") and c.get("price_src"):
        price_lines.append(f"{s.get('source_price_label', 'Цена на площадке:')} {money(c['price_src'], s.get('source_currency_sign', c['currency_src']))}")
    market = []
    if s.get("market_line", True) and c.get("market") and c.get("disc") and c["disc"] > 0:
        market = [f"📊 Средняя на av.by: ≈{NBSP}{money(c['market'])}", f"✅ Выгода: {c['disc']}{NBSP}%"]
    vals = {
        "header": esc(ci.get("header") or s.get("header")),
        "flags": (ci.get("flag") or "") * int(ci.get("flag_count", CFG.get("flag_count", 10))),
        "note": esc(s.get("note")),
        "title": esc(c["title"]), "year": esc(c["year"]),
        "price_block": "\n".join(html.escape(x) for x in price_lines if x),
        "price_turnkey": money(c["price"]) if mode != "auction" else "",
        "mileage": f"{fmt(c['km'])}{NBSP}км" if c.get("km") is not None else "",
        "engine": esc(str(c.get("engine") or "").replace(" л", NBSP + "л")),
        "transmission": esc(c.get("transmission")), "drive": esc(c.get("drive")), "color": esc(c.get("color")),
        "notes_block": notes, "market_block": "\n".join(html.escape(x) for x in market),
        "market_line": html.escape(market[0]) if market else "", "order_link": esc(CFG.get("order_link")),
    }
    out = []
    for line in s.get("template") or CFG["template"]:
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


def to_jpeg(b):
    """Telegram надёжно принимает JPEG: WEBP (так отдаёт mashina.kg) конвертируем."""
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        try:
            from PIL import Image
            out = io.BytesIO()
            Image.open(io.BytesIO(b)).convert("RGB").save(out, "JPEG", quality=88)
            return out.getvalue()
        except Exception:
            return b
    return b


def download(url):
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": UA})
        if r.ok and r.headers.get("content-type", "").startswith("image") and len(r.content) < 10_000_000:
            return to_jpeg(r.content)
    except requests.RequestException:
        pass
    return None


def send_photos(photos, caption, upload=False):
    """Одно фото или альбом (2–10). Сначала по ссылкам, при отказе Telegram (или upload=True) скачиваем и загружаем сами."""
    cap = {"caption": caption, "parse_mode": "HTML"} if caption else {}
    if len(photos) == 1:
        if not upload and tg("sendPhoto", photo=photos[0], **cap).ok:
            return True
        b = download(photos[0])
        return bool(b) and tg("sendPhoto", files={"photo": ("p.jpg", b)}, **cap).ok
    if not upload:
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
    if not text:
        print(f"⚠️ Пост не собран (нет корректной цены): {c.get('title')}")
        return False
    photos = c.get("photos", [])[:CFG.get("max_photos", 10)]
    caption = text if len(text) <= 1024 else None  # длинный текст уходит отдельным сообщением
    posted = bool(photos) and send_photos(photos, caption, c.get("upload_photos", False))
    if posted and caption:
        return True
    return tg("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview="true").ok


# ---------- основной сценарий ----------
def collect():
    fetched = {}
    for src in CFG["sources"]:
        if not src.get("enabled"):
            continue
        try:
            cars = fetch(src)
            fetched.update({c["id"]: c for c in cars})
            print(f"{src['name']}: получено {len(cars)}")
        except Exception as e:  # одна сломанная площадка не должна остановить остальные
            print(f"⚠️ {src['name']}: {e}")
    return fetched


def main():
    if not DRY and not (TOKEN and CHAT):
        sys.exit("Не заданы TG_TOKEN и TG_CHAT")
    state = load_state()
    now = now_local()
    today = now.date().isoformat()
    rd = CFG.get("customs", {}).get("rates_date")
    if rd and (date.today() - date.fromisoformat(rd)).days > 14:
        print(f"⚠️ Курсы НБРБ в config.json от {rd} устарели: обновите customs.byn_per_usd и customs.byn_per_eur")
    left = CFG["daily_limit"] - (0 if DEMO else state["sent"].get(today, 0))
    if left <= 0:
        print(f"Дневной лимит ({CFG['daily_limit']}) уже исчерпан")
        return
    quota = left if DEMO else batch_quota(left, now)
    seen = set(state["seen"])

    pool = state.get("pool") or {}
    if not DEMO and pool.get("day") == today and pool.get("fetched") and pool.get("v") == POOL_VERSION:
        cands = [c for c in pool.get("cars", []) if c["id"] not in seen]
        print(f"Площадки сегодня уже опрошены, в запасе {len(cands)} подходящих машин")
        if PREFETCH:
            return
    else:
        if not DEMO and any(s.get("enabled") and (s.get("price_currency") or "USD").upper() == "KRW" for s in CFG["sources"]):
            RATES["KRW"] = load_krw_rate(state)
        fetched = {c["id"]: dict(c) for c in DEMO_CARS} if DEMO else collect()
        if not fetched:
            print("Нет данных. Проверьте, что площадки включены в config.json (\"enabled\": true)")
            return
        for c in fetched.values():
            c["score"], c["disc"] = score(c, state["prices"])
        cands = [c for c in fetched.values() if c["id"] not in seen and good(c)]
        for c in fetched.values():  # копим цены для запасной оценки
            state["prices"].setdefault(key(c), {})[c["id"]] = c["price"]
        if MARKET.get("mode", "off") != "off":
            cands = apply_market(cands, state)
        if UNKNOWN_KO:
            print("Не переведены корейские слова (добавьте в config.json → translate):",
                  ", ".join(f"{w} ×{n}" for w, n in UNKNOWN_KO.most_common(15)))
        if not DEMO:
            top = sorted(cands, key=lambda c: -c["score"])[:CFG.get("pool_size", 40)]
            state["pool"] = {"day": today, "fetched": True, "cars": top, "v": POOL_VERSION}
        if PREFETCH:
            if not DRY and not DEMO:
                save_state(state)
            print(f"Предзагрузка: подготовлено {len(cands)} машин, публикация в ближайшее окно")
            return

    already = Counter() if DEMO else Counter(state["sent_src"].get(today, {}))
    picked = select(cands, quota, CFG["per_source_max"], already)
    print(f"Сегодня выложено: {0 if DEMO else CFG['daily_limit'] - left} из {CFG['daily_limit']}. "
          f"Подходит: {len(cands)}, в этом запуске: {len(picked)} (порция {quota})")
    sent = []
    for c in picked:
        if DRY:
            src_p = f"{fmt(c['price_src'])} {c.get('currency_src')}" if c.get("price_src") else "—"
            print(f"— [{c['src']} {country_of(c) or '??'}] {c['title']} · {c['year']} · площадка: {src_p}"
                  + (f" (курс {c['rate']:g})" if c.get("rate") else "")
                  + f" · в $: {c.get('price_usd')} · под ключ: {c['price']} · рынок: {c.get('market')} · выгода: {c['disc']}%"
                  + f" · фото: {len(c.get('photos', []))}")
        elif send(c):
            sent.append(c)
            time.sleep(5)
        else:
            print("⚠️ Не отправилось:", c["title"])
    if picked and not DRY and not sent:  # ни один пост не ушёл: запуск должен быть красным, а не «успешным»
        if not DEMO:
            save_state(state)  # пул дня сохраняем, чтобы следующий запуск не опрашивал площадки заново
        sys.exit("❌ Ни одно сообщение не отправилось: смотрите ошибки Telegram выше (токен, TG_CHAT, права бота в канале)")
    if DRY or DEMO:
        return
    state["seen"] = (state["seen"] + [c["id"] for c in sent])[-5000:]
    state["sent"][today] = state["sent"].get(today, 0) + len(sent)
    per = Counter(state["sent_src"].get(today, {}))
    per.update(c["src"] for c in sent)
    state["sent_src"][today] = dict(per)
    save_state(state)


if __name__ == "__main__":
    main()
