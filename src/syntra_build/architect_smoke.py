"""Explicit, single-call real-provider acceptance command (never used by CI)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.adapters.architect import OpenAIArchitectProvider
from syntra_build.adapters.telegram import TelegramClient, TelegramGateNotifier
from syntra_build.application.architect import ArchitectDesignService
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.application.gates import HumanGateService
from syntra_build.application.specification import (
    SpecificationDraftService,
    build_specification_request,
    execute_specification_draft,
)
from syntra_build.domain import (
    ArchitectDesignResponse,
    GateState,
    ProjectId,
    SpecificationDraft,
)
from syntra_build.infrastructure.config import (
    ApplicationConfig,
    SecretInputs,
    load_config,
    read_protected_secret_file,
)
from syntra_build.infrastructure.persistence import (
    SQLiteArchitectInteractionRepository,
    SQLiteDesignMessageRepository,
    SQLiteDesignPackageRepository,
    SQLiteHumanGateRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    SQLiteTelegramGateNotificationRepository,
    bootstrap_database,
)


def _load_host_config(
    config_path: Path,
    architect_key_path: Path,
    telegram_token_path: Path,
    github_token_path: Path,
) -> ApplicationConfig:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return load_config(
        raw,
        environ={},
        secrets=SecretInputs(
            architect_api_key=read_protected_secret_file(
                architect_key_path, "Architect API key"
            ),
            telegram_bot_token=read_protected_secret_file(
                telegram_token_path, "Telegram token"
            ),
            github_token=read_protected_secret_file(github_token_path, "GitHub token"),
        ),
    )


def _build_provider(config: ApplicationConfig) -> OpenAIArchitectProvider:
    key = config.secrets.architect_api_key
    if key is None:
        raise RuntimeError("OpenAI Architect credential is unavailable")
    assert config.architect.model is not None
    return OpenAIArchitectProvider(
        api_key=key.value,
        model=config.architect.model,
        reasoning_effort=config.architect.reasoning_effort,
        timeout_seconds=config.architect.api_timeout_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Perform exactly one persisted Architect design call"
    )
    parser.add_argument("project_id")
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument(
        "--specification-draft",
        action="store_true",
        help="spend one provider call drafting SPEC/AGENTS; does not create a package",
    )
    parser.add_argument(
        "--create-package",
        action="store_true",
        help="create/recover an M17 package and notify its Telegram approval gate",
    )
    parser.add_argument(
        "--telegram-chat-id",
        type=int,
        help="destination chat required with --create-package",
    )
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/syntra-build/config.json")
    )
    parser.add_argument(
        "--api-key-file", type=Path, default=Path("/etc/syntra-build/openai-api-key")
    )
    parser.add_argument(
        "--telegram-token-file",
        type=Path,
        default=Path("/etc/syntra-build/telegram-token"),
    )
    parser.add_argument(
        "--github-token-file",
        type=Path,
        default=Path("/etc/syntra-build/github-token"),
    )
    args = parser.parse_args()
    if args.specification_draft and args.create_package:
        parser.error("choose either --specification-draft or --create-package")
    if args.create_package and args.telegram_chat_id is None:
        parser.error("--create-package requires --telegram-chat-id")
    config = _load_host_config(
        args.config,
        args.api_key_file,
        args.telegram_token_file,
        args.github_token_file,
    )
    if (
        not config.architect.enabled
        or config.architect.provider != "openai"
        or config.architect.model is None
    ):
        raise RuntimeError("OpenAI Architect must be enabled and configured")
    connection = bootstrap_database(config)
    try:
        context = ProjectDesignContextService(
            SQLiteProjectRepository(
                connection, lambda: "smoke-does-not-create-projects"
            ),
            SQLiteDesignMessageRepository(connection),
            SQLiteProjectDecisionRepository(connection),
            SQLiteProjectDocumentRepository(connection),
        )
        provider = _build_provider(config)
        project_id = ProjectId.from_string(args.project_id)
        audit = SQLiteArchitectInteractionRepository(connection)
        result: ArchitectDesignResponse | SpecificationDraft
        if args.create_package:
            packages = SQLiteDesignPackageRepository(connection)
            package = packages.pending_for_project(project_id)
            if package is None:
                package = SpecificationDraftService(
                    context,
                    audit,
                    provider,
                    packages,
                    SQLiteProjectDocumentRepository(connection),
                    SQLiteHumanGateRepository(connection, lambda: str(uuid4())),
                    SQLiteProjectRepository(connection, lambda: str(uuid4())),
                    reasoning_effort=config.architect.reasoning_effort,
                ).generate(project_id, args.correlation_id)
            gate_repository = SQLiteHumanGateRepository(
                connection, lambda: str(uuid4())
            )
            gate = gate_repository.get(package.approval_gate_id)
            if gate.state is GateState.PENDING:
                HumanGateService(
                    gate_repository,
                    response_id_factory=lambda: str(uuid4()),
                    authorised_responder_ids=frozenset(
                        str(item) for item in config.telegram.authorised_user_ids
                    ),
                ).notify(
                    gate.id,
                    TelegramGateNotifier(
                        TelegramClient(config),
                        args.telegram_chat_id,
                        notifications=SQLiteTelegramGateNotificationRepository(
                            connection
                        ),
                    ),
                    occurred_at=datetime.now(UTC),
                )
            print(f"Design package: {package.id}")
            print(f"Gate: {package.approval_gate_id}")
            return 0
        if args.specification_draft:
            request = build_specification_request(
                context.reconstruct(project_id),
                args.correlation_id,
                SQLiteDesignPackageRepository(connection).feedback(project_id),
            )
            request_id = f"host-draft-{args.correlation_id}"
            result = execute_specification_draft(
                audit,
                provider,
                request,
                request_id=request_id,
                reasoning_effort=config.architect.reasoning_effort,
                clock=lambda: datetime.now(UTC),
            )
        else:
            result = ArchitectDesignService(
                context,
                audit,
                provider,
                reasoning_effort=config.architect.reasoning_effort,
            ).design(project_id, args.correlation_id)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        print(json.dumps(provider.telemetry(), indent=2, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
