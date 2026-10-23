"""Reject silently skipped tests and verify a newly generated fixed-flow report."""

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--rescored", type=Path)
    args = parser.parse_args()
    cases = list(ET.parse(args.junit).iter("testcase"))
    if not cases or any(
        case.find(tag) is not None for case in cases for tag in ("skipped", "failure", "error")
    ):
        raise SystemExit("Expected nonempty, fully passed tests with zero skips.")
    if bool(args.report) != bool(args.rescored):
        parser.error("report and rescored must be supplied together")
    if args.report:
        original = json.loads(args.report.read_text(encoding="utf-8"))
        rescored = json.loads(args.rescored.read_text(encoding="utf-8"))
        rows = original["rows"]
        if (
            not original["complete"]
            or len(rows) != 16
            or any(row["baseline"] != "fixed" or not row["grade"]["passed"] for row in rows)
            or any(row["usage"]["calls"] for row in rows)
            or original["summary"] != rescored["summary"]
            or rescored["revalidation_required"]
        ):
            raise SystemExit("Fixed-flow acceptance or offline rescore differs.")
    print(f"Verified {len(cases)} tests, zero skips; requested report checks passed.")


if __name__ == "__main__":
    main()
