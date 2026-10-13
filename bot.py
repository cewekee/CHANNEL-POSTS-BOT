"""
CHANNEL POSTS BOT — автономный постер для Telegram-канала в стиле ScalpLedger.

Каждый запуск (GitHub Actions раз в 10 минут) бот:
  1. забирает эмодзи из сообщений, которые вы переслали боту в личку (кастомные эмодзи → emoji.json),
     и запоминает каналы, куда его добавили (можно не искать CHAT_ID вручную);
  2. решает, пора ли публиковать, и публикует один пост;
  3. сохраняет состояние в state.json (его коммитит workflow).

Типы постов:
  • утро    — раз в день в случайное время 09:00–12:00: приветствие, BTC за сутки, призыв к реакциям;
  • шутка   — одна строка с эмодзи;
  • мем     — картинка: подпись сверху, график, панчлайн снизу + подпись к посту;
  • факт    — карточка «ФАКТ» с датой и львом + подпись.
Между постами — случайно от 60 до 150 минут. Ночью (00:30–09:00) тишина.

Переменные окружения:
  BOT_TOKEN          — токен бота (обязательно)
  CHAT_ID            — канал: @username или -100…; если пусто/AUTO/неверно — берётся канал, где бот админ
  ANTHROPIC_API_KEY  — ключ Claude для свежих шуток и мемов (необязательно, без него — банк)
  TZ_NAME            — часовой пояс расписания, по умолчанию Europe/Zagreb
  FORCE              — morning | joke | meme | fact — опубликовать сразу (для теста)
  DRY_RUN=1          — ничего не отправлять, сохранить посты в папку out/
"""

import html
import json
import logging
import os
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import content
import render

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
EMOJI_FILE = ROOT / "emoji.json"

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "").strip()
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Zagreb"))
DRY_RUN = os.getenv("DRY_RUN") == "1"
FORCE = os.getenv("FORCE", "").strip().lower()

MIN_GAP, MAX_GAP = 60, 150          # минуты между постами
DAY_START, DAY_END = (9, 0), (0, 30)  # постим с 09:00 до 00:30
MORNING_FROM, MORNING_TO = 9 * 60, 12 * 60
WEIGHTS = {"joke": 0.38, "meme": 0.37, "fact": 0.25}
MAX_FACTS_PER_DAY = 2

API = f"https://api.telegram.org/bot{BOT_TOKEN.strip()}"
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("posts-bot")


# ---------- файлы ----------

def load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- эмодзи ----------

class Emoji:
    """Выбирает эмодзи для поста. Кастомные (из пересланных постов) — если есть, иначе обычные."""

    def __init__(self, cfg: dict):
        self.pool = cfg.get("custom", [])
        self.plain = cfg.get("plain", ["🦁"])
        self.used: list[dict] = []

    def pick(self) -> int:
        src = [e for e in self.pool if e not in self.used] or self.pool
        if src:
            e = random.choice(src)
        else:
            e = {"alt": random.choice([p for p in self.plain if p not in [u["alt"] for u in self.used]] or self.plain)}
        self.used.append(e)
        return len(self.used) - 1

    def html(self, idx: int, custom: bool) -> str:
        e = self.used[idx]
        if custom and e.get("id"):
            return f'<tg-emoji emoji-id="{e["id"]}">{html.escape(e["alt"])}</tg-emoji>'
        alt = e["alt"]
        return alt if not e.get("id") else random.choice(self.plain)


def utf16_slice(text: str, offset: int, length: int) -> str:
    b = text.encode("utf-16-le")
    return b[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def collect_emoji(state: dict) -> None:
    """Забирает кастомные эмодзи из сообщений в личке и запоминает каналы, куда добавили бота."""
    if DRY_RUN or not BOT_TOKEN:
        return
    try:
        r = requests.get(f"{API}/getUpdates", params={
            "offset": state.get("update_offset", 0), "timeout": 0,
            "allowed_updates": '["message","channel_post","my_chat_member"]'}, timeout=20)
        data = r.json()
    except Exception as ex:
        log.warning("getUpdates не удался: %s", ex)
        return
    if not data.get("ok"):
        log.info("getUpdates: %s (если у бота стоит webhook — сбор эмодзи отключён)", data.get("description"))
        return

    cfg = load(EMOJI_FILE, {"custom": [], "plain": ["🦁"]})
    known = {e["id"] for e in cfg["custom"]}
    replies: dict[int, int] = {}
    for upd in data.get("result", []):
        state["update_offset"] = upd["update_id"] + 1
        # запоминаем каналы/группы, куда бота добавили, — чтобы не искать CHAT_ID вручную
        for key in ("my_chat_member", "channel_post"):
            ch = (upd.get(key) or {}).get("chat") or {}
            if ch.get("type") in ("channel", "supergroup", "group"):
                status = ((upd.get(key) or {}).get("new_chat_member") or {}).get("status", "administrator")
                chats = state.setdefault("known_chats", {})
                if status in ("left", "kicked"):
                    chats.pop(str(ch["id"]), None)
                else:
                    chats[str(ch["id"])] = ch.get("title", "")
                    log.info("Бот видит чат: %s (%s)", ch.get("title"), ch["id"])
        msg = upd.get("message") or {}
        if (msg.get("chat") or {}).get("type") != "private":
            continue
        text = msg.get("text") or msg.get("caption") or ""
        ents = msg.get("entities") or msg.get("caption_entities") or []
        added = 0
        for ent in ents:
            if ent.get("type") == "custom_emoji" and ent.get("custom_emoji_id") not in known:
                cfg["custom"].append({"id": ent["custom_emoji_id"],
                                      "alt": utf16_slice(text, ent["offset"], ent["length"]).strip() or "⭐"})
                known.add(ent["custom_emoji_id"])
                added += 1
        st = msg.get("sticker")
        if st and st.get("custom_emoji_id") and st["custom_emoji_id"] not in known:
            cfg["custom"].append({"id": st["custom_emoji_id"], "alt": st.get("emoji", "⭐")})
            known.add(st["custom_emoji_id"])
            added += 1
        replies[msg["chat"]["id"]] = replies.get(msg["chat"]["id"], 0) + added

    if replies:
        save(EMOJI_FILE, cfg)
        for chat, n in replies.items():
            tg("sendMessage", {"chat_id": chat, "text":
                f"Добавил новых эмодзи: {n}. Всего в пуле: {len(cfg['custom'])}.\n"
                "Пересылайте ещё посты с нужными эмодзи — я их подхвачу."})


# ---------- Telegram ----------

def tg(method: str, data: dict, files: dict | None = None) -> dict:
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/{method}", data=data, files=files, timeout=60)
            j = r.json()
        except Exception as ex:
            log.warning("Сеть: %s", ex)
            time.sleep(3)
            continue
        if j.get("ok"):
            return j
        retry = (j.get("parameters") or {}).get("retry_after")
        if r.status_code == 429 and retry:
            time.sleep(retry + 1)
            continue
        return j
    return {"ok": False, "description": "network"}


def publish(post: dict, em: Emoji) -> bool:
    """post: {"text": шаблон с {e0},{e1}…} или {"photo": bytes, "caption": шаблон}."""
    def build(custom: bool) -> str:
        tpl = post.get("text") or post.get("caption") or ""
        return tpl.format(**{f"e{i}": em.html(i, custom) for i in range(len(em.used))})

    if DRY_RUN:
        out = ROOT / "out"
        out.mkdir(exist_ok=True)
        stamp = datetime.now(TZ).strftime("%H%M%S") + f"_{post['kind']}"
        (out / f"{stamp}.txt").write_text(build(False), encoding="utf-8")
        if "photo" in post:
            (out / f"{stamp}.jpg").write_bytes(post["photo"])
        log.info("DRY_RUN: сохранён %s", stamp)
        return True

    for custom in (True, False):  # если кастомные эмодзи недоступны боту — шлём обычные
        body = build(custom)
        if "photo" in post:
            j = tg("sendPhoto", {"chat_id": CHAT_ID, "caption": body, "parse_mode": "HTML"},
                   files={"photo": ("post.jpg", post["photo"], "image/jpeg")})
        else:
            j = tg("sendMessage", {"chat_id": CHAT_ID, "text": body, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"})
        if j.get("ok"):
            return True
        log.error("Telegram: %s", j.get("description"))
        if not custom or not any(e.get("id") for e in em.used):
            return False
    return False


# ---------- рынок ----------

def btc_change() -> float | None:
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                         params={"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"},
                         timeout=20)
        return float(r.json()["bitcoin"]["usd_24h_change"])
    except Exception:
        pass
    try:
        r = requests.get("https://api.exchange.coinbase.com/products/BTC-USD/stats", timeout=20)
        s = r.json()
        return (float(s["last"]) / float(s["open"]) - 1) * 100
    except Exception as ex:
        log.warning("Не получил цену BTC: %s", ex)
        return None


# ---------- посты ----------

def esc(s: str) -> str:
    return html.escape(s, quote=False)


def _safe(tpl_text: str) -> str:
    """Экранирует HTML и фигурные скобки в тексте для str.format."""
    return esc(tpl_text).replace("{", "{{").replace("}", "}}")


def make_morning(em: Emoji) -> dict:
    a, b, c = em.pick(), em.pick(), em.pick()
    cta = random.choice(content.CTA)
    if "{up}" in cta:  # реакции — обычные эмодзи, их можно поставить под постом
        up, down = random.choice(content.REACTION_PAIRS)
        cta_html, tail = _safe(cta.format(up=up, down=down)), ""
    else:
        cta_html, tail = _safe(cta), f" {{e{c}}}"
    text = (f"{{e{a}}} <b>{_safe(random.choice(content.MORNING_HEADERS))}</b>\n\n"
            f"{_safe(content.btc_line(btc_change()))}\n\n"
            f"{{e{b}}} {cta_html}{tail}")
    return {"kind": "morning", "text": text}


def make_joke(em: Emoji, hist: dict) -> dict:
    joke = content.gen_joke(hist["jokes"])
    hist["jokes"].append(joke)
    return {"kind": "joke", "text": f"{{e{em.pick()}}} " + _safe(joke)}


def make_meme(em: Emoji, hist: dict) -> dict:
    m = content.gen_meme(hist["memes"])
    hist["memes"].append(f'{m.get("top", "")} / {m["bottom"]}')
    photo = render.render_meme(m.get("top", ""), m["bottom"], m["shape"], m.get("label"))
    return {"kind": "meme", "photo": photo, "caption": f"{{e{em.pick()}}} " + _safe(m["caption"])}


def make_fact(em: Emoji, hist: dict, now: datetime) -> dict:
    fact = content.pick_unused(content.FACTS, hist["facts"])
    hist["facts"].append(fact)
    photo = render.render_fact(fact, f"{now.day} {MONTHS[now.month - 1]}")
    return {"kind": "fact", "photo": photo,
            "caption": f"{{e{em.pick()}}} " + _safe(random.choice(content.FACT_CAPTIONS))}


# ---------- расписание ----------

def in_active_hours(now: datetime) -> bool:
    m = now.hour * 60 + now.minute
    return m >= DAY_START[0] * 60 + DAY_START[1] or m < DAY_END[0] * 60 + DAY_END[1]


def choose_kind(state: dict, today: str) -> str:
    kinds = dict(WEIGHTS)
    if state.get("last_kind") in kinds and len(kinds) > 1:
        kinds.pop(state["last_kind"])  # не два одинаковых подряд
    if state.get("facts_day") == today and state.get("facts_count", 0) >= MAX_FACTS_PER_DAY:
        kinds.pop("fact", None)
    return random.choices(list(kinds), weights=list(kinds.values()))[0]


def main() -> None:
    if not DRY_RUN and not BOT_TOKEN:
        raise SystemExit("Нужен BOT_TOKEN")

    state = load(STATE_FILE, {})
    hist = state.setdefault("history", {})
    for k in ("jokes", "memes", "facts"):
        hist.setdefault(k, [])

    collect_emoji(state)

    global CHAT_ID
    known = state.get("known_chats", {})
    if known:
        log.info("Каналы, где есть бот: %s", ", ".join(f"{t} = {i}" for i, t in known.items()))
    # CHAT_ID не задан, задан как AUTO или неверный, а бот состоит ровно в одном канале — берём его
    if len(known) == 1 and (CHAT_ID.upper() in ("", "AUTO") or CHAT_ID not in known and not CHAT_ID.startswith("@")):
        CHAT_ID = next(iter(known))
        log.info("Публикую в найденный канал %s", CHAT_ID)

    now = datetime.now(TZ)
    ts = time.time()
    # «день» канала начинается в 09:00, ночь после полуночи относится к прошлому дню
    day = (now - timedelta(hours=1)).date().isoformat() if now.hour < 1 else now.date().isoformat()

    if state.get("day") != day:
        state["day"] = day
        state["morning_done"] = False
        mins = random.randint(MORNING_FROM, MORNING_TO)
        state["morning_at"] = now.replace(hour=mins // 60, minute=mins % 60, second=0).timestamp()

    since_last = (ts - state.get("last_post_at", 0)) / 60
    kind = FORCE or None

    if not kind:
        if not in_active_hours(now):
            log.info("Ночь, молчим")
        elif not state.get("morning_done"):
            if now.hour >= 13:          # утро уже упущено (например, сбой) — живём дальше
                state["morning_done"] = True
            elif ts >= state["morning_at"] and since_last >= MIN_GAP:
                kind = "morning"
        elif ts >= state.get("next_at", 0):
            kind = choose_kind(state, day)
        else:
            log.info("До следующего поста %.0f мин", (state["next_at"] - ts) / 60)

    failed = False
    if kind:
        em = Emoji(load(EMOJI_FILE, {"custom": [], "plain": ["🦁"]}))
        builders = {"morning": lambda: make_morning(em), "joke": lambda: make_joke(em, hist),
                    "meme": lambda: make_meme(em, hist), "fact": lambda: make_fact(em, hist, now)}
        post = builders.get(kind, builders["joke"])()
        if publish(post, em):
            log.info("Опубликован пост: %s", kind)
            state["last_kind"] = kind
            state["last_post_at"] = ts
            state["next_at"] = ts + random.uniform(MIN_GAP, MAX_GAP - 10) * 60  # −10: шаг проверки 10 мин
            if kind == "morning":
                state["morning_done"] = True
            if kind == "fact":
                state["facts_count"] = state.get("facts_count", 0) + 1 if state.get("facts_day") == day else 1
                state["facts_day"] = day
            log.info("Следующий пост через %.0f мин", (state["next_at"] - ts) / 60)
        else:
            failed = True
            if not known:
                log.error("Бот не видит ни одного канала. Добавьте его в канал администратором "
                          "(или удалите и добавьте заново) — при следующем запуске он найдёт канал сам.")

    for k in hist:  # история не растёт бесконечно
        hist[k] = hist[k][-300:]
    if not DRY_RUN:
        save(STATE_FILE, state)
    if failed:  # запуск в GitHub станет красным — сразу видно, что пост не ушёл
        raise SystemExit("Пост не отправлен — смотрите ошибку Telegram выше")


if __name__ == "__main__":
    main()
