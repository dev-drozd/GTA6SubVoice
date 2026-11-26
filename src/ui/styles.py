DARK_STYLESHEET = """
QMainWindow, QDialog {
    background-color: #0B0B12;
    color: #F2F1FA;
    font-family: "SF Pro Text", "Segoe UI Variable", "Segoe UI", "Inter", sans-serif;
    font-size: 12px;
}

QWidget {
    color: #F2F1FA;
    font-family: "SF Pro Text", "Segoe UI Variable", "Segoe UI", "Inter", sans-serif;
    font-size: 12px;
}

QScrollArea {
    background: transparent;
    border: none;
}

QScrollArea > QWidget > QWidget {
    background: transparent;
}

QWidget#cardPanel {
    background-color: #161524;
    border: 1px solid #2A2740;
    border-radius: 9px;
}

QWidget#lastPhraseBox {
    background-color: #141321;
    border: 1px solid #28253D;
    border-radius: 8px;
}

/* Segmented Tabs (compact macOS / Windows 11 clean style) */
QTabWidget::pane {
    border: 1px solid #252238;
    border-top-left-radius: 0px;
    border-top-right-radius: 9px;
    border-bottom-left-radius: 9px;
    border-bottom-right-radius: 9px;
    background-color: #11101C;
    top: -1px;
}

QTabWidget[basicMode="true"]::pane {
    border-top-left-radius: 9px;
}

QTabBar::tab {
    background-color: #151423;
    color: #9893B0;
    border: 1px solid #252238;
    border-bottom: none;
    padding: 5px 12px 7px 12px;
    margin-right: 3px;
    border-top-left-radius: 7px;
    border-top-right-radius: 7px;
    font-weight: 600;
}

QTabBar::tab:selected {
    background-color: #1D1B30;
    color: #FFFFFF;
    border-color: #FF2E88;
}

QTabBar::tab:hover:!selected {
    background-color: #1B192C;
    color: #E7E5F5;
}

/* Compact Elevated Section Cards */
QGroupBox {
    border: 1px solid #26233A;
    border-radius: 9px;
    margin-top: 10px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
    color: #C8C3DE;
    background-color: #151422;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 11px;
    top: 1px;
    padding: 0 6px;
    color: #FF8754;
    font-size: 11px;
    font-weight: 700;
    background-color: #11101C;
    border-radius: 4px;
}

/* Compact Tactile Buttons (optical +1px bottom pad balances Segoe UI / SF Pro cap-height with SVG icons) */
QPushButton {
    background-color: #1E1C30;
    color: #F2F1FA;
    border: 1px solid #332F4D;
    border-radius: 7px;
    padding: 4px 10px 6px 10px;
    min-height: 18px;
    font-weight: 600;
}

QPushButton:hover {
    background-color: #282540;
    border-color: #FF4A98;
    color: #FFFFFF;
}

QPushButton:pressed {
    background-color: #171526;
    padding-top: 5px;
    padding-bottom: 5px;
}

QPushButton:disabled {
    background-color: #151421;
    color: #66627D;
    border-color: #232035;
}

QPushButton#primaryStart {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF2E88, stop:0.55 #FF566B, stop:1 #FF7A45);
    border: 1px solid #FF6BA8;
    color: #FFFFFF;
    font-size: 13px;
    font-weight: 700;
    padding: 6px 16px 8px 16px;
    min-height: 22px;
    border-radius: 8px;
}

QPushButton#primaryStart:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF4395, stop:0.55 #FF697C, stop:1 #FF8C5B);
    border-color: #FFA0C7;
}

QPushButton#primaryStart:pressed {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #E02275, stop:1 #E06434);
    padding-top: 7px;
    padding-bottom: 7px;
}

QPushButton#primaryStop {
    background-color: #112423;
    border: 1.5px solid #00D9B5;
    color: #2CF0CE;
    font-size: 13px;
    font-weight: 700;
    padding: 6px 16px 8px 16px;
    min-height: 22px;
    border-radius: 8px;
}

QPushButton#primaryStop:hover {
    background-color: #15312F;
    border-color: #4DFFE0;
    color: #FFFFFF;
}

QPushButton#primaryStop:pressed {
    background-color: #0D1C1B;
    padding-top: 7px;
    padding-bottom: 7px;
}

QPushButton#modeToggleBtn {
    background-color: #1B192C;
    border: 1px solid #353152;
    color: #E3DFF7;
    padding: 3px 10px 5px 10px;
    min-height: 18px;
    border-radius: 7px;
    font-size: 11px;
    font-weight: 600;
}

QPushButton#modeToggleBtn:hover {
    border-color: #FF7A45;
    color: #FFFFFF;
    background-color: #25213B;
}

QPushButton#modeToggleBtn:pressed {
    background-color: #161425;
}

QPushButton#supportBtn {
    background-color: #241628;
    border: 1px solid #6E284E;
    color: #FFD0E5;
    padding: 3px 9px 5px 9px;
    min-height: 18px;
    border-radius: 7px;
    font-size: 11px;
    font-weight: 600;
}

QPushButton#supportBtn:hover {
    background-color: #341A36;
    border-color: #FF2E88;
    color: #FFFFFF;
}

QPushButton#supportBtn:pressed {
    background-color: #1C1120;
}

QPushButton#modeCard {
    background-color: #11101C;
    border: 1px solid #2A2640;
    color: #ACA7C4;
    border-radius: 7px;
    padding: 5px 8px 7px 8px;
    min-height: 18px;
    font-weight: 600;
}

QPushButton#modeCard:hover {
    border-color: #FF7A45;
    color: #FFFFFF;
    background-color: #1A182B;
}

QPushButton#modeCard:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #35162F, stop:1 #362022);
    border: 1.5px solid #FF2E88;
    color: #FFFFFF;
}

QPushButton#modeCard:disabled {
    background-color: #0D0C15;
    border: 1px solid #1E1B2E;
    color: #5C5775;
}

QPushButton#modeCard:checked:disabled {
    background-color: #1A1424;
    border: 1px solid #592642;
    color: #8E7F9B;
}

QPushButton#accentBtn {
    background-color: #201C34;
    border: 1px solid #C93578;
    color: #FFFFFF;
    padding: 4px 10px 6px 10px;
    min-height: 18px;
}

QPushButton#accentBtn:hover {
    background-color: #2E203D;
    border-color: #FF6B9E;
}

QPushButton#dangerBtn {
    background-color: #291520;
    border: 1px solid #6E2840;
    color: #FFB6C9;
}

QPushButton#dangerBtn:hover {
    background-color: #3D1C2D;
    border-color: #FF3B7F;
    color: #FFFFFF;
}

/* Compact Inputs & Dropdowns */
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {
    background-color: #0E0D17;
    border: 1px solid #2E2A45;
    border-radius: 6px;
    padding: 3px 8px 5px 8px;
    min-height: 18px;
    color: #F2F1FA;
    selection-background-color: #FF2E88;
}

QComboBox:hover, QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover {
    border-color: #FF7A45;
}

QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border-color: #FF2E88;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 22px;
    border: none;
    background: transparent;
}

QComboBox::down-arrow {
    image: none;
    width: 0px;
    height: 0px;
}

QComboBox QAbstractItemView {
    background-color: #171526;
    color: #F2F1FA;
    selection-background-color: #FF2E88;
    selection-color: #FFFFFF;
    border: 1px solid #353052;
    border-radius: 7px;
    padding: 3px;
    outline: none;
}

/* Standard CheckBox (for dialogs/tables) */
QCheckBox {
    spacing: 6px;
    color: #E5E2F2;
    min-height: 20px;
}

QCheckBox::indicator {
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1.5px solid #443E63;
    background-color: #0E0D17;
}

QCheckBox::indicator:hover {
    border-color: #FF7A45;
}

QCheckBox::indicator:checked {
    background-color: #FF2E88;
    border-color: #FF7A45;
}

/* Tables & Logs */
QTableWidget {
    background-color: #0F0E1A;
    alternate-background-color: #151424;
    gridline-color: #242138;
    border: 1px solid #26233A;
    border-radius: 7px;
    selection-background-color: #2F2246;
}

QHeaderView::section {
    background-color: #1A182B;
    color: #C5C0DB;
    border: none;
    border-right: 1px solid #26233A;
    border-bottom: 1px solid #26233A;
    padding: 5px 8px;
    font-weight: 600;
    font-size: 11px;
}

QListWidget {
    background-color: #0D0C16;
    border: 1px solid #252238;
    border-radius: 7px;
    padding: 4px;
    font-family: "JetBrains Mono", "Cascadia Code", "SF Mono", "Consolas", monospace;
    font-size: 10.5px;
}

QListWidget::item {
    padding: 2px 4px;
    border-radius: 4px;
}

QListWidget::item:selected {
    background-color: #261F3D;
}

/* Sleek Cross-Platform Scrollbars (macOS / Win11 thin rounded style) */
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 2px 1px 2px 1px;
}

QScrollBar::handle:vertical {
    background: #353152;
    border-radius: 3px;
    min-height: 24px;
}

QScrollBar::handle:vertical:hover {
    background: #FF4A98;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
    background: none;
    height: 0px;
}

QScrollBar:horizontal {
    background: transparent;
    height: 6px;
    margin: 1px 2px 1px 2px;
}

QScrollBar::handle:horizontal {
    background: #353152;
    border-radius: 3px;
    min-width: 24px;
}

QScrollBar::handle:horizontal:hover {
    background: #FF4A98;
}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
    background: none;
    width: 0px;
}

/* Progress Bar */
QProgressBar {
    background-color: #100F1B;
    border: 1px solid #27243D;
    border-radius: 4px;
    text-align: center;
    color: #FFFFFF;
    font-size: 10.5px;
}

QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #FF2E88, stop:1 #FF7A45);
    border-radius: 4px;
}

/* Tooltips */
QToolTip {
    background-color: #1B192C;
    color: #F2F1FA;
    border: 1px solid #3C375E;
    border-radius: 6px;
    padding: 4px 7px;
    font-size: 11px;
}
"""
