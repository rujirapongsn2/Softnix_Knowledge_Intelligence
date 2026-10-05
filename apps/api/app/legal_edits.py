"""Which parts of a document's legal metadata a person wrote, so that extraction never replaces them.

A path is a top-level key, or ``instrument.<key>`` for a field of the instrument. The paths live in
``provenance.manual_paths`` of the metadata itself and only grow when a person saves a change.
"""
from __future__ import annotations

from typing import Any

PATHS_KEY = "manual_paths"
_NOT_A_VALUE = object()


def _paths(metadata: dict | None) -> list[str]:
    provenance = (metadata or {}).get("provenance")
    paths = provenance.get(PATHS_KEY) if isinstance(provenance, dict) else None
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def _get(metadata: dict, path: str) -> Any:
    if path.startswith("instrument."):
        instrument = metadata.get("instrument")
        return instrument.get(path.removeprefix("instrument."), _NOT_A_VALUE) if isinstance(instrument, dict) else _NOT_A_VALUE
    return metadata.get(path, _NOT_A_VALUE)


def _set(metadata: dict, path: str, value: Any) -> None:
    if path.startswith("instrument."):
        instrument = metadata.get("instrument") if isinstance(metadata.get("instrument"), dict) else {}
        metadata["instrument"] = {**instrument, path.removeprefix("instrument."): value}
    else:
        metadata[path] = value


def _changed_paths(old: dict, new: dict) -> list[str]:
    paths = []
    for key in new.keys() - {"provenance"}:
        if key == "instrument" and isinstance(new[key], dict):
            old_instrument = old.get(key) if isinstance(old.get(key), dict) else {}
            paths += [f"instrument.{k}" for k, v in new[key].items() if old_instrument.get(k, _NOT_A_VALUE) != v]
        elif old.get(key, _NOT_A_VALUE) != new[key]:
            paths.append(key)
    return paths


def with_manual_edits(old: dict | None, new: dict) -> dict:
    """``new`` as a person saved it, plus a record of what they changed compared with ``old``."""
    paths = list(dict.fromkeys(_paths(old) + _changed_paths(old or {}, new)))
    provenance = new.get("provenance") if isinstance(new.get("provenance"), dict) else {}
    return {**new, "provenance": {**provenance, PATHS_KEY: paths}} if paths else new


def keep_manual_edits(extracted: dict, previous: dict | None) -> dict:
    """``extracted`` with every path a person wrote taken from ``previous``."""
    result = {**extracted}
    paths = _paths(previous)
    for path in paths:
        value = _get(previous, path)
        if value is not _NOT_A_VALUE:
            _set(result, path, value)
    if paths:
        result["provenance"] = {**(result.get("provenance") or {}), PATHS_KEY: paths}
    return result
