"""Coverage accounting: no stale snapshots, hidden debt or lost outcome guards."""
import ast
import json
from pathlib import Path

import pytest

from tests.agnostic_guard import DATA, ROOT, SEEN_RENDERS, read_json, reconcile, scan_render
from tests.agnostic_harness import PROVENANCE, fixtures
from tests.agnostic_guard import OBSERVED_RENDERS


@pytest.fixture(scope="session", autouse=True)
def rendered_coverage_accounting():
    yield
    expected = set(read_json("inventory.json")["renders"])
    assert SEEN_RENDERS == expected, (
        "Run the complete S1a guard or full suite; every retained snapshot must be exercised. "
        f"Missing: {sorted(expected - SEEN_RENDERS)}; new: {sorted(SEEN_RENDERS - expected)}")
    reconcile([h for hits in OBSERVED_RENDERS.values() for h in hits],
              [h for h in read_json("debt.json") if h["path"].startswith("render/")])


def test_retained_render_inventory_and_debt_have_no_orphans():
    inventory = read_json("inventory.json")
    paths = sorted((DATA / "renders").glob("*.json"))
    assert [p.stem for p in paths] == inventory["renders"]
    assert sorted(fixtures()) == inventory["fixtures"]
    for path in paths:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert set(record) == {"sha256", "excerpt"}
        assert len(record["sha256"]) == 64 and len(record["excerpt"]) <= 160
    assert {h["path"].split("/")[1] for h in read_json("debt.json")
            if h["path"].startswith("render/")} <= set(inventory["renders"])


def test_all_runtime_industry_branches_have_real_caller_fixtures():
    from app import industry_lingo
    specs = fixtures()
    assert {n.removeprefix("seed_") for n in specs if n.startswith("seed_")} == set(industry_lingo.LINGO)
    assert all(s["industry"] for n, s in specs.items() if n.startswith("seed_"))


def test_verified_register_rows_and_pinned_test_guards_are_preserved(request):
    evidence = read_json("outcomes.json")
    current = (ROOT / "docs/verified-behaviours.md").read_text(encoding="utf-8")
    for row in evidence["register_rows"]:
        assert row in current, "A verified outcome changed: " + row
    collected = {Path(i.path).name for i in request.session.items}
    # Collection accounting complements the full CI run. An assertion cannot be
    # replaced by a documentation-only claim, nor either historically omitted
    # CRM outcome silently removed from the run.
    for module, functions in evidence["guard_functions"].items():
        path = ROOT / "tests" / module
        nodes = ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        current_names = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert set(functions) <= current_names, f"Lost outcome guard(s) in {module}"
        # The focused S1a command checks existence; full CI runs every guard.
        if "test_promise_autoclose.py" in collected:
            assert module in collected, f"Full suite omitted {module}"
    assert {"test_redraft_crm_context.py", "test_promise_autoclose.py"} <= evidence["guard_functions"].keys()
    assert set(evidence["common_evals"]) == {"E1", "E2", "E4", "E5", "E8"}


def test_ci_has_an_unconditional_guard_and_full_offline_run():
    workflow = (ROOT / ".github/workflows/offline-tests.yml").read_text(encoding="utf-8")
    # Runs for every PR push and every merge to main. Push is limited to main only
    # so a PR push isn't tested twice; the no-filter bans below still apply.
    assert "pull_request:" in workflow
    assert "push:\n    branches: [main]" in workflow
    guard_command = next(line for line in workflow.splitlines()
                         if "run: python tests/run_offline.py tests/test_agnostic_context.py" in line)
    for name in ("context", "surfaces", "outcomes", "inventory"):
        assert f"tests/test_agnostic_{name}.py" in guard_command
    assert workflow.index(guard_command) < workflow.index("Install and expose disposable PostgreSQL")
    assert "python tests/run_offline.py tests -q" in workflow
    assert "--basetemp=.cache/pytest-full" in workflow
    assert "timeout-minutes: 30" in workflow
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    # CI must find real PostgreSQL binaries and put them on PATH (the runner's
    # own install; the exact discovery command may change).
    assert "/usr/lib/postgresql" in workflow and 'test -x "$pg_bin/initdb"' in workflow         and "GITHUB_PATH" in workflow
    assert '"$GITHUB_PATH"' in workflow and "-rs" in workflow
    assert 'findall(".//skipped")' in workflow and 'findall(".//testcase")' in workflow
    for module in ("booking_capacity_database", "offer_deadlines_database", "booking_guarantees_rollback",
                   "no_defaults_rollback", "offer_merge"):
        assert f"tests/test_{module}.py" in workflow
    for forbidden in ("paths:", "paths-ignore:", "if:", "continue-on-error", "AGNOSTIC_GUARD"):
        assert forbidden not in workflow


def test_no_skip_decorators_in_s1a_guard():
    for path in (ROOT / "tests").glob("test_agnostic_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"skip", "skipif", "xfail"}, f"Guard bypass in {path.name}"


@pytest.mark.parametrize("xml,accepted", [
    ('<testsuites><testsuite><testcase name="db"/></testsuite></testsuites>', True),
    ('<testsuites><testsuite><testcase name="db"><skipped/></testcase></testsuite></testsuites>', False),
    ('<testsuites><testsuite/></testsuites>', False),
])
def test_ci_database_report_check_rejects_skips_and_empty_runs(monkeypatch, xml, accepted):
    import textwrap
    import xml.etree.ElementTree as ET
    workflow = (ROOT / ".github/workflows/offline-tests.yml").read_text(encoding="utf-8")
    script = workflow.split("python - <<'PY'\n", 1)[1].split("          PY", 1)[0]
    monkeypatch.setattr(ET, "parse", lambda path: ET.ElementTree(ET.fromstring(xml)))
    if accepted:
        exec(compile(textwrap.dedent(script), "workflow-db-check", "exec"), {})
    else:
        with pytest.raises(AssertionError):
            exec(compile(textwrap.dedent(script), "workflow-db-check", "exec"), {})
