"""Evidence-only Judge Agent for a single frozen Turn."""
from __future__ import annotations

import json
from typing import Any, Optional, Union, List

from agentscope.agent import AgentBase
from agentscope.message import Msg

from core.llm_response import extract_text_from_response
from core.intent_result import parse_json_object
from evaluation.models import JudgeResult, TurnEvaluationFacts, TurnEvaluationMetadata


class TurnEvaluatorAgent(AgentBase):
    def __init__(self, name: str = "turn_evaluator", model=None, **kwargs):
        super().__init__()
        self.name = name
        self.model = model

    async def reply(self, x: Optional[Union[Msg, List[Msg]]] = None) -> Msg:
        metadata, facts = self._validate_input(x)
        if (
            metadata.metadata_truncated
            or metadata.evaluation_context_quality == "reduced"
            or facts.metadata_completeness < 0.8
        ):
            return self._message({
                "schema_version": "eval.turn.result.1",
                "verdict": "unscored",
                "score": None,
                "dimensions": self._empty_dimensions(),
                "reason_codes": ["INSUFFICIENT_EVALUATION_CONTEXT"],
                "critical_errors": [],
                "findings": [{
                    "code": "INSUFFICIENT_EVALUATION_CONTEXT",
                    "severity": "medium",
                    "message": "快照发生关键截断或缺少评测所需字段。",
                    "subject_ref": "metadata_truncated",
                    "evidence_refs": [],
                }],
                "review_required": True,
                "summary": "评测上下文不足，未进行语义评分。",
            })
        if self.model is None:
            raise RuntimeError("Turn evaluator model is not configured")

        prompt = self._prompt(metadata, facts)
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                response = await self.model([
                    {
                        "role": "system",
                        "content": (
                            "你是企业差旅助手的独立质量评测器。只使用用户提供的冻结快照，"
                            "不得把自身知识当作公司制度，不调用工具，不输出思维过程。只输出 JSON。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ])
                raw = parse_json_object(await extract_text_from_response(response))
                validated = JudgeResult.model_validate(raw)
                return self._message(validated.model_dump(mode="json"))
            except Exception as exc:
                last_error = exc
        raise ValueError("Judge returned invalid structured output") from last_error

    def _validate_input(self, x) -> tuple[TurnEvaluationMetadata, TurnEvaluationFacts]:
        raw: Any = x[-1].content if isinstance(x, list) and x else getattr(x, "content", x)
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError("evaluation input must be an object")
        return (
            TurnEvaluationMetadata.model_validate(raw.get("metadata")),
            TurnEvaluationFacts.model_validate(raw.get("facts")),
        )

    @staticmethod
    def _prompt(metadata: TurnEvaluationMetadata, facts: TurnEvaluationFacts) -> str:
        return f"""请评价这一轮企业差旅助手对话。

生命周期规则：completed 评价完成度；waiting_user 评价追问是否必要且不重复；degraded 评价降级是否透明。
五项维度各 0-4 分：understanding、task_progress、groundedness、safety、clarity。
政策/合规正确性只能依据 metadata.evidence 和 metadata.answer.sources；证据不足时用 unscored，不能猜。
每个 finding 必须给出 subject_ref，涉及证据时给出 evidence_refs。

【确定性事实】
{json.dumps(facts.model_dump(mode='json'), ensure_ascii=False, indent=2)}

【冻结快照】
{json.dumps(metadata.model_dump(mode='json'), ensure_ascii=False, indent=2)}

严格输出 eval.turn.result.1 JSON。"""

    @staticmethod
    def _empty_dimensions() -> dict[str, int]:
        return {
            "understanding": 0,
            "task_progress": 0,
            "groundedness": 0,
            "safety": 0,
            "clarity": 0,
        }

    def _message(self, result: dict[str, Any]) -> Msg:
        validated = JudgeResult.model_validate(result)
        return Msg(
            name=self.name,
            content=json.dumps(validated.model_dump(mode="json"), ensure_ascii=False),
            role="assistant",
        )
