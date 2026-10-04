"""Discovery and recovery regressions from the September 19 weather/rail turn."""
from copy import deepcopy
import json

import pytest

from agent_runtime.capabilities import capability_prompt
from agent_runtime.engine import MAIN_TOOLS, Supervisor
from agent_runtime.services import TOOLS, tool_schemas
from tests.test_supervisor_control import role_of
from tests.test_supervisor_runtime import CONFIG, SCOPE, FakeServices, FakeStore, outputs, reply
from utils.skill_loader import SkillLoader


class NanjingServices(FakeServices):
    async def execute(self, scope, name, args):
        self.calls.append((scope, name))
        if name == "get_weather":
            assert args.city == "南京"
            return "weather", {"city": "南京", "forecasts": [
                {"date": "2026-09-19", "day_condition": "晴", "low_c": 20, "high_c": 29}]}
        if name == "search_trains":
            assert args.destination == "南京"
            return "train", [{"train_no": "G101", "from_station": "北京南", "to_station": "南京南",
                              "depart_time": "08:00", "arrive_time": "12:00", "duration": "04:00", "seats": {"二等座": "有"}}]
        raise AssertionError(name)


def query(name):
    return (name, {"city": "南京"} if name == "get_weather" else
            {"origin": "北京", "destination": "南京", "date": "2026-09-19"})


def report_for(reads):
    findings = [{"item": "南京天气" if value["kind"] == "weather" else "高铁",
                 "conclusion": "南京晴，20至29度" if value["kind"] == "weather" else "G101 北京南至南京南，08:00出发",
                 "evidence_refs": [value["id"]]} for value in reads]
    return {"summary": "查询结果如下", "data": {"findings": findings}, "evidence_refs": [r["id"] for r in reads]}


def checkpoint(store):
    return store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]


@pytest.mark.asyncio
async def test_wrong_skill_resources_do_not_drop_weather_after_successful_train_query():
    async def model(messages, tools, **kwargs):
        out = outputs(messages)
        if role_of(messages):
            task = json.loads(messages[1]["content"])["task"]
            if not out:
                return reply(query("get_weather" if "天气" in task else "search_trains"))
            if len(out) == 1:
                return reply(("read_source", {"source_id": out[0]["sources"][0]["source_id"]}))
            return reply(("report", report_for([out[1]])))
        step = len(out)
        if step in {0, 5, 6, 7}:
            resource = {0: "train-query", 5: "place-query", 6: "weather", 7: "references/weather"}[step]
            return reply(("read_skill", {"name": "query-info", "resource": resource}))
        if step == 1:
            assert out[-1]["failure"]["code"] == "SKILL_RESOURCE_NOT_FOUND"
            assert "name='train-query'" in out[-1]["error"]
            return reply(("read_skill", {"name": "query-info"}))
        if step == 2:
            assert out[-1]["available_resources"] == []
            return reply(("delegate", {"role": "travel_info", "task": "查询北京到南京高铁，2026-09-19"}))
        if step == 3:
            return reply(("read_result", {"result_id": out[2]["result_id"]}))
        if step == 4:
            return reply(("read_source", {"source_id": out[3]["evidence_refs"][0]}))
        if step == 8:
            assert all(v["available_resources"] == [] for v in out[5:8])
            return reply(("delegate", {"role": "travel_info", "task": "仅补查南京天气"}))
        return reply(("finish", {"result_ids": [out[2]["result_id"], out[8]["result_id"]]}))

    services = NanjingServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "查询南京天气和2026-09-19北京去南京的高铁")
    assert "南京" in result["response"] and "G101" in result["response"] and "晴" in result["response"]
    assert result["outcome"] != "degraded"
    assert [name for _, name in services.calls] == ["search_trains", "get_weather"]
    assert store.writes == 0
    assert "当前可直接调用的工具" not in checkpoint(store)["main"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_invalid_report_can_be_repaired_by_querying_weather_after_reading_train():
    child_round = 0
    async def model(messages, tools, **kwargs):
        nonlocal child_round
        out = outputs(messages)
        names = {t["function"]["name"] for t in tools}
        if role_of(messages) is None:
            if not out:
                return reply(("delegate", {"role": "travel_info", "task": "查询南京天气以及北京至南京高铁"}))
            return reply(("finish", {"result_ids": [out[0]["result_id"]]}))
        child_round += 1
        if child_round == 1:
            return reply(query("search_trains"))
        if child_round == 2:
            return reply(("read_source", {"source_id": out[0]["sources"][0]["source_id"]}))
        if child_round == 3:
            invalid = report_for([out[1]])
            invalid["data"]["findings"].append({"item": "天气", "conclusion": "伪造天气", "evidence_refs": ["weather"]})
            return reply(("report", invalid))
        if child_round == 4:
            assert out[-1]["failure"]["code"] == "UNKNOWN_EVIDENCE"
            assert "get_weather" in names
            return reply(query("get_weather"))
        if child_round == 5:
            return reply(("read_source", {"source_id": out[-1]["sources"][0]["source_id"]}))
        assert child_round == 6 and names == {"report"}
        return reply(("report", report_for([out[1], out[4]])))

    services = NanjingServices()
    store = FakeStore(services)
    result = await Supervisor(model, services, store, CONFIG).run(SCOPE, "查询南京天气和北京去南京的高铁")
    assert "晴" in result["response"] and "G101" in result["response"] and "伪造" not in result["response"]
    assert result["agents"][0]["status"] == "success" and child_round == 6
    assert len(services.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_name,code", [("nonexistent_weather_tool", "UNKNOWN_TOOL"), ("get_weather", "TOOL_NOT_AVAILABLE")])
async def test_unknown_or_indirect_tool_returns_corrective_feedback(bad_name, code):
    async def model(messages, tools, **kwargs):
        out = outputs(messages)
        if role_of(messages) is None:
            if not out:
                assert "get_weather(city: string)" not in messages[0]["content"]
                assert "travel_info" in messages[0]["content"]
                return reply((bad_name, {"city": "南京"}))
            if len(out) == 1:
                assert out[0]["failure"]["code"] == code
                assert out[0]["failure"]["next_action"] == "repair_arguments"
                assert out[0]["available_tools"] == sorted(t["function"]["name"] for t in tools)
                if code == "TOOL_NOT_AVAILABLE":
                    assert "travel_info" in out[0]["error"]
                return reply(("delegate", {"role": "travel_info", "task": "查询南京天气"}))
            return reply(("finish", {"result_ids": [out[-1]["result_id"]]}))
        if not out:
            return reply(("not_a_tool", {}), query("get_weather"))
        if len(out) == 2:
            assert out[0]["failure"]["code"] == "UNKNOWN_TOOL" and out[1]["sources"]
            return reply(("read_source", {"source_id": out[1]["sources"][0]["source_id"]}))
        return reply(("report", report_for([out[-1]])))

    services = NanjingServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "帮我查询南京的天气信息")
    assert result["outcome"] != "degraded" and "晴" in result["response"]
    assert len(services.calls) == 1


@pytest.mark.asyncio
async def test_uncooperative_discovery_is_still_bounded_without_external_calls():
    calls = 0
    async def model(messages, **kwargs):
        nonlocal calls
        calls += 1
        return reply(("does_not_exist", {}))
    services = NanjingServices()
    result = await Supervisor(model, services, FakeStore(services), CONFIG).run(SCOPE, "南京天气及高铁查询")
    assert calls == CONFIG["main_rounds"] and services.calls == []
    assert result["outcome"] == "degraded" and result["stop_reason"] == "SUPERVISOR_ROUND_LIMIT"


def test_catalog_tracks_registry_and_current_tool_schema_without_mutation(monkeypatch):
    original = deepcopy(MAIN_TOOLS)
    request, _ = TOOLS["get_weather"]
    monkeypatch.setitem(TOOLS, "get_weather", (request, "weather-provider-new-description"))
    prompt = capability_prompt(MAIN_TOOLS, None)
    assert "weather-provider-new-description" not in prompt and "city: string" not in prompt
    assert "travel_info" in prompt and "query-info" in prompt
    assert MAIN_TOOLS == original
    child = capability_prompt(tool_schemas(["get_weather"]), "travel_info")
    assert child == ""
    assert tool_schemas(["get_weather"])[0]["function"]["description"] == "weather-provider-new-description"


def test_resource_discovery_lists_only_existing_references(tmp_path):
    root = tmp_path / "sample"
    refs = root / "references"
    refs.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: sample\ndescription: Travel guidance\n---\nUse reference files.", encoding="utf-8")
    (refs / "example.md").write_text("Useful guidance", encoding="utf-8")
    (root / "private.txt").write_text("not a reference", encoding="utf-8")
    loader = SkillLoader(str(tmp_path))
    assert loader.list_skill_resources("sample") == ["references/example.md"]
    assert loader.list_skill_resources("missing") == []
