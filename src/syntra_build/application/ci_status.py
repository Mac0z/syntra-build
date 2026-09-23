"""Concise status/Telegram projection for M23 required-check progress."""

from syntra_build.domain.ci import CICheckStatus, CIProgress


def format_ci_progress(progress: CIProgress) -> str:
    def state(check_status: CICheckStatus, conclusion: object) -> str:
        if check_status is CICheckStatus.COMPLETED and conclusion is not None:
            return str(conclusion)
        return check_status.value

    checks = ". ".join(
        f"{item.name}: {state(item.status, item.conclusion)}"
        for item in progress.required_checks
    )
    check_text = checks or "Required checks are not observable yet"
    next_action = (
        "Architect review will begin after required CI passes."
        if progress.overall_status.value in {"QUEUED", "RUNNING", "UNKNOWN"}
        else "Syntra will process the terminal CI result."
    )
    return (
        f"PR #{progress.pull_request_number} CI is "
        f"{progress.overall_status.value.lower()} "
        f"for {progress.head_sha[:7]}. {check_text}. No human action is required. "
        f"{next_action}"
    )
