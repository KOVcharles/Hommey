"""train-query 后端接缝：12306 官方公开接口直连（默认，免费无 Key）+ juhe 预留。

照 ``rag/sparse.create_sparse_index`` 的工厂模式与 ``rag/embedder`` 的
session 注入模式：

- 后端名可配置（``HOMMEY_TRAIN_QUERY_BACKEND``），未知后端 fail-fast；
- 构造可注入 http session（测试用假 session 喂 fixture）；
- 每次外部调用走 ``consume_external_call("train")``，瞬时失败走指数退避重试
  （``utils.llm_resilience.retry_with_backoff``）；``ExecutionLimitExceeded``
  不重试、立即传播。

12306 协议（逆向的公开接口，仅供行程参考）：
  1. warm-up GET ``/otn/`` 拿会话 cookie；
  2. 拉 ``station_name.js`` 建「中文站名 → 三字电码」映射（进程内缓存，默认 7 天）；
  3. ``leftTicket/{query|queryA|queryZ|queryX}`` 查时刻+余票（后缀轮询，
     12306 会轮换接口路径）；
  4. 按 ``|`` 切 ``data.result[]`` 行并归一化（2024 版索引映射，
     见 ``Train12306Backend._SEAT_INDEX``；``data.map`` 提供站码→站名）。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from core.execution_budget import consume_external_call
from settings import TRAIN_QUERY_CONFIG
from utils.llm_resilience import retry_with_backoff

logger = logging.getLogger(__name__)

_OTN_BASE = "https://kyfw.12306.cn/otn"
_STATION_JS_URL = f"{_OTN_BASE}/resources/js/framework/station_name.js"
_LEFT_TICKET_SUFFIXES = ("query", "queryA", "queryZ", "queryX")
# station_name.js 形如: var station_names ='@bjb|北京北|VAP|beijingbei|bjb|0@bjd|...'
_STATION_JS_PATTERN = re.compile(r"@[a-z]+\|([一-鿿]+)\|([A-Z]{3})\|")
_TRAIN_CODE = re.compile(r"^([GCDZTKLS]\d+)$")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")
_DEFAULT_UA = "Hommey/1.0 (+12306 official public endpoint)"


class TrainQueryError(RuntimeError):
    """Train backend failure with a user-safe message (→ ``query_success: false``)."""


@dataclass(frozen=True)
class TrainQueryConfig:
    backend: str = "12306"
    juhe_train_key: Optional[str] = None
    timeout_sec: float = 10.0
    max_retries: int = 2
    retry_base_delay_sec: float = 1.0
    retry_max_delay_sec: float = 10.0
    station_cache_ttl_sec: int = 7 * 24 * 3600

    @classmethod
    def from_settings(cls, overrides: Optional[Dict[str, Any]] = None) -> "TrainQueryConfig":
        data = {
            "backend": TRAIN_QUERY_CONFIG.get("backend", cls.backend),
            "juhe_train_key": TRAIN_QUERY_CONFIG.get("juhe_train_key", cls.juhe_train_key),
            "timeout_sec": TRAIN_QUERY_CONFIG.get("timeout_sec", cls.timeout_sec),
            "max_retries": TRAIN_QUERY_CONFIG.get("max_retries", cls.max_retries),
            "retry_base_delay_sec": TRAIN_QUERY_CONFIG.get(
                "retry_base_delay_sec", cls.retry_base_delay_sec
            ),
            "retry_max_delay_sec": TRAIN_QUERY_CONFIG.get(
                "retry_max_delay_sec", cls.retry_max_delay_sec
            ),
            "station_cache_ttl_sec": TRAIN_QUERY_CONFIG.get(
                "station_cache_ttl_sec", cls.station_cache_ttl_sec
            ),
        }
        if overrides:
            data.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**data)


class TrainQueryBackend(Protocol):
    async def query_trains(self, origin: str, destination: str, date: str) -> List[Dict[str, Any]]:
        """Normalized train rows for ``origin → destination`` on ``date`` (YYYY-MM-DD)."""
        ...


class Train12306Backend:
    """12306 官方公开接口直连（免 Key、免费）。

    反爬韧性：
    - ``leftTicket`` 路径后缀轮询（``query``/``queryA``/``queryZ``/``queryX``）；
    - 站名表与 warm-up 仅进程内首次拉取（7 天 TTL 缓存），把每次车次查询的
      外部调用控制在 budget 的 per-type 上限（默认 6）以内；
    - 未知站名 / 接口变动 / 被挡 → ``TrainQueryError``，由 agent 降级为
      ``query_success: false`` + 官方核验提醒，绝不硬撑。
    """

    # 2024 版 data.result[] 行按 `|` 切分的索引映射（多个 2024 爬虫实现一致）。
    _SEAT_INDEX = {
        "商务座": 32,
        "一等座": 31,
        "二等座": 30,
        "软卧": 23,
        "硬卧": 28,
        "硬座": 29,
        "软座": 27,
        "无座": 26,
    }

    def __init__(
        self,
        *,
        config: Optional[TrainQueryConfig] = None,
        session: Any = None,
    ):
        self.config = config or TrainQueryConfig.from_settings()
        self._session = session
        self._station_map_cache: Dict[str, str] = {}
        self._station_map_at: float = 0.0
        self._warmed = False

    def _client(self) -> Any:
        if self._session is None:
            import httpx

            self._session = httpx.Client(
                timeout=self.config.timeout_sec,
                follow_redirects=True,
                headers={"User-Agent": _DEFAULT_UA},
            )
        return self._session

    async def query_trains(
        self, origin: str, destination: str, date: str
    ) -> List[Dict[str, Any]]:
        station_map = await self._station_map()
        origin_code = station_map.get(origin)
        destination_code = station_map.get(destination)
        if not origin_code or not destination_code:
            raise TrainQueryError(
                f"无法识别车站「{origin}」或「{destination}」，请使用 12306 官方站名"
            )
        result, names = await self._left_ticket(origin_code, destination_code, date)
        return self._normalize_rows(result, names, origin, destination)

    async def _station_map(self) -> Dict[str, str]:
        now = time.monotonic()
        if self._station_map_cache and (now - self._station_map_at) < self.config.station_cache_ttl_sec:
            return self._station_map_cache
        await self._warm_up()

        async def _fetch_stations():
            resp = await self._fetch(_STATION_JS_URL, kind="train")
            return _STATION_JS_PATTERN.findall(resp.text or "")

        pairs = await retry_with_backoff(
            _fetch_stations,
            max_retries=self.config.max_retries,
            base_delay_sec=self.config.retry_base_delay_sec,
            max_delay_sec=self.config.retry_max_delay_sec,
        )
        mapping = dict(pairs)
        if not mapping:
            raise TrainQueryError("12306 车站表为空（接口可能变动），请稍后重试")
        self._station_map_cache = mapping
        self._station_map_at = now
        return mapping

    async def _warm_up(self) -> None:
        if self._warmed:
            return
        await self._fetch(f"{_OTN_BASE}/", kind="train")
        self._warmed = True

    async def _left_ticket(
        self, from_code: str, to_code: str, date: str
    ) -> tuple[List[str], Dict[str, str]]:
        last_error: Optional[BaseException] = None
        for suffix in _LEFT_TICKET_SUFFIXES:
            params = {
                "leftTicketDTO.train_date": date,
                "leftTicketDTO.from_station": from_code,
                "leftTicketDTO.to_station": to_code,
                "purpose_codes": "ADULT",
            }
            try:
                resp = await self._fetch(f"{_OTN_BASE}/leftTicket/{suffix}", params=params, kind="train")
                payload = resp.json() or {}
                data = payload.get("data")
                # A successful 12306 response may legitimately contain an empty
                # result list (no remaining/direct trains for that date).  Empty
                # is a business result, not an upstream outage, and must stop the
                # suffix polling immediately.
                if isinstance(data, dict) and isinstance(data.get("result"), list):
                    return list(data["result"]), data.get("map") or {}
                detail = (
                    payload.get("messages")
                    or payload.get("validateMessages")
                    or "响应缺少 data.result"
                )
                if isinstance(detail, list):
                    detail = "；".join(str(item) for item in detail if item)
                last_error = TrainQueryError(f"leftTicket/{suffix} 返回无效响应：{detail}")
                logger.warning("leftTicket/%s returned invalid payload: %s", suffix, detail)
            except Exception as exc:  # noqa: BLE001 — 后缀轮询本身就是重试
                last_error = exc
                logger.warning("leftTicket/%s failed: %s", suffix, exc)
                continue
        raise TrainQueryError(f"12306 余票接口暂不可用：{last_error}")

    async def _fetch(self, url: str, *, params: Optional[Dict[str, str]] = None, kind: str = "train"):
        consume_external_call(kind)
        client = self._client()
        loop = asyncio.get_event_loop()

        def _get():
            resp = client.get(url, params=params, timeout=self.config.timeout_sec)
            resp.raise_for_status()
            return resp

        return await loop.run_in_executor(None, _get)

    @classmethod
    def _normalize_rows(
        cls, rows: List[str], names: Dict[str, str], origin: str, destination: str
    ) -> List[Dict[str, Any]]:
        """把 12306 的 `|` 分隔行归一化为结构化车次行。

        防御式解析：核心字段（车次/时间/历时）必须通过校验才保留，否则跳过该行
        ——接口索引变动时宁可少返回，也不返回垃圾数据。
        """
        trains: List[Dict[str, Any]] = []
        for raw in rows:
            fields = str(raw).split("|")
            train_no = fields[3] if len(fields) > 3 else ""
            depart = fields[8] if len(fields) > 8 else ""
            arrive = fields[9] if len(fields) > 9 else ""
            duration = fields[10] if len(fields) > 10 else ""
            if not _TRAIN_CODE.match(train_no) or not _TIME.match(depart) or not _TIME.match(arrive):
                continue
            seats: Dict[str, str] = {}
            for seat_name, index in cls._SEAT_INDEX.items():
                if index < len(fields) and fields[index]:
                    seats[seat_name] = fields[index]
            from_code = fields[6] if len(fields) > 6 else ""
            to_code = fields[7] if len(fields) > 7 else ""
            trains.append(
                {
                    "train_no": train_no,
                    "from_station": names.get(from_code) or origin,
                    "to_station": names.get(to_code) or destination,
                    "depart_time": depart,
                    "arrive_time": arrive,
                    "duration": duration,
                    "seats": seats,
                    # 票价查询（queryTicketPrice）为后置阶段，本期先留空。
                    "prices": {},
                }
            )
        return trains


class JuheTrainBackend:
    """聚合数据（juhe.cn）火车订票查询——付费/限频预留后端。

    接口：``https://apis.juhe.cn/fapigw/train/query``（需 AppKey）。
    免费额度很低（官方约 10 次/天），仅在正式环境确需稳定授权数据源时切换；
    本类为接缝占位，调用即抛 ``TrainQueryError``。
    """

    def __init__(self, *, config: Optional[TrainQueryConfig] = None, session: Any = None):
        self.config = config or TrainQueryConfig.from_settings()
        if not self.config.juhe_train_key:
            raise ValueError("juhe 后端需要配置 HOMMEY_JUHE_TRAIN_KEY")
        self._session = session

    async def query_trains(self, origin: str, destination: str, date: str) -> List[Dict[str, Any]]:
        raise TrainQueryError("juhe 后端尚未接入（预留配置接缝）")


def create_train_query_backend(
    backend: Optional[str] = None,
    *,
    config: Optional[TrainQueryConfig] = None,
    session: Any = None,
) -> TrainQueryBackend:
    """后端工厂：未知后端 fail-fast，便于测试注入假 session / 假 backend。"""
    cfg = config or TrainQueryConfig.from_settings()
    normalized = (backend or cfg.backend or "12306").lower()
    if normalized == "12306":
        return Train12306Backend(config=cfg, session=session)
    if normalized == "juhe":
        if not cfg.juhe_train_key:
            raise ValueError("juhe 后端需要配置 HOMMEY_JUHE_TRAIN_KEY")
        return JuheTrainBackend(config=cfg, session=session)
    raise ValueError(f"Unsupported train query backend: {backend}")
