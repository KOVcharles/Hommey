from pathlib import Path

from core.presentation import build_legacy_answer_document


def test_itinerary_result_becomes_dense_card_with_optional_plain_text():
    result_data = {
        "results": [{
            "agent_name": "itinerary_planning",
            "status": "success",
            "data": {
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
                    "notes": [
                        "南京8月5日天气：多云，26~32°C，注意防暑，携带雨具和防晒用品。",
                        "公司住宿标准未明确，预订前需要确认酒店价格。",
                    ],
                    "missing_info": ["客户会议的具体地点和时间"],
                    "reimbursement_checklist": ["车票", "住宿发票"],
                }
            },
        }]
    }

    document = build_legacy_answer_document(result_data, "完整文字方案")

    assert document.title == "北京至南京两日出差"
    assert document.plain_text == "完整文字方案"
    assert [section.title for section in document.sections] == [
        "行程概览", "第 1 天",
    ]
    overview = document.sections[0]
    assert {item.label for item in overview.items} >= {"行程时长", "首选交通", "住宿建议"}
    assert document.sections[1].items[0].value == "客户会议"
    assert document.pre_departure.pending_count == 2
    assert [item.label for item in document.pre_departure.critical_items] == [
        "会议安排", "交通票务", "酒店预订",
    ]
    assert document.pre_departure.weather.temperature == "26–32°C"
    assert document.pre_departure.weather.preparation == ["雨具", "防晒用品", "补水防暑"]
    assert [item.label for item in document.pre_departure.reimbursement_items] == [
        "交通票据", "住宿发票",
    ]


def test_single_weather_result_becomes_structured_weather_card():
    result_data = {
        "results": [{
            "agent_name": "information_query",
            "status": "success",
            "data": {"results": {
                "summary": (
                    "南京当前天气：晴，气温 31°C，湿度 73%。未来几日："
                    "2026-08-03: 晴，26~36°C；2026-08-04: 晴，25~33°C"
                )
            }},
        }]
    }

    document = build_legacy_answer_document(result_data)

    assert document.title == "出行信息"
    assert document.sections[0].kind == "weather"
    assert len(document.sections[0].days) == 2
    assert document.sections[0].items[0].label == "当前"


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
