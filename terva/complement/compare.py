#!/usr/bin/env python3
"""Compare a Complement run against the recorded baseline.

Complement emits one JSON object per line, `{"Action": ..., "Test": ...}`, where
Action is pass, fail, or skip. Upstream keeps its own results in
`tests/complement/results.jsonl`. This compares a fresh run against the snapshot
in `terva/complement/baseline.jsonl` and reports what moved.

Only a regression sets a non-zero exit status. An improvement is reported and
then ignored, because a fix should not have to update the gate before CI will
let it through. Update the snapshot in the same commit as the fix instead.

A test may appear more than once in one file. Upstream's own baseline records
TestSync as both pass and fail, so this is not hypothetical. The worst result
wins: a test that failed on any line counts as failed. That keeps a flaky test
from flipping the gate depending on which line landed last, at the cost of
recording it as a failure until it is fixed or quarantined.

Usage:
    terva/complement/compare.py RUN.jsonl
    terva/complement/compare.py RUN.jsonl --baseline terva/complement/baseline-complemau.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Worst first. A test's recorded result is the worst one it produced.
SEVERITY = {"fail": 2, "skip": 1, "pass": 0}

DEFAULT_BASELINE = Path(__file__).parent / "baseline.jsonl"


def load(path: Path) -> dict[str, str]:
    """Read a Complement results file into test name -> worst action."""
    results: dict[str, str] = {}
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno}: not valid JSON: {exc}") from exc

        try:
            test, action = record["Test"], record["Action"]
        except KeyError as exc:
            raise SystemExit(f"{path}:{lineno}: record has no {exc} field") from exc

        if action not in SEVERITY:
            raise SystemExit(f"{path}:{lineno}: unknown action {action!r}")

        previous = results.get(test)
        if previous is None or SEVERITY[action] > SEVERITY[previous]:
            results[test] = action

    if not results:
        raise SystemExit(f"{path}: no results, refusing to compare against nothing")
    return results


def report(heading: str, tests: list[str]) -> None:
    if not tests:
        return
    print(f"\n{heading} ({len(tests)})")
    for test in sorted(tests):
        print(f"  {test}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="results.jsonl from the run to check")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"baseline to compare against (default: {DEFAULT_BASELINE.name})",
    )
    args = parser.parse_args()

    for path in (args.run, args.baseline):
        if not path.is_file():
            raise SystemExit(f"{path}: no such file")

    baseline = load(args.baseline)
    run = load(args.run)

    # A regression is any test failing now that was not failing before, which
    # covers skip -> fail as well as pass -> fail.
    regressed = [t for t, a in run.items() if a == "fail" and baseline.get(t, "pass") != "fail"]
    improved = [t for t, a in run.items() if a != "fail" and baseline.get(t) == "fail"]

    # A test that stopped running is not a failure, but it is lost coverage and
    # nothing else would report it.
    disappeared = [t for t in baseline if t not in run]
    added = [t for t in run if t not in baseline]
    newly_skipped = [t for t, a in run.items() if a == "skip" and baseline.get(t) == "pass"]

    def tally(results: dict[str, str]) -> str:
        counts = {action: sum(1 for a in results.values() if a == action) for action in SEVERITY}
        return f"{counts['pass']} pass, {counts['fail']} fail, {counts['skip']} skip"

    print(f"baseline  {args.baseline}: {len(baseline)} tests, {tally(baseline)}")
    print(f"run       {args.run}: {len(run)} tests, {tally(run)}")

    report("REGRESSED, failing now and not before", regressed)
    report("Fixed, failing before and not now", improved)
    report("Newly skipped, passing before", newly_skipped)
    report("Gone from the run, present in the baseline", disappeared)
    report("New in the run, absent from the baseline", added)

    if regressed:
        print(f"\n{len(regressed)} regressed. Fix them, or say on the ticket why they are acceptable.")
        return 1

    if improved:
        print(
            f"\nNo regressions. {len(improved)} newly passing: refresh the baseline "
            "in the same commit as the fix."
        )
    else:
        print("\nNo regressions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
