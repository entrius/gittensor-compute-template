# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Shared by ``scripts/validate`` and ``scripts/qualify``: load a manifest, validate it against the schema, and
run the consistency checks the schema cannot express. Requires ``pyyaml`` and ``jsonschema`` (``pip install
jsonschema pyyaml`` or ``uv pip install -e '.[dev]'``)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

try:
    import yaml
    from jsonschema import Draft202012Validator
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        f'missing dependency: {e.name}. Run: uv pip install -e ".[dev]"  (or pip install jsonschema pyyaml)'
    )

REPO = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO / 'manifest.schema.json'
_DIGEST_RE = re.compile(r'@sha256:([a-f0-9]{64})$')
_ZERO_DIGEST = '0' * 64
_HTTP_ROUTE_TYPES = {'gateway-openai', 'http'}


def load_manifest(path: Path) -> dict[str, Any]:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f'{path}: top level must be a mapping')
    return data


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    return json.loads(path.read_text())


def schema_errors(manifest: dict[str, Any], schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path)):
        where = '.'.join(str(p) for p in err.absolute_path) or '<root>'
        out.append(f'{where}: {err.message}')
    return out


def consistency_errors(manifest: dict[str, Any], *, allow_placeholder_digest: bool = True) -> list[str]:
    """What the schema cannot say: digest pinned, unique routes, canaries aimed at declared routes, port agreement."""
    problems: list[str] = []
    image = manifest.get('image', '')
    m = _DIGEST_RE.search(image)
    if not m:
        problems.append('image: not pinned by digest (must end in @sha256:<64 hex>)')
    elif m.group(1) == _ZERO_DIGEST and not allow_placeholder_digest:
        problems.append('image: the all-zeros digest is a placeholder; pin the digest your CI pushed')

    fd = manifest.get('front_door', {})
    fd_type = fd.get('type')
    routes = fd.get('routes') or []
    seen: set[tuple[str, str]] = set()
    for r in routes:
        key = (r.get('method', ''), r.get('path', ''))
        if key in seen:
            problems.append(f'front_door.routes: duplicate route {key[0]} {key[1]}')
        seen.add(key)
    route_paths = {r.get('path') for r in routes}

    if fd_type == 'gateway-openai':
        for need in ('/v1/chat/completions', '/v1/models'):
            if need not in route_paths:
                problems.append(f'front_door.routes: gateway-openai images must declare {need}')

    health = manifest.get('health', {})
    if fd_type in _HTTP_ROUTE_TYPES and 'http' in health and health['http'].get('port') != fd.get('port'):
        problems.append(
            f'health.http.port ({health["http"].get("port")}) differs from front_door.port ({fd.get("port")}); '
            'the controller probes the port it routes to'
        )

    for i, canary in enumerate(manifest.get('entry_canary') or []):
        if canary.get('type') != 'http':
            continue
        http = canary.get('http', {})
        if fd_type in _HTTP_ROUTE_TYPES:
            if http.get('path') not in route_paths:
                problems.append(f'entry_canary[{i}].http.path {http.get("path")!r} is not one of front_door.routes')
            if http.get('port') != fd.get('port'):
                problems.append(
                    f'entry_canary[{i}].http.port {http.get("port")} differs from front_door.port {fd.get("port")}'
                )
        else:
            problems.append(f'entry_canary[{i}]: an http canary needs an HTTP front door (type {fd_type!r})')
        rule = canary.get('pass', {})
        if 'regex' in rule:
            try:
                re.compile(rule['regex'])
            except re.error as e:
                problems.append(f'entry_canary[{i}].pass.regex does not compile: {e}')

    for i, art in enumerate(manifest.get('artifacts') or []):
        mounts = [v.get('mount', '') for v in manifest.get('run', {}).get('volumes') or []]
        if mounts and not any(art.get('path', '').startswith(mnt.rstrip('/') + '/') for mnt in mounts):
            problems.append(
                f'artifacts[{i}].path {art.get("path")!r} is not under any run.volumes mount ({", ".join(mounts)}); '
                'the controller stages artifacts into the declared volume'
            )
    return problems


def check(path: Path, *, allow_placeholder_digest: bool = True) -> tuple[dict[str, Any] | None, list[str]]:
    """(manifest, problems). A manifest that fails to parse or the schema returns problems and no further checks."""
    try:
        manifest = load_manifest(path)
    except (OSError, ValueError, yaml.YAMLError) as e:
        return None, [f'cannot load {path}: {e}']
    problems = schema_errors(manifest, load_schema())
    if problems:
        return manifest, problems
    return manifest, consistency_errors(manifest, allow_placeholder_digest=allow_placeholder_digest)
