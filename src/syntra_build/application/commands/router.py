"""Provider-neutral deterministic command router."""

from __future__ import annotations

import logging

from syntra_build.application.commands.models import (
    Command,
    CommandResponse,
    CommandType,
    InboundMessage,
)
from syntra_build.application.commands.parser import CommandParser, ParseFailure
from syntra_build.application.commands.services import (
    CommandAuditRequest,
    CommandAuditSink,
    IntentResolver,
    LocalHealthService,
    ProjectCommandService,
    ProjectQueryService,
    ProjectSummary,
    ResolutionOutcome,
)
from syntra_build.infrastructure.logging import logging_context, new_correlation_id

HELP_TEXT = """Commands:
ping
health
projects
status <project>
pause <project>
resume <project>
cancel <project>"""

_LOGGER = logging.getLogger("syntra_build.application.commands")


class CommandRouter:
    def __init__(
        self,
        *,
        project_queries: ProjectQueryService,
        project_commands: ProjectCommandService,
        audit_sink: CommandAuditSink,
        health: LocalHealthService,
        parser: CommandParser | None = None,
        intent_resolver: IntentResolver | None = None,
    ) -> None:
        self._queries = project_queries
        self._commands = project_commands
        self._audit = audit_sink
        self._health = health
        self._parser = parser or CommandParser()
        self._intent_resolver = intent_resolver

    def route(self, message: InboundMessage) -> CommandResponse:
        parsed = self._parser.parse(message)
        command = parsed.command
        if (
            command is None
            and parsed.failure is ParseFailure.UNKNOWN
            and self._intent_resolver is not None
        ):
            command = self._intent_resolver.resolve(message)
        if command is None:
            correlation_id = new_correlation_id()
            _LOGGER.info(
                "Command help returned",
                extra={
                    "event": "command_help_returned",
                    "metadata": {
                        "source_update_id": message.source_update_id,
                        "source_message_id": message.source_message_id,
                        "reason": parsed.failure,
                    },
                },
            )
            return self._response(message, HELP_TEXT, correlation_id)

        with logging_context(correlation_id=command.correlation_id):
            _LOGGER.info(
                "Command parsed",
                extra={
                    "event": "command_parsed",
                    "metadata": {
                        "command": command.type,
                        "source_update_id": command.source_update_id,
                        "source_message_id": command.source_message_id,
                    },
                },
            )
            text = self._route_command(command)
            _LOGGER.info(
                "Command routed",
                extra={
                    "event": "command_routed",
                    "metadata": {"command": command.type},
                },
            )
        return self._response(message, text, command.correlation_id)

    def _route_command(self, command: Command) -> str:
        if command.type is CommandType.PING:
            return "pong"
        if command.type is CommandType.HEALTH:
            return self._health.current_health()
        if command.type is CommandType.LIST_PROJECTS:
            return self._format_projects(self._queries.list_projects())
        project = self._resolve(command)
        if isinstance(project, str):
            return project
        if command.type is CommandType.PROJECT_STATUS:
            return self._format_status(self._queries.get_project_status(project.id))

        audit = CommandAuditRequest(
            event_type="STATE_CHANGE_COMMAND_REQUESTED",
            command=command,
            project_id=project.id,
        )
        # Recording the request precedes the potentially side-effecting service call.
        self._audit.record(audit)
        _LOGGER.info(
            "State change command requested",
            extra={
                "event": "state_change_command_requested",
                "metadata": {
                    "command": command.type,
                    "project_id": str(project.id),
                    "source_update_id": command.source_update_id,
                    "source_message_id": command.source_message_id,
                },
            },
        )
        return self._commands.handle(command, project).message

    def _resolve(self, command: Command) -> ProjectSummary | str:
        reference = command.project_reference
        if reference is None:  # Parser guarantees this; retain a safe boundary guard.
            return HELP_TEXT
        resolution = self._queries.resolve_project(reference)
        if resolution.outcome is ResolutionOutcome.FOUND and resolution.project:
            return resolution.project
        if resolution.outcome is ResolutionOutcome.NOT_FOUND:
            return f"Project not found: {reference}"
        return f"Project reference is {resolution.outcome.value.lower()}: {reference}"

    @staticmethod
    def _format_projects(projects: tuple[ProjectSummary, ...]) -> str:
        if not projects:
            return "No projects."
        lines = ["Projects:"]
        lines.extend(f"{project.name} — {project.state.value}" for project in projects)
        return "\n".join(lines)

    @staticmethod
    def _format_status(project: ProjectSummary) -> str:
        return f"Project: {project.name}\nState: {project.state.value}"

    @staticmethod
    def _response(
        source: InboundMessage, text: str, correlation_id: str
    ) -> CommandResponse:
        return CommandResponse(
            text=text,
            correlation_id=correlation_id,
            chat_id=source.chat_id,
            thread_id=source.thread_id,
            reply_to_message_id=source.source_message_id,
        )
