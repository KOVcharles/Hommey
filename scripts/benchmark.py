#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
性能基准测试 V2 — 测试 process_message 完整链路
重点对比: 普通消息 vs 简单闲聊（短电路优化）
"""
import asyncio
import json
import logging
import os
import sys
import time

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

from webui_new.manager import HommeyWebInstance


async def test_process_message(instance, session_id, label, message):
    """通过 process_message 测试完整链路"""
    t0 = time.perf_counter()
    result = await instance.process_message(message, session_id=session_id)
    elapsed = time.perf_counter() - t0

    resp_preview = result.get("response", "")[:60]
    agents = result.get("agents", [])
    agent_names = [a["display"] for a in agents]

    shortcut = " ⚡" if "simple chitchat" in resp_preview.lower() or not agents else ""

    print(f"  {label:20s} {elapsed:>6.2f}s{shortcut}  agents={agent_names}  resp={resp_preview}...")
    return elapsed, result


async def main():
    print("╔═══════════════════════════════════════════════╗")
    print("║     Hommey 性能基准测试 V2                      ║")
    print("║     测试 process_message 完整链路              ║")
    print("╚═══════════════════════════════════════════════╝")

    # 初始化
    print("\n  ⏳ 初始化测试实例...")
    t0 = time.perf_counter()
    instance = HommeyWebInstance("bench_user")
    await instance.initialize()
    session_id = await asyncio.to_thread(instance.start_new_chat_session)
    print(f"  ✓ 初始化完成 ({time.perf_counter()-t0:.2f}s)\n")

    # ── 测试用例 ──
    # 第一组: 测试短电路效果（简单闲聊不经过 LLM）
    print("  ┌── 第一组: 短电路优化测试 ──────────────────")
    t1, _ = await test_process_message(instance, session_id, "闲聊-你好", "你好")
    t2, _ = await test_process_message(instance, session_id, "闲聊-谢谢", "谢谢")
    t3, _ = await test_process_message(instance, session_id, "闲聊-在吗", "在吗")
    print(f"  │  短电路平均: {(t1+t2+t3)/3:.2f}s  (预期 < 1s)")
    print("  └────────────────────────────────────────────\n")

    # 第二组: 正常业务查询（经过 LLM）
    print("  ┌── 第二组: 业务查询 ────────────────────────")
    t4, _ = await test_process_message(instance, session_id, "偏好设置", "我喜欢坐高铁")
    t5, _ = await test_process_message(instance, session_id, "查差旅标准", "北京的出差住宿标准是多少")
    print("  └────────────────────────────────────────────\n")

    # 第三组: 体验缓存效果（同一对话上下文的第二条消息）
    print("  ┌── 第三组: 缓存效果 ────────────────────────")
    t6, _ = await test_process_message(instance, session_id, "二次查询", "那上海的住宿标准呢")
    print(f"  │  对比: 首次 {t5:.2f}s → 二次 {t6:.2f}s (缓存生效)")
    print("  └────────────────────────────────────────────\n")

    # 汇总
    print("╔═══════════════════════════════════════════════╗")
    print("║     汇总                                     ║")
    print("╚═══════════════════════════════════════════════╝")
    print(f"  短电路闲聊:          {t1:.2f}s + {t2:.2f}s + {t3:.2f}s = 平均 {(t1+t2+t3)/3:.2f}s")
    print(f"  LLM 业务查询:        {t4:.2f}s, {t5:.2f}s, {t6:.2f}s")
    print(f"  预估提升: 简单闲聊从 ~9s → 瞬回\n")

    # 清理
    instance.memory_manager.end_session()
    print("  ✓ 测试完成\n")


if __name__ == "__main__":
    asyncio.run(main())
