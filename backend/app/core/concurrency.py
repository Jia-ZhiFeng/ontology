"""Keep blocking database work off the ASGI event loop."""

from functools import wraps

from starlette.concurrency import run_in_threadpool


def threaded_endpoint(function):
    """Preserve an awaitable adapter and its FastAPI signature for sync handlers."""
    @wraps(function)
    async def dispatch(*args, **kwargs):
        return await run_in_threadpool(function, *args, **kwargs)

    return dispatch
