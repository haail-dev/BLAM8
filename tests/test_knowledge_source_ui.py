from pathlib import Path
import shutil
import subprocess


def test_source_review_controller_serializes_requests_and_preserves_unsaved_work():
    node = shutil.which('node')
    assert node, 'Node is required for the offline source-review replay'
    result = subprocess.run([node, 'tests/replays/knowledge_source_review.mjs'],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
