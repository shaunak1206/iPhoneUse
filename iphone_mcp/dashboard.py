"""iPhone Live: a local dashboard and HTTP API for the phone.

    iphone live                    start it and open http://127.0.0.1:8780
    python -m iphone_mcp.dashboard

The page shows the phone's screen live (WebDriverAgent's MJPEG stream, proxied
so it is same-origin), status, Connect / Stop, the activity of every agent
using the phone, and storage reports. The same endpoints are an API for other
apps:

    GET  /api/status           GET /api/screen          GET /api/activity?since=<t>
    POST /api/connect          POST /api/disconnect
    POST /api/do {"steps": [...]}    POST /api/goto {"place": "storage"}
    GET  /api/storage?deep=1   GET /stream (MJPEG)

It listens on 127.0.0.1 only. Requests that act need the header
`X-iPhone-Agent: 1`; a web page on another origin cannot send it without a
CORS preflight, which this server never approves, so a random site open in the
browser cannot drive the phone.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .bridge import ROOT, log

HOST = "127.0.0.1"
PORT = int(os.environ.get("IPHONE_DASHBOARD_PORT", "8780"))
URL = f"http://{HOST}:{PORT}"
PAGE = Path(__file__).parent / "assets" / "dashboard.html"


def app(phone=None):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
    from starlette.routing import Route

    from . import journal, screen, storage
    from .phone import ACTIVITY_FILE, Phone, Refused

    phone = phone or Phone()
    cache: dict[str, tuple[float, dict]] = {}

    def allowed(req: Request, acting: bool) -> Response | None:
        host = (req.headers.get("host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "local only"}, 403)  # DNS rebinding
        if acting and req.headers.get("x-iphone-agent") != "1":
            return JSONResponse({"error": "missing X-iPhone-Agent header"}, 403)
        return None

    async def run(req: Request, coro_fn, acting: bool = True) -> Response:
        if (bad := allowed(req, acting)):
            return bad
        try:
            return JSONResponse(await coro_fn())
        except Refused as e:
            return JSONResponse({"refused": str(e)}, 409)
        except Exception as e:
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, 400)

    async def index(req: Request) -> Response:
        if (bad := allowed(req, False)):
            return bad
        html = PAGE.read_text().replace("__JOURNAL__", f"https://github.com/{journal.REPO}")
        return HTMLResponse(html)

    async def ping(req: Request) -> Response:
        return JSONResponse({"ok": True, "pid": os.getpid()})

    async def status(req: Request) -> Response:
        return await run(req, phone.status, acting=False)

    async def screen_(req: Request) -> Response:
        async def go():
            s = await phone.read()
            return {"text": screen.describe(s), "items": [
                {"text": i.text, "x": round(i.cx), "y": round(i.cy), "kind": i.kind} for i in s.items]}
        return await run(req, go, acting=False)

    async def activity(req: Request) -> Response:
        if (bad := allowed(req, False)):
            return bad
        since = float(req.query_params.get("since", 0))
        out = []
        if ACTIVITY_FILE.exists():
            for line in ACTIVITY_FILE.read_text(errors="replace").splitlines()[-300:]:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev["t"] > since:
                    out.append(ev)
        return JSONResponse(out[-100:])

    async def connect(req: Request) -> Response:
        async def go():
            return {"result": await phone.connect()}
        return await run(req, go)

    async def disconnect(req: Request) -> Response:
        async def go():
            stopped = await phone.disconnect()
            journal.background(journal.flush, "session")
            return {"stopped": stopped}
        return await run(req, go)

    async def do(req: Request) -> Response:
        body = await req.json()

        async def go():
            return {"result": await phone.do(body.get("steps") or [])}
        return await run(req, go)

    async def goto(req: Request) -> Response:
        body = await req.json()

        async def go():
            return {"result": await phone.after(await phone.goto(body.get("place", "")))}
        return await run(req, go)

    async def storage_(req: Request) -> Response:
        deep = req.query_params.get("deep") == "1"

        async def go():
            key = f"deep={deep}"
            if key in cache and time.time() - cache[key][0] < 600 and req.query_params.get("fresh") != "1":
                return cache[key][1]
            r = await phone.storage_report(deep=deep)
            text = storage.format_report(r)
            journal.background(journal.save_report, r, text)
            out = {"text": text, "free": r["free"], "capacity": r["capacity"],
                   "apps": r["apps"][:25], "suggestions": r["suggestions"], "media": r.get("media")}
            cache[key] = (time.time(), out)
            return out
        return await run(req, go, acting=False)

    async def stream(req: Request) -> Response:
        if (bad := allowed(req, False)):
            return bad
        src = phone.wda.stream_url
        if not src:
            return Response("WebDriverAgent is not running", 503)
        client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=3))
        try:
            upstream = await client.send(client.build_request("GET", src), stream=True)
        except httpx.HTTPError:
            await client.aclose()
            return Response("stream unreachable", 503)

        async def body():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
                await client.aclose()
        return StreamingResponse(body(), media_type=upstream.headers.get("content-type", "multipart/x-mixed-replace"))

    routes = [
        Route("/", index), Route("/api/ping", ping), Route("/api/status", status), Route("/api/screen", screen_),
        Route("/api/activity", activity), Route("/api/storage", storage_), Route("/stream", stream),
        Route("/api/connect", connect, methods=["POST"]), Route("/api/disconnect", disconnect, methods=["POST"]),
        Route("/api/do", do, methods=["POST"]), Route("/api/goto", goto, methods=["POST"]),
    ]
    return Starlette(routes=routes)


def running() -> bool:
    try:
        return httpx.get(f"{URL}/api/ping", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_running(wait: float = 10) -> str:
    """Start the dashboard as its own process (it outlives the caller) unless it is up."""
    if not running():
        logf = ROOT / "build" / "dashboard.log"
        logf.parent.mkdir(exist_ok=True)
        subprocess.Popen([sys.executable, "-m", "iphone_mcp.dashboard"], cwd=ROOT, stdout=open(logf, "a"),
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + wait
        while time.time() < end and not running():
            time.sleep(0.3)
    return URL


def main() -> None:
    import uvicorn
    log(f"iPhone Live on {URL}")
    uvicorn.run(app(), host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
