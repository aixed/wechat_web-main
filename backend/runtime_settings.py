"""Load deployment values and prompt templates from YAML without embedding them in code."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / 'config.yaml'
EXAMPLE_PATH = ROOT / 'config.example.yaml'
_MISSING = object()


@lru_cache(maxsize=16)
def _read_cached(path: str, modified: int, size: int) -> dict:
    with open(path, encoding='utf-8') as stream:
        value = yaml.safe_load(stream) or {}
    if not isinstance(value, dict):
        raise ValueError(f'Configuration must be a YAML mapping: {Path(path).name}')
    return value


def read_yaml(path: Path) -> dict:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    return _read_cached(str(path), stat.st_mtime_ns, stat.st_size)


def _lookup(document: dict, parts: tuple[str, ...]):
    value: Any = document
    for part in parts:
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def setting(*parts: str, default: Any = None):
    for path in (CONFIG_PATH, EXAMPLE_PATH):
        value = _lookup(read_yaml(path), parts)
        if value is not _MISSING:
            return value
    return default


def example(*parts: str, default: Any = None):
    value = _lookup(read_yaml(EXAMPLE_PATH), parts)
    return default if value is _MISSING else value


def prompt(name: str, **values: Any) -> str:
    for key in ('prompt_config_file', 'prompt_example_file'):
        filename = setting(key)
        if not filename:
            continue
        path = Path(str(filename))
        if not path.is_absolute():
            path = ROOT / path
        document = read_yaml(path)
        if name not in document:
            continue
        template = document[name]
        if isinstance(template, str) and template.strip():
            return re.sub(r'\{\{(\w+)\}\}', lambda match: str(values.get(match[1], match[0])), template)
        raise ValueError(f'Configured prompt must not be empty: {name}')
    raise ValueError(f'Missing configured prompt: {name}')
