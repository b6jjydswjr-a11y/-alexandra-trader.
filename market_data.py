"""Alpha Vantage REST client with strict parsing and fail-closed freshness checks."""

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Candle, MarketSnapshot

LOGGER_NAME = "alexandra.market_data"
API_URL = "https://www.alphavantage.co/query"

FIAT_CODES = {
    "AED", "AUD", "BRL", "CAD", "CHF", "CLP", "CNY", "COP", "CZK", "DKK",
    "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN",
    "MYR", "NOK", "NZD", "PHP", "PLN", "RON", "RUB", "SAR", "SEK", "SGD",
    "THB", "TRY", "TWD", "USD", "VND", "ZAR",
}
CRYPTO_QUOTES = (
    "USDT", "USDC", "BUSD", "USD", "EUR", "GBP", "JPY", "AUD", "CAD",
    "CHF", "CNY", "KRW",
)
COMMON_CRYPTO = {
    "ADA", "AVAX", "BCH", "BNB", "BTC", "DOGE", "DOT", "ETH", "LINK",
    "LTC", "SOL", "TRX", "XRP",
}
TIMEFRAMES = {
    "1m": ("1min", 1),
    "5m": ("5min", 5),
    "15m": ("15min", 15),
    "30m": ("30min", 30),
    "1h": ("60min", 60),
    "1d": ("daily", None),
    "1w": ("weekly", None),
    "1mo": ("monthly", None),
}


class MarketDataError(RuntimeError):
    """A safe-to-display data error. Raw provider responses are never retained."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def _normalize_market(symbol: str) -> tuple[str, str, str, str, str]:
    raw = symbol.strip().upper().replace(" ", "")
    if not raw:
        raise MarketDataError("invalid_symbol", "Enter a market symbol.")

    if "/" in raw or "-" in raw:
        separator = "/" if "/" in raw else "-"
        pieces = raw.split(separator)
        if len(pieces) != 2 or not all(pieces):
            raise MarketDataError("invalid_symbol", "Use BASE/QUOTE, for example BTC/USD.")
        base, quote = pieces
        if len(base) == 3 and len(quote) == 3 and base in FIAT_CODES and quote in FIAT_CODES:
            return f"{base}/{quote}", "forex", base, quote, ""
        return f"{base}/{quote}", "crypto", base, quote, ""

    compact = raw.replace("/", "").replace("-", "")
    if len(compact) == 6 and compact[:3] in FIAT_CODES and compact[3:] in FIAT_CODES:
        return f"{compact[:3]}/{compact[3:]}", "forex", compact[:3], compact[3:], ""

    for quote in CRYPTO_QUOTES:
        if compact.endswith(quote) and len(compact) > len(quote):
            base = compact[:-len(quote)]
            if base.isalpha():
                return f"{base}/{quote}", "crypto", base, quote, ""
    if compact in COMMON_CRYPTO:
        return f"{compact}/USD", "crypto", compact, "USD", ""

    if not all(character.isalnum() or character in ".^_" for character in raw):
        raise MarketDataError("invalid_symbol", "That symbol contains unsupported characters.")
    return raw, "equity", raw, "USD", ""


def _function_for(market_type: str, timeframe: str) -> tuple[str, str, int | None]:
    if timeframe not in TIMEFRAMES:
        supported = ", ".join(TIMEFRAMES)
        raise MarketDataError(
            "unsupported_timeframe",
            f"Alpha Vantage does not provide that native interval. Choose one of: {supported}.",
        )
    interval, minutes = TIMEFRAMES[timeframe]
    if minutes is not None:
        function = {
            "forex": "FX_INTRADAY",
            "crypto": "CRYPTO_INTRADAY",
            "equity": "TIME_SERIES_INTRADAY",
        }[market_type]
        return function, interval, minutes
    function = {
        ("forex", "daily"): "FX_DAILY",
        ("forex", "weekly"): "FX_WEEKLY",
        ("forex", "monthly"): "FX_MONTHLY",
        ("crypto", "daily"): "DIGITAL_CURRENCY_DAILY",
        ("crypto", "weekly"): "DIGITAL_CURRENCY_WEEKLY",
        ("crypto", "monthly"): "DIGITAL_CURRENCY_MONTHLY",
        ("equity", "daily"): "TIME_SERIES_DAILY",
        ("equity", "weekly"): "TIME_SERIES_WEEKLY",
        ("equity", "monthly"): "TIME_SERIES_MONTHLY",
    }[(market_type, interval)]
    return function, interval, None


def _safe_provider_error(payload: dict) -> MarketDataError | None:
    if "Note" in payload:
        return MarketDataError(
            "rate_limit",
            "Alpha Vantage reported a request limit. Wait before trying again.",
        )
    information = payload.get("Information")
    if information:
        text = str(information).lower()
        if any(term in text for term in (
            "rate limit", "call frequency", "requests per day", "calls per day", "per day",
        )):
            return MarketDataError(
                "rate_limit",
                "Alpha Vantage reported a request limit. Wait before trying again.",
            )
        if any(term in text for term in ("premium", "subscription", "plan", "access")):
            return MarketDataError(
                "plan_restricted",
                "This Alpha Vantage endpoint is not available to the configured API plan.",
            )
        return MarketDataError(
            "provider_notice",
            "Alpha Vantage could not serve this request. Check the key, endpoint access, and provider status.",
        )
    if "Error Message" in payload:
        return MarketDataError(
            "provider_rejected",
            "Alpha Vantage rejected the symbol or request parameters. Check the symbol format and supported market.",
        )
    return None


def _extract_series(payload: dict) -> dict:
    candidates = [
        value for key, value in payload.items()
        if "time series" in key.lower() and isinstance(value, dict) and value
    ]
    if len(candidates) != 1:
        raise MarketDataError(
            "missing_data",
            "Alpha Vantage returned no usable time series for this symbol and timeframe.",
        )
    return candidates[0]


def _find_value(row: dict, field_number: str, field_name: str, quote: str) -> float | None:
    direct = f"{field_number}. {field_name}"
    keys = [direct]
    if quote:
        keys.extend((f"{field_number}a. {field_name} ({quote})",
                     f"{field_number}b. {field_name} ({quote})"))
    for key in keys:
        if key in row:
            try:
                value = float(row[key])
                return value if math.isfinite(value) else None
            except (TypeError, ValueError):
                return None
    if quote:
        prefix = f"{field_number}"
        suffix = f". {field_name} ({quote})"
        for key, raw in row.items():
            if key.startswith(prefix) and key.endswith(suffix):
                try:
                    value = float(raw)
                    return value if math.isfinite(value) else None
                except (TypeError, ValueError):
                    return None
    return None


def _provider_timezone(metadata: dict, is_intraday: bool) -> str:
    for key, value in metadata.items():
        if "time zone" in key.lower() and isinstance(value, str) and value.strip():
            return value.strip()
    if is_intraday:
        raise MarketDataError(
            "missing_timezone",
            "The provider response did not include a timezone for its intraday timestamps.",
        )
    return "UTC (date-only provider bar)"


def _parse_bar_time(raw: str, provider_timezone: str) -> datetime | date:
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw)
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
        zone_label = provider_timezone.removesuffix(" (date-only provider bar)")
        try:
            zone = ZoneInfo(zone_label)
        except (ZoneInfoNotFoundError, ValueError):
            if zone_label.upper() != "UTC":
                raise MarketDataError(
                    "invalid_timezone",
                    "The provider returned an unsupported timestamp timezone; analysis was not produced.",
                ) from None
            zone = timezone.utc
        return parsed.replace(tzinfo=zone).astimezone(timezone.utc)
    except ValueError:
        raise MarketDataError(
            "invalid_timestamp",
            "Alpha Vantage returned an invalid candle timestamp.",
        ) from None


def _check_freshness(
    latest: datetime | date,
    market_type: str,
    timeframe: str,
    intraday_minutes: int | None,
) -> None:
    now = datetime.now(timezone.utc)
    if isinstance(latest, date) and not isinstance(latest, datetime):
        today = now.date()
        if latest > today:
            raise MarketDataError("future_data", "The latest provider bar is dated in the future.")
        age_days = (today - latest).days
        limits = {
            "1d": {"crypto": 2, "forex": 5, "equity": 5},
            "1w": {"crypto": 14, "forex": 14, "equity": 14},
            "1mo": {"crypto": 62, "forex": 62, "equity": 62},
        }
        if age_days > limits[timeframe][market_type]:
            raise MarketDataError(
                "stale_data",
                f"The latest historical bar is {age_days} days old, beyond the freshness limit.",
            )
        return

    age = now - latest
    if age < timedelta(minutes=-5):
        raise MarketDataError("future_data", "The latest provider bar is dated in the future.")
    allowed_age = timedelta(minutes=(intraday_minutes or 1) * 2 + 15)
    if age > allowed_age:
        raise MarketDataError(
            "stale_data",
            "The latest intraday bar is older than the configured freshness limit.",
        )


class AlphaVantageProvider:
    """Fetch actual Alpha Vantage bars; never substitutes sample data."""

    def __init__(self, api_key: str, timeout: int = 20):
        self._api_key = api_key.strip()
        self._timeout = timeout

    def fetch_snapshot(self, symbol: str, timeframe: str = "1d") -> MarketSnapshot:
        if not self._api_key:
            raise MarketDataError(
                "missing_api_key",
                "ALPHA_VANTAGE_API_KEY is not configured in project Secrets.",
            )

        normalized, market_type, base, quote, _ = _normalize_market(symbol)
        timeframe = timeframe.strip().lower()
        function, interval, intraday_minutes = _function_for(market_type, timeframe)
        params = {"function": function, "apikey": self._api_key}
        if market_type == "forex":
            params.update({"from_symbol": base, "to_symbol": quote})
        elif market_type == "crypto":
            params.update({"symbol": base, "market": quote})
        else:
            params["symbol"] = base
        if intraday_minutes is not None:
            params.update({"interval": interval, "outputsize": "compact"})
        elif function in {"FX_DAILY", "TIME_SERIES_DAILY"}:
            params["outputsize"] = "compact"

        request = urllib.request.Request(
            f"{API_URL}?{urllib.parse.urlencode(params)}",
            headers={"User-Agent": "AlexandraTrader/2.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            code = "rate_limit" if error.code == 429 else "http_error"
            message = (
                "Alpha Vantage rate limit reached. Wait before trying again."
                if error.code == 429
                else f"Alpha Vantage returned HTTP {error.code}; no analysis was produced."
            )
            raise MarketDataError(code, message) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise MarketDataError(
                "network_error",
                "Could not reach Alpha Vantage. No market analysis was produced.",
            ) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MarketDataError(
                "invalid_response",
                "Alpha Vantage returned an unreadable response. No analysis was produced.",
            ) from None

        if not isinstance(payload, dict):
            raise MarketDataError(
                "invalid_response",
                "Alpha Vantage returned an unexpected response. No analysis was produced.",
            )
        provider_error = _safe_provider_error(payload)
        if provider_error:
            raise provider_error

        series = _extract_series(payload)
        metadata = payload.get("Meta Data", {})
        if not isinstance(metadata, dict):
            metadata = {}
        is_intraday = intraday_minutes is not None
        provider_timezone = _provider_timezone(metadata, is_intraday)

        candles = []
        for timestamp, row in sorted(series.items()):
            if not isinstance(timestamp, str) or not isinstance(row, dict):
                continue
            open_price = _find_value(row, "1", "open", quote)
            high_price = _find_value(row, "2", "high", quote)
            low_price = _find_value(row, "3", "low", quote)
            close_price = _find_value(row, "4", "close", quote)
            if None in (open_price, high_price, low_price, close_price):
                continue
            if min(open_price, high_price, low_price, close_price) <= 0:
                continue
            if high_price < max(open_price, close_price, low_price):
                continue
            if low_price > min(open_price, close_price, high_price):
                continue
            volume = _find_value(row, "5", "volume", quote)
            if volume is not None and volume < 0:
                volume = None
            candles.append(Candle(
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                timestamp=timestamp,
            ))

        if not candles:
            raise MarketDataError(
                "missing_data",
                "Alpha Vantage returned no valid OHLC candles for this request.",
            )
        latest_raw = candles[-1].timestamp
        latest_time = _parse_bar_time(latest_raw, provider_timezone)
        _check_freshness(latest_time, market_type, timeframe, intraday_minutes)

        data_mode = (
            "Provider-described real-time intraday bars (not a broker quote)"
            if is_intraday
            else "Historical time-series bars"
        )
        return MarketSnapshot(
            symbol=normalized,
            market_type=market_type,
            quote_currency=quote,
            timeframe=timeframe,
            candles=tuple(candles),
            source=f"Alpha Vantage {function}",
            provider_timestamp=latest_raw,
            provider_timezone=provider_timezone,
            data_mode=data_mode,
        )
