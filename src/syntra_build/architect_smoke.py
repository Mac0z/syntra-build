"""Explicit, single-call real-provider acceptance command (never used by CI)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from syntra_build.adapters.architect import OpenAIArchitectProvider
from syntra_build.application.architect import ArchitectDesignService
from syntra_build.application.design import ProjectDesignContextService
from syntra_build.domain import ProjectId
from syntra_build.infrastructure.config import SecretInputs, SecretValue, load_config
from syntra_build.infrastructure.persistence import (
    SQLiteArchitectInteractionRepository,
    SQLiteDesignMessageRepository,
    SQLiteProjectDecisionRepository,
    SQLiteProjectDocumentRepository,
    SQLiteProjectRepository,
    bootstrap_database,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Perform exactly one persisted Architect design call"
    )
    parser.add_argument("project_id")
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/syntra-build/config.json")
    )
    parser.add_argument(
        "--api-key-file", type=Path, default=Path("/etc/syntra-build/openai-api-key")
    )
    args = parser.parse_args()
    if args.api_key_file.stat().st_mode & 0o077:
        raise RuntimeError("Architect API key file must have mode 0600")
    key = SecretValue(args.api_key_file.read_text().strip())
    config = load_config(
        json.loads(args.config.read_text()), secrets=SecretInputs(architect_api_key=key)
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
        provider = OpenAIArchitectProvider(
            api_key=key.value,
            model=config.architect.model,
            reasoning_effort=config.architect.reasoning_effort,
            timeout_seconds=config.architect.api_timeout_seconds,
        )
        result = ArchitectDesignService(
            context,
            SQLiteArchitectInteractionRepository(connection),
            provider,
            reasoning_effort=config.architect.reasoning_effort,
        ).design(ProjectId.from_string(args.project_id), args.correlation_id)
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        print(json.dumps(provider.telemetry(), indent=2, sort_keys=True))
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
