"""Receive robot-vision localization messages without blocking motor safety."""

import asyncio
import json
import logging
import threading

log = logging.getLogger("ai_source")


class NatsAiSource:
    def __init__(self, url, subject, drive):
        self.url = url
        self.subject = subject
        self.drive = drive
        self.error = None
        self._loop = None
        self._thread = None
        self._nc = None
        self._closed = threading.Event()

    @property
    def connected(self):
        return self._nc is not None and self._nc.is_connected

    def start(self):
        self._thread = threading.Thread(target=self._run, name="ai-nats", daemon=True)
        self._thread.start()

    def publish(self, subject, payload, timeout_s=2.0):
        """Publish one JSON message on the already-open connection.

        Returns False instead of raising when NATS is down, so callers can
        decide whether a missing control message is fatal.
        """
        loop = self._loop
        nc = self._nc
        if loop is None or nc is None or not loop.is_running() or not nc.is_connected:
            return False
        data = json.dumps(payload).encode("utf-8")

        async def send():
            await nc.publish(subject, data)
            await nc.flush(timeout=timeout_s)

        future = asyncio.run_coroutine_threadsafe(send(), loop)
        try:
            future.result(timeout=timeout_s + 0.5)
            return True
        except Exception as exc:
            log.error("NATS publish to %s failed: %s", subject, exc)
            return False

    def close(self):
        self._closed.set()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self):
        try:
            import nats
        except ImportError as exc:
            self.error = "nats-py is not installed: %s" % exc
            log.error(self.error)
            return

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        async def connect():
            self._nc = await nats.connect(
                servers=[self.url],
                connect_timeout=3,
                max_reconnect_attempts=-1,
            )

            async def receive(message):
                self.drive.ingest(message.data)

            await self._nc.subscribe(self.subject, cb=receive)
            log.info("AI source subscribed to %s at %s", self.subject, self.url)

        try:
            loop.run_until_complete(connect())
            if not self._closed.is_set():
                loop.run_forever()
        except Exception as exc:
            self.error = str(exc)
            log.error("AI NATS source failed: %s", exc)
        finally:
            try:
                if self._nc is not None and not self._nc.is_closed:
                    loop.run_until_complete(self._nc.close())
            except Exception:
                pass
            loop.close()
