"""Keeping problem packages between jobs.

The number this exists for: the same workload ran at 0.73 jobs/s/vCPU when
the worker shared a region with the bucket and 0.40 when it did not. The
fleet has to be spread across regions — the account's quotas leave no
choice — so that 46% was being paid on every contest. Two S3 round trips per
job, one of which was for a file the worker had already downloaded
thousands of times.
"""

from __future__ import annotations

import time

from cxxprobe_worker.packages import PackageCache, key_for


def make_cache(tmp_path, ttl=900.0):
    return PackageCache(root=tmp_path / "packages", ttl_seconds=ttl)


def a_package(tmp_path, name="pkg.zip", body=b"PK\x03\x04payload"):
    path = tmp_path / name
    path.write_bytes(body)
    return path


def test_an_empty_cache_is_a_miss(tmp_path):
    cache = make_cache(tmp_path)
    assert cache.fresh("s3://bucket/a.cxxpkg") is None


def test_a_stored_package_comes_back(tmp_path):
    cache = make_cache(tmp_path)
    source = a_package(tmp_path)
    cache.store("s3://bucket/a.cxxpkg", source)

    hit = cache.fresh("s3://bucket/a.cxxpkg")
    assert hit is not None
    assert hit.read_bytes() == source.read_bytes()


def test_two_packages_never_collide(tmp_path):
    """A collision would judge one problem's submissions against another's
    tests, which is the worst outcome this file could produce — so the key
    is a hash of the whole URI, not a sanitised version of it."""
    cache = make_cache(tmp_path)
    cache.store("s3://bucket/round-1.cxxpkg", a_package(tmp_path, "one.zip", b"ONE"))
    cache.store("s3://bucket/round_1.cxxpkg", a_package(tmp_path, "two.zip", b"TWO"))

    assert cache.fresh("s3://bucket/round-1.cxxpkg").read_bytes() == b"ONE"
    assert cache.fresh("s3://bucket/round_1.cxxpkg").read_bytes() == b"TWO"
    assert key_for("s3://bucket/round-1.cxxpkg") != key_for("s3://bucket/round_1.cxxpkg")


def test_an_expired_package_is_refetched(tmp_path):
    """A TTL rather than an ETag check: validating costs the round trip the
    cache exists to avoid. The window is what picks up an edited problem."""
    cache = make_cache(tmp_path, ttl=60)
    cache.store("s3://bucket/a.cxxpkg", a_package(tmp_path))

    assert cache.fresh("s3://bucket/a.cxxpkg") is not None
    assert cache.fresh("s3://bucket/a.cxxpkg", now=time.time() + 61) is None


def test_pruning_drops_only_what_expired(tmp_path):
    cache = make_cache(tmp_path, ttl=60)
    cache.store("s3://bucket/old.cxxpkg", a_package(tmp_path, "old.zip"))
    cache.store("s3://bucket/new.cxxpkg", a_package(tmp_path, "new.zip"))

    assert cache.prune(now=time.time()) == 0
    assert cache.prune(now=time.time() + 61) == 2
    assert cache.fresh("s3://bucket/new.cxxpkg") is None


def test_a_cache_that_cannot_write_still_judges(tmp_path):
    """A broken cache must degrade to no cache, never to a failed job."""
    source = a_package(tmp_path)
    blocked = tmp_path / "wall"
    blocked.write_text("not a directory")
    cache = PackageCache(root=blocked / "packages")

    # store() hands back something usable rather than raising.
    assert cache.store("s3://bucket/a.cxxpkg", source).read_bytes() == source.read_bytes()
    assert cache.fresh("s3://bucket/a.cxxpkg") is None


def test_a_half_written_package_is_never_visible(tmp_path):
    """Write-then-rename. Judging against a truncated zip fails in a way
    nobody would think to blame on the cache."""
    cache = make_cache(tmp_path)
    cache.store("s3://bucket/a.cxxpkg", a_package(tmp_path))
    # Only the finished file, no .part left behind.
    assert [p.suffix for p in cache.root.iterdir()] == [".pkg"]


# ── the behaviour that recovers the throughput ────────────────────────────


class CountingFetcher:
    """Stands in for S3, and counts what it was asked for."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.fetched: list[str] = []

    def fetch(self, uri: str, destination):
        self.fetched.append(uri)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payloads[uri])
        return destination


def test_a_package_is_fetched_once_and_a_submission_every_time(tmp_path):
    """The whole point. Seven packages and forty thousand submissions must
    not be forty thousand package downloads — which, cross-region, cost more
    than the judging did."""
    from cxxprobe_worker.executor import _stage_inputs
    from cxxprobe_worker.jobs import Job
    from cxxprobe_worker.workspace import WorkspaceManager

    pkg_uri = "s3://bucket/fleet/probe.cxxpkg"
    fetcher = CountingFetcher(
        {
            pkg_uri: b"PK\x03\x04package",
            "s3://bucket/subs/1.cpp": b"int main(){}",
            "s3://bucket/subs/2.cpp": b"int main(){return 1;}",
        }
    )
    cache = make_cache(tmp_path)
    manager = WorkspaceManager(tmp_path / "ws")

    for n in (1, 2):
        job = Job(
            job_id=f"j{n}",
            package_path=pkg_uri,
            submission_path=f"s3://bucket/subs/{n}.cpp",
        )
        with manager.session(job.job_id) as workspace:
            flag, path = _stage_inputs(job, workspace, fetcher, cache)
            assert flag == "--package"
            assert path.read_bytes() == b"PK\x03\x04package"

    assert fetcher.fetched.count(pkg_uri) == 1, "the package was downloaded twice"
    assert fetcher.fetched.count("s3://bucket/subs/2.cpp") == 1
    assert (cache.hits, cache.misses) == (1, 1)


def test_without_a_cache_every_job_refetches(tmp_path):
    """The behaviour before this change, kept so the difference is visible
    and a regression is loud."""
    from cxxprobe_worker.executor import _stage_inputs
    from cxxprobe_worker.jobs import Job
    from cxxprobe_worker.workspace import WorkspaceManager

    pkg_uri = "s3://bucket/p.cxxpkg"
    fetcher = CountingFetcher({pkg_uri: b"PK\x03\x04", "s3://b/s.cpp": b"x"})
    manager = WorkspaceManager(tmp_path / "ws")

    for n in (1, 2):
        job = Job(job_id=f"j{n}", package_path=pkg_uri, submission_path="s3://b/s.cpp")
        with manager.session(job.job_id) as workspace:
            _stage_inputs(job, workspace, fetcher, None)

    assert fetcher.fetched.count(pkg_uri) == 2
