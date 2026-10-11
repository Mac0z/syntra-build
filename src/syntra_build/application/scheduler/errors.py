"""Classifiable scheduler failures."""


class SchedulerError(RuntimeError):
    """Base class for M12 scheduler failures."""


class InvalidSchedulerConfigurationError(SchedulerError):
    """Worker capacity configuration is incomplete or invalid."""


class SchedulerCapacityExhaustedError(SchedulerError):
    """No worker slot is currently available."""


class CapacityLeaseError(SchedulerError):
    """A capacity lease was used inconsistently."""


class ExecutorUnavailableError(SchedulerError):
    """No executor is registered for a queued job's worker class."""


class SchedulerDrainingError(SchedulerError):
    """New dispatch is disabled while the scheduler drains."""


class WorkerStartError(SchedulerError):
    """A claimed worker could not begin or complete local execution."""


class WorkerExecutionError(SchedulerError):
    """A worker raised after dispatch; details are limited to safe categories."""

    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        safe_category: str,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.safe_category = safe_category
