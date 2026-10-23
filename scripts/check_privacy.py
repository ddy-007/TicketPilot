"""Review staged blobs for accidental secrets and local data, without printing values.

Run from the repository root: python scripts/check_privacy.py
Only the Git index is inspected; ignored local configuration is never opened.
This check catches common patterns and is not a guarantee against every kind of secret.
"""

import json
import re
import subprocess
import sys
from pathlib import PurePosixPath

PATTERNS = {
    "github_token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "model_api_key": re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "literal_bearer": re.compile(r"Bearer [A-Za-z0-9_.-]{24,}"),
    "literal_credential": re.compile(
        r"""(?i)(?:api[_-]?key|token|secret|password)["']?\s*[:=]\s*["'](?P<value>[A-Za-z0-9_./+=:-]{20,})["']"""
    ),
    "personal_device_path": re.compile(r"(?i)\b[A-Z]:[\\/]+(?:Users|sencond)[\\/]"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "phone": re.compile(r"(?<![\w])1[3-9][0-9]{9}(?![\w])"),
}
FORBIDDEN_PARTS = {
    "artifacts",
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "test-results",
    "playwright-report",
}
FORBIDDEN_SUFFIXES = {
    ".pem",
    ".key",
    ".p8",
    ".p12",
    ".pfx",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".dump",
    ".zip",
    ".log",
    ".mp4",
    ".webm",
    ".pyc",
}


def reserved_fixture(kind, match):
    if kind == "email":
        return match.group().rsplit("@", 1)[-1].lower().endswith(".invalid")
    if kind == "literal_credential":
        return match.group("value").startswith("replace-with-")
    return False


def main():
    files = (
        subprocess.check_output(["git", "ls-files", "--cached", "-z"]).decode("utf-8").split("\0")
    )
    findings = []
    total_bytes = 0
    for name in filter(None, files):
        path = PurePosixPath(name)
        if (
            FORBIDDEN_PARTS.intersection(path.parts)
            or path.suffix.lower() in FORBIDDEN_SUFFIXES
            or path.name.startswith(".env")
            and path.name != ".env.example"
            or path.name in {"id_rsa", "id_ed25519"}
            or name.startswith("frontend/dist/")
        ):
            findings.append({"file": name, "kind": "forbidden_path"})
            continue
        blob = subprocess.check_output(["git", "show", ":" + name])
        total_bytes += len(blob)
        try:
            content = blob.decode("utf-8-sig")
        except UnicodeDecodeError:
            findings.append({"file": name, "kind": "unexpected_binary"})
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for kind, pattern in PATTERNS.items():
                if any(not reserved_fixture(kind, match) for match in pattern.finditer(line)):
                    findings.append({"file": name, "line": number, "kind": kind})
    result = {"files": len([f for f in files if f]), "bytes": total_bytes, "findings": findings}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
