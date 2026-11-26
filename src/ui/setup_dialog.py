from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.dependency_manager import (
    DependencyManager,
    DownloadProgress,
    PreflightReport,
)
from src.ui.icons import svg_icon
from src.ui.styles import DARK_STYLESHEET
from src.ui.widgets import apply_interactive_cursors


class DownloadWorkerThread(QThread):
    progress_updated = Signal(object)  # Прогресс загрузки (DownloadProgress)
    finished_result = Signal(bool, str)

    def __init__(
        self,
        dep_manager: DependencyManager,
        component_ids: list[str],
        pause_event: threading.Event,
        cancel_event: threading.Event,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.dep_manager = dep_manager
        self.component_ids = component_ids
        self.pause_event = pause_event
        self.cancel_event = cancel_event

    def run(self) -> None:
        ok, msg = self.dep_manager.download_components(
            self.component_ids,
            progress_cb=lambda p: self.progress_updated.emit(p),
            pause_event=self.pause_event,
            cancel_event=self.cancel_event,
            verify_sha256=True,
        )
        self.finished_result.emit(ok, msg)


def _format_eta(seconds: float) -> str:
    if seconds <= 0:
        return "—"
    sec = int(round(seconds))
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h:d}ч {m:02d}м"
    if m > 0:
        return f"{m:d}м {s:02d}с"
    return f"{s:d}с"


class SetupDialog(QDialog):
    """Диалог явного согласия пользователя на проверку, скачивание или ручной выбор недостающих моделей."""

    def __init__(
        self,
        dep_manager: DependencyManager,
        report: PreflightReport,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.dep_manager = dep_manager
        self.report = report
        self.switched_to_winrt: bool = False
        self.installed_any: bool = False

        self._worker: Optional[DownloadWorkerThread] = None
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._checkboxes: dict[str, QCheckBox] = {}

        self.setWindowTitle("GTA6SubVoice — Установка необходимых компонентов")
        self.setMinimumSize(600, 380)
        self.resize(660, 420)
        self.setStyleSheet(DARK_STYLESHEET)

        self._build_ui()
        self._populate_table()
        apply_interactive_cursors(self)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        title = QLabel("Установка необходимых моделей и компонентов")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #ffffff;")
        root.addWidget(title)

        subtitle = QLabel(
            "Приложение никогда не загружает файлы без вашего разрешения. "
            "Выберите нужные компоненты для загрузки из официальных источников или укажите локальный файл вручную."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet("color: #aab4c8;")
        root.addWidget(subtitle)

        # Предупреждение Windows OCR (если выбран winrt без русского языкового пакета)
        self.winrt_banner = QWidget()
        self.winrt_banner.setStyleSheet(
            "background-color: #2b2214; border: 1px solid #c98a2c; border-radius: 6px; padding: 6px;"
        )
        wb_layout = QHBoxLayout(self.winrt_banner)
        wb_layout.setContentsMargins(10, 6, 10, 6)
        lbl_winrt = QLabel(
            "Системный языковой пакет Windows OCR (ru-RU) не обнаружен. "
            "Установите русский язык распознавания в настройках Windows или используйте RapidOCR."
        )
        lbl_winrt.setWordWrap(True)
        lbl_winrt.setStyleSheet("border: none; color: #f6cf8a;")
        wb_layout.addWidget(lbl_winrt, 1)
        btn_win_lang = QPushButton("Настройки языка Windows")
        btn_win_lang.clicked.connect(self._open_windows_language_settings)
        wb_layout.addWidget(btn_win_lang)
        self.winrt_banner.setVisible(self.report.ocr_engine == "winrt" and not self.report.winrt_ru_available)
        root.addWidget(self.winrt_banner)

        # Таблица компонентов
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["Выбор", "Компонент", "Статус", "Размер", "Источник (URL)", "Целевой путь"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 52)
        root.addWidget(self.table, 1)

        # Блок прогресса скачивания
        prog_box = QWidget()
        prog_box.setStyleSheet("background-color: #171a22; border: 1px solid #262b38; border-radius: 6px;")
        pb_layout = QVBoxLayout(prog_box)
        pb_layout.setContentsMargins(10, 8, 10, 8)
        pb_layout.setSpacing(6)

        self.lbl_file_progress = QLabel("Готово к установке. Ожидание подтверждения пользователя.")
        self.lbl_file_progress.setStyleSheet("border: none; color: #cfd8ea; font-weight: 600;")
        pb_layout.addWidget(self.lbl_file_progress)

        self.bar_file = QProgressBar()
        self.bar_file.setRange(0, 100)
        self.bar_file.setValue(0)
        self.bar_file.setFormat("Текущий файл: %p%")
        pb_layout.addWidget(self.bar_file)

        self.bar_overall = QProgressBar()
        self.bar_overall.setRange(0, 100)
        self.bar_overall.setValue(0)
        self.bar_overall.setFormat("Общий прогресс: %p%")
        pb_layout.addWidget(self.bar_overall)

        self.lbl_stats = QLabel("0.0 / 0.0 МБ  •  0%  •  Скорость: —  •  Осталось: —")
        self.lbl_stats.setStyleSheet("border: none; color: #8fa0be; font-family: Consolas, monospace;")
        pb_layout.addWidget(self.lbl_stats)
        root.addWidget(prog_box)

        # Кнопки действий
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_download = QPushButton(" Скачать и установить")
        self.btn_download.setObjectName("primaryStart")
        self.btn_download.setIcon(svg_icon("download", "#FFFFFF", 14))
        self.btn_download.setIconSize(QSize(14, 14))
        self.btn_download.clicked.connect(self._start_download)
        btn_row.addWidget(self.btn_download)

        self.btn_pause = QPushButton(" Пауза")
        self.btn_pause.setIcon(svg_icon("pause", "#F2F1FA", 13))
        self.btn_pause.setIconSize(QSize(13, 13))
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self._toggle_pause)
        btn_row.addWidget(self.btn_pause)

        self.btn_cancel_dl = QPushButton(" Отмена")
        self.btn_cancel_dl.setIcon(svg_icon("x", "#FFB6C9", 14))
        self.btn_cancel_dl.setIconSize(QSize(14, 14))
        self.btn_cancel_dl.setObjectName("dangerBtn")
        self.btn_cancel_dl.setEnabled(False)
        self.btn_cancel_dl.clicked.connect(self._cancel_download)
        btn_row.addWidget(self.btn_cancel_dl)

        btn_row.addStretch()

        self.btn_manual = QPushButton("Указать локальный файл вручную...")
        self.btn_manual.clicked.connect(self._manual_select_file)
        btn_row.addWidget(self.btn_manual)

        self.btn_close = QPushButton("Отложить / Закрыть")
        self.btn_close.clicked.connect(self._on_close_clicked)
        btn_row.addWidget(self.btn_close)

        root.addLayout(btn_row)

    def _populate_table(self) -> None:
        self._checkboxes.clear()
        self.table.setRowCount(0)
        status_labels = {
            "installed": ("Установлен", "#6be09d"),
            "missing": ("Отсутствует", "#ff7b8a"),
            "corrupted": ("Повреждён", "#f0ad62"),
        }

        for row, item in enumerate(self.report.components):
            self.table.insertRow(row)
            spec = item.spec

            chk_wrap = QWidget()
            chk_lay = QHBoxLayout(chk_wrap)
            chk_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chk_lay.setContentsMargins(0, 0, 0, 0)
            chk = QCheckBox()
            need_dl = item.status != "installed"
            default_checked = need_dl and (
                spec.required
                or (spec.id == "rapidocr_cyrillic" and self.report.ocr_engine == "rapidocr")
            )
            chk.setChecked(default_checked)
            if spec.required and need_dl:
                chk.setEnabled(False)  # Обязательный компонент нельзя снять, пока он отсутствует
                chk.setToolTip("Обязательный компонент для озвучки")
            elif not need_dl:
                chk.setToolTip("Уже установлен — можно отметить для переустановки")
            else:
                chk.setToolTip("Опциональный: при снятии будет использован встроенный Windows OCR")
            self._checkboxes[spec.id] = chk
            chk_lay.addWidget(chk)
            self.table.setCellWidget(row, 0, chk_wrap)

            name_item = QTableWidgetItem(spec.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(spec.description)
            self.table.setItem(row, 1, name_item)

            st_text, st_color = status_labels.get(item.status, (item.status, "#dce3f0"))
            st_item = QTableWidgetItem(st_text)
            st_item.setForeground(QColor(st_color))
            st_item.setFlags(st_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            st_item.setToolTip(item.message)
            self.table.setItem(row, 2, st_item)

            sz_item = QTableWidgetItem(f"{spec.total_size_mb:.1f} МБ")
            sz_item.setFlags(sz_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 3, sz_item)

            url_item = QTableWidgetItem(spec.source_summary)
            url_item.setFlags(url_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            url_item.setToolTip(spec.source_summary)
            self.table.setItem(row, 4, url_item)

            path_item = QTableWidgetItem(str(item.target_path))
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            path_item.setToolTip(str(item.target_path))
            self.table.setItem(row, 5, path_item)

            self.table.setRowHeight(row, 32)

    def _open_windows_language_settings(self) -> None:
        try:
            os.startfile("ms-settings:regionlanguage")
        except Exception:
            QMessageBox.information(
                self,
                "Языковые настройки Windows",
                "Откройте: Параметры Windows → Время и язык → Язык и регион → Русский → Языковые параметры.",
            )

    def _selected_component_ids(self) -> list[str]:
        return [cid for cid, chk in self._checkboxes.items() if chk.isChecked()]

    def _set_downloading_state(self, active: bool) -> None:
        self.btn_download.setEnabled(not active)
        self.btn_manual.setEnabled(not active)
        self.btn_close.setEnabled(not active)
        self.btn_pause.setEnabled(active)
        self.btn_cancel_dl.setEnabled(active)
        for cid, chk in self._checkboxes.items():
            if not active:
                comp = next((c for c in self.report.components if c.spec.id == cid), None)
                chk.setEnabled(not (comp and comp.spec.required and comp.status != "installed"))
            else:
                chk.setEnabled(False)

    def _start_download(self) -> None:
        selected = self._selected_component_ids()
        if not selected:
            # Проверяем, не пропустил ли пользователь опциональные компоненты при уже установленной Silero
            silero_ok = any(c.spec.id == "silero_v5_6" and c.status == "installed" for c in self.report.components)
            if silero_ok:
                rapid_item = next((c for c in self.report.components if c.spec.id == "rapidocr_cyrillic"), None)
                if rapid_item and rapid_item.status != "installed":
                    self.switched_to_winrt = True
                self.accept()
                return
            QMessageBox.warning(self, "Нет выбранных компонентов", "Выберите хотя бы один компонент для установки.")
            return

        # Если пользователь снял галочку с RapidOCR, переключаем конфиг на Windows OCR (WinRT)
        if "rapidocr_cyrillic" not in selected:
            rapid_item = next((c for c in self.report.components if c.spec.id == "rapidocr_cyrillic"), None)
            if rapid_item and rapid_item.status != "installed":
                self.switched_to_winrt = True

        self._pause_event.clear()
        self._cancel_event.clear()
        self.btn_pause.setText(" Пауза")
        self.btn_pause.setIcon(svg_icon("pause", "#F2F1FA", 13))
        self._set_downloading_state(True)
        self.lbl_file_progress.setText("Подключение к серверу загрузки...")

        self._worker = DownloadWorkerThread(
            self.dep_manager,
            selected,
            self._pause_event,
            self._cancel_event,
            parent=self,
        )
        self._worker.progress_updated.connect(self._on_progress)
        self._worker.finished_result.connect(self._on_download_finished)
        self._worker.start()

    def _toggle_pause(self) -> None:
        if self._pause_event.is_set():
            self._pause_event.clear()
            self.btn_pause.setText(" Пауза")
            self.btn_pause.setIcon(svg_icon("pause", "#F2F1FA", 13))
            self.lbl_file_progress.setText("Возобновление загрузки (HTTP Range)...")
        else:
            self._pause_event.set()
            self.btn_pause.setText(" Продолжить")
            self.btn_pause.setIcon(svg_icon("play", "#F2F1FA", 13))
            self.lbl_file_progress.setText("Загрузка на паузе. Нажмите «Продолжить» для докачки.")

    def _cancel_download(self) -> None:
        self._cancel_event.set()
        self._pause_event.clear()
        self.lbl_file_progress.setText("Отмена загрузки и очистка временных файлов...")

    def _on_progress(self, prog: DownloadProgress) -> None:
        file_pct = int(round((prog.file_downloaded / max(1, prog.file_total)) * 100.0))
        all_pct = int(round(prog.percent))
        self.bar_file.setValue(max(0, min(100, file_pct)))
        self.bar_overall.setValue(max(0, min(100, all_pct)))
        self.lbl_file_progress.setText(f"Скачивание: {prog.file_name}")

        done_mb = prog.overall_downloaded / (1024.0 * 1024.0)
        tot_mb = prog.overall_total / (1024.0 * 1024.0)
        self.lbl_stats.setText(
            f"{done_mb:.1f} / {tot_mb:.1f} МБ  •  {prog.percent:.1f}%  •  "
            f"Скорость: {prog.speed_mbps:.2f} МБ/с  •  Осталось: {_format_eta(prog.eta_seconds)}"
        )

    def _on_download_finished(self, ok: bool, message: str) -> None:
        self._set_downloading_state(False)
        self.report = self.dep_manager.run_preflight(ocr_engine="winrt" if self.switched_to_winrt else self.report.ocr_engine)
        self._populate_table()

        if ok:
            self.installed_any = True
            self.bar_file.setValue(100)
            self.bar_overall.setValue(100)
            self.lbl_file_progress.setText(message)
            self.btn_download.setText(" Скачать и установить")
            self.btn_download.setIcon(svg_icon("download", "#FFFFFF", 14))
            QMessageBox.information(self, "Установка завершена", message)
            self.accept()
        else:
            self.lbl_file_progress.setText(message)
            self.btn_download.setText(" Повторить загрузку")
            self.btn_download.setIcon(svg_icon("refresh-cw", "#FFFFFF", 14))
            if not self._cancel_event.is_set():
                QMessageBox.warning(self, "Ошибка загрузки", message)

    def _manual_select_file(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Укажите локальный файл модели Silero TTS 5.6 (.pt)",
            "",
            "PyTorch Package (*.pt);;All Files (*)",
        )
        if not path_str:
            return
        ok, msg = self.dep_manager.install_local_file("silero_v5_6", Path(path_str))
        if ok:
            self.installed_any = True
            self.report = self.dep_manager.run_preflight(ocr_engine=self.report.ocr_engine)
            self._populate_table()
            self.lbl_file_progress.setText(msg)
            if self.report.all_ready:
                self.accept()
        else:
            QMessageBox.warning(self, "Некорректный файл модели", msg)

    def _on_close_clicked(self) -> None:
        if self._worker and self._worker.isRunning():
            self._cancel_event.set()
            self._worker.wait(2000)
        self.reject()
