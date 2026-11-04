"""Fail when a pytest run collected or passed far fewer tests than expected.

A module that cannot import an optional dependency can vanish from a run (or skip
wholesale) and the run still looks green; see "Run tests" in CLAUDE.md. This reads
the JUnit report, so the floors apply to what actually ran.

    python check_test_counts.py report.xml --min-collected 800 --min-passed 760 --max-skipped 60
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="pytest --junit-xml file")
    parser.add_argument("--min-collected", type=int, required=True)
    parser.add_argument("--min-passed", type=int, required=True)
    parser.add_argument("--max-skipped", type=int, required=True)
    args = parser.parse_args()

    root = ET.parse(args.report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    total = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failed = sum(int(s.get("failures", 0)) + int(s.get("errors", 0)) for s in suites)
    passed = total - skipped - failed
    print(f"collected {total}, passed {passed}, skipped {skipped}, failed {failed}")

    problems = []
    if total < args.min_collected:
        problems.append(f"collected {total} tests, expected at least {args.min_collected}")
    if passed < args.min_passed:
        problems.append(f"passed {passed} tests, expected at least {args.min_passed}")
    if skipped > args.max_skipped:
        problems.append(f"skipped {skipped} tests, expected at most {args.max_skipped}")
    for problem in problems:
        print(f"::error::{problem}. A test module may have stopped running; read the skips.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
