from __future__ import annotations

from dataclasses import dataclass

from pa_agent.gui.support_resistance import (
    extract_structure_levels,
    filter_levels_near_price,
    format_level,
)


@dataclass(frozen=True)
class _Bar:
    high: float
    low: float


def test_extracts_levels_from_structured_fields() -> None:
    payload = {
        "diagnosis_summary": {
            "support_levels": [{"low": 4220, "high": 4230}],
            "resistance_levels": [4306],
        }
    }

    levels = extract_structure_levels(payload)

    assert [(lvl.kind, lvl.low, lvl.high) for lvl in levels] == [
        ("support", 4220.0, 4230.0),
        ("resistance", 4306.0, 4306.0),
    ]


def test_extracts_levels_from_chinese_reasoning_text() -> None:
    payload = {
        "decision": {
            "reasoning": "若回撤，支撑依次为4270-4280、4250-4260。上方阻力在4306。"
        }
    }

    levels = extract_structure_levels(payload)

    assert [(lvl.kind, format_level(lvl)) for lvl in levels] == [
        ("support", "4270-4280"),
        ("support", "4250-4260"),
        ("resistance", "4306"),
    ]


def test_dedupes_repeated_levels() -> None:
    payload = {
        "support_zone": "4220-4230",
        "reasoning": "支撑区4220-4230仍有效。",
    }

    levels = extract_structure_levels(payload)

    assert len(levels) == 1
    assert levels[0].kind == "support"
    assert format_level(levels[0]) == "4220-4230"


def test_format_level_avoids_scientific_notation_for_large_prices() -> None:
    """Futures prices around 100000 must stay readable in GUI labels."""
    levels = extract_structure_levels(
        {"reasoning": "上方阻力在106720，支撑区106390-106620。"},
        max_levels_per_kind=10,
    )

    formatted = [(lvl.kind, format_level(lvl)) for lvl in levels]

    assert ("resistance", "106720") in formatted
    assert ("support", "106390-106620") in formatted
    assert all("e+" not in text and "e-" not in text for _, text in formatted)


def test_ignores_percentages_and_k_numbers_in_reasoning_text() -> None:
    payload = {
        "next_bar_prediction": {
            "probabilities": {"bullish": 38, "bearish": 27, "neutral": 35},
            "reasoning": (
                "当前市场处于交易区间下边界（1205），没有有效跌破（K5/K4/K2/K1低点均为1205）。"
                "买盘承接，K1-K4的barbwire形态表明多空在该区域激烈争夺但方向未定。"
                "1205支撑多次测试有效，交易区间中80%的突破尝试失败，价格从下边界反弹的统计概率较高。"
                "综合判断，55-60只是胜率描述，不是支撑价位。"
            ),
        }
    }

    levels = extract_structure_levels(payload, max_levels_per_kind=10)

    formatted = [(lvl.kind, format_level(lvl)) for lvl in levels]
    assert ("support", "1205") in formatted
    assert ("support", "80") not in formatted
    assert ("support", "55-60") not in formatted
    assert ("support", "1") not in formatted


def test_filters_levels_outside_current_price_neighborhood() -> None:
    levels = extract_structure_levels(
        {
            "reasoning": "1205支撑有效，支撑还误写了89和55-60。上方阻力误写20。",
        },
        max_levels_per_kind=10,
    )
    bars = [_Bar(high=1220.0, low=1198.0), _Bar(high=1216.0, low=1204.0)]

    filtered = filter_levels_near_price(levels, bars, max_levels_per_kind=10)

    assert [(lvl.kind, format_level(lvl)) for lvl in filtered] == [
        ("support", "1205")
    ]


def test_filters_single_digit_support_when_current_price_is_thousands() -> None:
    """A stray section number like §2 must not flatten the chart price axis."""
    levels = extract_structure_levels(
        {
            "decision": {
                "reasoning": "宽通道顺势H2突破单做多。下方支撑误写为2，上方阻力在4416。"
            }
        },
        max_levels_per_kind=10,
    )
    bars = [
        _Bar(high=4460.0, low=4360.0),
        _Bar(high=4435.0, low=4320.0),
        _Bar(high=4200.0, low=2.0),  # stale/outlier row must not expand the accepted band
    ]

    filtered = filter_levels_near_price(levels, bars, max_levels_per_kind=10)

    assert ("support", "2") not in [(lvl.kind, format_level(lvl)) for lvl in filtered]
    assert ("resistance", "4416") in [(lvl.kind, format_level(lvl)) for lvl in filtered]
