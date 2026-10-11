"""Concise status/Telegram projection for M23 required-check progress."""

from syntra_build.domain.ci import CICheckStatus, CIDiagnosis, CIProgress


def ci_diagnosis_action(diagnosis: CIDiagnosis) -> str:
    if diagnosis in {
        CIDiagnosis.PROVIDER_AUTHENTICATION,
        CIDiagnosis.PROVIDER_REJECTION,
        CIDiagnosis.PROVIDER_MALFORMED,
        CIDiagnosis.PROVIDER_UNAVAILABLE,
    }:
        return (
            "Verify Syntra's GitHub access and GitHub API availability, "
            "then resolve the recorded CI blocker."
        )
    return (
        "Verify the approved workflow exists for this PR head, its triggers and "
        "filters, GitHub Actions is enabled, and Syntra's GitHub access; "
        "then resolve the recorded CI blocker."
    )


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
    if progress.diagnosis is not None:
        if checks:
            check_text = f"Last observed checks: {checks}"
        diagnosis = progress.diagnosis_summary or progress.diagnosis.value
        if progress.next_check_at is None:
            ci_state = "CI monitoring is blocked"
            action = "Human action is required. " + ci_diagnosis_action(
                progress.diagnosis
            )
        else:
            ci_state = f"CI is {progress.overall_status.value.lower()}"
            action = (
                f"Syntra will recheck at {progress.next_check_at.isoformat()}. "
                "Architect review remains gated on required CI passing."
            )
        return (
            f"PR #{progress.pull_request_number} {ci_state} for {progress.head_sha}. "
            f"{check_text}. Diagnosis: {progress.diagnosis.value} — "
            f"{diagnosis}. {action}"
        )
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
