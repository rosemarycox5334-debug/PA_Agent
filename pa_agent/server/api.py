"""服务端 REST API：配置 / 状态 / 历史 / 轮巡控制 + 静态前端托管."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pa_agent.config.paths import RECORDS_PENDING_DIR  # noqa: F401  (供测试 patch)
from pa_agent.notify.order_opportunity import has_order_opportunity
from pa_agent.server.bootstrap import (
    ServerContext,
    rebuild_client,
    rebuild_data_source,
)
from pa_agent.server.scheduler import WatchScheduler
from pa_agent.server.service import run_symbol_analysis
from pa_agent.server.state import ServerState
from pa_agent.util.mask_secret import mask_secret

#: (段名, 字段名) —— GET 返回掩码、PUT 空串/掩码原样回传 = 保留旧值
SECRET_FIELDS: tuple[tuple[str, str], ...] = (
    ("provider", "api_key"),
    ("feishu", "secret"),
    ("feishu", "app_secret"),
    ("pushplus", "token"),
    ("tushare", "token"),
)

_RECORD_NAME_RE = re.compile(r"^[\w\-.]+\.json$")


def masked_settings_dict(settings: Any) -> dict[str, Any]:
    """全量配置 dict，敏感字段脱敏，剔除内部加密字段."""
    data = settings.model_dump(mode="json")
    for section, field in SECRET_FIELDS:
        value = data.get(section, {}).get(field) or ""
        if value:
            data[section][field] = mask_secret(value)
    data.get("provider", {}).pop("api_key_encrypted", None)
    return data


def apply_settings_update(settings: Any, payload: dict[str, Any]) -> Any:
    """把前端提交的配置合并进现有 Settings 并校验.

    敏感字段规则：空串或与当前掩码值相同 → 保留旧值不覆盖。
    """
    from pa_agent.config.settings import Settings

    current = settings.model_dump(mode="json")
    merged = _deep_merge(current, payload)
    for section, field in SECRET_FIELDS:
        old = current.get(section, {}).get(field) or ""
        new = merged.get(section, {}).get(field) or ""
        if old and (not new or new == mask_secret(old)):
            merged[section][field] = old
    # 内部字段不接受前端覆盖
    if "provider" in merged and isinstance(merged["provider"], dict):
        merged["provider"]["api_key_encrypted"] = current.get("provider", {}).get(
            "api_key_encrypted", ""
        )
    return Settings.model_validate(merged)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _send_feishu_test(settings: Any) -> tuple[bool, str]:
    """向飞书 webhook 发送一条测试文本消息."""
    import time as _time

    webhook = (getattr(settings.feishu, "webhook_url", "") or "").strip()
    if not webhook:
        return False, "未配置 webhook_url"
    try:
        import requests

        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": "✅ PA Agent 服务端测试消息：飞书通知配置正常。"},
        }
        secret = (getattr(settings.feishu, "secret", "") or "").strip()
        if secret:
            from pa_agent.notify.feishu_notifier import _gen_sign

            ts = int(_time.time())
            payload["timestamp"] = str(ts)
            payload["sign"] = _gen_sign(secret, ts)
        resp = requests.post(webhook, json=payload, timeout=10)
        body = resp.json()
        ok = body.get("code") == 0 or body.get("StatusCode") == 0
        return ok, json.dumps(body, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return False, f"发送失败：{exc}"


def _record_summary(path: Path) -> dict[str, Any] | None:
    """从 pending 记录文件提取列表摘要；解析失败返回 None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    meta = data.get("meta") or {}
    stage2 = data.get("stage2_decision") or {}
    inner = stage2.get("decision") or {}
    return {
        "name": path.name,
        "ts_ms": meta.get("timestamp_local_ms"),
        "symbol": meta.get("symbol") or "",
        "timeframe": meta.get("timeframe") or "",
        "ok": data.get("exception") in (None, {}) and bool(stage2),
        "direction": inner.get("order_direction"),
        "order_type": inner.get("order_type"),
        "confidence": inner.get("trade_confidence"),
        "has_order": has_order_opportunity(inner),
    }


def create_app(ctx: ServerContext, *, settings_path: Path | None = None) -> FastAPI:
    """构建 FastAPI 应用（含调度器与状态实例）."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import save_settings

    save_path = settings_path or SETTINGS_JSON_PATH
    state = ServerState()
    scheduler = WatchScheduler(ctx, state)
    app = FastAPI(title="PA Agent 服务端", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.state.server_state = state
    app.state.scheduler = scheduler
    manual_lock = threading.Lock()

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return state.snapshot()

    @app.post("/api/watch/start")
    def api_watch_start() -> JSONResponse:
        err = scheduler.start()
        if err:
            return JSONResponse(status_code=400, content={"ok": False, "error": err})
        return JSONResponse(content={"ok": True})

    @app.post("/api/watch/stop")
    def api_watch_stop() -> dict[str, Any]:
        scheduler.stop()
        return {"ok": True}

    @app.post("/api/analyze")
    def api_analyze(body: dict[str, Any]) -> JSONResponse:
        symbol = str(body.get("symbol") or "").strip()
        if not symbol:
            return JSONResponse(
                status_code=400, content={"ok": False, "error": "symbol 不能为空"}
            )
        if scheduler.running:
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "轮巡运行中，请先停止再手动分析"},
            )
        if not manual_lock.acquire(blocking=False):
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "已有手动分析在进行中"},
            )
        timeframe = (
            str(body.get("timeframe") or "").strip()
            or getattr(ctx.settings.general, "last_timeframe", "15m")
        )

        def _run() -> None:
            try:
                run_symbol_analysis(ctx, state, symbol, timeframe)
            finally:
                manual_lock.release()

        threading.Thread(target=_run, name=f"manual-{symbol}", daemon=True).start()
        return JSONResponse(content={"ok": True, "message": f"已开始分析 {symbol}"})

    @app.get("/api/settings")
    def api_settings_get() -> dict[str, Any]:
        return masked_settings_dict(ctx.settings)

    @app.put("/api/settings")
    def api_settings_put(payload: dict[str, Any]) -> JSONResponse:
        from pydantic import ValidationError

        old = ctx.settings
        try:
            new_settings = apply_settings_update(old, payload)
        except ValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={"ok": False, "error": "配置校验失败", "detail": exc.errors()},
            )
        save_settings(new_settings, save_path)
        ctx.settings = new_settings
        try:
            rebuild_client(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.logger.warning("LLM 客户端重建失败: %s", exc)
        ds_changed = (
            getattr(old.general, "last_data_source", "")
            != getattr(new_settings.general, "last_data_source", "")
            or getattr(old.general, "last_tradingview_exchange", "")
            != getattr(new_settings.general, "last_tradingview_exchange", "")
        )
        if ds_changed:
            try:
                rebuild_data_source(ctx)
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("数据源重建失败: %s", exc)
        state.add_event("配置已更新" + ("（数据源已切换）" if ds_changed else ""))
        return JSONResponse(content={"ok": True})

    @app.post("/api/feishu/test")
    def api_feishu_test() -> dict[str, Any]:
        ok, detail = _send_feishu_test(ctx.settings)
        return {"ok": ok, "detail": detail}

    @app.get("/api/records")
    def api_records(symbol: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        pending_dir: Path = RECORDS_PENDING_DIR
        paths = sorted(pending_dir.glob("*.json"), key=lambda p: p.name, reverse=True)
        summaries = [s for p in paths if (s := _record_summary(p)) is not None]
        if symbol.strip():
            want = symbol.strip().upper()
            summaries = [s for s in summaries if s["symbol"].upper() == want]
        total = len(summaries)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        return {"total": total, "items": summaries[offset : offset + limit]}

    @app.get("/api/records/{name}")
    def api_record_detail(name: str, request: Request) -> dict[str, Any]:
        pending_dir: Path = RECORDS_PENDING_DIR
        if not _RECORD_NAME_RE.match(name) or ".." in name:
            raise HTTPException(status_code=400, detail="非法记录名")
        path = (pending_dir / name).resolve()
        if path.parent != pending_dir.resolve() or not path.is_file():
            raise HTTPException(status_code=404, detail="记录不存在")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise HTTPException(status_code=500, detail=f"记录读取失败：{exc}") from exc

    static_dir = Path(__file__).parent / "static"
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

    return app
