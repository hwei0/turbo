"""Thread pool wrapper with future tracking and periodic health checks.

ThreadPoolManager wraps a ThreadPoolExecutor and tracks submitted futures. It provides
periodic checks (every REFRESH_INTERVAL seconds) to detect completed or failed tasks,
propagating exceptions from background threads. Used throughout the system to offload
non-blocking I/O operations (e.g., SpillableStore writes) without blocking the main loop.
"""

from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Lock
import time

REFRESH_INTERVAL = 5


class ThreadPoolManager:
    def __init__(self, *args, **kwargs):
        self.thread_pool = ThreadPoolExecutor(*args, **kwargs)
        self.awaitables = []
        self.lock = Lock()
        self.previous_time = time.perf_counter()

    def submit(self, fn, *args, **kwargs):
        with self.lock:
            task = self.thread_pool.submit(fn, *args, **kwargs)
            self.awaitables.append(task)

    def check_due(self):
        return time.perf_counter() - self.previous_time >= REFRESH_INTERVAL

    def check_pending(self):
        with self.lock:
            del_list = []
            old_len = len(self.awaitables)
            for future in self.awaitables:
                if future.done() and future.exception():
                    raise future.exception()
                if future.done():
                    del_list.append(future)
                else:
                    pass

            self.awaitables = [
                future for future in self.awaitables if future not in del_list
            ]
            assert old_len == len(self.awaitables) + len(del_list)
            self.previous_time = time.perf_counter()

    def await_all(self):
        """Wait for all submitted tasks to complete. Blocks until done.

        Note: This is a blocking synchronous method, not async.
        """
        with self.lock:
            for future in self.awaitables:
                future.result()

            self.awaitables = []
