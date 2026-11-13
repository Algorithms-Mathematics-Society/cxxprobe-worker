"""Stage API source bundles without trusting archive member names."""

from __future__ import annotations

import json
import re
import stat
import zipfile
from pathlib import Path


class BundleError(ValueError):
    pass


_SUFFIXES = (".cpp", ".cc", ".cxx", ".h", ".hh", ".hpp", ".hxx")
_UNITS = (".cpp", ".cc", ".cxx")
_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _path(path: object) -> str:
    if (
        not isinstance(path, str)
        or not path
        or len(path) > 160
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or "\\" in path
        or any(part in ("", ".", "..") for part in path.split("/"))
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
        or not path.lower().endswith(_SUFFIXES)
        or any(
            not _SEGMENT.fullmatch(part)
            or part.startswith(".")
            or part.endswith(".")
            or part.split(".", 1)[0].upper() in _RESERVED
            for part in path.split("/")
        )
    ):
        raise BundleError("invalid source bundle path")
    return path


def unpack(archive_path: Path, target: Path) -> Path:
    """Return staged entrypoint and write the engine's explicit source list."""
    try:
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) < 2 or len(infos) > 17 or infos[0].filename != "manifest.json":
                raise BundleError("invalid source bundle members")
            if len({info.filename for info in infos}) != len(infos):
                raise BundleError("duplicate source bundle members")
            if infos[0].file_size > 16 * 1024:
                raise BundleError("source bundle manifest is too large")
            manifest = json.loads(archive.read("manifest.json"))
            if (
                not isinstance(manifest, dict)
                or set(manifest) != {"format", "entrypoint", "compilation_units"}
                or manifest.get("format") != "ams-source-bundle-v1"
            ):
                raise BundleError("unsupported source bundle format")
            members = {}
            folded = set()
            total = 0
            for info in infos[1:]:
                if not info.filename.startswith("files/"):
                    raise BundleError("unexpected source bundle member")
                path = _path(info.filename[6:])
                if path.casefold() in folded or stat.S_ISLNK(info.external_attr >> 16):
                    raise BundleError("duplicate or symbolic source bundle member")
                folded.add(path.casefold())
                if info.file_size > 256_000:
                    raise BundleError("source bundle member is too large")
                total += info.file_size
                members[path] = info
            if total > 1024 * 1024:
                raise BundleError("source bundle exceeds size limit")
            if any(
                a.casefold().startswith(b.casefold() + "/")
                for a in members
                for b in members
                if a != b
            ):
                raise BundleError("source file path collides with directory")
            entry = manifest.get("entrypoint")
            units = manifest.get("compilation_units")
            if (
                not isinstance(entry, str)
                or entry not in members
                or not entry.lower().endswith(_UNITS)
                or not isinstance(units, list)
                or not units
                or any(not isinstance(unit, str) for unit in units)
                or len(set(units)) != len(units)
                or entry not in units
                or any(unit not in members or not unit.lower().endswith(_UNITS) for unit in units)
            ):
                raise BundleError("invalid bundle entrypoint or compilation units")
            target.mkdir(parents=True, exist_ok=False)
            for path, info in members.items():
                destination = target / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                body = archive.read(info)
                if len(body) != info.file_size or b"\x00" in body:
                    raise BundleError("invalid source bundle content")
                body.decode("utf-8")
                destination.write_bytes(body)
            entry_path = target / entry
            sidecar = Path(str(entry_path) + ".cxxprobe.json")
            sidecar.write_text(
                json.dumps(
                    {
                        "format": "cxxprobe-candidate-v1",
                        "root": str(target.resolve()),
                        "units": sorted(units),
                        "files": sorted(members),
                    },
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            return entry_path
    except (
        OSError,
        zipfile.BadZipFile,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RuntimeError,
        NotImplementedError,
        TypeError,
        KeyError,
        ValueError,
    ) as exc:
        if isinstance(exc, BundleError):
            raise
        raise BundleError(f"cannot stage source bundle: {exc}") from exc
