import asyncio
import os


os.environ.setdefault("FENNEC_SERVICE_TOKEN", "test-service-token-at-least-24-characters")
os.environ.setdefault("FENNEC_SESSION_SECRET", "test-session-secret-at-least-32-characters-long")


async def wait_until(predicate, *, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
