"""Robot control web app: RC monitor + safe RC/WEB control of two hardware PWM outputs.

    sudo .venv/bin/jetson-brain-v2            # real hardware PWM
    sudo .venv/bin/jetson-brain-v2 --dry-run  # PWM untouched, prints WOULD SET PWM
"""

import argparse
import asyncio
import atexit
import json
import logging
import os
import signal
import subprocess
import uuid
import weakref
from pathlib import Path

from aiohttp import WSMsgType, web
from aiohttp.helpers import content_disposition_header

from .ai_admin import AiAdmin
from .camera import CameraConfig, CameraManager
from .cpu_tuning import CpuTuning
from .motor_io import MotorIO
from .nats_monitor import NatsMonitor
from .rc_input import RCInput
from .safety import ArmError, SafetyController

BASE = Path(__file__).resolve().parent
STATUS_PUSH_S = 0.05
PIDFILE = "/run/robot-control.pid"

log = logging.getLogger("app")
CTRL = web.AppKey("ctrl", SafetyController)
SOCKETS = web.AppKey("sockets", weakref.WeakSet)
AI_ADMIN = web.AppKey("ai_admin", AiAdmin)
CONTROL_URL = web.AppKey("control_url", str)
CAMERA = web.AppKey("camera", CameraManager)
PREVIEW_JOBS = web.AppKey("preview_jobs", dict)
NATS_MONITOR = web.AppKey("nats_monitor", NatsMonitor)


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
    return web.FileResponse(BASE / "web" / "templates" / "index.html", headers={"Cache-Control": "no-store"})


async def api_status(request):
    result = request.app[CTRL].status(_client_id(request))
    result["ai"] = request.app[AI_ADMIN].status()
    result["camera"] = request.app[CAMERA].status()
    result["nats"] = request.app[NATS_MONITOR].status()
    return web.json_response(result)


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
    await asyncio.to_thread(request.app[AI_ADMIN].stop)
    return web.json_response({"ok": True, "mode": ctrl.mode})


async def api_ai_config(request):
    try:
        config = request.app[AI_ADMIN].save_wasd_config(await _body(request))
    except (TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    from dataclasses import asdict
    return web.json_response({"ok": True, "config": asdict(config)})


async def api_ai_start(request):
    if request.app[CTRL].mode != "DISARMED":
        return web.json_response({"ok": False, "error": "STOP manual control before ARM AI"}, status=409)
    body = await _body(request)
    try:
        state = request.app[AI_ADMIN].start(body.get("nats_url", ""), request.app[CONTROL_URL],
                                            allow_dry_run=request.app[CTRL].status()["pwm_status"] == "DRY-RUN",
                                            route=body.get("route") or None)
    except (OSError, TypeError, ValueError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    return web.json_response({"ok": True, "ai": state})


async def api_ai_stop(request):
    request.app[CTRL].stop("STOP AI")
    return web.json_response({"ok": True, "ai": await asyncio.to_thread(request.app[AI_ADMIN].stop)})


async def api_camera_record_start(request):
    try:
        path = request.app[CAMERA].start_recording()
    except (OSError, RuntimeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=409)
    return web.json_response({"ok": True, "file": path})


async def api_camera_record_stop(request):
    path = request.app[CAMERA].stop_recording()
    return web.json_response({"ok": True, "file": path})


async def api_camera_focus(request):
    try:
        state = await asyncio.to_thread(request.app[CAMERA].focus_status)
    except (OSError, RuntimeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=503)
    return web.json_response({"ok": True, "focus": state})


async def api_camera_focus_set(request):
    body = await _body(request)
    if not body or set(body) - {"auto", "value"}:
        return web.json_response({"ok": False, "error": "Укажите режим или значение фокуса"}, status=400)
    try:
        state = await asyncio.to_thread(request.app[CAMERA].set_focus,
                                        auto=body.get("auto"), value=body.get("value"))
    except ValueError as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except (OSError, RuntimeError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=503)
    return web.json_response({"ok": True, "focus": state})


async def api_camera_recordings(request):
    try:
        recordings = request.app[CAMERA].recordings()
    except OSError as exc:
        log.error("cannot list camera recordings: %s", exc)
        return web.json_response({"error": "Не удалось прочитать папку записей"}, status=503)
    return web.json_response({"recordings": recordings})


async def api_camera_recording_download(request):
    try:
        path = request.app[CAMERA].recording_file(request.match_info["name"])
    except FileNotFoundError:
        raise web.HTTPNotFound()
    response = web.FileResponse(path, headers={"Content-Type": "video/x-msvideo"})
    response.headers["Content-Disposition"] = content_disposition_header("attachment", filename=path.name)
    return response


async def api_camera_recording_preview(request):
    try:
        path = request.app[CAMERA].prepared_preview_file(request.match_info["name"])
    except FileNotFoundError:
        raise web.HTTPNotFound()
    if path is None:
        raise web.HTTPConflict(text="video preview is not ready")
    return web.FileResponse(path, headers={"Content-Type": "video/mp4"})


async def api_camera_recording_prepare(request):
    name = request.match_info["name"]
    camera = request.app[CAMERA]
    try:
        ready = camera.prepared_preview_file(name) is not None
    except FileNotFoundError:
        raise web.HTTPNotFound()
    jobs = request.app[PREVIEW_JOBS]
    job = jobs.get(name)
    if not ready and (job is None or job.done()):
        jobs[name] = asyncio.create_task(asyncio.to_thread(camera.preview_file, name))
    return web.json_response({"ok": True, "ready": ready})


async def api_camera_recording_preview_status(request):
    name = request.match_info["name"]
    try:
        ready = request.app[CAMERA].prepared_preview_file(name) is not None
    except FileNotFoundError:
        raise web.HTTPNotFound()
    job = request.app[PREVIEW_JOBS].get(name)
    if job is not None and job.done() and not ready:
        try:
            job.result()
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            log.error("video preview failed: %s", exc)
            return web.json_response({"ok": False, "error": "Не удалось подготовить видео"}, status=500)
    return web.json_response({"ok": True, "ready": ready})


async def api_camera_recording_delete(request):
    name = request.match_info["name"]
    job = request.app[PREVIEW_JOBS].get(name)
    if job is not None and not job.done():
        return web.json_response({"ok": False, "error": "Сначала дождитесь подготовки видео"}, status=409)
    try:
        await asyncio.to_thread(request.app[CAMERA].delete_recording, name)
    except FileNotFoundError:
        raise web.HTTPNotFound()
    except RuntimeError:
        return web.json_response({"ok": False, "error": "Сначала остановите запись"}, status=409)
    request.app[PREVIEW_JOBS].pop(name, None)
    return web.json_response({"ok": True})


async def camera_stream(request):
    camera = request.app[CAMERA]
    if not camera.status()["configured"]:
        raise web.HTTPServiceUnavailable(text="camera is not configured")
    response = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame",
        "Cache-Control": "no-store",
    })
    await response.prepare(request)
    last_id = -1
    try:
        while True:
            frame_id, frame = await asyncio.to_thread(camera.wait_frame, last_id)
            if frame_id is None:
                break
            if frame is None or frame_id == last_id:
                continue
            last_id = frame_id
            await response.write(
                b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                str(len(frame)).encode("ascii") + b"\r\n\r\n" + frame + b"\r\n")
    except (ConnectionError, asyncio.CancelledError):
        pass
    return response


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
                status = ctrl.status(cid)
                status["ai"] = request.app[AI_ADMIN].status()
                status["camera"] = request.app[CAMERA].status()
                status["nats"] = request.app[NATS_MONITOR].status()
                await ws.send_str(json.dumps(status))
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
    await asyncio.to_thread(app[AI_ADMIN].stop)
    await asyncio.to_thread(app[CAMERA].stop)
    for ws in list(app[SOCKETS]):
        await ws.close(code=1001, message=b"server shutdown")


@web.middleware
async def no_cache(request, handler):
    resp = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


async def nats_monitor_context(app):
    monitor = app[NATS_MONITOR]
    monitor.start()
    try:
        yield
    finally:
        await monitor.stop()


def create_app(ctrl, ai_admin, control_url, camera, nats_monitor):
    app = web.Application(middlewares=[no_cache])
    app[CTRL] = ctrl
    app[SOCKETS] = weakref.WeakSet()
    app[AI_ADMIN] = ai_admin
    app[CONTROL_URL] = control_url
    app[CAMERA] = camera
    app[PREVIEW_JOBS] = {}
    app[NATS_MONITOR] = nats_monitor
    app.cleanup_ctx.append(nats_monitor_context)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/arm/rc", api_arm_rc)
    app.router.add_post("/api/arm/web", api_arm_web)
    app.router.add_post("/api/stop", api_stop)
    app.router.add_post("/api/center", api_center)
    app.router.add_post("/api/web-control", api_web_control)
    app.router.add_post("/api/heartbeat", api_heartbeat)
    app.router.add_post("/api/ai/config", api_ai_config)
    app.router.add_post("/api/ai/start", api_ai_start)
    app.router.add_post("/api/ai/stop", api_ai_stop)
    app.router.add_post("/api/camera/record/start", api_camera_record_start)
    app.router.add_post("/api/camera/record/stop", api_camera_record_stop)
    app.router.add_get("/api/camera/focus", api_camera_focus)
    app.router.add_post("/api/camera/focus", api_camera_focus_set)
    app.router.add_get("/api/camera/recordings", api_camera_recordings)
    app.router.add_get("/api/camera/recordings/{name}/download", api_camera_recording_download)
    app.router.add_get("/api/camera/recordings/{name}/preview", api_camera_recording_preview)
    app.router.add_post("/api/camera/recordings/{name}/prepare", api_camera_recording_prepare)
    app.router.add_get("/api/camera/recordings/{name}/preview/status", api_camera_recording_preview_status)
    app.router.add_post("/api/camera/recordings/{name}/delete", api_camera_recording_delete)
    app.router.add_get("/api/camera/stream.mjpg", camera_stream)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static", BASE / "web" / "static")
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
    camera = None
    try:
        ai_admin = AiAdmin(os.environ.get("JETSON_AI_CONFIG", "config/ai.json"))
        camera_path = Path(os.environ.get("JETSON_CAMERA_CONFIG", "config/camera.json"))
        camera = CameraManager(CameraConfig(**json.loads(camera_path.read_text())) if camera_path.exists() else None,
                               os.environ.get("JETSON_RECORDINGS_DIR", "recordings"),
                               os.environ.get("JETSON_CAMERA_FOCUS_CONFIG", "config/focus.json"))
        camera.start()
        nats_monitor = NatsMonitor(os.environ.get("JETSON_NATS_URL", "nats://127.0.0.1:4222"))
        web.run_app(create_app(ctrl, ai_admin, "http://127.0.0.1:%d" % args.port, camera, nats_monitor), host=args.host, port=args.port, print=None,
                    handle_signals=True, shutdown_timeout=2.0)
    finally:
        if camera is not None:
            camera.stop()
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
