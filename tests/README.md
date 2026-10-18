# Offline regression tests

Run the suite from the repository root:

```sh
python tests/run_offline.py tests -q --tb=short
```

The runner disables local `.env` loading and outbound Python sockets before
importing application code. Database tests use disposable local PostgreSQL
clusters. Set `OUTREACH_TEST_POSTGRES_BIN` to the directory containing the
PostgreSQL binaries if they are not on PATH.

- `test_*.py`: application behaviour and integration regressions.
- `fixtures/`: synthetic test inputs and reviewed agnostic-guard records.
- `replays/`: Node-based JavaScript replays with synthetic events and fake DOMs.
- `speech/`: separate speech worker protocol tests using fake models.
- `agnostic_*.py` and other fixture helpers: shared test support.

Node.js is needed for the JavaScript replays. No Playwright installation or
browser screenshot pipeline is required. Speech protocol tests require the
development dependencies, including NumPy; they download no speech models.

Test filenames stay stable because existing regression modules share fixtures
and CI selects certain database tests explicitly. Keep new generated evidence
under `.cache/`, not beside test source.
