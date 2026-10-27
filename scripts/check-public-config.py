"""Reject local runtime configs and real IPs in public configuration examples."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import PurePosixPath
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
    for raw_path in filter(None, paths):
        path = raw_path.decode("utf-8")
        if PurePosixPath(path).name.lower() == "config.yaml":
            errors.append(f"{label}: {path}: local config.yaml must never be tracked")
        if path.startswith("backend/db/") and path != "backend/db/.gitkeep":
            errors.append(f"{label}: {path}: local database data must never be tracked")
        if PurePosixPath(path).name.lower() == "config.example.yaml":
            content = git("show", f"{revision or ''}:{path}")
            for number, line in enumerate(content.splitlines(), 1):
                if any(real_address(m.group()) for m in IPV4.finditer(line)):
                    errors.append(f"{label}: {path}:{number}: replace real IP with 127.0.0.1")

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
