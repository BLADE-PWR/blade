"""NATS helpers. Bus messages are JSON Envelopes; on binary subjects (frames.<sid>) the envelope is
the `Blade-Env` header and the body is the blob."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time

import nats
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from .contracts import Envelope

ENV_HEADER = "Blade-Env"


def now() -> float:
    return time.time()


async def connect(name: str, url: str | None = None) -> NATS:
    url = url or os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
    # connect_timeout: at startup every service connects at once and NATS can take a few seconds to answer
    return await nats.connect(url, name=name, connect_timeout=10, max_reconnect_attempts=-1, reconnect_time_wait=1.0)


async def close(nc: NATS) -> None:
    try:
        await asyncio.wait_for(nc.drain(), 3)
    except Exception:
        with contextlib.suppress(Exception):
            await nc.close()


async def publish(nc: NATS, subject: str, env: Envelope, blob: bytes | None = None) -> None:
    text = env.model_dump_json(exclude_none=True)
    if blob is None:
        await nc.publish(subject, text.encode())
    else:
        await nc.publish(subject, blob, headers={ENV_HEADER: text})


def decode(msg: Msg) -> tuple[Envelope, bytes | None]:
    if msg.headers and ENV_HEADER in msg.headers:
        return Envelope.model_validate_json(msg.headers[ENV_HEADER]), msg.data
    return Envelope.model_validate_json(msg.data), None


def stamp(env: Envelope, stage: str, t_in: float, t_out: float | None = None) -> Envelope:
    env.ts[stage] = [round(t_in, 6), round(t_out if t_out is not None else now(), 6)]
    return env


def forward(src: Envelope, data: dict, stage: str, t_in: float) -> Envelope:
    """New envelope for the next subject: same key and timings, new payload, this stage stamped."""
    env = Envelope(sid=src.sid, frame=src.frame, wid=src.wid, ts=dict(src.ts), data=data)
    return stamp(env, stage, t_in)


def jdump(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


async def kv_bucket(js, name: str, ttl: float | None = None):
    try:
        return await js.key_value(name)
    except Exception:
        return await js.create_key_value(bucket=name, history=1, **({"ttl": ttl} if ttl else {}))


async def kv_read(kv, prefix: str = "") -> dict[str, dict]:
    """Every JSON entry of a KV bucket (whose key starts with prefix)."""
    out = {}
    try:
        keys = await kv.keys()
    except Exception:  # raised when the bucket is empty
        return out
    for k in keys:
        if k.startswith(prefix):
            with contextlib.suppress(Exception):
                out[k] = json.loads((await kv.get(k)).value)
    return out
