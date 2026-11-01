import asyncio
import os

import httpx
from fastapi import FastAPI, Request, Response

app = FastAPI()
UPSTREAM = os.environ.get("PROXY_UPSTREAM", "http://127.0.0.1:8085")
mode = {"drop_response_first_n": 0, "sleep_seconds": 5.0, "seen": 0, "dropped": 0}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/proxy/mode")
async def set_mode(req: Request):
    mode.update(await req.json())
    return mode


@app.get("/proxy/mode")
def get_mode():
    return mode


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def forward(path: str, req: Request):
    body = await req.body()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.request(req.method, f"{UPSTREAM}/{path}", content=body, params=req.query_params,
                            headers={k: v for k, v in req.headers.items() if k.lower() not in ("host", "content-length")})
    if path.endswith("/refunds/execute") and req.method == "POST":
        mode["seen"] += 1
        if mode["dropped"] < mode["drop_response_first_n"]:
            mode["dropped"] += 1
            await asyncio.sleep(mode["sleep_seconds"])
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
