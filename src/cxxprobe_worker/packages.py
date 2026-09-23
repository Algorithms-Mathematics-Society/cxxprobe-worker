"""A local cache of problem packages.

**Why this exists.** `_stage_inputs` fetched the package from S3 on *every*
job. In a contest that is one download per submission for a set of maybe
seven packages — roughly 42,000 fetches of 7 distinct files.

It barely matters when the worker shares a region with the bucket. It
matters a great deal when it does not, and on this account it cannot: the
Free Tier caps EC2 at 5-8 vCPU per region, so contest capacity only exists
spread across seventeen of them while the bucket stays in ap-south-1.
Measured 2026-09-20, the same workload ran at **0.73 jobs/s/vCPU** in the
bucket's own region and **0.40** spread out — a 46% loss, and two S3 round
trips per job at ~220 ms each against a ~0.5 s job accounts for all of it.

Caching the package removes one of those two round trips for every
submission after the first.

**Why a TTL and not an ETag check.** Validating with a HEAD costs the round
trip the cache exists to avoid — for a 4 KB package the cost *is* the round
trip, not the bytes. A package is immutable for the duration of a contest:
problems are finalised before it starts, and a rejudge after an edit goes
through a fresh contest anyway. So the cache keys on the URI and expires,
and the window is short enough that an edited package is picked up within
minutes rather than needing a worker restart.

The submission is never cached. It is different every time, which is the
whole point of it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

# How long a cached package is trusted. Short enough that editing a problem
# mid-rehearsal is picked up without restarting seventeen regions' worth of
# workers; long enough that a three-hour contest fetches each package a
# handful of times rather than thousands.
DEFAULT_TTL_SECONDS = 900


def key_for(uri: str) -> str:
    """A filename-safe, collision-resistant name for a URI.

    Hashed rather than sanitised: two packages whose URIs differ only in
    characters a sanitiser would strip must not collide, because the result
    would be judging one problem's submissions against another's tests.
    """
    return hashlib.sha256(uri.encode()).hexdigest()[:32]


@dataclass
class PackageCache:
    """Package files kept between jobs, keyed by URI.

    Deliberately dumb: no locking beyond an atomic rename, no eviction
    policy beyond the TTL. Two workers fetching the same package at once
    both download it and both rename into place; the loser's write is
    harmless because the bytes are identical.
    """

    root: Path
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    # Counted so a worker can report whether the cache is doing anything.
    hits: int = field(default=0)
    misses: int = field(default=0)

    def path_for(self, uri: str) -> Path:
        return self.root / f"{key_for(uri)}.pkg"

    def fresh(self, uri: str, *, now: float | None = None) -> Path | None:
        """The cached file, if there is one and it has not expired."""
        path = self.path_for(uri)
        try:
            age = (now or time.time()) - path.stat().st_mtime
        except OSError:
            return None
        if age > self.ttl_seconds:
            return None
        return path

    def store(self, uri: str, source: Path) -> Path:
        """Copy a freshly-fetched package in. Returns the cached path.

        Write-then-rename so a reader never sees a half-written package —
        judging against a truncated zip would fail in a way nobody would
        think to blame on the cache.
        """
        destination = self.path_for(uri)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            staging = destination.with_suffix(f".{os.getpid()}.part")
            shutil.copy2(source, staging)
            staging.replace(destination)
        except OSError:
            # A cache that cannot write must not stop a job being judged;
            # it just stops being a cache.
            return source
        return destination

    def prune(self, *, now: float | None = None) -> int:
        """Drop expired entries. Safe to call at any time."""
        now = now or time.time()
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return 0
        for entry in entries:
            try:
                if now - entry.stat().st_mtime > self.ttl_seconds:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
