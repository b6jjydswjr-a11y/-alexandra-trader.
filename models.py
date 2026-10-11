"""Shared provider-independent market data models."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Candle:
    """A source candle; volume and provider timestamp may be unavailable."""

    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    market_type: str
    quote_currency: str
    timeframe: str
    candles: tuple[Candle, ...]
    source: str
    provider_timestamp: str
    provider_timezone: str
    data_mode: str
