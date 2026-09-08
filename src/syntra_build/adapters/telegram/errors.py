"""Typed, secret-safe Telegram adapter failures."""


class TelegramError(Exception):
    """Base class for Telegram gateway failures."""


class TelegramConfigurationError(TelegramError):
    """Raised when the gateway cannot be used with its supplied configuration."""


class TelegramTransportError(TelegramError):
    """Raised when a bounded HTTP interaction cannot be completed."""


class TelegramProtocolError(TelegramError):
    """Raised when Telegram returns a malformed response."""


class TelegramAPIError(TelegramError):
    """Raised when Telegram returns a valid unsuccessful API envelope."""

    def __init__(self, *, error_code: int | None, description: str | None) -> None:
        self.error_code = error_code
        self.description = description
        detail = description or "unspecified provider error"
        code = f" (code {error_code})" if error_code is not None else ""
        super().__init__(f"Telegram API request failed{code}: {detail}")
