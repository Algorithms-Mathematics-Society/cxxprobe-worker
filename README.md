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
| `recover-result` | Inspect or enqueue one exact saved callback; dry-run by default |
| `cancel` | Cancel a queued benchmark job; any bencher that claims it refuses it |
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

## Selecting one problem or project part

Ordinary jobs (`job_type: problem`), including custom runs and rejudges, may
set `problem_slug` to an **exact engine identity**. A plain problem uses its
folder basename; a declared project part uses `project-slug/part-basename`.
For example, `desk/nested/beta` on disk, declared as `dir: nested/beta` in
`desk/project.yaml`, has identity `desk/beta`. Its part label is not a selector.

For ZIP inputs (including S3 packages), the worker uses `cxxprobe package unpack`
in the private workspace, resolves that identity against the declared project
parts or plain problems, and invokes `cxxprobe judge --problem-dir` on the
selected directory. Directory inputs may be a plain problem, a project root,
or a contest root. Shared headers and sources stay alongside the selected part.
This follows the engine's `project_discovery.cpp` identity contract; `judge`
has no selector flag and its direct `--package` mode accepts only one plain
problem. Unknown/ambiguous selectors and escaping part paths fail permanently,
without choosing the first part or substituting a reference solution.

Omit `problem_slug` to retain the existing single-problem command path. An empty
string is an invalid selector, not an omission. Project and benchmark jobs
continue to use their existing `project_slug` and part-label contracts.

## Benchmark jobs

A worker is either a judge or a bencher (`role: judge | bencher`), never both.
A bencher (`config/bench.yaml`) runs exactly one job at a time, only benchmark
jobs, from its own queue; a job for the other role goes back on the queue
untouched. See `deploy/README.md` for the pool it runs on.

```json
{"job_id": "b-123", "job_type": "benchmark",
 "package_path": "s3://bucket/packs/round-2.cxxpkg", "project_slug": "order-desk",
 "parts": {"1": "s3://bucket/subs/t7/1.cpp", "2": "...", "3": "..."},
 "team_id": "team-7", "revision": "9f1c2ab", "revision_seq": 41,
 "benchmark_input": "s3://bucket/bench/round-2/order-desk/input-r2.txt"}
```

Every part must be supplied (no fallbacks), the hidden `benchmark_input` is
staged by the worker and never part of the package, and the job runs
`cxxprobe bench project`, which builds the parts once, verifies the binary's
output on that input against the reference parts, and only then times that
same binary (cxxprobe's `schemas/benchmark-result.md`).

| Engine result | Job outcome | Scored? |
|---|---|---|
| `OK` | `succeeded`; the team's newest `revision_seq` is recorded | yes |
| `NOISY` | `retryable` | no, retried |
| `FAILED_VERIFY` | `failed`, with the reason (`rejected from benchmarking: ...`) | no |
| superseded, cancelled, rate-limited | `failed` before anything is fetched | no |
| bencher unhealthy (calibration) | `retryable` for a healthy bencher | no |

The published result carries the engine's record unchanged plus its
provenance, under `benchmark`: `status`, `median_ns`, `iqr_ns`,
`instructions`, `repetitions`, `pinned_core`, `machine_id`,
`calibration_ok`, `input_hash` (SHA-256 of the input actually timed),
`revision_hash`, `team_id`, `revision_seq`, and the per-part profile shares.
Results are only comparable within one `input_hash`, and only an `OK` record
with `calibration_ok: true` may rank. The flamegraph and folded stacks are
stored as `profile/flamegraph.svg` and `profile/stacks.folded`.

**Calibration.** A bencher runs `cxxprobe bench calibrate` before it claims
anything, then every `bench.calibration_every_seconds` or
`calibration_every_jobs` jobs. A noisy or drifted calibration stops claiming
and writes `unhealthy: <reason>` to the health file until a later calibration
passes. Set `bench.calibration_baseline_ns` to the pool's measured median so
every machine is held to the same speed.

**Supersession and rate limits.** Benchers coordinate through markers in the
shared artifact storage under the reserved `_bench` namespace: a job older
than its team's newest benchmarked `revision_seq` is refused before it runs and
again before its result is published, so a late older result never
overwrites a newer one; `cxxprobe-worker cancel <job_id>` refuses a queued job;
and a team's benchmarks are at least `bench.min_team_interval_seconds` apart.
The markers are best-effort across machines, so the consumer must also keep
the highest `revision_seq` per team, which every result carries.

## Storage and queue backends

`IArtifactStorage` and `IJobQueue` have filesystem/local and S3/SQS implementations.
The local queue has atomic claims, visibility timeouts, delayed publishing,
receive accounting across restarts, and a `dead/` directory. SQS publishing can
target any configured priority, primary, or secondary tier with a bounded delay.

## Durable callback delivery (opt-in)

`result_delivery_enabled` defaults to **false**. The optional
`metadata.attempt_token` must be a UUID string; it is canonicalized and forwarded
in `JobResult.attempt_token` and the HTTP callback even when production is off.
Tokenless jobs, including legacy synthetic probes, keep the original path and
are **never cached by job_id**. The API can reuse a job ID for a genuine new judge
attempt, so reusing a tokenless error would poison every future retry.

For a token-bearing job with production enabled, the worker stores an immutable
complete callback at:

```
<storage prefix>/<job_id>/results/<sha256(attempt_token)>/<time_ns>-<execution_uuid>.json
```

The envelope contains the exact HTTP payload, the original job and queue tier,
and compact result metadata. It does not duplicate the raw `JobResult.report`;
the ordinary `report.json` artifact holds that report. Replay returns compact
result metadata and sends the saved callback without reinterpreting its report
or replacing its original worker UID or attempt token. No candidate output is
newly truncated. A preparation failure or a final exhausted execution retry is
saved too. Transient `RETRYABLE` executions still release their original message.

Before executing a token-bearing source job, readers list **only that attempt's
prefix**, with a hard 32-envelope limit. They replay a saved callback without
executing again. Listing/read/validation failure retains the source instead of
falling back to execution. Concurrent in-flight executions before either writes
an envelope remain possible under the queue's at-least-once contract; their
artifacts have unique keys and cannot overwrite each other.

After an upload failure, the worker publishes a job-shaped `metadata.delivery`
marker referencing the same envelope to the **original queue tier**, then deletes
the source. Marker processing always precedes execution and never runs the judge.
Storage, successor-send, DLQ-send, or source-delete failure leaves the source
unacknowledged. A lost HTTP response or a duplicate queue delivery can repeat the
identical callback; API attempt-token idempotency handles that repetition.

Each automatic chain keeps a fixed deadline (default one hour) and round limit
(default eight). Successor receives, including failed sends/restarts, count
against its budget. Delays start at two seconds, double, and cap at 300 seconds.
On exhaustion the worker retains the envelope, emits `result.recovery_required`
with the exact job/artifact/token/tier, publishes an exhausted marker to the DLQ,
then deletes the source. If backend operations remain unavailable, ordinary queue
visibility/redrive is the backstop. Keep `queue.max_delivery_attempts` aligned with
SQS's native `maxReceiveCount`.

### Activation and rollback

1. Deploy the API companion with optional attempt-token emission still off.
   Deploy this worker to **every queue reader**, including standby and Spot
   launch templates. Old readers can parse a job-shaped marker but would execute
   it, so mixed-reader activation is unsafe.
2. Enable API UUID token emission for new jobs. Verify forwarding and API
   idempotency first. A known retired token is acknowledged; an unknown token
   receives HTTP 409, which is a failed delivery, not permission to drop a verdict.
3. Configure an **existing, distinct DLQ** and permissions, then enable production:

   ```yaml
   result_delivery_enabled: true
   result_delivery_max_rounds: 8
   result_delivery_deadline_seconds: 3600
   queue:
     dead_letter_queue_url: https://sqs.REGION.amazonaws.com/ACCOUNT/EXISTING-DLQ
   ```

   The DLQ must differ from every polled tier after URL normalization; both config
   and queue boundaries reject overlap, including during rollback. This change
   provisions no queues, IAM roles, or lifecycle rules.

Turning production off stops **new envelope writes**. Readers of existing
markers and cached source envelopes remain active with the flag off. Keep this
worker version, storage access, token protocol, and DLQ permissions until all
saved deliveries have drained. Marker bodies with missing storage, invalid
identity, or a disabled control plane are retained and never executed.

S3/Spot deployments need `s3:PutObject` and `s3:GetObject` on the configured
artifact keys plus `s3:ListBucket` restricted to the artifact/job/attempt prefixes.
The SDK must support `PutObject.IfNoneMatch`; immutable writes fail closed if it
does not. Filesystem envelopes use same-filesystem atomic publication and fsync
of data and directory entries. They survive process restarts on the **same host**,
not host/disk loss. Use shared S3 artifacts for Spot or cross-host recovery.

All readers need `sqs:SendMessage` on **every original queue tier** and the
configured DLQ, in addition to their existing receive/delete/visibility rights.
These permissions remain necessary during rollback. Configure artifact retention
and DLQ retention longer than the automatic deadline **plus the operator response
window**; no automatic envelope deletion is introduced. Lifecycle expiry before
recovery removes the only authoritative callback. Alert on
`result.recovery_required` and monitor the DLQ.

### Explicit recovery

The recovery command reads one exact job/artifact key, never scans a bucket or
walks unrelated jobs, and defaults to a dry-run:

```bash
uv run cxxprobe-worker recover-result --env prod --job-id JOB_UUID \
  --artifact results/TOKEN_HASH/EXECUTION.json --dry-run
# After inspection, start one new bounded delivery window on the original tier:
uv run cxxprobe-worker recover-result --env prod --job-id JOB_UUID \
  --artifact results/TOKEN_HASH/EXECUTION.json --enqueue
```

Obtain the key from `result.persisted` / `result.recovery_required` logs or an
exhausted DLQ marker. `--enqueue` preserves the exact callback and original token;
it resets only the explicit manual recovery round/deadline window and never
rejudges. The artifact remains immutable. The command does not delete an existing
DLQ message. A successful enqueue means queued for replay, not accepted by the
API. The control plane must be enabled. Standalone judging remains available with
no control plane and does not produce delivery envelopes.

Durability starts when the envelope write succeeds. Host loss between judging
and that write can still require execution again. No artifact store or queue
alone can make HTTP delivery exactly-once; the API companion supplies idempotency.

## Development

```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # strict type check
uv run pytest                # unit tests; real-engine integrations may skip
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

CI runs these three cases in mandatory mode:

```bash
CXXPROBE_BINARY=/path/to/cxxprobe uv run pytest tests/test_integration.py --require-real-engine -v
```

Mandatory mode fails on missing prerequisites, failed scaffolding/packaging,
unavailable sandboxing, skipped cases, or missing cases. The CI integration job
builds the exact engine commit recorded in `.github/workflows/ci.yml`, delegates
an isolated cgroup tree, and runs the tests as the normal runner user. The source
commit is verified before building; no release token or unverified binary download
is needed. This compatibility pin is independent of the fleet release pin in
`deploy/spot-user-data.sh`; updating CI does not deploy a new engine.

A local mandatory run requires the test shell inside the delegated cgroup tree,
with CPU, memory, and process controllers enabled. Optional local runs retain
the skips described above. CI uploads a JUnit report so executed/skipped counts
are visible.
