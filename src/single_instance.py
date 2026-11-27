from __future__ import annotations

import ctypes
import sys
import time
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_ERROR_ALREADY_EXISTS = 183


class SingleInstanceGuard(QObject):
    """Кроссплатформенная защита от повторного запуска (Named Pipe + Mutex в Windows / Unix-сокет в macOS).
    - Гарантирует запуск только ОДНОГО экземпляра GUI GTA6SubVoice одновременно.
    - При попытке открыть второй экземпляр просит уже запущенный показать окно (даже из трея) и сразу завершается.
    """

    activation_requested = Signal()

    def __init__(self, app_id: str = "GTA6SubVoice_SingleInstance_v1", parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.app_id = app_id
        self._server: Optional[QLocalServer] = None
        self._mutex_handle: Optional[int] = None
        self._is_primary: bool = False

    @property
    def is_primary(self) -> bool:
        return self._is_primary

    def _try_notify_existing(self, timeout_ms: int = 250) -> bool:
        """Подключается к уже запущенному основному экземпляру и просит вывести его окно на передний план."""
        sock = QLocalSocket()
        sock.connectToServer(self.app_id)
        if not sock.waitForConnected(timeout_ms):
            return False
        try:
            sock.write(b"ACTIVATE\n")
            sock.flush()
            sock.waitForBytesWritten(timeout_ms)
        finally:
            sock.disconnectFromServer()
        return True

    def _try_acquire_once(self, notify_existing: bool) -> bool:
        """Одна попытка занять системный mutex и локальный IPC-сервер."""
        # 1. Атомарный системный mutex в Windows исключает гонки при одновременном запуске
        if sys.platform == "win32":
            try:
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.CreateMutexW(None, False, f"Local\\{self.app_id}")
                last_err = kernel32.GetLastError()
                if handle:
                    self._mutex_handle = int(handle)
                if last_err == _ERROR_ALREADY_EXISTS:
                    if notify_existing:
                        self._try_notify_existing()
                    self.release()
                    return False
            except Exception:
                pass

        # 2. Проверяем активный IPC-сервер (работает и в Windows, и в macOS)
        if notify_existing:
            if self._try_notify_existing():
                self.release()
                return False
        else:
            sock = QLocalSocket()
            sock.connectToServer(self.app_id)
            if sock.waitForConnected(120):
                sock.disconnectFromServer()
                self.release()
                return False

        # 3. Очищаем устаревший сокет после аварийного завершения и поднимаем основной IPC-сервер
        QLocalServer.removeServer(self.app_id)
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.app_id):
            if notify_existing:
                self._try_notify_existing()
            self.release()
            return False

        server.newConnection.connect(self._on_new_connection)
        self._server = server
        self._is_primary = True
        return True

    def acquire(self, wait_timeout_ms: int = 0) -> bool:
        """Пытается стать основным экземпляром приложения.
        При wait_timeout_ms > 0 (например, при перезапуске после обновления)
        ждёт освобождения блокировки старым процессом без его повторной активации.
        """
        deadline = time.monotonic() + max(0, int(wait_timeout_ms)) / 1000.0
        while wait_timeout_ms > 0 and time.monotonic() < deadline:
            if self._try_acquire_once(notify_existing=False):
                return True
            time.sleep(0.15)
        return self._try_acquire_once(notify_existing=True)

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            conn = self._server.nextPendingConnection()
            if conn is None:
                continue
            conn.waitForReadyRead(150)
            data = bytes(conn.readAll()).decode("utf-8", errors="ignore").strip()
            conn.disconnectFromServer()
            conn.deleteLater()
            if not data or "ACTIVATE" in data:
                self.activation_requested.emit()

    def release(self) -> None:
        self._is_primary = False
        if self._server is not None:
            try:
                self._server.close()
            except Exception:
                pass
            self._server = None
            try:
                QLocalServer.removeServer(self.app_id)
            except Exception:
                pass

        if self._mutex_handle is not None and sys.platform == "win32":
            try:
                ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            except Exception:
                pass
            self._mutex_handle = None
