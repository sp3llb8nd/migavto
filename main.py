#!/usr/bin/env python3
"""Автопостинг выгодных авто в Telegram-канал MIGAVTO.

Конвейер: площадка → разбор и проверка → нормализация (цена в $, страна, флаг) → проверка дублей →
база объявлений (state.json) → отбор по рынку av.by → очередь → планировщик (интервалы, рабочие часы) →
отправка в Telegram → запись результата. Каждая площадка обрабатывается независимо.

python main.py --fetch       собрать объявления с площадок, у которых подошёл срок, и поставить в очередь
python main.py --fetch-all   то же, но со всех площадок сразу
python main.py --publish     опубликовать объявление, если подошло его время (запускается каждые 15 минут)
python main.py --status      показать очередь, ближайшие публикации и ошибки
python main.py --dry         собрать со всех площадок и показать, что попадёт в очередь; ничего не сохраняет
python main.py --demo        отправить 3 тестовых поста (по одному на страну); база не меняется
"""
import atexit
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
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
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
UA = "Mozilla/5.0 (compatible; cars-bot)"
HANGUL = r"[\uac00-\ud7a3]+"
UNKNOWN_KO = Counter()

with open("config.json", encoding="utf-8") as f:
    CFG = json.load(f)
MARKET = CFG.get("market", {})
SCHED = CFG.get("schedule", {})
STATE_FILE = "state.json"
LOCK_FILE = "bot.lock"


# ---------- журнал ----------
STATS = defaultdict(Counter)   # воронка по площадкам: где теряются объявления
SUMMARY = []                   # копия журнала для сводки запуска в GitHub
TAGS = {"Encar": "ENCAR", "Copart": "COPART", "mashina.kg": "MASHINA.KG"}


def tag(name):
    return TAGS.get(name) or str(name).upper()


def log(t, msg):
    line = f"[{t}] {msg}"
    print(line, flush=True)
    SUMMARY.append(line)


DEMO_COUNTRY = {"Encar": "KR", "Copart": "US", "mashina.kg": "KG"}


def _demo(src, i, title, make, model, year, km, price, market, engine, notes):
    return {
        "src": src, "id": f"demo:{src}:{i}", "title": title, "make": make, "model": model, "country": DEMO_COUNTRY[src],
        "price_usd": {"Encar": price - 9000, "Copart": price - 8000, "mashina.kg": price - 2600}[src],
        "price_src": None, "currency_src": "USD", "rate": None, "price_kind": "main",
        "costs": {"delivery": 4000, "customs": 4000} if src == "Copart" else None,
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
    tz = SCHED.get("tz") or CFG.get("tz")
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
        log("БАЗА", f"⚠️ Площадка «{c.get('src')}»: страна не задана или её нет в config.json → countries; флаг не ставится")
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
        log("КУРС", "⚠️ В config.json не задан krw_rates.sheet_id")
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
        log("КУРС", f"Курс вон из таблицы: рабочий {work:g}" + (f", реальный {real:g}" if real else "") + f" ({check})")
        return rate
    except Exception as e:
        cached = state.get("krw_rate")
        if cached and (date.today() - date.fromisoformat(cached["day"])).days <= rc.get("max_cache_days", 3):
            log("КУРС", f"⚠️ {e}. Использую последний курс из таблицы от {cached['day']}: {cached['working']:g}")
            return dict(cached, source="кэш")
        manual, mday = rc.get("manual_working_rate"), rc.get("manual_rate_date")
        if manual and mday and lo <= manual <= hi and (date.today() - date.fromisoformat(mday)).days <= rc.get("max_manual_days", 7):
            log("КУРС", f"⚠️ {e}. Таблица недоступна — беру РУЧНОЙ курс из config.json: {manual:g} от {mday}")
            return {"working": float(manual), "real": None, "day": mday, "source": "ручной"}
        log("КУРС", f"⚠️ Курс вон не получен: {e}. Encar в этом запуске не опрашивается (чтобы не тратить Apify)")
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
    """База объявлений. Старый формат (список seen и запас дня) переносится: опубликованное остаётся опубликованным."""
    state = {"schema": 3, "items": {}, "fetch": {}, "prices": {}, "market": {}, "legacy_sent": {}}
    if not os.path.exists(STATE_FILE):
        return state
    with open(STATE_FILE, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("schema") == 3:
        state.update(data)
        return state
    for k in ("prices", "market", "krw_rate"):
        if k in data:
            state[k] = data[k]
    for i in data.get("seen", []):
        state["items"][i] = {"id": i, "src": i.split(":")[0], "status": "published", "published_at": None,
                             "attempts": 1, "note": "перенесено из старой версии бота"}
    state["legacy_sent"] = data.get("sent", {})
    log("БАЗА", f"Старый формат состояния перенесён: {len(state['items'])} опубликованных объявлений")
    return state


def save_state(state):
    """Запись атомарно (через временный файл): при сбое старая версия не портится."""
    keep = SCHED.get("skipped_keep_days", 3)
    cut = (date.today() - timedelta(days=keep)).isoformat()
    state["items"] = {k: r for k, r in state["items"].items()
                      if not (r.get("status") == "skipped" and (r.get("processed_at") or "")[:10] < cut)}
    state["prices"] = {k: dict(list(v.items())[-300:]) for k, v in state["prices"].items()}
    state["legacy_sent"] = dict(sorted(state.get("legacy_sent", {}).items())[-7:])
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


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


APIFY_API = "https://api.apify.com/v2"


def apify_error(r):
    try:
        e = r.json().get("error") or {}
        return f"Apify HTTP {r.status_code}: {e.get('type', '')} {e.get('message', '')}".strip()
    except ValueError:
        return f"Apify HTTP {r.status_code}: {r.text[:200]}"


def apify_start(actor, inp, timeout_sec=600):
    """Запуск актора без ожидания (run-sync обрывается через 300 с, а резидентные прокси бывают медленнее)."""
    if not APIFY:
        raise ValueError("не задан секрет APIFY_TOKEN")
    r = requests.post(f"{APIFY_API}/acts/{actor}/runs", params={"token": APIFY, "timeout": int(timeout_sec)},
                      json=inp, timeout=60)
    if not r.ok:
        raise ValueError(apify_error(r))
    return r.json()["data"]


def apify_wait(run, deadline):
    while run.get("status") in ("READY", "RUNNING") and time.time() < deadline:
        try:
            r = requests.get(f"{APIFY_API}/actor-runs/{run['id']}", params={"token": APIFY, "waitForFinish": 60}, timeout=90)
            if r.ok:
                run = r.json()["data"]
                continue
        except requests.RequestException:
            pass
        time.sleep(10)
    return run


def apify_abort(run):
    try:
        requests.post(f"{APIFY_API}/actor-runs/{run['id']}/abort", params={"token": APIFY}, timeout=30)
    except requests.RequestException:
        pass


def apify_items(run):
    r = requests.get(f"{APIFY_API}/datasets/{run['defaultDatasetId']}/items",
                     params={"token": APIFY, "clean": "true", "format": "json"}, timeout=120)
    if not r.ok:
        raise ValueError(apify_error(r))
    return r.json() or []


def apify_budget():
    """Сколько потрачено из месячного лимита Apify: частая причина «тишины» Encar и Copart."""
    try:
        r = requests.get(f"{APIFY_API}/users/me/limits", params={"token": APIFY}, timeout=30)
        d = r.json().get("data", {}) if r.ok else {}
        used, lim = pick(d, "current.monthlyUsageUsd"), pick(d, "limits.maxMonthlyUsageUsd")
        if isinstance(used, (int, float)) and isinstance(lim, (int, float)) and lim:
            log("APIFY", f"Израсходовано {used:.2f} $ из {lim:.2f} $ в этом месяце"
                + (" — ⚠️ лимит почти исчерпан, запуски Encar и Copart будут отклоняться" if used >= lim * 0.9 else ""))
            return used, lim
        if not r.ok:
            log("APIFY", f"⚠️ Не удалось проверить баланс: {apify_error(r)}")
    except Exception as e:
        log("APIFY", f"⚠️ Не удалось проверить баланс: {str(e)[:120]}")
    return None


def run_apify(actor, inp, budget_sec, label=""):
    run = apify_start(actor, inp, budget_sec)
    run = apify_wait(run, time.time() + budget_sec + 60)
    if run.get("status") in ("READY", "RUNNING"):
        apify_abort(run)
    items = apify_items(run)
    if run.get("status") != "SUCCEEDED" and not items:
        raise ValueError(f"запуск {run.get('status')}: {run.get('statusMessage') or ''}")
    return items


def raw_items(src, subs=None):
    subs = subs or {}
    if src["type"] in ("apify", "apify_list"):
        data = run_apify(src["actor"], subst(src.get("input", {}), subs), src.get("run_timeout_sec", 600))
        return (pick(data, src["items_path"]) if src.get("items_path") else data) or []
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
        v = int(v)
        if 190001 <= v <= 210012 and 1 <= v % 100 <= 12:  # ГГГГММ (Encar: 202105 = май 2021)
            return v // 100
        return v if 1900 <= v <= 2100 else None
    if re.fullmatch(r"\s*(?:19|20)\d{2}(0[1-9]|1[0-2])\s*", str(v)):
        return int(str(v).strip()[:4])
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


def parse_date(v):
    """Дата торгов: «2026-11-20T…», «2026-11-20», метка времени в мс или с → «2026-11-20»."""
    if v in (None, "", 0) or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e11 else v
        try:
            return datetime.utcfromtimestamp(ts).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", str(v))
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def engine_text(cc, fuel, est=False):
    return (("≈" if est else "") + f"{cc / 1000:.1f} л" + (f", {fuel}" if fuel else "")) if cc else ""


DIESEL_WORDS = ("дизел", "diesel", "디젤")


def default_cc(src, text, fuel):
    """Типовой объём двигателя модели, если площадка его не дала (Encar в списке объявлений объём не отдаёт).
    Правила из config.json → engine_defaults: [«марка модель», «diesel»/«petrol»/null, см³]; первое совпадение."""
    t = " ".join(str(text or "").lower().replace("-", " ").split())
    diesel = any(w in str(fuel or "").lower() for w in DIESEL_WORDS)
    for rule in src.get("engine_defaults") or CFG.get("engine_defaults") or []:
        match, kind, cc = rule
        if match.lower().replace("-", " ") in t and (kind is None or (kind == "diesel") == diesel):
            return cc
    return None


def vin_fill(raws, src, t):
    """Copart: у части лотов поле engine пустое или без объёма. Объём берём по VIN из бесплатной базы NHTSA (США)."""
    vf = src.get("vin_field")
    if not vf:
        return
    need = {}
    for r in raws:
        if isinstance(r, dict) and not engine_cc(pick(r, src["fields"].get("engine_cc"))):
            vin = str(next((pick(r, k) for k in ([vf] if isinstance(vf, str) else vf) if pick(r, k)), "") or "").strip().upper()
            if len(vin) == 17 and "*" not in vin:
                need.setdefault(vin, []).append(r)
    if not need:
        return
    found, vins = 0, list(need)
    for i in range(0, len(vins), 50):
        try:
            resp = requests.post("https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVINValuesBatch/",
                                 data={"format": "json", "data": ";".join(vins[i:i + 50])}, timeout=60)
            rows = resp.json().get("Results") or []
        except Exception as ex:
            log(t, f"⚠️ Расшифровка VIN (NHTSA) не удалась: {str(ex)[:150]}")
            return
        for row in rows:
            cc = engine_cc(row.get("DisplacementCC")) or engine_cc(row.get("DisplacementL"))
            for r in need.get(str(row.get("VIN") or "").upper(), []):
                if cc:
                    r["_vin_cc"] = cc
                    found += 1
    log(t, f"Объём двигателя по VIN: найден у {found} из {sum(map(len, need.values()))}")


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
    price_src, year, kind = parse_number(get("price")), parse_year(get("year")), "main"
    if (not price_src or price_src <= 0) and f.get("price_alt"):  # запасное поле: манвоны (Encar), «купить сейчас» (Copart)
        alt = parse_number(pick(raw, f["price_alt"]))
        price_src, kind = (alt * f.get("price_alt_mult", 1), "alt") if alt else (None, kind)
    cur = (src.get("price_currency") or "USD").upper()
    if cur != "USD" and src.get("price_mult", 1) != 1:
        raise ValueError(f"в config.json у «{src['name']}» одновременно price_currency={cur} и price_mult: двойная конвертация")
    if not year:
        raise ValueError("год не распознан")
    if not price_src or price_src <= 0:
        raise ValueError("нет цены (у лота Copart нет ни ставки, ни цены «купить сейчас»)")
    if price_src in (src.get("invalid_prices") or []) or price_src < src.get("min_price", 0):
        raise ValueError(f"цена {fmt(price_src)} {cur} похожа на заглушку площадки: объявление пропущено")
    tk = src["turnkey"]
    usd, rate = to_usd(price_src * src.get("price_mult", 1), cur, src)  # перевод в доллары: один раз
    ccf = f.get("engine_cc")
    cc = next((x for x in (engine_cc(pick(raw, p)) for p in ccf) if x), None) if isinstance(ccf, list) else engine_cc(get("engine_cc"))
    fuel = vmap(tr(txt("fuel")))
    cc = cc or engine_cc(raw.get("_vin_cc")) if isinstance(raw, dict) else cc
    est = False
    if not cc and (fuel or "").strip().lower() not in EV_WORDS:
        cc = default_cc(src, " ".join((txt("make"), txt("model"), txt("title"), tr(txt("make")), tr(txt("model")))), fuel)
        est = bool(cc)
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
    notes, auction = [], parse_date(pick(raw, f.get("auction_date"))) if f.get("auction_date") else None
    if auction:
        notes.append(f"📅 Торги: {auction[8:10]}.{auction[5:7]}.{auction[:4]}")
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
        "price_src": price_src, "currency_src": cur, "rate": rate, "price_usd": round(usd), "price_kind": kind,
        "rate_source": (RATES.get("KRW") or {}).get("source") if rate else None,
        "costs": {"delivery": tk["add_usd"], "customs": round(extra)} if tk.get("customs") else None,
        "auction_date": auction, "country": src.get("country", ""),
        "km": None if km is None else km * src.get("km_mult", 1), "url": txt("url"), "photos": photos,
        "engine": engine_text(cc, fuel, est) if est else (tr(txt("engine")) or engine_text(cc, fuel)), "engine_cc": cc,
        "engine_est": est, "fuel": fuel,
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
        STATS[src["name"]]["разбор: " + reason_cat(msg)] += n
    STATS[src["name"]]["получено"] += len(cars) + sum(errs.values())
    return cars


def reason_cat(msg):
    """Короткая причина отказа для воронки."""
    m = str(msg).lower()
    for k, cat in (("курс", "нет курса вон"), ("объём", "нет объёма двигателя"), ("заглуш", "цена-заглушка"),
                   ("нет цены", "нет цены или ставки"), ("год", "нет года"), ("не настроены поля", "поля не совпали с ответом площадки"),
                   ("двойная", "ошибка настройки валюты"), ("валют", "валюта не поддерживается")):
        if k in m:
            return cat
    return "другое: " + str(msg)[:60]


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
        extra = src.get("links_file")  # дополнительные ссылки поиска (для Encar: www.encar.com)
        if extra and os.path.exists(extra):
            with open(extra, encoding="utf-8") as f2:
                text += "\n" + f2.read()
        for ln in text.splitlines():
            key, sep, url = ln.strip().partition(" — ")
            if sep and url.startswith("http"):
                by_url.setdefault(url.strip(), []).append(key.strip())
        for url, keys in by_url.items():
            entries.append({"label": keys[0], "brand": keys[0].split()[0], "keys": keys, "subs": {"url": copart_url(url, src)}})
    else:
        names = src.get("names", {})  # название из файла → как его понимает поиск Encar («5 Series G30» → «5 Series»)
        groups = {}
        for brand, model, yf, yt, ctype in ENCAR_REC.findall(" ".join(text.split())):
            model = model.strip()
            mb, mm = names.get(brand, brand), names.get(model, model)
            g = groups.setdefault((mb, mm), {"years": set(), "ctype": ctype, "brand": brand, "keys": set()})
            g["years"].update(range(int(yf), int(yt) + 1))
            g["keys"].update(f"{brand} {model}|{y}".lower() for y in range(int(yf), int(yt) + 1))
        for (mb, mm), g in groups.items():
            ys = sorted(g["years"])
            spans = [(ys[0], ys[-1])] if src.get("group_years", True) else [(y, y) for y in ys]
            for y0, y1 in spans:
                keys = sorted(k for k in g["keys"] if y0 <= int(k.rpartition("|")[2]) <= y1)
                entries.append({"label": f"{mb} {mm} {y0}" + (f"–{y1}" if y1 != y0 else ""), "brand": g["brand"], "keys": keys,
                                "subs": {"manufacturer": mb, "model": mm, "yearFrom": y0, "yearTo": y1, "carType": g["ctype"]}})
    ymin = (CFG.get("filters") or {}).get("year_min") or CFG.get("year_min")
    if ymin:  # запросы только по годам ниже минимального — пустая трата Apify: все машины всё равно отсеются
        entries = [e for e in entries if not e["keys"] or any(int(k.rpartition("|")[2]) >= ymin for k in e["keys"]
                                                              if k.rpartition("|")[2].isdigit())]
    ob = {b.lower() for b in src.get("enabled_brands", [])}
    entries = [e for e in entries if not ob or e["brand"].lower() in ob or "url" in e["subs"]]
    if src.get("links_file") and src.get("entries_format") != "links" and os.path.exists(src["links_file"]):
        entries += read_entries(dict(src, entries_file=src["links_file"], entries_format="links", links_file=None))
    return entries


def copart_url(url, src):
    """Актор Copart понимает только ссылки поиска /lotSearchResults и /lot/…; ссылки вида
    /vehicle-search-year/ford/mustang/2020 он не разбирает и отдаёт одни и те же лоты без учёта года.
    Переводим их в поиск по словам «2020 ford mustang»."""
    if not src.get("rewrite_copart_links"):
        return url
    m = re.search(r"/vehicle-search-year/([^/?#]+)/([^/?#]+)/(\d{4})", url)
    if not m:
        return url
    make, model, year = (requests.utils.unquote(x).replace("-", " ") for x in m.groups())
    return "https://www.copart.com/lotSearchResults?free=true&query=" + requests.utils.quote(f"{year} {make} {model}")


def pick_batch(entries, runs):
    """Сегодняшняя порция запросов: по кругу, в зависимости от даты (без хранения состояния)."""
    n = len(entries)
    start = (date.today().toordinal() * runs) % n if n else 0
    return [entries[(start + i) % n] for i in range(min(runs, n))]


def fetch_apify_list(src):
    name, t = src["name"], tag(src["name"])
    if not turnkey_ok(src):
        raise ValueError("не заданы turnkey.mult и turnkey.add_usd: цена «под ключ» не посчитана, площадка пропущена")
    entries = read_entries(src)
    if not entries:
        raise ValueError("в файле нет подходящих запросов (проверьте enabled_brands)")
    batch = pick_batch(entries, int(src.get("runs_per_day", 3)))
    log(t, f"Запросов сегодня: {len(batch)} из {len(entries)} — " + "; ".join(e["label"] for e in batch))
    budget = src.get("time_budget_sec", 1500)
    deadline, runs, failed = time.time() + budget, [], 0
    for e in batch:  # все запуски стартуют сразу и работают параллельно
        run_src = dict(src, input=src["input_links"]) if "url" in e["subs"] and src.get("input_links") else src
        try:
            runs.append((e, apify_start(src["actor"], subst(run_src.get("input", {}), e["subs"]), src.get("run_timeout_sec", 600))))
        except Exception as ex:
            failed += 1
            log(t, f"⚠️ {e['label']}: запуск не удался — {str(ex)[:200]}")
            if any(w in str(ex) for w in ("402", "not-enough-usage", "usage-limit", "monthly-usage")):
                log(t, "⚠️ Похоже, исчерпан лимит Apify: остальные запросы не запускаю")
                failed = len(batch) - len(runs)
                break
    cars, errs = {}, Counter()
    for e, run in runs:
        run = apify_wait(run, deadline)
        status = run.get("status")
        if status in ("READY", "RUNNING"):
            apify_abort(run)
            log(t, f"⚠️ {e['label']}: не уложился в {budget} с — остановлен, беру то, что успел собрать")
        try:
            raw = apify_items(run)
        except Exception as ex:
            failed += 1
            log(t, f"⚠️ {e['label']}: не удалось забрать результаты — {str(ex)[:200]}")
            continue
        if status not in ("SUCCEEDED", "READY", "RUNNING"):
            log(t, f"⚠️ {e['label']}: запуск завершился со статусом {status}: {str(run.get('statusMessage') or '')[:200]}")
            if not raw:
                failed += 1
                continue
        STATS[name]["получено"] += len(raw)
        vin_fill(raw, src, t)
        got = 0
        for r in raw:
            try:
                c = normalize(r, src)
            except (KeyError, TypeError, ValueError) as ex:
                errs[reason_cat(ex)] += 1
                if isinstance(ex, KeyError) and errs[reason_cat(ex)] == 1:
                    log(t, f"⚠️ {str(ex)[:300]}")
                continue
            c["market_keys"] = [k for k in e["keys"] if k.rpartition("|")[2] == str(c["year"])]
            cars.setdefault(c["id"], c)
            got += 1
        log(t, f"{e['label']}: получено {len(raw)}, разобрано {got}")
    for cat, n in errs.items():
        STATS[name]["разбор: " + cat] += n
    if not cars and failed and failed >= len(batch):
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


def mashina_in_kg(it, src):
    """Объявление с mashina.kg — машина, которая уже в Кыргызстане и в наличии. Перекупы с машинами в США, Китае,
    Корее (город «США», «Китай», статус «В пути», фото аукционов) отсеиваются: они не из Кыргызстана."""
    ap, A = it.get("analytics_params") or {}, {a.get("slug"): a for a in it.get("attributes") or []}
    avail = (it.get("availability") or "").strip()
    need = src.get("require_availability")
    if need and avail not in need:
        return False
    if avail in src.get("exclude_availability", []):
        return False
    city = {str((A.get("city") or {}).get("value_text") or "").strip().lower(), str(ap.get("city") or "").strip().lower()}
    if city & {c.lower() for c in src.get("exclude_cities", [])}:
        return False
    text = f"{it.get('title') or ''} {it.get('description') or ''}".lower()
    return not any(w.lower() in text for w in src.get("exclude_words", []))


def mashina_car(it, src, keys):
    """Объявление mashina.kg → машина в общем формате. keys — позиции таблицы цен, для которых открыта ссылка."""
    ap, A = it.get("analytics_params") or {}, {a.get("slug"): a for a in it.get("attributes") or []}
    usd = next((p.get("amount") for p in it.get("prices") or [] if p.get("currency") == "USD"), None) or ap.get("price_usd")
    year = parse_year(ap.get("year") or (A.get("year") or {}).get("value_number"))
    if not usd or not year or not it.get("slug"):
        STATS[src["name"]]["разбор: нет цены или года"] += 1
        return None
    if not mashina_in_kg(it, src):
        STATS[src["name"]]["разбор: машина не в Кыргызстане или не в наличии"] += 1
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
    if isinstance(km, (int, float)) and 0 < km < 1000 and year and date.today().year - year >= 2:
        km *= 1000  # продавцы часто пишут пробег в тысячах («108» = 108 000 км); у машины старше года 108 км не бывает
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
    cars, st, t0, t = {}, Counter(), time.time(), tag(src["name"])
    log(t, f"Ссылок поиска: {len(links)}")
    for n, (url, keys) in enumerate(links, 1):
        if time.time() - t0 > src.get("time_budget_sec", 900):
            log(t, f"⚠️ Время на опрос вышло ({src.get('time_budget_sec', 900)} с): обработано {n - 1} из {len(links)} ссылок")
            break
        for page in range(1, int(src.get("pages", 1)) + 1):
            u = re.sub(r"([?&]page=)\d+", rf"\g<1>{page}", url) if page > 1 else url
            try:
                r = requests.get(u, headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"}, timeout=30)
                if r.status_code in (403, 429):
                    log(t, f"⚠️ Сайт ограничил доступ (HTTP {r.status_code}) на ссылке {n} из {len(links)}: остановка, получено {len(cars)}")
                    STATS[src["name"]]["получено"] += st["объявлений"]
                    return list(cars.values())
                r.raise_for_status()
                items = mashina_page_items(r.text)
            except Exception as e:
                st["ошибок"] += 1
                if st["ошибок"] <= 3:
                    log(t, f"⚠️ Ссылка {n}: {str(e)[:120]}")
                break
            st["страниц"] += 1
            for it in items:
                st["объявлений"] += 1
                c = mashina_car(it, src, keys)
                if c:
                    cars.setdefault(c["id"], c)
            if len(items) < 20:  # последняя страница
                break
        time.sleep(src.get("delay_sec", 1.0))
    STATS[src["name"]]["получено"] += st["объявлений"]
    log(t, f"Страниц {st['страниц']}, ошибок {st['ошибок']}, объявлений {st['объявлений']}, уникальных машин {len(cars)}")
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
            log("AV.BY", f"⚠️ Рынок для {k}: {e}")
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
        alt = c.get("price_kind") == "alt"
        price_lines = [f"{s.get('price_label_alt' if alt else 'price_label', 'Цена на аукционе:')} {CFG.get('price_icon', '')}{money(c['price_usd'])}",
                       s.get("price_note_alt" if alt else "price_note", "")]
    else:
        price_lines = [f"{s.get('price_label', 'Цена:')} {CFG.get('price_icon', '')}{money(c['price'])}", s.get("price_note", "под ключ до Минска")]
    if s.get("show_source_price") and c.get("currency_src") not in (None, "USD") and c.get("price_src"):
        price_lines.append(f"{s.get('source_price_label', 'Цена на площадке:')} {money(c['price_src'], s.get('source_currency_sign', c['currency_src']))}")
    costs = []
    if mode == "auction" and s.get("show_costs") and c.get("costs"):
        costs = [s.get("costs_header", "Расходы до Минска (оценка):"), f"• доставка, оформление: {money(c['costs']['delivery'])}"]
        if c["costs"].get("customs"):
            costs.append(f"• растаможка: ≈{NBSP}{money(c['costs']['customs'])}")
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
        "costs_block": "\n".join(html.escape(x) for x in costs),
        "source_line": html.escape(f"🔎 Источник: {s.get('source_label') or c['src']}") if s.get("show_source", True) else "",
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
def tg_result(**kw):
    r = {"ok": False, "ids": [], "error": "", "retry_after": 0, "uncertain": False, "fatal": False, "skip": False,
         "temporary": False}
    r.update(kw)
    return r


def tg(method, files=None, **data):
    """Один вызов Telegram Bot API. Ответ — словарь:
    ok и ids (message_id) — успех; retry_after — Telegram просит подождать дольше разумного;
    uncertain — ответ не получен, сообщение могло уйти (повторять нельзя, иначе дубль);
    fatal — ошибка настроек (токен, чат, права бота)."""
    for attempt in range(2):
        try:
            r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data={"chat_id": CHAT, **data},
                              files=files, timeout=(15, 120))
        except requests.exceptions.ConnectTimeout as e:
            return tg_result(error=f"нет соединения с Telegram: {str(e)[:120]}")
        except requests.exceptions.ConnectionError as e:
            msg = str(e)
            if any(w in msg for w in ("NewConnectionError", "Name or service not known", "Failed to resolve", "Connection refused")):
                return tg_result(error=f"нет соединения с Telegram: {msg[:120]}")
            return tg_result(error=f"связь оборвалась во время отправки: {msg[:120]}", uncertain=True)
        except requests.exceptions.Timeout as e:
            return tg_result(error=f"Telegram не ответил вовремя: {str(e)[:120]}", uncertain=True)
        except requests.RequestException as e:
            return tg_result(error=f"ошибка запроса к Telegram: {str(e)[:120]}")
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 429:
            ra = int(pick(body, "parameters.retry_after") or 5)
            if attempt == 0 and ra <= SCHED.get("max_retry_after_wait_sec", 60):
                log("TELEGRAM", f"Ограничение частоты Telegram: жду {ra} с")
                time.sleep(ra + 1)
                continue
            return tg_result(error=f"ограничение частоты Telegram: ждать {ra} с", retry_after=ra)
        if r.ok and body.get("ok", True):
            res = body.get("result")
            ids = [m.get("message_id") for m in res if isinstance(m, dict)] if isinstance(res, list) else \
                ([res.get("message_id")] if isinstance(res, dict) else [])
            return tg_result(ok=True, ids=ids)
        desc = str(body.get("description") or r.text[:200])
        low = desc.lower()
        fatal = r.status_code in (401, 403, 404) or any(w in low for w in ("chat not found", "not enough rights", "bot was kicked"))
        log("TELEGRAM", f"⚠️ {method}: {r.status_code} {desc}")
        return tg_result(error=f"{method}: {r.status_code} {desc}", fatal=fatal, temporary=r.status_code >= 500)
    return tg_result(error="ограничение частоты Telegram не снялось")


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


def _stop(r):
    """После такого ответа нельзя пробовать запасной способ: можно задвоить пост или упереться в лимит."""
    return r["ok"] or r["uncertain"] or r["retry_after"] or r["fatal"] or r["temporary"]


def send_photos(photos, caption, upload=False):
    """Одно фото или альбом (2–10). Сначала по ссылкам; если Telegram не смог их скачать — скачиваем и загружаем сами."""
    cap = {"caption": caption, "parse_mode": "HTML"} if caption else {}
    if len(photos) == 1:
        if not upload:
            r = tg("sendPhoto", photo=photos[0], **cap)
            if _stop(r):
                return r
        b = download(photos[0])
        return tg("sendPhoto", files={"photo": ("p.jpg", b)}, **cap) if b else tg_result(error="фото не скачалось")
    if not upload:
        media = [{"type": "photo", "media": u} for u in photos]
        if cap:
            media[0].update(cap)
        r = tg("sendMediaGroup", media=json.dumps(media))
        if _stop(r):
            return r
    files, media = {}, []
    for i, u in enumerate(photos):
        b = download(u)
        if b:
            files[f"p{i}"] = (f"p{i}.jpg", b)
            media.append({"type": "photo", "media": f"attach://p{i}"})
    if not media:
        return tg_result(error="ни одно фото не скачалось")
    if len(media) == 1:
        return tg("sendPhoto", files={"photo": next(iter(files.values()))}, **cap)
    if cap:
        media[0].update(cap)
    return tg("sendMediaGroup", files=files, media=json.dumps(media))


def send(c):
    """Публикация одного объявления: альбом с подписью; длинный текст — отдельным сообщением; без фото — только текст."""
    text = render(c)
    if not text:
        return tg_result(error="пост не собран: нет корректной цены", skip=True)
    photos = c.get("photos", [])[:CFG.get("max_photos", 10)]
    caption = text if len(text) <= 1024 else None
    ids = []
    if photos:
        r = send_photos(photos, caption, c.get("upload_photos", False))
        if r["ok"]:
            ids = r["ids"]
            if caption:
                return r
        elif _stop(r):
            return r
        else:
            log("TELEGRAM", f"Фото не отправились ({r['error']}), публикую текстом")
    r = tg("sendMessage", text=text, parse_mode="HTML", disable_web_page_preview="true")
    r["ids"] = ids + r["ids"]
    if not r["ok"] and ids:  # альбом уже в канале, повтор задвоил бы его
        r.update(ok=True, error="альбом опубликован, отдельный текст не отправился: " + r["error"])
    return r


# ---------- время и рабочие часы ----------
def iso(dt):
    return dt.isoformat(timespec="seconds") if dt else None


def parse_dt(s, tzinfo=None):
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    return dt.replace(tzinfo=tzinfo) if dt.tzinfo is None and tzinfo else dt


def _hm(s):
    h, m = str(s).split(":")
    return int(h), int(m)


def window(day, tzinfo):
    """Рабочее окно публикаций на дату: (начало, конец)."""
    sh, sm = _hm(SCHED.get("start", "09:00"))
    eh, em = _hm(SCHED.get("end", "21:30"))
    return (datetime(day.year, day.month, day.day, sh, sm, tzinfo=tzinfo),
            datetime(day.year, day.month, day.day, eh, em, tzinfo=tzinfo))


def working_day(day):
    return day.isoweekday() in SCHED.get("days", [1, 2, 3, 4, 5, 6, 7])


def src_rule(name, key, default):
    return (src_cfg(name).get("schedule") or {}).get(key, default)


# ---------- база объявлений: проверка, дубли, очередь ----------
def fingerprint(c):
    """Отпечаток для переподанных объявлений (новый ID, та же машина): площадка, название, год, пробег до тысяч км."""
    km = c.get("km")
    return f"{c['src']}|{' '.join(tokens(c.get('title', '')))}|{c.get('year')}|{int(round(km / 1000)) if km else '-'}"


def verdict(c, state, look):
    """Почему объявление не публикуется: (категория, подробность) или None, если подходит."""
    f = CFG["filters"]
    if not price_ok(c):
        return "нет корректной цены", "нет корректной цены"
    if c["year"] < f.get("year_min", 0):
        return "год меньше минимального", f"год {c['year']} < {f.get('year_min')}"
    if c["price"] > f.get("price_usd_max", 10 ** 12):
        return "дороже лимита «под ключ»", f"под ключ {c['price']} $ > {f.get('price_usd_max')} $"
    if c.get("km") is not None and c["km"] > f.get("km_max", 10 ** 12):
        return "пробег больше лимита", f"пробег {c['km']:.0f} км > {f.get('km_max')} км"
    if MARKET.get("mode", "off") == "off":
        return None
    mp = c.get("market") or market_price(c, state, look)
    if not mp:
        if MARKET.get("require", True):
            return "нет цены av.by в таблице", f"нет цены av.by для «{mkey(c)}»"
        return None
    c["market"] = mp
    d = (mp - c["price"]) / mp
    c["disc"], c["score"] = round(d * 100), d
    need = MARKET.get("min_discount_pct", 10)
    if MARKET.get("require", True) and d * 100 < need:
        return f"выгода меньше {need}%", f"выгода {round(d * 100)}% < {need}% (под ключ {c['price']} $, av.by {mp} $)"
    return None


def ingest(name, cars, state, now):
    """Объявления площадки → база: новые проходят отбор и встают в очередь, известные не дублируются."""
    items, st, look = state["items"], STATS[name], {"n": 0}
    fps = {r.get("fp"): r["id"] for r in items.values()
           if r.get("fp") and r.get("status") in ("published", "publishing", "queued", "scheduled", "failed")}
    for c in cars:
        rid, rec = c["id"], items.get(c["id"])
        if rec and rec.get("status") in ("published", "publishing", "failed"):
            st["дубль: уже опубликовано или в работе"] += 1
            continue
        fp = fingerprint(c)
        if fps.get(fp) and fps[fp] != rid:
            st["дубль: переподано с новым ID"] += 1
            continue
        c["score"], c["disc"] = score(c, state["prices"])
        state["prices"].setdefault(key(c), {})[rid] = c["price"]
        v = verdict(c, state, look)
        rec = rec or {"id": rid, "src": name, "status": "new", "discovered_at": iso(now), "attempts": 0}
        was = rec.get("status")
        rec.update(country=country_of(c), title=c["title"], url=c["url"], fp=fp, processed_at=iso(now))
        if v:
            if was in ("queued", "scheduled"):
                log(tag(name), f"Снято с очереди: {c['title']} — {v[1]}")
            rec.update(status="skipped", skip_reason=v[1], car=None, scheduled_at=None)
            st["отбор: " + v[0]] += 1
        elif was in ("queued", "scheduled"):
            rec.update(car=c, score=c.get("score"))  # свежие цена и фото, время публикации сохраняется
            st["обновлено в очереди"] += 1
        else:
            rec.update(status="queued", queued_at=iso(now), car=c, score=c.get("score"), skip_reason=None, error=None)
            st["добавлено в очередь"] += 1
            fps[fp] = rid
        items[rid] = rec


def funnel(name):
    """Где теряются объявления площадки."""
    st, t = STATS[name], tag(name)
    group = lambda p: {k[len(p):]: v for k, v in st.items() if k.startswith(p) and v}
    show = lambda d: ", ".join(f"{k}: {v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))
    log(t, f"Получено с площадки: {st['получено']}")
    if group("разбор: "):
        log(t, f"Отброшено при разборе: {sum(group('разбор: ').values())} ({show(group('разбор: '))})")
    if group("дубль: "):
        log(t, f"Уже в базе: {sum(group('дубль: ').values())} ({show(group('дубль: '))})")
    if group("отбор: "):
        log(t, f"Не прошли отбор: {sum(group('отбор: ').values())} ({show(group('отбор: '))})")
    log(t, f"Добавлено в очередь: {st['добавлено в очередь']}"
        + (f", обновлено в очереди: {st['обновлено в очереди']}" if st["обновлено в очереди"] else ""))


def expire(state, now):
    """Объявления, которые слишком долго ждут очереди (машину могли продать) или с прошедшими торгами, снимаются."""
    ttl = timedelta(hours=SCHED.get("queue_ttl_hours", 72))
    for rec in state["items"].values():
        if rec.get("status") not in ("queued", "scheduled"):
            continue
        qa, ad = parse_dt(rec.get("queued_at"), now.tzinfo), (rec.get("car") or {}).get("auction_date")
        why = ("торги уже прошли" if ad and ad < now.date().isoformat() else
               f"устарело: в очереди дольше {SCHED.get('queue_ttl_hours', 72)} ч" if qa and now - qa > ttl else None)
        if why:
            rec.update(status="skipped", skip_reason=why, car=None, scheduled_at=None, processed_at=iso(now))
            log(tag(rec["src"]), f"Снято с очереди: {rec.get('title')} — {why}")


def recover(state, now):
    """Отправка, прерванная падением запуска: неизвестно, ушёл ли пост. Не повторяем, чтобы не было дубля."""
    for rec in state["items"].values():
        if rec.get("status") == "publishing":
            rec.update(status="failed", error="отправка была прервана сбоем запуска; повтор не делался, чтобы не задвоить пост — "
                                              "проверьте канал вручную", finished_at=iso(now))
            log("SCHEDULER", f"⚠️ Прерванная отправка: {rec.get('title')} — помечено как ошибка, без повтора")


# ---------- планировщик ----------
def _gap(rnd, lo, hi):
    return timedelta(minutes=rnd.uniform(lo, hi))


def reflow(state, now, rnd=random):
    """Бот простаивал (GitHub не запускал, сбой): просроченные публикации не отправляются пачкой,
    а заново раскладываются от текущего момента с обычными интервалами."""
    tzi, mn, mx = now.tzinfo, SCHED.get("min_interval_min", 20), SCHED.get("max_interval_min", 60)
    sch = sorted((r for r in state["items"].values() if r.get("status") == "scheduled"),
                 key=lambda r: r["scheduled_at"])
    ws, we = window(now.date(), tzi)
    for r in sch:  # не успели в прошлые дни → обратно в очередь, на сегодня их распределит планировщик
        if parse_dt(r["scheduled_at"], tzi).date() < now.date():
            r.update(status="queued", scheduled_at=None)
    sch = [r for r in sch if r.get("status") == "scheduled"]
    if not sch:
        return
    first = parse_dt(sch[0]["scheduled_at"], tzi)
    if now - first <= timedelta(minutes=SCHED.get("overdue_grace_min", 30)):
        return
    late = sum(1 for r in sch if parse_dt(r["scheduled_at"], tzi) <= now)
    log("SCHEDULER", f"Просрочено публикаций: {late} — бот не запускался вовремя. Перераспределяю без массовой отправки")
    t = max(now, ws)
    for i, r in enumerate(sch):
        if i:
            t = t + _gap(rnd, mn, mx)
        if t > we:
            r.update(status="queued", scheduled_at=None)
        else:
            r["scheduled_at"] = iso(t)


def plan(state, now, rnd=random):
    """Назначает время публикаций на сегодня: случайный интервал в пределах min–max, равномерно по оставшемуся
    рабочему времени; лимит в день, лимит и пауза по площадкам, чередование площадок."""
    tzi = now.tzinfo
    if not working_day(now.date()):
        return
    ws, we = window(now.date(), tzi)
    if now >= we:
        return
    items, today = state["items"].values(), now.date().isoformat()
    mn, mx = SCHED.get("min_interval_min", 20), SCHED.get("max_interval_min", 60)
    pub = [r for r in items if r.get("status") == "published" and (r.get("published_at") or "")[:10] == today]
    sch = [r for r in items if r.get("status") == "scheduled"]
    cap = (SCHED.get("daily_limit", CFG.get("daily_limit", 10)) - len(pub) - len(sch)
           - state.get("legacy_sent", {}).get(today, 0))
    cnt = Counter(r["src"] for r in pub + sch)
    last_src, times = {}, []
    for r in pub + sch:
        t = parse_dt(r.get("published_at") or r.get("scheduled_at"), tzi)
        if t:
            times.append(t)
            last_src[r["src"]] = max(last_src.get(r["src"], t), t)
    anchor = max(times) if times else None
    queue = sorted((r for r in items if r.get("status") == "queued"), key=lambda r: -(r.get("score") or 0))
    while cap > 0 and queue:
        elig = [r for r in queue if cnt[r["src"]] < src_rule(r["src"], "daily_limit", CFG.get("per_source_max", 4))]
        if not elig:
            break
        if anchor is None:  # первая публикация дня — вскоре после начала окна (или после запуска, если окно уже идёт)
            t = max(now, ws) + _gap(rnd, 0, min(mn, 15))
        else:
            n = min(cap, len(elig))
            rest = (we - max(anchor, now)).total_seconds() / 60
            target = rest / n if n else mx
            center = max(mn, min(mx, target))  # равномерно по оставшемуся времени, но в пределах min–max
            spread = (mx - mn) * 0.25          # и всегда со случайным разбросом
            t = anchor + _gap(rnd, max(mn, center - spread), min(mx, center + spread))
            if t < now:
                t = now + _gap(rnd, 0, min(mn, 10))
        elig.sort(key=lambda r: (cnt[r["src"]], -(r.get("score") or 0)))
        pick_rec = None
        for r in elig:
            lt = last_src.get(r["src"])
            if not lt or t >= lt + timedelta(minutes=src_rule(r["src"], "min_gap_min", 0)):
                pick_rec = r
                break
        if not pick_rec:  # у всех площадок ещё идёт пауза: ждём ближайшую
            pick_rec = min(elig, key=lambda r: last_src[r["src"]] + timedelta(minutes=src_rule(r["src"], "min_gap_min", 0)))
            t = last_src[pick_rec["src"]] + timedelta(minutes=src_rule(pick_rec["src"], "min_gap_min", 0))
        if t > we:
            break
        pick_rec.update(status="scheduled", scheduled_at=iso(t))
        queue.remove(pick_rec)
        cap -= 1
        cnt[pick_rec["src"]] += 1
        last_src[pick_rec["src"]] = t
        anchor = t
        log("SCHEDULER", f"Запланировано на {t:%H:%M}: [{tag(pick_rec['src'])}] {pick_rec.get('title')}")


def replan_future(state, now):
    """После сбора: посты, назначенные позже чем через 20 минут, возвращаются в очередь и планируются заново,
    чтобы площадки чередовались, а не шли подряд из той, что была собрана первой."""
    n = 0
    for r in state["items"].values():
        t = parse_dt(r.get("scheduled_at"), now.tzinfo) if r.get("status") == "scheduled" else None
        if t and t > now + timedelta(minutes=20):
            r.update(status="queued", scheduled_at=None)
            n += 1
    if n:
        log("SCHEDULER", f"Расписание пересобрано с учётом новых объявлений ({n} постов)")


def keep_spacing(state, now, rnd=random):
    """После публикации следующая не раньше чем через минимальный интервал."""
    tzi, mn = now.tzinfo, SCHED.get("min_interval_min", 20)
    _, we = window(now.date(), tzi)
    prev = now
    for r in sorted((r for r in state["items"].values() if r.get("status") == "scheduled"), key=lambda r: r["scheduled_at"]):
        t = parse_dt(r["scheduled_at"], tzi)
        if t < prev + timedelta(minutes=mn):
            t = prev + _gap(rnd, mn, mn + 5)
            if t > we:
                r.update(status="queued", scheduled_at=None)
                continue
            r["scheduled_at"] = iso(t)
        prev = t


def next_publication(state, now):
    sch = sorted(r["scheduled_at"] for r in state["items"].values() if r.get("status") == "scheduled")
    if sch:
        t = parse_dt(sch[0], now.tzinfo)
        log("SCHEDULER", f"Следующая публикация: {t:%d.%m %H:%M}")
    else:
        q = sum(1 for r in state["items"].values() if r.get("status") == "queued")
        log("SCHEDULER", f"Запланированных публикаций нет; в очереди {q} (распределятся в рабочее время)")


def publish_due(state, now, rnd=random, saver=None):
    """Публикует подошедшие по времени объявления: не больше max_per_run за запуск, с записью результата."""
    tzi = now.tzinfo
    ws, we = window(now.date(), tzi)
    if not working_day(now.date()) or not ws <= now <= we:
        log("SCHEDULER", "Сейчас нерабочее время для публикаций")
        return {"sent": 0, "fatal": False, "tried": 0}
    due = sorted((r for r in state["items"].values()
                  if r.get("status") == "scheduled" and parse_dt(r["scheduled_at"], tzi) <= now),
                 key=lambda r: r["scheduled_at"])
    out = {"sent": 0, "fatal": False, "tried": 0}
    for rec in due[:SCHED.get("max_per_run", 1)]:
        c = rec.get("car") or {}
        rec.update(status="publishing", attempts=rec.get("attempts", 0) + 1, started_at=iso(now))
        if saver:
            saver()  # статус «publishing» на диске до отправки: при падении запуска пост не повторится
        out["tried"] += 1
        res = send(c)
        if res["ok"]:
            rec.update(status="published", published_at=iso(now), message_ids=res["ids"], error=res["error"] or None,
                       car=None, price=c.get("price"), price_usd=c.get("price_usd"))
            out["sent"] += 1
            log("TELEGRAM", f"Опубликовано: [{tag(rec['src'])}] {rec.get('title')} (message_id {', '.join(map(str, res['ids']))})")
        elif res["skip"]:
            rec.update(status="skipped", skip_reason=res["error"], car=None, scheduled_at=None)
            log("TELEGRAM", f"Пропущено: {rec.get('title')} — {res['error']}")
        elif res["uncertain"]:
            rec.update(status="failed", error=res["error"] + " — повтор не делается, чтобы не задвоить пост; проверьте канал")
            log("TELEGRAM", f"⚠️ {rec.get('title')}: {rec['error']}")
        else:
            max_att = SCHED.get("max_attempts", 4)
            if res["retry_after"]:
                wait = timedelta(seconds=res["retry_after"] + 5)
            else:
                wait = timedelta(minutes=SCHED.get("retry_base_min", 15) * 2 ** (rec["attempts"] - 1))
            if rec["attempts"] >= max_att:
                rec.update(status="failed", error=res["error"])
                log("TELEGRAM", f"⚠️ {rec.get('title')}: попытки исчерпаны ({max_att}) — {res['error']}")
            else:
                rec.update(status="scheduled", scheduled_at=iso(now + wait), error=res["error"])
                log("TELEGRAM", f"⚠️ {rec.get('title')}: {res['error']}. Повтор в {now + wait:%H:%M} "
                                f"(попытка {rec['attempts'] + 1} из {max_att})")
            out["fatal"] = out["fatal"] or res["fatal"]
        if saver:
            saver()
    if out["sent"]:
        keep_spacing(state, now, rnd)
    return out


# ---------- сбор с площадок ----------
def fetch_due(src, state, now, force):
    if force:
        return True
    last = parse_dt((state["fetch"].get(src["name"]) or {}).get("last_ok"), now.tzinfo)
    every = src_rule(src["name"], "fetch_every_hours", 24)
    return not last or (now - last).total_seconds() / 3600 >= every - 2  # запас 2 ч на задержки запусков GitHub


def collect(state, now, force=False):
    """Каждая площадка — отдельно: сбой одной не останавливает остальные, неудачный сбор повторится в следующий запуск."""
    srcs = [s for s in CFG["sources"] if s.get("enabled")]
    if any(s.get("type") in ("apify", "apify_list") and fetch_due(s, state, now, force) for s in srcs):
        apify_budget()
    for src in srcs:
        name, t = src["name"], tag(src["name"])
        if not fetch_due(src, state, now, force):
            log(t, "Сбор не нужен: последний был недавно")
            continue
        fs = state["fetch"].setdefault(name, {})
        fs["last_try"] = iso(now)
        if (src.get("price_currency") or "USD").upper() == "KRW":
            if "KRW" not in RATES:
                RATES["KRW"] = load_krw_rate(state)
            if not RATES.get("KRW"):
                fs["last_error"] = "нет курса вон"
                log(t, "⚠️ Пропуск: нет курса вон из Google-таблицы")
                continue
        log(t, "Проверка каталога началась")
        try:
            cars = fetch(src)
        except Exception as e:
            fs["last_error"] = str(e)[:300]
            log(t, f"⚠️ Ошибка площадки: {str(e)[:300]}")
            funnel(name)
            continue
        log(t, f"Найдено подходящих по разбору: {len(cars)}")
        ingest(name, cars, state, now)
        fs.update(last_ok=iso(now), last_error=None, last_count=len(cars))
        funnel(name)


# ---------- отчёт ----------
def report(state, now):
    items = list(state["items"].values())
    by = defaultdict(Counter)
    for r in items:
        by[r.get("status")][r.get("src")] += 1
    for stt in ("queued", "scheduled", "publishing", "published", "failed", "skipped"):
        if by[stt]:
            log("БАЗА", f"{stt}: {sum(by[stt].values())} (" + ", ".join(f"{k}: {v}" for k, v in by[stt].items()) + ")")
    for r in sorted((r for r in items if r.get("status") == "scheduled"), key=lambda r: r["scheduled_at"])[:12]:
        log("SCHEDULER", f"{parse_dt(r['scheduled_at'], now.tzinfo):%d.%m %H:%M} [{tag(r['src'])}] {r.get('title')}")
    for r in [r for r in items if r.get("status") == "failed"][-10:]:
        log("TELEGRAM", f"Ошибка: [{tag(r['src'])}] {r.get('title')} — {r.get('error')}")
    for name, fs in state.get("fetch", {}).items():
        log(tag(name), f"Последний успешный сбор: {fs.get('last_ok') or 'не было'}; "
                       f"последняя ошибка: {fs.get('last_error') or 'нет'}")


def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path and SUMMARY:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("### Журнал бота\n\n```\n" + "\n".join(SUMMARY[-400:]) + "\n```\n")
            SUMMARY.clear()
        except OSError:
            pass


# ---------- защита от параллельного запуска ----------
def release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def acquire_lock():
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        atexit.register(release_lock)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(LOCK_FILE) > 3600:  # «зависшая» блокировка старше часа
                os.remove(LOCK_FILE)
                return acquire_lock()
        except OSError:
            pass
        return False


# ---------- основной сценарий ----------
def run_demo():
    seen, sent = set(), 0
    for c in DEMO_CARS:
        if c["src"] in seen:
            continue
        seen.add(c["src"])
        if sent:
            time.sleep(5)
        r = send(dict(c))
        log("TELEGRAM", ("Демо опубликовано: " if r["ok"] else "⚠️ Демо не отправилось: ") + c["title"]
            + ("" if r["ok"] else f" — {r['error']}"))
        sent += r["ok"]
    if not sent:
        sys.exit("❌ Ни одно демо-сообщение не отправилось: смотрите ошибки Telegram выше (токен, TG_CHAT, права бота)")


def main():
    mode = next((a[2:] for a in sys.argv[1:] if a.startswith("--")), "publish")
    if mode not in ("publish", "fetch", "fetch-all", "status", "dry", "demo"):
        sys.exit(f"Неизвестный режим --{mode}")
    if mode in ("publish", "demo") and not (TOKEN and CHAT):
        sys.exit("Не заданы TG_TOKEN и TG_CHAT")
    if mode == "demo":
        try:
            return run_demo()
        finally:
            write_summary()
    if mode != "dry" and not acquire_lock():
        log("SCHEDULER", "Другой запуск бота ещё работает — этот завершается, чтобы не было параллельной отправки")
        return
    try:
        return run_mode(mode)
    finally:
        write_summary()
        if mode != "dry":
            release_lock()


def run_mode(mode):
    now = now_local()
    rd = CFG.get("customs", {}).get("rates_date")
    if rd and (date.today() - date.fromisoformat(rd)).days > 14:
        log("КУРС", f"⚠️ Курсы НБРБ в config.json от {rd} устарели: обновите customs.byn_per_usd и customs.byn_per_eur")
    state = load_state()
    if mode == "status":
        return report(state, now)
    recover(state, now)
    if mode in ("fetch", "fetch-all", "dry"):
        collect(state, now, force=mode != "fetch")
        if UNKNOWN_KO:
            log("ENCAR", "Не переведены корейские слова (добавьте в config.json → translate): "
                + ", ".join(f"{w} ×{n}" for w, n in UNKNOWN_KO.most_common(15)))
    expire(state, now)
    reflow(state, now)
    if mode in ("fetch", "fetch-all"):
        replan_future(state, now)
    plan(state, now)
    if mode == "dry":
        log("SCHEDULER", "Режим проверки: ничего не сохранено и не отправлено")
        for r in sorted((r for r in state["items"].values() if r.get("status") in ("scheduled", "queued")),
                        key=lambda r: r.get("scheduled_at") or "9"):
            c = r.get("car") or {}
            log(tag(r["src"]), f"{(r.get('scheduled_at') or 'в очереди')[11:16] or 'в очереди'} · {c.get('title')} · {c.get('year')}"
                + (f" · {fmt(c['price_src'])} {c.get('currency_src')} → {c.get('price_usd')} $ по курсу {c['rate']:g}" if c.get("rate") else "")
                + f" · под ключ {c.get('price')} $ · av.by {c.get('market')} $ · выгода {c.get('disc')}% · фото {len(c.get('photos', []))}")
        return
    if mode == "publish":
        res = publish_due(state, now, saver=lambda: save_state(state))
        save_state(state)
        next_publication(state, now)
        if res["fatal"] and not res["sent"]:
            sys.exit("❌ Публикация не удалась из-за настроек Telegram (токен, TG_CHAT, права бота) — смотрите ошибку выше")
        return
    save_state(state)
    next_publication(state, now)


if __name__ == "__main__":
    main()
