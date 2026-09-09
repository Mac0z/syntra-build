"""Small synchronous Telegram Bot API gateway with an injectable HTTP seam."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from syntra_build.adapters.telegram.errors import (
    TelegramAPIError,
    TelegramConfigurationError,
    TelegramProtocolError,
    TelegramTransportError,
)
from syntra_build.adapters.telegram.models import (
    TelegramInboundMessage,
    TelegramPolledUpdate,
    TelegramSentMessage,
    TelegramUpdateDisposition,
)
from syntra_build.infrastructure.config import ApplicationConfig

_LOGGER = logging.getLogger("syntra_build.adapters.telegram")
_API_ROOT: Final = "https://api.telegram.org"
_TRANSPORT_OVERHEAD_SECONDS: Final = 10.0


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    """Minimal HTTP response used by the injectable transport boundary."""

    status: int
    body: bytes


HTTPTransport = Callable[[Request, float], HTTPResponse]


def _stdlib_transport(request: Request, timeout: float) -> HTTPResponse:
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            return HTTPResponse(status=response.status, body=response.read())
    except HTTPError as error:
        return HTTPResponse(status=error.code, body=error.read())


class TelegramClient:
    """Perform one bounded Telegram provider interaction per public method call."""

    def __init__(
        self,
        config: ApplicationConfig,
        *,
        transport: HTTPTransport = _stdlib_transport,
    ) -> None:
        token = config.secrets.telegram_bot_token
        if not config.telegram.enabled:
            raise TelegramConfigurationError("Telegram integration is disabled")
        if token is None:
            raise TelegramConfigurationError("Telegram bot token is unavailable")
        self._token = token.value
        self._authorised_user_ids = frozenset(config.telegram.authorised_user_ids)
        self._poll_timeout = config.telegram.polling_timeout_seconds
        self._transport = transport

    def poll_updates(
        self, *, offset: int | None = None
    ) -> tuple[TelegramPolledUpdate, ...]:
        """Fetch updates, retaining IDs while enforcing authorization locally."""
        parameters: dict[str, object] = {"timeout": self._poll_timeout}
        if offset is not None:
            parameters["offset"] = offset
        _LOGGER.info(
            "Telegram poll started",
            extra={"event": "telegram_poll_started", "metadata": {"offset": offset}},
        )
        result = self._request("getUpdates", parameters, self._poll_timeout + 10.0)
        if not isinstance(result, list):
            raise TelegramProtocolError("Telegram getUpdates result must be a list")

        updates: list[TelegramPolledUpdate] = []
        for update in result:
            raw = _expect_mapping(update, "Telegram update")
            update_id = _expect_int(raw.get("update_id"), "update_id")
            message = self._normalise_update(raw)
            if message is None:
                updates.append(
                    TelegramPolledUpdate(
                        update_id, TelegramUpdateDisposition.UNSUPPORTED
                    )
                )
                continue
            if message.user_id not in self._authorised_user_ids:
                _LOGGER.warning(
                    "Telegram update ignored",
                    extra={
                        "event": "telegram_update_ignored",
                        "metadata": {
                            "update_id": message.update_id,
                            "reason": "unauthorised_user",
                        },
                    },
                )
                updates.append(
                    TelegramPolledUpdate(
                        update_id, TelegramUpdateDisposition.UNAUTHORISED
                    )
                )
                continue
            updates.append(
                TelegramPolledUpdate(
                    update_id, TelegramUpdateDisposition.ROUTABLE, message
                )
            )
            _LOGGER.info(
                "Telegram update received",
                extra={
                    "event": "telegram_update_received",
                    "metadata": {
                        "update_id": message.update_id,
                        "message_id": message.message_id,
                        "user_id": message.user_id,
                    },
                },
            )
        _LOGGER.info(
            "Telegram poll completed",
            extra={
                "event": "telegram_poll_completed",
                "metadata": {
                    "provider_update_count": len(updates),
                    "authorised_update_count": sum(
                        item.disposition is TelegramUpdateDisposition.ROUTABLE
                        for item in updates
                    ),
                },
            },
        )
        return tuple(sorted(updates, key=lambda item: item.update_id))

    def send_text(
        self,
        *,
        chat_id: int,
        text: str,
        thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> TelegramSentMessage:
        """Send one plain-text message and return its stable provider reference."""
        parameters: dict[str, object] = {"chat_id": chat_id, "text": text}
        if thread_id is not None:
            parameters["message_thread_id"] = thread_id
        if reply_to_message_id is not None:
            parameters["reply_to_message_id"] = reply_to_message_id
        result = self._request("sendMessage", parameters, _TRANSPORT_OVERHEAD_SECONDS)
        message = _expect_mapping(result, "Telegram sendMessage result")
        message_id = _expect_int(message.get("message_id"), "message_id")
        response_chat = _expect_mapping(message.get("chat"), "chat")
        response_chat_id = _expect_int(response_chat.get("id"), "chat.id")
        response_thread_id = _optional_int(
            message.get("message_thread_id"), "thread ID"
        )
        sent = TelegramSentMessage(message_id, response_chat_id, response_thread_id)
        _LOGGER.info(
            "Telegram message sent",
            extra={
                "event": "telegram_message_sent",
                "metadata": {"chat_id": response_chat_id, "message_id": message_id},
            },
        )
        return sent

    def _request(
        self, method: str, parameters: Mapping[str, object], timeout: float
    ) -> object:
        # The credential-bearing URL is deliberately never included in diagnostics.
        request = Request(
            f"{_API_ROOT}/bot{self._token}/{method}",
            data=urlencode(parameters).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            response = self._transport(request, timeout)
        except Exception as error:
            _LOGGER.error(
                "Telegram transport failed",
                extra={
                    "event": "telegram_transport_failed",
                    "metadata": {"method": method},
                },
            )
            raise TelegramTransportError("Telegram transport request failed") from error
        if not 200 <= response.status < 300:
            raise TelegramTransportError(
                f"Telegram HTTP request failed with status {response.status}"
            )
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TelegramProtocolError(
                "Telegram response was not valid JSON"
            ) from error
        envelope = _expect_mapping(payload, "Telegram response")
        if type(envelope.get("ok")) is not bool:
            raise TelegramProtocolError("Telegram response is missing boolean ok")
        if not envelope["ok"]:
            error_code = _optional_int(envelope.get("error_code"), "error_code")
            description_value = envelope.get("description")
            description = (
                description_value.replace(self._token, "[REDACTED]")[:500]
                if isinstance(description_value, str)
                else None
            )
            raise TelegramAPIError(error_code=error_code, description=description)
        if "result" not in envelope:
            raise TelegramProtocolError("Telegram response is missing result")
        return envelope["result"]

    @staticmethod
    def _normalise_update(raw_update: object) -> TelegramInboundMessage | None:
        update = _expect_mapping(raw_update, "Telegram update")
        update_id = _expect_int(update.get("update_id"), "update_id")
        raw_message = update.get("message")
        if not isinstance(raw_message, Mapping):
            _log_unsupported(update_id)
            return None
        text = raw_message.get("text")
        if not isinstance(text, str) or not text:
            _log_unsupported(update_id)
            return None
        sender = _expect_mapping(raw_message.get("from"), "message.from")
        chat = _expect_mapping(raw_message.get("chat"), "message.chat")
        timestamp = _expect_int(raw_message.get("date"), "message.date")
        reply = raw_message.get("reply_to_message")
        reply_id = None
        if reply is not None:
            reply_id = _expect_int(
                _expect_mapping(reply, "reply_to_message").get("message_id"),
                "reply_to_message.message_id",
            )
        return TelegramInboundMessage(
            update_id=update_id,
            message_id=_expect_int(raw_message.get("message_id"), "message_id"),
            chat_id=_expect_int(chat.get("id"), "chat.id"),
            user_id=_expect_int(sender.get("id"), "from.id"),
            text=text,
            received_at=datetime.fromtimestamp(timestamp, UTC),
            thread_id=_optional_int(raw_message.get("message_thread_id"), "thread ID"),
            reply_to_message_id=reply_id,
        )


def _log_unsupported(update_id: int) -> None:
    _LOGGER.info(
        "Telegram update ignored",
        extra={
            "event": "telegram_update_ignored",
            "metadata": {"update_id": update_id, "reason": "unsupported_update"},
        },
    )


def _expect_mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise TelegramProtocolError(f"{context} must be an object")
    return value


def _expect_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise TelegramProtocolError(f"Telegram {field} must be an integer")
    return value


def _optional_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    return _expect_int(value, field)
