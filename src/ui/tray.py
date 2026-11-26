from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from src.paths import find_app_icon_path


_APP_ICON_CACHE: dict[bool, QIcon] = {}


def build_app_icon(active: bool = False) -> QIcon:
    """Загружает и кэширует официальную иконку GTA6SubVoice из icons/ (с активным индикатором в трее при active=True)."""
    cached = _APP_ICON_CACHE.get(active)
    if cached is not None and not cached.isNull():
        return cached
    icon_path = find_app_icon_path()
    if icon_path is not None:
        base_icon = QIcon(str(icon_path))
        if not base_icon.isNull():
            if not active:
                _APP_ICON_CACHE[False] = base_icon
                return base_icon
            pm = base_icon.pixmap(64, 64)
            if not pm.isNull():
                out = QPixmap(pm)
                p = QPainter(out)
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                # Аккуратная неоново-мятная точка активности в правом нижнем углу иконки в системном трее
                dot = max(14, out.width() // 3)
                margin = 2
                x = out.width() - dot - margin
                y = out.height() - dot - margin
                p.setPen(QPen(QColor("#0D0C14"), 3))
                p.setBrush(QColor("#00E5C3"))
                p.drawEllipse(x, y, dot, dot)
                p.end()
                res = QIcon(out)
                _APP_ICON_CACHE[True] = res
                return res
            _APP_ICON_CACHE[active] = base_icon
            return base_icon

    # Запасная программная иконка на случай отсутствия папки icons/
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    grad = QLinearGradient(0, 0, size, size)
    if active:
        grad.setColorAt(0.0, QColor("#00E5C3"))
        grad.setColorAt(1.0, QColor("#0d6efd"))
    else:
        grad.setColorAt(0.0, QColor("#FF2E93"))
        grad.setColorAt(1.0, QColor("#FF7A3D"))

    p.setBrush(grad)
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 4, size - 8, size - 8, 14, 14)

    p.setPen(QColor("#ffffff"))
    p.setFont(QFont("Segoe UI", 24, QFont.Weight.Bold))
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "VI")
    p.end()
    return QIcon(pm)


class TrayController:
    def __init__(
        self,
        on_show_hide: Callable[[], None],
        on_toggle_pause: Callable[[], None],
        on_select_roi: Callable[[], None],
        on_interrupt: Callable[[], None],
        on_quit: Callable[[], None],
        parent=None,
    ) -> None:
        self.tray = QSystemTrayIcon(build_app_icon(active=False), parent)
        self.tray.setToolTip("GTA6SubVoice — Озвучка субтитров")

        self._menu = QMenu(parent)
        self.act_show = QAction("Показать / Скрыть окно", self._menu)
        self.act_show.triggered.connect(on_show_hide)
        self._menu.addAction(self.act_show)

        self._menu.addSeparator()

        self.act_pause = QAction("Старт / Стоп чтения субтитров (F8)", self._menu)
        self.act_pause.triggered.connect(on_toggle_pause)
        self._menu.addAction(self.act_pause)

        self.act_roi = QAction("Выбрать область субтитров (F9)", self._menu)
        self.act_roi.triggered.connect(on_select_roi)
        self._menu.addAction(self.act_roi)

        self.act_interrupt = QAction("Прервать фразу (F10)", self._menu)
        self.act_interrupt.triggered.connect(on_interrupt)
        self._menu.addAction(self.act_interrupt)

        self._menu.addSeparator()

        self.act_quit = QAction("Выход", self._menu)
        self.act_quit.triggered.connect(on_quit)
        self._menu.addAction(self.act_quit)

        self.tray.setContextMenu(self._menu)
        self.tray.activated.connect(self._on_activated)
        self._on_show_hide = on_show_hide
        self.tray.show()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_show_hide()

    def set_active(self, active: bool, tooltip_suffix: str = "") -> None:
        self.tray.setIcon(build_app_icon(active=active))
        base = "GTA6SubVoice — Озвучка субтитров"
        self.tray.setToolTip(f"{base} ({tooltip_suffix})" if tooltip_suffix else base)

    def notify(self, title: str, message: str) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 2500)

    def hide(self) -> None:
        self.tray.hide()
