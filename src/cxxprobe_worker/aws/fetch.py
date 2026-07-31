"""Materialising remote job inputs into a local workspace.

A cloud job carries `s3://` URIs rather than local paths, so something has
to turn them into files before `cxxprobe judge` runs. That happens here, in
one place, so the executor keeps a single notion of "the inputs are now on
disk" regardless of where they came from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class FetchError(RuntimeError):
    """A job input could not be downloaded."""


def is_remote(uri: str | Path) -> bool:
    return str(uri).startswith("s3://")


def parse_s3_uri(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc:
        raise FetchError(f"not an s3 uri: {uri}")
    key = parsed.path.lstrip("/")
    if not key:
        raise FetchError(f"s3 uri has no key: {uri}")
    return parsed.netloc, key


class S3Fetcher:
    """Downloads `s3://` URIs to local paths."""

    def __init__(self, region: str | None = None, client: Any = None) -> None:
        if client is not None:
            self._s3 = client
        else:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover
                raise FetchError(
                    "fetching s3:// inputs needs boto3 — install with `uv sync --extra aws`"
                ) from exc
            self._s3 = boto3.client("s3", region_name=region)

    def fetch(self, uri: str, destination: Path) -> Path:
        bucket, key = parse_s3_uri(uri)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._s3.download_file(bucket, key, str(destination))
        except Exception as exc:
            raise FetchError(f"cannot download {uri}: {exc}") from exc
        return destination
