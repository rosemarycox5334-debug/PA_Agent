"""服务端 REST API：配置 / 状态 / 历史 / 轮巡控制 + 静态前端托管."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import Body as FastAPIBody
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from pa_agent.config.paths import RECORDS_PENDING_DIR  # noqa: F401  (供测试 patch)
from pa_agent.notify.order_opportunity import has_order_opportunity
from pa_agent.server.bootstrap import ServerContext, rebuild_engine
from pa_agent.server.ds_pool import DataSourcePool
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
        elif isinstance(out.get(key), dict):
            continue  # 段位是 dict 而 patch 给了标量：拒绝整段覆盖
        else:
            out[key] = value
    return out


def _send_feishu_test(
    settings: Any,
    *,
    webhook_override: str | None = None,
    secret_override: str | None = None,
) -> tuple[bool, str]:
    """向飞书 webhook 发送一条测试文本消息.

    override 参数来自前端表单当前值（未保存也可测试）；为空时回落到已保存配置。
    """
    import time as _time

    webhook = (
        webhook_override or getattr(settings.feishu, "webhook_url", "") or ""
    ).strip()
    if not webhook:
        return False, "未填写 Webhook 地址"
    try:
        import requests

        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": "✅ PA Agent 服务端测试消息：飞书通知配置正常。"},
        }
        secret = (
            secret_override or getattr(settings.feishu, "secret", "") or ""
        ).strip()
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


def _record_summary(
    path: Path, confidence_threshold: int | None = None
) -> dict[str, Any] | None:
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
        "has_order": has_order_opportunity(
            inner, confidence_threshold=confidence_threshold
        ),
    }


def _symbol_from_record_name(name: str) -> str:
    """从文件名 {日期}_{时间}_{symbol}_{周期}.json 提取品种；无法解析返回空串."""
    parts = name.rsplit(".", 1)[0].split("_")
    if len(parts) >= 4:
        return "_".join(parts[2:-1])
    return ""


def create_app(ctx: ServerContext, *, settings_path: Path | None = None) -> FastAPI:
    """构建 FastAPI 应用（含调度器与状态实例）."""
    from pa_agent.config.paths import SETTINGS_JSON_PATH
    from pa_agent.config.settings import save_settings

    save_path = settings_path or SETTINGS_JSON_PATH
    state = ServerState()
    ds_pool = DataSourcePool(ctx.settings, size=8)
    scheduler = WatchScheduler(ctx, state, ds_pool)
    app = FastAPI(title="PA Agent 服务端", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.state.server_state = state
    app.state.scheduler = scheduler
    app.state.ds_pool = ds_pool
    # 手动分析持有此锁直到分析结束；轮巡启动也短暂持有 —— 二者互斥收敛于此
    analysis_lock = threading.Lock()
    settings_lock = threading.Lock()

    @app.get("/api/status")
    def api_status() -> dict[str, Any]:
        return state.snapshot()

    @app.post("/api/watch/start")
    def api_watch_start() -> JSONResponse:
        if not analysis_lock.acquire(blocking=False):
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "手动分析进行中，请等待其完成后再启动轮巡"},
            )
        try:
            err = scheduler.start()
        finally:
            analysis_lock.release()
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
        if not analysis_lock.acquire(blocking=False):
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "已有手动分析在进行中"},
            )
        # 持锁后再确认轮巡状态，封住「检查后启动前」被轮巡插入的窗口
        if scheduler.running:
            analysis_lock.release()
            return JSONResponse(
                status_code=409,
                content={"ok": False, "error": "轮巡运行中，请先停止再手动分析"},
            )
        timeframe = (
            str(body.get("timeframe") or "").strip()
            or getattr(ctx.settings.general, "last_timeframe", "15m")
        )
        try:
            ds = ds_pool.acquire(timeout=5)
        except TimeoutError as exc:
            analysis_lock.release()
            return JSONResponse(
                status_code=409, content={"ok": False, "error": str(exc)}
            )

        def _run() -> None:
            try:
                run_symbol_analysis(ctx, state, symbol, timeframe, data_source=ds)
            finally:
                ds_pool.release(ds)
                analysis_lock.release()

        threading.Thread(target=_run, name=f"manual-{symbol}", daemon=True).start()
        return JSONResponse(content={"ok": True, "message": f"已开始分析 {symbol}"})

    @app.get("/api/settings")
    def api_settings_get() -> dict[str, Any]:
        return masked_settings_dict(ctx.settings)

    @app.put("/api/settings")
    def api_settings_put(payload: dict[str, Any]) -> JSONResponse:
        from pydantic import ValidationError

        with settings_lock:
            old = ctx.settings
            try:
                new_settings = apply_settings_update(old, payload)
            except ValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "ok": False,
                        "error": "配置校验失败",
                        "detail": exc.errors(include_input=False, include_url=False),
                    },
                )
            ds_changed = (
                getattr(old.general, "last_data_source", "")
                != getattr(new_settings.general, "last_data_source", "")
                or getattr(old.general, "last_tradingview_exchange", "")
                != getattr(new_settings.general, "last_tradingview_exchange", "")
            )
            # 数据源被正在运行的分析线程持有引用，运行中热切换会把它踢下线
            if ds_changed and (scheduler.running or analysis_lock.locked()):
                return JSONResponse(
                    status_code=409,
                    content={
                        "ok": False,
                        "error": "分析进行中无法切换数据源，请先停止轮巡（或等手动分析结束）再保存",
                    },
                )
            save_settings(new_settings, save_path)
            ctx.settings = new_settings
            try:
                rebuild_engine(ctx)
            except Exception as exc:  # noqa: BLE001
                ctx.logger.warning("引擎组件重建失败: %s", exc)
            if ds_changed:
                try:
                    ds_pool.rebuild(new_settings)
                except Exception as exc:  # noqa: BLE001
                    ctx.logger.warning("数据源池重建失败: %s", exc)
            state.add_event("配置已更新" + ("（数据源已切换）" if ds_changed else ""))
            return JSONResponse(content={"ok": True})

    @app.post("/api/feishu/test")
    def api_feishu_test(
        body: dict[str, Any] | None = FastAPIBody(default=None),
    ) -> dict[str, Any]:
        body = body or {}
        ok, detail = _send_feishu_test(
            ctx.settings,
            webhook_override=str(body.get("webhook_url") or "").strip() or None,
            secret_override=str(body.get("secret") or "").strip() or None,
        )
        return {"ok": ok, "detail": detail}

    @app.get("/api/live/{symbol}")
    def api_live(symbol: str) -> dict[str, Any]:
        live = state.get_live(symbol)
        if live is None:
            raise HTTPException(status_code=404, detail="该品种尚无分析活动")
        return live

    @app.get("/api/records")
    def api_records(
        symbol: str = "", limit: int = 50, offset: int = 0, latest: int = 0
    ) -> dict[str, Any]:
        if latest:
            if not symbol.strip():
                raise HTTPException(
                    status_code=400, detail="latest=1 时必须携带 symbol 参数"
                )
            limit, offset = 1, 0
        pending_dir: Path = RECORDS_PENDING_DIR
        # 只 stat 排序 + 按文件名过滤品种，仅对当前页的文件做 JSON 解析，
        # 记录量增长后列表接口的开销保持 O(页大小)
        paths = list(pending_dir.glob("*.json"))
        if symbol.strip():
            want = symbol.strip().upper()
            paths = [
                p for p in paths if _symbol_from_record_name(p.name).upper() == want
            ]
        paths.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
        total = len(paths)
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        threshold = int(
            getattr(ctx.settings.general, "decision_confidence_threshold", 0) or 0
        )
        items = [
            s
            for p in paths[offset : offset + limit]
            if (s := _record_summary(p, confidence_threshold=threshold)) is not None
        ]
        return {"total": total, "items": items}

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
