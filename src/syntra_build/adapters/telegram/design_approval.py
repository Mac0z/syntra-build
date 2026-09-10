# ruff: noqa: E501
"""Telegram presentation and routing for exact M17 design packages."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Final

from syntra_build.adapters.telegram.client import TelegramClient
from syntra_build.adapters.telegram.models import (
    TelegramCallbackQuery,
    TelegramInboundMessage,
)
from syntra_build.application.commands.models import Command, CommandType
from syntra_build.application.specification import DesignPackageDecisionHandler
from syntra_build.domain import (
    DesignPackage,
    DesignPackageId,
    DocumentType,
    GateId,
    GateState,
    GateType,
    HumanGate,
)
from syntra_build.infrastructure.persistence import (
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    SQLiteTelegramGateInteractionRepository,
    SQLiteTelegramGateNotificationRepository,
)
from syntra_build.infrastructure.persistence.connection import transaction
from syntra_build.infrastructure.persistence.errors import PersistenceError

_CALLBACK: Final = re.compile(
    r"design:(spec|agents|approve|changes):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z"
)
_ARTIFACT_PREFIX: Final = "design-package:"


def design_callback_data(action: str, gate_id: GateId) -> str:
    value = f"design:{action}:{gate_id}"
    if (
        action not in {"spec", "agents", "approve", "changes"}
        or len(value.encode()) > 64
    ):
        raise ValueError("invalid Telegram design callback")
    return value


def markdown_pdf(
    project_name: str, document_type: str, revision: int, content: str
) -> bytes:
    """Create a bounded, non-authoritative readable rendering in memory."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_compression(False)
    pdf.add_page()
    pdf.set_auto_page_break(True, 15)
    pdf.set_font("Helvetica", "B", 16)
    pdf.multi_cell(
        0, 8, _latin(f"{project_name} - {document_type} revision {revision}")
    )
    pdf.ln(2)
    in_code = False
    for raw in content.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            pdf.set_font("Courier", size=9)
            pdf.multi_cell(0, 5, _latin(line) or " ")
        elif match := re.match(r"^(#{1,6})\s+(.*)$", line):
            pdf.set_font("Helvetica", "B", max(10, 17 - len(match.group(1))))
            pdf.multi_cell(0, 7, _latin(match.group(2)))
        elif re.match(r"^\s*([-*+] |\d+[.)] )", line):
            pdf.set_font("Helvetica", size=10)
            pdf.multi_cell(0, 6, _latin(line))
        else:
            pdf.set_font("Helvetica", size=10)
            pdf.multi_cell(0, 6, _latin(line) or " ")
    output = pdf.output()
    return bytes(output)


def _latin(value: str) -> str:
    return value.encode("latin-1", "replace").decode("latin-1")


@dataclass(slots=True)
class TelegramDesignApprovalHandler:
    connection: sqlite3.Connection
    client: TelegramClient
    authorised_user_ids: frozenset[str]

    def handle_callback(self, callback: TelegramCallbackQuery) -> None:
        """Acknowledge, then resolve all authority from SQLite rather than payload data."""
        if str(callback.user_id) not in self.authorised_user_ids:
            raise PermissionError("responder is not authorised")
        match = _CALLBACK.fullmatch(callback.callback_data)
        if match is None:
            self.client.answer_callback(
                callback.callback_query_id, "Unsupported action."
            )
            return
        self.client.answer_callback(callback.callback_query_id)
        action, raw_gate_id = match.groups()
        try:
            gate_id = GateId.from_string(raw_gate_id)
            gate, package = self._resolve_callback(gate_id, callback)
            if action in {"spec", "agents"}:
                self._deliver_document(callback, package.id, action)
            elif action == "approve":
                command = self._command(callback, gate_id, "APPROVE", None)
                result = DesignPackageDecisionHandler(
                    SQLiteDesignPackageRepository(self.connection),
                    self.authorised_user_ids,
                ).respond(command, gate)
                self.client.send_text(
                    chat_id=callback.chat_id,
                    text=result,
                    thread_id=callback.thread_id,
                    reply_to_message_id=callback.source_message_id,
                )
            else:
                interactions = SQLiteTelegramGateInteractionRepository(self.connection)
                interaction = interactions.begin(
                    gate_id,
                    package.id,
                    str(callback.chat_id),
                    str(callback.user_id),
                    str(callback.thread_id) if callback.thread_id is not None else None,
                    callback.received_at,
                )
                if interaction.prompt_message_id is None:
                    sent = self.client.send_text(
                        chat_id=callback.chat_id,
                        text="What would you like changed? Reply to this message with your feedback.",
                        thread_id=callback.thread_id,
                        reply_to_message_id=callback.source_message_id,
                        reply_markup={"force_reply": True, "selective": True},
                    )
                    interactions.activate(interaction.id, str(sent.message_id))
        except PersistenceError, ValueError:
            self.client.send_text(
                chat_id=callback.chat_id,
                text="This design approval has already been resolved.",
                thread_id=callback.thread_id,
                reply_to_message_id=callback.source_message_id,
            )

    def handle_feedback_reply(self, message: TelegramInboundMessage) -> bool:
        if (
            str(message.user_id) not in self.authorised_user_ids
            or message.reply_to_message_id is None
        ):
            return False
        interactions = SQLiteTelegramGateInteractionRepository(self.connection)
        item = interactions.for_reply(
            str(message.chat_id), str(message.user_id), str(message.reply_to_message_id)
        )
        if item is None:
            return False
        feedback = message.text.strip()
        if not feedback:
            self.client.send_text(
                chat_id=message.chat_id,
                text="Feedback cannot be empty; reply again with the requested changes.",
                thread_id=message.thread_id,
                reply_to_message_id=message.message_id,
            )
            return True
        gate, _ = self._resolve_gate(item.gate_id)
        command = Command(
            CommandType.RESPOND_GATE,
            str(message.user_id),
            message.received_at,
            "telegram",
            str(message.update_id),
            str(message.message_id),
            f"telegram:{message.update_id}",
            gate_reference=str(item.gate_id),
            gate_response="REQUEST_CHANGES",
            gate_feedback=feedback,
            chat_id=str(message.chat_id),
            thread_id=str(message.thread_id) if message.thread_id else None,
        )
        with transaction(self.connection):
            DesignPackageDecisionHandler(
                SQLiteDesignPackageRepository(self.connection), self.authorised_user_ids
            ).respond(command, gate)
            interactions.resolve(item.id, message.received_at)
        self.client.send_text(
            chat_id=message.chat_id,
            text="Requested design changes recorded.",
            thread_id=message.thread_id,
            reply_to_message_id=message.message_id,
        )
        return True

    def _resolve_callback(
        self, gate_id: GateId, callback: TelegramCallbackQuery
    ) -> tuple[HumanGate, DesignPackage]:
        SQLiteTelegramGateNotificationRepository(self.connection).validate_callback(
            gate_id,
            str(callback.chat_id),
            str(callback.thread_id) if callback.thread_id is not None else None,
            str(callback.source_message_id),
        )
        return self._resolve_gate(gate_id)

    def _resolve_gate(self, gate_id: GateId) -> tuple[HumanGate, DesignPackage]:
        gate = SQLiteHumanGateRepository(self.connection, lambda: "unused").get(gate_id)
        if (
            gate.gate_type is not GateType.DESIGN_APPROVAL
            or gate.state is not GateState.NOTIFIED
        ):
            raise PersistenceError("design gate is not answerable")
        if not gate.artifact_reference or not gate.artifact_reference.startswith(
            _ARTIFACT_PREFIX
        ):
            raise PersistenceError("gate has no package")
        package = SQLiteDesignPackageRepository(self.connection).get(
            DesignPackageId.from_string(
                gate.artifact_reference.removeprefix(_ARTIFACT_PREFIX)
            )
        )
        if package.approval_gate_id != gate.id or package.project_id != gate.project_id:
            raise PersistenceError("design package does not match gate")
        return gate, package

    def _deliver_document(
        self, callback: TelegramCallbackQuery, package_id: DesignPackageId, action: str
    ) -> None:
        package = SQLiteDesignPackageRepository(self.connection).get(package_id)
        document_id = (
            package.spec_document_id if action == "spec" else package.agents_document_id
        )
        document = SQLiteProjectDocumentRepository(self.connection).get(
            package.project_id, document_id
        )
        expected = DocumentType.SPEC if action == "spec" else DocumentType.AGENTS
        if document.document_type is not expected:
            raise PersistenceError("package document type is invalid")
        project = SQLiteProjectRepository(self.connection, lambda: "unused").get(
            package.project_id
        )
        stem = f"{expected.value}-r{document.revision}"
        caption = (
            f"{project.name} - {expected.value} revision {document.revision}\n"
            f"SHA-256: {document.content_hash}\n"
            "The Markdown file is authoritative; the PDF is a review rendering."
        )
        self.client.send_document(
            chat_id=callback.chat_id,
            content=document.content.encode("utf-8"),
            filename=f"{stem}.md",
            mime_type="text/markdown",
            caption=caption,
            thread_id=callback.thread_id,
            reply_to_message_id=callback.source_message_id,
        )
        self.client.send_document(
            chat_id=callback.chat_id,
            content=markdown_pdf(
                project.name, expected.value, document.revision, document.content
            ),
            filename=f"{stem}.pdf",
            mime_type="application/pdf",
            caption=caption,
            thread_id=callback.thread_id,
            reply_to_message_id=callback.source_message_id,
        )

    @staticmethod
    def _command(
        callback: TelegramCallbackQuery,
        gate_id: GateId,
        outcome: str,
        feedback: str | None,
    ) -> Command:
        return Command(
            CommandType.RESPOND_GATE,
            str(callback.user_id),
            callback.received_at,
            "telegram",
            str(callback.update_id),
            f"callback:{callback.callback_query_id}",
            f"telegram:{callback.update_id}",
            gate_reference=str(gate_id),
            gate_response=outcome,
            gate_feedback=feedback,
            chat_id=str(callback.chat_id),
            thread_id=str(callback.thread_id)
            if callback.thread_id is not None
            else None,
        )
