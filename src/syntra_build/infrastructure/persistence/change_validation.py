"""Append-only persistence for M21 validation evidence."""

from __future__ import annotations

import json
import sqlite3

from syntra_build.domain.change_validation import ChangeSet


class SQLiteValidationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save(self, change_set: ChangeSet) -> None:
        files = [
            {
                "path": item.path,
                "status": item.status,
                "staged": item.staged,
                "binary": item.binary,
                "content_hash": item.content_hash,
            }
            for item in change_set.files
        ]
        self.connection.execute(
            """INSERT INTO change_sets
            (id,interface_version,project_id,milestone_id,worktree_id,branch_name,
             base_sha,head_sha_before_commit,diff_hash,is_empty,files_json,decision,
             correlation_id,scanner_version,policy_version,created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                change_set.id,
                change_set.interface_version,
                str(change_set.project_id),
                str(change_set.milestone_id),
                change_set.workspace_id,
                change_set.branch_name,
                change_set.base_sha,
                change_set.trusted_head_sha,
                change_set.diff_hash,
                change_set.is_empty,
                json.dumps(files, sort_keys=True),
                change_set.decision.value,
                change_set.correlation_id,
                change_set.scanner_version,
                change_set.policy_version,
                change_set.created_at.isoformat(),
            ),
        )
        for finding in change_set.findings:
            self.connection.execute(
                """INSERT INTO validation_findings VALUES
                (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    finding.id,
                    finding.change_set_id,
                    finding.code.value,
                    finding.severity.value,
                    finding.path,
                    finding.location,
                    finding.fingerprint,
                    finding.message,
                    finding.remediation,
                    finding.blocking,
                    finding.created_at.isoformat(),
                    change_set.correlation_id,
                ),
            )

    def accepted_hash(self, workspace_id: str, diff_hash: str) -> bool:
        return (
            self.connection.execute(
                """SELECT 1 FROM change_sets WHERE worktree_id=? AND diff_hash=?
            AND decision='ACCEPT' ORDER BY created_at DESC LIMIT 1""",
                (workspace_id, diff_hash),
            ).fetchone()
            is not None
        )
