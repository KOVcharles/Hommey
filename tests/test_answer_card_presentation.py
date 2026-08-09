from pathlib import Path
import asyncio

from core.orchestration.fallback_composer import FallbackComposer
from core.orchestration.composer import AnswerComposer
from core.orchestration.models import IntentTask, TaskResult


def _compose(task, result):
    return FallbackComposer().compose([task], [result])


def test_itinerary_result_becomes_dense_trip_card():
    task = IntentTask(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        query="规划南京出差行程",
        entities={"destination": "南京"},
        display_order=0,
    )
    result = TaskResult(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        agent_name="itinerary_planning",
        status="success",
        data={
            "itinerary": {
                "title": "北京至南京两日出差",
                "duration": "2天1晚",
                "transport_recommendation": {
                    "preferred": "高铁",
                    "reason": "市中心往返更稳定",
                    "alternative": "飞机",
                },
                "lodging_advice": "会议地点附近，400元/晚以内",
                "daily_plans": [{
                    "day": 1,
                    "activities": [{
                        "time": "14:00",
                        "activity": "客户会议",
                        "transport": "地铁",
                    }],
                }],
                "notes": ["南京8月5日天气：多云，26~32°C，注意防暑。"],
                "reimbursement_checklist": ["车票", "住宿发票"],
            }
        },
        display_order=0,
    )

    document = _compose(task, result)

    assert document.title == "南京差旅信息"
    assert document.sections[0].kind == "trip"
    assert document.sections[0].title == "北京至南京两日出差"
    overview_items = {item.label for item in document.sections[0].items}
    assert {"行程时长", "首选交通", "住宿建议"} <= overview_items
    assert document.sections[1].title == "第 1 天"
    assert document.sections[1].items[0].value == "客户会议"
    assert document.plain_text  # render_plain_text 生成可读文字
    assert document.sections[0].status == "success"  # 成功 section 标记 success，非 error


def test_long_trip_fallback_does_not_crash():
    # P1-2：>12 天行程不再因 AnswerDocument.sections 上限抛 ValidationError。
    days = 15
    task = IntentTask(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        query="规划超长差旅行程",
        entities={"destination": "深圳"},
        display_order=0,
    )
    result = TaskResult(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        agent_name="itinerary_planning",
        status="success",
        data={
            "itinerary": {
                "title": "十五日差旅行程",
                "duration": f"{days}天",
                "daily_plans": [
                    {"day": index, "activities": [{"time": "09:00", "activity": f"第{index}天活动"}]}
                    for index in range(1, days + 1)
                ],
            }
        },
        display_order=0,
    )

    document = _compose(task, result)

    assert document.sections[0].kind == "trip"
    assert len(document.sections) == days + 1
    assert document.sections[-1].title == f"第 {days} 天"
    assert document.plain_text


def test_trip_card_shape_does_not_depend_on_composer_llm_output():
    task = IntentTask(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        query="规划东京出差行程",
        entities={"destination": "东京"},
        display_order=0,
    )
    result = TaskResult(
        task_id="itinerary_planning-itinerary_planning",
        goal_id="itinerary_planning",
        intent="itinerary_planning",
        agent_name="itinerary_planning",
        status="success",
        data={"itinerary": {
            "title": "东京一日出差",
            "daily_plans": [{"day": 1, "activities": [{
                "time": "09:00", "activity": "客户会议", "description": "按时到达",
            }]}],
        }},
        display_order=0,
    )
    called = False

    async def shape_changing_model(_messages):
        nonlocal called
        called = True
        return {"content": "{}"}

    document = asyncio.run(AnswerComposer(model=shape_changing_model).compose(
        task.query, [task], [result],
    ))

    assert called is False
    assert [section.title for section in document.sections] == ["东京一日出差", "第 1 天"]
    assert document.sections[1].items[0].label == "09:00"


def test_trip_compliance_unknown_is_rendered_as_human_summary():
    task = IntentTask(
        task_id="itinerary_planning",
        intent="itinerary_planning",
        query="规划广州出差行程",
        entities={"destination": "广州"},
        display_order=0,
    )
    plan = TaskResult(
        task_id="itinerary_planning-itinerary_planning",
        goal_id="itinerary_planning",
        intent="itinerary_planning",
        agent_name="itinerary_planning",
        status="success",
        data={"itinerary": {
            "title": "广州出差行程",
            "daily_plans": [],
            "notes": ["请出行前核验航班。"],
        }},
        display_order=0,
    )
    compliance = TaskResult(
        task_id="itinerary_planning-trip_compliance",
        goal_id="itinerary_planning",
        intent="itinerary_planning",
        agent_name="trip_compliance",
        status="success",
        data={
            "verdict": "unknown",
            "summary": "已取得制度来源，但交通等级仍需人工核验。",
        },
        display_order=1,
    )

    document = FallbackComposer().compose([task], [plan, compliance])

    assert "请出行前核验航班。" in document.notices
    assert "已取得制度来源，但交通等级仍需人工核验。" in document.notices
    assert all(notice.lower() != "unknown" for notice in document.notices)


def test_single_weather_result_becomes_structured_weather_card():
    task = IntentTask(
        task_id="information_query",
        intent="information_query",
        query="查南京天气",
        entities={"destination": "南京"},
        display_order=0,
    )
    result = TaskResult(
        task_id="information_query",
        intent="information_query",
        agent_name="information_query",
        status="success",
        data={"results": {
            "summary": (
                "南京当前天气：晴，气温 31°C，湿度 73%。未来几日："
                "2026-08-03: 晴，26~36°C；2026-08-04: 晴，25~33°C"
            )
        }},
        display_order=0,
    )

    document = _compose(task, result)

    assert document.sections[0].kind == "weather"
    assert len(document.sections[0].days) == 2
    assert document.sections[0].days[0].low == "26°C"
    assert document.sections[0].days[0].high == "36°C"
    assert document.sections[0].items[0].label == "当前"


def test_frontend_localizes_weather_api_conditions_to_chinese():
    script = (Path(__file__).resolve().parents[1] / "webui_new/static/answer-card.js").read_text(encoding="utf-8")

    assert "'cloudy': '多云'" in script
    assert "'patchy rain nearby': '附近有零星降雨'" in script
    assert "'light rain shower': '小阵雨'" in script
    assert "localizeWeatherPresentation" in script


def test_frontend_uses_full_width_scroll_layer_and_nontransparent_idle_thumb():
    root = Path(__file__).resolve().parents[1]
    css = (root / "webui_new/static/hommey.css").read_text(encoding="utf-8")
    app = (root / "webui_new/static/app.js").read_text(encoding="utf-8")
    intake = (root / "webui_new/static/trip-intake-card.js").read_text(encoding="utf-8")

    assert ".chat-messages {\n    width: 100%;" in css
    assert "scrollbar-color: color-mix" in css
    assert ".chat-messages.is-scrolling" in css
    assert "markConversationScrolling" in app
    assert "classList.remove('is-scrolling')" in app
    assert "查看文字版" in intake
    answer_card = (root / "webui_new/static/answer-card.js").read_text(encoding="utf-8")
    assert "renderPreDeparture" in answer_card
    assert "departure-critical-list" in answer_card
    assert "departure-reimbursement-list" in answer_card
    assert "upgradeLegacyPreDeparture" in answer_card
    assert "legacyReimbursementItem" in answer_card
    assert "normalizeMachinePlaceholders" in answer_card
    assert "upgradeLegacyTripTimeline" in answer_card
    assert "renderNotices" in answer_card
    assert "展开完整内容" in answer_card
    assert "查看行程细节" in answer_card
    assert "20260809-weather-zh-v8" in (root / "webui_new/templates/chat.html").read_text(encoding="utf-8")


def test_structured_cards_and_composer_share_one_content_rail():
    root = Path(__file__).resolve().parents[1]
    layout = (root / "webui_new/static/hommey.css").read_text(encoding="utf-8")
    answer = (root / "webui_new/static/answer-card.css").read_text(encoding="utf-8")
    intake = (root / "webui_new/static/trip-intake-card.css").read_text(encoding="utf-8")
    template = (root / "webui_new/templates/chat.html").read_text(encoding="utf-8")

    assert "--content: 800px" in layout
    assert ".message-row.ai { position: relative; display: block; }" in layout
    assert "right: calc(100% + 8px)" in layout
    assert ".message-row.ai:has(.answer-card) .msg-avatar.ai { display: none; }" in layout
    assert "width: calc(100% - 26px)" in layout
    assert ".answer-card {\n    width: 100%;" in answer
    assert ".trip-intake-card {\n    width: 100%;" in intake
    assert "20260809-knowledge-layout-v5" in template
