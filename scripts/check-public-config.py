"""Reject local runtime configs and real IPs in public configuration examples."""

from __future__ import annotations

import argparse
import ast
import ipaddress
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


IPV4 = re.compile(rb"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
URL_IPV4 = re.compile(
    rb"(?:https?|wss?)://(?:[^/\s@]+@)?(?P<host>(?:\d{1,3}\.){3}\d{1,3})"
    rb"(?=[:/\s\x22\x27`<>)]|$)",
    re.IGNORECASE,
)
ZERO_OID = "0" * 40
PUBLIC_SYSTEM_NAME = 'mcp系统名称'
SOURCE_URL = re.compile(rb'(?:https?|wss?)://([a-zA-Z0-9][a-zA-Z0-9.:-]*)(?:/[^\s\x22\x27<>]*)?')
NAMESPACE_HOSTS = {b'schemas.openxmlformats.org', b'purl.org', b'www.w3.org'}


def configured_system_names() -> set[bytes]:
    path = Path(git('rev-parse', '--show-toplevel').decode().strip()) / 'config.yaml'
    if not path.exists():
        return set()
    names = set()
    section = ''
    for line in path.read_text(encoding='utf-8').splitlines():
        if re.match(r'^[A-Za-z_][\w-]*\s*:', line):
            section = line.split(':', 1)[0]
        if section == 'mcp_connections':
            match = re.match(r'\s+name:\s*(.*?)\s*$', line)
            if match:
                name = match[1].strip('\"\x27')
                if name and name != PUBLIC_SYSTEM_NAME:
                    names.add(name.encode())
    return names


def check_source(path: str, content: bytes, label: str) -> list[str]:
    errors = []
    for number, line in enumerate(content.splitlines(), 1):
        if any(match.group(1) not in NAMESPACE_HOSTS for match in SOURCE_URL.finditer(line)):
            errors.append(f'{label}: {path}:{number}: service addresses belong in YAML configuration')
    if path.endswith('.py'):
        try:
            tree = ast.parse(content.decode('utf-8-sig'))
        except SyntaxError as exc:
            return [*errors, f'{label}: {path}:{exc.lineno}: invalid Python source']
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == 'strip':
                value = value.func.value
            if any('PROMPT' in name or name == 'instructions' for name in names) and isinstance(value, (ast.Constant, ast.JoinedStr)):
                if isinstance(value, ast.JoinedStr) or isinstance(value.value, str) and value.value.strip():
                    errors.append(f'{label}: {path}:{node.lineno}: prompt templates belong in YAML configuration')
    return errors


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def real_address(value: bytes) -> bool:
    try:
        address = ipaddress.IPv4Address(value.decode("ascii"))
    except ipaddress.AddressValueError:
        return False
    return not (address.is_loopback or address.is_unspecified)


def check(revision: str | None) -> list[str]:
    label = revision[:12] if revision else "staged"
    paths = (
        git("ls-tree", "-r", "--name-only", "-z", revision)
        if revision else git("ls-files", "-z")
    ).split(b"\0")
    errors = []
    system_names = configured_system_names()
    for raw_path in filter(None, paths):
        path = raw_path.decode("utf-8")
        if PurePosixPath(path).name.lower() in {"config.yaml", "prompts.yaml"}:
            errors.append(f"{label}: {path}: local runtime configuration must never be tracked")
        if path.startswith("backend/db/") and path != "backend/db/.gitkeep":
            errors.append(f"{label}: {path}: local database data must never be tracked")
        if PurePosixPath(path).name.lower() == "config.example.yaml":
            content = git("show", f"{revision or ''}:{path}")
            section = ''
            for number, line in enumerate(content.decode('utf-8').splitlines(), 1):
                if re.match(r'^[A-Za-z_][\w-]*\s*:', line):
                    section = line.split(':', 1)[0]
                if section in {'mcp_connections', 'mcp_defaults'} and re.match(r'\s+name:', line):
                    name = line.split(':', 1)[1].strip().strip('\"\x27')
                    if name != PUBLIC_SYSTEM_NAME:
                        errors.append(f'{label}: {path}:{number}: use the public placeholder for the MCP system name')
                credential = re.match(r'\s*(token|ai_api_key|api_key|protocol_session_list_key):\s*(.*?)\s*$', line)
                if credential and credential[2].split('#', 1)[0].strip() not in {'', '\"\"', "''", 'null', '~'}:
                    errors.append(f'{label}: {path}:{number}: credentials in public examples must be empty')
            for number, line in enumerate(content.splitlines(), 1):
                if any(real_address(m.group()) for m in IPV4.finditer(line)):
                    errors.append(f"{label}: {path}:{number}: replace real IP with 127.0.0.1")
        is_source = path.endswith(('.py', '.ts', '.tsx')) and not path.startswith('backend/tests/')
        if is_source or system_names:
            content = git('show', f"{revision or ''}:{path}")
            if b'\0' not in content:
                if any(name in content for name in system_names):
                    errors.append(f'{label}: {path}: replace the deployment system name with a public placeholder')
                if is_source:
                    errors.extend(check_source(path, content, label))

    args = ["git", "grep", "-I", "-n", "-E"]
    if revision is None:
        args.append("--cached")
    args.append(r"([0-9]{1,3}\.){3}[0-9]{1,3}")
    if revision:
        args.append(revision)
    args.extend(["--", "."])
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode not in (0, 1):
        raise RuntimeError("Unable to scan Git content")
    for entry in result.stdout.splitlines():
        if revision:
            entry = entry.split(b":", 1)[1]
        path, number, line = entry.split(b":", 2)
        if any(real_address(m.group("host")) for m in URL_IPV4.finditer(line)):
            location = (path + b":" + number).decode("utf-8", errors="replace")
            errors.append(f"{label}: {location}: replace real IP in URL with 127.0.0.1")
    return errors


def pushed_revisions() -> list[str]:
    revisions = set()
    for line in sys.stdin:
        _local_ref, local_oid, _remote_ref, remote_oid = line.split()
        if local_oid == ZERO_OID:
            continue
        revisions.add(local_oid)
        if remote_oid != ZERO_OID:
            args = ["rev-list", local_oid, f"^{remote_oid}"]
        else:
            args = ["rev-list", local_oid, "--not", "--remotes"]
        revisions.update(git(*args).decode("ascii").splitlines())
    return sorted(revisions)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--revision")
    mode.add_argument("--push", action="store_true")
    args = parser.parse_args()
    revisions = pushed_revisions() if args.push else [args.revision]
    errors = [error for revision in revisions for error in check(revision)]
    if errors:
        print("Public configuration check failed:", file=sys.stderr)
        for error in sorted(set(errors)):
            print(f"  {error}", file=sys.stderr)
        return 1
    print("Public configuration check passed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        print(f"Public configuration check could not complete: {exc}", file=sys.stderr)
        raise SystemExit(1)
