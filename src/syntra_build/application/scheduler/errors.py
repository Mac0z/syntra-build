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
