"""Bounded host acceptance entry point for persisted M27 crash fixtures."""

import argparse
from pathlib import Path

from syntra_build.application.recovery import RecoveryCoordinator
from syntra_build.application.scheduler import Scheduler, WorkerCapacity
from syntra_build.infrastructure.config import SchedulerConfig
from syntra_build.infrastructure.persistence import (
    SQLiteJobRepository,
    apply_migrations,
    open_database,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reconcile a controlled M27 database")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    with open_database(args.database) as connection:
        apply_migrations(connection)
        scheduler = Scheduler(
            SQLiteJobRepository(connection, lambda: "smoke-job-history"),
            WorkerCapacity(SchedulerConfig().worker_class_limits()),
            {},
        )
        try:
            coordinator = RecoveryCoordinator(connection, scheduler)
            run_id = coordinator.recover()
            for row in coordinator.repository.observations(run_id):
                print(
                    f"{row['project_id']} {row['category']} "
                    f"{row['disposition']} {row['resulting_action']}"
                )
        finally:
            scheduler.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
