from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.ui.icons import svg_icon
from src.ui.styles import DARK_STYLESHEET
from src.ui.widgets import apply_interactive_cursors
from src.updater import (
    ReleaseInfo,
    UpdateDownloadProgress,
    create_staging_dir,
    download_and_verify_release,
    extract_release_zip,
    fetch_latest_release,
    is_newer_version,
    is_self_update_supported,
    launch_self_update,
)
from src.version import APP_NAME, APP_VERSION


class UpdateCheckThread(QThread):
    """Фоновый поток проверки наличия новой версии программы на GitHub Releases."""

    checked = Signal(object, str, bool)  # (ReleaseInfo_или_None, сообщение_ошибки, ручная_проверка)

    def __init__(self, manual: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.manual = manual

    def run(self) -> None:
        try:
            rel = fetch_latest_release()
            if rel is not None and is_newer_version(rel.tag_name, APP_VERSION):
                self.checked.emit(rel, "", self.manual)
            else:
                self.checked.emit(None, "", self.manual)
        except Exception as exc:
            self.checked.emit(None, str(exc), self.manual)


class _UpdateWorker(QThread):
    """Фоновый поток загрузки, проверки SHA-256 и распаковки архива обновления."""

    progress_changed = Signal(object)
    stage_changed = Signal(str)
    finished_ready = Signal(object, object)  # Кортеж путей (source_app_dir, staging_root)
    failed = Signal(str)

    def __init__(self, release: ReleaseInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.release = release
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        staging_root: Optional[Path] = None
        try:
            staging_root = create_staging_dir()
            self.stage_changed.emit("Скачиваю архив обновления...")
            zip_path = download_and_verify_release(
                self.release,
                dest_dir=staging_root,
                progress_cb=lambda p: self.progress_changed.emit(p),
                cancel_event=self.cancel_event,
            )
            self.stage_changed.emit("Проверяю и распаковываю файлы...")
            source_app_dir = extract_release_zip(zip_path, staging_root / "extracted")
            self.finished_ready.emit(source_app_dir, staging_root)
        except Exception as exc:
            if staging_root is not None and staging_root.exists():
                shutil.rmtree(staging_root, ignore_errors=True)
            self.failed.emit(str(exc))


class UpdateDialog(QDialog):
    """Окно предложения и выполнения обновления приложения с GitHub Releases."""

    def __init__(
        self,
        release: ReleaseInfo,
        on_restart_requested,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.release = release
        self._on_restart_requested = on_restart_requested
        self._worker: Optional[_UpdateWorker] = None

        self.setWindowTitle(f"Обновление {APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(470)
        self.setStyleSheet(DARK_STYLESHEET)

        self._build_ui()
        apply_interactive_cursors(self)
        self.adjustSize()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        # Шапка окна обновления
        hdr = QHBoxLayout()
        hdr.setSpacing(10)
        hdr.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        badge = QLabel()
        badge.setFixedSize(38, 38)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setPixmap(svg_icon("sparkles", "#00E5C3", 19).pixmap(19, 19))
        badge.setStyleSheet(
            "background-color: #102B2B; border: 1px solid #1E6E64; border-radius: 10px;"
        )
        hdr.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        lbl_title = QLabel(f"Доступна новая версия: v{self.release.version}")
        lbl_title.setStyleSheet("font-size: 15px; font-weight: 700; color: #FFFFFF;")
        size_mb = self.release.asset_size / (1024.0 * 1024.0) if self.release.asset_size > 0 else 0.0
        size_hint = f" • Размер: ~{size_mb:.0f} МБ" if size_mb > 0 else ""
        lbl_sub = QLabel(f"Установлена: v{APP_VERSION}{size_hint}")
        lbl_sub.setStyleSheet("font-size: 11px; color: #9EEBD9; font-weight: 600;")
        title_col.addWidget(lbl_title)
        title_col.addWidget(lbl_sub)
        hdr.addLayout(title_col, 1)
        root.addLayout(hdr)

        # Описание изменений (Changelog из GitHub Release)
        notes_card = QFrame()
        notes_card.setObjectName("cardPanel")
        nc_lay = QVBoxLayout(notes_card)
        nc_lay.setContentsMargins(10, 10, 10, 10)
        nc_lay.setSpacing(6)

        lbl_notes_hdr = QLabel("Что нового в релизе:")
        lbl_notes_hdr.setStyleSheet("font-weight: 700; color: #E8E5FA; font-size: 12px;")
        nc_lay.addWidget(lbl_notes_hdr)

        self.txt_notes = QTextEdit()
        self.txt_notes.setReadOnly(True)
        self.txt_notes.setMinimumHeight(130)
        self.txt_notes.setMaximumHeight(220)
        body_text = self.release.notes or "Вышло новое обновление программы с улучшениями и исправлениями."
        self.txt_notes.setMarkdown(body_text)
        nc_lay.addWidget(self.txt_notes)
        root.addWidget(notes_card)

        # Прогресс загрузки обновления (скрыт до нажатия «Обновить сейчас»)
        self.lbl_progress = QLabel("")
        self.lbl_progress.setStyleSheet("color: #B9B4D0; font-size: 11px;")
        self.lbl_progress.setVisible(False)
        root.addWidget(self.lbl_progress)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(16)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        # Нижние кнопки
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self.btn_open_web = QPushButton("  Открыть на GitHub")
        self.btn_open_web.setIcon(svg_icon("external-link", "#E3DFF7", 13))
        self.btn_open_web.setIconSize(QSize(13, 13))
        self.btn_open_web.setMinimumHeight(32)
        self.btn_open_web.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self.release.html_url))
        )

        self.btn_later = QPushButton("Позже")
        self.btn_later.setMinimumHeight(32)
        self.btn_later.clicked.connect(self._on_cancel_or_later)

        self.btn_update = QPushButton("  Обновить сейчас")
        self.btn_update.setObjectName("primaryStart")
        self.btn_update.setIcon(svg_icon("download", "#FFFFFF", 13))
        self.btn_update.setIconSize(QSize(13, 13))
        self.btn_update.setMinimumHeight(32)
        self.btn_update.clicked.connect(self._on_start_update)

        btn_row.addWidget(self.btn_open_web)
        btn_row.addStretch(1)
        btn_row.addWidget(self.btn_later)
        btn_row.addWidget(self.btn_update)
        root.addLayout(btn_row)

        if not is_self_update_supported():
            self.lbl_progress.setText(
                "Программа запущена из исходников Python (.py) — автоматическая замена файлов "
                "доступна только в собранной версии .exe."
            )
            self.lbl_progress.setStyleSheet("color: #FF9F43; font-size: 11px;")
            self.lbl_progress.setVisible(True)
            self.btn_update.setEnabled(False)

    def _on_start_update(self) -> None:
        if self._worker is not None:
            return
        self.btn_update.setEnabled(False)
        self.btn_later.setText("Отмена")
        self.lbl_progress.setStyleSheet("color: #B9B4D0; font-size: 11px;")
        self.lbl_progress.setText("Подготовка к загрузке обновления...")
        self.lbl_progress.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)

        self._worker = _UpdateWorker(self.release, parent=self)
        self._worker.stage_changed.connect(self.lbl_progress.setText)
        self._worker.progress_changed.connect(self._on_progress)
        self._worker.finished_ready.connect(self._on_download_finished)
        self._worker.failed.connect(self._on_download_failed)
        self._worker.start()

    def _on_progress(self, p: UpdateDownloadProgress) -> None:
        self.progress_bar.setValue(int(round(p.percent)))
        dl_mb = p.downloaded_bytes / (1024.0 * 1024.0)
        tot_mb = p.total_bytes / (1024.0 * 1024.0)
        self.lbl_progress.setText(
            f"Загрузка: {dl_mb:.1f} / {tot_mb:.1f} МБ ({p.percent:.0f}%) • {p.speed_mbps:.1f} МБ/с"
        )

    def _on_download_finished(self, source_app_dir: Path, staging_root: Path) -> None:
        self.progress_bar.setValue(100)
        self.lbl_progress.setText("Перезапуск и установка обновления...")
        QApplication.processEvents()
        if self._worker is not None and self._worker.isRunning():
            self._worker.wait(2500)
        try:
            launch_self_update(source_app_dir=source_app_dir, staging_root=staging_root)
            self.hide()
            self.accept()
            QApplication.processEvents()
            if callable(self._on_restart_requested):
                QTimer.singleShot(50, self._on_restart_requested)
        except Exception as exc:
            self._worker = None
            self.btn_update.setEnabled(is_self_update_supported())
            self.btn_later.setText("Позже")
            QMessageBox.warning(
                self,
                "Ошибка обновления",
                f"Не удалось запустить установку обновления:\n{exc}",
            )

    def _on_download_failed(self, msg: str) -> None:
        self._worker = None
        self.btn_update.setEnabled(is_self_update_supported())
        self.btn_later.setText("Позже")
        self.progress_bar.setVisible(False)
        if "отменено" in msg.lower():
            self.lbl_progress.setText("Загрузка отменена.")
            return
        self.lbl_progress.setStyleSheet("color: #FF4D6D; font-size: 11px;")
        self.lbl_progress.setText(f"Ошибка: {msg}")

    def _on_cancel_or_later(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self.lbl_progress.setText("Отменяю загрузку...")
            self._worker.cancel()
            self._worker.wait(2500)
        self.reject()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(2500)
        super().closeEvent(event)
