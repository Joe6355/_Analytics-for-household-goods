import argparse
import json
import logging
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from account_storage import DEFAULT_DATABASE, AccountStorage, RequestError


DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 64 * 1024


class AccountHandler(BaseHTTPRequestHandler):
    timeout = 10

    def do_GET(self):
        self.handle_api()

    def do_POST(self):
        self.handle_api()

    def handle_api(self):
        try:
            path = urlsplit(self.path).path
            data = self.read_json() if self.command == "POST" else None
            scheme, _, token = self.headers.get("Authorization", "").partition(" ")
            status, data = self.server.storage.request(path, data=data, token=token if scheme.lower() == "bearer" else None)
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


class AccountServer(ThreadingHTTPServer):
    def __init__(self, port, database_path):
        self.storage = AccountStorage(database_path)
        self.storage.initialize()
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
