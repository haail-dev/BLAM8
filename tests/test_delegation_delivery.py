import pathlib
import shutil
import subprocess


def test_actual_delivery_scheduler_deferred_audio_and_revision_races():
    root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run([shutil.which("node") or "node", "tests/replays/delegation_delivery_replay.mjs"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_actual_panel_snapshot_versions_and_approval_gap_wiring():
    root = pathlib.Path(__file__).resolve().parents[1]
    result = subprocess.run([shutil.which("node") or "node", "tests/replays/delegation_panel_replay.mjs"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
