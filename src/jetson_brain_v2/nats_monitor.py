"""Read-only NATS and vision-command status for the operator dashboard."""

import asyncio
import json
import logging
import time

import nats

log = logging.getLogger("nats_monitor")


class NatsMonitor:
    def __init__(self, url, subject="robot.vision.localization"):
        self.url = url
        self.subject = subject
        self._client = None
        self._task = None
        self._last_at = None
        self._count = 0
        self._move_type = None
        self._route = None
        self.error = None

    def start(self):
        self._task = asyncio.create_task(self._run(), name="nats-monitor")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self):
        age_ms = None if self._last_at is None else max(0, round((time.monotonic() - self._last_at) * 1000))
        return {
            "connected": self._client is not None and self._client.is_connected,
            "url": self.url,
            "subject": self.subject,
            "message_count": self._count,
            "last_message_age_ms": age_ms,
            "recent": age_ms is not None and age_ms < 2000,
            "last_move_type": self._move_type,
            "last_route": self._route,
            "error": self.error,
        }

    async def _receive(self, message):
        self._last_at = time.monotonic()
        self._count += 1
        try:
            data = json.loads(message.data)
            if isinstance(data, dict):
                self._move_type = data.get("move_type") if isinstance(data.get("move_type"), str) else None
                self._route = data.get("route") if isinstance(data.get("route"), str) else None
            else:
                self._move_type = None
                self._route = None
        except (UnicodeDecodeError, ValueError):
            self._move_type = None
            self._route = None

    async def _run(self):
        while True:
            client = None
            try:
                client = await nats.connect(
                    servers=[self.url], connect_timeout=2,
                    allow_reconnect=False,
                )
                self._client = client
                self.error = None
                await client.subscribe(self.subject, cb=self._receive)
                log.info("NATS monitor subscribed to %s at %s", self.subject, self.url)
                while not client.is_closed:
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc)
                log.warning("NATS monitor unavailable: %s", exc)
            finally:
                self._client = None
                if client is not None and not client.is_closed:
                    await client.close()
            await asyncio.sleep(2)
