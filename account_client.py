import json
import os
import re
from html import escape

from PyQt6.QtCore import QByteArray, QDateTime, QObject, Qt, QUrl
from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PyQt6.QtWidgets import QTableWidgetItem


DEFAULT_API_URL = "http://127.0.0.1:8765"
MAX_RESPONSE_BYTES = 1024 * 1024
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class AccountController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.network = QNetworkAccessManager(self)
        self.api_url = QUrl(os.environ.get("HOUSEHOLD_API_URL", DEFAULT_API_URL).strip(), QUrl.ParsingMode.StrictMode)
        self.token = None
        self.history_request = 0
        self.server_rows = []
        self.csv_rows = None

        window.accountStatus.setTextFormat(Qt.TextFormat.PlainText)
        window.historyHint.setTextFormat(Qt.TextFormat.PlainText)
        window.signInTitle.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        window.signInButton.clicked.connect(self.sign_in_or_out)
        window.registerButton.clicked.connect(lambda: self.authenticate("/register"))
        window.passwordInput.returnPressed.connect(self.sign_in_or_out)
        self.show_session()

    def send(self, path, on_finished, *, data=None, token=None):
        url = self.api_url
        local_http = url.scheme() == "http" and url.host() in ("127.0.0.1", "::1", "localhost")
        if (not url.isValid() or not url.host() or url.userInfo() or url.hasQuery() or url.hasFragment()
                or (url.scheme() != "https" and not local_http)):
            on_finished(0, {}, "Укажите HTTPS-адрес сервера или адрес localhost в HOUSEHOLD_API_URL.")
            return

        request = QNetworkRequest(QUrl(url.toString().rstrip("/") + path))
        request.setTransferTimeout(8000)
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        request.setRawHeader(b"Accept", b"application/json")
        if token:
            request.setRawHeader(b"Authorization", f"Bearer {token}".encode("ascii"))

        if data is None:
            reply = self.network.get(request)
        else:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            body = QByteArray(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            reply = self.network.post(request, body)
        reply.setReadBufferSize(MAX_RESPONSE_BYTES + 1)
        reply.readyRead.connect(lambda: self.check_response_size(reply))
        reply.finished.connect(lambda: self.finish_request(reply, on_finished))

    def check_response_size(self, reply):
        if reply.bytesAvailable() > MAX_RESPONSE_BYTES:
            reply.abort()

    def finish_request(self, reply, on_finished):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute) or 0
        raw = bytes(reply.read(MAX_RESPONSE_BYTES + 1) or b"")
        network_error = reply.error()
        reply.deleteLater()
        try:
            data = json.loads(raw.decode("utf-8")) if len(raw) <= MAX_RESPONSE_BYTES else None
        except (ValueError, RecursionError):
            data = None

        if 200 <= status < 300 and network_error == QNetworkReply.NetworkError.NoError and isinstance(data, dict):
            on_finished(status, data, "")
        elif status and isinstance(data, dict) and isinstance(data.get("error"), str):
            on_finished(status, {}, data["error"])
        elif status and network_error == QNetworkReply.NetworkError.NoError:
            on_finished(status, {}, "Получен неожиданный ответ сервера.")
        else:
            on_finished(status, {}, "Не удалось связаться с сервером личного кабинета.")

    def set_auth_busy(self, busy):
        for field in (self.window.emailInput, self.window.passwordInput,
                      self.window.signInButton, self.window.registerButton):
            field.setEnabled(not busy)

    def authenticate(self, path):
        email = self.window.emailInput.text().strip()
        password = self.window.passwordInput.text()
        if not email or not password:
            self.window.accountStatus.setText("Введите почту и пароль.")
            return

        self.set_auth_busy(True)
        self.window.accountStatus.setText("Подключаюсь к серверу…")
        self.send(path, self.finish_authentication, data={"email": email, "password": password})

    def finish_authentication(self, _status, data, error):
        self.set_auth_busy(False)
        token = data.get("token")
        email = data.get("email")
        if error or not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token) or not isinstance(email, str):
            self.window.accountStatus.setText(error or "Получен неожиданный ответ сервера.")
            return

        self.token = token
        self.window.emailInput.setText(email)
        self.show_session()
        self.refresh_history()

    def sign_in_or_out(self):
        if not self.token:
            self.authenticate("/login")
            return

        token = self.token
        self.token = None
        self.show_session()
        self.set_auth_busy(True)
        self.window.accountStatus.setText("Завершаю сеанс…")
        self.send("/logout", self.finish_logout, data={}, token=token)

    def finish_logout(self, _status, _data, error):
        self.set_auth_busy(False)
        message = "Сервер пока не подтвердил завершение сеанса." if error else "Вы вышли из аккаунта."
        self.window.accountStatus.setText(message)

    def show_session(self):
        window = self.window
        signed_in = self.token is not None
        window.emailInput.setReadOnly(signed_in)
        window.passwordInput.clear()
        window.passwordLabel.setVisible(not signed_in)
        window.passwordInput.setVisible(not signed_in)
        window.registerButton.setVisible(not signed_in)
        window.signInButton.setText("Выйти" if signed_in else "Войти")
        window.signInTitle.setText("Аккаунт" if signed_in else "Вход")
        window.accountStatus.setText("" if signed_in else "Для регистрации нужен пароль от 15 символов.")
        self.server_rows = []
        self.csv_rows = None
        window.historyTable.setRowCount(0)
        window.historyHint.setText("" if signed_in else "История появится после входа.")

    def end_expired_session(self):
        self.token = None
        self.show_session()
        self.window.accountStatus.setText("Сеанс завершён. Войдите снова.")

    def refresh_history(self):
        token = self.token
        if not token or self.csv_rows is not None:
            return
        self.history_request += 1
        request_number = self.history_request
        self.window.historyHint.setText("Загружаю историю…")

        def display(status, data, error):
            if token != self.token or request_number != self.history_request:
                return
            if status == 401:
                self.end_expired_session()
            elif error:
                if self.csv_rows is None:
                    self.window.historyHint.setText(error)
            else:
                self.show_history(data.get("entries"))

        self.send("/history", display, token=token)

    def show_history(self, entries):
        if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
            if self.csv_rows is None:
                self.window.historyHint.setText("Сервер вернул историю в неизвестном формате.")
            return

        rows = []
        for entry in entries:
            created_at = QDateTime.fromString(str(entry.get("created_at", "")), Qt.DateFormat.ISODate)
            date = created_at.toLocalTime().toString("dd.MM.yyyy HH:mm") if created_at.isValid() else "—"
            product = str(entry.get("product", "—"))
            action = str(entry.get("action", "—"))
            details = str(entry.get("details", ""))
            rows.append((date, product, action, details))
        self.server_rows = rows
        if self.csv_rows is None:
            self.display_history(rows)
            self.window.historyHint.setText("Наведи на действие, чтобы посмотреть результат." if rows else "Пока пусто.")

    def display_history(self, rows):
        table = self.window.historyTable
        table.setRowCount(len(rows))
        for row, (date, product, action, details) in enumerate(rows):
            for column, text in enumerate((date, product, action)):
                item = QTableWidgetItem(text)
                tip = details if column == 2 and details else text
                item.setToolTip("<qt>" + escape(tip).replace("\n", "<br>") + "</qt>")
                if column == 2:
                    item.setData(Qt.ItemDataRole.UserRole, details)
                table.setItem(row, column, item)

    def show_csv_history(self, rows):
        self.csv_rows = rows
        self.display_history(rows)
        self.window.historyHint.setText(
            f"История из CSV: {len(rows)} записей. Для возврата нажмите «История аккаунта»."
        )

    def restore_history(self):
        self.csv_rows = None
        self.display_history(self.server_rows)
        self.window.historyHint.setText("Пока пусто." if self.token else "История появится после входа.")
        self.refresh_history()

    def record(self, action, product, details):
        token = self.token
        if not token:
            return
        payload = {"action": action, "product": product.strip()[:160] or "Без названия", "details": details[:600]}

        def saved(status, _data, error):
            if token != self.token:
                return
            if status == 401:
                self.end_expired_session()
                self.window.statusBar().showMessage("Сеанс завершён. Войдите снова, чтобы сохранять историю.", 8000)
            elif error:
                self.window.statusBar().showMessage(f"Не удалось сохранить действие. {error}", 8000)
            elif self.window.sectionStack.currentWidget() is self.window.accountPage:
                self.refresh_history()

        self.send("/history", saved, data=payload, token=token)
