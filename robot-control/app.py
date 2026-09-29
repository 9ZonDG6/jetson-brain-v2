"""Robot control web app: RC monitor + safe RC/WEB control of two hardware PWM outputs.

    sudo .venv/bin/python app.py            # real hardware PWM
    sudo .venv/bin/python app.py --dry-run  # PWM untouched, prints WOULD SET PWM
"""

import argparse
import asyncio
import atexit
import json
import logging
import os
import signal
import uuid
import weakref
from pathlib import Path

from aiohttp import WSMsgType, web

from cpu_tuning import CpuTuning
from motor_io import MotorIO
from rc_input import RCInput
from safety import ArmError, SafetyController

BASE = Path(__file__).resolve().parent
STATUS_PUSH_S = 0.05
PIDFILE = "/run/robot-control.pid"

log = logging.getLogger("app")
CTRL = web.AppKey("ctrl", SafetyController)
SOCKETS = web.AppKey("sockets", weakref.WeakSet)


async def _body(request):
    try:
        text = await request.text()
        data = json.loads(text) if text else {}
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def _client_id(request, body=None):
    return request.headers.get("X-Client-Id") or (body or {}).get("client_id") or request.query.get("client_id")


async def index(request):
    return web.FileResponse(BASE / "templates" / "index.html", headers={"Cache-Control": "no-store"})


async def api_status(request):
    return web.json_response(request.app[CTRL].status(_client_id(request)))


async def api_arm_rc(request):
    ctrl = request.app[CTRL]
    try:
        ctrl.arm_rc()
    except ArmError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=409)
    return web.json_response({"ok": True, "mode": ctrl.mode})


async def api_arm_web(request):
    ctrl = request.app[CTRL]
    body = await _body(request)
    try:
        ctrl.arm_web(_client_id(request, body))
    except ArmError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=409)
    return web.json_response({"ok": True, "mode": ctrl.mode})


async def api_stop(request):
    ctrl = request.app[CTRL]
    ctrl.stop("STOP" if request.query.get("reason") != "pagehide" else "STOP (page closed)")
    return web.json_response({"ok": True, "mode": ctrl.mode})


async def api_center(request):
    ctrl = request.app[CTRL]
    body = await _body(request)
    ctrl.set_web(_client_id(request, body), 1500, 1500)
    return web.json_response({"ok": True})


async def api_web_control(request):
    ctrl = request.app[CTRL]
    body = await _body(request)
    ok = ctrl.set_web(_client_id(request, body), body.get("ch1_us"), body.get("ch2_us"))
    if not ok:
        return web.json_response({"ok": False, "error": "WEB control not armed by this client"}, status=409)
    st = ctrl.status()
    return web.json_response({"ok": True, "out1_us": st["out1_us"], "out2_us": st["out2_us"]})


async def api_heartbeat(request):
    ctrl = request.app[CTRL]
    body = await _body(request)
    return web.json_response({"ok": ctrl.heartbeat(_client_id(request, body))})


async def ws_handler(request):
    ctrl = request.app[CTRL]
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    cid = request.query.get("client_id") or uuid.uuid4().hex

    async def push():
        try:
            while not ws.closed:
                await ws.send_str(json.dumps(ctrl.status(cid)))
                await asyncio.sleep(STATUS_PUSH_S)
        except ConnectionError:
            pass

    pusher = asyncio.create_task(push())
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if kind == "hb":
                ctrl.heartbeat(cid)
            elif kind == "control":
                ctrl.set_web(cid, data.get("ch1_us"), data.get("ch2_us"))
    finally:
        pusher.cancel()
        ctrl.client_disconnected(cid)
    return ws


async def on_shutdown(app):
    # Neutral first, then drop browsers so graceful shutdown does not wait on them.
    app[CTRL].begin_shutdown()
    for ws in list(app[SOCKETS]):
        await ws.close(code=1001, message=b"server shutdown")


@web.middleware
async def no_cache(request, handler):
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def create_app(ctrl):
    app = web.Application(middlewares=[no_cache])
    app[CTRL] = ctrl
    app[SOCKETS] = weakref.WeakSet()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/arm/rc", api_arm_rc)
    app.router.add_post("/api/arm/web", api_arm_web)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/center", api_center)
    app.router.add_post("/api/web-control", api_web_control)
    app.router.add_post("/api/heartbeat", api_heartbeat)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", BASE / "static")
    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="do not touch hardware PWM, print WOULD SET PWM")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-cpu-tuning", action="store_true",
                    help="keep CPU governor/idle as is (RC timing jitter gets much worse)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    try:
        with open(PIDFILE, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    tuning = CpuTuning()
    if not args.no_cpu_tuning:
        tuning.apply()
    atexit.register(tuning.restore)

    motor = MotorIO(dry_run=args.dry_run)
    try:
        motor.initialize()
    except Exception as exc:
        log.error("PWM ERROR: %s (arming disabled)", exc)

    rc = RCInput()
    rc.start()
    ctrl = SafetyController(motor, rc)
    ctrl.start()
    atexit.register(ctrl.shutdown)

    def _hup(signum, frame):
        raise KeyboardInterrupt  # ssh disconnect -> same clean path as Ctrl+C

    signal.signal(signal.SIGHUP, _hup)

    log.info("Web UI: http://%s:%d  (%s)", args.host, args.port, "DRY-RUN" if args.dry_run else "HARDWARE PWM")
    try:
        web.run_app(create_app(ctrl), host=args.host, port=args.port, print=None,
                    handle_signals=True, shutdown_timeout=2.0)
    finally:
        ctrl.shutdown()
        rc.stop()
        tuning.restore()
        try:
            os.unlink(PIDFILE)
        except OSError:
            pass
        log.info("stopped, outputs 1500/1500")


if __name__ == "__main__":
    main()
