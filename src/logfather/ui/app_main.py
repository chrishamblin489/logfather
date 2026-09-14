"""Application entry point: splash screen + main().

Extracted from Main_Window so the hub module is importable without the
bootstrap path. Run the app either way:

    .venv/Scripts/python.exe src/Main_Window.py
    .venv/Scripts/python.exe src/app_main.py
"""
from __future__ import annotations

import sys

from PySide6.QtCore import Qt, QTimer, QVariantAnimation, QEasingCurve
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QSplashScreen, QWidget

from logfather.ui.app_assets import resolve_asset_path as _resolve_asset_path
from logfather.ui import theme
from logfather.core.app_version import format_version_label, load_version_info
from logfather.core.log import log

SPLASH_IMAGE_FILENAME = "Logfather Argus II.jpg"


class FadeSplashScreen(QSplashScreen):
    def __init__(self, pixmap: QPixmap, flags=Qt.WindowType.Widget):
        super().__init__(pixmap, flags)
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(220)
        self._fade_anim.setStartValue(1.0)
        self._fade_anim.setEndValue(0.0)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim.valueChanged.connect(self._on_fade_value_changed)
        self._fade_anim.finished.connect(self._on_fade_finished)

    def fade_and_finish(self, widget: QWidget):
        try:
            widget.raise_()
            widget.activateWindow()
        except Exception:
            pass
        self.setWindowOpacity(1.0)
        self._fade_anim.start()

    def _on_fade_value_changed(self, value):
        try:
            self.setWindowOpacity(float(value))
        except Exception:
            pass

    def _on_fade_finished(self):
        self.close()



DEFAULT_WINDOW_SIZE = (1400, 700)


def _apply_startup_geometry(win: QWidget, settings) -> None:
    """Restore the last-close geometry, else center at the default size.

    Either way the rect is clamped fully onto a live screen: a position
    remembered from a now-disconnected monitor (or the OS's cascade
    placement) must never leave the window half off-screen."""
    from PySide6.QtCore import QRect

    saved = getattr(settings, "window_geometry", None)
    rect = None
    maximized = False
    if isinstance(saved, dict):
        try:
            rect = QRect(
                int(saved["x"]), int(saved["y"]), int(saved["w"]), int(saved["h"])
            )
            if rect.width() < 200 or rect.height() < 150:
                rect = None
            else:
                maximized = bool(saved.get("maximized"))
        except Exception:
            rect = None

    screen = None
    if rect is not None:
        screen = QApplication.screenAt(rect.center())
    if screen is None:
        screen = QApplication.primaryScreen()
    avail = screen.availableGeometry()

    if rect is None:
        w = min(DEFAULT_WINDOW_SIZE[0], avail.width())
        h = min(DEFAULT_WINDOW_SIZE[1], avail.height())
        rect = QRect(
            avail.center().x() - w // 2, avail.center().y() - h // 2, w, h
        )

    win.setGeometry(clamp_rect_to_screen(rect, avail))
    if maximized:
        win.setWindowState(win.windowState() | Qt.WindowMaximized)
    return maximized


def _nudge_frame_fully_onscreen(win: QWidget) -> None:
    """Post-show correction: geometry() excludes the window frame, and
    before show() the frame margins are unknown, so a restored position can
    leave the title bar off the top of the screen. Once shown, the frame
    rect is real - shift the window so all of it is visible."""
    if win.isMaximized():
        return
    screen = win.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    frame = win.frameGeometry()
    dx = 0
    dy = 0
    if frame.right() > avail.right():
        dx = avail.right() - frame.right()
    if frame.bottom() > avail.bottom():
        dy = avail.bottom() - frame.bottom()
    # Top/left last: the title bar must win when the window is oversized.
    if frame.left() + dx < avail.left():
        dx = avail.left() - frame.left()
    if frame.top() + dy < avail.top():
        dy = avail.top() - frame.top()
    if dx or dy:
        win.move(win.x() + dx, win.y() + dy)


def clamp_rect_to_screen(rect, avail):
    """Shrink and shift `rect` so it lies fully inside `avail` (QRects)."""
    from PySide6.QtCore import QRect

    w = min(rect.width(), avail.width())
    h = min(rect.height(), avail.height())
    x = max(avail.left(), min(rect.x(), avail.right() - w + 1))
    y = max(avail.top(), min(rect.y(), avail.bottom() - h + 1))
    return QRect(x, y, w, h)


def _desktop_dir():
    """The user's Desktop, honouring OneDrive folder redirection."""
    import os
    import winreg
    from pathlib import Path

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _kind = winreg.QueryValueEx(key, "Desktop")
        return Path(os.path.expandvars(str(raw)))
    except Exception:
        return Path.home() / "Desktop"


def _refresh_desktop_shortcut_name() -> None:
    """Rename the desktop shortcut to carry the running version, e.g.
    "Logfather (v0.133)" (Chris, 2026-09-04). The dev shortcut always
    launches the current checkout and the version bumps per commit, so
    the name is refreshed on every startup rather than stamped once.
    Matches the original "The Logfather" name and any previously
    version-stamped name; never overwrites an existing file."""
    try:
        version = str(load_version_info().get("version") or "")
        if not version or version == "dev":
            return
        desktop = _desktop_dir()
        if not desktop.is_dir():
            return
        target = desktop / f"Logfather (v{version}).lnk"
        for link in desktop.glob("*.lnk"):
            stem = link.stem
            if stem != "The Logfather" and not (
                stem.startswith("Logfather (v") and stem.endswith(")")
            ):
                continue
            if link == target:
                return
            if not target.exists():
                link.rename(target)
                log("main", f"desktop shortcut renamed to {target.name}")
            return
    except Exception:
        pass


# Single-instance guard: two instances autosave the same settings file and
# clobber each other, so a second launch just fronts the running one
# (Chris, 2026-09-03). The name is shared by dev runs and installed builds
# on purpose - they use the same settings file.
SINGLE_INSTANCE_KEY = "TheLogfatherSingleInstance"


def _activate_running_instance() -> bool:
    """True when another instance is already running (it has been asked to
    come to the front)."""
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_KEY)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"activate\n")
    sock.flush()
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def _start_instance_server(win: QWidget) -> QLocalServer | None:
    # A crashed instance can leave a stale name behind (not on Windows,
    # where named pipes die with the process, but harmless to clear).
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer(win)
    if not server.listen(SINGLE_INSTANCE_KEY):
        log("main", f"single-instance server failed: {server.errorString()}")
        return None

    def _on_second_instance():
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.close()
        win.setWindowState((win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        win.show()
        win.raise_()
        win.activateWindow()
        log("main", "second launch detected; fronting this window")

    server.newConnection.connect(_on_second_instance)
    return server


def main():
    app = QApplication(sys.argv)
    theme.apply_app_theme(app)
    if _activate_running_instance():
        log("main", "already running - switched to the open instance")
        return
    icon_path = _resolve_asset_path("logfather.ico")
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))
    splash = _build_splash_image()
    if splash is not None:
        splash.show()
        app.processEvents()
    from logfather.ui.Main_Window import MainWindow

    win = MainWindow()
    _instance_server = _start_instance_server(win)  # noqa: F841 (kept alive)
    _refresh_desktop_shortcut_name()
    maximized = _apply_startup_geometry(win, win.settings)
    # showMaximized as well as the pre-show window state: setting the
    # state alone before show() intermittently comes up restored on
    # Windows (Chris, 2026-09-05: "often not fullscreen").
    if maximized:
        win.showMaximized()
    else:
        win.show()
    if splash is not None:
        splash.fade_and_finish(win)
    QTimer.singleShot(0, lambda: _nudge_frame_fully_onscreen(win))
    sys.exit(app.exec())

def _build_splash_image() -> QSplashScreen | None:
    try:
        image_path = _resolve_asset_path(SPLASH_IMAGE_FILENAME)
        if not image_path:
            return None
        pixmap = QPixmap(image_path)
        if pixmap.isNull():
            return None
        scaled = pixmap.scaled(
            max(1, int(pixmap.width() * 0.33)),
            max(1, int(pixmap.height() * 0.33)),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        framed = QPixmap(scaled.width() + 4, scaled.height() + 4)
        framed.fill(QColor(theme.BG))
        painter = QPainter(framed)
        painter.drawPixmap(2, 2, scaled)
        pen = QPen(QColor(theme.BORDER_LIGHT))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRect(1, 1, framed.width() - 3, framed.height() - 3)
        painter.end()
        splash = FadeSplashScreen(framed, Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        splash.setFont(QFont("", 10))
        splash.showMessage(
            f"Loading...  {format_version_label()}",
            Qt.AlignBottom | Qt.AlignHCenter,
            QColor(255, 255, 255, 210),
        )
        return splash
    except Exception:
        return None





if __name__ == "__main__":
    main()
