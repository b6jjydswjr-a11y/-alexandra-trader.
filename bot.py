"""Phone-friendly Telegram interface for Alexandra's market analyst."""

import html
import logging
import time
from datetime import datetime, timezone

from .analysis import analyze
from .market_data import AlphaVantageProvider, MarketDataError
from .telegram_api import TelegramAPIError, TelegramClient

LOGGER = logging.getLogger("alexandra")
CACHE_SECONDS = 90


def menu_markup() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Analyze EUR/USD · 1D", "callback_data": "analyze:EURUSD:1d"},
                {"text": "Analyze BTC/USD · 1D", "callback_data": "analyze:BTC/USD:1d"},
            ],
            [
                {"text": "Markets", "callback_data": "markets"},
                {"text": "News", "callback_data": "news"},
            ],
            [
                {"text": "Recent signals", "callback_data": "signals"},
                {"text": "Help", "callback_data": "help"},
            ],
        ]
    }


def _risk_text() -> str:
    return (
        "⚠️ Estimates only—not financial advice or a promise of accuracy. "
        "Historical/provider data can be delayed and may differ from Quotex prices. "
        "Review independently; Alexandra never places trades."
    )


def format_analysis(result: dict) -> str:
    values = result["indicators"]
    validation = result["validation"]
    fmt = lambda value: "Unavailable" if value is None else f"{value:.6g}"
    percent = lambda value: "Unavailable" if value is None else f"{value:+.2f}%"
    alignment = validation["rule_alignment_pct"]
    alignment_text = (
        "Unavailable (incomplete indicators)"
        if alignment is None
        else f"{alignment}% ({validation['directional_votes']})"
    )
    market_type = html.escape(result["market_type"])
    symbol = html.escape(result["symbol"])
    return (
        f"<b>Alexandra · {symbol}</b>\n"
        f"Market: {market_type} · quote {html.escape(result['quote_currency'])}\n"
        f"Provider: Alpha Vantage · {html.escape(result['source'])}\n"
        f"Data type: {html.escape(result['data_mode'])}\n"
        f"Timeframe: {html.escape(result['timeframe'])}\n"
        f"Latest source bar: {html.escape(result['provider_timestamp'])} "
        f"({html.escape(result['provider_timezone'])})\n"
        f"Analysis generated: {html.escape(result['generated_at'])}\n\n"
        f"<b>Assessment: {validation['decision']}</b>\n"
        f"Rule alignment: {alignment_text} — indicator agreement, not a win probability.\n"
        f"Signal validator: {html.escape(validation['reason'])}\n\n"
        f"Latest source close: {fmt(values['last'])} {html.escape(result['quote_currency'])}\n"
        f"RSI (14, Wilder): {fmt(values['rsi14'])}\n"
        f"MACD (12,26,9) / signal: {fmt(values['macd'])} / {fmt(values['macd_signal'])}\n"
        f"MACD histogram: {fmt(values['macd_histogram'])}\n"
        f"SMA (20 / 50): {fmt(values['sma20'])} / {fmt(values['sma50'])}\n"
        f"10-bar momentum: {percent(values['momentum10_pct'])}\n"
        f"Volume change: {percent(values['volume_change_pct'])}\n\n"
        f"{_risk_text()}"
    )


def no_trade_text(symbol: str, timeframe: str, error: MarketDataError) -> str:
    return (
        f"<b>Assessment: NO TRADE · {html.escape(symbol.upper())}</b>\n"
        "Provider: Alpha Vantage\n"
        f"Requested timeframe: {html.escape(timeframe)}\n"
        f"Checked: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        "No price, indicator, or signal was produced from unavailable data.\n"
        f"Reason: {html.escape(error.public_message)}\n\n"
        f"{_risk_text()}"
    )


def markets_text() -> str:
    return (
        "<b>Markets & data availability</b>\n"
        "• Forex: use <code>EURUSD</code> or <code>EUR/USD</code>\n"
        "• Crypto: use <code>BTC/USD</code>, <code>BTCUSDT</code>, or a supported base such as <code>BTC</code>\n"
        "• Equities: use a provider symbol such as <code>AAPL</code>\n\n"
        "Historical intervals: <code>1d</code>, <code>1w</code>, <code>1mo</code>.\n"
        "Intraday intervals: <code>1m</code>, <code>5m</code>, <code>15m</code>, "
        "<code>30m</code>, <code>1h</code>; Alpha Vantage marks these market-data endpoints "
        "as premium. Access depends on the configured key's plan.\n"
        "Daily/weekly/monthly records are historical bars, not a live broker quote. "
        "If an endpoint is unavailable, stale, or limited, Alexandra returns NO TRADE."
    )


def news_text() -> str:
    return (
        "<b>News Analyst</b>\n"
        "No verified financial-news provider is connected. There are no headlines or "
        "sentiment results to report. Alexandra will not invent news."
    )


def help_text() -> str:
    return (
        "<b>Alexandra · Market Analyst</b>\n\n"
        "<code>/analyze SYMBOL [TIMEFRAME]</code> — example: <code>/analyze EURUSD 1d</code>\n"
        "<code>/markets</code> — symbols, intervals, and data limits\n"
        "<code>/news</code> — news-provider status\n"
        "<code>/signals</code> — successful analyses from this running session\n"
        "<code>/start</code> — menu\n"
        "<code>/help</code> — this guide\n\n"
        "Indicator rules use source OHLC data only: Wilder RSI(14), EMA MACD(12,26,9), "
        "SMA(20/50), 10-bar close momentum, and volume change only when source volume exists. "
        "Rule alignment is a count of indicator votes, not a calibrated probability. "
        "Missing, stale, or conflicting data means NO TRADE. No Quotex connection, automated "
        "clicks, or trade placement."
    )


def start_text() -> str:
    return (
        "<b>Hi, I’m Alexandra.</b>\n"
        "I check provider-sourced market bars, calculate transparent indicators, and reject "
        "weak or incomplete setups. Daily bars are historical; intraday access depends on your "
        "Alpha Vantage plan. News is not connected, and no trades are executed.\n\n"
        "Tap a shortcut or send <code>/analyze EURUSD 1d</code>."
    )


def _analysis_for(
    provider: AlphaVantageProvider,
    symbol: str,
    timeframe: str,
    results_cache: dict,
) -> str:
    normalized = symbol.strip().upper()
    key = (normalized, timeframe.lower())
    now = time.monotonic()
    cached = results_cache.get(key)
    if cached and now - cached["cached_at"] < CACHE_SECONDS:
        return format_analysis(cached["result"])
    try:
        snapshot = provider.fetch_snapshot(normalized, timeframe)
        result = analyze(snapshot)
    except MarketDataError as error:
        LOGGER.warning("Market request declined (%s).", error.code)
        return no_trade_text(normalized or "unknown symbol", timeframe, error)
    except Exception as error:
        LOGGER.error("Market analysis failed (%s).", type(error).__name__)
        safe_error = MarketDataError(
            "internal_error",
            "An internal analysis error occurred. No analysis or signal was produced.",
        )
        return no_trade_text(normalized or "unknown symbol", timeframe, safe_error)

    results_cache[key] = {"cached_at": now, "result": result}
    return format_analysis(result)


def _signals_text(results_cache: dict) -> str:
    if not results_cache:
        return (
            "<b>Recent assessments</b>\n"
            "NO TRADE — no successful Alpha Vantage analysis has been completed in this "
            "bot session. Run <code>/analyze SYMBOL 1d</code> first."
        )
    items = sorted(
        (entry["result"] for entry in results_cache.values()),
        key=lambda result: result["generated_at"],
        reverse=True,
    )[:8]
    lines = []
    for result in items:
        validation = result["validation"]
        alignment = validation["rule_alignment_pct"]
        align_text = "not assigned" if alignment is None else f"{alignment}% rule alignment"
        lines.append(
            f"• <code>{html.escape(result['symbol'])}</code> · "
            f"{html.escape(result['timeframe'])} · <b>{validation['decision']}</b>\n"
            f"  Source bar: {html.escape(result['provider_timestamp'])} · "
            f"{html.escape(align_text)}\n"
            f"  {html.escape(validation['reason'])}"
        )
    return (
        "<b>Recent provider-backed assessments</b>\n"
        + "\n".join(lines)
        + "\n\nThese are analyses requested in this running session, not independent trade orders. "
        + _risk_text()
    )


def _message_reply(client: TelegramClient, chat_id: int, text: str) -> None:
    client.send_message(chat_id, text, menu_markup())


def _handle_command(
    client: TelegramClient,
    chat_id: int,
    text: str,
    provider: AlphaVantageProvider,
    results_cache: dict,
) -> None:
    command, _, arguments = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    if command == "/start":
        response = start_text()
    elif command == "/help":
        response = help_text()
    elif command == "/markets":
        response = markets_text()
    elif command == "/news":
        response = news_text()
    elif command == "/signals":
        response = _signals_text(results_cache)
    elif command == "/analyze":
        parts = arguments.split()
        symbol = parts[0] if parts else ""
        timeframe = parts[1].lower() if len(parts) > 1 else "1d"
        if not symbol:
            response = (
                "Use <code>/analyze SYMBOL [TIMEFRAME]</code>, for example "
                "<code>/analyze EURUSD 1d</code>."
            )
        else:
            response = _analysis_for(provider, symbol, timeframe, results_cache)
    else:
        response = "I didn’t recognize that command. Use /help to see the menu."
    _message_reply(client, chat_id, response)


def _handle_callback(
    client: TelegramClient,
    callback: dict,
    provider: AlphaVantageProvider,
    results_cache: dict,
) -> None:
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    data = callback.get("data", "")
    if callback_id:
        client.answer_callback(callback_id)
    if not chat_id:
        return
    if data.startswith("analyze:"):
        _, symbol, *timeframe_parts = data.split(":")
        timeframe = timeframe_parts[0] if timeframe_parts else "1d"
        text = _analysis_for(provider, symbol, timeframe, results_cache)
    elif data == "markets":
        text = markets_text()
    elif data == "news":
        text = news_text()
    elif data == "signals":
        text = _signals_text(results_cache)
    elif data == "help":
        text = help_text()
    else:
        text = start_text()
    _message_reply(client, chat_id, text)


def run_polling(token: str, provider: AlphaVantageProvider) -> None:
    client = TelegramClient(token)
    client.call(
        "setMyCommands",
        {
            "commands": [
                {"command": "start", "description": "Open Alexandra's menu"},
                {"command": "analyze", "description": "Analyze a symbol and timeframe"},
                {"command": "markets", "description": "Show supported markets and data limits"},
                {"command": "news", "description": "Check financial news status"},
                {"command": "signals", "description": "Show recent provider-backed assessments"},
                {"command": "help", "description": "Show commands and safety notes"},
            ]
        },
    )
    LOGGER.info("Telegram bot started; credentials are not logged.")
    offset = None
    backoff = 1
    results_cache: dict = {}
    while True:
        payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        try:
            updates = client.call("getUpdates", payload, timeout=35)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    callback = update.get("callback_query")
                    if callback:
                        _handle_callback(client, callback, provider, results_cache)
                        continue
                    message = update.get("message") or {}
                    chat_id = (message.get("chat") or {}).get("id")
                    text = message.get("text", "").strip()
                    if chat_id and text.startswith("/"):
                        _handle_command(client, chat_id, text, provider, results_cache)
                except TelegramAPIError as error:
                    LOGGER.warning("Telegram request failed while handling an update (%s).", str(error))
                except Exception as error:
                    LOGGER.error("Update handling failed (%s).", type(error).__name__)
            backoff = 1
        except TelegramAPIError as error:
            LOGGER.warning("Telegram polling request failed (%s); retrying.", str(error))
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
