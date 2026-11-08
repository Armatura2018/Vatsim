#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ОДНОФАЙЛОВЫЙ Telegram-бот METAR / TAF + VATSIM.

Нужен Python 3.10+. Внешние библиотеки не нужны.

Переменные хостинга:
  BOT_TOKEN=токен_от_BotFather
  DATABASE_PATH=/app/data/bot.db
  METAR_SITE_BASE=https://metar-taf.com

Локальный запуск Linux:
  export BOT_TOKEN="ТОКЕН"
  python bot.py

Windows PowerShell:
  $env:BOT_TOKEN="ТОКЕН"
  python bot.py

База SQLite создаётся автоматически в /app/data/bot.db.
Папка /app/data должна быть постоянным хранилищем (volume).

Источники:
  METAR/TAF: https://metar-taf.com/{ICAO}
  VATSIM: https://data.vatsim.net/v3/vatsim-data.json

Ограничения:
- METAR/TAF извлекаются из HTML metar-taf.com; при изменении сайта парсер
  может потребовать правки. CAPTCHA/ограничения доступа не обходятся.
- VATSIM feed не даёт полную геометрию всех секторов. /atc выдаёт ориентир,
  а не гарантированный прогноз передачи управления.
- Автоматическая расшифровка учебная, не для реального принятия решений.
"""

import os, re, json, time, html, sqlite3, logging, urllib.request, urllib.parse
from pathlib import Path
from html.parser import HTMLParser

TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DATABASE_PATH", "/app/data/bot.db")
METAR_BASE = os.getenv("METAR_SITE_BASE", "https://metar-taf.com").rstrip("/")
VATSIM_URL = os.getenv("VATSIM_DATA_URL", "https://data.vatsim.net/v3/vatsim-data.json")
API = "https://api.telegram.org/bot" + TOKEN
UA = "Mozilla/5.0 (compatible; METAR-VATSIM-Bot/1.0)"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")


# ---------------- HTTP / Telegram ----------------

def get_url(url, timeout=25, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def tg(method, data=None):
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    req = urllib.request.Request(
        API + "/" + method,
        data=json.dumps(data or {}).encode(),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log.error("Telegram API %s: %s", method, e)
        return {"ok": False}

def esc(x):
    return html.escape(str(x if x is not None else "—"))

def send(chat, text, keyboard=None):
    d = {"chat_id": chat, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    if keyboard:
        d["reply_markup"] = keyboard
    return tg("sendMessage", d)

def callback_answer(cid, text=""):
    tg("answerCallbackQuery", {"callback_query_id": cid, "text": text[:180]})

def split_send(chat, text):
    while len(text) > 3900:
        cut = text.rfind("\n", 0, 3900)
        if cut < 500:
            cut = 3900
        send(chat, esc(text[:cut]))
        text = text[cut:].lstrip()
    if text:
        send(chat, esc(text))


# ---------------- SQLite ----------------

def connect_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with connect_db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            username TEXT,
            vatsim_cid INTEGER,
            language TEXT NOT NULL DEFAULT 'ru',
            awaiting_cid INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
        existing = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for name, decl in {
            "username": "TEXT", "vatsim_cid": "INTEGER",
            "language": "TEXT NOT NULL DEFAULT 'ru'",
            "awaiting_cid": "INTEGER NOT NULL DEFAULT 0",
            "updated_at": "TIMESTAMP"
        }.items():
            if name not in existing:
                c.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")
        c.commit()
    log.info("SQLite: %s", DB_PATH)

def user_get(uid):
    with connect_db() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

def user_save(uid, username=None, **kw):
    allowed = {"vatsim_cid", "language", "awaiting_cid"}
    kw = {k: v for k, v in kw.items() if k in allowed}
    with connect_db() as c:
        c.execute("INSERT OR IGNORE INTO users(id,username) VALUES(?,?)", (uid, username))
        if kw:
            fields = ",".join(f"{k}=?" for k in kw)
            c.execute(f"UPDATE users SET {fields}, username=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (*kw.values(), username, uid))
        else:
            c.execute("UPDATE users SET username=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (username, uid))
        c.commit()


# ---------------- METAR / TAF source parser ----------------

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.hidden = [], 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self.hidden:
            self.hidden -= 1
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def norm(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()

def extract_report(text, icao, kind):
    if kind == "METAR":
        patterns = [
            rf"\b(?:METAR|SPECI)\s+{icao}\s+\d{{6}}Z\b.{{0,650}}?(?=\s+(?:TAF|METAR|SPECI)\b|$)",
            rf"\b{icao}\s+\d{{6}}Z\b.{{0,500}}?(?=\s+(?:TAF|METAR|SPECI)\b|$)"
        ]
    else:
        patterns = [
            rf"\bTAF(?:\s+(?:AMD|COR))?\s+{icao}\s+\d{{6}}Z\b.{{0,1600}}?(?=\s+(?:METAR|SPECI|TAF)\b|$)"
        ]
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return norm(m.group(0))[:1600]
    return None



def fetch_reports(icao):
    import urllib.parse
    from datetime import datetime, timezone, timedelta
    import re

    icao = icao.upper().strip()

    if not re.fullmatch(r"[A-Z0-9]{4}", icao):
        raise RuntimeError("Некорректный ICAO-код")

    base = "https://aviationweather.gov/api/data"

    headers = {
        "User-Agent": "METAR-VATSIM-Telegram-Bot/1.0 contact bot",
        "Accept": "text/plain"
    }

    now = datetime.now(timezone.utc)

    def request(endpoint, hours):
        url = base + "/" + endpoint + "?" + urllib.parse.urlencode({
            "ids": icao,
            "format": "raw",
            "hours": str(hours)
        })

        try:
            data = get_url(url, timeout=30, headers=headers)
            return data.decode("utf-8", "replace").strip()
        except Exception as e:
            raise RuntimeError(f"Ошибка API {endpoint}: {e}")

    def report_time(report):
        # METAR: METAR EDDF 081120Z ...
        # TAF: TAF EDDF 081100Z 0812/0918 ...
        match = re.search(r"\b(\d{2})(\d{2})(\d{2})Z\b", report)

        if not match:
            return datetime.min.replace(tzinfo=timezone.utc)

        day, hour, minute = map(int, match.groups())

        try:
            candidate = now.replace(
                day=day,
                hour=hour,
                minute=minute,
                second=0,
                microsecond=0
            )

            # Корректировка перехода через границу месяца.
            if candidate > now + timedelta(hours=1):
                if candidate.month == 1:
                    candidate = candidate.replace(
                        year=candidate.year - 1,
                        month=12
                    )
                else:
                    candidate = candidate.replace(
                        month=candidate.month - 1
                    )

            return candidate

        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)

    # ---------------- METAR ----------------

    metar_data = request("metar", 6)

    metar_reports = []

    for line in metar_data.splitlines():
        line = line.strip()

        if not line:
            continue

        # Оставляем только сообщения нужного аэродрома.
        if re.search(
            rf"^(?:METAR|SPECI)\s+{icao}\s+\d{{6}}Z\b",
            line,
            re.I
        ):
            metar_reports.append(line)

    if not metar_reports:
        raise RuntimeError(
            f"Свежий METAR для {icao} не найден за последние 6 часов. "
            "Возможно, аэропорт не передаёт наблюдения."
        )

    # Самое новое наблюдение, включая SPECI.
    metar = max(metar_reports, key=report_time)

    # ---------------- TAF ----------------

    taf = None

    try:
        taf_data = request("taf", 24)

        taf_reports = []

        for line in taf_data.splitlines():
            line = line.strip()

            if not line:
                continue

            if re.search(
                rf"^TAF\s+(?:(?:AMD|COR)\s+)?{icao}\s+\d{{6}}Z\b",
                line,
                re.I
            ):
                taf_reports.append(line)

        if taf_reports:
            taf = max(taf_reports, key=report_time)

    except Exception:
        taf = None

    return {
        "metar": metar,
        "taf": taf,
        "url": f"https://metar-taf.com/{icao}"
    }



# ---------------- METAR / TAF decoder ----------------

CLOUDS = {
 "FEW":("мало облаков (1–2 октанта)","few clouds (1–2 oktas)"),
 "SCT":("рассеянная облачность (3–4 октанта)","scattered clouds (3–4 oktas)"),
 "BKN":("значительная облачность (5–7 октантов)","broken clouds (5–7 oktas)"),
 "OVC":("сплошная облачность (8 октантов)","overcast (8 oktas)"),
 "NSC":("значимой облачности нет","no significant cloud"),
 "NCD":("автоматическая система не обнаружила облаков","no clouds detected by automatic system"),
 "SKC":("ясно","sky clear"), "CLR":("облаков ниже 12 000 ft нет (код США)","no clouds below 12,000 ft (US code)")
}
WX = {
 "SH":("ливневый","showery"),"FZ":("переохлаждённый/замерзающий","freezing"),
 "DR":("позёмок","low drifting"),"BL":("метельный перенос","blowing"),
 "MI":("низкий/приземный","shallow"),"PR":("частичный","partial"),
 "BC":("клочья","patches"),"TS":("гроза","thunderstorm"),
 "DZ":("морось","drizzle"),"RA":("дождь","rain"),"SN":("снег","snow"),
 "SG":("снежные зёрна","snow grains"),"IC":("ледяные кристаллы","ice crystals"),
 "PL":("ледяная крупа","ice pellets"),"GR":("град","hail"),
 "GS":("мелкий град/снежная крупа","small hail/snow pellets"),
 "BR":("дымка","mist"),"FG":("туман","fog"),"FU":("дым","smoke"),
 "HZ":("мгла","haze"),"VA":("вулканический пепел","volcanic ash"),
 "SA":("песок","sand"),"DU":("пыль","dust"),"SQ":("шквал","squall"),
 "FC":("смерч/воронкообразное облако","funnel cloud/tornado"),
 "SS":("песчаная буря","sandstorm"),"DS":("пыльная буря","duststorm"),
 "UP":("неизвестные осадки","unknown precipitation")
}
def tr(ru, en, lang):
    return ru if lang == "ru" else en

def wx_decode(token, lang):
    body = token.lstrip("+-")
    out = []
    if body.startswith("VC"):
        out.append(tr("в окрестностях аэродрома","in the vicinity",lang))
        body = body[2:]
    out.append(tr("сильная интенсивность" if token.startswith("+") else
                  "слабая интенсивность" if token.startswith("-") else "умеренная интенсивность",
                  "heavy intensity" if token.startswith("+") else
                  "light intensity" if token.startswith("-") else "moderate intensity", lang))
    out += [tr(ru,en,lang) for code,(ru,en) in WX.items() if code in body]
    return ", ".join(out) if len(out) > 1 else None

def temp_val(s):
    return -int(s[1:]) if s.startswith("M") else int(s)

def decode_metar(raw, lang="ru"):
    out = [tr("РАСШИФРОВКА METAR","METAR DECODING",lang), raw, ""]
    for t in raw.upper().split():
        if re.fullmatch(r"[A-Z]{4}",t) and t not in {"AUTO","COR","AMD","METAR","SPECI","TAF"}:
            out.append(f"• {tr('Аэродром ICAO','ICAO station',lang)}: {t}")
        elif re.fullmatch(r"\d{6}Z",t):
            out.append(f"• {tr('Время наблюдения','Observation time',lang)}: {t[:2]} число, {t[2:4]}:{t[4:6]} UTC")
        elif t in ("AUTO","COR"):
            out.append(f"• {t} — {tr('автоматическое наблюдение' if t=='AUTO' else 'исправленное сообщение','automated report' if t=='AUTO' else 'corrected report',lang)}")
        elif re.fullmatch(r"(VRB|\d{3})\d{2,3}(?:G\d{2,3})?(KT|MPS|KMH)",t):
            m = re.fullmatch(r"(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(KT|MPS|KMH)",t)
            unit = {"KT":"kt","MPS":"m/s","KMH":"km/h"}[m.group(4)]
            direction = tr("переменный","variable",lang) if m.group(1)=="VRB" else m.group(1)+"°"
            gust = f", {tr('порывы','gusts',lang)} {m.group(3)} {unit}" if m.group(3) else ""
            out.append(f"• {tr('Ветер','Wind',lang)}: {direction}, {m.group(2)} {unit}{gust}")
        elif t == "CAVOK":
            out.append("• CAVOK — " + tr("видимость 10 км и более; нет значимой текущей погоды; нет облаков ниже 5000 ft или минимальной высоты сектора (берётся большее значение), нет CB/TCU","visibility 10 km or more; no significant present weather; no clouds below 5000 ft or minimum sector altitude (whichever is higher), and no CB/TCU",lang))
        elif t == "9999":
            out.append(f"• 9999 — {tr('видимость 10 км и более','visibility 10 km or more',lang)}")
        elif re.fullmatch(r"\d{4}",t):
            out.append(f"• {tr('Видимость','Visibility',lang)}: {t} {tr('метров','metres',lang)}")
        elif re.fullmatch(r"R\d{2}[LCR]?/(?:M|P)?\d{4}[UDN]?",t):
            out.append(f"• {tr('Видимость на ВПП (RVR)','Runway visual range (RVR)',lang)}: {t}; U — increasing, D — decreasing, N — no change")
        elif re.fullmatch(r"[+-]?(?:VC)?[A-Z]{2,8}",t):
            d = wx_decode(t,lang)
            if d: out.append(f"• {tr('Погодные явления','Weather',lang)}: {d}")
        elif re.fullmatch(r"(FEW|SCT|BKN|OVC)\d{3}[A-Z]{0,2}",t):
            code,h = t[:3],t[3:6]
            out.append(f"• {tr('Облачность','Cloud layer',lang)}: {tr(*CLOUDS[code],lang)}, {int(h)*100} ft")
        elif t.startswith("VV") and len(t)>=5 and t[2:5].isdigit():
            out.append(f"• VV — {tr('вертикальная видимость','vertical visibility',lang)} {int(t[2:5])*100} ft")
        elif re.fullmatch(r"M?\d{2}/M?\d{2}",t):
            a,b = t.split("/")
            out.append(f"• {tr('Температура / точка росы','Temperature / dew point',lang)}: {temp_val(a)}°C / {temp_val(b)}°C")
        elif re.fullmatch(r"Q\d{4}",t):
            out.append(f"• QNH: {t[1:]} hPa")
        elif re.fullmatch(r"A\d{4}",t):
            out.append(f"• Altimeter: {int(t[1:])/100:.2f} inHg")
        elif t == "NOSIG":
            out.append(f"• NOSIG — {tr('значительных изменений не ожидается','no significant change expected',lang)}")
        elif t == "NSW":
            out.append(f"• NSW — {tr('значимых погодных явлений нет','no significant weather',lang)}")
        elif t.startswith("WS"):
            out.append(f"• {t} — {tr('сдвиг ветра','wind shear',lang)}")
        elif t == "RMK":
            out.append(f"• RMK — {tr('начало примечаний','remarks begin',lang)}")
    out += ["",tr("Автоматический разбор может не распознать отдельные региональные или специальные группы. Сверяйте исходную строку и официальные данные.","Automatic decoding may not recognize all regional or special groups. Check the raw report and official data.",lang)]
    return "\n".join(out)

def decode_taf(raw, lang="ru"):
    out = [tr("РАСШИФРОВКА TAF","TAF DECODING",lang),raw,""]
    for t in raw.upper().split():
        if re.fullmatch(r"\d{6}Z",t):
            out.append(f"• {tr('Выпущен','Issued',lang)}: {t} UTC")
        elif re.fullmatch(r"\d{4}/\d{4}",t):
            out.append(f"• {tr('Период действия','Validity period',lang)}: {t} UTC")
        elif re.fullmatch(r"FM\d{6}",t):
            out.append(f"• FM — {tr('с этого времени новые условия','from this time new conditions')}: {t[2:]} UTC")
        elif t in ("TEMPO","BECMG","PROB30","PROB40","INTER"):
            meanings = {
                "TEMPO":("временные колебания условий","temporary fluctuations"),
                "BECMG":("постепенный переход к новым условиям","gradual transition"),
                "PROB30":("вероятность 30%","30% probability"),
                "PROB40":("вероятность 40%","40% probability"),
                "INTER":("частые кратковременные изменения","frequent temporary changes")
            }
            out.append(f"• {t} — {tr(*meanings[t],lang)}")
        elif re.fullmatch(r"(FEW|SCT|BKN|OVC)\d{3}[A-Z]{0,2}",t):
            out.append(f"• {t} — {tr(*CLOUDS[t[:3]],lang)}, {int(t[3:6])*100} ft")
        elif t == "CAVOK":
            out.append(f"• CAVOK — {tr('условия CAVOK','CAVOK conditions',lang)}")
        elif t == "9999":
            out.append(f"• 9999 — {tr('видимость 10 км и более','visibility 10 km or more',lang)}")
        elif re.fullmatch(r"\d{4}",t):
            out.append(f"• {tr('Видимость','Visibility',lang)}: {t} m")
        elif re.fullmatch(r"(VRB|\d{3})\d{2,3}(?:G\d{2,3})?(KT|MPS|KMH)",t):
            out.append(f"• {tr('Ветер','Wind',lang)}: {t}")
        elif re.fullmatch(r"Q\d{4}",t):
            out.append(f"• QNH {t[1:]} hPa")
        else:
            d = wx_decode(t,lang) if re.fullmatch(r"[+-]?(?:VC)?[A-Z]{2,8}",t) else None
            if d: out.append(f"• {tr('Погода','Weather',lang)}: {d}")
    out += ["",tr("TAF — прогноз, а не наблюдение. Проверяйте период действия, FM/TEMPO/BECMG/PROB и время UTC.","TAF is a forecast, not an observation. Check validity, FM/TEMPO/BECMG/PROB groups and UTC times.",lang)]
    return "\n".join(out)


# ---------------- VATSIM ----------------

def feed_get():
    return json.loads(get_url(VATSIM_URL, timeout=30).decode())

def pilot_by_cid(feed,cid):
    return next((p for p in feed.get("pilots",[]) if str(p.get("cid"))==str(cid)),None)

def nm_between(a,b,c,d):
    import math
    r=3440.065
    p1,p2=math.radians(float(a)),math.radians(float(c))
    dp=math.radians(float(c)-float(a)); dl=math.radians(float(d)-float(b))
    x=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(x))

def stage(p):
    alt=int(p.get("altitude") or 0); gs=int(p.get("groundspeed") or 0)
    if gs<40 and alt<3000: return "ground"
    if alt<10000: return "terminal"
    return "cruise"

def atc_relevant(feed,p):
    fp=p.get("flight_plan") or {}
    dep=(fp.get("departure") or "").upper()
    arr=(fp.get("arrival") or "").upper()
    lat,lon=p.get("latitude"),p.get("longitude")
    st=stage(p); found=[]
    # Facility: 1 FSS, 2 DEL, 3 GND, 4 TWR, 5 APP, 6 DEP, 7 CTR.
    for c in feed.get("controllers",[]):
        call=(c.get("callsign") or "").upper()
        try: fac=int(c.get("facility") or 0)
        except: fac=0
        prefix=call.split("_")[0] if "_" in call else ""
        dist=None
        if lat is not None and lon is not None and c.get("latitude") is not None and c.get("longitude") is not None:
            try: dist=nm_between(lat,lon,c["latitude"],c["longitude"])
            except: pass
        if st=="ground":
            ok=fac in (2,3,4) and (prefix in (dep,arr) or (dist is not None and dist<=35))
        elif st=="terminal":
            ok=fac in (4,5,6,7) and (prefix in (dep,arr) or (dist is not None and dist<=80))
        else:
            # На эшелоне не предлагаем GND/TWR/DEL/APP из-за пролёта мимо аэропорта.
            ok=fac in (1,7)
            if dist is not None:
                ok=ok and dist<=max(180,float(c.get("visual_range") or 0)*2)
        if ok:
            found.append((dist,call,c.get("frequency") or "—"))
    found.sort(key=lambda x:x[0] if x[0] is not None else 999999)
    return found

def get_flight(cid):
    feed=feed_get()
    p=pilot_by_cid(feed,cid)
    return p,atc_relevant(feed,p) if p else [],feed


# ---------------- Telegram handlers ----------------

HELP = ("<b>METAR / TAF + VATSIM</b>\n\n"
        "/start — регистрация и VATSIM CID\n"
        "/metar ICAO — METAR, например /metar UNNT\n"
        "/flight — ваш текущий полёт\n"
        "/atc — подходящие онлайн-позиции ATC\n"
        "/profile — профиль\n/setcid — изменить CID\n"
        "/language — язык расшифровки\n/help — справка")

def keyboard_metar(icao):
    return {"inline_keyboard":[[
        {"text":"📖 Расшифровать","callback_data":"decode:"+icao},
        {"text":"🛫 TAF","callback_data":"taf:"+icao}
    ]]}

def keyboard_lang():
    return {"inline_keyboard":[[
        {"text":"🇷🇺 Русский","callback_data":"lang:ru"},
        {"text":"🇬🇧 English","callback_data":"lang:en"}
    ]]}

def on_start(chat,user):
    old=user_get(user["id"])
    user_save(user["id"],user.get("username"),awaiting_cid=0 if old and old.get("vatsim_cid") else 1)
    old=user_get(user["id"])
    if old and old.get("vatsim_cid"):
        send(chat,f"С возвращением! Ваш VATSIM CID: <code>{old['vatsim_cid']}</code>.\n\n{HELP}")
    else:
        send(chat,"Привет! Пришлите числовой VATSIM CID одним сообщением. Я сохраню его.")

def on_metar(chat,args):
    if len(args)!=1 or not re.fullmatch(r"[A-Za-z0-9]{4}",args[0]):
        return send(chat,"Формат: <code>/metar UNNT</code> — четырёхсимвольный ICAO.")
    icao=args[0].upper()
    send(chat,f"Получаю METAR {icao} с metar-taf.com…")
    try:
        r=fetch_reports(icao)
        send(chat,f"<b>METAR {icao}</b>\n<code>{esc(r['metar'])}</code>\n\n<a href=\"{esc(r['url'])}\">Источник: metar-taf.com</a>",keyboard_metar(icao))
    except Exception as e:
        send(chat,f"Не удалось получить METAR: {esc(e)}")

def on_callback(q):
    data=q.get("data",""); u=q.get("from",{}); uid=u.get("id")
    msg=q.get("message",{}); chat=(msg.get("chat") or {}).get("id")
    if data.startswith("lang:"):
        lang=data.split(":")[1]
        if lang not in ("ru","en"): return callback_answer(q["id"],"Unknown language")
        user_save(uid,u.get("username"),language=lang)
        callback_answer(q["id"],"Язык сохранён" if lang=="ru" else "Language saved")
        if chat: send(chat,"Язык расшифровки: Русский" if lang=="ru" else "Decoding language: English")
        return
    if data.startswith(("decode:","taf:")):
        action,icao=data.split(":",1)
        if not re.fullmatch(r"[A-Z0-9]{4}",icao): return callback_answer(q["id"],"Некорректный ICAO")
        callback_answer(q["id"],"Получаю данные…")
        lang=(user_get(uid) or {}).get("language","ru")
        try:
            r=fetch_reports(icao)
            if action=="decode":
                split_send(chat,decode_metar(r["metar"],lang))
            elif not r["taf"]:
                send(chat,f"TAF для {icao} не найден на metar-taf.com.")
            else:
                split_send(chat,"TAF "+icao+"\n"+r["taf"]+"\n\n"+decode_taf(r["taf"],lang))
        except Exception as e:
            send(chat,f"Ошибка получения отчёта: {esc(e)}")

def on_text(m):
    chat=m["chat"]["id"]; u=m.get("from",{}); uid=u.get("id")
    text=(m.get("text") or "").strip()
    if not text: return
    profile=user_get(uid)
    if profile and profile.get("awaiting_cid"):
        if not re.fullmatch(r"\d{4,8}",text):
            return send(chat,"CID должен содержать 4–8 цифр. Пришлите ещё раз.")
        user_save(uid,u.get("username"),vatsim_cid=int(text),awaiting_cid=0)
        return send(chat,f"Готово! VATSIM CID <code>{text}</code> сохранён. Доступны /flight и /atc.")
    parts=text.split(); cmd=parts[0].split("@")[0].lower(); args=parts[1:]
    if cmd=="/start": on_start(chat,u)
    elif cmd=="/help": send(chat,HELP)
    elif cmd=="/metar": on_metar(chat,args)
    elif cmd=="/setcid":
        user_save(uid,u.get("username"),awaiting_cid=1); send(chat,"Пришлите новый числовой VATSIM CID.")
    elif cmd=="/language": send(chat,"Выберите язык расшифровки:",keyboard_lang())
    elif cmd=="/profile":
        p=user_get(uid)
        if not p: send(chat,"Сначала выполните /start.")
        else: send(chat,f"<b>Профиль</b>\nTelegram ID: <code>{uid}</code>\nVATSIM CID: <code>{esc(p.get('vatsim_cid') or 'не указан')}</code>\nЯзык: <code>{esc(p.get('language'))}</code>")
    elif cmd in ("/flight","/atc"):
        p=user_get(uid)
        if not p or not p.get("vatsim_cid"): return send(chat,"Сначала укажите CID: /start")
        send(chat,"Проверяю официальный VATSIM Data Feed…")
        try:
            pilot,controllers,feed=get_flight(p["vatsim_cid"])
            if not pilot: return send(chat,f"CID {p['vatsim_cid']} сейчас не найден среди онлайн-пилотов.")
            fp=pilot.get("flight_plan") or {}
            if cmd=="/flight":
                gen=feed.get("general") or {}
                send(chat,"<b>Ваш полёт в VATSIM</b>\n"
                     f"Позывной: <code>{esc(pilot.get('callsign'))}</code>\n"
                     f"Маршрут: <code>{esc(fp.get('departure'))} → {esc(fp.get('arrival'))}</code>\n"
                     f"Тип ВС: {esc(fp.get('aircraft_short') or fp.get('aircraft'))}\n"
                     f"Высота: {esc(pilot.get('altitude'))} ft\n"
                     f"Скорость: {esc(pilot.get('groundspeed'))} kt\n"
                     f"Курс: {esc(pilot.get('heading'))}°\n"
                     f"План маршрута: <code>{esc(fp.get('route') or 'не указан')}</code>\n"
                     f"Обновление feed: {esc(gen.get('update_timestamp'))}")
            else:
                st=stage(pilot)
                names={"ground":"земля / подготовка к вылету","terminal":"терминальный участок (оценка по высоте)","cruise":"маршрутный участок / эшелон"}
                lines=["<b>Подходящие онлайн-позиции ATC</b>",
                       f"Рейс: <code>{esc(pilot.get('callsign'))}</code>",
                       f"Маршрут: <code>{esc(fp.get('departure'))} → {esc(fp.get('arrival'))}</code>",
                       f"Этап: <b>{names[st]}</b>",""]
                if controllers:
                    for dist,call,freq in controllers[:20]:
                        d=f" — примерно {round(dist)} NM" if dist is not None else ""
                        lines.append(f"• <b>{esc(call)}</b> — {esc(freq)} MHz{d}")
                else: lines.append("Подходящих позиций в текущем feed не найдено.")
                lines += ["","<i>Подсказка ориентировочная: feed не содержит полной геометрии секторов и не гарантирует handoff. На эшелоне наземные позиции и вышки аэропортов по пути не предлагаются только из-за их близости к маршруту. Сверяйтесь с картой VATSIM и диспетчером.</i>"]
                send(chat,"\n".join(lines))
        except Exception as e: send(chat,f"Ошибка VATSIM: {esc(e)}")
    else:
        send(chat,"Неизвестная команда. Используйте /help.")

def main():
    if not TOKEN:
        raise SystemExit("Не задан BOT_TOKEN. Добавьте BOT_TOKEN в переменные окружения хостинга.")
    init_db()
    # Long polling не работает, если на боте установлен webhook.
    tg("deleteWebhook",{"drop_pending_updates":False})
    offset=None
    log.info("Bot started")
    while True:
        try:
            params={"timeout":45,"allowed_updates":json.dumps(["message","callback_query"])}
            if offset is not None: params["offset"]=offset
            url=API+"/getUpdates?"+urllib.parse.urlencode(params)
            updates=json.loads(get_url(url,timeout=55).decode())
            if not updates.get("ok"):
                time.sleep(3); continue
            for update in updates.get("result",[]):
                offset=update["update_id"]+1
                try:
                    if "callback_query" in update: on_callback(update["callback_query"])
                    elif "message" in update: on_text(update["message"])
                except Exception:
                    log.exception("Update handling error")
        except KeyboardInterrupt:
            log.info("Bot stopped"); break
        except Exception:
            log.exception("Polling error"); time.sleep(5)

if __name__=="__main__":
    main()
