"""LLM 响应缓存：输入逐字节相同时复用上次的模型输出。

**为什么需要它**（实测数据，2026-10-15）：
- 一次完整分析的耗时 ≈ 99% 在模型（阶段一 31.4s + 阶段二 42.7s），本地代码合计仅 ~6ms；
- 其中解码（生成思考+JSON）约 36s、预填充约 7s —— 想提速就只能"少问模型"；
- 实测「无新增K线，基于上一轮结论复核」这种**输入完全相同**的重跑，花了 29s / 215k tokens
  才得出与上一次相同的结论。

缓存键 = sha256(base_url + api 模型名 + 全部消息 + system + thinking/effort/max_tokens)，
即"输入指纹"。任何会改变输出的因素变了（新 K 线、改提示词文件、换模型、改思考档位），
指纹就变，缓存自然失效。

缓存文件落在 ``records/llm_cache/``（`.gitignore` 的 ``records/**/*`` 已覆盖，不入库）。
默认 TTL 1800 秒，最多保留 300 条，超出按写入时间清理最旧的。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pa_agent.util.atomic_write import atomic_write_text

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "records" / "llm_cache"

#: 默认存活时间（秒）；0 表示永不过期
DEFAULT_TTL_S = 1800
#: 最多保留多少条缓存
MAX_ENTRIES = 300


@dataclass(frozen=True)
class CachedResponse:
    """一条可复用的模型响应。"""

    content: str
    reasoning_content: str
    request_id: str
    stored_at: float
    prompt_tokens: int
    completion_tokens: int
    model: str


def make_key(
    *,
    base_url: str,
    model: str,
    api_messages: list[dict[str, Any]],
    system_param: str | None,
    thinking: bool | None,
    effort: str | None,
    max_tokens: int | None,
) -> str:
    """输入指纹：任何影响输出的参数变化都会改变它。"""
    payload = {
        "base_url": base_url or "",
        "model": model or "",
        "system": system_param or "",
        "thinking": bool(thinking),
        "effort": effort or "",
        "max_tokens": int(max_tokens or 0),
        "messages": [
            {"role": str(m.get("role") or ""), "content": str(m.get("content") or "")}
            for m in api_messages
            if isinstance(m, dict)
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path_for(key: str) -> Path:
    return CACHE_DIR / f"{key}.json"


def load(key: str, *, ttl_s: int = DEFAULT_TTL_S) -> CachedResponse | None:
    """读取缓存；过期则删除并返回 None。"""
    path = _path_for(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("LLM 缓存读取失败 %s: %s", key[:12], exc)
        return None
    stored_at = float(data.get("stored_at") or 0.0)
    if ttl_s and stored_at and time.time() - stored_at > ttl_s:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    content = str(data.get("content") or "")
    if not content.strip():
        return None
    return CachedResponse(
        content=content,
        reasoning_content=str(data.get("reasoning_content") or ""),
        request_id=str(data.get("request_id") or ""),
        stored_at=stored_at,
        prompt_tokens=int(data.get("prompt_tokens") or 0),
        completion_tokens=int(data.get("completion_tokens") or 0),
        model=str(data.get("model") or ""),
    )


def store(
    key: str,
    *,
    content: str,
    reasoning_content: str,
    request_id: str,
    prompt_tokens: int,
    completion_tokens: int,
    model: str,
) -> None:
    """写入缓存（原子写）；调用方无需处理失败——缓存永远不影响主流程。"""
    if not content.strip():
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": key,
            "stored_at": time.time(),
            "model": model,
            "request_id": request_id,
            "content": content,
            "reasoning_content": reasoning_content,
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
        }
        atomic_write_text(_path_for(key), json.dumps(payload, ensure_ascii=False))
        _prune()
    except Exception as exc:  # noqa: BLE001 - 缓存失败绝不能影响分析
        logger.debug("LLM 缓存写入失败 %s: %s", key[:12], exc)


def _prune(max_entries: int = MAX_ENTRIES) -> None:
    files = sorted(CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[max_entries:]:
        try:
            old.unlink()
        except OSError:
            pass


def clear() -> int:
    """清空缓存，返回删除的文件数。"""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


def stats() -> dict[str, Any]:
    """缓存概览：条数、占用字节、最旧/最新写入时间。"""
    if not CACHE_DIR.exists():
        return {"entries": 0, "bytes": 0, "oldest_age_s": None, "newest_age_s": None}
    files = list(CACHE_DIR.glob("*.json"))
    if not files:
        return {"entries": 0, "bytes": 0, "oldest_age_s": None, "newest_age_s": None}
    mtimes = [f.stat().st_mtime for f in files]
    now = time.time()
    return {
        "entries": len(files),
        "bytes": sum(f.stat().st_size for f in files),
        "oldest_age_s": round(now - min(mtimes), 1),
        "newest_age_s": round(now - max(mtimes), 1),
    }
