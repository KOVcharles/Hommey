"""
车次查询智能体 - 真实 12306 数据版（免费、无 Key）

职责边界：
- 只做与当前/明确公司差旅相关的车次、时刻、历时、余票查询（结构化为行）。
- 不预订、不抢票、不核验购票资格；不做制度/报销/RAG 问答；不做通用搜索。
- 每次外部调用受 core.execution_budget 的 per-type 上限约束（默认 6 次/批）。

返回契约（reply 的 Msg.content 为 JSON）：
- 成功：{"query_type": "高铁/火车车次查询", "query_success": true,
        "results": {"trains": [{train_no, from_station, to_station, depart_time,
                                arrive_time, duration, seats, prices}],
                    "summary", "note"}}
- 失败：{"query_type": "高铁/火车车次查询", "query_success": false,
        "results": {"message", "note"}}  → result_rules.error_when_field 把它
        标为 error 节点，编排 on_failure: continue 继续。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple, Union

from agentscope.agent import AgentBase
from agentscope.message import Msg

# 项目根（core/settings 等）与本脚本目录（兄弟模块 train_backend）。
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.abspath(os.path.join(_SCRIPT_DIR, "../../../..")))
sys.path.append(_SCRIPT_DIR)

from core.execution_budget import ExecutionLimitExceeded  # noqa: E402
from train_backend import TrainQueryError, create_train_query_backend  # noqa: E402

logger = logging.getLogger(__name__)

_VERIFICATION_NOTE = (
    "车次、时刻与余票来自铁路12306公开接口，仅供行程参考，"
    "请通过12306官方App或授权差旅渠道实时核验。"
)
_PROMPT_COMPLETE_MSG = (
    "请告诉我出发城市、到达城市和出行日期，"
    "例如「帮我查一下明天上海到北京的高铁车次」。"
)

_ROUTE_RE = re.compile(
    r"(?:从)?([一-鿿]{2,8}?)(?:到|→)([一-鿿]{2,8}?)(?=$|，|,|的|高铁|火车|动车|车次|班次|线路|路线|\s)"
)
# 问句前缀（锚定开头）与日期短语：路由解析前先剥离，避免「帮我查一下这周四上海」
# 这类动词/日期前缀污染出发站。日期仍由 _parse_date 在原句上解析，此处只清理路由段。
_QUERY_LEAD_RE = re.compile(
    r"^(?:帮我|麻烦你?|请问|请|我想|我要|我打算|出差去|出差|去)?"
    r"(?:查询|查一下|查一查|查查|看一下|看一看|看看|搜一下|搜下|搜索|查|看|搜)+"
)
_ROUTE_NOISE_RE = re.compile(
    r"(?:这?下?周[一二三四五六日天]|今天|明天|后天|\d{1,2}月\d{1,2}日)"
)
_DURATION_RE = re.compile(r"^(?:(\d+)天)?(\d{1,2}):(\d{2})$")
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_CN_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
_WEEKDAY_RE = re.compile(r"下?周([一二三四五六日天])")
_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


class TrainQueryAgent(AgentBase):
    """车次查询智能体：真实 12306 车次/时刻/余票，结构化喂给行程编排。"""

    def __init__(
        self,
        name: str = "TrainQueryAgent",
        model=None,
        skills_root: Optional[str] = None,
        **kwargs,
    ):
        super().__init__()
        self.name = name
        self.model = model
        # 后端注入点：测试直接替换为假 backend；None 时按配置工厂创建。
        self._backend = None

    async def reply(
        self, x: Optional[Union[Msg, List[Msg]]] = None
    ) -> Msg:
        if x is None:
            return Msg(
                name=self.name,
                content=json.dumps({"query_success": False}, ensure_ascii=False),
                role="assistant",
            )

        content = x.content if not isinstance(x, list) else x[-1].content
        payload: Dict[str, Any] = {}
        user_query = ""
        if isinstance(content, str):
            try:
                payload = json.loads(content)
                context = payload.get("context", {})
                active_task = context.get("active_task") or {}
                entities = active_task.get("entities") or {}
                user_query = (
                    active_task.get("query")
                    or context.get("agent_query")
                    or context.get("rewritten_query", "")
                    or content
                )
            except json.JSONDecodeError:
                user_query = content
        else:
            user_query = str(content)

        trip = self._trip_from_previous_results(payload.get("previous_results") or [])
        origin, destination, date = self._resolve_query(trip, entities, user_query)

        if not origin or not destination:
            return self._failure(_PROMPT_COMPLETE_MSG)
        logger.info("Train query: %s → %s on %s", origin, destination, date)
        try:
            trains = await self._backend_instance().query_trains(origin, destination, date)
        except ExecutionLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — 后端任意失败都降级为核验提醒
            if isinstance(exc, TrainQueryError):
                message = str(exc)
            else:
                message = f"车次查询暂时不可用：{exc}"
            logger.warning("Train query failed for %s→%s: %s", origin, destination, exc)
            return self._failure(message)

        return Msg(
            name=self.name,
            content=json.dumps(self._build_result(origin, destination, date, trains), ensure_ascii=False),
            role="assistant",
        )

    # ---- 输入解析 --------------------------------------------------------

    @staticmethod
    def _trip_from_previous_results(
        previous_results: List[Dict],
    ) -> Optional[Dict[str, Any]]:
        """读 event_collection 行程卡（与 query-info 同源，保证编排内一致）。"""
        for item in reversed(previous_results):
            if item.get("agent_name") != "event_collection":
                continue
            data = (item.get("result") or {}).get("data") or {}
            if data.get("planning_ready") and data.get("origin") and data.get("destination"):
                return data
        return None

    def _resolve_query(
        self,
        trip: Optional[Dict[str, Any]],
        entities: Dict[str, Any],
        user_query: str,
    ) -> Tuple[str, str, str]:
        """字段提取顺序：行程卡 → 实体 → 用户问句解析。"""
        origin = destination = date = ""
        if trip:
            origin = str(trip.get("origin") or "").strip()
            destination = str(trip.get("destination") or "").strip()
            date = self._normalize_date(trip.get("start_date") or "")
        if not origin:
            origin = str(entities.get("origin") or "").strip()
        if not destination:
            destination = str(entities.get("destination") or "").strip()
        if not date:
            date = self._normalize_date(entities.get("start_date") or "")
        if not origin or not destination:
            parsed_origin, parsed_destination = self._parse_route(user_query)
            origin = origin or parsed_origin
            destination = destination or parsed_destination
        if not date:
            date = self._parse_date(user_query)
        return origin, destination, date

    @staticmethod
    def _parse_route(query: str) -> Tuple[str, str]:
        q = (query or "").strip()
        q = _QUERY_LEAD_RE.sub("", q)
        q = _ROUTE_NOISE_RE.sub("", q)
        m = _ROUTE_RE.search(q)
        if not m:
            return "", ""
        return m.group(1).strip(), m.group(2).strip()

    @classmethod
    def _parse_date(cls, query: str) -> str:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        q = query or ""

        m = _ISO_DATE_RE.search(q)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        m = _CN_DATE_RE.search(q)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            year = today.year if month >= today.month else today.year + 1
            return f"{year:04d}-{month:02d}-{day:02d}"
        if "大后天" in q:
            return (today + timedelta(days=3)).isoformat()
        if "后天" in q:
            return (today + timedelta(days=2)).isoformat()
        if "明天" in q:
            return (today + timedelta(days=1)).isoformat()
        if "今天" in q:
            return today.isoformat()
        m = _WEEKDAY_RE.search(q)
        if m:
            target = _WEEKDAY_MAP[m.group(1)]
            offset = (target - today.weekday()) % 7
            if "下周" in q:
                offset += 7
            return (today + timedelta(days=offset)).isoformat()
        # 用户未指定日期时，产品规则是按中国时区的当天查询，不再追问。
        return today.isoformat()

    @classmethod
    def _normalize_date(cls, raw: str) -> str:
        """把非 ISO 的日期（如「明天」「8月14日」）归一化为 YYYY-MM-DD。"""
        value = (raw or "").strip()
        if not value:
            return ""
        if _ISO_DATE_RE.match(value):
            return value
        return cls._parse_date(value)

    @staticmethod
    def _duration_minutes(duration: str) -> Optional[int]:
        m = _DURATION_RE.match(str(duration or ""))
        if not m:
            return None
        days = int(m.group(1)) if m.group(1) else 0
        return days * 1440 + int(m.group(2)) * 60 + int(m.group(3))

    # ---- 输出组装 --------------------------------------------------------

    def _build_result(
        self, origin: str, destination: str, date: str, trains: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        ordered = sorted(
            trains,
            key=lambda row: (self._duration_minutes(row.get("duration")) or 10**9),
        )
        if not ordered:
            return {
                "query_type": "高铁/火车车次查询",
                "query_success": True,
                "results": {
                    "trains": [],
                    "summary": f"未查询到 {origin}→{destination} 在 {date} 的直达车次，"
                    "可尝试其他日期或查询中转方案。",
                    "note": _VERIFICATION_NOTE,
                },
            }
        fastest = ordered[0]
        summary = (
            f"查询到 {len(ordered)} 趟车次，最快 {fastest['train_no']} "
            f"历时 {fastest['duration']}。"
        )
        return {
            "query_type": "高铁/火车车次查询",
            "query_success": True,
            "results": {
                "trains": ordered,
                "summary": summary,
                "note": _VERIFICATION_NOTE,
            },
        }

    @staticmethod
    def _failure(message: str) -> Msg:
        return Msg(
            name="TrainQueryAgent",
            content=json.dumps(
                {
                    "query_type": "高铁/火车车次查询",
                    "query_success": False,
                    "results": {"message": message, "note": _VERIFICATION_NOTE},
                },
                ensure_ascii=False,
            ),
            role="assistant",
        )

    def _backend_instance(self):
        if self._backend is None:
            self._backend = create_train_query_backend()
        return self._backend
