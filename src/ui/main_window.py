from __future__ import annotations

import os
import weakref
import webbrowser
from pathlib import Path
from typing import Optional

import psutil
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.audio_dsp import PRESET_DISPLAY_LABELS, normalize_speed_preset
from src.audio_player import list_output_devices
from src.capture import bring_target_window_to_front, ensure_target_window_windowed, find_target_window_hwnd
from src.config import ConfigManager, RoiConfig, export_profile_bundle, load_profile_bundle
from src.dependency_manager import DependencyManager
from src.ducking import PRESET_GAME_PROCESSES, list_selectable_processes
from src.hotkeys import GlobalHotkeyManager
from src.mobile_companion import (
    MobileCompanionServer,
    detect_lan_ips,
    generate_access_token,
    open_firewall_port_windows,
)
from src.paths import find_silero_model, to_portable_path
from src.pipeline import PipelineLogEvent, SubtitlePipeline
from src.qr_code import render_qr_pixmap
from src.text_filter import StressDictManager
from src.ui.icons import svg_icon
from src.ui.roi_overlay import RoiOverlay
from src.ui.styles import DARK_STYLESHEET
from src.ui.support_dialog import SupportDialog
from src.ui.tray import TrayController, build_app_icon
from src.ui.update_dialog import UpdateCheckThread, UpdateDialog
from src.version import APP_VERSION
from src.ui.widgets import (
    AnimatedStatusBadge,
    TactileSlider,
    ViceHeaderBar,
    ViceToggleSwitch,
    apply_interactive_cursors,
)
from src.voice_manager import (
    ALL_SILERO_SPEAKERS,
    FEMALE_SPEAKERS,
    MALE_SPEAKERS,
    VoiceManager,
    clean_character_name,
    speaker_display_name,
)

MAX_LOG_ITEMS = 20
NO_TARGET_PROCESS_LABEL = "— Выберите процесс / окно —"

STATE_COLORS = {
    "idle": "#9A96B0",
    "loading": "#FF7A3D",
    "waiting": "#00E5C3",
    "paused_window": "#FF9F43",
    "speaking": "#FF2E93",
    "filtered": "#FF9F43",
    "error": "#FF4D6D",
}


class AddCharacterDialog(QDialog):
    """Простой диалог ручного создания персонажа (Имя + Пол + Чистый нативный голос)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Добавить персонажа")
        self.setMinimumWidth(310)
        self.setStyleSheet(DARK_STYLESHEET)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setSpacing(8)

        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("Например: Джейсон, Люсия, Брайан...")
        form.addRow("Имя персонажа:", self.edit_name)

        self.combo_gender = QComboBox()
        self.combo_gender.addItem("Мужской (М)", "male")
        self.combo_gender.addItem("Женский (Ж)", "female")
        form.addRow("Пол:", self.combo_gender)

        self.combo_voice = QComboBox()
        for sp in ALL_SILERO_SPEAKERS:
            self.combo_voice.addItem(speaker_display_name(sp), sp)
        form.addRow("Чистый голос:", self.combo_voice)

        self.combo_gender.currentIndexChanged.connect(self._on_gender_changed)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        apply_interactive_cursors(self)

    def _on_gender_changed(self) -> None:
        g = self.combo_gender.currentData()
        default_sp = "eugene" if g == "male" else "kseniya"
        idx = self.combo_voice.findData(default_sp)
        if idx >= 0:
            self.combo_voice.setCurrentIndex(idx)

    def get_values(self) -> tuple[str, str, str]:
        return (
            clean_character_name(self.edit_name.text()),
            str(self.combo_gender.currentData()),
            str(self.combo_voice.currentData()),
        )


class SelectProcessDialog(QDialog):
    """Диалог с предложением выбрать из списка или ввести целевой процесс игры / окна."""

    def __init__(
        self,
        prompt_text: Optional[str] = None,
        current_process: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Выбор окна процесса")
        self.setMinimumWidth(440)
        self.setMinimumHeight(340)
        self.setStyleSheet(DARK_STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        msg = QLabel(
            prompt_text
            or "Выберите окно игры или приложение, с которого будут считываться субтитры:"
        )
        msg.setWordWrap(True)
        msg.setStyleSheet("color: #EAE8F7; font-size: 12px;")
        layout.addWidget(msg)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        lbl_list = QLabel("Запущенные окна и аудиопроцессы:")
        lbl_list.setStyleSheet("color: #9A96B0; font-size: 11px;")
        self.btn_refresh = QPushButton(" Обновить")
        self.btn_refresh.setIcon(svg_icon("refresh-cw", "#E3DFF7", 13))
        self.btn_refresh.setIconSize(QSize(13, 13))
        top_row.addWidget(lbl_list, 1)
        top_row.addWidget(self.btn_refresh)
        layout.addLayout(top_row)

        self.list_proc = QListWidget()
        self.list_proc.setMinimumHeight(170)
        layout.addWidget(self.list_proc, 1)

        manual_row = QHBoxLayout()
        manual_row.setSpacing(8)
        manual_row.addWidget(QLabel("Имя процесса (.exe):"))
        self.edit_exe = QLineEdit()
        self.edit_exe.setPlaceholderText("Например: XboxPcApp.exe, GTA6.exe, chrome.exe...")
        if current_process.strip():
            self.edit_exe.setText(current_process.strip())
        manual_row.addWidget(self.edit_exe, 1)
        layout.addLayout(manual_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel_btn = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if ok_btn is not None:
            ok_btn.setText("Выбрать")
            ok_btn.setObjectName("accentBtn")
        if cancel_btn is not None:
            cancel_btn.setText("Отмена")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.btn_refresh.clicked.connect(self._populate)
        self.list_proc.itemClicked.connect(self._on_item_clicked)
        self.list_proc.itemDoubleClicked.connect(self._on_item_double_clicked)

        self._populate(current_process=current_process)
        apply_interactive_cursors(self)

    def _populate(self, _checked: bool = False, current_process: str = "") -> None:
        target = (current_process or self.edit_exe.text()).strip().lower()
        self.list_proc.clear()
        items = list_selectable_processes()
        if not items:
            placeholder = QListWidgetItem("Открытые окна / аудиопроцессы не обнаружены — введите .exe ниже")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list_proc.addItem(placeholder)
            return
        matched_row = -1
        for idx, (exe, label) in enumerate(items):
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, exe)
            self.list_proc.addItem(item)
            if target and exe.lower() == target and matched_row < 0:
                matched_row = idx
        if matched_row >= 0:
            self.list_proc.setCurrentRow(matched_row)
        elif not target and self.list_proc.count() > 0:
            self.list_proc.setCurrentRow(0)
            first_exe = str(self.list_proc.item(0).data(Qt.ItemDataRole.UserRole) or "")
            if first_exe:
                self.edit_exe.setText(first_exe)

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        exe = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if exe:
            self.edit_exe.setText(exe)

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        exe = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if exe:
            self.edit_exe.setText(exe)
            self.accept()

    def _on_accept(self) -> None:
        if not self.get_selected_process():
            QMessageBox.warning(
                self,
                "Процесс не выбран",
                "Выберите приложение из списка или введите имя процесса (например: GTA6.exe).",
            )
            return
        self.accept()

    def get_selected_process(self) -> str:
        raw = self.edit_exe.text().strip()
        if raw == NO_TARGET_PROCESS_LABEL:
            return ""
        if " — " in raw:
            raw = raw.split(" — ", 1)[0].strip()
        return raw


class MainWindow(QMainWindow):
    mobile_stress_synced = Signal(str, str, str)
    mobile_character_synced = Signal(str, str, str, str)
    mobile_client_activity = Signal(str, int, str)
    mobile_report_saved = Signal(str, str)
    mobile_settings_synced = Signal()

    def __init__(
        self,
        config_manager: ConfigManager,
        voice_manager: VoiceManager,
        stress_dict: StressDictManager,
        pipeline: SubtitlePipeline,
        hotkey_manager: GlobalHotkeyManager,
        dep_manager: DependencyManager,
    ) -> None:
        super().__init__()
        self.cfg_mgr = config_manager
        self.voice_mgr = voice_manager
        self.stress_dict = stress_dict
        self.pipeline = pipeline
        self.hotkey_mgr = hotkey_manager
        self.dep_mgr = dep_manager

        self._roi_overlay: Optional[RoiOverlay] = None
        self._roi_opening = False
        self._main_was_visible_before_roi = False
        self._force_quit = False
        self._did_shutdown = False
        self._table_updating = False
        self._syncing_ui = False
        self._proc = psutil.Process(os.getpid())

        # Таймеры с задержкой (debounce) для автосохранения и перезагрузки моделей без кнопки «Сохранить»
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(150)
        self._save_timer.timeout.connect(self._flush_auto_save)

        self._reload_model_timer = QTimer(self)
        self._reload_model_timer.setSingleShot(True)
        self._reload_model_timer.setInterval(500)
        self._reload_model_timer.timeout.connect(self.pipeline.load_models_async)

        self._mobile_port_timer = QTimer(self)
        self._mobile_port_timer.setSingleShot(True)
        self._mobile_port_timer.setInterval(450)
        self._mobile_port_timer.timeout.connect(self._apply_mobile_sync_settings)

        self.mobile_server = MobileCompanionServer(
            cfg_mgr=self.cfg_mgr,
            stress_dict=self.stress_dict,
            voice_mgr=self.voice_mgr,
            pipeline=self.pipeline,
            on_stress_changed=lambda act, w, r: self.mobile_stress_synced.emit(act, w, r),
            on_character_changed=lambda act, n, g, sp: self.mobile_character_synced.emit(act, n, g, sp),
            on_client_activity=lambda ip, cnt, ts: self.mobile_client_activity.emit(ip, cnt, ts),
            on_report_saved=lambda sp, txt: self.mobile_report_saved.emit(sp, txt),
            on_settings_changed=lambda _d: self.mobile_settings_synced.emit(),
        )

        self.setWindowTitle("GTA VI SubVoice — Нейронная озвучка")
        self.setWindowIcon(build_app_icon(active=False))
        self.setStyleSheet(DARK_STYLESHEET)
        if self.cfg_mgr.config.always_on_top:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        self._build_ui()
        self._bind_signals()
        self._sync_ui_from_config()
        self._apply_ui_mode(self.cfg_mgr.config.ui_mode, save=False)
        self._refresh_characters_table()
        self._refresh_stress_table()
        self._register_hotkeys()
        apply_interactive_cursors(self)

        self.tray = TrayController(
            on_show_hide=self.toggle_window_visibility,
            on_toggle_pause=self._toggle_start_stop,
            on_select_roi=self.open_roi_selector,
            on_interrupt=self.pipeline.interrupt_speech,
            on_quit=self.quit_application,
            parent=self,
        )

        self._ram_timer = QTimer(self)
        self._ram_timer.setInterval(3000)
        self._ram_timer.timeout.connect(self._update_ram_label)
        self._ram_timer.start()
        self._update_ram_label()
        self._refresh_own_hwnds()

        # Фоновая проверка обновлений на GitHub при старте
        self._update_check_thread: Optional[UpdateCheckThread] = None
        self._startup_update_checked = False
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(1500)
        self._update_timer.timeout.connect(lambda: self._start_update_check(manual=False))

    # ------------------------------------------------------------------ Инициализация при запуске
    def start_bootstrap(self) -> None:
        self._show_loading_banner("Проверка локальных моделей и аудиоустройств...")
        preflight = self.dep_mgr.run_preflight(ocr_engine=self.cfg_mgr.config.ocr_engine, verify_hash=False)
        self._refresh_components_table(verify_hash=False)
        self._populate_audio_devices()

        silero_ready = any(c.spec.id == "silero_v5_6" and c.status == "installed" for c in preflight.components)
        if silero_ready:
            self.pipeline.load_models_async()
        else:
            self._hide_loading_banner()
            self._on_status_changed("error", "Нет модели Silero (models/v5_6_ru.pt)")
            self._on_log_event(
                PipelineLogEvent(
                    "ERR",
                    "error",
                    "—",
                    self.cfg_mgr.config.ocr_engine,
                    0.0,
                    "Файл модели Silero не найден в папке models/. Проверьте целостность сборки.",
                )
            )
        if not self._startup_update_checked:
            self._startup_update_checked = True
            self._update_timer.start()
        if self.cfg_mgr.config.mobile_sync_enabled:
            self._apply_mobile_sync_settings()

    def _show_loading_banner(self, text: str) -> None:
        self.lbl_boot_status.setText(text)
        self.boot_banner.setVisible(True)
        self.btn_start_stop.setEnabled(False)
        self.btn_test_voice.setEnabled(False)

    def _hide_loading_banner(self) -> None:
        self.boot_banner.setVisible(False)
        self.btn_start_stop.setEnabled(True)
        self.btn_test_voice.setEnabled(True)

    # ------------------------------------------------------------------ Построение интерфейса
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Единая шапка Vice City с анимированной сумеречной акцентной линией
        self.header_bar = ViceHeaderBar()
        hb_layout = QHBoxLayout(self.header_bar)
        hb_layout.setContentsMargins(10, 6, 8, 8)
        hb_layout.setSpacing(6)
        hb_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        brand = QLabel(
            "<span style='font-size:13px; font-weight:800; color:#FF2E88;'>GTA VI</span>"
            "<span style='font-size:12px; font-weight:700; color:#FF8452;'> SUBVOICE</span>"
        )
        brand.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        brand.setMinimumWidth(116)
        brand.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        brand.setStyleSheet("border: none; background: transparent; padding-bottom: 1px;")
        hb_layout.addWidget(brand, 0, Qt.AlignmentFlag.AlignVCenter)

        self.status_badge = AnimatedStatusBadge("Запуск...")
        hb_layout.addWidget(self.status_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        hb_layout.addStretch()

        self.btn_support = QPushButton(" Поддержать")
        self.btn_support.setObjectName("supportBtn")
        self.btn_support.setIcon(svg_icon("heart", "#FF4A98", 12))
        self.btn_support.setIconSize(QSize(12, 12))
        self.btn_support.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_support.setToolTip("Поддержать разработку и информация о версии")
        hb_layout.addWidget(self.btn_support, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn_toggle_mode = QPushButton(" Эксперт")
        self.btn_toggle_mode.setObjectName("modeToggleBtn")
        self.btn_toggle_mode.setIcon(svg_icon("sliders", "#E3DFF7", 13))
        self.btn_toggle_mode.setIconSize(QSize(13, 13))
        self.btn_toggle_mode.setCursor(Qt.CursorShape.PointingHandCursor)
        hb_layout.addWidget(self.btn_toggle_mode, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addWidget(self.header_bar)

        # Баннер прогрева моделей при запуске
        self.boot_banner = QWidget()
        self.boot_banner.setObjectName("cardPanel")
        self.boot_banner.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.boot_banner.setStyleSheet(
            "QWidget#cardPanel { background-color: #171525; border: 1px solid #FF2E88; border-radius: 8px; }"
        )
        bb_layout = QVBoxLayout(self.boot_banner)
        bb_layout.setContentsMargins(10, 7, 10, 7)
        bb_layout.setSpacing(5)
        self.lbl_boot_status = QLabel("Инициализация GTA VI SubVoice...")
        self.lbl_boot_status.setStyleSheet("border: none; background: transparent; font-weight: 600; font-size: 11px;")
        bb_layout.addWidget(self.lbl_boot_status)
        self.boot_progress = QProgressBar()
        self.boot_progress.setRange(0, 0)
        self.boot_progress.setFixedHeight(5)
        self.boot_progress.setTextVisible(False)
        bb_layout.addWidget(self.boot_progress)
        root.addWidget(self.boot_banner)

        # Вкладки (панель вкладок скрыта в базовом режиме и видна в экспертном)
        self.tabs = QTabWidget()
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabs.setIconSize(QSize(13, 13))
        root.addWidget(self.tabs, 1)

        self._build_home_tab()
        self._build_characters_and_dict_tab()
        self._build_settings_tab()

    def _build_home_tab(self) -> None:
        # Область прокрутки гарантирует, что элементы управления НИКОГДА не сожмутся, не обрежутся и не исчезнут при любом размере окна и DPI
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # 1. Основные кнопки управления
        self.btn_start_stop = QPushButton("  Старт озвучки (F8)")
        self.btn_start_stop.setObjectName("primaryStart")
        self.btn_start_stop.setIcon(svg_icon("play", "#FFFFFF", 14))
        self.btn_start_stop.setIconSize(QSize(14, 14))
        self.btn_start_stop.setMinimumHeight(36)
        self.btn_start_stop.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.btn_start_stop)

        sub_actions = QHBoxLayout()
        sub_actions.setSpacing(8)
        self.btn_select_roi = QPushButton("  Область (F9)")
        self.btn_select_roi.setObjectName("accentBtn")
        self.btn_select_roi.setIcon(svg_icon("scan", "#FF7AA8", 14))
        self.btn_select_roi.setIconSize(QSize(14, 14))
        self.btn_select_roi.setMinimumHeight(30)

        self.btn_test_voice = QPushButton("  Тест голоса")
        self.btn_test_voice.setIcon(svg_icon("volume-2", "#00D9B5", 14))
        self.btn_test_voice.setIconSize(QSize(14, 14))
        self.btn_test_voice.setMinimumHeight(30)

        sub_actions.addWidget(self.btn_select_roi, 3)
        sub_actions.addWidget(self.btn_test_voice, 2)
        layout.addLayout(sub_actions)

        # 1b. Целевой процесс игры / окна (всегда доступен даже в базовом режиме)
        proc_group = QGroupBox("Процесс игры / окна")
        proc_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        pg_lay = QHBoxLayout(proc_group)
        pg_lay.setContentsMargins(10, 8, 10, 8)
        pg_lay.setSpacing(6)
        self.combo_target_proc = QComboBox()
        self.combo_target_proc.setEditable(True)
        self.combo_target_proc.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.combo_target_proc.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo_target_proc.setMinimumContentsLength(16)
        self.combo_target_proc.setMinimumHeight(28)
        self.combo_target_proc.addItem(NO_TARGET_PROCESS_LABEL, "")
        for exe, label in PRESET_GAME_PROCESSES:
            self.combo_target_proc.addItem(label, exe)
        self.btn_pick_duck_proc = QPushButton(" Выбрать...")
        self.btn_pick_duck_proc.setIcon(svg_icon("headphones", "#E3DFF7", 13))
        self.btn_pick_duck_proc.setIconSize(QSize(13, 13))
        self.btn_pick_duck_proc.setMinimumHeight(28)
        self.btn_pick_duck_proc.setToolTip("Выбрать окно игры или приложение из списка запущенных программ")
        pg_lay.addWidget(self.combo_target_proc, 1)
        pg_lay.addWidget(self.btn_pick_duck_proc)
        layout.addWidget(proc_group)

        # 2. Режим озвучки (3 компактные тактильные кнопки-карточки)
        voice_group = QGroupBox("Озвучка (Silero v5.6)")
        voice_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        vg_layout = QVBoxLayout(voice_group)
        vg_layout.setContentsMargins(10, 10, 10, 8)
        vg_layout.setSpacing(7)

        cards_row = QHBoxLayout()
        cards_row.setSpacing(6)
        self.btn_mode_single = QPushButton(" 1 голос")
        self.btn_mode_single.setIcon(svg_icon("user", "#E5E2F5", 13))
        self.btn_mode_dual = QPushButton(" 2 голоса (М/Ж)")
        self.btn_mode_dual.setIcon(svg_icon("users", "#E5E2F5", 13))
        self.btn_mode_multi = QPushButton(" Многоголосый")
        self.btn_mode_multi.setIcon(svg_icon("audio-lines", "#E5E2F5", 13))
        self.mode_btn_group = QButtonGroup(self)
        self.mode_btn_group.setExclusive(True)
        for btn in (self.btn_mode_single, self.btn_mode_dual, self.btn_mode_multi):
            btn.setObjectName("modeCard")
            btn.setCheckable(True)
            btn.setIconSize(QSize(13, 13))
            btn.setMinimumHeight(30)
            self.mode_btn_group.addButton(btn)
            cards_row.addWidget(btn)
        vg_layout.addLayout(cards_row)

        # Подпанель для режима «1 голос»
        self.panel_single = QWidget()
        self.panel_single.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ps_lay = QHBoxLayout(self.panel_single)
        ps_lay.setContentsMargins(0, 1, 0, 0)
        ps_lay.setSpacing(8)
        ps_lay.addWidget(QLabel("Диктор:"))
        self.combo_single_voice = QComboBox()
        self.combo_single_voice.setMinimumHeight(28)
        for sp in ALL_SILERO_SPEAKERS:
            self.combo_single_voice.addItem(speaker_display_name(sp), sp)
        ps_lay.addWidget(self.combo_single_voice, 1)
        vg_layout.addWidget(self.panel_single)

        # Подпанель для режима «2 голоса (М/Ж)»
        self.panel_dual = QWidget()
        self.panel_dual.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        pd_lay = QHBoxLayout(self.panel_dual)
        pd_lay.setContentsMargins(0, 1, 0, 0)
        pd_lay.setSpacing(6)
        pd_lay.addWidget(QLabel("М:"))
        self.combo_dual_male = QComboBox()
        self.combo_dual_male.setMinimumHeight(28)
        for sp in MALE_SPEAKERS + FEMALE_SPEAKERS:
            self.combo_dual_male.addItem(speaker_display_name(sp, short=True), sp)
        pd_lay.addWidget(self.combo_dual_male, 1)

        pd_lay.addWidget(QLabel("Ж:"))
        self.combo_dual_female = QComboBox()
        self.combo_dual_female.setMinimumHeight(28)
        for sp in FEMALE_SPEAKERS + MALE_SPEAKERS:
            self.combo_dual_female.addItem(speaker_display_name(sp, short=True), sp)
        pd_lay.addWidget(self.combo_dual_female, 1)
        vg_layout.addWidget(self.panel_dual)

        # Подпанель для многоголосого режима
        self.lbl_multi_hint = QLabel("До 5 нейронных голосов Silero 5.6 (Евгений и Ксения — приоритет)")
        self.lbl_multi_hint.setStyleSheet("color: #9A96B0; font-size: 11px; padding: 2px;")
        self.lbl_multi_hint.setMinimumHeight(22)
        vg_layout.addWidget(self.lbl_multi_hint)
        layout.addWidget(voice_group)

        # 3. Секция звука и нативного нейронного темпа (фиксированная высота, чтобы строки не сжимались)
        audio_group = QGroupBox("Звук и темп")
        audio_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        ag_layout = QVBoxLayout(audio_group)
        ag_layout.setContentsMargins(10, 10, 10, 8)
        ag_layout.setSpacing(6)

        speed_row = QHBoxLayout()
        speed_row.setSpacing(6)
        speed_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        lbl_sp_cap = QLabel("Темп речи:")
        lbl_sp_cap.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        lbl_sp_cap.setStyleSheet("padding-bottom: 1px;")
        lbl_sp_cap.setMinimumHeight(22)
        speed_row.addWidget(lbl_sp_cap, 0, Qt.AlignmentFlag.AlignVCenter)
        self.lbl_speed_value = QLabel("Обычный (1.0x)")
        self.lbl_speed_value.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.lbl_speed_value.setMinimumHeight(22)
        self.lbl_speed_value.setStyleSheet("font-size: 12px; font-weight: 700; color: #FF2E88; padding-bottom: 1px;")
        speed_row.addWidget(self.lbl_speed_value, 0, Qt.AlignmentFlag.AlignVCenter)
        speed_row.addStretch()
        self.chk_adaptive_tempo = ViceToggleSwitch("Автоускорение темпа", accent="sunset")
        self.chk_adaptive_tempo.setToolTip(
            "Если включено, задаётся темп medium (ручной выбор отключается), при необходимости речь ускоряется максимум до fast (1.2x)"
        )
        speed_row.addWidget(self.chk_adaptive_tempo, 0, Qt.AlignmentFlag.AlignVCenter)
        ag_layout.addLayout(speed_row)

        speed_cards = QHBoxLayout()
        speed_cards.setSpacing(6)
        self.btn_speed_medium = QPushButton("Обычный (1.0x)")
        self.btn_speed_fast = QPushButton("Быстрый (1.2x)")
        self.btn_speed_xfast = QPushButton("Очень быстрый (1.5x)")
        self.speed_btn_group = QButtonGroup(self)
        self.speed_btn_group.setExclusive(True)
        for btn in (self.btn_speed_medium, self.btn_speed_fast, self.btn_speed_xfast):
            btn.setObjectName("modeCard")
            btn.setCheckable(True)
            btn.setMinimumHeight(28)
            self.speed_btn_group.addButton(btn)
            speed_cards.addWidget(btn)
        ag_layout.addLayout(speed_cards)

        vol_row = QHBoxLayout()
        vol_row.setSpacing(6)
        vol_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        lbl_vol_cap = QLabel("Громкость диктора:")
        lbl_vol_cap.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        lbl_vol_cap.setMinimumHeight(20)
        vol_row.addWidget(lbl_vol_cap, 0, Qt.AlignmentFlag.AlignVCenter)
        vol_row.addStretch()
        self.lbl_volume_value = QLabel("100%")
        self.lbl_volume_value.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        self.lbl_volume_value.setMinimumHeight(20)
        self.lbl_volume_value.setStyleSheet("font-size: 12.5px; font-weight: 700; color: #00D9B5;")
        vol_row.addWidget(self.lbl_volume_value, 0, Qt.AlignmentFlag.AlignVCenter)
        ag_layout.addLayout(vol_row)

        self.slider_volume = TactileSlider(Qt.Orientation.Horizontal, accent="mint")
        self.slider_volume.setRange(0, 200)
        self.slider_volume.setSingleStep(5)
        ag_layout.addWidget(self.slider_volume)

        self.chk_ducking = ViceToggleSwitch("Приглушать звук игры во время речи (Auto-Ducking)", accent="mint")
        ag_layout.addWidget(self.chk_ducking)

        self.chk_mute_window = ViceToggleSwitch("Отключать звук окна, пока идёт чтение", accent="sunset")
        self.chk_mute_window.setToolTip(
            "Если включено, звук окна с субтитрами полностью отключается на всё время, пока идёт чтение (от «Старт» до «Стоп»)"
        )
        ag_layout.addWidget(self.chk_mute_window)

        # Настройки внутри вкладки «Главная», доступные только в экспертном режиме (устройство вывода WASAPI)
        self.expert_home_audio = QWidget()
        self.expert_home_audio.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        eha_lay = QVBoxLayout(self.expert_home_audio)
        eha_lay.setContentsMargins(0, 4, 0, 0)
        eha_lay.setSpacing(6)
        dev_row = QHBoxLayout()
        dev_row.setSpacing(6)
        dev_row.addWidget(QLabel("Вывод WASAPI:"))
        self.combo_device = QComboBox()
        self.combo_device.setMinimumHeight(28)
        self.combo_device.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        dev_row.addWidget(self.combo_device, 1)
        self.btn_refresh_devices = QPushButton()
        self.btn_refresh_devices.setIcon(svg_icon("refresh-cw", "#E3DFF7", 13))
        self.btn_refresh_devices.setIconSize(QSize(13, 13))
        self.btn_refresh_devices.setFixedSize(30, 28)
        self.btn_refresh_devices.setToolTip("Обновить список аудиоустройств")
        dev_row.addWidget(self.btn_refresh_devices)
        eha_lay.addLayout(dev_row)
        ag_layout.addWidget(self.expert_home_audio)
        layout.addWidget(audio_group)

        # 4a. Компактная строка последней фразы для базового режима
        self.basic_last_phrase_box = QWidget()
        self.basic_last_phrase_box.setObjectName("lastPhraseBox")
        self.basic_last_phrase_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lp_lay = QVBoxLayout(self.basic_last_phrase_box)
        lp_lay.setContentsMargins(10, 7, 10, 7)
        lp_lay.setSpacing(2)
        lbl_lp_cap = QLabel("Последняя фраза")
        lbl_lp_cap.setStyleSheet("color: #9893B0; font-size: 10.5px; font-weight: 600; border: none; background: transparent;")
        self.lbl_last_phrase = QLabel("Ожидание субтитров...")
        self.lbl_last_phrase.setWordWrap(True)
        self.lbl_last_phrase.setMinimumHeight(18)
        self.lbl_last_phrase.setStyleSheet("color: #F2F1FA; font-weight: 600; border: none; background: transparent;")
        lp_lay.addWidget(lbl_lp_cap)
        lp_lay.addWidget(self.lbl_last_phrase)
        layout.addWidget(self.basic_last_phrase_box)

        # 4b. Живой технический журнал для экспертного режима (последние 20 событий)
        self.expert_log_group = QGroupBox("Живой технический лог (последние 20 событий)")
        self.expert_log_group.setMinimumHeight(130)
        self.expert_log_group.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lg_lay = QVBoxLayout(self.expert_log_group)
        lg_lay.setContentsMargins(8, 10, 8, 8)
        self.log_list = QListWidget()
        self.log_list.setMinimumHeight(90)
        lg_lay.addWidget(self.log_list)
        layout.addWidget(self.expert_log_group, 1)

        layout.addStretch(0)
        scroll.setWidget(tab)
        self.tabs.addTab(scroll, svg_icon("home", "#C7C2DC", 13), "Главная")

    def _build_characters_and_dict_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Sub-tabs: Персонажи / Словарь ударений
        self.char_subtabs = QTabWidget()
        self.char_subtabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.char_subtabs.setIconSize(QSize(14, 14))
        layout.addWidget(self.char_subtabs, 1)

        # --- Подвкладка 1: Персонажи ---
        char_page = QWidget()
        cp_lay = QVBoxLayout(char_page)
        cp_lay.setContentsMargins(12, 12, 12, 12)
        cp_lay.setSpacing(10)

        top_btns = QHBoxLayout()
        self.btn_add_char = QPushButton(" Добавить персонажа")
        self.btn_add_char.setObjectName("accentBtn")
        self.btn_add_char.setIcon(svg_icon("plus", "#FFFFFF", 15))
        self.btn_add_char.setIconSize(QSize(15, 15))
        self.btn_clear_chars = QPushButton(" Очистить базу")
        self.btn_clear_chars.setObjectName("dangerBtn")
        self.btn_clear_chars.setIcon(svg_icon("trash-2", "#FFB6C9", 15))
        self.btn_clear_chars.setIconSize(QSize(15, 15))
        top_btns.addWidget(self.btn_add_char)
        top_btns.addStretch()
        top_btns.addWidget(self.btn_clear_chars)
        cp_lay.addLayout(top_btns)

        # Аккуратная таблица из 5 колонок без обрезанных заголовков
        self.char_table = QTableWidget(0, 5)
        self.char_table.setHorizontalHeaderLabels(
            ["Имя", "Пол (М/Ж)", "Чистый голос", "Тест", "Удалить"]
        )
        self.char_table.setAlternatingRowColors(True)
        self.char_table.verticalHeader().setVisible(False)
        self.char_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        hdr = self.char_table.horizontalHeader()
        hdr.setMinimumSectionSize(76)
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        cp_lay.addWidget(self.char_table, 1)

        hint = QLabel("Ручной выбор голоса закрепляет диктора за персонажем (locked: true) и сразу сохраняется в data/characters.json.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #9A96B0; font-size: 11px;")
        cp_lay.addWidget(hint)
        self.char_subtabs.addTab(char_page, svg_icon("users", "#C7C2DC", 14), "Персонажи")

        # --- Подвкладка 2: Словарь ударений (stress_dict.json) ---
        dict_page = QWidget()
        dp_lay = QVBoxLayout(dict_page)
        dp_lay.setContentsMargins(12, 12, 12, 12)
        dp_lay.setSpacing(10)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.edit_stress_word = QLineEdit()
        self.edit_stress_word.setPlaceholderText("Слово (напр.: Вайс-Сити)")
        self.edit_stress_repl = QLineEdit()
        self.edit_stress_repl.setPlaceholderText("Произношение с + (напр.: В+айс-С+ити)")
        self.btn_add_stress = QPushButton(" Добавить")
        self.btn_add_stress.setObjectName("accentBtn")
        self.btn_add_stress.setIcon(svg_icon("plus", "#FFFFFF", 15))
        self.btn_add_stress.setIconSize(QSize(15, 15))
        add_row.addWidget(self.edit_stress_word, 1)
        add_row.addWidget(self.edit_stress_repl, 1)
        add_row.addWidget(self.btn_add_stress)
        dp_lay.addLayout(add_row)

        self.stress_table = QTableWidget(0, 4)
        self.stress_table.setHorizontalHeaderLabels(["Слово", "Произношение (+)", "Тест слова", "Удалить"])
        self.stress_table.setAlternatingRowColors(True)
        self.stress_table.verticalHeader().setVisible(False)
        shdr = self.stress_table.horizontalHeader()
        shdr.setMinimumSectionSize(84)
        shdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        shdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        shdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        shdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        dp_lay.addWidget(self.stress_table, 1)

        self.char_subtabs.addTab(dict_page, svg_icon("book-open", "#C7C2DC", 14), "Словарь ударений (stress_dict.json)")
        self.tabs.addTab(tab, svg_icon("users", "#C7C2DC", 15), "Персонажи и словарь")

    def _build_settings_tab(self) -> None:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # 1. Синтезатор речи Silero TTS 5.6
        model_group = QGroupBox("Нейронный синтез речи (Silero TTS 5.6 CPU)")
        mg_lay = QVBoxLayout(model_group)
        path_row = QHBoxLayout()
        self.edit_model_path = QLineEdit()
        self.edit_model_path.setPlaceholderText("models/v5_6_ru.pt (автоопределение)")
        self.btn_browse_model = QPushButton("Обзор...")
        self.btn_autodetect_model = QPushButton("Авто")
        path_row.addWidget(self.edit_model_path, 1)
        path_row.addWidget(self.btn_browse_model)
        path_row.addWidget(self.btn_autodetect_model)
        mg_lay.addLayout(path_row)

        threads_row = QHBoxLayout()
        threads_row.addWidget(QLabel("Потоки PyTorch CPU:"))
        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, 16)
        threads_row.addWidget(self.spin_threads)
        threads_row.addStretch()
        self.lbl_ram = QLabel("RAM: — МБ")
        self.lbl_ram.setStyleSheet("color: #00E5C3; font-weight: 700;")
        threads_row.addWidget(self.lbl_ram)
        mg_lay.addLayout(threads_row)
        layout.addWidget(model_group)

        # 1b. Связь со смартфоном по локальному Wi-Fi (быстрые правки ударений, слово-замен и персонажей)
        mobile_group = QGroupBox("Связь со смартфоном в одной Wi-Fi сети (ударения и персонажи)")
        mob_lay = QVBoxLayout(mobile_group)
        mob_lay.setSpacing(8)

        mob_top = QHBoxLayout()
        mob_top.setSpacing(8)
        self.chk_mobile_sync = ViceToggleSwitch(
            "Включить веб-пульт для смартфона по Wi-Fi",
            accent="mint",
        )
        self.lbl_mobile_status = QLabel("Выключен")
        self.lbl_mobile_status.setStyleSheet("color: #9A96B0; font-size: 11px; font-weight: 700;")
        mob_top.addWidget(self.chk_mobile_sync, 1)
        mob_top.addWidget(self.lbl_mobile_status)
        mob_lay.addLayout(mob_top)

        self.mobile_details_box = QWidget()
        md_lay = QHBoxLayout(self.mobile_details_box)
        md_lay.setContentsMargins(0, 2, 0, 0)
        md_lay.setSpacing(12)

        # Слева: чёткий QR-код на белой подложке для быстрого сканирования камерой
        qr_col = QVBoxLayout()
        qr_col.setSpacing(4)
        self.lbl_mobile_qr = QLabel()
        self.lbl_mobile_qr.setFixedSize(164, 164)
        self.lbl_mobile_qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_mobile_qr.setStyleSheet("background: #FFFFFF; border-radius: 10px; padding: 4px;")
        qr_cap = QLabel("Сканируйте камерой")
        qr_cap.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr_cap.setStyleSheet("color: #9A96B0; font-size: 10px;")
        qr_col.addWidget(self.lbl_mobile_qr)
        qr_col.addWidget(qr_cap)
        qr_col.addStretch(1)
        md_lay.addLayout(qr_col)

        # Справа: сеть, порт, код доступа, URL и кнопки управления
        info_col = QVBoxLayout()
        info_col.setSpacing(6)

        net_row = QHBoxLayout()
        net_row.setSpacing(6)
        net_row.addWidget(QLabel("IP:"))
        self.combo_mobile_ip = QComboBox()
        self.combo_mobile_ip.setMinimumHeight(26)
        self.btn_mobile_refresh_ip = QPushButton()
        self.btn_mobile_refresh_ip.setIcon(svg_icon("refresh-cw", "#E3DFF7", 13))
        self.btn_mobile_refresh_ip.setIconSize(QSize(13, 13))
        self.btn_mobile_refresh_ip.setToolTip("Обновить список сетевых адресов Wi-Fi / LAN")
        net_row.addWidget(self.combo_mobile_ip, 1)
        net_row.addWidget(self.btn_mobile_refresh_ip)
        net_row.addWidget(QLabel("Порт:"))
        self.spin_mobile_port = QSpinBox()
        self.spin_mobile_port.setRange(1024, 65535)
        self.spin_mobile_port.setMinimumWidth(72)
        net_row.addWidget(self.spin_mobile_port)
        info_col.addLayout(net_row)

        tok_row = QHBoxLayout()
        tok_row.setSpacing(6)
        tok_row.addWidget(QLabel("Код доступа:"))
        self.lbl_mobile_token = QLabel("—")
        self.lbl_mobile_token.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_mobile_token.setStyleSheet(
            "color: #00E5C3; font-weight: 800; font-size: 12px; letter-spacing: 1px;"
        )
        self.btn_mobile_regen_token = QPushButton(" Новый код")
        self.btn_mobile_regen_token.setIcon(svg_icon("refresh-cw", "#E3DFF7", 12))
        self.btn_mobile_regen_token.setIconSize(QSize(12, 12))
        tok_row.addWidget(self.lbl_mobile_token)
        tok_row.addStretch(1)
        tok_row.addWidget(self.btn_mobile_regen_token)
        info_col.addLayout(tok_row)

        url_row = QHBoxLayout()
        url_row.setSpacing(6)
        self.edit_mobile_url = QLineEdit()
        self.edit_mobile_url.setReadOnly(True)
        self.btn_mobile_copy = QPushButton(" Копировать")
        self.btn_mobile_copy.setIcon(svg_icon("copy", "#00D9B5", 12))
        self.btn_mobile_copy.setIconSize(QSize(12, 12))
        url_row.addWidget(self.edit_mobile_url, 1)
        url_row.addWidget(self.btn_mobile_copy)
        info_col.addLayout(url_row)

        act_row = QHBoxLayout()
        act_row.setSpacing(6)
        self.btn_mobile_open = QPushButton(" Открыть в браузере")
        self.btn_mobile_open.setIcon(svg_icon("external-link", "#E3DFF7", 12))
        self.btn_mobile_open.setIconSize(QSize(12, 12))
        self.btn_mobile_firewall = QPushButton(" Разрешить порт в Firewall")
        self.btn_mobile_firewall.setIcon(svg_icon("shield-check", "#00D9B5", 12))
        self.btn_mobile_firewall.setIconSize(QSize(12, 12))
        self.btn_mobile_firewall.setToolTip("Добавить правило для порта в брандмауэр Windows, если телефон не открывает ссылку")
        act_row.addWidget(self.btn_mobile_open)
        act_row.addWidget(self.btn_mobile_firewall)
        act_row.addStretch(1)
        info_col.addLayout(act_row)

        mob_hint = QLabel(
            "Телефон и ПК должны быть в одной Wi-Fi сети. Правки ударений, слово-замен, пола и голоса применяются сразу без перезапуска."
        )
        mob_hint.setWordWrap(True)
        mob_hint.setStyleSheet("color: #9A96B0; font-size: 11px;")
        info_col.addWidget(mob_hint)

        self.lbl_mobile_peer = QLabel("Подключения по сети: 0 (ожидание телефона)")
        self.lbl_mobile_peer.setStyleSheet("color: #B4AFC9; font-size: 11px;")
        info_col.addWidget(self.lbl_mobile_peer)

        md_lay.addLayout(info_col, 1)
        mob_lay.addWidget(self.mobile_details_box)
        self.mobile_details_box.setVisible(False)
        layout.addWidget(mobile_group)

        # 2. Автоприглушение звука и отслеживание активности целевого окна
        duck_group = QGroupBox("Приглушение звука и фильтр активности окна")
        df = QFormLayout(duck_group)
        df.setSpacing(6)

        duck_depth_row = QHBoxLayout()
        duck_depth_row.setSpacing(6)
        self.spin_duck_depth = QSpinBox()
        self.spin_duck_depth.setRange(10, 95)
        self.spin_duck_depth.setSingleStep(5)
        self.spin_duck_depth.setSuffix(" %")
        self.btn_test_duck = QPushButton(" Проверить")
        self.btn_test_duck.setIcon(svg_icon("volume-2", "#00D9B5", 13))
        self.btn_test_duck.setIconSize(QSize(13, 13))
        self.btn_test_duck.setToolTip("Проверить, найдена ли аудиосессия выбранного процесса и работает ли приглушение")
        duck_depth_row.addWidget(self.spin_duck_depth, 1)
        duck_depth_row.addWidget(self.btn_test_duck)
        df.addRow("Глубина приглушения:", duck_depth_row)

        self.chk_only_when_active = ViceToggleSwitch(
            "Искать и озвучивать субтитры только когда окно игры активно",
            accent="sunset",
        )
        df.addRow(self.chk_only_when_active)
        layout.addWidget(duck_group)

        # 3. Область захвата (ROI) и распознавание текста (OCR)
        ocr_group = QGroupBox("Захват ROI и OCR (RapidOCR)")
        form = QFormLayout(ocr_group)
        form.setSpacing(6)

        self.spin_fps = QSpinBox()
        self.spin_fps.setRange(2, 30)
        self.spin_fps.setSuffix(" FPS")
        form.addRow("Частота сканирования ROI:", self.spin_fps)

        self.spin_debounce = QSpinBox()
        self.spin_debounce.setRange(0, 400)
        self.spin_debounce.setSingleStep(10)
        self.spin_debounce.setSuffix(" мс")
        form.addRow("Задержка стабилизации (debounce):", self.spin_debounce)

        self.spin_caps = QDoubleSpinBox()
        self.spin_caps.setRange(0.40, 1.00)
        self.spin_caps.setSingleStep(0.02)
        self.spin_caps.setDecimals(2)
        form.addRow("Порог фильтра ЗАГЛАВНЫХ:", self.spin_caps)

        self.chk_punct = ViceToggleSwitch("Восстанавливать финальную пунктуацию (?, !, .)", accent="mint")
        form.addRow(self.chk_punct)

        self.chk_font_clf = ViceToggleSwitch(
            "Нейро-фильтр шрифта субтитров (ONNX, отсекает карту/UI)",
            accent="mint",
        )
        form.addRow(self.chk_font_clf)

        self.chk_always_on_top = ViceToggleSwitch(
            "Окно программы и выделение области поверх всех окон",
            accent="mint",
        )
        form.addRow(self.chk_always_on_top)

        self.chk_bind_xbox = ViceToggleSwitch(
            "Привязывать координаты области (%) к окну выбранного процесса",
            accent="mint",
        )
        form.addRow(self.chk_bind_xbox)

        self.chk_announce_status = ViceToggleSwitch(
            "Озвучивать голосом Старт / Стоп чтения субтитров",
            accent="sunset",
        )
        form.addRow(self.chk_announce_status)

        dbg_row = QHBoxLayout()
        dbg_row.setSpacing(8)
        self.chk_debug_log = ViceToggleSwitch(
            "Лог отладки (текст + скрины озвученных субтитров)",
            accent="mint",
        )
        self.chk_debug_log.setToolTip(
            "Сохраняет events.jsonl и JPEG-скриншоты только для реально озвученных фраз в data/debug_logs/"
        )
        self.btn_open_debug_logs = QPushButton(" Папка логов")
        self.btn_open_debug_logs.setIcon(svg_icon("folder-open", "#E3DFF7", 13))
        self.btn_open_debug_logs.setIconSize(QSize(13, 13))
        self.btn_open_debug_logs.setToolTip("Открыть папку data/debug_logs/ с записями отладки")
        dbg_row.addWidget(self.chk_debug_log, 1)
        dbg_row.addWidget(self.btn_open_debug_logs)
        form.addRow(dbg_row)
        layout.addWidget(ocr_group)

        # 4. Компоненты и модели (полная высота без тесной полосы прокрутки)
        comp_group = QGroupBox("Компоненты и модели (автономность и целостность)")
        cg_lay = QVBoxLayout(comp_group)
        cg_lay.setSpacing(8)

        self.comp_table = QTableWidget(0, 4)
        self.comp_table.setHorizontalHeaderLabels(["Компонент", "Статус", "Размер", "Путь"])
        self.comp_table.verticalHeader().setVisible(False)
        self.comp_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.comp_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.comp_table.setAlternatingRowColors(True)
        self.comp_table.setMinimumHeight(94)
        chdr = self.comp_table.horizontalHeader()
        chdr.setFixedHeight(28)
        chdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        chdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        chdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        chdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        cg_lay.addWidget(self.comp_table)

        comp_btns = QHBoxLayout()
        comp_btns.setSpacing(6)
        self.btn_verify_models = QPushButton(" Проверить")
        self.btn_verify_models.setIcon(svg_icon("shield-check", "#00D9B5", 13))
        self.btn_verify_models.setIconSize(QSize(13, 13))
        self.btn_manual_model = QPushButton(" Файл Silero (.pt) вручную")
        self.btn_manual_model.setIcon(svg_icon("folder-open", "#E3DFF7", 13))
        self.btn_manual_model.setIconSize(QSize(13, 13))
        comp_btns.addWidget(self.btn_verify_models)
        comp_btns.addWidget(self.btn_manual_model)
        comp_btns.addStretch(1)
        cg_lay.addLayout(comp_btns)
        layout.addWidget(comp_group)

        # 5. Горячие клавиши
        hk_group = QGroupBox("Глобальные горячие клавиши")
        hk_form = QFormLayout(hk_group)
        hk_form.setSpacing(6)
        self.edit_hk_pause = QLineEdit()
        self.edit_hk_roi = QLineEdit()
        self.edit_hk_interrupt = QLineEdit()
        hk_form.addRow("Старт / Стоп озвучки:", self.edit_hk_pause)
        hk_form.addRow("Выбрать область ROI:", self.edit_hk_roi)
        hk_form.addRow("Прервать текущую фразу:", self.edit_hk_interrupt)
        layout.addWidget(hk_group)

        # 6. Экспорт/импорт профиля и сброс настроек (все изменения сохраняются автоматически)
        prof_group = QGroupBox("Профиль и сброс (все настройки сохраняются автоматически)")
        pg_lay = QHBoxLayout(prof_group)
        pg_lay.setSpacing(6)
        self.btn_export_profile = QPushButton(" Экспорт...")
        self.btn_export_profile.setIcon(svg_icon("upload", "#00D9B5", 13))
        self.btn_export_profile.setIconSize(QSize(13, 13))
        self.btn_export_profile.setToolTip("Сохранить настройки, параметры моделей, ударения и персонажей в один файл")

        self.btn_import_profile = QPushButton(" Импорт...")
        self.btn_import_profile.setIcon(svg_icon("folder-open", "#E3DFF7", 13))
        self.btn_import_profile.setIconSize(QSize(13, 13))
        self.btn_import_profile.setToolTip("Загрузить настройки, параметры моделей, ударения и персонажей из файла")

        self.btn_reset_defaults = QPushButton(" Сбросить по умолчанию")
        self.btn_reset_defaults.setObjectName("dangerBtn")
        self.btn_reset_defaults.setIcon(svg_icon("rotate-ccw", "#FFB6C9", 13))
        self.btn_reset_defaults.setIconSize(QSize(13, 13))

        pg_lay.addWidget(self.btn_export_profile)
        pg_lay.addWidget(self.btn_import_profile)
        pg_lay.addStretch()
        pg_lay.addWidget(self.btn_reset_defaults)
        layout.addWidget(prof_group)

        # 7. О программе и обновления
        about_group = QGroupBox("Обновления и версия программы")
        ab_lay = QHBoxLayout(about_group)
        ab_lay.setSpacing(10)

        ver_col = QVBoxLayout()
        ver_col.setSpacing(2)
        self.lbl_app_version = QLabel(f"Текущая версия: v{APP_VERSION}")
        self.lbl_app_version.setStyleSheet("font-weight: 700; color: #FFFFFF; font-size: 12px;")
        self.lbl_update_status = QLabel("Автопроверка обновлений с GitHub при запуске включена")
        self.lbl_update_status.setStyleSheet("color: #9A96B0; font-size: 11px;")
        ver_col.addWidget(self.lbl_app_version)
        ver_col.addWidget(self.lbl_update_status)
        ab_lay.addLayout(ver_col, 1)

        self.btn_check_updates = QPushButton(" Проверить обновления")
        self.btn_check_updates.setIcon(svg_icon("refresh-cw", "#00D9B5", 13))
        self.btn_check_updates.setIconSize(QSize(13, 13))
        self.btn_check_updates.setMinimumHeight(30)
        ab_lay.addWidget(self.btn_check_updates)
        layout.addWidget(about_group)

        layout.addStretch()
        scroll.setWidget(container)
        self.tabs.addTab(scroll, svg_icon("settings", "#C7C2DC", 13), "Настройки")

    # ------------------------------------------------------------------ Сигналы
    def _bind_signals(self) -> None:
        self.btn_support.clicked.connect(self._on_open_support_dialog)
        self.btn_toggle_mode.clicked.connect(self._on_toggle_ui_mode)
        self.btn_start_stop.clicked.connect(self._toggle_start_stop)
        self.btn_select_roi.clicked.connect(self.open_roi_selector)
        self.btn_test_voice.clicked.connect(self._on_test_voice)
        self.btn_refresh_devices.clicked.connect(self._populate_audio_devices)

        self.btn_mode_single.clicked.connect(lambda: self._on_voice_mode_selected("single"))
        self.btn_mode_dual.clicked.connect(lambda: self._on_voice_mode_selected("dual_mj"))
        self.btn_mode_multi.clicked.connect(lambda: self._on_voice_mode_selected("multi"))

        self.combo_single_voice.currentIndexChanged.connect(self._on_quick_setting_changed)
        self.combo_dual_male.currentIndexChanged.connect(self._on_quick_setting_changed)
        self.combo_dual_female.currentIndexChanged.connect(self._on_quick_setting_changed)

        self.btn_speed_medium.clicked.connect(lambda: self._on_speed_preset_selected("medium"))
        self.btn_speed_fast.clicked.connect(lambda: self._on_speed_preset_selected("fast"))
        self.btn_speed_xfast.clicked.connect(lambda: self._on_speed_preset_selected("x-fast"))
        self.slider_volume.valueChanged.connect(self._on_volume_changed)
        self.chk_adaptive_tempo.toggled.connect(self._on_quick_setting_changed)
        self.chk_ducking.toggled.connect(self._on_quick_setting_changed)
        self.chk_mute_window.toggled.connect(self._on_quick_setting_changed)
        self.combo_device.currentIndexChanged.connect(self._on_device_changed)

        self.btn_add_char.clicked.connect(self._on_add_character_dialog)
        self.btn_clear_chars.clicked.connect(self._on_clear_characters)
        self.btn_add_stress.clicked.connect(self._on_add_stress_entry)
        self.edit_stress_word.returnPressed.connect(self._on_add_stress_entry)
        self.edit_stress_repl.returnPressed.connect(self._on_add_stress_entry)
        self.stress_table.cellDoubleClicked.connect(self._on_stress_table_double_click)

        self.btn_browse_model.clicked.connect(self._on_browse_model)
        self.btn_autodetect_model.clicked.connect(self._on_autodetect_model)
        self.btn_pick_duck_proc.clicked.connect(self._on_pick_duck_process)
        self.btn_test_duck.clicked.connect(self._on_test_ducking)

        # Автосохранение и мгновенное применение каждого параметра настроек (без кнопки «Сохранить»)
        self.edit_model_path.editingFinished.connect(self._on_settings_auto_changed)
        self.spin_threads.valueChanged.connect(self._on_settings_auto_changed)
        self.combo_target_proc.currentIndexChanged.connect(self._on_target_process_changed)
        if self.combo_target_proc.lineEdit() is not None:
            self.combo_target_proc.lineEdit().editingFinished.connect(self._on_target_process_changed)
        self.spin_duck_depth.valueChanged.connect(self._on_settings_auto_changed)
        self.chk_only_when_active.toggled.connect(self._on_settings_auto_changed)
        self.spin_fps.valueChanged.connect(self._on_settings_auto_changed)
        self.spin_debounce.valueChanged.connect(self._on_settings_auto_changed)
        self.spin_caps.valueChanged.connect(self._on_settings_auto_changed)
        self.chk_punct.toggled.connect(self._on_settings_auto_changed)
        self.chk_font_clf.toggled.connect(self._on_settings_auto_changed)
        self.chk_always_on_top.toggled.connect(self._on_settings_auto_changed)
        self.chk_bind_xbox.toggled.connect(self._on_settings_auto_changed)
        self.chk_announce_status.toggled.connect(self._on_settings_auto_changed)
        self.chk_debug_log.toggled.connect(self._on_settings_auto_changed)
        self.btn_open_debug_logs.clicked.connect(self._on_open_debug_logs)
        self.edit_hk_pause.editingFinished.connect(self._on_settings_auto_changed)
        self.edit_hk_roi.editingFinished.connect(self._on_settings_auto_changed)
        self.edit_hk_interrupt.editingFinished.connect(self._on_settings_auto_changed)

        self.btn_export_profile.clicked.connect(self._on_export_profile)
        self.btn_import_profile.clicked.connect(self._on_import_profile)
        self.btn_reset_defaults.clicked.connect(self._on_reset_defaults)

        self.btn_verify_models.clicked.connect(self._on_verify_models)
        self.btn_manual_model.clicked.connect(self._on_manual_install_silero)
        self.btn_check_updates.clicked.connect(lambda: self._start_update_check(manual=True))

        # Связь со смартфоном (Wi-Fi пульт)
        self.chk_mobile_sync.toggled.connect(self._on_mobile_sync_toggled)
        self.spin_mobile_port.valueChanged.connect(self._on_mobile_port_changed)
        self.combo_mobile_ip.currentIndexChanged.connect(self._refresh_mobile_qr_and_url)
        self.btn_mobile_refresh_ip.clicked.connect(self._populate_mobile_ips)
        self.btn_mobile_regen_token.clicked.connect(self._on_mobile_regen_token)
        self.btn_mobile_copy.clicked.connect(self._on_mobile_copy_url)
        self.btn_mobile_open.clicked.connect(self._on_mobile_open_browser)
        self.btn_mobile_firewall.clicked.connect(self._on_mobile_open_firewall)
        self.mobile_stress_synced.connect(self._on_mobile_stress_synced)
        self.mobile_character_synced.connect(self._on_mobile_character_synced)
        self.mobile_client_activity.connect(self._on_mobile_client_activity)
        self.mobile_report_saved.connect(self._on_mobile_report_saved)
        self.mobile_settings_synced.connect(self._on_mobile_settings_synced)

        self.pipeline.status_changed.connect(self._on_status_changed)
        self.pipeline.loading_progress.connect(self._show_loading_banner)
        self.pipeline.log_event.connect(self._on_log_event)
        self.pipeline.last_phrase_changed.connect(self._on_last_phrase_changed)
        self.pipeline.character_updated.connect(lambda _name: self._refresh_characters_table())
        self.pipeline.model_ready.connect(self._on_model_ready)
        self.pipeline.error_occurred.connect(self._on_pipeline_error)

        self.hotkey_mgr.hotkey_triggered.connect(self._on_global_hotkey)

    # ------------------------------------------------------------------ Переключатель «Базовый / Эксперт»
    def _on_open_support_dialog(self) -> None:
        dlg = SupportDialog(parent=self)
        dlg.exec()

    def _on_toggle_ui_mode(self) -> None:
        new_mode = "expert" if self.cfg_mgr.config.ui_mode == "basic" else "basic"
        self._apply_ui_mode(new_mode, save=True)

    def _apply_ui_mode(self, mode: str, save: bool = True) -> None:
        is_expert = mode == "expert"
        self.cfg_mgr.config.ui_mode = "expert" if is_expert else "basic"
        if save:
            self.cfg_mgr.save()

        self.tabs.tabBar().setVisible(is_expert)
        self.tabs.setProperty("basicMode", not is_expert)
        self.tabs.style().unpolish(self.tabs)
        self.tabs.style().polish(self.tabs)
        if not is_expert:
            self.tabs.setCurrentIndex(0)

        self.expert_home_audio.setVisible(is_expert)
        self.expert_log_group.setVisible(is_expert)
        self.basic_last_phrase_box.setVisible(not is_expert)

        if is_expert:
            self.btn_toggle_mode.setText(" Базовый")
            self.btn_toggle_mode.setIcon(svg_icon("layout-grid", "#E3DFF7", 13))
            min_w, min_h = 580, 480
            target_w, target_h = 650, 690
        else:
            self.btn_toggle_mode.setText(" Эксперт")
            self.btn_toggle_mode.setIcon(svg_icon("sliders", "#E3DFF7", 13))
            min_w, min_h = 440, 400
            target_w, target_h = 468, 562

        # Ограничиваем размеры доступной рабочей областью текущего монитора под любой экран и DPI
        scr = self.screen() or QGuiApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            max_w = max(min_w, int(avail.width() * 0.90))
            max_h = max(min_h, int(avail.height() * 0.88))
            target_w = min(target_w, max_w)
            target_h = min(target_h, max_h)
            min_w = min(min_w, max_w)
            min_h = min(min_h, max_h)

        self.setMinimumSize(min_w, min_h)
        self.resize(target_w, target_h)

    # ------------------------------------------------------------------ Синхронизация конфигурации
    def _populate_audio_devices(self) -> None:
        current_saved = self.cfg_mgr.config.audio_device
        self.combo_device.blockSignals(True)
        self.combo_device.clear()
        self.combo_device.addItem("Системное устройство по умолчанию (WASAPI)", None)
        selected_idx = 0
        for dev in list_output_devices():
            label = f"{dev.name} [{dev.hostapi}]"
            self.combo_device.addItem(label, dev.name)
            if current_saved and dev.name == current_saved:
                selected_idx = self.combo_device.count() - 1
        self.combo_device.setCurrentIndex(selected_idx)
        self.combo_device.blockSignals(False)

    def _update_voice_mode_subpanels(self, mode: str) -> None:
        self.btn_mode_single.setChecked(mode == "single")
        self.btn_mode_dual.setChecked(mode == "dual_mj")
        self.btn_mode_multi.setChecked(mode == "multi")
        self.panel_single.setVisible(mode == "single")
        self.panel_dual.setVisible(mode == "dual_mj")
        self.lbl_multi_hint.setVisible(mode == "multi")

    def _set_target_process_combo(self, proc_name: str) -> None:
        proc = (proc_name or "").strip()
        self.combo_target_proc.blockSignals(True)
        if not proc:
            self.combo_target_proc.setCurrentIndex(0)
        else:
            idx = self.combo_target_proc.findData(proc)
            if idx < 0:
                # Регистронезависимый поиск среди предустановленных процессов
                for i in range(self.combo_target_proc.count()):
                    d = str(self.combo_target_proc.itemData(i) or "")
                    if d.lower() == proc.lower():
                        idx = i
                        break
            if idx >= 0:
                self.combo_target_proc.setCurrentIndex(idx)
            else:
                self.combo_target_proc.addItem(proc, proc)
                self.combo_target_proc.setCurrentIndex(self.combo_target_proc.count() - 1)
        if self.combo_target_proc.lineEdit() is not None:
            self.combo_target_proc.lineEdit().setCursorPosition(0)
        self.combo_target_proc.blockSignals(False)

    def _current_target_process(self) -> str:
        cur_txt = self.combo_target_proc.currentText().strip()
        if not cur_txt or cur_txt == NO_TARGET_PROCESS_LABEL:
            return ""
        idx = self.combo_target_proc.currentIndex()
        if idx >= 0 and cur_txt == self.combo_target_proc.itemText(idx):
            data = self.combo_target_proc.itemData(idx)
            return str(data or "").strip()
        # Поддержка ввода как имени exe, так и формата 'XboxPcApp.exe — описание'
        if " — " in cur_txt:
            cur_txt = cur_txt.split(" — ", 1)[0].strip()
        return "" if cur_txt == NO_TARGET_PROCESS_LABEL else cur_txt

    def _schedule_auto_save(self) -> None:
        self._save_timer.start()

    def _flush_auto_save(self) -> None:
        self.cfg_mgr.save()

    def _sync_ui_from_config(self) -> None:
        self._syncing_ui = True
        try:
            cfg = self.cfg_mgr.config
            self._update_voice_mode_subpanels(cfg.voice_mode)

            for cb, val in (
                (self.combo_single_voice, cfg.single_speaker),
                (self.combo_dual_male, cfg.dual_male_speaker),
                (self.combo_dual_female, cfg.dual_female_speaker),
            ):
                cb.blockSignals(True)
                idx = cb.findData(val)
                if idx >= 0:
                    cb.setCurrentIndex(idx)
                cb.blockSignals(False)

            self._update_speed_preset_buttons(cfg.speech_speed)

            self.slider_volume.blockSignals(True)
            self.slider_volume.setValue(cfg.volume)
            self.lbl_volume_value.setText(f"{cfg.volume}%")
            self.slider_volume.blockSignals(False)

            for chk, val in (
                (self.chk_adaptive_tempo, cfg.adaptive_tempo),
                (self.chk_ducking, cfg.ducking_enabled),
                (self.chk_mute_window, cfg.mute_target_window),
                (self.chk_only_when_active, cfg.only_when_target_active),
                (self.chk_punct, cfg.enable_punctuation),
                (self.chk_font_clf, cfg.font_classifier_enabled),
                (self.chk_always_on_top, cfg.always_on_top),
                (self.chk_bind_xbox, cfg.roi.bind_to_xbox),
                (self.chk_announce_status, cfg.announce_status),
                (self.chk_debug_log, cfg.debug_capture_log),
            ):
                chk.blockSignals(True)
                chk.setChecked(val)
                chk.blockSignals(False)

            self._sync_speed_controls_state()

            # Вкладка настроек
            self.edit_model_path.blockSignals(True)
            self.edit_model_path.setText(cfg.silero_model_path)
            self.edit_model_path.blockSignals(False)

            self.spin_threads.blockSignals(True)
            self.spin_threads.setValue(cfg.torch_num_threads)
            self.spin_threads.blockSignals(False)

            self._set_target_process_combo(cfg.target_process)

            self.spin_duck_depth.blockSignals(True)
            self.spin_duck_depth.setValue(cfg.ducking_depth_pct)
            self.spin_duck_depth.blockSignals(False)

            for spin, val in (
                (self.spin_fps, cfg.scan_fps),
                (self.spin_debounce, cfg.debounce_ms),
                (self.spin_caps, cfg.caps_ratio_threshold),
            ):
                spin.blockSignals(True)
                spin.setValue(val)
                spin.blockSignals(False)

            for ed, val in (
                (self.edit_hk_pause, cfg.hotkeys.toggle_pause),
                (self.edit_hk_roi, cfg.hotkeys.select_roi),
                (self.edit_hk_interrupt, cfg.hotkeys.interrupt),
            ):
                ed.blockSignals(True)
                ed.setText(val)
                ed.blockSignals(False)

            self.chk_mobile_sync.blockSignals(True)
            self.chk_mobile_sync.setChecked(cfg.mobile_sync_enabled)
            self.chk_mobile_sync.blockSignals(False)
            self.spin_mobile_port.blockSignals(True)
            self.spin_mobile_port.setValue(cfg.mobile_sync_port)
            self.spin_mobile_port.blockSignals(False)
            self._populate_mobile_ips()
            self._refresh_mobile_qr_and_url()
        finally:
            self._syncing_ui = False

    def _register_hotkeys(self) -> None:
        hk = self.cfg_mgr.config.hotkeys
        self.hotkey_mgr.register_all(
            {
                "toggle_pause": hk.toggle_pause,
                "select_roi": hk.select_roi,
                "interrupt": hk.interrupt,
            }
        )

    # ------------------------------------------------------------------ Действия
    def _prompt_select_target_process(self, reason: str = "start") -> bool:
        if not self.isVisible() or self.isMinimized():
            self.bring_to_front()
        prompt = (
            "Процесс игры / окна не выбран.\n"
            "Чтобы запустить озвучку, выберите окно или приложение из списка (или введите .exe вручную):"
            if reason == "start"
            else "Процесс игры / окна не выбран.\n"
            "Сначала выберите окно или приложение, в котором нужно выделить область субтитров:"
        )
        dlg = SelectProcessDialog(
            prompt_text=prompt,
            current_process=self._current_target_process(),
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            exe_name = dlg.get_selected_process()
            if exe_name:
                self._set_target_process_combo(exe_name)
                self._on_target_process_changed()
                return True
        return False

    def _on_target_process_changed(self) -> None:
        if self._syncing_ui:
            return
        cfg = self.cfg_mgr.config
        cfg.target_process = self._current_target_process()
        cfg.ducking_processes = [cfg.target_process] if cfg.target_process else []
        self.pipeline.apply_config(cfg)
        self._schedule_auto_save()

    def _toggle_start_stop(self, _checked: bool = False, *, from_hotkey: bool = False) -> None:
        if not self.pipeline.is_running:
            target_proc = self._current_target_process()
            if not target_proc:
                self._prompt_select_target_process(reason="start")
                return
            if self.cfg_mgr.config.target_process != target_proc:
                self._on_target_process_changed()
            _found_win, exited_fs = ensure_target_window_windowed(target_proc)
            if exited_fs:
                self._on_log_event(
                    PipelineLogEvent(
                        "WIN",
                        "info",
                        "—",
                        self.cfg_mgr.config.ocr_engine,
                        0.0,
                        f"Окно «{target_proc}» переведено в оконный режим",
                    )
                )

        running = self.pipeline.toggle_start_stop()
        if running:
            self.btn_start_stop.setText("  Остановить (F8)")
            self.btn_start_stop.setIcon(svg_icon("square", "#2CF0CE", 13))
            self.btn_start_stop.setObjectName("primaryStop")
            self.header_bar.set_live_shimmer(True)
            self.tray.set_active(True, "Слушаю субтитры")
            if from_hotkey and self.isVisible():
                self.hide()
                self._refresh_own_hwnds()
        else:
            self.btn_start_stop.setText("  Старт озвучки (F8)")
            self.btn_start_stop.setIcon(svg_icon("play", "#FFFFFF", 14))
            self.btn_start_stop.setObjectName("primaryStart")
            self.header_bar.set_live_shimmer(False)
            self.tray.set_active(False, "Остановлено")
        self.btn_start_stop.style().unpolish(self.btn_start_stop)
        self.btn_start_stop.style().polish(self.btn_start_stop)

    def _on_global_hotkey(self, action: str) -> None:
        if action == "toggle_pause":
            self._toggle_start_stop(from_hotkey=True)
        elif action == "select_roi":
            self.open_roi_selector()
        elif action == "interrupt":
            self.pipeline.interrupt_speech()

    def open_roi_selector(self) -> None:
        if self._roi_overlay is not None:
            self._roi_overlay.raise_()
            return
        if self._roi_opening:
            return

        target_proc = (self.cfg_mgr.config.target_process or self._current_target_process()).strip()
        if not target_proc:
            if not self._prompt_select_target_process(reason="roi"):
                return
            target_proc = (self.cfg_mgr.config.target_process or "").strip()
            if not target_proc:
                return

        if find_target_window_hwnd(target_proc, include_minimized=True) is None:
            QMessageBox.warning(
                self,
                "Окно процесса не найдено",
                f"Окно выбранного процесса «{target_proc}» сейчас не найдено.\n\n"
                "Запустите игру / выбранное приложение или выберите другой процесс на главной странице, "
                "затем повторите выбор области (F9).",
            )
            return

        # Временно скрываем MainWindow ДО восстановления/фокусировки окна игры, чтобы оно не перекрывало зону субтитров;
        # НИКОГДА не вызываем activateWindow() на RoiOverlay, чтобы полноэкранные окна игр и Xbox не сворачивались!
        self._roi_opening = True
        self._main_was_visible_before_roi = self.isVisible() and not self.isMinimized()
        if self._main_was_visible_before_roi:
            self.hide()
        self.pipeline.set_roi_selecting(True)

        found, was_minimized, already_fg = bring_target_window_to_front(target_proc)
        if not found:
            self._restore_window_after_roi()
            return

        _found_fs, exited_fs = ensure_target_window_windowed(target_proc)
        if exited_fs:
            self._on_log_event(
                PipelineLogEvent(
                    "WIN",
                    "info",
                    "—",
                    self.cfg_mgr.config.ocr_engine,
                    0.0,
                    f"Окно «{target_proc}» переведено в оконный режим",
                )
            )

        delay_ms = 0 if already_fg else (240 if was_minimized else 110)
        if exited_fs:
            delay_ms = max(delay_ms, 280)
        if delay_ms > 0:
            QTimer.singleShot(delay_ms, self._launch_roi_overlay)
        else:
            self._launch_roi_overlay()

    def _launch_roi_overlay(self) -> None:
        self._roi_opening = False
        if self._roi_overlay is not None:
            self._roi_overlay.raise_()
            return
        self._roi_overlay = RoiOverlay(
            self.cfg_mgr.config.roi,
            target_process=self.cfg_mgr.config.target_process,
        )
        self._roi_overlay.roi_selected.connect(self._on_roi_selected)
        self._roi_overlay.destroyed.connect(self._on_roi_overlay_closed)
        self._roi_overlay.cancelled.connect(self._on_roi_overlay_closed)
        self._roi_overlay.show()
        self._roi_overlay.raise_()

    def _restore_window_after_roi(self) -> None:
        self._roi_opening = False
        self.pipeline.set_roi_selecting(False)
        if self._main_was_visible_before_roi:
            self._main_was_visible_before_roi = False
            self.show()

    def _on_roi_selected(self, new_roi: RoiConfig) -> None:
        self.cfg_mgr.config.roi = new_roi
        self.cfg_mgr.save()
        self.pipeline.apply_config(self.cfg_mgr.config)
        self.pipeline.change_detector.reset()
        self._on_log_event(
            PipelineLogEvent(
                timestamp="ROI",
                status="info",
                speaker="—",
                voice="—",
                latency_ms=0.0,
                message=(
                    f"Область сохранена: x={new_roi.x_pct*100:.1f}%, y={new_roi.y_pct*100:.1f}%, "
                    f"w={new_roi.w_pct*100:.1f}%, h={new_roi.h_pct*100:.1f}%"
                ),
            )
        )
        self._roi_overlay = None
        self._restore_window_after_roi()

    def _on_roi_overlay_closed(self) -> None:
        self._roi_overlay = None
        self._restore_window_after_roi()

    def _on_voice_mode_selected(self, mode: str) -> None:
        self.cfg_mgr.config.voice_mode = mode
        self._update_voice_mode_subpanels(mode)
        self.cfg_mgr.save()
        self.pipeline.apply_config(self.cfg_mgr.config)

    def _on_test_voice(self) -> None:
        cfg = self.cfg_mgr.config
        if cfg.voice_mode == "single":
            sp = cfg.single_speaker
        else:
            sp = cfg.dual_male_speaker
        self.pipeline.speak_test_async(
            text="Вы позвонили Брайану Хедеру, добро пожаловать в Вайс-Сити!",
            speaker=sp,
            label=speaker_display_name(sp, short=True),
        )

    def _sync_speed_controls_state(self) -> None:
        adaptive = self.chk_adaptive_tempo.isChecked()
        for btn in (self.btn_speed_medium, self.btn_speed_fast, self.btn_speed_xfast):
            btn.setEnabled(not adaptive)
        if adaptive:
            self._update_speed_preset_buttons("medium")
            self.lbl_speed_value.setText("Авто (medium → fast)")
        else:
            self._update_speed_preset_buttons(self.cfg_mgr.config.speech_speed)

    def _update_speed_preset_buttons(self, raw_speed: str | float) -> None:
        preset = normalize_speed_preset(raw_speed)
        for btn, p in (
            (self.btn_speed_medium, "medium"),
            (self.btn_speed_fast, "fast"),
            (self.btn_speed_xfast, "x-fast"),
        ):
            btn.blockSignals(True)
            btn.setChecked(preset == p)
            btn.blockSignals(False)
        self.lbl_speed_value.setText(PRESET_DISPLAY_LABELS.get(preset, "Обычный (1.0x)"))

    def _on_speed_preset_selected(self, preset: str) -> None:
        if self._syncing_ui or self.chk_adaptive_tempo.isChecked():
            return
        norm = normalize_speed_preset(preset)
        self._update_speed_preset_buttons(norm)
        self.cfg_mgr.config.speech_speed = norm
        self.pipeline.apply_config(self.cfg_mgr.config)
        self._schedule_auto_save()

    def _on_volume_changed(self, value: int) -> None:
        if self._syncing_ui:
            return
        self.lbl_volume_value.setText(f"{value}%")
        self.cfg_mgr.config.volume = value
        self.pipeline.apply_config(self.cfg_mgr.config)
        self._schedule_auto_save()

    def _on_device_changed(self, _index: int) -> None:
        if self._syncing_ui:
            return
        dev_name = self.combo_device.currentData()
        self.cfg_mgr.config.audio_device = dev_name
        self.pipeline.apply_config(self.cfg_mgr.config)
        self._schedule_auto_save()

    def _on_quick_setting_changed(self) -> None:
        if self._syncing_ui:
            return
        cfg = self.cfg_mgr.config
        cfg.single_speaker = str(self.combo_single_voice.currentData() or "eugene")
        cfg.dual_male_speaker = str(self.combo_dual_male.currentData() or "eugene")
        cfg.dual_female_speaker = str(self.combo_dual_female.currentData() or "kseniya")
        cfg.adaptive_tempo = self.chk_adaptive_tempo.isChecked()
        if cfg.adaptive_tempo:
            cfg.speech_speed = "medium"
        self._sync_speed_controls_state()
        cfg.ducking_enabled = self.chk_ducking.isChecked()
        cfg.mute_target_window = self.chk_mute_window.isChecked()
        self.pipeline.apply_config(cfg)
        self._schedule_auto_save()

    def _on_browse_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите локальную модель Silero 5.6 (.pt)",
            "",
            "PyTorch Model (*.pt);;All Files (*)",
        )
        if path:
            self.edit_model_path.setText(to_portable_path(path))
            self._on_settings_auto_changed()

    def _on_autodetect_model(self) -> None:
        found = find_silero_model(None)
        if found:
            self.edit_model_path.setText(to_portable_path(found))
            self._on_settings_auto_changed()
        else:
            QMessageBox.warning(
                self,
                "Поиск модели",
                "Локальный файл models/v5_6_ru.pt не найден. Нажмите «Докачать / Обновить» в блоке компонентов.",
            )

    def _on_pick_duck_process(self) -> None:
        dlg = SelectProcessDialog(
            current_process=self._current_target_process(),
            parent=self,
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            exe_name = dlg.get_selected_process()
            if exe_name:
                self._set_target_process_combo(exe_name)
                self._on_target_process_changed()

    def _on_test_ducking(self) -> None:
        cfg = self.cfg_mgr.config
        matched, all_active = self.pipeline.ducker.probe_target_sessions()
        last_err = self.pipeline.ducker.last_error
        if matched:
            self.pipeline.speak_test_async(
                text=f"Проверка приглушения звука для процесса {', '.join(matched)}.",
                speaker=cfg.single_speaker if cfg.voice_mode == "single" else cfg.dual_male_speaker,
                label="Ducking",
            )
            mode_lines = [
                f"Приглушение во время речи (Auto-Ducking): {'ВКЛ' if cfg.ducking_enabled else 'ВЫКЛ'} (-{cfg.ducking_depth_pct}%)",
                f"Полное отключение звука окна при чтении: {'ВКЛ' if cfg.mute_target_window else 'ВЫКЛ'}",
            ]
            if last_err:
                mode_lines.append(f"Последняя ошибка: {last_err}")
            QMessageBox.information(
                self,
                "Приглушение звука",
                f"Аудиосессия найдена: {', '.join(matched)}\n"
                + "\n".join(mode_lines)
                + "\n\nВо время тестовой фразы звук этого процесса будет приглушён (если включён Auto-Ducking).",
            )
        else:
            active_list = "\n".join(f"  • {p}" for p in all_active) if all_active else "  (нет активных аудиопроцессов)"
            err_note = f"\n\nОшибка WASAPI: {last_err}" if last_err else ""
            QMessageBox.warning(
                self,
                "Аудиосессия не найдена",
                f"Сейчас Windows не видит активной аудиосессии для «{cfg.target_process}».\n\n"
                f"Процессы, которые сейчас выводят звук:\n{active_list}\n\n"
                "Убедитесь, что игра/стрим запущены со звуком, или выберите нужный процесс кнопкой «Выбрать...»."
                + err_note,
            )

    def _on_settings_auto_changed(self) -> None:
        if self._syncing_ui:
            return
        cfg = self.cfg_mgr.config
        old_model = cfg.silero_model_path
        old_threads = cfg.torch_num_threads
        old_hotkeys = (cfg.hotkeys.toggle_pause, cfg.hotkeys.select_roi, cfg.hotkeys.interrupt)

        cfg.silero_model_path = to_portable_path(self.edit_model_path.text().strip())
        cfg.torch_num_threads = self.spin_threads.value()
        cfg.ducking_depth_pct = self.spin_duck_depth.value()
        cfg.only_when_target_active = self.chk_only_when_active.isChecked()

        cfg.ocr_engine = "rapidocr"
        cfg.scan_fps = self.spin_fps.value()
        cfg.debounce_ms = self.spin_debounce.value()
        cfg.caps_ratio_threshold = round(self.spin_caps.value(), 2)
        cfg.enable_punctuation = self.chk_punct.isChecked()
        cfg.font_classifier_enabled = self.chk_font_clf.isChecked()

        new_top = self.chk_always_on_top.isChecked()
        if new_top != cfg.always_on_top:
            cfg.always_on_top = new_top
            was_vis = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, new_top)
            self._refresh_own_hwnds()
            if was_vis:
                self.show()

        cfg.roi.bind_to_xbox = self.chk_bind_xbox.isChecked()
        cfg.announce_status = self.chk_announce_status.isChecked()
        cfg.debug_capture_log = self.chk_debug_log.isChecked()
        cfg.hotkeys.toggle_pause = self.edit_hk_pause.text().strip() or "F8"
        cfg.hotkeys.select_roi = self.edit_hk_roi.text().strip() or "F9"
        cfg.hotkeys.interrupt = self.edit_hk_interrupt.text().strip() or "F10"

        self.pipeline.apply_config(cfg)
        self._schedule_auto_save()

        new_hotkeys = (cfg.hotkeys.toggle_pause, cfg.hotkeys.select_roi, cfg.hotkeys.interrupt)
        if new_hotkeys != old_hotkeys:
            self._register_hotkeys()

        if cfg.silero_model_path != old_model or cfg.torch_num_threads != old_threads:
            self._reload_model_timer.start()

    def _on_open_debug_logs(self) -> None:
        folder = self.pipeline.debug_logger.current_session_dir or self.pipeline.debug_logger.ensure_root_dir()
        try:
            os.startfile(str(folder))
        except Exception as exc:
            QMessageBox.information(self, "Папка логов отладки", f"Логи отладки сохраняются в:\n{folder}\n({exc})")

    def _on_export_profile(self) -> None:
        self._flush_auto_save()
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт настроек, моделей и словаря в один файл",
            "GTA6SubVoice_profile.json",
            "GTA6SubVoice Profile (*.json);;All Files (*)",
        )
        if not path_str:
            return
        try:
            export_profile_bundle(
                Path(path_str),
                config=self.cfg_mgr.config,
                stress_entries=self.stress_dict.list_entries(),
                characters=self.voice_mgr.export_raw(),
            )
            self._on_log_event(
                PipelineLogEvent("CFG", "info", "—", "—", 0.0, f"Профиль экспортирован: {Path(path_str).name}")
            )
            QMessageBox.information(
                self,
                "Экспорт завершён",
                f"Настройки, конфигурация моделей, словарь ударений и персонажи сохранены в:\n{path_str}",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Ошибка экспорта", f"Не удалось сохранить файл:\n{exc}")

    def _on_import_profile(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Импорт настроек, моделей и словаря из файла",
            "",
            "GTA6SubVoice Profile (*.json);;All Files (*)",
        )
        if not path_str:
            return
        try:
            old_model = self.cfg_mgr.config.silero_model_path
            old_threads = self.cfg_mgr.config.torch_num_threads
            old_top = self.cfg_mgr.config.always_on_top

            new_cfg, stress_entries, characters = load_profile_bundle(Path(path_str))
            # Автоматический поиск локальной модели Silero, если импортированный путь отсутствует на этом ПК
            detected = find_silero_model(new_cfg.silero_model_path)
            if detected is not None:
                new_cfg.silero_model_path = to_portable_path(detected)

            self.cfg_mgr.save(new_cfg)
            if stress_entries is not None:
                self.stress_dict.replace_all(stress_entries)
            if characters is not None:
                self.voice_mgr.replace_all(characters)

            self._sync_ui_from_config()
            self._apply_ui_mode(new_cfg.ui_mode, save=False)
            self._populate_audio_devices()
            self._refresh_characters_table()
            self._refresh_stress_table()
            self._refresh_components_table(verify_hash=False)
            self.pipeline.apply_config(new_cfg)
            self._register_hotkeys()
            self._apply_mobile_sync_settings()

            if new_cfg.always_on_top != old_top:
                was_vis = self.isVisible()
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, new_cfg.always_on_top)
                if was_vis:
                    self.show()

            if new_cfg.silero_model_path != old_model or new_cfg.torch_num_threads != old_threads:
                self.pipeline.load_models_async()

            self._on_log_event(
                PipelineLogEvent("CFG", "info", "—", new_cfg.ocr_engine, 0.0, f"Профиль импортирован: {Path(path_str).name}")
            )
            QMessageBox.information(
                self,
                "Импорт завершён",
                "Настройки, параметры моделей, словарь ударений и персонажи успешно загружены и применены.",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Ошибка импорта", f"Не удалось прочитать файл профиля:\n{exc}")

    def _on_reset_defaults(self) -> None:
        ans = QMessageBox.question(
            self,
            "Сбросить настройки по умолчанию",
            "Сбросить все настройки программы (громкость 100%, темп medium, Auto-Ducking 75%, сброс процесса и хоткеев) к значениям по умолчанию?",
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        old_model = self.cfg_mgr.config.silero_model_path
        old_threads = self.cfg_mgr.config.torch_num_threads
        old_top = self.cfg_mgr.config.always_on_top

        fresh = self.cfg_mgr.reset_to_defaults(keep_ui_mode=True)
        self._sync_ui_from_config()
        self._populate_audio_devices()
        self.pipeline.apply_config(fresh)
        self._register_hotkeys()
        self._refresh_components_table(verify_hash=False)
        self._apply_mobile_sync_settings()

        if fresh.always_on_top != old_top:
            was_vis = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, fresh.always_on_top)
            if was_vis:
                self.show()

        if fresh.silero_model_path != old_model or fresh.torch_num_threads != old_threads:
            self.pipeline.load_models_async()

        self._on_log_event(
            PipelineLogEvent("CFG", "info", "—", fresh.ocr_engine, 0.0, "Настройки сброшены по умолчанию")
        )

    # ------------------------------------------------------------------ Блок компонентов
    def _refresh_components_table(self, verify_hash: bool = False) -> None:
        report = self.dep_mgr.run_preflight(ocr_engine=self.cfg_mgr.config.ocr_engine, verify_hash=verify_hash)
        self.comp_table.setRowCount(0)
        st_map = {
            "installed": ("Установлен", "#00E5C3"),
            "missing": ("Не установлен", "#FF4D6D"),
            "corrupted": ("Ошибка", "#FF9F43"),
        }
        row_h = 30
        for row, item in enumerate(report.components):
            self.comp_table.insertRow(row)
            c0 = QTableWidgetItem(item.spec.name)
            c0.setFlags(c0.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.comp_table.setItem(row, 0, c0)

            txt, col = st_map.get(item.status, (item.status, "#F4F3FA"))
            c1 = QTableWidgetItem(txt)
            c1.setForeground(QColor(col))
            c1.setToolTip(item.message)
            c1.setFlags(c1.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.comp_table.setItem(row, 1, c1)

            mb = item.actual_size_bytes / (1024.0 * 1024.0) if item.actual_size_bytes > 0 else 0.0
            c2 = QTableWidgetItem(f"{mb:.1f} МБ" if mb > 0 else f"0 / {item.spec.total_size_mb:.1f} МБ")
            c2.setFlags(c2.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.comp_table.setItem(row, 2, c2)

            c3 = QTableWidgetItem(str(item.target_path))
            c3.setToolTip(str(item.target_path))
            c3.setFlags(c3.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.comp_table.setItem(row, 3, c3)
            self.comp_table.setRowHeight(row, row_h)

        header_h = max(28, self.comp_table.horizontalHeader().height())
        self.comp_table.setFixedHeight(header_h + max(1, len(report.components)) * row_h + 8)

    def _on_verify_models(self) -> None:
        self._refresh_components_table(verify_hash=True)
        QMessageBox.information(self, "Проверка целостности", "Проверка размеров, заголовков и SHA-256 завершена.")

    def _on_manual_install_silero(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите локальный файл модели Silero 5.6 (.pt)",
            "",
            "PyTorch Package (*.pt);;All Files (*)",
        )
        if not path_str:
            return
        ok, msg = self.dep_mgr.install_local_file("silero_v5_6", Path(path_str))
        if ok:
            found = find_silero_model(None)
            if found:
                self.cfg_mgr.config.silero_model_path = to_portable_path(found)
                self.cfg_mgr.save()
                self.edit_model_path.setText(self.cfg_mgr.config.silero_model_path)
                self.pipeline.load_models_async()
            self._refresh_components_table(verify_hash=False)
            QMessageBox.information(self, "Модель установлена", msg)
        else:
            QMessageBox.warning(self, "Ошибка", msg)

    # ------------------------------------------------------------------ Персонажи и словарь ударений
    def _refresh_characters_table(self) -> None:
        self._table_updating = True
        profiles = self.voice_mgr.list_profiles()
        speakers = self.voice_mgr.get_all_speakers()
        self.char_table.setRowCount(0)

        for row_idx, (name, prof) in enumerate(sorted(profiles.items(), key=lambda kv: kv[0].lower())):
            self.char_table.insertRow(row_idx)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.char_table.setItem(row_idx, 0, name_item)

            cb_gender = QComboBox()
            cb_gender.setCursor(Qt.CursorShape.PointingHandCursor)
            cb_gender.addItem("М", "male")
            cb_gender.addItem("Ж", "female")
            cb_gender.setCurrentIndex(0 if prof.gender == "male" else 1)
            cb_gender_ref = weakref.ref(cb_gender)
            cb_gender.currentIndexChanged.connect(
                lambda _i, n=name, ref=cb_gender_ref: (
                    self._on_char_gender_changed(n, ref().currentData()) if ref() is not None else None
                )
            )
            self.char_table.setCellWidget(row_idx, 1, cb_gender)

            cb_voice = QComboBox()
            cb_voice.setCursor(Qt.CursorShape.PointingHandCursor)
            for sp in speakers:
                cb_voice.addItem(speaker_display_name(sp), sp)
            sp_idx = cb_voice.findData(prof.speaker)
            if sp_idx >= 0:
                cb_voice.setCurrentIndex(sp_idx)
            cb_voice_ref = weakref.ref(cb_voice)
            cb_voice.currentIndexChanged.connect(
                lambda _i, n=name, ref=cb_voice_ref: (
                    self._on_char_voice_changed(n, str(ref().currentData())) if ref() is not None else None
                )
            )
            self.char_table.setCellWidget(row_idx, 2, cb_voice)

            btn_test = QPushButton(" Тест")
            btn_test.setIcon(svg_icon("play", "#00D9B5", 12))
            btn_test.setIconSize(QSize(12, 12))
            btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_test.clicked.connect(lambda _c=False, n=name: self._on_char_test(n))
            self.char_table.setCellWidget(row_idx, 3, btn_test)

            btn_del = QPushButton()
            btn_del.setIcon(svg_icon("trash-2", "#FFB6C9", 13))
            btn_del.setIconSize(QSize(13, 13))
            btn_del.setObjectName("dangerBtn")
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.setToolTip("Удалить персонажа")
            btn_del.clicked.connect(lambda _c=False, n=name: self._on_char_delete(n))
            self.char_table.setCellWidget(row_idx, 4, btn_del)

            self.char_table.setRowHeight(row_idx, 32)

        apply_interactive_cursors(self.char_table)
        self._table_updating = False

    def _on_char_gender_changed(self, name: str, gender: str) -> None:
        if self._table_updating:
            return
        self.voice_mgr.add_or_update_profile(name, gender=gender)
        self.pipeline.text_filter.reset_dedup()
        self._refresh_characters_table()

    def _on_char_voice_changed(self, name: str, speaker: str) -> None:
        if self._table_updating:
            return
        self.voice_mgr.add_or_update_profile(name, speaker=speaker, locked=True)
        self.pipeline.text_filter.reset_dedup()

    def _on_char_test(self, name: str) -> None:
        prof = self.voice_mgr.get_profile(name)
        if prof is None:
            return
        self.pipeline.speak_test_async(
            text=f"Привет! Я {name}, проверка нейронного голоса.",
            speaker=prof.speaker,
            label=name,
        )

    def _on_char_delete(self, name: str) -> None:
        self.voice_mgr.delete_profile(name)
        self.pipeline.text_filter.reset_dedup()
        self._refresh_characters_table()

    def _on_add_character_dialog(self) -> None:
        dlg = AddCharacterDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            name, gender, sp = dlg.get_values()
            if name:
                res = self.voice_mgr.add_or_update_profile(name, gender=gender, speaker=sp, locked=True)
                if res is None:
                    QMessageBox.warning(self, "Некорректное имя", "Введите корректное имя (от 2 букв без случайного смешивания латиницы и кириллицы).")
                else:
                    self.pipeline.text_filter.reset_dedup()
                self._refresh_characters_table()

    def _on_clear_characters(self) -> None:
        ans = QMessageBox.question(
            self,
            "Очистить базу персонажей",
            "Удалить всех сохранённых персонажей из data/characters.json?",
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.voice_mgr.clear_all()
            self.pipeline.text_filter.reset_dedup()
            self._refresh_characters_table()

    def _refresh_stress_table(self) -> None:
        entries = self.stress_dict.list_entries()
        self.stress_table.setRowCount(0)
        for row_idx, (word, repl) in enumerate(sorted(entries.items(), key=lambda kv: kv[0].lower())):
            self.stress_table.insertRow(row_idx)
            w_item = QTableWidgetItem(word)
            w_item.setFlags(w_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.stress_table.setItem(row_idx, 0, w_item)

            r_item = QTableWidgetItem(repl)
            r_item.setFlags(r_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.stress_table.setItem(row_idx, 1, r_item)

            btn_test = QPushButton(" Тест")
            btn_test.setIcon(svg_icon("play", "#00D9B5", 13))
            btn_test.setIconSize(QSize(13, 13))
            btn_test.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_test.clicked.connect(
                lambda _c=False, w=word, r=repl: self.pipeline.speak_test_async(
                    text=f"{r}.",
                    speaker=self.cfg_mgr.config.dual_male_speaker,
                    label=w,
                )
            )
            self.stress_table.setCellWidget(row_idx, 2, btn_test)

            btn_del = QPushButton()
            btn_del.setIcon(svg_icon("trash-2", "#FFB6C9", 14))
            btn_del.setIconSize(QSize(14, 14))
            btn_del.setObjectName("dangerBtn")
            btn_del.setCursor(Qt.CursorShape.PointingHandCursor)
            btn_del.setToolTip("Удалить запись")
            btn_del.clicked.connect(lambda _c=False, w=word: self._on_delete_stress_entry(w))
            self.stress_table.setCellWidget(row_idx, 3, btn_del)
            self.stress_table.setRowHeight(row_idx, 30)
        apply_interactive_cursors(self.stress_table)

    def _on_add_stress_entry(self) -> None:
        word = self.edit_stress_word.text().strip()
        repl = self.edit_stress_repl.text().strip()
        if not word or not repl:
            return
        self.stress_dict.set_entry(word, repl)
        self.pipeline.text_filter.reset_dedup()
        self.edit_stress_word.clear()
        self.edit_stress_repl.clear()
        self._refresh_stress_table()

    def _on_stress_table_double_click(self, row: int, _col: int) -> None:
        w_item = self.stress_table.item(row, 0)
        r_item = self.stress_table.item(row, 1)
        if w_item is None or r_item is None:
            return
        self.edit_stress_word.setText(w_item.text())
        self.edit_stress_repl.setText(r_item.text())
        self.edit_stress_repl.setFocus()
        self.edit_stress_repl.selectAll()

    def _on_delete_stress_entry(self, word: str) -> None:
        self.stress_dict.delete_entry(word)
        self.pipeline.text_filter.reset_dedup()
        self._refresh_stress_table()

    # ------------------------------------------------------------------ Связь со смартфоном (Wi-Fi пульт)
    def _populate_mobile_ips(self) -> None:
        prev_ip = str(self.combo_mobile_ip.currentData() or "")
        ips = detect_lan_ips()
        self.combo_mobile_ip.blockSignals(True)
        self.combo_mobile_ip.clear()
        for idx, ip in enumerate(ips):
            label = f"{ip} (основной Wi-Fi/LAN)" if idx == 0 else ip
            self.combo_mobile_ip.addItem(label, ip)
        if prev_ip:
            p_idx = self.combo_mobile_ip.findData(prev_ip)
            if p_idx >= 0:
                self.combo_mobile_ip.setCurrentIndex(p_idx)
        self.combo_mobile_ip.blockSignals(False)
        self._refresh_mobile_qr_and_url()

    def _refresh_mobile_qr_and_url(self) -> None:
        cfg = self.cfg_mgr.config
        if not cfg.mobile_sync_token:
            cfg.mobile_sync_token = generate_access_token()
            self._schedule_auto_save()
        self.lbl_mobile_token.setText(cfg.mobile_sync_token)

        running = self.mobile_server.is_running
        self.mobile_details_box.setVisible(bool(cfg.mobile_sync_enabled or running))

        if running:
            port = self.mobile_server.bound_port
            host = self.mobile_server.bound_host
            self.lbl_mobile_status.setText(f"Онлайн ({host}:{port})")
            self.lbl_mobile_status.setStyleSheet("color: #00E5C3; font-size: 11px; font-weight: 700;")
            peer_ip, peer_ts, hits = self.mobile_server.last_peer_info
            if hits > 0 and peer_ip:
                self.lbl_mobile_peer.setText(f"Последнее подключение с телефона: {peer_ip} в {peer_ts} (запросов: {hits})")
                self.lbl_mobile_peer.setStyleSheet("color: #00E5C3; font-size: 11px; font-weight: 600;")
            else:
                self.lbl_mobile_peer.setText(f"Сокет {host}:{port} слушает LAN • Подключений с телефона: 0")
                self.lbl_mobile_peer.setStyleSheet("color: #B4AFC9; font-size: 11px;")
        elif cfg.mobile_sync_enabled and self.mobile_server.last_error:
            self.lbl_mobile_status.setText("Ошибка порта")
            self.lbl_mobile_status.setStyleSheet("color: #FF4D6D; font-size: 11px; font-weight: 700;")
            self.lbl_mobile_peer.setText(self.mobile_server.last_error)
            self.lbl_mobile_peer.setStyleSheet("color: #FF4D6D; font-size: 11px;")
        else:
            self.lbl_mobile_status.setText("Выключен")
            self.lbl_mobile_status.setStyleSheet("color: #9A96B0; font-size: 11px; font-weight: 700;")
            self.lbl_mobile_peer.setText("Подключения по сети: сервер остановлен")
            self.lbl_mobile_peer.setStyleSheet("color: #9A96B0; font-size: 11px;")

        cur_ip = str(self.combo_mobile_ip.currentData() or detect_lan_ips()[0])
        url = self.mobile_server.build_url(ip_address=cur_ip, include_token=True)
        self.edit_mobile_url.setText(url)
        try:
            pm = render_qr_pixmap(url, target_px=152, quiet_modules=3)
            self.lbl_mobile_qr.setPixmap(pm)
        except Exception:
            self.lbl_mobile_qr.setText("QR\nошибка")

    def _on_mobile_sync_toggled(self, checked: bool) -> None:
        if self._syncing_ui:
            return
        self.cfg_mgr.config.mobile_sync_enabled = bool(checked)
        self._apply_mobile_sync_settings()

    def _on_mobile_port_changed(self, value: int) -> None:
        if self._syncing_ui:
            return
        self.cfg_mgr.config.mobile_sync_port = int(value)
        self._schedule_auto_save()
        if self.cfg_mgr.config.mobile_sync_enabled:
            self._mobile_port_timer.start()
        else:
            self._refresh_mobile_qr_and_url()

    def _apply_mobile_sync_settings(self) -> None:
        cfg = self.cfg_mgr.config
        if not cfg.mobile_sync_token:
            cfg.mobile_sync_token = generate_access_token()

        if not cfg.mobile_sync_enabled:
            self.mobile_server.stop()
            self._schedule_auto_save()
            self._refresh_mobile_qr_and_url()
            return

        self._populate_mobile_ips()
        ok, actual_port, err = self.mobile_server.start(
            port=cfg.mobile_sync_port,
            token=cfg.mobile_sync_token,
            auto_fallback_port=True,
        )
        if ok:
            if actual_port != cfg.mobile_sync_port:
                cfg.mobile_sync_port = actual_port
                self.spin_mobile_port.blockSignals(True)
                self.spin_mobile_port.setValue(actual_port)
                self.spin_mobile_port.blockSignals(False)
            self._on_log_event(
                PipelineLogEvent(
                    "WIFI",
                    "info",
                    "—",
                    cfg.ocr_engine,
                    0.0,
                    f"Веб-пульт Wi-Fi запущен: {self.mobile_server.build_url(str(self.combo_mobile_ip.currentData() or ''), include_token=False)}",
                )
            )
        else:
            self._on_log_event(
                PipelineLogEvent("WIFI", "error", "—", cfg.ocr_engine, 0.0, err or "Не удалось запустить Wi-Fi пульт")
            )
            QMessageBox.warning(self, "Веб-пульт Wi-Fi", err or "Не удалось запустить локальный веб-сервер.")

        self._schedule_auto_save()
        self._refresh_mobile_qr_and_url()

    def _on_mobile_regen_token(self) -> None:
        cfg = self.cfg_mgr.config
        cfg.mobile_sync_token = generate_access_token()
        self.mobile_server.set_token(cfg.mobile_sync_token)
        self._schedule_auto_save()
        self._refresh_mobile_qr_and_url()

    def _on_mobile_copy_url(self) -> None:
        url = self.edit_mobile_url.text().strip()
        if not url:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(url)
        self._on_log_event(
            PipelineLogEvent("WIFI", "info", "—", self.cfg_mgr.config.ocr_engine, 0.0, "Ссылка Wi-Fi пульта скопирована в буфер обмена")
        )

    def _on_mobile_open_browser(self) -> None:
        url = self.edit_mobile_url.text().strip()
        if url:
            webbrowser.open(url)

    def _on_mobile_open_firewall(self) -> None:
        port = self.mobile_server.bound_port or self.cfg_mgr.config.mobile_sync_port
        ok, msg = open_firewall_port_windows(port)
        if ok:
            QMessageBox.information(self, "Брандмауэр Windows", msg)
        else:
            QMessageBox.warning(self, "Брандмауэр Windows", msg)

    def _on_mobile_stress_synced(self, action: str, word: str, replacement: str) -> None:
        self._refresh_stress_table()
        msg = (
            f"Удалено слово (Wi-Fi): «{word}»"
            if action == "delete"
            else f"Словарь (Wi-Fi): «{word}» → «{replacement}»"
        )
        self._on_log_event(PipelineLogEvent("WIFI", "info", "—", self.cfg_mgr.config.ocr_engine, 0.0, msg))

    def _on_mobile_character_synced(self, action: str, name: str, gender: str, speaker: str) -> None:
        self._refresh_characters_table()
        if action == "delete":
            msg = f"Персонаж удалён (Wi-Fi): {name}"
        else:
            g_lbl = "Ж" if gender == "female" else "М"
            sp_lbl = speaker_display_name(speaker, short=True)
            msg = f"Персонаж (Wi-Fi): {name} → {g_lbl}, {sp_lbl}"
        self._on_log_event(PipelineLogEvent("WIFI", "info", "—", self.cfg_mgr.config.ocr_engine, 0.0, msg))

    def _on_mobile_client_activity(self, ip: str, hits: int, ts: str) -> None:
        if hits == 1:
            self._on_log_event(
                PipelineLogEvent("WIFI", "info", "—", self.cfg_mgr.config.ocr_engine, 0.0, f"Подключилось устройство по Wi-Fi: {ip}")
            )
        self.lbl_mobile_peer.setText(f"Последнее подключение с телефона: {ip} в {ts} (запросов: {hits})")
        self.lbl_mobile_peer.setStyleSheet("color: #00E5C3; font-size: 11px; font-weight: 600;")

    def _on_mobile_report_saved(self, speaker: str, raw_text: str) -> None:
        short = raw_text[:60] + ("…" if len(raw_text) > 60 else "")
        sp = speaker or "Без имени"
        self._on_log_event(
            PipelineLogEvent(
                "WIFI",
                "info",
                sp,
                self.cfg_mgr.config.ocr_engine,
                0.0,
                f"Реплика сохранена с телефона в saved_subtitles.txt: {short}",
            )
        )

    def _on_mobile_settings_synced(self) -> None:
        self._sync_ui_from_config()

    # ------------------------------------------------------------------ События конвейера
    def _on_status_changed(self, state_code: str, label: str) -> None:
        color = STATE_COLORS.get(state_code, "#9A96B0")
        pulsing = state_code in ("loading", "waiting", "speaking")
        self.status_badge.set_status(label, color, pulsing=pulsing)
        self.header_bar.set_live_shimmer(state_code in ("waiting", "speaking"))

    def _on_last_phrase_changed(self, speaker: str, phrase: str) -> None:
        if speaker and speaker != "Диктор":
            self.lbl_last_phrase.setText(f"{speaker}: {phrase}")
        else:
            self.lbl_last_phrase.setText(phrase)

    def _on_log_event(self, ev: PipelineLogEvent) -> None:
        lat = f"{ev.latency_ms:.0f}мс" if ev.latency_ms > 0 else "—"
        line = f"[{ev.timestamp}] {ev.speaker} ({ev.voice}) {lat} | {ev.message}"
        item = QListWidgetItem(line)
        color_map = {
            "spoken": QColor("#00E5C3"),
            "filtered": QColor("#FF9F43"),
            "error": QColor("#FF4D6D"),
            "info": QColor("#B4AFC9"),
        }
        item.setForeground(color_map.get(ev.status, QColor("#F4F3FA")))
        self.log_list.insertItem(0, item)
        while self.log_list.count() > MAX_LOG_ITEMS:
            old_item = self.log_list.takeItem(self.log_list.count() - 1)
            del old_item

    def _on_model_ready(self, _speakers: list, _load_ms: float) -> None:
        self._hide_loading_banner()
        if self.pipeline.tts.model_path:
            p_str = to_portable_path(self.pipeline.tts.model_path)
            if self.cfg_mgr.config.silero_model_path != p_str:
                self.cfg_mgr.config.silero_model_path = p_str
                self.cfg_mgr.save()
                self.edit_model_path.setText(p_str)
        self._refresh_characters_table()
        self._refresh_components_table(verify_hash=False)
        self._update_ram_label()

    def _on_pipeline_error(self, _msg: str) -> None:
        self._hide_loading_banner()
        self._on_status_changed("error", "Ошибка")

    def _update_ram_label(self) -> None:
        if not self.isVisible() or not self.lbl_ram.isVisible():
            return
        try:
            mb = self._proc.memory_info().rss / (1024 * 1024)
            self.lbl_ram.setText(f"RAM: {mb:.0f} МБ")
        except Exception:
            pass

    def _refresh_own_hwnds(self) -> None:
        try:
            hwnds = [int(self.winId())]
            if self._roi_overlay is not None:
                hwnds.append(int(self._roi_overlay.winId()))
            self.pipeline.set_own_hwnds(hwnds)
        except Exception:
            pass

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_own_hwnds()
        if not self._ram_timer.isActive():
            self._ram_timer.start()
        self._update_ram_label()

    def hideEvent(self, event) -> None:
        if self._ram_timer.isActive():
            self._ram_timer.stop()
        super().hideEvent(event)

    # ------------------------------------------------------------------ Трей и жизненный цикл
    def bring_to_front(self) -> None:
        """Разворачивает окно из трея или свёрнутого состояния при попытке запуска второго экземпляра."""
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._refresh_own_hwnds()

    def toggle_window_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.bring_to_front()

    # ------------------------------------------------------------------ Обновления (GitHub Releases)
    def _start_update_check(self, manual: bool = False) -> None:
        if self._did_shutdown:
            return
        if self._update_check_thread is not None and self._update_check_thread.isRunning():
            return
        if manual:
            self.btn_check_updates.setEnabled(False)
            self.btn_check_updates.setText(" Проверяю...")
            self.lbl_update_status.setText("Проверяю наличие обновлений на GitHub...")
            self.lbl_update_status.setStyleSheet("color: #9A96B0; font-size: 11px;")

        th = UpdateCheckThread(manual=manual, parent=None)
        th.checked.connect(self._on_update_check_done)
        th.finished.connect(th.deleteLater)
        th.finished.connect(self._on_update_thread_finished)
        self._update_check_thread = th
        th.start()

    def _on_update_thread_finished(self) -> None:
        sender_obj = self.sender()
        if sender_obj is not None and sender_obj is self._update_check_thread:
            self._update_check_thread = None

    def _on_update_check_done(self, release_obj: object, err_msg: str, manual: bool) -> None:
        self.btn_check_updates.setEnabled(True)
        self.btn_check_updates.setText(" Проверить обновления")
        if self._did_shutdown:
            return

        if err_msg:
            if manual:
                self.lbl_update_status.setText("Не удалось проверить обновления")
                self.lbl_update_status.setStyleSheet("color: #FF9F43; font-size: 11px;")
                QMessageBox.warning(
                    self,
                    "Ошибка проверки обновлений",
                    f"Не удалось получить данные о релизах с GitHub:\n{err_msg}",
                )
            return

        if release_obj is None:
            self.lbl_update_status.setText(f"Установлена актуальная версия (v{APP_VERSION})")
            self.lbl_update_status.setStyleSheet("color: #00E5C3; font-size: 11px;")
            if manual:
                QMessageBox.information(
                    self,
                    "Обновлений нет",
                    f"У вас уже установлена последняя версия GTA6SubVoice (v{APP_VERSION}).",
                )
            return

        self.lbl_update_status.setText(f"Доступна новая версия: v{release_obj.version}")
        self.lbl_update_status.setStyleSheet("color: #FF8754; font-size: 11px; font-weight: 600;")
        dlg = UpdateDialog(release_obj, on_restart_requested=self.quit_application, parent=self)
        dlg.exec()

    def _perform_clean_shutdown(self) -> None:
        if self._did_shutdown:
            return
        self._did_shutdown = True
        try:
            self.pipeline.request_shutdown()
        except Exception:
            pass
        try:
            if self._save_timer.isActive():
                self._save_timer.stop()
                self.cfg_mgr.save()
        except Exception:
            pass
        for tm in (self._reload_model_timer, self._ram_timer, self._update_timer, self._mobile_port_timer):
            try:
                tm.stop()
            except Exception:
                pass
        try:
            self.mobile_server.stop()
        except Exception:
            pass
        th = self._update_check_thread
        self._update_check_thread = None
        if th is not None:
            try:
                th.checked.disconnect(self._on_update_check_done)
            except Exception:
                pass
            if th.isRunning():
                try:
                    th.wait(300)
                except Exception:
                    pass
        if self._roi_overlay is not None:
            try:
                self._roi_overlay.close()
            except Exception:
                pass
            self._roi_overlay = None
        try:
            self.hotkey_mgr.unregister_all()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        try:
            self.pipeline.shutdown()
        except Exception:
            pass

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self.cfg_mgr.save()
        if not self._force_quit:
            event.ignore()
            self.hide()
            self.tray.notify("GTA VI SubVoice", "Приложение свёрнуто в системный трей.")
            return
        self._perform_clean_shutdown()
        event.accept()

    def quit_application(self) -> None:
        self._force_quit = True
        # Мгновенно скрываем окно, ROI-оверлей и иконку в трее до остановки фоновых потоков,
        # чтобы Windows не показывал «зависшее» полупрозрачное окно при выходе и обновлении.
        if self._roi_overlay is not None:
            try:
                self._roi_overlay.hide()
            except Exception:
                pass
        try:
            if self.isVisible():
                self.hide()
        except Exception:
            pass
        try:
            self.tray.hide()
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            # Закрываем открытые дочерние диалоги (включая UpdateDialog), чтобы не держали event loop
            for w in list(app.topLevelWidgets()):
                if w is not self:
                    try:
                        w.close()
                    except Exception:
                        pass
            app.processEvents()
        self._perform_clean_shutdown()
        try:
            self.close()
        except Exception:
            pass
        if app is not None:
            app.quit()
