from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.ui.icons import svg_icon
from src.ui.styles import DARK_STYLESHEET
from src.ui.widgets import apply_interactive_cursors
from src.version import (
    APP_NAME,
    APP_VERSION,
    AUTHOR_NAME,
    AUTHOR_URL,
    BINANCE_PAY_ID,
    REPO_URL,
    REVOLUT_URL,
    SPONSORS_URL,
)


class SupportDialog(QDialog):
    """Компактное модальное окно поддержки проекта, копирования реквизитов и просмотра версии/автора."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Поддержать {APP_NAME}")
        self.setModal(True)
        self.setFixedWidth(430)
        self.setStyleSheet(DARK_STYLESHEET)

        self._build_ui()
        apply_interactive_cursors(self)
        self.adjustSize()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        # Заголовок с иконкой и названием
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        hdr.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        icon_badge = QLabel()
        icon_badge.setFixedSize(36, 36)
        icon_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_badge.setPixmap(svg_icon("coffee", "#FF4A98", 18).pixmap(18, 18))
        icon_badge.setStyleSheet(
            "background-color: #25162B; border: 1px solid #6E284E; border-radius: 10px;"
        )
        hdr.addWidget(icon_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        lbl_title = QLabel("Поддержать проект")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #FFFFFF;")
        lbl_sub = QLabel("Спасибо, что пользуетесь GTA VI SubVoice!")
        lbl_sub.setStyleSheet("font-size: 11px; color: #FF8754; font-weight: 600;")
        title_col.addWidget(lbl_title)
        title_col.addWidget(lbl_sub)
        hdr.addLayout(title_col, 1)
        root.addLayout(hdr)

        # Тёплый мотивирующий текст
        lbl_desc = QLabel(
            "Программа полностью бесплатна, работает без подписок и доступна каждому игроку. "
            "На её разработку, тонкую настройку голосов и тестирование в реальных сценах ушло немало "
            "вечеров и кружек кофе.\n\n"
            "Если озвучка сделала ваше прохождение комфортнее — буду искренне рад любой поддержке. "
            "Это напрямую помогает выпускать обновления и улучшать программу!"
        )
        lbl_desc.setWordWrap(True)
        lbl_desc.setStyleSheet("color: #D7D3EB; line-height: 1.38; font-size: 12px;")
        root.addWidget(lbl_desc)

        # Карточки способов поддержки
        root.addWidget(
            self._make_option_card(
                title="GitHub Sponsors",
                subtitle="github.com/sponsors/dev-drozd",
                open_url=SPONSORS_URL,
                copy_value=SPONSORS_URL,
                accent="#FF2E88",
            )
        )
        root.addWidget(
            self._make_option_card(
                title="Revolut",
                subtitle="revolut.me/devdrozd",
                open_url=REVOLUT_URL,
                copy_value=REVOLUT_URL,
                accent="#FF7A45",
            )
        )
        root.addWidget(
            self._make_option_card(
                title="Binance Pay ID",
                subtitle=BINANCE_PAY_ID,
                open_url=None,
                copy_value=BINANCE_PAY_ID,
                accent="#00D9B5",
                mono_subtitle=True,
            )
        )

        # Разделитель
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #26233A; max-height: 1px; border: none;")
        root.addWidget(sep)

        # Нижняя панель: версия, ссылка на репозиторий, автор и кнопка закрытия
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(2)

        lbl_ver = QLabel(
            f"<span style='color:#9893B0;'>Версия:</span> "
            f"<a href='{REPO_URL}' style='color:#00D9B5; text-decoration:none; font-weight:600;'>"
            f"{APP_NAME} v{APP_VERSION} (GitHub)</a>"
        )
        lbl_ver.setOpenExternalLinks(True)
        lbl_ver.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lbl_ver.setCursor(Qt.CursorShape.PointingHandCursor)
        lbl_ver.setStyleSheet("font-size: 11px;")

        lbl_author = QLabel(
            f"<span style='color:#9893B0;'>Автор:</span> "
            f"<a href='{AUTHOR_URL}' style='color:#E5E2F5; text-decoration:none; font-weight:600;'>"
            f"{AUTHOR_NAME}</a> "
            f"<span style='color:#7D7896;'>(github.com/dev-drozd)</span>"
        )
        lbl_author.setOpenExternalLinks(True)
        lbl_author.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        lbl_author.setCursor(Qt.CursorShape.PointingHandCursor)
        lbl_author.setStyleSheet("font-size: 11px;")

        meta_col.addWidget(lbl_ver)
        meta_col.addWidget(lbl_author)
        footer.addLayout(meta_col, 1)

        btn_close = QPushButton("Закрыть")
        btn_close.setMinimumWidth(84)
        btn_close.clicked.connect(self.accept)
        footer.addWidget(btn_close, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(footer)

    def _make_option_card(
        self,
        title: str,
        subtitle: str,
        open_url: Optional[str],
        copy_value: str,
        accent: str,
        mono_subtitle: bool = False,
    ) -> QWidget:
        card = QWidget()
        card.setObjectName("cardPanel")
        card.setStyleSheet(
            "QWidget#cardPanel { background-color: #151422; border: 1px solid #2A2740; border-radius: 9px; }"
        )
        lay = QHBoxLayout(card)
        lay.setContentsMargins(11, 8, 10, 8)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        lbl_t = QLabel(title)
        lbl_t.setStyleSheet(f"font-weight: 700; color: {accent}; font-size: 12px; border: none; background: transparent;")
        lbl_s = QLabel(subtitle)
        font_css = 'font-family: "JetBrains Mono", "Cascadia Code", "Consolas", monospace; ' if mono_subtitle else ""
        lbl_s.setStyleSheet(
            f"{font_css}color: #E8E5F7; font-size: 11.5px; border: none; background: transparent;"
        )
        lbl_s.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_col.addWidget(lbl_t)
        text_col.addWidget(lbl_s)
        lay.addLayout(text_col, 1)

        if open_url:
            btn_open = QPushButton(" Открыть")
            btn_open.setObjectName("accentBtn")
            btn_open.setIcon(svg_icon("external-link", "#FFFFFF", 12))
            btn_open.setIconSize(QSize(12, 12))
            btn_open.clicked.connect(lambda _c=False, u=open_url: QDesktopServices.openUrl(QUrl(u)))
            lay.addWidget(btn_open, 0, Qt.AlignmentFlag.AlignVCenter)

            btn_copy = QPushButton()
            btn_copy.setIcon(svg_icon("copy", "#D7D3EB", 13))
            btn_copy.setIconSize(QSize(13, 13))
            btn_copy.setFixedSize(30, 28)
            btn_copy.setToolTip("Копировать ссылку")
            btn_copy.clicked.connect(
                lambda _c=False, b=btn_copy, val=copy_value: self._copy_with_feedback(b, val, icon_only=True)
            )
            lay.addWidget(btn_copy, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            btn_copy = QPushButton(" Копировать ID")
            btn_copy.setObjectName("accentBtn")
            btn_copy.setIcon(svg_icon("copy", "#FFFFFF", 12))
            btn_copy.setIconSize(QSize(12, 12))
            btn_copy.clicked.connect(
                lambda _c=False, b=btn_copy, val=copy_value: self._copy_with_feedback(b, val, icon_only=False)
            )
            lay.addWidget(btn_copy, 0, Qt.AlignmentFlag.AlignVCenter)

        return card

    def _copy_with_feedback(self, btn: QPushButton, value: str, icon_only: bool) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(value)

        orig_text = btn.text()
        orig_icon = btn.icon()
        if icon_only:
            btn.setIcon(svg_icon("check", "#00E5C3", 13, 2.3))
        else:
            btn.setText(" Скопировано!")
            btn.setIcon(svg_icon("check", "#00E5C3", 12, 2.3))

        def _restore() -> None:
            try:
                if not icon_only:
                    btn.setText(orig_text)
                btn.setIcon(orig_icon)
            except RuntimeError:
                pass

        QTimer.singleShot(1500, _restore)
