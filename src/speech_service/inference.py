"""Bounded FIFO inference; cancellation never starts overlapping model calls."""
import asyncio
from contextlib import suppress


class Busy(Exception):
    pass


class Worker:
    def __init__(self, size):
        self.queue = asyncio.Queue(maxsize=size)
        self.task = None
        self.busy = False

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def call(self, function, *args, optional=False):
        if optional and (self.busy or not self.queue.empty()):
            raise Busy()
        future = asyncio.get_running_loop().create_future()
        try:
            self.queue.put_nowait((future, function, args))
        except asyncio.QueueFull:
            raise Busy() from None
        return await future

    async def run(self):
        while True:
            future, function, args = await self.queue.get()
            try:
                if future.cancelled():
                    continue
                self.busy = True
                result = await asyncio.to_thread(function, *args)
                if not future.done():
                    future.set_result(result)
            except Exception as exc:
                if not future.done():
                    future.set_exception(exc)
            finally:
                self.busy = False
                self.queue.task_done()

    async def close(self):
        await self.queue.join()
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
