"""Provider-neutral command contracts and parsing.

Routing is intentionally not re-exported from this package.  Lower-level
application services import the command contracts, while the router composes
those services; eagerly importing the router here would reverse that dependency.
"""

from syntra_build.application.commands.models import (
    READ_ONLY_COMMANDS,
    STATE_CHANGING_COMMANDS,
    Command,
    CommandResponse,
    CommandType,
    InboundMessage,
)
from syntra_build.application.commands.parser import (
    CommandParser,
    ParseFailure,
    ParseResult,
)
from syntra_build.application.commands.services import (
    CommandAuditRequest,
    CommandAuditSink,
    IntentResolver,
    LocalHealthService,
    ProjectCommandResult,
    ProjectCommandService,
    ProjectQueryService,
    ProjectResolution,
    ProjectSummary,
    ResolutionOutcome,
)

__all__ = [
    "READ_ONLY_COMMANDS",
    "STATE_CHANGING_COMMANDS",
    "Command",
    "CommandAuditRequest",
    "CommandAuditSink",
    "CommandParser",
    "CommandResponse",
    "CommandType",
    "InboundMessage",
    "IntentResolver",
    "LocalHealthService",
    "ParseFailure",
    "ParseResult",
    "ProjectCommandResult",
    "ProjectCommandService",
    "ProjectQueryService",
    "ProjectResolution",
    "ProjectSummary",
    "ResolutionOutcome",
]
