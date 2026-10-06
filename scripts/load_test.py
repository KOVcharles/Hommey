"""Small HTTP load tests for the Spring/Python stack; uses existing httpx.

Run --help. Reports contain timings and status codes, never tokens or replies.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import getpass
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import uuid

import httpx


@dataclass
class Sample:
    ok: bool = False
    status: int | None = None
    error: str | None = None
    elapsed_ms: float = 0
    first_text_ms: float | None = None
    first_content_ms: float | None = None
    dispatch_lag_ms: float = 0
    outcome: str | None = None
    # Relative to the start of a measured stage, for throughput inside its window.
    finished_s: float = 0


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None, "p99": None}
    result = {"n": len(ordered)}
    for name, fraction in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99)):
        result[name] = round(ordered[math.ceil(len(ordered) * fraction) - 1], 2)
    return result


def summarize(samples, elapsed, dropped=0):
    good = [sample for sample in samples if sample.ok]
    return {
        "attempted": len(samples),
        "successful": len(good),
        "unsuccessful": len(samples) - len(good),
        "dropped_before_dispatch": dropped,
        "elapsed_s": round(elapsed, 3),
        "successful_per_s_including_drain": round(len(good) / elapsed, 3),
        "unsuccessful_rate": (
            (len(samples) - len(good)) / len(samples) if samples else None
        ),
        "latency_all_ms": percentiles([s.elapsed_ms for s in samples]),
        "latency_successful_ms": percentiles([s.elapsed_ms for s in good]),
        "first_text_successful_ms": percentiles(
            [s.first_text_ms for s in good if s.first_text_ms is not None]
        ),
        "first_content_successful_ms": percentiles(
            [s.first_content_ms for s in good if s.first_content_ms is not None]
        ),
        "dispatch_lag_ms": percentiles([s.dispatch_lag_ms for s in samples]),
        "http_status_counts": dict(Counter(str(s.status) for s in samples)),
        "unsuccessful_codes": dict(Counter(s.error for s in samples if not s.ok)),
        "outcomes": dict(Counter(s.outcome or "unspecified" for s in samples)),
    }


async def api_request(client, path, target, timeout, scheduled=None, origin=None):
    started = time.perf_counter()
    sample = Sample(
        dispatch_lag_ms=(
            max(0, started - scheduled) * 1000 if scheduled is not None else 0
        )
    )
    try:
        async with asyncio.timeout(timeout):
            response = await client.get(path)
            sample.status = response.status_code
            if response.status_code != 200:
                sample.error = f"HTTP_{response.status_code}"
            else:
                payload = response.json()
                valid = isinstance(payload, dict) and (
                    isinstance(payload.get("sessions"), list)
                    if target == "sessions"
                    else "profile" in payload
                    and isinstance(payload.get("revision"), int)
                )
                sample.ok = (
                    valid
                    and not payload.get("error")
                    and payload.get("success") is not False
                )
                if not sample.ok:
                    sample.error = "INVALID_API_RESPONSE"
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        sample.error = type(exc).__name__
    sample.elapsed_ms = (time.perf_counter() - started) * 1000
    sample.finished_s = time.perf_counter() - origin if origin is not None else 0
    return sample


async def run_arrivals(client, path, target, qps, duration, max_inflight, timeout):
    """Fixed arrival schedule. Skip missed/full slots rather than build a queue."""
    started = time.perf_counter()
    active, samples = set(), []
    dropped = 0
    slots = math.ceil(qps * duration)
    for index in range(slots):
        scheduled = started + index / qps
        await asyncio.sleep(max(0, scheduled - time.perf_counter()))
        done = {task for task in active if task.done()}
        samples.extend(task.result() for task in done)
        active.difference_update(done)
        if time.perf_counter() - scheduled >= 1 / qps or len(active) >= max_inflight:
            dropped += 1
            continue
        active.add(
            asyncio.create_task(
                api_request(client, path, target, timeout, scheduled, started)
            )
        )
    await asyncio.sleep(max(0, started + duration - time.perf_counter()))
    if active:
        samples.extend(await asyncio.gather(*active))
    elapsed = time.perf_counter() - started
    result = summarize(samples, elapsed, dropped)
    result.update(
        {
            "target_qps": qps,
            "window_s": duration,
            "scheduled": slots,
            "dispatched_per_s": round(len(samples) / duration, 3),
            "successful_completed_in_window_per_s": round(
                sum(s.ok and s.finished_s <= duration for s in samples) / duration, 3
            ),
            "samples": [asdict(sample) for sample in samples],
        }
    )
    return result


async def chat_request(client, user, session, message, timeout):
    started = time.perf_counter()
    sample = Sample()
    done = content = False
    interrupted = False
    request_id = "load_" + uuid.uuid4().hex
    try:
        async with asyncio.timeout(timeout):
            async with client.stream(
                "POST",
                f"/api/{user}/chat/stream",
                headers={"X-Request-ID": request_id},
                json={
                    "message": message,
                    "session_id": session,
                    "client_request_id": request_id,
                },
            ) as response:
                sample.status = response.status_code
                if response.status_code != 200:
                    sample.error = f"HTTP_{response.status_code}"
                else:
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            sample.error = "INVALID_STREAM_EVENT"
                            break
                        kind = event.get("type")
                        if kind == "error":
                            sample.error = str(event.get("code") or "STREAM_ERROR")[:80]
                            break
                        if kind == "interrupted":
                            interrupted = True
                        if kind == "response_reset":
                            content = False
                        if (
                            kind == "chunk"
                            and isinstance(event.get("text"), str)
                            and event["text"].strip()
                        ):
                            content = True
                            arrived_ms = (time.perf_counter() - started) * 1000
                            if sample.first_text_ms is None:
                                sample.first_text_ms = arrived_ms
                            if sample.first_content_ms is None:
                                sample.first_content_ms = arrived_ms
                        if kind in {
                            "answer_document",
                            "presentation_document",
                        } and event.get("document"):
                            content = True
                            if sample.first_content_ms is None:
                                sample.first_content_ms = (
                                    time.perf_counter() - started
                                ) * 1000
                        if kind == "done":
                            done = True
                            sample.outcome = event.get("outcome")
                            interrupted = interrupted or bool(event.get("interrupted"))
            if sample.error is None:
                if interrupted:
                    sample.error = "INTERRUPTED"
                elif not done:
                    sample.error = "MISSING_DONE"
                elif not content:
                    sample.error = "EMPTY_RESPONSE"
                elif sample.outcome != "completed":
                    sample.error = (
                        "OUTCOME_" + str(sample.outcome or "UNSPECIFIED").upper()
                    )
                else:
                    sample.ok = True
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        sample.error = type(exc).__name__
    sample.elapsed_ms = (time.perf_counter() - started) * 1000
    return sample


async def run_chat(client, user, concurrency, rounds, message, timeout):
    # Create all sessions BEFORE measurement; each measured turn has a fresh one.
    sessions = []
    for _ in range(concurrency * rounds):
        response = await client.post(f"/api/{user}/sessions")
        if response.status_code != 200:
            raise RuntimeError(f"创建压测会话失败：HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("session_id"):
            raise RuntimeError("创建会话未返回 session_id")
        sessions.append(payload["session_id"])
    samples = []
    started = time.perf_counter()

    async def worker(index):
        for offset in range(rounds):
            sample = await chat_request(
                client, user, sessions[index * rounds + offset], message, timeout
            )
            samples.append(sample)
            print(
                f"已完成 {len(samples)}/{len(sessions)}：{'成功' if sample.ok else sample.error}，{sample.elapsed_ms / 1000:.1f}s",
                flush=True,
            )

    await asyncio.gather(*(worker(index) for index in range(concurrency)))
    elapsed = time.perf_counter() - started
    result = summarize(samples, elapsed)
    result.update(
        {
            "concurrency": concurrency,
            "rounds_per_worker": rounds,
            "successful_turns_per_min": round(
                sum(s.ok for s in samples) / elapsed * 60, 3
            ),
            "samples": [asdict(sample) for sample in samples],
            "note": "闭环短样本：会话创建不计时；吞吐含冷启动和最后一批收尾，不能视为持续容量。",
        }
    )
    return result


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def parser():
    command = argparse.ArgumentParser(
        description="Hommey 小型压测：普通 API 固定到达速率 / AI 有限并发"
    )
    command.add_argument("--base-url", default="http://localhost:8088")
    command.add_argument(
        "--email", help="已有测试账号；密码通过终端隐藏输入，也可使用 HOMMEY_LOAD_TOKEN"
    )
    command.add_argument(
        "--output", type=Path, help="报告 JSON 路径；默认 benchmark-results/load-*.json"
    )
    modes = command.add_subparsers(dest="mode", required=True)
    api = modes.add_parser("api", help="只读业务接口；先从 10 QPS 开始")
    api.add_argument("--target", choices=("sessions", "profile"), default="sessions")
    api.add_argument("--qps", type=positive_int, default=10)
    api.add_argument("--duration", type=positive_int, default=60)
    api.add_argument("--warmup", type=positive_int, default=5)
    api.add_argument("--max-inflight", type=positive_int, default=100)
    api.add_argument("--timeout", type=positive_int, default=10)
    chat = modes.add_parser("chat", help="调用真实 AI；每个请求创建独立会话并保留历史")
    chat.add_argument("--concurrency", type=positive_int, default=1)
    chat.add_argument(
        "--rounds", type=positive_int, default=2, help="每个并发 worker 发送的问题数"
    )
    chat.add_argument("--timeout", type=positive_int, default=180)
    chat.add_argument(
        "--message",
        default="请根据重庆大学财务报销指南，说明因公出差交通费报销需要哪些材料，并给出制度来源。",
    )
    return command


async def main(args):
    url = urlsplit(args.base_url)
    if (
        url.scheme not in {"http", "https"}
        or not url.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise RuntimeError(
            "base-url 应为 http(s)://主机:端口，不含凭据、路径或查询参数"
        )
    connections = args.max_inflight if args.mode == "api" else args.concurrency
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(args.timeout, connect=5, pool=5),
        limits=httpx.Limits(
            max_connections=max(2, connections),
            max_keepalive_connections=max(2, connections),
        ),
    ) as client:
        if args.email:
            password = getpass.getpass("测试账号密码（隐藏输入）：")
            response = await client.post(
                "/auth/login", json={"email": args.email, "password": password}
            )
            del password
            if response.status_code != 200:
                raise RuntimeError(f"登录失败：HTTP {response.status_code}")
            token = response.json().get("access_token")
        else:
            token = os.getenv("HOMMEY_LOAD_TOKEN")
        if not token:
            raise RuntimeError("请传入 --email，或设置 HOMMEY_LOAD_TOKEN")
        client.headers["Authorization"] = f"Bearer {token}"
        me = await client.get("/api/me")
        if me.status_code != 200:
            raise RuntimeError(f"获取账号失败：HTTP {me.status_code}")
        user = str(me.json()["id"])
        if args.mode == "api":
            path = f"/api/{user}/{args.target}"
            smoke = await api_request(client, path, args.target, args.timeout)
            if not smoke.ok:
                raise RuntimeError(f"接口预检查失败：{smoke.error}")
            print(
                f"预热 {args.warmup}s；随后 {args.qps} QPS × {args.duration}s，最大在途 {args.max_inflight}",
                flush=True,
            )
            await run_arrivals(
                client,
                path,
                args.target,
                args.qps,
                args.warmup,
                args.max_inflight,
                args.timeout,
            )
            report = await run_arrivals(
                client,
                path,
                args.target,
                args.qps,
                args.duration,
                args.max_inflight,
                args.timeout,
            )
            report["target"] = args.target
        else:
            print(
                f"真实 AI 测试：{args.concurrency} 并发 × 每路 {args.rounds} 轮 = {args.concurrency * args.rounds} 个请求；会创建并保留测试会话。",
                flush=True,
            )
            report = await run_chat(
                client, user, args.concurrency, args.rounds, args.message, args.timeout
            )
    report.update(
        {
            "mode": args.mode,
            "base_url": args.base_url,
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "request_timeout_s": args.timeout,
        }
    )
    output = args.output or Path(__file__).resolve().parents[
        1
    ] / "benchmark-results" / (
        "load-"
        + args.mode
        + "-"
        + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        + ".json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {key: value for key, value in report.items() if key != "samples"},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"报告：{output.resolve()}")
    return 1 if report["unsuccessful"] or report["dropped_before_dispatch"] else 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main(parser().parse_args())))
    except KeyboardInterrupt:
        print("压测已停止。")
        raise SystemExit(130)
    except (RuntimeError, ValueError, KeyError, httpx.HTTPError) as exc:
        # Never dump response bodies, headers, login credentials or a traceback.
        print(
            f"无法开始/完成压测：{str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__}"
        )
        raise SystemExit(2)
