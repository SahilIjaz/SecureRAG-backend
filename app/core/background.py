"""
Fire-and-forget background tasks, shared by any endpoint that wants to do
work after a response is already on its way to the client (conversation
classification, notification dispatch, ...).

asyncio.create_task() only holds a *weak* reference to the task it returns —
with nothing else referencing it, the GC can collect (and silently cancel)
it before it ever runs. This module is that "something else": every task
gets parked in a module-level set until it finishes, per the asyncio docs'
own recommended pattern. Confirmed live: without this, a task created this
way can vanish before doing any work at all, with no error and no log line.
"""

import asyncio
from typing import Coroutine

_background_tasks: set[asyncio.Task] = set()

def fire_and_forget(coro: Coroutine) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
