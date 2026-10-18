import contextvars
import threading
import time

import pytest

from app import parallel_reads


def test_results_come_back_in_input_order_and_overlap():
    started = time.monotonic()
    out = parallel_reads.run_all([lambda i=i: (time.sleep(0.2), i)[1] for i in range(6)])
    assert out == list(range(6))
    assert time.monotonic() - started < 0.8   # six 0.2 s reads side by side, not 1.2 s in a row


def test_context_reaches_the_workers():
    var = contextvars.ContextVar("v", default="none")
    var.set("request")
    assert parallel_reads.run_all([var.get, var.get]) == ["request", "request"]


def test_a_failing_read_raises_like_the_sequential_loop():
    def boom():
        raise ValueError("read failed")
    with pytest.raises(ValueError):
        parallel_reads.run_all([lambda: 1, boom])


def test_one_call_runs_inline():
    assert parallel_reads.run_all([threading.current_thread]) == [threading.current_thread()]
