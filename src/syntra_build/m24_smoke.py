"""Bounded host seam for one exact-head M24 Architect review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from syntra_build.adapters.architect.openai import OpenAIArchitectProvider
from syntra_build.adapters.github.pull_requests import GitHubPullRequestAdapter
from syntra_build.application.architect_review import ArchitectReviewService
from syntra_build.domain.identifiers import MilestoneId, ProjectId
from syntra_build.infrastructure.persistence.connection import open_database
from syntra_build.m18_smoke import load_m18_host_config


def main() -> int:
    parser = argparse.ArgumentParser(description="Review one exact CI-passing PR head")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--milestone-id", required=True)
    parser.add_argument("--expected-head-sha", required=True)
    parser.add_argument("--correlation-id", required=True)
    args = parser.parse_args()
    config = load_m18_host_config()
    key = config.secrets.architect_api_key
    if not config.architect.enabled or key is None or config.architect.model is None:
        raise RuntimeError("Architect configuration is required")
    provider = OpenAIArchitectProvider(
        api_key=key.value,
        model=config.architect.model,
        reasoning_effort=config.architect.reasoning_effort,
        timeout_seconds=config.architect.api_timeout_seconds,
    )
    github = GitHubPullRequestAdapter(config)
    with open_database(args.database) as connection:
        result = ArchitectReviewService(connection, github, github, provider).review(
            ProjectId.from_string(args.project_id),
            MilestoneId.from_string(args.milestone_id),
            args.correlation_id,
            expected_head_sha=args.expected_head_sha,
        )
    print(
        json.dumps(
            {
                "review_id": result.id,
                "reviewed_sha": result.reviewed_sha,
                "verdict": result.verdict,
                "finding_count": result.finding_count,
                "superseded": result.superseded_at is not None,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
