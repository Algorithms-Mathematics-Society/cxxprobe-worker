"""Remove editable starter files from a private staged judge package.

Only explicitly declared reference files remain available to the compiler.
The original package and the package cache are never changed.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import yaml


class CandidatePackageError(ValueError):
    pass


def _contained(root: Path, relative: str) -> Path:
    if (
        not relative
        or relative.startswith("/")
        or "\\" in relative
        or ":" in relative
        or any(part in ("", ".", "..") for part in relative.split("/"))
    ):
        raise CandidatePackageError("unsafe candidate attachment path")
    target = (root / PurePosixPath(relative)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise CandidatePackageError("candidate attachment escapes staged package")
    return target


def _yaml(path: Path) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CandidatePackageError(f"cannot read staged package declaration: {path.name}") from exc
    if not isinstance(document, dict):
        raise CandidatePackageError("staged package declaration must be a mapping")
    return document


def sanitize_attachments(problem_dir: Path) -> None:
    """Keep only authored read-only refs in a copied problem attachment tree."""
    spec = _yaml(problem_dir / "problem.yaml")
    attachments = spec.get("attachments") or {}
    if not isinstance(attachments, dict):
        raise CandidatePackageError("attachments must be a mapping")
    directory = attachments.get("dir", "attachments")
    if not isinstance(directory, str):
        raise CandidatePackageError("attachments.dir must be a path")
    public = _contained(problem_dir, directory)
    if not public.exists():
        public.mkdir(parents=True)
    if not public.is_dir():
        raise CandidatePackageError("attachments.dir is not a directory")
    refs = attachments.get("reference_files") or []
    if not isinstance(refs, list) or len(refs) > 16:
        raise CandidatePackageError("reference_files must be a bounded list")
    kept: set[Path] = set()
    for ref in refs:
        if not isinstance(ref, str):
            raise CandidatePackageError("reference_files must contain paths")
        path = _contained(public, ref)
        if not path.is_file() or path.is_symlink():
            raise CandidatePackageError("read-only reference file is missing or symbolic")
        kept.add(path)
    entries = list(public.rglob("*"))
    if len(entries) > 1024:
        raise CandidatePackageError("attachment tree is too large")
    for entry in entries:
        if entry.is_symlink():
            raise CandidatePackageError("attachment tree contains a symbolic link")
        if entry.is_file() and entry.resolve() not in kept:
            entry.unlink()
    (public / ".cxxprobe-sanitized").write_text("readonly references only\n", encoding="utf-8")


def sanitize_project(contest_dir: Path, project_slug: str, bundled_labels: set[str]) -> None:
    """Sanitize only project parts whose candidate supplied a bundle."""
    project_dir = _contained(contest_dir, project_slug)
    spec = _yaml(project_dir / "project.yaml")
    parts = spec.get("parts")
    if not isinstance(parts, list) or len(parts) > 64:
        raise CandidatePackageError("project parts declaration is invalid")
    found: set[str] = set()
    for part in parts:
        if not isinstance(part, dict):
            raise CandidatePackageError("project part declaration is invalid")
        label, directory = part.get("label"), part.get("dir")
        if label not in bundled_labels:
            continue
        if not isinstance(label, str) or not isinstance(directory, str):
            raise CandidatePackageError("project part label or directory is invalid")
        found.add(label)
        sanitize_attachments(_contained(project_dir, directory))
    if found != bundled_labels:
        raise CandidatePackageError("project bundle labels do not match project parts")
