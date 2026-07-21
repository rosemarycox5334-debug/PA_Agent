"""Detect stage-2 order opportunities and format alert text.

判定与文案的纯函数已移至 :mod:`pa_agent.notify.order_opportunity`
（服务端/GUI 共用）；本模块保留 Qt 弹窗与提示音，并 re-export 纯函数。
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

from PyQt6.QtCore import Qt

from pa_agent.notify.order_opportunity import (  # noqa: F401
    ORDER_OPPORTUNITY_TYPES,
    format_order_alert_message,
    has_order_opportunity,
)

logger = logging.getLogger(__name__)


def _windows_alert_wav_paths() -> list[str]:
    media = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media")
    names = (
        "notify.wav",
        "Windows Notify.wav",
        "Alarm01.wav",
        "Windows Exclamation.wav",
    )
    return [os.path.join(media, name) for name in names]


ORDER_ALERT_AUTO_CLOSE_MS = 120_000


def show_order_opportunity_alert(parent: Any, decision: dict[str, Any]) -> None:
    """Non-modal alert that auto-closes after :data:`ORDER_ALERT_AUTO_CLOSE_MS`."""
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QMessageBox

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("下单机会")
    box.setText(format_order_alert_message(decision))
    box.setStandardButtons(QMessageBox.StandardButton.Ok)
    timer = QTimer(box)
    timer.setSingleShot(True)
    timer.timeout.connect(box.accept)
    timer.start(ORDER_ALERT_AUTO_CLOSE_MS)
    # Non-modal: avoid blocking the main event loop after analysis completes.
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    box.show()


def play_order_alert_sound() -> bool:
    """Play a short alert sound (best-effort). Returns True if playback was attempted."""
    if sys.platform == "win32":
        import winsound

        for path in _windows_alert_wav_paths():
            if not os.path.isfile(path):
                continue
            try:
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                return True
            except Exception as exc:
                logger.debug("order alert PlaySound file %s failed: %s", path, exc)

        for alias in ("SystemExclamation", "SystemHand", "SystemAsterisk"):
            try:
                winsound.PlaySound(
                    alias,
                    winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
                return True
            except Exception as exc:
                logger.debug("order alert PlaySound alias %s failed: %s", alias, exc)

        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            return True
        except Exception as exc:
            logger.debug("order alert MessageBeep failed: %s", exc)

    try:
        from PyQt6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.beep()
            return True
    except Exception as exc:
        logger.debug("order alert QApplication.beep failed: %s", exc)

    return False
