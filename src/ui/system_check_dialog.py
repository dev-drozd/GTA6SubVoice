from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.system_check import SystemCheckReport
from src.ui.icons import svg_icon
from src.ui.styles import DARK_STYLESHEET
from src.ui.widgets import apply_interactive_cursors
from src.version import APP_NAME


class SystemRequirementsDialog(QDialog):
    """Окно уведомления о несоответствии ПК минимальным системным требованиям при запуске."""

    def __init__(self, report: SystemCheckReport, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.report = report
        self.setWindowTitle(f"{APP_NAME} — Проверка системы")
        self.setModal(True)
        self.setFixedWidth(480)
        self.setStyleSheet(DARK_STYLESHEET)

        self._build_ui()
        apply_interactive_cursors(self)
        self.adjustSize()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        # Заголовок с предупреждающей иконкой
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        hdr.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        badge = QLabel()
        badge.setFixedSize(38, 38)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(svg_icon("alert-triangle", "#FF4D6D", 20).pixmap(20, 20))
        badge.setStyleSheet(
            "background-color: #2A1420; border: 1px solid #7A2940; border-radius: 10px;"
        )
        hdr.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        lbl_title = QLabel("Ваш ПК не соответствует требованиям")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #FFFFFF;")
        lbl_sub = QLabel(f"Запуск {APP_NAME} невозможен на текущей конфигурации")
        lbl_sub.setStyleSheet("font-size: 11px; color: #FF7A95; font-weight: 600;")
        title_col.addWidget(lbl_title)
        title_col.addWidget(lbl_sub)
        hdr.addLayout(title_col, 1)
        root.addLayout(hdr)

        lbl_intro = QLabel(
            "Для локального распознавания субтитров и синтеза речи в реальном времени "
            "компьютер должен соответствовать следующим минимальным требованиям:"
        )
        lbl_intro.setWordWrap(True)
        lbl_intro.setStyleSheet("color: #CCC8E0; font-size: 12px;")
        root.addWidget(lbl_intro)

        # Карточка со списком проверок
        card = QFrame()
        card.setObjectName("cardPanel")
        card_lay = QVBoxLayout(card)
        card_lay.setContentsMargins(12, 10, 12, 10)
        card_lay.setSpacing(8)

        for idx, item in enumerate(self.report.items):
            if idx > 0:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.HLine)
                sep.setStyleSheet("background-color: #242138; max-height: 1px; border: none;")
                card_lay.addWidget(sep)

            row = QHBoxLayout()
            row.setSpacing(10)
            row.setAlignment(Qt.AlignmentFlag.AlignVCenter)

            st_icon = QLabel()
            st_icon.setFixedSize(22, 22)
            st_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
            if item.passed:
                st_icon.setPixmap(svg_icon("check", "#00E5C3", 15).pixmap(15, 15))
                st_icon.setStyleSheet("background-color: #112A28; border-radius: 6px;")
            else:
                st_icon.setPixmap(svg_icon("x", "#FF4D6D", 15).pixmap(15, 15))
                st_icon.setStyleSheet("background-color: #2E1521; border-radius: 6px;")
            row.addWidget(st_icon, 0, Qt.AlignmentFlag.AlignTop)

            text_col = QVBoxLayout()
            text_col.setSpacing(2)
            lbl_name = QLabel(item.title)
            lbl_name.setStyleSheet("font-weight: 700; color: #F5F4FB; font-size: 12px;")

            val_color = "#9EEBD9" if item.passed else "#FF8FA3"
            lbl_val = QLabel(f"На вашем ПК: {item.detected}")
            lbl_val.setWordWrap(True)
            lbl_val.setStyleSheet(f"color: {val_color}; font-size: 11px; font-weight: 600;")

            lbl_req = QLabel(f"Требуется: {item.required}")
            lbl_req.setStyleSheet("color: #9A96B0; font-size: 11px;")

            text_col.addWidget(lbl_name)
            text_col.addWidget(lbl_val)
            text_col.addWidget(lbl_req)
            row.addLayout(text_col, 1)

            card_lay.addLayout(row)

        root.addWidget(card)

        lbl_note = QLabel(
            "Если требования по процессору или памяти не выполнены, на ПК возможны вылеты, "
            "сильные зависания озвучки или нехватка памяти при работе вместе с игрой."
        )
        lbl_note.setWordWrap(True)
        lbl_note.setStyleSheet("color: #9A96B0; font-size: 11px;")
        root.addWidget(lbl_note)

        # Нижняя кнопка закрытия
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.btn_close = QPushButton("  Закрыть")
        self.btn_close.setObjectName("primaryStart")
        self.btn_close.setIcon(svg_icon("x", "#FFFFFF", 13))
        self.btn_close.setIconSize(QSize(13, 13))
        self.btn_close.setMinimumHeight(32)
        self.btn_close.setMinimumWidth(120)
        self.btn_close.clicked.connect(self.accept)
        btn_row.addWidget(self.btn_close)
        root.addLayout(btn_row)
