"""train-query 后端测试：配置接缝、12306 解析、未知站名降级、juhe 预留。"""
import asyncio
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(".agents/skills/train-query/script/train_backend.py")


def _load_module():
    # 通过正常导入机制加载：train_backend 用了 @dataclass + from __future__ import
    # annotations，spec_from_file_location 不注册 sys.modules 会破坏字符串注解解析。
    script_dir = str(SCRIPT_PATH.parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    import train_backend

    return train_backend


def _row_12306(overrides=None):
    """构造一条 2024 版 leftTicket result[] 行（按 `|` 索引填充 33 字段）。"""
    row = [""] * 33
    row[3] = "G2"        # 车次
    row[6] = "AOH"       # 出发站电码
    row[7] = "VNP"       # 到达站电码
    row[8] = "07:00"     # 出发时间
    row[9] = "11:30"     # 到达时间
    row[10] = "04:30"    # 历时
    row[23] = "有"       # 软卧
    row[26] = "无"       # 无座
    row[27] = "有"       # 软座
    row[28] = "10"       # 硬卧
    row[29] = "有"       # 硬座
    row[30] = "有"       # 二等座
    row[31] = "5"        # 一等座
    row[32] = "有"       # 商务座
    for index, value in (overrides or {}).items():
        row[index] = value
    return "|".join(row)


def _left_ticket_payload(*rows):
    return {
        "data": {
            "result": list(rows),
            "map": {"AOH": "上海虹桥", "VNP": "北京南"},
        }
    }


class FakeResponse:
    def __init__(self, text="", json_data=None):
        self.text = text
        self._json = json_data
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class FakeSession:
    def __init__(self, station_js, left_ticket):
        self._station_js = station_js
        self._left = left_ticket
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if "station_name.js" in url:
            return FakeResponse(text=self._station_js)
        if "/leftTicket/" in url:
            return FakeResponse(json_data=self._left)
        return FakeResponse(text="<html></html>")


def _backend(module, **config_overrides):
    session = config_overrides.pop("session", None)
    config = module.TrainQueryConfig.from_settings(config_overrides)
    return module.Train12306Backend(config=config, session=session)


def test_create_train_query_backend_unknown_raises():
    module = _load_module()

    with pytest.raises(ValueError, match="Unsupported train query backend"):
        module.create_train_query_backend("nonexistent")


def test_train_query_config_defaults_and_overrides():
    module = _load_module()

    default = module.TrainQueryConfig.from_settings({})
    assert default.backend == "12306"
    assert default.max_retries == 2
    assert default.station_cache_ttl_sec == 7 * 24 * 3600

    overridden = module.TrainQueryConfig.from_settings({"max_retries": 0, "timeout_sec": 3.0})
    assert overridden.max_retries == 0
    assert overridden.timeout_sec == 3.0

    # None 覆盖值被丢弃（保留默认）。
    dropped = module.TrainQueryConfig.from_settings({"juhe_train_key": None, "max_retries": None})
    assert dropped.max_retries == 2


def test_12306_backend_parses_left_ticket_fixture():
    module = _load_module()
    station_js = (
        "@beijingbei|北京北|VAP|beijingbei|bjb|0"
        "@aoh|上海虹桥|AOH|shanghaihongqiao|shhq|0"
        "@vnp|北京南|VNP|beijingnan|bjn|0"
    )
    payload = _left_ticket_payload(_row_12306())
    session = FakeSession(station_js, payload)
    backend = _backend(module, max_retries=0, session=session)

    trains = asyncio.run(backend.query_trains("上海虹桥", "北京南", "2026-08-14"))

    assert len(trains) == 1
    train = trains[0]
    assert train["train_no"] == "G2"
    assert train["from_station"] == "上海虹桥"
    assert train["to_station"] == "北京南"
    assert train["depart_time"] == "07:00"
    assert train["arrive_time"] == "11:30"
    assert train["duration"] == "04:30"
    assert train["seats"] == {
        "软卧": "有", "硬卧": "10", "软座": "有", "硬座": "有",
        "无座": "无", "二等座": "有", "一等座": "5", "商务座": "有",
    }
    assert train["prices"] == {}

    # warm-up + station JS 缓存后，余票只请求一次。
    left_ticket_calls = [(url, params) for url, params in session.calls if "/leftTicket/" in url]
    assert len(left_ticket_calls) == 1
    assert left_ticket_calls[0][1]["leftTicketDTO.from_station"] == "AOH"
    assert left_ticket_calls[0][1]["leftTicketDTO.to_station"] == "VNP"
    assert left_ticket_calls[0][1]["leftTicketDTO.train_date"] == "2026-08-14"


def test_12306_backend_skips_rows_failing_sanity_check():
    module = _load_module()
    station_js = "@aoh|上海虹桥|AOH|x|0@vnp|北京南|VNP|y|0"
    garbage = _row_12306({3: "not-a-train", 8: "", 9: ""})
    payload = _left_ticket_payload(_row_12306(), garbage)
    session = FakeSession(station_js, payload)
    backend = _backend(module, max_retries=0, session=session)

    trains = asyncio.run(backend.query_trains("上海虹桥", "北京南", "2026-08-14"))

    assert len(trains) == 1
    assert trains[0]["train_no"] == "G2"


def test_12306_backend_treats_valid_empty_result_as_no_trains_not_outage():
    module = _load_module()
    station_js = "@aoh|上海虹桥|AOH|x|0@vnp|北京南|VNP|y|0"
    payload = {
        "status": True,
        "httpstatus": 200,
        "messages": "",
        "data": {"flag": "1", "result": [], "map": {}},
    }
    session = FakeSession(station_js, payload)
    backend = _backend(module, max_retries=0, session=session)

    trains = asyncio.run(backend.query_trains("上海虹桥", "北京南", "2026-08-14"))

    assert trains == []
    left_ticket_calls = [url for url, _ in session.calls if "/leftTicket/" in url]
    assert len(left_ticket_calls) == 1


def test_12306_backend_malformed_payload_reports_reason_instead_of_none():
    module = _load_module()
    station_js = "@aoh|上海虹桥|AOH|x|0@vnp|北京南|VNP|y|0"
    session = FakeSession(
        station_js,
        {"status": False, "messages": ["系统繁忙"], "data": {}},
    )
    backend = _backend(module, max_retries=0, session=session)

    with pytest.raises(module.TrainQueryError, match="系统繁忙") as exc_info:
        asyncio.run(backend.query_trains("上海虹桥", "北京南", "2026-08-14"))

    assert not str(exc_info.value).endswith("None")


def test_unknown_station_fails_gracefully():
    module = _load_module()
    station_js = "@aoh|上海虹桥|AOH|x|0@vnp|北京南|VNP|y|0"
    session = FakeSession(station_js, _left_ticket_payload(_row_12306()))
    backend = _backend(module, max_retries=0, session=session)

    with pytest.raises(module.TrainQueryError, match="无法识别车站"):
        asyncio.run(backend.query_trains("上海虹桥", "北京西", "2026-08-14"))


def test_station_map_cached_within_ttl():
    module = _load_module()
    station_js = "@aoh|上海虹桥|AOH|x|0@vnp|北京南|VNP|y|0"
    session = FakeSession(station_js, _left_ticket_payload(_row_12306()))
    backend = _backend(module, max_retries=0, session=session)

    asyncio.run(backend.query_trains("上海虹桥", "北京南", "2026-08-14"))
    asyncio.run(backend.query_trains("上海虹桥", "北京南", "2026-08-15"))

    station_js_calls = [url for url, _ in session.calls if "station_name.js" in url]
    assert len(station_js_calls) == 1


def test_juhe_backend_without_key_raises():
    module = _load_module()

    with pytest.raises(ValueError, match="HOMMEY_JUHE_TRAIN_KEY"):
        module.create_train_query_backend("juhe")


def test_juhe_backend_with_key_is_stub_that_fails_on_query():
    module = _load_module()
    config = module.TrainQueryConfig.from_settings({"juhe_train_key": "fake-key"})
    backend = module.create_train_query_backend("juhe", config=config)

    with pytest.raises(module.TrainQueryError, match="尚未接入"):
        asyncio.run(backend.query_trains("上海", "北京", "2026-08-14"))
