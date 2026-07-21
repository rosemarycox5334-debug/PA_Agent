"""单品种分析编排：订阅 → 等数据 → 构帧 → submit → 判定 → 推送.

对应 GUI 侧「_start_analysis → _AnalysisWorker → _spawn_post_order_followup」
的整条链路，纯 Python 实现，供轮巡调度器与手动触发 API 共用。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from pa_agent.notify.order_opportunity import (
    _parse_trade_confidence,
    has_order_opportunity,
)
from pa_agent.server.bootstrap import build_orchestrator  # noqa: F401  (供测试 patch)
from pa_agent.server.state import ServerState
from pa_agent.util.threading import CancelToken, OrchestratorEvent

#: 等待数据源就绪（能构出完整分析快照）的最长秒数
DATA_READY_TIMEOUT_S = 120
#: 单品种一次两阶段分析的最长秒数（LLM 可能很慢，给足余量）
ANALYSIS_TIMEOUT_S = 1800
#: 数据就绪轮询间隔秒数
_WAIT_POLL_S = 3.0

_EVENT_LABELS = {  # 与 GUI _AnalysisWorker 一致的中文文案
    OrchestratorEvent.Stage1Started: "阶段一分析中",
    OrchestratorEvent.Stage1Retry: "阶段一重试",
    OrchestratorEvent.Stage1Done: "阶段一完成",
    OrchestratorEvent.Stage2Started: "阶段二分析中",
    OrchestratorEvent.Stage2Retry: "阶段二重试",
    OrchestratorEvent.Stage2Done: "阶段二完成",
    OrchestratorEvent.RecordSaved: "记录已保存",
    OrchestratorEvent.Cancelled: "已取消",
    OrchestratorEvent.Stage1Failed: "阶段一失败",
    OrchestratorEvent.Stage2Failed: "阶段二失败",
    OrchestratorEvent.InsufficientData: "数据不足",
}


def run_symbol_analysis(
    ctx: Any,
    state: ServerState,
    symbol: str,
    timeframe: str,
    *,
    cancel_token: CancelToken | None = None,
    round_num: int = 1,
    idx: int = 0,
    total: int = 1,
) -> dict[str, Any]:
    """阻塞执行一次完整单品种分析，返回结果摘要（异常不外抛）.

    摘要键：``ts, ok, direction, order_type, confidence, has_order, error``。
    执行中通过 *state* 更新阶段与事件日志，结束时写入 symbol result。
    """
    cancel_token = cancel_token or CancelToken()
    summary: dict[str, Any] = {
        "ts": time.time(),
        "ok": False,
        "direction": None,
        "order_type": None,
        "confidence": None,
        "has_order": False,
        "error": None,
    }
    try:
        settings = ctx.settings
        bar_count = int(getattr(settings.general, "analysis_bar_count", 100))

        state.set_current(symbol, "waiting_data", round_num, idx, total)
        state.add_event(f"{symbol} 等待数据就绪")
        frame = _wait_frame(ctx, symbol, timeframe, bar_count)

        state.set_current(symbol, "stage1", round_num, idx, total)
        record = _submit_with_watchdog(
            ctx, state, frame, cancel_token, symbol, round_num, idx, total
        )

        stage2_full = getattr(record, "stage2_decision", None) or {}
        inner = stage2_full.get("decision") or {}
        threshold = int(
            getattr(settings.general, "decision_confidence_threshold", 0) or 0
        )
        summary["direction"] = inner.get("order_direction")
        summary["order_type"] = inner.get("order_type")
        summary["confidence"] = _parse_trade_confidence(inner) if inner else None
        summary["has_order"] = has_order_opportunity(
            inner, confidence_threshold=threshold
        )
        summary["ok"] = True

        if summary["has_order"]:
            state.set_current(symbol, "notifying", round_num, idx, total)
            state.add_event(
                f"{symbol} 发现下单机会（{inner.get('order_direction') or '—'} "
                f"{inner.get('order_type') or '—'}），推送通知"
            )
            _notify_order(
                ctx, inner, stage2_full, symbol, timeframe, frame, record, state
            )
        state.set_current(symbol, "done", round_num, idx, total)
        state.add_event(f"{symbol} 分析完成")
    except Exception as exc:  # noqa: BLE001 — 单品种失败不得中断轮巡
        summary["error"] = str(exc)
        ctx.logger.warning("%s 分析失败: %s", symbol, exc, exc_info=True)
        state.add_event(f"{symbol} 分析失败：{exc}")
    finally:
        summary["ts"] = time.time()
        state.set_symbol_result(symbol, summary)
    return summary


def _wait_frame(ctx: Any, symbol: str, timeframe: str, bar_count: int) -> Any:
    """订阅并轮询数据源，直到能构建完整分析快照或超时."""
    from pa_agent.data.snapshot import INDICATOR_WARMUP_BARS, build_display_frame
    from pa_agent.util.timefmt import now_local_ms

    ds = ctx.data_source
    if ds is None:
        raise RuntimeError("数据源未初始化")
    if not getattr(ds, "_connected", False):
        ds.connect()
    ds.subscribe(symbol, timeframe)

    need = bar_count + INDICATOR_WARMUP_BARS + 1
    deadline = time.monotonic() + DATA_READY_TIMEOUT_S
    last_err: Exception | None = None
    while True:
        try:
            bars = ds.latest_snapshot(need)
            frame = build_display_frame(
                bars, bar_count, symbol, timeframe, now_ms=now_local_ms()
            )
            if frame is not None:
                return frame
        except Exception as exc:  # noqa: BLE001 — 网络抖动继续重试到超时
            last_err = exc
        if time.monotonic() >= deadline:
            detail = f"（最后错误：{last_err}）" if last_err else ""
            raise TimeoutError(
                f"{symbol} 数据未就绪（{DATA_READY_TIMEOUT_S}s 超时）{detail}"
            )
        time.sleep(_WAIT_POLL_S)


def _submit_with_watchdog(
    ctx: Any,
    state: ServerState,
    frame: Any,
    cancel_token: CancelToken,
    symbol: str,
    round_num: int,
    idx: int,
    total: int,
) -> Any:
    """在内层线程执行 orchestrator.submit，超时则取消并抛 TimeoutError."""
    orch = build_orchestrator(ctx)
    result_box: dict[str, Any] = {}

    def on_event(event: OrchestratorEvent) -> None:
        label = _EVENT_LABELS.get(event, str(event))
        state.add_event(f"{symbol} {label}")
        if event == OrchestratorEvent.Stage1Started:
            state.set_current(symbol, "stage1", round_num, idx, total)
        elif event == OrchestratorEvent.Stage2Started:
            state.set_current(symbol, "stage2", round_num, idx, total)

    def _run() -> None:
        try:
            result_box["record"] = orch.submit(frame, cancel_token, on_event)
        except Exception as exc:  # noqa: BLE001 — 转交外层线程重新抛出
            result_box["exc"] = exc

    worker = threading.Thread(
        target=_run, name=f"analysis-{symbol}", daemon=True
    )
    worker.start()
    worker.join(ANALYSIS_TIMEOUT_S)
    if worker.is_alive():
        cancel_token.set()
        worker.join(30)
        raise TimeoutError(f"分析超时（>{ANALYSIS_TIMEOUT_S}s），已请求取消")
    if "exc" in result_box:
        raise result_box["exc"]
    return result_box["record"]


def _notify_order(
    ctx: Any,
    inner: dict[str, Any],
    stage2_full: dict[str, Any],
    symbol: str,
    timeframe: str,
    frame: Any,
    record: Any,
    state: ServerState,
) -> None:
    """交易记录落盘（含图表 PNG）+ 飞书/PushPlus 推送.

    与 GUI `_spawn_post_order_followup` 行为一致，但同步执行（调用方已在
    后台线程）。任何失败只记事件日志，不影响轮巡。
    """
    settings = ctx.settings
    stage1_diag = getattr(record, "stage1_diagnosis", None)
    try:
        from pa_agent.records.trade_logger import save_trade_record

        save_trade_record(
            decision_inner=inner,
            stage2_full=stage2_full,
            stage1_diagnosis=stage1_diag if isinstance(stage1_diag, dict) else None,
            frame=frame,
            meta_symbol=symbol,
            meta_timeframe=timeframe,
            decision_stance=getattr(settings.general, "decision_stance", "") or "",
            model_name=getattr(settings.provider, "model", "") or "",
            structure_flip_cooldown_bars=int(
                getattr(settings.general, "structure_flip_cooldown_bars", 3) or 3
            ),
        )
    except Exception as exc:  # noqa: BLE001
        ctx.logger.warning("交易记录落盘失败: %s", exc)

    try:
        from pa_agent.notify.feishu_notifier import (
            send_order_signal as send_feishu_order,
        )
        from pa_agent.notify.pushplus_notifier import (
            pushplus_is_active,
            send_order_signal as send_pushplus_order,
        )
        from pa_agent.records.trade_logger import _TRADE_RECORDS_DIR

        safe_sym = symbol.replace("/", "-").replace("\\", "-")
        safe_tf = timeframe.replace("/", "-")
        candidates = sorted(
            _TRADE_RECORDS_DIR.glob(f"{safe_sym}_{safe_tf}_*.png"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        latest_img = candidates[0] if candidates else None

        send_feishu_order(
            decision_inner=inner,
            stage2_full=stage2_full,
            symbol=symbol,
            timeframe=timeframe,
            chart_image_path=latest_img,
            settings=settings,
        )
        if pushplus_is_active(settings):
            send_pushplus_order(
                decision_inner=inner,
                stage2_full=stage2_full,
                symbol=symbol,
                timeframe=timeframe,
                settings=settings,
            )
    except Exception as exc:  # noqa: BLE001
        ctx.logger.warning("下单信号通知失败（不影响主流程）: %s", exc)
        state.add_event(f"{symbol} 推送失败：{exc}")
