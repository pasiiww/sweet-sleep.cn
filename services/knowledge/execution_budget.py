"""Cooperative deadlines shared by an agent's synchronous I/O tools."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar('sweet_agent_deadline', default=None)


class DeadlineExceeded(RuntimeError):
    pass


@contextmanager
def until(deadline):
    token = _deadline.set(deadline)
    try:
        check()
        yield
    finally:
        _deadline.reset(token)


def remaining():
    deadline = _deadline.get()
    if deadline is None:
        return None
    value = deadline - time.monotonic()
    if value <= 0:
        raise DeadlineExceeded('agent_deadline')
    return value


def check():
    remaining()


@contextmanager
def locked(lock):
    value = remaining()
    acquired = lock.acquire() if value is None else lock.acquire(timeout=value)
    if not acquired:
        raise DeadlineExceeded('lock_deadline')
    try:
        check()
        yield
    finally:
        lock.release()


def timeout(default):
    value = remaining()
    return default if value is None else min(default, value)


def read_response(response, limit, socket_timeout=8):
    """Bound each chunk by the remaining wall-clock budget, including slow bodies."""
    if not callable(getattr(response, 'read1', None)):
        check()
        result = response.read(limit + 1)
        check()
        return result
    chunks, size = [], 0
    while size <= limit:
        interval = timeout(socket_timeout)
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(interval)
        chunk = response.read1(min(65536, limit + 1 - size))
        check()
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b''.join(chunks)
