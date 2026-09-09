"""Explicit, single-call real-provider acceptance command (never used by CI)."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from syntra_build.adapters.architect import OpenAIArchitectProvider
from syntra_build.application.architect import ArchitectDesignService
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.application.specification import build_specification_request
from syntra_build.domain import ArchitectDesignResponse, ProjectId, SpecificationDraft
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
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
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
        if args.specification_draft:
            request = build_specification_request(
                context.reconstruct(project_id),
                args.correlation_id,
                SQLiteDesignPackageRepository(connection).feedback(project_id),
            )
            request_id = f"host-draft-{args.correlation_id}"
            started = datetime.now(UTC)
            audit.begin_draft(
                request_id=request_id,
                request=request,
                provider=provider.provider_name,
                model=provider.model,
                reasoning_effort=config.architect.reasoning_effort,
                created_at=started,
            )
            result = provider.draft_specification(request)
            usage = provider.telemetry()
            response_id = usage.get("provider_response_id")
            audit.succeed_draft(
                request_id=request_id,
                response=result,
                provider_response_id=response_id
                if isinstance(response_id, str)
                else None,
                usage=usage,
                completed_at=datetime.now(UTC),
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
