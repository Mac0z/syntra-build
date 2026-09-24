"""Bounded real-Telegram host acceptance seam for one persisted M25 gate."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.adapters.telegram.client import TelegramClient
from syntra_build.adapters.telegram.gates import TelegramGateNotifier
from syntra_build.application.gates import HumanGateService
from syntra_build.domain.gates import GateState, GateType
from syntra_build.domain.identifiers import GateId
from syntra_build.infrastructure.config import load_host_config
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.infrastructure.persistence.cursors import (
    SQLiteProviderCursorRepository,
)
from syntra_build.infrastructure.persistence.gates import SQLiteHumanGateRepository
from syntra_build.infrastructure.persistence.telegram_interactions import (
    SQLiteTelegramGateNotificationRepository,
)
from syntra_build.smoke import build_host_router, run_telegram_once


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Notify one M25 gate and process one bounded Telegram poll"
    )
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument(
        "--config", type=Path, default=Path("/etc/syntra-build/config.json")
    )
    parser.add_argument(
        "--token-file", type=Path, default=Path("/etc/syntra-build/telegram-token")
    )
    parser.add_argument(
        "--github-token-file",
        type=Path,
        default=Path("/etc/syntra-build/github-token"),
    )
    parser.add_argument(
        "--architect-api-key-file",
        type=Path,
        default=Path("/etc/syntra-build/openai-api-key"),
    )
    args = parser.parse_args()
    config = load_host_config(
        args.config,
        telegram_token_path=args.token_file,
        github_token_path=args.github_token_file,
        architect_api_key_path=args.architect_api_key_file,
    )
    authorised = frozenset(str(item) for item in config.telegram.authorised_user_ids)
    client = TelegramClient(config)
    with open_database(config.database.sqlite_path) as connection:
        repository = SQLiteHumanGateRepository(connection, lambda: str(uuid4()))
        service = HumanGateService(
            repository,
            response_id_factory=lambda: str(uuid4()),
            authorised_responder_ids=authorised,
        )
        gate = repository.get(GateId.from_string(args.gate_id))
        if gate.gate_type not in {
            GateType.HUMAN_TEST,
            GateType.PRODUCT_DECISION,
            GateType.TECHNICAL_DECISION,
        }:
            raise RuntimeError("gate is not an M25 human intervention")
        if gate.state is GateState.PENDING:
            gate = service.notify(
                gate.id,
                TelegramGateNotifier(
                    client,
                    args.chat_id,
                    notifications=SQLiteTelegramGateNotificationRepository(connection),
                ),
                occurred_at=datetime.now(UTC),
            )
        processed = run_telegram_once(
            client,
            build_host_router(config, connection),
            SQLiteProviderCursorRepository(connection),
        )
        gate = repository.get(gate.id)
    print(
        json.dumps(
            {
                "gate_id": str(gate.id),
                "gate_state": gate.state.value,
                "processed_updates": processed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
