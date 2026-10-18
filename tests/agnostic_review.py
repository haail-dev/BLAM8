"""Export full real-caller renders for review without blessing any baseline.

Run: python tests/agnostic_review.py .cache/agnostic-review
The offline runner still executes every assertion; a changed baseline exits
nonzero but its complete actual render and stable-key debt diff are retained.
"""
import json
from pathlib import Path
import runpy
import sys


def pytest_addoption(parser):
    parser.addoption("--agnostic-review-dir", required=True)


def pytest_configure(config):
    from tests import agnostic_guard as guard
    target = Path(config.getoption("--agnostic-review-dir")).resolve()
    assert target.is_relative_to(guard.ROOT.resolve()), "Review output must stay in this worktree"
    target.mkdir(parents=True, exist_ok=False)
    guard.REVIEW_DIR = target


def pytest_sessionfinish(session, exitstatus):
    from tests import agnostic_guard as guard
    actual = guard.scan_sources() + [h for hits in guard.OBSERVED_RENDERS.values() for h in hits]
    previous = {guard.key(h): h for h in guard.read_json("debt.json")}
    current = {guard.key(h): h for h in actual}
    changes = {"new_or_changed": [current[k] for k in sorted(current.keys() - previous.keys())],
               "stale_or_changed": [previous[k] for k in sorted(previous.keys() - current.keys())],
               "test_exit_status": int(exitstatus)}
    (guard.REVIEW_DIR / "debt-changes.json").write_text(
        json.dumps(changes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python tests/agnostic_review.py .cache/agnostic-review")
    target = sys.argv[1]
    sys.argv = [str(root / "tests/run_offline.py"), "-p", "tests.agnostic_review",
                "--agnostic-review-dir", target,
                *[f"tests/test_agnostic_{name}.py" for name in ("context", "surfaces", "outcomes", "inventory")],
                "-q", "--tb=short", "--disable-warnings", "--basetemp=.cache/pytest-review"]
    runpy.run_path(str(root / "tests/run_offline.py"), run_name="__main__")
