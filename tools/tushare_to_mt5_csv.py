"""Export Tushare A-share bars to a MetaTrader 5 custom-symbol CSV."""
from __future__ import annotations

import argparse
import csv
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


_DATE_RE = re.compile(r"^\d{8}$")
_TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$", re.IGNORECASE)


@dataclass(frozen=True)
class ExportRow:
    date: str
    time: str
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    volume: int
    spread: int = 0


def normalize_ts_code(value: str) -> str:
    """Normalize common A-share inputs to Tushare ts_code."""
    raw = (value or "").strip().upper()
    if _TS_CODE_RE.match(raw):
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) != 6:
        raise ValueError("股票代码必须是 6 位数字，或 Tushare 格式如 600519.SH")
    if digits.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return f"{digits}.SH"
    if digits.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return f"{digits}.SZ"
    if digits.startswith(("8", "4")):
        return f"{digits}.BJ"
    raise ValueError(f"无法自动判断交易所，请使用完整 ts_code，如 {digits}.SH 或 {digits}.SZ")


def validate_yyyymmdd(value: str, *, name: str) -> str:
    text = (value or "").strip()
    if not _DATE_RE.match(text):
        raise ValueError(f"{name} 必须是 YYYYMMDD 格式")
    datetime.strptime(text, "%Y%m%d")
    return text


def _mt5_date(value: object) -> str:
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise ValueError(f"无法解析交易日期: {value!r}")
    return f"{text[0:4]}.{text[4:6]}.{text[6:8]}"


def _row_number(row: object, key: str, default: float = 0.0) -> float:
    try:
        value = row[key]  # type: ignore[index]
    except Exception:
        return default
    if value is None:
        return default
    try:
        if value != value:
            return default
    except Exception:
        pass
    return float(value)


def dataframe_to_mt5_rows(df: object) -> list[ExportRow]:
    """Convert a Tushare OHLCV DataFrame to ascending MT5 export rows."""
    try:
        empty = bool(df.empty)  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise TypeError("df must be a pandas DataFrame") from exc
    if empty:
        return []

    out = df.copy()  # type: ignore[attr-defined]
    if "trade_date" not in out.columns:
        raise ValueError("Tushare 返回数据缺少 trade_date 字段")
    required = {"open", "high", "low", "close"}
    missing = sorted(required - set(out.columns))
    if missing:
        raise ValueError(f"Tushare 返回数据缺少字段: {', '.join(missing)}")

    out = out.sort_values("trade_date").reset_index(drop=True)
    rows: list[ExportRow] = []
    for _, row in out.iterrows():
        vol_hands = max(0.0, _row_number(row, "vol"))
        rows.append(
            ExportRow(
                date=_mt5_date(row["trade_date"]),
                time="00:00:00",
                open=_row_number(row, "open"),
                high=_row_number(row, "high"),
                low=_row_number(row, "low"),
                close=_row_number(row, "close"),
                tick_volume=int(round(vol_hands)),
                volume=int(round(vol_hands * 100)),
                spread=0,
            )
        )
    return rows


def write_mt5_csv(rows: Iterable[ExportRow], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow(
                [
                    row.date,
                    row.time,
                    f"{row.open:.6f}".rstrip("0").rstrip("."),
                    f"{row.high:.6f}".rstrip("0").rstrip("."),
                    f"{row.low:.6f}".rstrip("0").rstrip("."),
                    f"{row.close:.6f}".rstrip("0").rstrip("."),
                    row.tick_volume,
                    row.volume,
                    row.spread,
                ]
            )
            count += 1
    return count


def fetch_tushare_daily(
    *,
    token: str,
    ts_code: str,
    start_date: str,
    end_date: str,
    adj: str,
) -> object:
    import tushare as ts

    ts.set_token(token)
    if adj == "none":
        pro = ts.pro_api(token)
        return pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    return ts.pro_bar(
        ts_code=ts_code,
        asset="E",
        adj=adj,
        freq="D",
        start_date=start_date,
        end_date=end_date,
    )


def _default_out_path(ts_code: str, start_date: str, end_date: str, adj: str) -> Path:
    safe = ts_code.replace(".", "_")
    return Path("data") / "tushare" / f"{safe}_D_{adj}_{start_date}_{end_date}_mt5.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Tushare A-share daily bars and export MT5 import CSV."
    )
    parser.add_argument("--symbol", required=True, help="A 股代码，如 600519 或 600519.SH")
    parser.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument(
        "--adj",
        choices=("none", "qfq", "hfq"),
        default="qfq",
        help="复权方式：none 未复权，qfq 前复权，hfq 后复权。默认 qfq",
    )
    parser.add_argument("--out", help="输出 CSV 路径。默认 data/tushare/..._mt5.csv")
    parser.add_argument(
        "--raw-out",
        help="可选：同时保存 Tushare 原始 CSV，便于核查字段和价格",
    )
    parser.add_argument(
        "--token",
        help="Tushare token。建议改用环境变量 TUSHARE_TOKEN，避免命令历史泄露",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = (args.token or os.environ.get("TUSHARE_TOKEN") or "").strip()
    if not token:
        raise SystemExit("缺少 TUSHARE_TOKEN。请先设置环境变量，或临时传 --token。")

    ts_code = normalize_ts_code(args.symbol)
    start_date = validate_yyyymmdd(args.start, name="--start")
    end_date = validate_yyyymmdd(args.end, name="--end")
    if start_date > end_date:
        raise SystemExit("--start 不能晚于 --end")

    df = fetch_tushare_daily(
        token=token,
        ts_code=ts_code,
        start_date=start_date,
        end_date=end_date,
        adj=args.adj,
    )
    rows = dataframe_to_mt5_rows(df)
    if not rows:
        raise SystemExit(f"Tushare 未返回数据: {ts_code} {start_date}-{end_date}")

    out_path = (
        Path(args.out)
        if args.out
        else _default_out_path(ts_code, start_date, end_date, args.adj)
    )
    count = write_mt5_csv(rows, out_path)

    if args.raw_out:
        raw_path = Path(args.raw_out)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        df.sort_values("trade_date").to_csv(raw_path, index=False, encoding="utf-8-sig")

    print(f"OK: {ts_code} {start_date}-{end_date} {args.adj} -> {out_path} ({count} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
