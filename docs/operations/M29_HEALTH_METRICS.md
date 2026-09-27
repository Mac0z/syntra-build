# M29 operational health and metrics

Syntra exposes a local, observation-only HTTP surface. `STARTING`, `RECOVERING`, and
`DRAINING` are alive but not ready; `HEALTHY` is ready; `DEGRADED` remains ready;
`UNHEALTHY` returns 503 from health and readiness. `/health` is otherwise 200,
`/ready` is 200 only when ready, and `/metrics` is Prometheus text. Unknown paths are
bounded JSON 404 responses.

Disk space is sampled on the filesystem containing `data_root`. Configured security
thresholds (defaults 20/10/5 percent free) respectively warn, suppress new Codex
dispatch, and suppress every implementation worker. Denied jobs remain queued and
running work is never cancelled. Messaging, status, health, and metrics remain usable.
Artifact bytes cover only `data_root/artifacts` and are TTL-cached.

Metrics use the `syntra_build_` prefix: readiness/health, projects and jobs by bounded
state, worker capacity, disk and artifact bytes, persisted Codex/Architect/CI totals,
state transitions, duration histograms, API failures, and resource-guard denials.
Labels never include IDs, names, repositories, paths, URLs, SHAs, prompts, errors, or
secrets. Scrapes use short-lived SQLite read connections and make no provider calls.

Prometheus configuration (default local bind):

```yaml
scrape_configs:
  - job_name: syntra-build
    static_configs:
      - targets: ["127.0.0.1:9464"]
```

Useful Grafana PromQL includes `syntra_build_ready`,
`syntra_build_disk_free_percent`, `sum by (state) (syntra_build_projects)`,
`sum by (worker_class,state) (syntra_build_jobs)`,
`rate(syntra_build_codex_runs_total[5m])`, histogram quantiles over each
`*_duration_seconds_bucket`, `rate(syntra_build_api_failures_total[5m])`,
`rate(syntra_build_state_transitions_total[5m])`, `syntra_build_artifact_bytes`, and
`rate(syntra_build_resource_guard_denials_total[5m])`. Alert when readiness is zero,
disk free is below 20/10/5, or API failure rate is elevated.

Run `python -m syntra_build.m29_smoke` for a bounded loopback probe, or
`python -m syntra_build.m29_smoke --serve-seconds 60` for a bounded host scrape.
The command uses host configuration and schema 25 without dispatching or mutation.
M30 retains ownership of daemon/systemd lifecycle, graceful shutdown, backup/restore,
and administrative CLI behavior.
