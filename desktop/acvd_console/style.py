"""ACVD-inspired native widget palette; all visual elements are self-contained."""

STYLE = """
QWidget { background: #080e13; color: #b9ced5; font-family: 'DejaVu Sans'; font-size: 12px; }
QMainWindow, QStackedWidget { background: #080e13; }
QLabel#title { color: #e0f3f6; font-size: 26px; font-weight: 600; letter-spacing: 3px; }
QLabel#eyebrow { color: #12c8f0; font-size: 10px; letter-spacing: 3px; }
QLabel#muted { color: #79929e; }
QLabel#notice { color: #fa8e32; background: #191714; padding: 12px; border-left: 2px solid #ff8d32; }
QLabel#metric { color: #dcf7ff; font-size: 28px; }
QFrame#card { background: #101b23; border: 1px solid #233a45; border-top: 2px solid #167b96; }
QFrame#card QLabel { background: transparent; }
QPushButton { background: #10242f; color: #a9d8e6; border: 1px solid #345360; padding: 9px 15px; }
QPushButton:hover { border-color: #17c9ef; color: #ff973e; background: #11394c; }
QPushButton:pressed { background: #176383; }
QPushButton:disabled { background: #10181d; color: #53616a; border-color: #21303a; }
QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #107797,stop:1 #123346); color: #ff9a40; border-color: #42849a; }
QPushButton#primary:disabled { background: #10181d; color: #53616a; border-color: #21303a; }
QPushButton#danger { color: #f09b81; border-color: #775046; }
QLineEdit, QPlainTextEdit, QSpinBox, QDateTimeEdit { background: #050b10; border: 1px solid #2b414e; padding: 7px; selection-background-color: #165e76; }
QPlainTextEdit { font-family: 'DejaVu Sans Mono'; font-size: 11px; }
QComboBox { background: #101e29; padding: 7px; border: 1px solid #304a58; }
QListWidget { background: #080f16; border: none; border-right: 1px solid #25414c; }
QListWidget::item { padding: 17px 18px; color: #91acb9; border-bottom: 1px solid #142530; }
QListWidget::item:selected { color: #ff9638; background: #11364b; border-left: 3px solid #17c8ee; }
QTableWidget { background: #0b141b; alternate-background-color: #101f29; gridline-color: #19313d; border: 1px solid #263e4b; selection-background-color: #174359; }
QHeaderView::section { background: #102632; color: #58bfdb; border: none; padding: 8px; border-bottom: 1px solid #31738b; }
QTabWidget::pane { border: 1px solid #25414e; }
QTabBar::tab { background: #111e29; padding: 9px 15px; border-bottom: 2px solid #243e4b; }
QTabBar::tab:selected { color: #ff9638; border-bottom-color: #13c6ec; }
QCheckBox { spacing: 9px; }
QCheckBox::indicator { width: 15px; height: 15px; background: #0b141b; border: 1px solid #386477; }
QCheckBox::indicator:checked { background: #20bce4; border: 2px solid #95d3e5; }
QScrollBar:vertical { background:#0a1219; width:10px; }
QScrollBar::handle:vertical { background:#294956; min-height:25px; }
QStatusBar { color:#829ba7; border-top:1px solid #213c4a; }
QToolTip { color:#d7ebf1; background:#142833; border:1px solid #3b6879; }
"""
