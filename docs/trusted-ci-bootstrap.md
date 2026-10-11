# Trusted CI bootstrap for generated repositories

Issue #73 Deliverable A extends M18 provisioning so each newly generated
repository starts with approved design documents and a trusted GitHub Actions
workflow in the same initial commit. This implements the control boundaries in
[ADR-005](adr/ADR-005-codex-no-privileged-github-credentials.md),
[ADR-006](adr/ADR-006-syntra-controls-git-pr-and-merge.md), and
[ADR-010](adr/ADR-010-github-actions-for-heavy-ci.md).

## Approve an explicit profile

The generated `SPEC.md` must contain exactly one `syntra-ci` fenced TOML block.
The first supported profile is `python-pytest-v1`, with Python `3.12`, `3.13`, or
`3.14`. Select the version that matches the project's approved runtime:

````markdown
```syntra-ci
profile = "python-pytest-v1"
python-version = "3.12"
```
````

The human approves this block as part of the versioned design package. Syntra
validates the approved document and selects only its bounded profile and version.
Missing, malformed, ambiguous, or unsupported selections fail provisioning
before repository operations. Project prose, arbitrary shell commands, and
provider-authored YAML cannot select or replace the trusted workflow.

The specification-drafting prompt asks the Architect to include this profile for
compatible Python projects. It also asks the Architect to surface the need for
an approved supported profile for non-Python or incompatible targets. Approving
such a design does not bypass the profile requirement; support must first be
implemented and reviewed in Syntra. Generated project runtimes are separate from
Syntra Build's own Python 3.14 runtime.

## What the profile runs

The generated `.github/workflows/ci.yml` workflow runs the `tests` job on pull
requests targeting `main`. It checks out the exact pull-request head SHA, installs
the approved Python version, installs project dependencies from a PEP 621
`pyproject.toml` (including the `dev` extra when declared), installs pytest, and
runs `python -I -m pytest -o addopts= --junitxml=...` with `PYTEST_ADDOPTS` cleared.
The profile ignores pytest `addopts` so settings such as `--collect-only` cannot
replace test execution. A JUnit report in a temporary directory outside the
worktree must contain at least one executed, non-skipped test and no failures or
errors. Python invocations use isolated mode so project files cannot shadow the
installed pytest, pip, or standard-library modules used by the trusted steps.
The first implementation milestone must provide the package metadata and
tests required by its approved design. Missing metadata, broken dependency
installation, failing tests, no collected tests, or all tests skipped fails CI.

The job runs on `ubuntu-24.04` with a 15-minute timeout. Its permissions are
limited to `contents: read`, checkout does not persist credentials, and the
template uses reviewed action commit SHAs. It exposes no secrets or privileged
token environment and has no pull-request-target trigger or dependency cache.

The initial repository contains the approved `SPEC.md`, approved `AGENTS.md`,
and deterministic workflow bytes. Workflow paths remain protected during coding
agent validation. Changes to them require a separately approved policy change;
a coding agent cannot replace required testing with a successful placeholder.
The versioned template is fixed: changing its actions or policy requires a new
reviewed profile ID.

## Provisioning and recovery

Before any repository side effect, Syntra durably records the CI intent alongside
the existing repository intent. It binds the profile, Python version, workflow
path, exact workflow bytes and hash, default branch, repository identity, and
approved document revisions and hashes. The initial commit uses these persisted
bytes. Retry compares the approved design and trusted policy with the recorded
intent rather than silently changing it.

After push, Syntra reads the workflow back from the exact baseline commit and
verifies its hash alongside the approved documents before marking the repository
verified or the project `READY`. Reconciliation after an ambiguous push checks
the same files and commit. A missing or changed workflow fails closed. The
`m18_smoke` result includes `ci_profile_id`, `ci_python_version`,
`ci_workflow_path`, and `ci_workflow_hash` for operator inspection.

## Migration and scope

Migration `029_trusted_ci_bootstrap` adds immutable `repository_ci_intents`.
It preserves existing repository, baseline, document, and event history and
does not backfill or rewrite previously provisioned repositories. An older
baseline without trusted CI intent cannot be resumed through the new bootstrap
path; it requires a separately reviewed remediation plan. In particular, this
change does not modify the live `m32-fx-reconciler` project.

Take a database backup before upgrade. Migrations are forward-only: rollback
requires restoring the pre-migration backup together with the previous
application version. Do not run an older daemon against a database containing
new CI intents or delete intent records to force reprovisioning.

Deliverables B and C of issue #73 are separate work. This bootstrap does not
retrofit existing repositories or authorize workflow-policy changes by coding
agents.
