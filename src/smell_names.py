"""Canonical smell names and backward-compatible readers for analysis artifacts.

Historical files stay unchanged. Readers accept the old identifier, while new
detector results and analysis records use ``no_fallback_cache`` exclusively.
The YAML input name ``restore-keys`` and fallback subtypes are not renamed.
"""
from __future__ import annotations

import csv
import json
from collections.abc import Mapping
from typing import Any

NO_FALLBACK_CACHE = 'no_fallback_cache'
SMELL_NAME_ALIASES = {'no_restore_keys': NO_FALLBACK_CACHE}
_LITERAL_FIELDS = {
    'job_name', 'job_id', 'repository_name', 'workflow_path', 'content_file',
    'yml_file', 'message', 'suggestion', 'code_snippet', 'raw_annotation',
    'action', 'name', 'display_name', 'run', 'yaml', 'yaml_content', 'path', 'key',
}


def canonical_smell_name(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith('co_smell='):
        prefix, name = value.split('=', 1)
        return prefix + '=' + SMELL_NAME_ALIASES.get(name, name)
    return SMELL_NAME_ALIASES.get(value, value)


def normalize_smell_data(value: Any) -> Any:
    """Normalize schema keys and explicit type identifiers, without editing prose.

    Mixed old/new fields must agree; conflicting values raise instead of being
    summed or silently overwritten. Existing file paths and provenance hashes
    remain verbatim. Exact strings in type lists are normalized as well.
    """
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            name = canonical_smell_name(key)
            if key in _LITERAL_FIELDS:
                normalized = item
            elif key == 'jobs' and isinstance(item, Mapping):
                normalized = {job: normalize_smell_data(body) for job, body in item.items()}
            else:
                normalized = normalize_smell_data(item)
            if name in result and result[name] != normalized:
                raise ValueError(f'Conflicting legacy/canonical smell fields: {key!r} -> {name!r}')
            result[name] = normalized
        return result
    if isinstance(value, list):
        return [normalize_smell_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_smell_data(item) for item in value)
    return canonical_smell_name(value)


def load_smell_json(handle, **kwargs):
    return normalize_smell_data(json.load(handle, **kwargs))


def loads_smell_json(text, **kwargs):
    return normalize_smell_data(json.loads(text, **kwargs))


class SmellDictReader(csv.DictReader):
    """A DictReader that accepts legacy smell headers and type-column values."""

    @property
    def fieldnames(self):
        raw = csv.DictReader.fieldnames.fget(self)
        return [canonical_smell_name(name) for name in raw] if raw is not None else None

    @fieldnames.setter
    def fieldnames(self, value):
        self._fieldnames = value

    def __next__(self):
        # Keep raw headers until collision checking; exposing canonical headers
        # also supports callers forwarding reader.fieldnames to a new CSV writer.
        names = csv.DictReader.fieldnames.fget(self)
        row = next(self.reader)
        while row == []:
            row = next(self.reader)
        self.line_num = self.reader.line_num
        result = dict(zip(names, row))
        if len(names) < len(row):
            result[self.restkey] = row[len(names):]
        elif len(names) > len(row):
            for name in names[len(row):]:
                result[name] = self.restval
        return normalize_smell_data(result)


def read_smell_frame(*args, **kwargs):
    """Read a pandas CSV using canonical smell headers and exact type values."""
    import pandas as pd
    frame = pd.read_csv(*args, **kwargs)
    result = pd.DataFrame(index=frame.index)
    for column in frame.columns:
        name = canonical_smell_name(column)
        series = frame[column].replace(SMELL_NAME_ALIASES)
        if name in result.columns and not result[name].equals(series):
            raise ValueError(f'Conflicting legacy/canonical smell columns: {column!r}')
        result[name] = series
    return result
