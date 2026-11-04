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

**Why the expiry stayed short for project packages.** A project package —
shared headers, an integration suite, a reference and tests per part — is
two orders of magnitude bigger than the 4 KB case above, so a fetch costs
bytes as well as a round trip. The expiry does not change *how many* fetches
there are, though: one per package per worker per window, whatever the
size. At 900 s a three-hour round is at most twelve fetches of a package per
worker, against one per submission without the cache, and every part of a
project is judged in one job against one staged copy. The URIs ams-api hands
out are already version-scoped (``problems/<problem>/<version>/…``), so a
re-uploaded problem is a new key and needs no invalidation; the window only
matters for the few fixed keys, such as the fleet probe, that are rewritten
in place.

**Why a size bound too.** Age alone was enough when every package was 4 KB.
A contest holding several projects can hold far more than a judge's disk
wants to give a cache, so the cache is also capped by total size, evicting
the oldest entries first. Eviction never fails a job: a package that vanished
between lookup and copy is simply fetched again.

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

# Total bytes the cache may hold before the oldest entries go. Room for many
# project packages, small next to a judge's disk.
DEFAULT_MAX_BYTES = 1024**3


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

    Deliberately dumb: no locking beyond an atomic rename, and eviction by
    age and then by total size, oldest first. Two workers fetching the same
    package at once both download it and both rename into place; the loser's
    write is harmless because the bytes are identical.
    """

    root: Path
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    max_bytes: int = DEFAULT_MAX_BYTES
    """Zero means no size bound, only the TTL."""
    # Counted so a worker can report whether the cache is doing anything.
    hits: int = field(default=0)
    misses: int = field(default=0)
    evictions: int = field(default=0)

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
        self.evict(keep=destination)
        return destination if destination.exists() else source

    def evict(self, *, keep: Path | None = None, now: float | None = None) -> int:
        """Drop expired entries, then the oldest until under ``max_bytes``.

        ``keep`` is the entry just stored, which goes last: evicting what a
        job is about to reuse would make the cache pointless. If it alone is
        bigger than the bound it is not kept at all, rather than pushing out
        everything else to make room. Returns how many entries were removed.
        """
        removed = self.prune(now=now)
        if self.max_bytes > 0:
            entries: list[tuple[float, int, Path]] = []
            try:
                for entry in self.root.iterdir():
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    entries.append((st.st_mtime, st.st_size, entry))
            except OSError:
                entries = []
            total = sum(size for _, size, _ in entries)
            kept = next((e for e in entries if e[2] == keep), None)
            if kept is not None and kept[1] > self.max_bytes:
                removed += self._unlink(kept[2])
                total -= kept[1]
            for _, size, entry in sorted(entries):
                if total <= self.max_bytes:
                    break
                if entry == keep:
                    continue
                removed += self._unlink(entry)
                total -= size
        self.evictions += removed
        return removed

    @staticmethod
    def _unlink(path: Path) -> int:
        try:
            path.unlink()
        except OSError:
            return 0
        return 1

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
