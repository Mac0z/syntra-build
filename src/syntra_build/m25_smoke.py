"""Bounded host acceptance seam for one already-notified M25 gate."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from syntra_build.application.commands.models import Command, CommandType
from syntra_build.application.human_intervention import HumanInterventionService
from syntra_build.domain.identifiers import GateId
from syntra_build.infrastructure.persistence.connection import open_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve one persisted M25 human gate")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--responder-id", required=True)
    parser.add_argument(
        "--response", choices=("PASS", "FAIL", "BLOCKED"), required=True
    )
    parser.add_argument("--feedback")
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    with open_database(args.database) as connection:
        service = HumanInterventionService(
            connection, authorised_responder_ids=frozenset({args.responder_id})
        )
        gate = service.gate_repository.get(GateId.from_string(args.gate_id))
        result = service.respond(
            Command(
                CommandType.RESPOND_GATE,
                args.responder_id,
                datetime.now(UTC),
                "telegram",
                f"m25-smoke-{uuid4()}",
                f"m25-smoke-{uuid4()}",
                args.correlation_id,
                gate_reference=args.gate_id,
                gate_response=args.response,
                gate_feedback=args.feedback,
            ),
            gate,
        )
        resolved = service.gate_repository.get(gate.id)
        milestone = service.milestones.get(
            service._milestone_id(resolved), resolved.project_id
        )
    print(
        json.dumps(
            {
                "gate_id": str(resolved.id),
                "gate_state": resolved.state.value,
                "milestone_state": milestone.state.value,
                "result": result,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
