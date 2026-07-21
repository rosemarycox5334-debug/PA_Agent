"""多品种轮巡监控控制器.

按「监控列表」轮流驱动主窗口的单品种分析流水线：

    切换品种 → 等待数据源就绪 → 提交分析 → 等待分析结束 → 下一个品种
    一轮全部品种完成后，等待设定的间隔分钟数，再开始下一轮。

复用现有单品种链路，因此下单信号的警报、飞书推送、交易记录等行为
与手动分析完全一致。任一品种数据获取失败或分析超时都会跳过该品种，
不会卡死整个轮巡。

约束
----
- 轮巡期间会自动关闭「持续跟踪分析」（两个自动触发机制互斥）。
- 轮巡使用当前周期下拉框选中的周期，所有品种共用同一周期。
- 演示模式下不可启动。
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from PyQt6.QtCore import QObject, QTimer

if TYPE_CHECKING:
    from pa_agent.gui.main_window import MainWindow

logger = logging.getLogger(__name__)

_TICK_MS = 3000
#: 切换品种后等待数据源订阅就绪的最长秒数，超时跳过该品种
_SWITCH_TIMEOUT_S = 60
#: 提交分析后等待 worker 真正启动（含后台取K线）的最长秒数
_ANALYSIS_START_TIMEOUT_S = 120
#: 单个品种一轮分析的最长秒数（LLM 可能很慢，给足余量）
_ANALYSIS_RUN_TIMEOUT_S = 1800


def parse_watch_symbols(raw: str) -> list[str]:
    """把逗号/中文逗号/空格分隔的品种串解析成去重列表（保序）."""
    normalized = raw.replace("，", ",").replace(" ", ",").replace("、", ",")
    seen: set[str] = set()
    result: list[str] = []
    for part in normalized.split(","):
        sym = part.strip()
        if sym and sym.upper() not in seen:
            seen.add(sym.upper())
            result.append(sym)
    return result


class WatchRotationController(QObject):
    """驱动主窗口在多个品种间轮流执行「切换 → 分析」的状态机."""

    # 状态: idle / switch / wait_data / analyzing / round_wait
    def __init__(self, win: "MainWindow") -> None:
        super().__init__(win)
        self._win = win
        self._symbols: list[str] = []
        self._interval_s: float = 600.0
        self._idx = 0
        self._round = 0
        self._phase = "idle"
        self._deadline = 0.0
        self._analysis_seen = False
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)

    # ── 对外接口 ─────────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._phase != "idle"

    def start(self, symbols: list[str], interval_min: int) -> str | None:
        """启动轮巡；成功返回 None，失败返回原因文本."""
        win = self._win
        if getattr(win, "_demo_mode", False):
            return "演示模式中无法启动轮巡，请先退出演示模式"
        if not win._has_api_key_configured():
            return "未配置 API Key，请先在「AI 模型设置」中填写"
        if len(symbols) < 1:
            return "监控列表为空，请至少填写一个品种"

        # 与「持续跟踪分析」互斥：两个自动触发机制会互相打架
        keep_cb = getattr(win, "_keep_analysis_checkbox", None)
        if keep_cb is not None and keep_cb.isChecked():
            keep_cb.setChecked(False)

        self._symbols = list(symbols)
        self._interval_s = max(0, int(interval_min)) * 60.0
        self._idx = 0
        self._round = 1
        self._phase = "switch"
        self._timer.start()
        logger.info(
            "多品种轮巡启动：%s，每轮间隔 %d 分钟", self._symbols, interval_min
        )
        win._status_bar.showMessage(
            f"多品种轮巡已启动：{'、'.join(self._symbols)}"
        )
        return None

    def stop(self) -> None:
        if self._phase == "idle":
            return
        self._phase = "idle"
        self._timer.stop()
        logger.info("多品种轮巡已停止")
        try:
            self._win._status_bar.showMessage("多品种轮巡已停止")
        except RuntimeError:
            pass  # 窗口已销毁

    def status_text(self) -> str:
        if self._phase == "idle":
            return "未运行"
        cur = self._symbols[self._idx] if self._symbols else "—"
        if self._phase == "round_wait":
            remain = max(0, int(self._deadline - time.monotonic()))
            return f"第 {self._round} 轮已完成，{remain // 60} 分 {remain % 60} 秒后开始下一轮"
        return f"第 {self._round} 轮：{cur}（{self._idx + 1}/{len(self._symbols)}）{self._phase}"

    # ── 状态机 ───────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        win = self._win
        if getattr(win, "_window_closing", False):
            self.stop()
            return
        now = time.monotonic()

        if self._phase == "switch":
            sym = self._symbols[self._idx]
            tf = win._tf_combo.currentText().strip()
            win._status_bar.showMessage(
                f"轮巡监控：切换到 {sym}（{self._idx + 1}/{len(self._symbols)}，第 {self._round} 轮）"
            )
            combo = win._symbol_combo
            combo.blockSignals(True)
            combo.setCurrentText(sym)
            combo.blockSignals(False)
            win._on_symbol_or_tf_changed(sym, tf)
            self._phase = "wait_data"
            self._deadline = now + _SWITCH_TIMEOUT_S

        elif self._phase == "wait_data":
            data_source = getattr(win._ctx, "data_source", None)
            ready = (
                data_source is not None
                and getattr(data_source, "_connected", False)
                and not win._switching
                and not win._analysis_in_progress
            )
            if ready:
                sym = self._symbols[self._idx]
                tf = win._tf_combo.currentText().strip()
                self._analysis_seen = False
                win._start_analysis(sym, tf, win._analysis_bar_count())
                self._phase = "analyzing"
                self._deadline = now + _ANALYSIS_START_TIMEOUT_S
            elif now > self._deadline:
                logger.warning(
                    "轮巡监控：%s 数据未就绪，跳过", self._symbols[self._idx]
                )
                self._advance()

        elif self._phase == "analyzing":
            if win._analysis_in_progress:
                if not self._analysis_seen:
                    self._analysis_seen = True
                    self._deadline = now + _ANALYSIS_RUN_TIMEOUT_S
                elif now > self._deadline:
                    logger.warning(
                        "轮巡监控：%s 分析超时，跳过", self._symbols[self._idx]
                    )
                    self._advance()
            else:
                if self._analysis_seen:
                    self._advance()  # 正常结束
                elif now > self._deadline:
                    logger.warning(
                        "轮巡监控：%s 分析未能启动（取数失败？），跳过",
                        self._symbols[self._idx],
                    )
                    self._advance()

        elif self._phase == "round_wait":
            if now >= self._deadline:
                self._round += 1
                self._phase = "switch"

    def _advance(self) -> None:
        self._idx += 1
        if self._idx < len(self._symbols):
            self._phase = "switch"
            return
        self._idx = 0
        if self._interval_s <= 0:
            self._round += 1
            self._phase = "switch"
            return
        self._phase = "round_wait"
        self._deadline = time.monotonic() + self._interval_s
        mins = int(self._interval_s // 60)
        self._win._status_bar.showMessage(
            f"轮巡监控：第 {self._round} 轮完成，{mins} 分钟后开始下一轮"
        )
