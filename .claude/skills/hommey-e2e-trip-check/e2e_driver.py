"""Hommey 完整差旅链路 E2E 驱动（在 app 容器内运行）。

用法（宿主机）：
    docker exec -i hommey-app python - < .claude/skills/hommey-e2e-trip-check/e2e_driver.py

流程：建测试用户 → 签 access token → 建会话 → 三连轮：
  T1 我要去南京出差（预期 intake 表单 waiting_input）
  T2 补全行程信息（预期 行程/制度/天气交通(12306车次)/记忆/规划 五段）
  T3 合规检查（预期 compliance succeeded）

只打测试用户自己的数据，不动其他会话；输出 JSON 摘要供完善度评估。
"""
import asyncio
import json
import re
import time
import uuid

import httpx

from webui_new.auth.security import create_access_token
from webui_new.auth.storage import create_user, get_conn
from context.memory_repository import stable_uuid

BASE = "http://127.0.0.1:8000"
MESSAGES = [
    "我要去南京出差",
    "从北京出发，2026-09-13出发，出差2天，出差目的：参加客户会议",
    "帮我检查一下这个行程是否符合制度要求",
]

_TRAIN_CODE = re.compile(r"\b[GCDZKT]\d{1,4}\b")
_AMOUNT = re.compile(r"\d+元")
_TIMEOUT_MARK = re.compile(r"达到时间上限|TURN_TIMEOUT|预算已用完|时间上限")


def fetch_answer(user_id: str, sid: str) -> dict:
    """从 supervisor_runs 提取该轮权威 answer_document（流事件里不含完整答案）。"""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT response FROM supervisor_runs WHERE user_id=%s AND session_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, stable_uuid(sid, namespace="session")),
        )
        row = cur.fetchone()
    if not row:
        return {}
    response = row["response"]
    if not isinstance(response, dict):
        response = json.loads(response)
    return (response or {}).get("answer_document") or {}


def assess(answer: dict) -> dict:
    """从 answer_document 提取完善度信号。"""
    sections = (answer or {}).get("sections") or []
    out = {"sections": [], "has_trains": False, "train_samples": [], "has_itinerary": False,
           "policy_items": 0, "timeout_marks": 0}
    for s in sections:
        title, kind, status = s.get("title", ""), s.get("kind", ""), s.get("status", "")
        text = json.dumps(s, ensure_ascii=False)
        out["sections"].append({"title": title, "kind": kind, "status": status,
                                "items": len(s.get("items", [])), "days": len(s.get("days", []))})
        codes = sorted(set(_TRAIN_CODE.findall(text)))
        if codes:
            out["has_trains"] = True
            out["train_samples"] = codes[:8]
        if kind == "trip" and (s.get("days") or s.get("items")) and title not in ("行程信息整理",):
            out["has_itinerary"] = True
        if kind == "train":
            out["has_train_section"] = True
            out["train_rows"] = out.get("train_rows", 0) + len(s.get("items", []))
        if kind == "policy":
            out["policy_items"] += len(s.get("items", []))
            if title != "合规检查":
                out["policy_amounts"] = out.get("policy_amounts", 0) + len(_AMOUNT.findall(text))
        out["timeout_marks"] += len(_TIMEOUT_MARK.findall(text))
    return out


async def run_turn(client, token, user_id, sid, message, label):
    t0 = time.monotonic()
    steps, tasks, done_ev, answer_ev = [], [], None, None
    async with client.stream(
        "POST", f"{BASE}/api/{user_id}/chat/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": message, "session_id": sid},
    ) as r:
        assert r.status_code == 200, f"HTTP {r.status_code}"
        async for line in r.aiter_lines():
            if not line.strip():
                continue
            ev = json.loads(line)
            kind = ev.get("type")
            if kind == "execution_plan":
                steps = [(s["title"], s["status"], s.get("summary", "")) for s in ev.get("steps", [])]
            elif kind == "task_status":
                tasks.append((ev.get("intent"), ev.get("phase"), round(time.monotonic() - t0, 1)))
            elif kind == "done":
                done_ev = ev
            elif kind == "presentation_document":
                answer_ev = ev.get("document")
    elapsed = round(time.monotonic() - t0, 1)
    report = {"label": label, "elapsed_s": elapsed, "outcome": (done_ev or {}).get("outcome"),
              "stop_reason": (done_ev or {}).get("stop_reason"), "steps": steps, "tasks": tasks}
    answer = answer_ev or fetch_answer(user_id, sid)
    if answer:
        report["assess"] = assess(answer)
    print(json.dumps(report, ensure_ascii=False))
    return report


async def main():
    with get_conn() as conn:
        email = f"e2e-{uuid.uuid4().hex[:8]}@hommey.local"
        user = create_user(conn, email, "$2b$12$e2echeckplaceholderhashnotused")
        conn.commit()
    user_id = str(user.id)
    token = create_access_token(user.id)
    print(json.dumps({"user_id": user_id, "email": email}, ensure_ascii=False))

    async with httpx.AsyncClient(timeout=300) as client:
        r = await client.post(f"{BASE}/api/{user_id}/sessions", headers={"Authorization": f"Bearer {token}"})
        sid = r.json()["session_id"]
        print(json.dumps({"session_id": sid}, ensure_ascii=False))
        for i, message in enumerate(MESSAGES, 1):
            await run_turn(client, token, user_id, sid, message, f"T{i}")


if __name__ == "__main__":
    asyncio.run(main())
