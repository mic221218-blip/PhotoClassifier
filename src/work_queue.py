"""Keep only a bounded number of background operations in flight."""
from concurrent.futures import FIRST_COMPLETED, wait


def bounded_results(pool, items, work, *, limit, cancelled):
    """Yield (input, future), draining running work after cancellation."""
    items = iter(items)
    pending = {}
    exhausted = False
    while True:
        if cancelled():
            exhausted = True
            for future in pending:
                future.cancel()
        while not exhausted and len(pending) < limit and not cancelled():
            try:
                item = next(items)
            except StopIteration:
                exhausted = True
                break
            if cancelled():
                exhausted = True
                break
            pending[pool.submit(work, item)] = item
        if not pending:
            return
        finished, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
        for future in finished:
            item = pending.pop(future)
            if not future.cancelled():
                yield item, future
