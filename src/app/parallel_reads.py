"""Run independent blocking reads side by side instead of one after another.

Each Supabase read from Render costs ~45-110 ms of network, so a request that
does N reads in a row pays N times. These reads share nothing, so they overlap.
The caller's context (request timing, actor) is copied into each worker and
results come back in input order; an exception in any worker is raised here,
exactly as it would have been in the sequential loop.
"""
import contextvars
from concurrent.futures import ThreadPoolExecutor

MAX_WORKERS = 8


def run_all(calls):
    """Run each zero-argument callable and return their results in order."""
    calls = list(calls)
    if len(calls) < 2:
        return [call() for call in calls]
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(calls))) as pool:
        futures = [pool.submit(contextvars.copy_context().run, call) for call in calls]
        return [future.result() for future in futures]
