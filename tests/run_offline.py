"""Run pytest without reading .env or permitting outbound Python sockets.

Usage: python tests/run_offline.py [pytest arguments]
The optional PostgreSQL integration fixture creates its own disposable cluster;
it never obtains a connection string from the environment.
"""
from pathlib import Path
import os
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)
# Keep disposable files inside this isolated worktree, including on locked-down Windows.
import tempfile
scratch_root = Path(os.environ.get("BLAM8_PG_TEST_TMP", ROOT / ".cache"))
scratch = scratch_root / "offline-tmp"
scratch.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(scratch)
os.environ["TEMP"] = os.environ["TMP"] = str(scratch)

# Do this before importing application modules or collecting any tests.
keep = {k: v for k, v in os.environ.items() if k.upper() in {
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT",
    "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL",
    "BLAM8_PG_TEST_TMP",
    "OUTREACH_TEST_POSTGRES_BIN",  # local disposable PostgreSQL binary directory
    "BLAM8_TEST_CHROMIUM_BIN",  # local headless browser executable, no account/profile
}}
os.environ.clear()
os.environ.update(keep)
os.environ.update({
    "GMAIL_POLL_SECONDS": "0", "CALENDAR_SYNC_SECONDS": "0",
    "SUBSCRIPTION_RENEW_SECONDS": "0", "RUN_DAILY_JOBS": "false",
    "SUPABASE_URL": "https://offline.invalid", "SUPABASE_SERVICE_ROLE_KEY": "offline-test",
    "ANTHROPIC_API_KEY": "offline-test", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
})

from pydantic_settings import BaseSettings

_settings_init = BaseSettings.__init__


def _without_dotenv(self, *args, **kwargs):
    kwargs["_env_file"] = None
    return _settings_init(self, *args, **kwargs)


BaseSettings.__init__ = _without_dotenv

_connect = socket.socket.connect


def _local_socketpair(*args, **kwargs):
    # Windows asyncio uses a TCP socketpair for its own wake-up pipe. Permit
    # precisely a connection to the listener we just created, not localhost
    # generally (where a developer may have a production tunnel running).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        writer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            _connect(writer, listener.getsockname())
            reader, _ = listener.accept()
            return reader, writer
        except BaseException:
            writer.close()
            raise


if os.name == "nt":
    socket.socketpair = _local_socketpair


def _blocked(*args, **kwargs):
    raise RuntimeError("offline test runner blocked a network connection")


socket.socket.connect = _blocked
socket.socket.connect_ex = _blocked
socket.create_connection = _blocked
socket.getaddrinfo = _blocked

if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main(["-p", "pytest_asyncio.plugin", *sys.argv[1:]]))
