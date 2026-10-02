"""Cursor Pixel Tooltip - a Krita plugin.

Shows the image pixel under the pointer as a static readout in the status
bar (bottom of the window). Toggle it from
Tools > Scripts > Cursor Pixel Position.

Needs Krita 5.2 or newer (it relies on View.flakeToImageTransform()).
"""

import math

from krita import Krita, Extension

try:  # Krita 6
    from PyQt6.QtCore import Qt, QPointF, QTimer
    from PyQt6.QtGui import QCursor
    from PyQt6.QtWidgets import QApplication, QLabel, QMdiArea
except ImportError:  # Krita 5
    from PyQt5.QtCore import Qt, QPointF, QTimer
    from PyQt5.QtGui import QCursor
    from PyQt5.QtWidgets import QApplication, QLabel, QMdiArea


# ---- Settings you may want to change ---------------------------------------
UPDATE_INTERVAL_MS = 200     # how often the readout refreshes in ms (100 ms = 10/s)
HIDE_OUTSIDE_IMAGE = False   # True: blank the readout when off the image edges
TEXT_FORMAT = "{x}, {y}"     # e.g. "X: {x}  Y: {y}"
WIDEST_TEXT = " 000000, 000000 "  # reserves a fixed width so the bar never shifts
# -----------------------------------------------------------------------------

ACTION_ID = "cursor_pixel_tooltip_toggle"
SETTINGS_GROUP = "cursor_pixel_tooltip"
LABEL_NAME = "cursor_pixel_position_label"
CANVAS_CLASSES = ("KisOpenGLCanvas2", "KisQPainterCanvas")


def _is_canvas(widget):
    """True if the widget is one of Krita's canvas widgets."""
    name = widget.metaObject().className()
    return name in CANVAS_CLASSES or (name.startswith("Kis") and "Canvas" in name)


def _window_for(widget):
    """The Krita Window that owns the widget (falls back to the active one)."""
    app = Krita.instance()
    for window in app.windows():
        qwin = window.qwindow()
        if qwin is not None and qwin.isAncestorOf(widget):
            return window
    return app.activeWindow()


def _find_label(qwin):
    """The readout label in a main window's status bar, or None."""
    return qwin.statusBar().findChild(
        QLabel, LABEL_NAME, Qt.FindChildOption.FindDirectChildrenOnly)


class CursorPixelTooltip(Extension):

    def __init__(self, parent):
        super().__init__(parent)
        self._enabled = False
        self._actions = []
        self._label = None  # the label we last wrote to
        self._timer = QTimer(self)
        self._timer.setInterval(UPDATE_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)

    # -- Krita hooks -----------------------------------------------------------

    def setup(self):
        saved = Krita.instance().readSetting(SETTINGS_GROUP, "enabled", "true")
        self._enabled = str(saved).lower() == "true"

    def createActions(self, window):
        action = window.createAction(ACTION_ID, "Cursor Pixel Position", "tools/scripts")
        action.setCheckable(True)
        action.setChecked(self._enabled)
        action.toggled.connect(self._set_enabled)
        self._actions.append(action)
        if self._enabled:
            self._timer.start()

    # -- Toggle ----------------------------------------------------------------

    def _set_enabled(self, enabled):
        self._enabled = bool(enabled)
        Krita.instance().writeSetting(
            SETTINGS_GROUP, "enabled", "true" if self._enabled else "false")

        # Keep the menu item in every open Krita window in sync.
        alive = []
        for action in self._actions:
            try:
                if action.isChecked() != self._enabled:
                    action.blockSignals(True)
                    action.setChecked(self._enabled)
                    action.blockSignals(False)
                alive.append(action)
            except RuntimeError:  # that window has been closed
                pass
        self._actions = alive

        if self._enabled:
            self._timer.start()
        else:
            self._timer.stop()
            self._hide_all()

    # -- Status bar readout ----------------------------------------------------

    def _label_for(self, window):
        """The readout label for a Krita window, created on first use."""
        qwin = window.qwindow()
        label = _find_label(qwin)
        if label is None:
            bar = qwin.statusBar()
            label = QLabel(bar)
            label.setObjectName(LABEL_NAME)
            label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            label.setMinimumWidth(label.fontMetrics().horizontalAdvance(WIDEST_TEXT))
            label.setToolTip("Image pixel under the cursor")
            bar.insertPermanentWidget(0, label)
        return label

    def _clear(self):
        """Blank the readout (pointer is not over a canvas)."""
        if self._label is not None:
            try:
                self._label.setText("")
            except RuntimeError:  # its window has been closed
                self._label = None

    def _hide_all(self):
        self._label = None
        for window in Krita.instance().windows():
            qwin = window.qwindow()
            label = _find_label(qwin) if qwin is not None else None
            if label is not None:
                label.setText("")
                label.hide()

    def _tick(self):
        try:
            hit = self._locate(QCursor.pos())
            if hit is None:
                self._clear()
                return
            window, x, y = hit
            label = self._label_for(window)
            if label is not self._label:
                self._clear()
                self._label = label
            label.setText(TEXT_FORMAT.format(x=x, y=y))
            if not label.isVisible():
                label.show()
        except Exception:
            # Never let a hiccup (document closing mid-update, etc.) spam
            # error dialogs from a timer; just skip this update.
            pass

    # -- Coordinate lookup -----------------------------------------------------

    def _locate(self, global_pos):
        """(window, x, y) for the image pixel under a screen position, or None."""
        # Nothing to report when Krita isn't focused or a menu/dialog is up.
        if (QApplication.activeWindow() is None
                or QApplication.activePopupWidget() is not None
                or QApplication.activeModalWidget() is not None):
            return None

        widget = QApplication.widgetAt(global_pos)
        if widget is None or not _is_canvas(widget):
            return None

        window = _window_for(widget)
        if window is None or window.qwindow() is None:
            return None
        view = window.activeView()
        if view is None:
            return None
        document = view.document()
        if document is None:
            return None

        # In subwindow mode several canvases can be visible at once; only the
        # active one matches the transforms we get from activeView().
        mdi = window.qwindow().findChild(QMdiArea)
        if mdi is not None and mdi.isAncestorOf(widget):
            current = mdi.currentSubWindow()
            if current is not None and not current.isAncestorOf(widget):
                return None

        # canvas widget -> "flake" -> image pixels. These transforms already
        # account for zoom, pan, canvas rotation and mirroring.
        widget_to_flake, invertible = view.flakeToCanvasTransform().inverted()
        if not invertible:
            return None
        widget_to_image = widget_to_flake * view.flakeToImageTransform()

        local = widget.mapFromGlobal(global_pos)
        point = widget_to_image.map(QPointF(local))
        x = math.floor(point.x())
        y = math.floor(point.y())

        if HIDE_OUTSIDE_IMAGE and not (
                0 <= x < document.width() and 0 <= y < document.height()):
            return None
        return window, x, y


Krita.instance().addExtension(CursorPixelTooltip(Krita.instance()))
