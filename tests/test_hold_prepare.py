import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_press_and_hold_prepares_voice_on_pointerdown():
    result = subprocess.run([shutil.which("node"), "tests/replays/hold_prepare_replay.mjs"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
