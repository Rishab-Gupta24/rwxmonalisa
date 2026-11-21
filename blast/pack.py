"""Split a set of tests over up to 8 workers for the lowest cost.

cost = busiest worker's milliseconds + 2000 ms per worker; no tests costs 0.
"""

WORKER_MS = 2000


def lpt(durations, k):
    """Longest-processing-time first onto the least loaded worker."""
    loads = [0] * k
    bins = [[] for _ in range(k)]
    for num, ms in durations:
        i = loads.index(min(loads))
        loads[i] += ms
        bins[i].append(num)
    return max(loads), bins


def pack(tests_ms):
    """tests_ms: list of (number, ms). Returns (cost_ms, workers)."""
    if not tests_ms:
        return 0, []
    items = sorted(tests_ms, key=lambda x: -x[1])
    total = sum(ms for _, ms in items)
    biggest = items[0][1]
    best = None
    for k in range(1, min(8, len(items)) + 1):
        # no split over k workers can beat this bound
        if best is not None and max(biggest, total / k) + WORKER_MS * k >= best[0]:
            continue
        span, bins = lpt(items, k)
        cost = span + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, [b for b in bins if b])
    return best
