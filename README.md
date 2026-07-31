# cxxprobe-worker

A job-execution daemon for [cxxprobe](https://github.com/Algorithms-Mathematics-Society/cxxprobe).

It claims jobs from a queue, runs `cxxprobe judge` in an isolated workspace,
persists the resulting artifacts, and reports health. That is the whole of
its job.

## What it deliberately does not do

No judging logic lives here. There are no checkers, validators, generators,
verdicts, problems, or contests in this codebase — all of that is cxxprobe's,
and the worker reaches it through exactly one interface:

```
cxxprobe judge (--package ZIP | --problem-dir DIR) --submission FILE --output REPORT.json
```

If you find yourself wanting to parse a verdict or understand a package here,
the change almost certainly belongs in cxxprobe instead.

## Responsibilities

| Area | What it means |
|---|---|
| **Job execution** | Claim a job, invoke `cxxprobe judge`, classify the outcome |
| **Workspace management** | A private directory per job, removed when the job ends |
| **Artifact management** | Persist the report, logs, and submission past the workspace's life |
| **Monitoring** | Structured logs, counters, and a heartbeat file |

## Quick start

```bash
uv sync --all-groups

# Check the environment before running anything real.
uv run cxxprobe-worker doctor --env test

# Queue a job and run it.
uv run cxxprobe-worker submit --package ./my-problem --submission ./solution.cpp
uv run cxxprobe-worker once

# Or run the poll loop.
uv run cxxprobe-worker run --env test
```

## Commands

| Command | Purpose |
|---|---|
| `run` | Poll the queue until stopped (SIGINT/SIGTERM drain in-flight jobs first) |
| `once` | Claim and run at most one job. Exit 0 success, 1 failure, 3 empty queue |
| `submit` | Enqueue a job — for local testing and manual rejudges |
| `status` | Queue depth plus the last heartbeat |
| `doctor` | Verify the cxxprobe binary is runnable and every configured path is writable |

## Configuration

One YAML file per environment under `config/`, selected with `--env`:

```
config/test.yaml       everything under /tmp, verbose text logs
config/staging.yaml    prod's shape, lower concurrency, debug logs
config/prod.yaml       concurrency 4, JSON logs, recycle after 1000 jobs
```

Unknown keys are rejected rather than ignored, so a typo is a startup failure
instead of a setting that silently never applied.

Any field can be overridden from the environment with a `CXXPROBE_WORKER_`
prefix and `__` for nesting — how a container tunes a baked-in image:

```bash
CXXPROBE_WORKER_CONCURRENCY=8 \
CXXPROBE_WORKER_JUDGE__TIMEOUT_SECONDS=120 \
  cxxprobe-worker run --env prod
```

## How a job is classified

The **exit code**, not the presence of a report, decides whether judging
happened:

| cxxprobe exit | Report | Outcome | Requeued? |
|---|---|---|---|
| 0 | yes | `succeeded` | no |
| 1 (a real WA/TLE verdict) | yes | `succeeded` | no |
| 0 or 1 | missing | `failed` | no |
| 2 (could not judge) | either | `retryable` | yes, up to `max_delivery_attempts` |

Two details worth stating plainly, because getting either wrong loses work:

- **Exit 1 is a success.** The submission was wrong; that is a normal, final
  outcome. Retrying it would redo the same work for ever.
- **Exit 2 with a report is still not a verdict.** cxxprobe writes a report
  on exit 2 as well — on a host without cgroup delegation it emits one whose
  `overall` is `ERROR`. Recording that as a verdict would fail a submission
  that was never actually run.

Retryable jobs are redelivered up to `queue.max_delivery_attempts` (default 5)
and then parked in `<queue root>/dead/`, because a retryable classification
cannot distinguish "this host is broken" from "this package is broken" on the
first attempt.

## Storage and queue backends

`IArtifactStorage` and `IJobQueue` are protocols. Only the filesystem
implementations exist today — `FilesystemArtifactStorage` and `LocalJobQueue`
— and they are real implementations, not stubs: the local queue has atomic
claims (a POSIX rename), visibility timeouts, redelivery, and a dead-letter
path.

The lease model matches SQS deliberately, so a future S3/SQS backend is a new
file rather than a refactor. No such backend is written yet.

## Development

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # strict type check
uv run pytest                # 96 tests
uv run pytest --cov=cxxprobe_worker --cov-report=term-missing
```

The integration suite runs against a real cxxprobe build and is skipped
without one:

```bash
CXXPROBE_BINARY=/path/to/cxxprobe uv run pytest tests/test_integration.py -v
```

Those tests also skip on a host without cgroup delegation, since the worker
correctly reports that as `retryable` and there is nothing further to assert.
Everything else in the suite runs everywhere, using a scripted stand-in that
reproduces cxxprobe's exit-code contract exactly.
