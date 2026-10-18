"""Execute the actual browser reveal code against a deterministic DOM harness."""
from pathlib import Path
import shutil
import subprocess


def test_ghost_dom_replay():
    node = shutil.which("node")
    assert node, "Node is required for the ghost DOM replay"
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([node, str(root / "tests" / "replays" / "ghost_replay.cjs")],
                            cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_live_stream_flushes_without_delaying_events_and_reinitialises_after_swap():
    root = Path(__file__).resolve().parents[1]
    js = (root / "src/app/static/live.js").read_text(encoding="utf-8")
    assert js.index("thread.innerHTML = html") < js.index("window.blam8Ghost.scan(thread)")
    assert "data.key !== draftKey" in js  # unrelated draft completion cannot erase this trail
    assert "stepSequence" in js  # repeated actions remain distinct real events
    assert "draftSteps[draftSteps.length - 1] !== data.label" not in js
