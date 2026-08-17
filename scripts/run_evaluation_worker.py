#!/usr/bin/env python
"""Run the independent Turn evaluation worker."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agents.lazy_agent_registry import LazyAgentRegistry
from config_agentscope import init_agentscope
from evaluation.repository import EvaluationRepository
from evaluation.reconciler import TurnEvaluationReconciler
from evaluation.worker import TurnEvaluationWorker
from settings import EVALUATION_CONFIG


def _judge_agent():
    if not EVALUATION_CONFIG.get("llm_enabled", False):
        return None
    model_name = str(EVALUATION_CONFIG.get("judge_model") or "").strip()
    if not model_name:
        raise RuntimeError("HOMMEY_EVALUATION_JUDGE_MODEL is required when Judge LLM is enabled")

    init_agentscope()
    from agentscope.model import OpenAIChatModel

    model = OpenAIChatModel(
        model_name=model_name,
        api_key=EVALUATION_CONFIG.get("judge_api_key") or "",
        client_kwargs={
            "base_url": EVALUATION_CONFIG.get("judge_base_url"),
            "timeout": float(EVALUATION_CONFIG.get("judge_timeout_sec", 30.0)),
        },
        generate_kwargs={"temperature": 0.0, "max_tokens": 3000},
    )
    registry = LazyAgentRegistry(
        model=model,
        cache={},
        memory_manager=None,
        mcp_manager=None,
    )
    return registry["turn_evaluator"]


async def _main(once: bool, reconcile: bool) -> None:
    repository = EvaluationRepository()
    if not repository.configured:
        raise RuntimeError("HOMMEY_POSTGRES_DSN is required for evaluation")
    worker = TurnEvaluationWorker(
        repository,
        judge_agent=_judge_agent(),
        concurrency=int(EVALUATION_CONFIG.get("worker_concurrency", 2)),
        lease_seconds=int(EVALUATION_CONFIG.get("lease_seconds", 90)),
        sample_rate=float(EVALUATION_CONFIG.get("sample_rate", 0.2)),
        judge_enabled=bool(EVALUATION_CONFIG.get("llm_enabled", False)),
    )
    try:
        if reconcile:
            reconciler = TurnEvaluationReconciler(
                repository,
                evaluator_version=str(EVALUATION_CONFIG.get("evaluator_version", "turn-evaluator-v1")),
                judge_model=str(EVALUATION_CONFIG.get("judge_model", "")),
                judge_prompt_version=str(EVALUATION_CONFIG.get("judge_prompt_version", "turn-judge-v1")),
                rubric_version=str(EVALUATION_CONFIG.get("rubric_version", "travel-rubric-v1")),
            )
            await reconciler.run_once()
        if once:
            await worker.run_once()
        else:
            await worker.run_forever()
    finally:
        repository.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Claim one batch and exit")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="Backfill missing persisted Turns once before evaluating",
    )
    args = parser.parse_args()
    asyncio.run(_main(args.once, args.reconcile))
