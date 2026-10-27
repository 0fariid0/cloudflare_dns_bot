"""Keep each user's forms ordered while other users remain responsive."""
import asyncio
from contextvars import ContextVar
from telegram.ext import BaseUpdateProcessor

# Mutable scope propagates through asyncio.to_thread and back to the calling task.
# Independent updates never overwrite each other's Cloudflare error messages.
api_error_scope = ContextVar('api_error_scope', default=None)


class PerUserProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates=8):
        super().__init__(max_concurrent_updates)
        self._locks = {}

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_process_update(self, update, coroutine):
        user = getattr(update, 'effective_user', None)
        key = ('user', user.id) if user else ('update', getattr(update, 'update_id', id(update)))
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        token = api_error_scope.set({'error': None})
        try:
            async with entry[0]:
                await coroutine
        finally:
            api_error_scope.reset(token)
            entry[1] -= 1
            if not entry[1]:
                self._locks.pop(key, None)
