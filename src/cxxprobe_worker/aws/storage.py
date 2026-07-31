"""S3 implementation of ``IArtifactStorage``.

Same contract as the filesystem backend: keys are ``<job_id>/<name>``,
traversal is rejected, and a locator string comes back. The only difference
is that the locator is an ``s3://`` URI rather than a path.

``boto3`` is an optional dependency (`uv sync --extra aws`) so a filesystem
deployment stays dependency-free — hence the import inside the constructor
rather than at module scope.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from cxxprobe_worker.storage import StorageError, _validate_name

if TYPE_CHECKING:  # pragma: no cover
    from mypy_boto3_s3.client import S3Client


class S3ArtifactStorage:
    """Stores artifacts under ``s3://<bucket>/<prefix>/<job_id>/<name>``."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "artifacts",
        region: str | None = None,
        client: Any = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        if client is not None:
            self._s3: S3Client = client
        else:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover
                raise StorageError(
                    "the s3 storage backend needs boto3 — install with `uv sync --extra aws`"
                ) from exc
            self._s3 = boto3.client("s3", region_name=region)

    @property
    def bucket(self) -> str:
        return self._bucket

    def _key_for(self, job_id: str, name: str) -> str:
        _validate_name(job_id)
        _validate_name(name)
        parts = [p for p in (self._prefix, job_id, name) if p]
        return "/".join(parts)

    def _locator(self, key: str) -> str:
        return f"s3://{self._bucket}/{key}"

    def put_bytes(self, job_id: str, name: str, data: bytes) -> str:
        key = self._key_for(job_id, name)
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        except Exception as exc:
            raise StorageError(f"cannot write s3://{self._bucket}/{key}: {exc}") from exc
        return self._locator(key)

    def put_text(self, job_id: str, name: str, text: str) -> str:
        return self.put_bytes(job_id, name, text.encode())

    def put_file(self, job_id: str, name: str, source: Path) -> str:
        key = self._key_for(job_id, name)
        try:
            self._s3.upload_file(str(source), self._bucket, key)
        except Exception as exc:
            raise StorageError(
                f"cannot upload {source} to s3://{self._bucket}/{key}: {exc}"
            ) from exc
        return self._locator(key)

    def get_bytes(self, job_id: str, name: str) -> bytes:
        key = self._key_for(job_id, name)
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=key)
            body: bytes = resp["Body"].read()
        except Exception as exc:
            raise StorageError(f"cannot read s3://{self._bucket}/{key}: {exc}") from exc
        return body

    def exists(self, job_id: str, name: str) -> bool:
        key = self._key_for(job_id, name)
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            return False
        return True

    def list_artifacts(self, job_id: str) -> list[str]:
        _validate_name(job_id)
        base = "/".join(p for p in (self._prefix, job_id) if p) + "/"
        names: list[str] = []
        try:
            paginator = self._s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=base):
                for obj in page.get("Contents", []):
                    names.append(obj["Key"][len(base) :])
        except Exception as exc:
            raise StorageError(f"cannot list s3://{self._bucket}/{base}: {exc}") from exc
        return sorted(names)
