import datetime as dt
import getpass
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Helsinki")
YEAR = 2026
CALENDAR = "🎄 Посмотреть календарь"
EDIT = "✍️ Подготовить сюрприз"
TEST = "🧪 Отправить пробное сообщение"
SAVE = "💾 Сохранить"
PREVIEW = "👀 Предпросмотр"
CANCEL = "↩️ Отмена"


class ApiError(Exception):
    pass


class Bot:
    def __init__(self, token, db):
        self.token = token
        self.db = db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS surprises (day INTEGER PRIMARY KEY, parts TEXT);
            CREATE TABLE IF NOT EXISTS events (kind TEXT, day INTEGER, PRIMARY KEY (kind, day));
        """)
        self.draft = None

    def api(self, method, **params):
        request = Request(
            f"https://api.telegram.org/bot{self.token}/{method}",
            json.dumps(params).encode(),
            {"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=35) as response:
                result = json.load(response)
        except HTTPError as error:
            raise ApiError(f"{method}: HTTP {error.code}") from None
        except (URLError, TimeoutError):
            raise ApiError(f"{method}: ошибка соединения") from None
        if not result["ok"]:
            raise ApiError(f"{method}: Telegram отклонил запрос")
        return result["result"]

    def setting(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    def event(self, kind, day):
        return self.db.execute("SELECT 1 FROM events WHERE kind = ? AND day = ?", (kind, day)).fetchone()

    def record(self, kind, day):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO events VALUES (?, ?)", (kind, day))

    def parts(self, day):
        row = self.db.execute("SELECT parts FROM surprises WHERE day = ?", (day,)).fetchone()
        return json.loads(row[0]) if row else []

    def send(self, chat, text, **options):
        return self.api("sendMessage", chat_id=chat, text=text, **options)

    def menu(self, chat, owner):
        rows = [[CALENDAR]]
        if owner:
            rows += [[EDIT], [TEST]]
        self.send(chat, "🎄 Волшебный адвент-календарь", reply_markup={
            "keyboard": rows, "resize_keyboard": True,
        })

    @staticmethod
    def button(label, data):
        return {"text": label, "callback_data": data}

    def grid(self, chat, mode, today):
        buttons = []
        for day in range(1, 25):
            if mode in ("edit", "test"):
                icon = "💌" if self.parts(day) else "▫️"
            elif today < dt.date(YEAR, 12, day):
                icon = "🔒"
            elif self.event("opened", day):
                icon = "✨"
            else:
                icon = "💌" if self.parts(day) else "▫️"
            buttons.append(self.button(f"{icon} {day}", f"{mode}:{day}"))
        title = "Выбери день" if mode != "open" else f"🎄 Декабрь {YEAR}"
        self.send(chat, title, reply_markup={
            "inline_keyboard": [buttons[i:i + 4] for i in range(0, 24, 4)],
        })

    def deliver(self, chat, parts):
        for part in parts:
            if "photo" in part:
                self.api("sendPhoto", chat_id=chat, **part)
            else:
                self.send(chat, part["text"])

    def announce(self, chat, day, test=False):
        prefix = "🧪 Пробное сообщение\n\n" if test else ""
        action = "test_open" if test else "open"
        self.send(chat, f"{prefix}Сегодня {day}-й день волшебного адвент-календаря. "
                        "Посмотри, что ждёт тебя сегодня! ❤️",
                  reply_markup={"inline_keyboard": [[self.button("💌 Открыть", f"{action}:{day}")]]})

    def callback(self, query, chat, owner, today):
        def answer(message=""):
            args = {"text": message} if message else {}
            self.api("answerCallbackQuery", callback_query_id=query["id"], **args)

        match = re.fullmatch(r"(edit|test|test_open|open):([1-9]|1[0-9]|2[0-4])", query.get("data", ""))
        if not match:
            return answer()
        action, number = match.groups()
        day = int(number)
        if action != "open" and not owner:
            return answer("Эта кнопка доступна только автору.")
        if action == "open" and today < dt.date(YEAR, 12, day):
            return answer(f"Это окошко откроется {day} декабря ✨")
        parts = self.parts(day)
        if action == "edit":
            if self.draft is not None:
                return answer("Сначала сохрани или отмени текущий черновик.")
            answer()
            self.draft = (day, [])
            self.send(chat, f"Окошко {day} декабря. Отправь текст и фото в нужном порядке. "
                      "При сохранении они целиком заменят прежний сюрприз.",
                      reply_markup={"keyboard": [[PREVIEW, SAVE], [CANCEL]], "resize_keyboard": True})
        elif not parts:
            answer("Сюрприз для этого дня пока не готов.")
        elif action == "test":
            answer()
            self.announce(chat, day, test=True)
        else:
            answer()
            self.deliver(chat, parts)
            if action == "open" and not owner:
                self.record("opened", day)

    def message(self, msg, chat, owner, today):
        text = msg.get("text", "")
        if text.split(maxsplit=1)[:1] == ["/start"]:
            self.menu(chat, owner)
            if owner:
                self.send(chat, "/invite — ссылка для него\n/time 08:00 — время по Хельсинки")
        elif owner and text == "/invite":
            if self.setting("recipient"):
                return self.send(chat, "Получатель уже подключён ❤️")
            name = self.api("getMe")["username"]
            self.send(chat, f"https://t.me/{name}?start={self.setting('invite')}")
        elif owner and text.split(maxsplit=1)[:1] == ["/time"]:
            value = text[5:].strip()
            if re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
                self.set("time", value)
                self.send(chat, f"Время отправки — {value} по Хельсинки.")
            else:
                self.send(chat, f"Сейчас {self.setting('time', '08:00')} по Хельсинки. Пример: /time 09:00")
        elif owner and self.draft is not None:
            day, parts = self.draft
            if text == CANCEL:
                self.draft = None
                self.menu(chat, owner)
            elif text == SAVE and parts:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO surprises VALUES (?, ?)", (day, json.dumps(parts)))
                self.draft = None
                self.send(chat, f"Сюрприз на {day} декабря сохранён 💌")
                self.menu(chat, owner)
            elif text == PREVIEW and parts:
                self.deliver(chat, parts)
            elif text in (PREVIEW, SAVE):
                self.send(chat, "Сначала добавь текст или фото.")
            elif msg.get("photo"):
                parts.append({"photo": msg["photo"][-1]["file_id"], "caption": msg.get("caption", "")})
            elif text:
                parts.append({"text": text})
            else:
                self.send(chat, "Отправь текст или картинку как фото.")
        elif text in (CALENDAR, "/calendar", "посмотреть календарь"):
            self.grid(chat, "open", today)
        elif owner and text in (EDIT, TEST):
            self.grid(chat, "edit" if text == EDIT else "test", today)

    def update(self, update, today):
        query = update.get("callback_query")
        msg = query.get("message") if query else update.get("message")
        if not msg or msg["chat"]["type"] != "private":
            return
        chat = str(msg["chat"]["id"])
        text = msg.get("text", "")
        owner = chat == self.setting("owner")
        recipient = self.setting("recipient")
        if not query and text.startswith("/start "):
            key = text.split(maxsplit=1)[1]
            setup = self.setting("setup")
            invite = self.setting("invite")
            if not self.setting("owner") and setup and hmac.compare_digest(key.encode(), setup.encode()):
                self.set("owner", chat)
                owner = True
            elif not owner and not recipient and invite and hmac.compare_digest(key.encode(), invite.encode()):
                self.set("recipient", chat)
                recipient = chat
        if not owner and chat != recipient:
            if query:
                self.api("answerCallbackQuery", callback_query_id=query["id"])
            elif text.startswith("/start"):
                self.send(chat, "Этот календарь открывается по персональной ссылке 💌")
            return
        if query:
            self.callback(query, chat, owner, today)
        else:
            self.message(msg, chat, owner, today)

    def scheduled(self, now):
        if now.year != YEAR or now.month != 12 or now.day > 24:
            return
        if now.strftime("%H:%M") < self.setting("time", "08:00"):
            return
        recipient = self.setting("recipient")
        owner = self.setting("owner")
        day = now.day
        if not self.parts(day):
            if owner and not self.event("reminder", day):
                self.send(owner, f"Окошко на {day} декабря пока пустое. Заполни его, когда будешь готова 💌")
                self.record("reminder", day)
        elif recipient and not self.event("sent", day):
            self.announce(recipient, day)
            self.record("sent", day)

    def run(self):
        name = self.api("getMe")["username"]
        if not self.setting("invite"):
            self.set("invite", secrets.token_urlsafe(24))
        if not self.setting("owner"):
            if not self.setting("setup"):
                self.set("setup", secrets.token_urlsafe(24))
            print(f"Личная ссылка автора: https://t.me/{name}?start={self.setting('setup')}", flush=True)
        self.api("setMyCommands", commands=[
            {"command": "start", "description": "Открыть меню"},
            {"command": "calendar", "description": "Посмотреть календарь"},
        ])
        print(f"@{name} запущен. Для остановки нажми Ctrl+C.", flush=True)
        offset = int(self.setting("offset", "0"))
        while True:
            try:
                self.scheduled(dt.datetime.now(ZONE))
            except ApiError as error:
                print(error, flush=True)
            try:
                updates = self.api("getUpdates", offset=offset, timeout=15,
                                   allowed_updates=["message", "callback_query"])
            except ApiError as error:
                print(error, flush=True)
                time.sleep(5)
                continue
            for update in updates:
                try:
                    self.update(update, dt.datetime.now(ZONE).date())
                except ApiError as error:
                    print(error, flush=True)
                offset = update["update_id"] + 1
                self.set("offset", offset)


def main():
    cloud = bool(os.environ.get("RAILWAY_ENVIRONMENT_ID"))
    volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
    path = Path(os.environ.get(
        "ADVENT_DB",
        str(Path(volume) / "advent.sqlite3") if volume else str(Path(__file__).with_name("advent.sqlite3")),
    ))
    if cloud:
        if not volume:
            raise SystemExit("Подключи Volume к сервису адвента по пути /data.")
        # Wait without opening the database so a complete file can be uploaded first.
        print("Подготовка адвента. Для запуска нужны база и ADVENT_BOT_TOKEN.", flush=True)
        while not path.is_file() or not os.environ.get("ADVENT_BOT_TOKEN", "").strip():
            time.sleep(5)
        uri = path.resolve().as_uri() + "?mode=rw"
        db = sqlite3.connect(uri, uri=True)
    else:
        db = sqlite3.connect(path)
    token = (os.environ.get("ADVENT_BOT_TOKEN") or getpass.getpass("Токен BotFather: ")).strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        db.close()
        raise SystemExit("Проверь формат ADVENT_BOT_TOKEN в настройках сервиса.")
    try:
        with db:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise SystemExit("Проверка базы не пройдена.")
            if cloud:
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"settings", "surprises", "events"}.issubset(tables):
                    raise SystemExit("Загруженная база адвента не содержит нужных таблиц.")
            Bot(token, db).run()
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except ApiError as error:
        raise SystemExit(str(error)) from None
