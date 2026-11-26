from __future__ import annotations

import ctypes
import sys
from typing import Optional

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from src.capture import Rect, find_target_window, rect_to_roi_percentages, resolve_roi_rect
from src.config import RoiConfig
from src.ui.icons import svg_icon

HANDLE_MARGIN = 10
MIN_W = 80
MIN_H = 30

# Константы Win32 для неактивируемого оверлея поверх всех окон (не сворачивает полноэкранные игры и Xbox!)
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_HWND_TOPMOST = -1
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040
_VK_RETURN = 0x0D
_VK_ESCAPE = 0x1B


def _apply_win32_topmost_noactivate(hwnd: int) -> None:
    """Устанавливает WS_EX_NOACTIVATE + HWND_TOPMOST, чтобы оверлей оставался поверх игр без перехвата фокуса."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        ex_style = user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        new_style = ex_style | _WS_EX_TOPMOST | _WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
        if new_style != ex_style:
            user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, new_style)
        user32.SetWindowPos(
            hwnd,
            _HWND_TOPMOST,
            0,
            0,
            0,
            0,
            _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
        )
    except Exception:
        pass


class RoiOverlay(QWidget):
    """Неактивируемый полупрозрачный оверлей выбора области (ROI) поверх всех окон:
    - Всегда находится строго ПОВЕРХ всех окон (включая Xbox / Borderless / полноэкранные трансляции).
    - Использует WS_EX_NOACTIVATE + WA_ShowWithoutActivating, чтобы окно игры НИКОГДА не теряло фокус и не сворачивалось.
    - Поддерживает перетаскивание и изменение размера мышью, плавающие кнопки Сохранить/Отмена,
      двойной клик для сохранения, ПКМ для отмены и опрос клавиш Enter/Esc без активации окна.
    """

    roi_selected = Signal(object)  # Испускает обновлённый RoiConfig
    cancelled = Signal()

    def __init__(
        self,
        current_roi: RoiConfig,
        target_process: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._base_roi = current_roi
        self._target_process = (target_process or "").strip()
        self._closed = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)

        # Охватываем весь виртуальный рабочий стол без showFullScreen(), который вызывает сворачивание окон в DWM
        vgeom = QGuiApplication.primaryScreen().virtualGeometry()
        self.setGeometry(vgeom)
        self._origin = vgeom.topLeft()
        self._dpr = float(self.devicePixelRatioF() or 1.0)

        # Ограничиваем выделение области строго внутри выбранного окна процесса, если оно открыто
        self._target_win_bounds: Optional[QRect] = None
        found_win = find_target_window(self._target_process)
        if found_win is not None:
            _hwnd, wr = found_win
            wx = int(round(wr.left / self._dpr)) - self._origin.x()
            wy = int(round(wr.top / self._dpr)) - self._origin.y()
            ww = max(MIN_W, int(round(wr.width / self._dpr)))
            wh = max(MIN_H, int(round(wr.height / self._dpr)))
            wb = QRect(wx, wy, ww, wh).intersected(self.rect().adjusted(2, 2, -2, -2))
            if wb.width() >= MIN_W and wb.height() >= MIN_H:
                self._target_win_bounds = wb

        abs_rect, self._ref_source = resolve_roi_rect(current_roi, target_process=self._target_process)
        lx = int(round(abs_rect.left / self._dpr)) - self._origin.x()
        ly = int(round(abs_rect.top / self._dpr)) - self._origin.y()
        lw = max(MIN_W, int(round(abs_rect.width / self._dpr)))
        lh = max(MIN_H, int(round(abs_rect.height / self._dpr)))
        bounds = self._target_win_bounds if self._target_win_bounds is not None else self.rect().adjusted(4, 4, -4, -4)
        self._sel = QRect(lx, ly, lw, lh).intersected(bounds)
        if self._sel.width() < MIN_W or self._sel.height() < MIN_H:
            self._sel = QRect(
                bounds.left() + int(bounds.width() * 0.20),
                bounds.top() + int(bounds.height() * 0.78),
                max(MIN_W, int(bounds.width() * 0.60)),
                max(MIN_H, int(bounds.height() * 0.14)),
            ).intersected(bounds)

        self._drag_mode: Optional[str] = None
        self._drag_start = QPoint()
        self._orig_sel = QRect(self._sel)

        # Плавающая панель управления над/под выделением
        self._toolbar = QWidget(self)
        self._toolbar.setCursor(Qt.CursorShape.ArrowCursor)
        self._toolbar.setStyleSheet(
            "background-color: rgba(14, 13, 23, 242); "
            "border: 1px solid #FF2E88; border-radius: 10px;"
        )
        tb_layout = QHBoxLayout(self._toolbar)
        tb_layout.setContentsMargins(12, 7, 10, 7)
        tb_layout.setSpacing(10)

        self._info_label = QLabel()
        self._info_label.setStyleSheet("border: none; color: #F4F3FA; font-weight: 600; font-size: 12px;")
        tb_layout.addWidget(self._info_label)

        btn_save = QPushButton(" Сохранить (Enter)")
        btn_save.setIcon(svg_icon("check", "#0B0B12", 15, 2.4))
        btn_save.setIconSize(QSize(15, 15))
        btn_save.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.setStyleSheet(
            "QPushButton { background-color: #00D9B5; color: #0B0B12; border: none; "
            "border-radius: 7px; padding: 5px 13px 7px 13px; font-weight: 700; }"
            "QPushButton:hover { background-color: #2CF0CE; }"
            "QPushButton:pressed { background-color: #00B596; }"
        )
        btn_save.clicked.connect(self._accept)
        tb_layout.addWidget(btn_save)

        btn_cancel = QPushButton(" Отмена (Esc / ПКМ)")
        btn_cancel.setIcon(svg_icon("x", "#F4F3FA", 15, 2.2))
        btn_cancel.setIconSize(QSize(15, 15))
        btn_cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cancel.setStyleSheet(
            "QPushButton { background-color: #27243B; color: #F4F3FA; border: 1px solid #3D3859; "
            "border-radius: 7px; padding: 5px 12px 7px 12px; font-weight: 600; }"
            "QPushButton:hover { background-color: #363252; border-color: #FF4A98; color: #FFFFFF; }"
            "QPushButton:pressed { background-color: #1D1B2E; }"
        )
        btn_cancel.clicked.connect(self._cancel)
        tb_layout.addWidget(btn_cancel)

        self._reposition_toolbar()

        # Удерживаем HWND_TOPMOST без активации окна и опрашиваем Enter/Esc без кражи фокуса
        self._guard_timer = QTimer(self)
        self._guard_timer.setInterval(100)
        self._guard_timer.timeout.connect(self._on_guard_tick)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _apply_win32_topmost_noactivate(int(self.winId()))
        # Сбрасываем залипшее состояние клавиш Enter/Esc перед запуском таймера
        if sys.platform == "win32":
            try:
                ctypes.windll.user32.GetAsyncKeyState(_VK_RETURN)
                ctypes.windll.user32.GetAsyncKeyState(_VK_ESCAPE)
            except Exception:
                pass
        self._guard_timer.start()

    def closeEvent(self, event) -> None:
        self._guard_timer.stop()
        super().closeEvent(event)

    def _on_guard_tick(self) -> None:
        if self._closed or not self.isVisible():
            self._guard_timer.stop()
            return
        _apply_win32_topmost_noactivate(int(self.winId()))
        if sys.platform == "win32":
            try:
                u32 = ctypes.windll.user32
                if u32.GetAsyncKeyState(_VK_ESCAPE) & 0x0001:
                    self._cancel()
                    return
                if u32.GetAsyncKeyState(_VK_RETURN) & 0x0001:
                    self._accept()
                    return
            except Exception:
                pass

    def _current_physical_rect(self) -> Rect:
        gx = int(round((self._sel.x() + self._origin.x()) * self._dpr))
        gy = int(round((self._sel.y() + self._origin.y()) * self._dpr))
        gw = max(32, int(round(self._sel.width() * self._dpr)))
        gh = max(16, int(round(self._sel.height() * self._dpr)))
        return Rect(gx, gy, gw, gh)

    def _reposition_toolbar(self) -> None:
        phys = self._current_physical_rect()
        roi_pct = rect_to_roi_percentages(phys, self._base_roi, target_process=self._target_process)
        src_label = self._target_process if self._ref_source.startswith("window:") else "Экран"
        self._info_label.setText(
            f"Область субтитров ({src_label}): {phys.width}×{phys.height} px  "
            f"[{roi_pct.x_pct*100:.1f}%, {roi_pct.y_pct*100:.1f}%, "
            f"{roi_pct.w_pct*100:.1f}%×{roi_pct.h_pct*100:.1f}%]"
        )
        self._toolbar.adjustSize()
        tx = self._sel.center().x() - self._toolbar.width() // 2
        tx = max(12, min(self.width() - self._toolbar.width() - 12, tx))
        ty = self._sel.top() - self._toolbar.height() - 10
        if ty < 12:
            ty = min(self.height() - self._toolbar.height() - 12, self._sel.bottom() + 10)
        self._toolbar.move(tx, ty)

    def _hit_test(self, pos: QPoint) -> Optional[str]:
        r = self._sel
        m = HANDLE_MARGIN
        left = abs(pos.x() - r.left()) <= m
        right = abs(pos.x() - r.right()) <= m
        top = abs(pos.y() - r.top()) <= m
        bottom = abs(pos.y() - r.bottom()) <= m
        in_x = r.left() - m <= pos.x() <= r.right() + m
        in_y = r.top() - m <= pos.y() <= r.bottom() + m

        if left and top:
            return "tl"
        if right and top:
            return "tr"
        if left and bottom:
            return "bl"
        if right and bottom:
            return "br"
        if left and in_y:
            return "l"
        if right and in_y:
            return "r"
        if top and in_x:
            return "t"
        if bottom and in_x:
            return "b"
        if r.contains(pos):
            return "move"
        return "new"

    def _update_cursor(self, mode: Optional[str]) -> None:
        mapping = {
            "tl": Qt.CursorShape.SizeFDiagCursor,
            "br": Qt.CursorShape.SizeFDiagCursor,
            "tr": Qt.CursorShape.SizeBDiagCursor,
            "bl": Qt.CursorShape.SizeBDiagCursor,
            "l": Qt.CursorShape.SizeHorCursor,
            "r": Qt.CursorShape.SizeHorCursor,
            "t": Qt.CursorShape.SizeVerCursor,
            "b": Qt.CursorShape.SizeVerCursor,
            "move": Qt.CursorShape.SizeAllCursor,
            "new": Qt.CursorShape.CrossCursor,
        }
        self.setCursor(mapping.get(mode, Qt.CursorShape.ArrowCursor))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        veil = QColor(8, 10, 16, 130)
        w, h = self.width(), self.height()
        r = self._sel
        painter.fillRect(0, 0, w, r.top(), veil)
        painter.fillRect(0, r.bottom() + 1, w, h - r.bottom() - 1, veil)
        painter.fillRect(0, r.top(), r.left(), r.height(), veil)
        painter.fillRect(r.right() + 1, r.top(), w - r.right() - 1, r.height(), veil)

        # Подсвечиваем границу окна целевого процесса, чтобы пользователь видел точную зону привязанного захвата
        if self._target_win_bounds is not None:
            wpen = QPen(QColor(0, 217, 181, 180), 1.5, Qt.PenStyle.DashLine)
            painter.setPen(wpen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self._target_win_bounds)

        pen = QPen(QColor("#FF2E93"), 2.5)
        painter.setPen(pen)
        painter.drawRect(r)

        painter.setBrush(QColor("#00E5C3"))
        painter.setPen(QPen(QColor("#0D0C14"), 1.5))
        hs = 8
        for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
            painter.drawRect(pt.x() - hs // 2, pt.y() - hs // 2, hs, hs)

        painter.setPen(QColor("#F4F3FA"))
        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        if self._target_win_bounds is not None:
            banner_txt = (
                f"Окно процесса: {self._target_process}  •  "
                "Двойной клик / Enter — сохранить  •  ПКМ / Esc — отмена"
            )
        else:
            banner_txt = (
                f"Окно {self._target_process} сейчас не найдено — откройте игру  •  "
                "Enter — сохранить  •  Esc — отмена"
            )
        painter.drawText(
            QRect(0, 14, w, 28),
            Qt.AlignmentFlag.AlignCenter,
            banner_txt,
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self._cancel()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position().toPoint()
            self._drag_mode = self._hit_test(pos)
            self._drag_start = pos
            self._orig_sel = QRect(self._sel)
            if self._drag_mode == "new":
                self._sel = QRect(pos, pos)
            self.update()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._sel.contains(event.position().toPoint()):
                self._accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        pos = event.position().toPoint()
        if not self._drag_mode:
            self._update_cursor(self._hit_test(pos))
            return

        dx = pos.x() - self._drag_start.x()
        dy = pos.y() - self._drag_start.y()
        r = QRect(self._orig_sel)
        bounds = self._target_win_bounds if self._target_win_bounds is not None else self.rect()

        if self._drag_mode == "move":
            r.translate(dx, dy)
            if r.left() < bounds.left():
                r.moveLeft(bounds.left())
            if r.top() < bounds.top():
                r.moveTop(bounds.top())
            if r.right() > bounds.right():
                r.moveRight(bounds.right())
            if r.bottom() > bounds.bottom():
                r.moveBottom(bounds.bottom())
        elif self._drag_mode == "new":
            clamped_start = QPoint(
                max(bounds.left(), min(bounds.right(), self._drag_start.x())),
                max(bounds.top(), min(bounds.bottom(), self._drag_start.y())),
            )
            clamped_pos = QPoint(
                max(bounds.left(), min(bounds.right(), pos.x())),
                max(bounds.top(), min(bounds.bottom(), pos.y())),
            )
            r = QRect(clamped_start, clamped_pos).normalized()
            if r.width() < MIN_W:
                r.setWidth(MIN_W)
            if r.height() < MIN_H:
                r.setHeight(MIN_H)
            r = r.intersected(bounds)
        else:
            if "l" in self._drag_mode:
                r.setLeft(min(r.right() - MIN_W, max(bounds.left(), r.left() + dx)))
            if "r" in self._drag_mode:
                r.setRight(max(r.left() + MIN_W, min(bounds.right(), r.right() + dx)))
            if "t" in self._drag_mode:
                r.setTop(min(r.bottom() - MIN_H, max(bounds.top(), r.top() + dy)))
            if "b" in self._drag_mode:
                r.setBottom(max(r.top() + MIN_H, min(bounds.bottom(), r.bottom() + dy)))

        self._sel = r
        self._reposition_toolbar()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_mode = None
            self._reposition_toolbar()
            self.update()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._accept()
        elif event.key() == Qt.Key.Key_Escape:
            self._cancel()

    def _accept(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._guard_timer.stop()
        phys = self._current_physical_rect()
        new_roi = rect_to_roi_percentages(phys, self._base_roi, target_process=self._target_process)
        self.roi_selected.emit(new_roi)
        self.close()

    def _cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._guard_timer.stop()
        self.cancelled.emit()
        self.close()
