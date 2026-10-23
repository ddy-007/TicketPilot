"""Package project sources and selected M5 evidence; exclude local configuration and runtimes."""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "dist",
    "test-results",
    "playwright-report",
}
TOP_FILES = (
    "README.md",
    "DEVLOG.md",
    "TicketPilot-详细设计文档.md",
    "TicketPilot-项目开发文档.md",
    "compose.yaml",
    ".dockerignore",
    ".gitignore",
    ".env.example",
)
EVIDENCE = (
    "m5-fixed-project-fixes-2026-10-23.json",
    "m5-fixed-project-fixes-recomputed-2026-10-23.json",
    "m5-measured-backend-source-project-fixes-2026-10-23.zip",
    "project-fixes-backend-2026-10-23.xml",
    "project-fixes-backend-2026-10-23.log",
    "project-fixes-browser-2026-10-23.log",
    "project-fixes-browser-final-2026-10-23.log",
    "project-fixes-pagination-final-2026-10-23.log",
    "project-fixes-docker-build-2026-10-23.log",
    "project-fixes-docker-build-final-2026-10-23.log",
    "project-fixes-targeted-2026-10-23.xml",
    "project-fixes-2026-10-23/original-probes.xml",
    "project-fixes-2026-10-23/original-probes.log",
    "project-fixes-2026-10-23/test_original_probes.py",
    "project-fixes-2026-10-23/rejected-shared-operation.json",
    "project-fixes-2026-10-23/ticket-list-size.json",
    "project-fixes-2026-10-23/read-attempt-timing.json",
    "project-fixes-2026-10-23/smoke-partial-usage.json",
    "project-fixes-2026-10-23/evidence-summary.json",
    "project-fixes-2026-10-23/collect_evidence.py",
    "project-audit-2026-10-23/TicketPilot-全项目审核报告.md",
    "project-audit-2026-10-23/test_project_review.py",
    "project-audit-2026-10-23/probes.log",
    "project-audit-2026-10-23/probes.xml",
    "project-audit-2026-10-23/smoke-probe.log",
    "project-audit-2026-10-23/smoke-probe.xml",
    "project-audit-2026-10-23/rejected-shared-operation.json",
    "project-audit-2026-10-23/ticket-list-size.json",
    "project-audit-2026-10-23/read-attempt-timing.json",
    "project-audit-2026-10-23/smoke-partial-usage.json",
    "project-audit-2026-10-23/evidence-summary.json",
    "project-audit-backend-2026-10-23.xml",
    "project-audit-backend-2026-10-23.log",
    "project-audit-browser-2026-10-23.log",
    "m5-fixed-v3-2026-10-23.json",
    "m5-fixed-v3-recomputed-2026-10-23.json",
    "m5-agent-dev-legacy-v3-2026-10-23.json",
    "m5-repaired-backend-tests-2026-10-23.xml",
    "m5-repair-probes-2026-10-23.xml",
    "m5-measured-backend-source-v3-2026-10-23.zip",
    "m5-audit-2026-10-23/M5-全面审查报告.md",
    "m5-audit-2026-10-23/test_review_probes.py",
    "m5-audit-2026-10-23/probes.xml",
    "m5-audit-2026-10-23/probes.log",
    "m5-fixed-2026-10-23.json",
    "m5-fixed-first.json",
    "m5-fixed-reviewed-2026-10-20.json",
    "m5-agent-dev-2026-10-20.json",
    "m5-agent-dev-2026-10-20-rescored-2026-10-23.json",
    "m5-provider-probe-2026-10-23.json",
    "m5-clean-startup-2026-10-23.json",
    "m5-backend-tests-2026-10-23.xml",
    "m5-review-tests-2026-10-23.xml",
    "m5-browser-run-2026-10-23.log",
    "m5-docker-build-2026-10-23.log",
    "m5-measured-backend-source-2026-10-23.zip",
    "m5-demo-delivery-2026-10-23.mp4",
    "m5-demo-final-2026-10-23-evidence.json",
    "m5-final-frame-approved.png",
    "m5-final-results-card.png",
    "m5-architecture.png",
    "m0-llm-2026-10-19.json",
    "m2-real-2026-10-19.json",
    "m2-real-isolated-2026-10-19.json",
    "m2-real-refined-2026-10-19.json",
    "m3-real-approval-2026-10-19.json",
    "m4-workbench-approved.png",
)


def paths():
    selected = [ROOT / name for name in TOP_FILES]
    for folder in ("backend", "frontend", "docs", "scripts"):
        selected.extend(
            path
            for path in (ROOT / folder).rglob("*")
            if path.is_file()
            and not EXCLUDED.intersection(path.relative_to(ROOT).parts)
            and not path.name.startswith(".env")
            and path.suffix not in {".pyc", ".pyo"}
        )
    selected.extend(ROOT / "artifacts" / name for name in EVIDENCE)
    return sorted(set(selected))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "artifacts/TicketPilot-project-fixes-2026-10-23.zip"
    )
    args = parser.parse_args()
    selected = paths()
    missing = [str(path.relative_to(ROOT)) for path in selected if not path.is_file()]
    if missing:
        parser.error("Required files missing: " + ", ".join(missing))
    manifest = {
        "stage": "PROJECT_AUDIT_FIXES_REAL_MODEL_VALIDATION_PENDING",
        "excludes": [".env", "credentials", "installed dependencies", "database contents"],
        "files": {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in selected
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(args.output, "x", compression=ZIP_DEFLATED) as archive:
            for path in selected:
                archive.write(path, path.relative_to(ROOT).as_posix())
            archive.writestr(
                "DELIVERY-MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2)
            )
    except FileExistsError:
        parser.error("output exists; choose a new path")
    print(json.dumps({"files": len(selected), "zip_bytes": args.output.stat().st_size}))


if __name__ == "__main__":
    main()
