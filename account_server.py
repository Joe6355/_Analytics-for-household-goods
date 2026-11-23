import argparse
import json
import logging
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac, sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import time
from urllib.parse import urlsplit


DEFAULT_PORT = 8765
DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "accounts.sqlite3"
PASSWORD_ROUNDS = 600_000
SESSION_SECONDS = 24 * 60 * 60
HISTORY_LIMIT = 100
MAX_REQUEST_BYTES = 64 * 1024
EMAIL_PATTERN = re.compile(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    salt BLOB NOT NULL,
    password_hash BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash BLOB PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    product TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL,
    snapshot TEXT
);
CREATE INDEX IF NOT EXISTS history_by_user ON history(user_id, id);
"""


class RequestError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def open_database(path):
    database = sqlite3.connect(path)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return database


def password_hash(password, salt):
    return pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ROUNDS)


def read_credentials(data, *, registering):
    email = data.get("email")
    password = data.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise RequestError(400, "Введите почту и пароль.")
    email = email.strip().casefold()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise RequestError(400, "Проверьте адрес электронной почты.")
    if registering and not 15 <= len(password) <= 128:
        raise RequestError(400, "Введите пароль длиной от 15 до 128 символов.")
    if not 1 <= len(password) <= 128:
        raise RequestError(400, "Проверьте пароль.")
    return email, password


def create_session(database, user_id, email):
    now = int(time())
    token = secrets.token_urlsafe(32)
    database.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    database.execute(
        "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
        (sha256(token.encode("ascii")).digest(), user_id, now + SESSION_SECONDS),
    )
    return {"email": email, "token": token}


class AccountHandler(BaseHTTPRequestHandler):
    timeout = 10

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        try:
            with closing(open_database(self.server.database_path)) as database, database:
                status, data = self.route(database)
        except RequestError as error:
            status, data = error.status, {"error": str(error)}
        except (ValueError, RecursionError):
            status, data = 400, {"error": "Проверьте данные запроса."}
        except TimeoutError:
            status, data = 408, {"error": "Время получения запроса истекло."}
        except sqlite3.Error:
            logging.exception("Ошибка базы данных личного кабинета")
            status, data = 503, {"error": "База данных временно недоступна."}

        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length_header = self.headers.get("Content-Length", "")
        if self.headers.get("Transfer-Encoding") or not length_header.isascii() or not length_header.isdecimal():
            raise RequestError(400, "Ожидается запрос с данными JSON.")
        length = int(length_header)
        if not 0 < length <= MAX_REQUEST_BYTES:
            raise RequestError(413, "Слишком большой запрос.")
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "Ожидаются данные JSON.")
        body = self.rfile.read(length)
        if len(body) != length:
            raise RequestError(400, "Не удалось прочитать запрос целиком.")
        data = json.loads(body.decode("utf-8"))
        if not isinstance(data, dict):
            raise RequestError(400, "Ожидается объект JSON.")
        return data

    def route(self, database):
        path = urlsplit(self.path).path
        if self.command == "POST" and path == "/register":
            return 201, self.register(database)
        if self.command == "POST" and path == "/login":
            return 200, self.login(database)
        if self.command == "POST" and path == "/logout":
            token_hash = self.read_token()
            database.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return 200, {}
        if path == "/history":
            user_id = self.authenticate(database)
            if self.command == "POST":
                self.save_history(database, user_id)
                return 201, {}
            rows = database.execute(
                "SELECT id, created_at, product, action, details, snapshot FROM history "
                "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, HISTORY_LIMIT),
            )
            return 200, {
                "entries": [{**row, "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None} for row in rows],
            }
        raise RequestError(404, "Этот адрес приложения не найден на сервере.")

    def register(self, database):
        email, password = read_credentials(self.read_json(), registering=True)
        salt = secrets.token_bytes(16)
        result = database.execute(
            "INSERT INTO users (email, salt, password_hash) VALUES (?, ?, ?) ON CONFLICT(email) DO NOTHING",
            (email, salt, password_hash(password, salt)),
        )
        if not result.rowcount:
            raise RequestError(409, "Эта почта уже зарегистрирована. Войдите в аккаунт.")
        return create_session(database, result.lastrowid, email)

    def login(self, database):
        email, password = read_credentials(self.read_json(), registering=False)
        user = database.execute(
            "SELECT id, salt, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
        salt = user["salt"] if user else bytes(16)
        actual_hash = password_hash(password, salt)
        if user is None or not secrets.compare_digest(actual_hash, user["password_hash"]):
            raise RequestError(401, "Неверная почта или пароль.")
        return create_session(database, user["id"], email)

    def read_token(self):
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or len(token) != 43 or not token.isascii():
            raise RequestError(401, "Войдите в аккаунт.")
        return sha256(token.encode("ascii")).digest()

    def authenticate(self, database):
        session = database.execute(
            "SELECT user_id FROM sessions WHERE token_hash = ? AND expires_at > ?",
            (self.read_token(), int(time())),
        ).fetchone()
        if session is None:
            raise RequestError(401, "Сеанс завершён. Войдите снова.")
        return session["user_id"]

    def save_history(self, database, user_id):
        data = self.read_json()
        product = data.get("product")
        action = data.get("action")
        details = data.get("details", "")
        snapshot = data.get("snapshot")
        if not isinstance(product, str) or not product.strip():
            raise RequestError(400, "Проверьте название товара в записи истории.")
        if not isinstance(action, str) or not 1 <= len(action.strip()) <= 64:
            raise RequestError(400, "Проверьте действие в записи истории.")
        if not isinstance(details, str):
            raise RequestError(400, "Проверьте описание результата.")
        if snapshot is not None and (
                not isinstance(snapshot, dict) or any(type(value) not in (str, int) for value in snapshot.values())):
            raise RequestError(400, "Проверьте параметры сохранённого расчёта.")
        parameters = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")) if snapshot is not None else None
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        database.execute(
            "INSERT INTO history (user_id, created_at, product, action, details, snapshot) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, created_at, product.strip(), action.strip(), details, parameters),
        )


class AccountServer(ThreadingHTTPServer):
    def __init__(self, port, database_path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(open_database(self.database_path)) as database:
            database.execute("PRAGMA journal_mode = WAL")
            database.executescript(SCHEMA)
            with database:
                database.execute("BEGIN IMMEDIATE")
                if "snapshot" not in {row["name"] for row in database.execute("PRAGMA table_info(history)")}:
                    database.execute("ALTER TABLE history ADD COLUMN snapshot TEXT")
        super().__init__(("127.0.0.1", port), AccountHandler)


def main():
    parser = argparse.ArgumentParser(description="Сервер личного кабинета")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    args = parser.parse_args()
    with AccountServer(args.port, args.database) as server:
        print(f"Сервер личного кабинета открыт на http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
