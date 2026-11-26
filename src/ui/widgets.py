from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    QRectF,
    QSize,
    Qt,
    QVariantAnimation,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QPushButton,
    QSizePolicy,
    QSlider,
    QTabBar,
    QWidget,
)


def _lerp_color(c1: QColor, c2: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(
        int(round(c1.red() + (c2.red() - c1.red()) * t)),
        int(round(c1.green() + (c2.green() - c1.green()) * t)),
        int(round(c1.blue() + (c2.blue() - c1.blue()) * t)),
        int(round(c1.alpha() + (c2.alpha() - c1.alpha()) * t)),
    )


class ViceToggleSwitch(QAbstractButton):
    """Плавный анимированный переключатель (в стиле macOS/Win11) с палитрой Vice City sunset/mint.
    Полностью совместим по API с QCheckBox (isChecked / setChecked / toggled / setToolTip)
    и никогда не обрезается и не сжимается по вертикали.
    """

    def __init__(
        self,
        text: str = "",
        accent: str = "sunset",  # «sunset» (#FF2E88->#FF7A45) | «mint» (#00D9B5)
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(22)

        self._accent = accent
        self._progress = 0.0
        self._hovered = False

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(170)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_val)

        self.toggled.connect(self._start_anim)

    def _on_anim_val(self, val: object) -> None:
        self._progress = float(val)
        self.update()

    def _start_anim(self, checked: bool) -> None:
        target = 1.0 if checked else 0.0
        if not self.isVisible():
            self._anim.stop()
            self._progress = target
            self.update()
            return
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(target)
        self._anim.start()

    def setChecked(self, checked: bool) -> None:
        was = self.isChecked()
        super().setChecked(checked)
        if was == checked:
            self._progress = 1.0 if checked else 0.0
            self.update()
        elif self.signalsBlocked():
            self._anim.stop()
            self._progress = 1.0 if checked else 0.0
            self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        text_w = fm.horizontalAdvance(self.text()) if self.text() else 0
        gap = 8 if text_w > 0 else 0
        return QSize(34 + gap + text_w + 4, max(22, fm.height() + 6))

    def minimumSizeHint(self) -> QSize:
        fm = QFontMetrics(self.font())
        return QSize(44, max(22, fm.height() + 6))

    def enterEvent(self, event) -> None:
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        track_w = 32.0
        track_h = 17.0
        y = (self.height() - track_h) * 0.5
        track_rect = QRectF(1.0, y, track_w, track_h)

        # Лёгкое свечение вокруг дорожки при наведении
        if self._hovered and self.isEnabled():
            halo_c = QColor("#00D9B5") if self._accent == "mint" else QColor("#FF2E88")
            halo_c.setAlpha(45)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(halo_c)
            p.drawRoundedRect(track_rect.adjusted(-2.0, -2.0, 2.0, 2.0), track_h * 0.6, track_h * 0.6)

        # Фон дорожки (плавная интерполяция цвета)
        off_bg = QColor("#242235")
        if self._progress <= 0.01:
            p.setBrush(off_bg)
        elif self._accent == "mint":
            c = _lerp_color(off_bg, QColor("#00C8A7"), self._progress)
            p.setBrush(c)
        else:
            grad = QLinearGradient(track_rect.left(), 0, track_rect.right(), 0)
            grad.setColorAt(0.0, _lerp_color(off_bg, QColor("#FF2E88"), self._progress))
            grad.setColorAt(1.0, _lerp_color(off_bg, QColor("#FF7A45"), self._progress))
            p.setBrush(grad)

        border_off = QColor("#4A4568" if self._hovered else "#383452")
        border_on = QColor("#2CF0CE" if self._accent == "mint" else "#FF6AA7")
        p.setPen(QPen(_lerp_color(border_off, border_on, self._progress), 1.0))
        p.drawRoundedRect(track_rect, track_h * 0.5, track_h * 0.5)

        # Круглый ползунок переключателя
        knob_d = 11.5
        travel = track_w - knob_d - 5.5
        kx = track_rect.left() + 2.75 + travel * self._progress
        ky = track_rect.top() + (track_h - knob_d) * 0.5
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#FFFFFF" if self.isEnabled() else "#8B87A0"))
        p.drawEllipse(QRectF(kx, ky, knob_d, knob_d))

        # Чёткий текст подписи, оптически выровненный с переключателем по высоте прописных букв
        if self.text():
            txt_color = QColor("#ECEAF6" if self.isEnabled() else "#6B6780")
            if self._hovered and self.isEnabled():
                txt_color = QColor("#FFFFFF")
            p.setPen(txt_color)
            fm = QFontMetrics(self.font())
            cy = self.height() * 0.5
            baseline_y = cy + fm.capHeight() * 0.48
            p.drawText(QPointF(track_w + 8.0, baseline_y), self.text())

        p.end()


class TactileSlider(QSlider):
    """Кроссплатформенный тактильный горизонтальный слайдер:
    - Курсор PointingHand при наведении и ClosedHand при перетаскивании
    - Клик в любом месте шкалы плавно переносит ползунок на это значение
    - Сглаженный градиентный трек Vice City + подсветка бегунка при наведении
    - Фиксированная удобная высота (22 px), чтобы компоновка не сжимала виджет
    """

    def __init__(
        self,
        orientation: Qt.Orientation = Qt.Orientation.Horizontal,
        accent: str = "sunset",  # «sunset» | «mint»
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(orientation, parent)
        self._accent = accent
        self._hovered = False
        self._pressed = False
        self._hover_t = 0.0

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(140)
        self._hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._hover_anim.valueChanged.connect(self._on_hover_val)

    def _on_hover_val(self, val: object) -> None:
        self._hover_t = float(val)
        self.update()

    def _animate_hover(self, target: float) -> None:
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover_t)
        self._hover_anim.setEndValue(target)
        self._hover_anim.start()

    def _value_from_x(self, x: float) -> int:
        margin = 8.0
        span = max(1.0, self.width() - 2.0 * margin)
        ratio = max(0.0, min(1.0, (x - margin) / span))
        val = self.minimum() + ratio * (self.maximum() - self.minimum())
        step = max(1, self.singleStep())
        snapped = round((val - self.minimum()) / step) * step + self.minimum()
        return int(max(self.minimum(), min(self.maximum(), snapped)))

    def enterEvent(self, event) -> None:
        self._hovered = True
        self._animate_hover(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self._hovered = False
        if not self._pressed:
            self._animate_hover(0.0)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            self.setValue(self._value_from_x(event.position().x()))
            self._animate_hover(1.0)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._pressed:
            self.setValue(self._value_from_x(event.position().x()))
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._pressed:
            self._pressed = False
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            if not self._hovered:
                self._animate_hover(0.0)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        margin = 8.0
        cy = self.height() * 0.5
        groove_h = 5.0
        track_x0 = margin
        track_w = max(4.0, self.width() - 2.0 * margin)
        span = max(1, self.maximum() - self.minimum())
        ratio = (self.value() - self.minimum()) / float(span)
        cx = track_x0 + track_w * ratio

        # Неактивная дорожка
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#232136"))
        p.drawRoundedRect(QRectF(track_x0, cy - groove_h * 0.5, track_w, groove_h), 2.5, 2.5)

        # Заливка активной части градиентом
        fill_w = max(0.0, cx - track_x0)
        if fill_w > 1.0:
            if self._accent == "mint":
                grad = QLinearGradient(track_x0, 0, track_x0 + track_w, 0)
                grad.setColorAt(0.0, QColor("#00B89C"))
                grad.setColorAt(1.0, QColor("#00E5C3"))
            else:
                grad = QLinearGradient(track_x0, 0, track_x0 + track_w, 0)
                grad.setColorAt(0.0, QColor("#FF2E88"))
                grad.setColorAt(1.0, QColor("#FF7A45"))
            p.setBrush(grad)
            p.drawRoundedRect(QRectF(track_x0, cy - groove_h * 0.5, fill_w, groove_h), 2.5, 2.5)

        # Внешнее свечение при наведении
        if self._hover_t > 0.01:
            halo_color = QColor("#00E5C3" if self._accent == "mint" else "#FF2E88")
            halo_color.setAlpha(int(round(65 * self._hover_t)))
            halo_r = 8.5 + 2.0 * self._hover_t
            p.setBrush(halo_color)
            p.drawEllipse(QPointF(cx, cy), halo_r, halo_r)

        # Чёткий бегунок с акцентной обводкой
        thumb_r = 5.8 + (0.9 if self._pressed else 0.4 * self._hover_t)
        ring_color = QColor("#00E5C3" if self._accent == "mint" else "#FF3B8E")
        p.setPen(QPen(ring_color, 1.8))
        p.setBrush(QColor("#FFFFFF"))
        p.drawEllipse(QPointF(cx, cy), thumb_r, thumb_r)
        p.end()


class AnimatedStatusBadge(QWidget):
    """Матовая плашка статуса с плавно пульсирующим индикатором во время работы или загрузки."""

    def __init__(self, initial_text: str = "Загрузка...", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._text = initial_text
        self._color = QColor("#FF7A45")
        self._pulse = 0.0
        self._pulsing = False

        self.setFixedHeight(23)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._anim = QVariantAnimation(self)
        self._anim.setDuration(1500)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_pulse)

    def _on_pulse(self, val: object) -> None:
        self._pulse = float(val)
        self.update()

    def _sync_anim_state(self) -> None:
        if self._pulsing and self.isVisible():
            if self._anim.state() != QVariantAnimation.State.Running:
                self._anim.start()
        else:
            if self._anim.state() == QVariantAnimation.State.Running:
                self._anim.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_anim_state()

    def hideEvent(self, event) -> None:
        # Останавливаем 60 FPS анимацию пульсации сразу при скрытии в трей -> 0% фоновой нагрузки на GUI CPU
        if self._anim.state() == QVariantAnimation.State.Running:
            self._anim.stop()
        super().hideEvent(event)

    def set_status(self, text: str, color_hex: str, pulsing: bool = False) -> None:
        self._text = text
        self._color = QColor(color_hex)
        self._pulsing = pulsing
        if not pulsing:
            self._pulse = 0.0
        self._sync_anim_state()
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        font = self.font()
        font.setPointSizeF(max(8.5, font.pointSizeF() - 0.5))
        font.setWeight(font.Weight.DemiBold)
        fm = QFontMetrics(font)
        return QSize(24 + fm.horizontalAdvance(self._text) + 12, 23)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        bg = QColor("#151422")
        border = QColor(self._color)
        border.setAlpha(90 if self._pulsing else 45)
        p.setBrush(bg)
        p.setPen(QPen(border, 1.0))
        p.drawRoundedRect(rect, 11.0, 11.0)

        cx = 11.5
        cy = self.height() * 0.5

        # Плавное внешнее кольцо при активном статусе
        if self._pulsing:
            wave = 0.5 - 0.5 * math.cos(self._pulse * 2.0 * math.pi)
            ring = QColor(self._color)
            ring.setAlpha(int(round(125 * (1.0 - wave))))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ring)
            p.drawEllipse(QPointF(cx, cy), 3.3 + 3.8 * wave, 3.3 + 3.8 * wave)

        # Центральная точка индикатора
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QPointF(cx, cy), 3.3, 3.3)

        # Текст статуса — оптически отцентрирован относительно точки по capHeight шрифта
        p.setPen(QColor("#F4F3FA"))
        font = self.font()
        font.setPointSizeF(max(8.5, font.pointSizeF() - 0.5))
        font.setWeight(font.Weight.DemiBold)
        p.setFont(font)
        fm = QFontMetrics(font)
        baseline_y = cy + fm.capHeight() * 0.48
        p.drawText(QPointF(21.0, baseline_y), self._text)
        p.end()


class ViceHeaderBar(QWidget):
    """Верхняя панель заголовка с тонкой неоновой линией Vice City по нижнему краю.
    Во время работы (прослушивание / озвучка) градиент мягко переливается вдоль полосы.
    При скрытии в трей анимация автоматически останавливается для нулевой нагрузки на CPU.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("headerBar")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(42)

        self._phase = 0.0
        self._active = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(3600)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_phase)

    def _on_phase(self, val: object) -> None:
        self._phase = float(val)
        self.update()

    def _sync_anim_state(self) -> None:
        if self._active and self.isVisible():
            if self._anim.state() != QVariantAnimation.State.Running:
                self._anim.start()
        else:
            if self._anim.state() == QVariantAnimation.State.Running:
                self._anim.stop()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_anim_state()

    def hideEvent(self, event) -> None:
        if self._anim.state() == QVariantAnimation.State.Running:
            self._anim.stop()
        super().hideEvent(event)

    def set_live_shimmer(self, active: bool) -> None:
        self._active = active
        if not active:
            self._phase = 0.0
        self._sync_anim_state()
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        radius = 9.0

        # Мягкий тёмный фон карточки шапки
        bg_grad = QLinearGradient(0, 0, self.width(), self.height())
        bg_grad.setColorAt(0.0, QColor("#171526"))
        bg_grad.setColorAt(1.0, QColor("#12111E"))
        p.setBrush(bg_grad)
        p.setPen(QPen(QColor(255, 255, 255, 20), 1.0))
        p.drawRoundedRect(rect, radius, radius)

        # Обрезаем нижнюю акцентную линию строго внутри скруглённой рамки
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        p.setClipPath(clip)

        bar_h = 2.5 if self._active else 2.0
        w = float(self.width())
        shift = self._phase * w if self._active else 0.0
        accent = QLinearGradient(-w + shift, 0, w + shift, 0)
        accent.setColorAt(0.00, QColor("#FF2E88"))
        accent.setColorAt(0.25, QColor("#FF7A45"))
        accent.setColorAt(0.50, QColor("#00D9B5"))
        accent.setColorAt(0.75, QColor("#FF2E88"))
        accent.setColorAt(1.00, QColor("#FF7A45"))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRect(QRectF(0.0, self.height() - bar_h, w, bar_h))
        p.end()


class _ComboChevronOverlay(QWidget):
    """Рисует чёткую сглаженную стрелку-шеврон поверх любого QComboBox (одинаково в Windows и macOS)."""

    def __init__(self, combo: QComboBox) -> None:
        super().__init__(combo)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self._combo = combo
        combo.installEventFilter(self)
        self._sync_geom()

    def _sync_geom(self) -> None:
        w = 20
        self.setGeometry(max(0, self._combo.width() - w - 3), 0, w, self._combo.height())

    def eventFilter(self, obj, event) -> bool:
        if obj is self._combo and event.type() in (event.Type.Resize, event.Type.Show):
            self._sync_geom()
        return False

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#A8A3C2"), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        cx = self.width() * 0.5
        cy = self.height() * 0.5
        path = QPainterPath()
        path.moveTo(cx - 3.5, cy - 1.6)
        path.lineTo(cx, cy + 2.0)
        path.lineTo(cx + 3.5, cy - 1.6)
        p.drawPath(path)
        p.end()


def apply_interactive_cursors(root: QWidget) -> None:
    """Назначает курсор PointingHandCursor всем интерактивным виджетам и крепит векторные шевроны на QComboBox."""
    for cls in (QPushButton, QCheckBox, QComboBox, QSlider, ViceToggleSwitch):
        for w in root.findChildren(cls):
            w.setCursor(Qt.CursorShape.PointingHandCursor)
    for cb in root.findChildren(QComboBox):
        if not cb.property("_vice_chevron_attached"):
            cb.setProperty("_vice_chevron_attached", True)
            _ComboChevronOverlay(cb).show()
    for tb in root.findChildren(QTabBar):
        tb.setCursor(Qt.CursorShape.PointingHandCursor)
