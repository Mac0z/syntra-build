"""Reviewed pre-install safety entry point, usable before M30 is installed."""

from __future__ import annotations

import argparse
from pathlib import Path

from syntra_build.admin import _config
from syntra_build.infrastructure.backup import BackupReason, SQLiteBackupService
from syntra_build.infrastructure.config.host import DEFAULT_HOST_CONFIG_PATH


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_HOST_CONFIG_PATH)
    args = parser.parse_args(argv)
    config = _config(args.config)
    result = SQLiteBackupService(
        config.database.sqlite_path, config.filesystem.backup_root
    ).create(BackupReason.PRE_UPGRADE)
    print(
        f"verified_pre_upgrade_backup={result.path} schema={result.schema_version} "
        f"bytes={result.byte_size}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
