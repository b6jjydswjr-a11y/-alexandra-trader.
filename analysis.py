"""Transparent, rule-based indicators and conservative signal validation."""

from datetime import datetime, timezone

from .models import MarketSnapshot


def _sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = sum(max(change, 0) for change in changes[:period]) / period
    losses = sum(max(-change, 0) for change in changes[:period]) / period
    for change in changes[period:]:
        gains = (gains * (period - 1) + max(change, 0)) / period
        losses = (losses * (period - 1) + max(-change, 0)) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100 - (100 / (1 + gains / losses))


def calculate_indicators(snapshot: MarketSnapshot) -> dict[str, float | None]:
    closes = [candle.close for candle in snapshot.candles]
    if not closes:
        return {
            "last": None,
            "rsi14": None,
            "macd": None,
            "macd_signal": None,
            "macd_histogram": None,
            "sma20": None,
            "sma50": None,
            "momentum10_pct": None,
            "volume_change_pct": None,
        }

    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    macd_values = [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]
    signal_values = _ema_series(macd_values, 9)
    volumes = [candle.volume for candle in snapshot.candles]
    volume_change = None
    if len(volumes) >= 2 and all(volume is not None for volume in volumes[-2:]):
        prior, current = volumes[-2:]
        if prior and current is not None:
            volume_change = ((current - prior) / prior) * 100

    momentum = None
    if len(closes) > 10 and closes[-11] != 0:
        momentum = (closes[-1] / closes[-11] - 1) * 100

    macd = macd_values[-1] if macd_values else None
    signal = signal_values[-1] if signal_values else None
    return {
        "last": closes[-1],
        "rsi14": _rsi(closes),
        "macd": macd,
        "macd_signal": signal,
        "macd_histogram": macd - signal if macd is not None and signal is not None else None,
        "sma20": _sma(closes, 20),
        "sma50": _sma(closes, 50),
        "momentum10_pct": momentum,
        "volume_change_pct": volume_change,
    }


def validate_signal(indicators: dict[str, float | None]) -> dict[str, str | int]:
    """Check a candidate independently; missing or conflicting evidence means NO TRADE."""
    last = indicators["last"]
    sma20 = indicators["sma20"]
    sma50 = indicators["sma50"]
    histogram = indicators["macd_histogram"]
    momentum = indicators["momentum10_pct"]
    rsi = indicators["rsi14"]

    if any(value is None for value in (last, sma20, sma50, histogram, momentum, rsi)):
        return {
            "decision": "NO TRADE",
            "reason": "At least 50 source candles and all required indicators are needed.",
            "rule_alignment_pct": None,
            "directional_votes": None,
        }

    up_votes = int(last > sma20) + int(sma20 > sma50) + int(histogram > 0) + int(momentum > 0)
    down_votes = int(last < sma20) + int(sma20 < sma50) + int(histogram < 0) + int(momentum < 0)
    candidate = "UP" if up_votes >= 3 else "DOWN" if down_votes >= 3 else "NO TRADE"
    score_votes = up_votes if candidate == "UP" else down_votes if candidate == "DOWN" else max(up_votes, down_votes)
    alignment = round(score_votes / 4 * 100)
    if candidate == "NO TRADE":
        return {
            "decision": "NO TRADE",
            "reason": "No direction reached the minimum three-of-four indicator agreement.",
            "rule_alignment_pct": alignment,
            "directional_votes": f"UP {up_votes}/4 · DOWN {down_votes}/4",
        }
    rsi_ok = 35 <= rsi <= 70 if candidate == "UP" else 30 <= rsi <= 65
    if not rsi_ok:
        return {
            "decision": "NO TRADE",
            "reason": "The independent RSI validator rejected an overextended or conflicting candidate.",
            "rule_alignment_pct": alignment,
            "directional_votes": f"UP {up_votes}/4 · DOWN {down_votes}/4",
        }
    return {
        "decision": candidate,
        "reason": "The independent validator accepted at least three aligned checks and RSI within its guard range.",
        "rule_alignment_pct": alignment,
        "directional_votes": f"UP {up_votes}/4 · DOWN {down_votes}/4",
    }


def analyze(snapshot: MarketSnapshot) -> dict:
    indicators = calculate_indicators(snapshot)
    validation = validate_signal(indicators)
    return {
        "symbol": snapshot.symbol,
        "market_type": snapshot.market_type,
        "timeframe": snapshot.timeframe,
        "indicators": indicators,
        "validation": validation,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "source": snapshot.source,
        "provider_timestamp": snapshot.provider_timestamp,
        "provider_timezone": snapshot.provider_timezone,
        "data_mode": snapshot.data_mode,
        "quote_currency": snapshot.quote_currency,
    }
