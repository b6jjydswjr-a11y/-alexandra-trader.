import logging
import os
import sys

from .bot import run_polling
from .market_data import AlphaVantageProvider
from .telegram_api import TelegramAPIError


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        logging.getLogger("alexandra").error(
            "Missing TELEGRAM_BOT_TOKEN. Add it using the project's secure Secrets tool."
        )
        sys.exit(1)
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY", "").strip()
    if not api_key:
        logging.getLogger("alexandra").warning(
            "ALPHA_VANTAGE_API_KEY is not configured; market requests will return NO TRADE."
        )
    try:
        run_polling(token, AlphaVantageProvider(api_key))
    except KeyboardInterrupt:
        logging.getLogger("alexandra").info("Bot stopped.")
    except ValueError:
        logging.getLogger("alexandra").critical(
            "TELEGRAM_BOT_TOKEN has an invalid format. Recheck the value in Secrets using BotFather; "
            "the token is not logged."
        )
        sys.exit(1)
    except TelegramAPIError as error:
        logging.getLogger("alexandra").critical(
            "Telegram rejected the request (%s). Verify that TELEGRAM_BOT_TOKEN is the current "
            "BotFather token for @bablugBot; the token is not logged.",
            str(error),
        )
        sys.exit(1)
    except Exception as error:
        logging.getLogger("alexandra").critical(
            "Bot stopped because startup failed (%s).", type(error).__name__
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
