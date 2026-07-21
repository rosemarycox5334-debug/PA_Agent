"""REST API 测试（TestClient + 真实 Settings + tmp 目录）."""
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from pa_agent.config.settings import Settings, save_settings
    from pa_agent.server import api as api_mod
    from pa_agent.server.bootstrap import ServerContext

    settings = Settings()
    settings.provider.api_key = "sk-1234567890abcdef"
    settings.general.watch_symbols = "XAUUSD"
    sp = tmp_path / "settings.json"
    save_settings(settings, sp)
    pending = tmp_path / "pending"
    pending.mkdir()
    monkeypatch.setattr(api_mod, "RECORDS_PENDING_DIR", pending)
    ctx = ServerContext(
        settings=settings,
        logger=logging.getLogger("t"),
        data_source=None,
        client=None,
        assembler=None,
        router=None,
        validator=None,
        pending_writer=None,
        exp_reader=None,
    )
    app = api_mod.create_app(ctx, settings_path=sp)
    return TestClient(app), pending, sp


def _write_record(pending: Path, name: str, *, symbol="XAUUSD", timeframe="15m",
                  order_type="限价单", exception=None):
    rec = {
        "meta": {
            "timestamp_local_iso": "2026-07-22T10:00:00",
            "timestamp_local_ms": 1786500000000,
            "symbol": symbol,
            "timeframe": timeframe,
            "bar_count": 100,
            "ai_provider": {},
            "decision_stance": "conservative",
        },
        "exception": exception,
        "stage2_decision": {
            "decision": {
                "order_type": order_type,
                "order_direction": "做多",
                "trade_confidence": 70,
            }
        },
    }
    (pending / name).write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")


def test_settings_get_masks_secret(client):
    c, _, _ = client
    data = c.get("/api/settings").json()
    assert "sk-1234567890abcdef" not in json.dumps(data)
    assert data["provider"]["api_key"].endswith("cdef")
    assert "api_key_encrypted" not in data["provider"]


def test_settings_put_empty_secret_keeps_old(client):
    c, _, sp = client
    data = c.get("/api/settings").json()
    data["provider"]["api_key"] = ""  # 空 = 不修改
    data["general"]["watch_symbols"] = "BTCUSD"
    resp = c.put("/api/settings", json=data)
    assert resp.status_code == 200, resp.text
    saved = json.loads(Path(sp).read_text(encoding="utf-8"))
    assert saved["provider"]["api_key"] == "sk-1234567890abcdef"
    assert saved["general"]["watch_symbols"] == "BTCUSD"


def test_settings_put_masked_secret_keeps_old(client):
    c, _, sp = client
    data = c.get("/api/settings").json()
    # 前端把 GET 到的掩码原样回传 → 也应保留旧值
    resp = c.put("/api/settings", json=data)
    assert resp.status_code == 200
    saved = json.loads(Path(sp).read_text(encoding="utf-8"))
    assert saved["provider"]["api_key"] == "sk-1234567890abcdef"


def test_settings_put_new_secret_overwrites(client):
    c, _, sp = client
    data = c.get("/api/settings").json()
    data["provider"]["api_key"] = "sk-NEW"
    c.put("/api/settings", json=data)
    saved = json.loads(Path(sp).read_text(encoding="utf-8"))
    assert saved["provider"]["api_key"] == "sk-NEW"


def test_settings_put_invalid_rejected(client):
    c, _, _ = client
    data = c.get("/api/settings").json()
    data["general"]["analysis_bar_count"] = 999999  # 超出 le=5000
    assert c.put("/api/settings", json=data).status_code == 422


def test_status_shape(client):
    c, _, _ = client
    s = c.get("/api/status").json()
    assert {"scheduler", "current", "results", "events", "round_wait_eta"} <= set(s)


def test_records_list_filter_and_detail(client):
    c, pending, _ = client
    _write_record(pending, "20260722_100000_XAUUSD_15m.json")
    _write_record(pending, "20260722_110000_BTCUSD_15m.json", symbol="BTCUSD",
                  order_type="观望")
    _write_record(pending, "20260722_120000_ETHUSD_15m.json", symbol="ETHUSD",
                  exception={"type": "network_error"})

    body = c.get("/api/records").json()
    assert body["total"] == 3
    names = [i["symbol"] for i in body["items"]]
    assert names == ["ETHUSD", "BTCUSD", "XAUUSD"]  # 按文件名倒序
    eth = body["items"][0]
    assert eth["ok"] is False  # 有 exception
    xau = body["items"][2]
    assert xau["has_order"] is True and xau["direction"] == "做多"
    btc = body["items"][1]
    assert btc["has_order"] is False

    only_btc = c.get("/api/records", params={"symbol": "BTCUSD"}).json()
    assert only_btc["total"] == 1 and only_btc["items"][0]["symbol"] == "BTCUSD"

    detail = c.get(f"/api/records/{body['items'][2]['name']}").json()
    assert detail["stage2_decision"]["decision"]["order_direction"] == "做多"


def test_records_path_traversal_rejected(client):
    c, _, _ = client
    assert c.get("/api/records/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404, 422)
    assert c.get("/api/records/%2e%2e%2fsettings.json").status_code in (400, 404, 422)


def test_watch_start_stop(client, monkeypatch):
    c, _, _ = client
    from pa_agent.server import scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "run_symbol_analysis", lambda *a, **k: {"ok": True})
    r = c.post("/api/watch/start")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert c.get("/api/status").json()["scheduler"]["running"] is True
    c.post("/api/watch/stop")
    assert c.get("/api/status").json()["scheduler"]["running"] is False


def test_watch_start_without_symbols_400(client):
    c, _, sp = client
    data = c.get("/api/settings").json()
    data["general"]["watch_symbols"] = ""
    c.put("/api/settings", json=data)
    r = c.post("/api/watch/start")
    assert r.status_code == 400 and "监控列表" in r.json()["error"]


def test_feishu_test_uses_form_values(client, monkeypatch):
    """测试按钮直接用表单传来的 webhook（未保存配置也能测通）."""
    c, _, _ = client
    captured = {}

    class _FakeResp:
        def json(self):
            return {"code": 0, "msg": "success"}

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        return _FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    r = c.post(
        "/api/feishu/test",
        json={"webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/test-x"},
    )
    assert r.json()["ok"] is True
    assert captured["url"].endswith("/hook/test-x")


def test_feishu_test_without_webhook_reports_missing(client):
    c, _, _ = client
    r = c.post("/api/feishu/test")
    assert r.json()["ok"] is False and "Webhook" in r.json()["detail"]


def test_settings_put_garbage_section_ignored(client):
    """段位传标量（如 provider: 'haha'）不得整段覆盖或 500."""
    c, _, sp = client
    resp = c.put("/api/settings", json={"provider": "haha", "feishu": 123})
    assert resp.status_code == 200
    saved = json.loads(Path(sp).read_text(encoding="utf-8"))
    assert saved["provider"]["api_key"] == "sk-1234567890abcdef"


def test_watch_start_blocked_by_manual_analysis(client, monkeypatch):
    """手动分析进行中不得启动轮巡（互斥双向生效）."""
    import time

    from pa_agent.server import api as api_mod

    c, _, _ = client
    monkeypatch.setattr(
        api_mod, "run_symbol_analysis", lambda *a, **k: time.sleep(1.0) or {"ok": True}
    )
    assert c.post("/api/analyze", json={"symbol": "XAUUSD"}).status_code == 200
    r = c.post("/api/watch/start")
    assert r.status_code == 409 and "手动分析" in r.json()["error"]


def test_settings_ds_change_rejected_while_watch_running(client, monkeypatch):
    """轮巡运行中切换数据源必须被 409 拒绝（数据源被分析线程持有）."""
    import time

    from pa_agent.server import scheduler as sched_mod

    c, _, _ = client
    monkeypatch.setattr(
        sched_mod,
        "run_symbol_analysis",
        lambda *a, **k: time.sleep(0.5) or {"ok": True},
    )
    c.post("/api/watch/start")
    data = c.get("/api/settings").json()
    data["general"]["last_data_source"] = "akshare"
    r = c.put("/api/settings", json=data)
    assert r.status_code == 409
    c.post("/api/watch/stop")


def test_analyze_conflicts_with_running_watch(client, monkeypatch):
    c, _, _ = client
    import time

    from pa_agent.server import scheduler as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "run_symbol_analysis",
        lambda *a, **k: time.sleep(0.5) or {"ok": True},
    )
    c.post("/api/watch/start")
    r = c.post("/api/analyze", json={"symbol": "BTCUSD"})
    assert r.status_code == 409
    c.post("/api/watch/stop")
