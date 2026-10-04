"""Personal profile dependencies, authorization, model projection and turn refresh."""
from copy import deepcopy
from datetime import date, timedelta
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from agent_runtime.engine import Supervisor, Turn
from agent_runtime.user_profile import profile_from_personal, profile_for_role
from context.user_profile_repository import ProfileConflict
from core.user_profile import PersonalProfile
from webui_new.auth.deps import get_current_user
from webui_new.auth.storage import User
from webui_new.core.errors import register_error_handlers
from webui_new.routes.personal_profile import create_personal_profile_router
from tests.test_supervisor_runtime import CONFIG, SCOPE, TEXT, FakeServices, FakeStore, JourneyModel, model_payload


def staff_profile():
    return PersonalProfile.model_validate({
        "basic_info": {"real_name": "测试用户", "personnel_category": "staff", "employee_number": "0000123",
                       "student_number": "0000321", "gender": "female", "birth_date": "1971-10-05"},
        "policy_identity": {"professional_title_level": "senior", "professional_position_grade": 2, "staff_grade": 3},
        "funding": {"default_project_id": "project-a", "projects": [{"id": "project-a", "name": "课题 A",
            "financial_project_code": "SECRET-PROJECT-CODE", "funding_category": "research", "research_type": "vertical",
            "program_type": "national_social_science", "funding_source": "fiscal", "user_project_role": "principal"}]},
        "settlement": {"has_official_card": False}})


def test_unknown_values_remain_null_and_student_identity_cannot_be_set():
    student = PersonalProfile.model_validate({"basic_info": {"personnel_category": "student", "student_number": "00123"}})
    assert student.basic_info.student_number == "00123"
    assert all(value is None for value in student.policy_identity.model_dump().values())
    assert student.settlement.has_official_card is None
    assert student.funding.projects == []
    for field, value in (("professional_title_level", "senior"), ("staff_grade", 3), ("nationally_recognized_expert", False)):
        with pytest.raises(ValidationError):
            PersonalProfile.model_validate({"basic_info": {"personnel_category": "student"}, "policy_identity": {field: value}})


@pytest.mark.parametrize("identity", [{"staff_grade": 3}, {"administrative_rank": "other"}])
def test_external_personnel_has_no_internal_staff_grade(identity):
    with pytest.raises(ValidationError):
        PersonalProfile.model_validate({"basic_info": {"personnel_category": "external"}, "policy_identity": identity})


def test_none_title_and_future_birthday_are_invalid():
    with pytest.raises(ValidationError):
        PersonalProfile.model_validate({"basic_info": {"personnel_category": "staff"},
            "policy_identity": {"professional_title_level": "none", "professional_position_grade": 2}})
    with pytest.raises(ValidationError):
        PersonalProfile.model_validate({"basic_info": {"birth_date": (date.today() + timedelta(days=2)).isoformat()}})


@pytest.mark.parametrize("key,value", [("research_type", "vertical"), ("program_type", "national_social_science"), ("is_military_project", False)])
def test_non_research_funding_rejects_stale_research_fields(key, value):
    with pytest.raises(ValidationError):
        PersonalProfile.model_validate({"funding": {"projects": [{"funding_category": "non_research", key: value}]}})


def test_default_project_is_owned_by_this_profile_and_ids_unique():
    for funding in ({"default_project_id": "another-users-project"}, {"projects": [{"id": "a"}, {"id": "a"}]}):
        with pytest.raises(ValidationError):
            PersonalProfile.model_validate({"funding": funding})


def test_projection_uses_reference_date_and_omits_private_fields():
    full = staff_profile().model_dump(mode="json")
    before = deepcopy(full)
    view = profile_from_personal(full, {"home_location": "上海"}, "2026-10-04")
    assert view["identity"]["age_years"] == 54
    assert view["identity"]["age_as_of"] == "2026-10-04"
    assert profile_from_personal(full, {}, "2026-10-05")["identity"]["age_years"] == 55
    encoded = json.dumps(view, ensure_ascii=False)
    for value in ("0000123", "0000321", "1971-10-05", "gender", "real_name"):
        assert value not in encoded
    assert view["default_funding"]["program_type"] == "national_social_science"
    assert view["default_funding"]["financial_project_code"] == "SECRET-PROJECT-CODE"
    assert view["settlement"]["has_official_card"] is False
    assert full == before
    for role in ("policy_rag", "compliance", "trip_planner"):
        assert profile_for_role(view, role)["identity"]["professional_position_grade"] == 2
    assert "identity" not in profile_for_role(view, "travel_info")
    assert "default_funding" not in profile_for_role(view, "trip_context")


@pytest.mark.parametrize("default_id", [None, "b"])
def test_all_funding_projects_reach_relevant_roles_with_unknowns_and_explicit_false(default_id):
    personal = PersonalProfile.model_validate({"basic_info": {"institution": None}, "funding": {
        "default_project_id": default_id, "projects": [
            {"id": "a", "name": "同名课题", "financial_project_code": "0000123", "funding_category": "research",
             "research_type": "horizontal", "is_military_project": False, "user_project_role": "principal"},
            {"id": "b", "name": "同名课题", "funding_category": "non_research", "user_project_role": "member"}]}}).model_dump(mode="json")
    original = deepcopy(personal)
    view = profile_from_personal(personal, {}, "2026-10-04")
    assert view["source"] == "saved_personal_profile" and view["verification"] == "user_entered"
    assert view["funding"] == personal["funding"]
    assert view["funding"]["projects"][0]["is_military_project"] is False
    assert view["funding"]["projects"][1]["research_type"] is None
    assert ("default_funding" in view) == (default_id is not None)
    for role in (None, "memory", "trip_planner", "policy_rag", "compliance"):
        scoped = profile_for_role(view, role)
        assert scoped["funding"] == original["funding"]
        scoped["funding"]["projects"][0]["name"] = "本地修改"
        assert view["funding"] == original["funding"]
    for role in ("trip_context", "travel_info"):
        assert "funding" not in profile_for_role(view, role)
    assert personal == original


def test_maximum_funding_catalog_is_never_truncated_or_implicitly_selected():
    personal = {"funding": {"projects": [{"id": str(index), "name": f"课题 {index}"} for index in range(20)]}}
    view = profile_from_personal(personal, {}, "2026-10-04")
    assert [p["id"] for p in view["funding"]["projects"]] == [str(index) for index in range(20)]
    assert view["funding"]["default_project_id"] is None and "default_funding" not in view
    no_id = profile_from_personal({"funding": {"projects": [{"name": "未生成编号的项目"}]}}, {}, "2026-10-04")
    assert "default_funding" not in no_id


@pytest.mark.parametrize("identifier", ["13800138000", "202510042345678901", "000012345678901234"])
def test_checkpoint_preserves_funding_identifiers_without_disabling_other_redaction(identifier):
    from agent_runtime.store import safe_checkpoint
    personal = {"funding": {"default_project_id": identifier, "projects": [
        {"id": identifier, "financial_project_code": identifier}]}}
    view = profile_from_personal(personal, {}, "2026-10-04")
    payload = {"user_profile": view, "note": "电话 13800138000，密码是secret-password"}
    saved = safe_checkpoint({**payload, "messages": [{"content": json.dumps(payload, ensure_ascii=False)}]})
    assert saved["user_profile"] == view
    assert json.loads(saved["messages"][0]["content"])["user_profile"] == view
    assert "13800138000" not in saved["note"] and "secret-password" not in saved["note"]


class FakeRepository:
    def __init__(self):
        self.records = {}
        self.calls = []

    def get(self, user_id):
        self.calls.append(("get", user_id))
        return deepcopy(self.records.get(user_id, {"profile": PersonalProfile().model_dump(mode="json"),
            "onboarding_status": "pending", "revision": 0, "updated_at": None}))

    def save(self, user_id, profile, revision):
        current = self.get(user_id)
        if revision != current["revision"]:
            raise ProfileConflict()
        self.records[user_id] = {**current, "profile": profile.model_dump(mode="json"), "revision": revision + 1, "onboarding_status": "completed"}
        return deepcopy(self.records[user_id])

    def skip(self, user_id, revision):
        current = self.get(user_id)
        if revision != current["revision"]:
            raise ProfileConflict()
        self.records[user_id] = {**current, "revision": revision + 1, "onboarding_status": "skipped"}
        return deepcopy(self.records[user_id])


@pytest.fixture
def client():
    repo = FakeRepository()
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(create_personal_profile_router(repo))
    app.dependency_overrides[get_current_user] = lambda: User(1, "qa@example.com", "unused", "unused")
    with TestClient(app) as client:
        yield client, repo


def test_api_initialization_skip_then_settings_save_and_conflict(client):
    client, repo = client
    initial = client.get("/api/1/profile").json()
    assert initial["onboarding_status"] == "pending" and initial["revision"] == 0
    skipped = client.post("/api/1/profile/skip", json={"revision": 0}).json()
    assert skipped["onboarding_status"] == "skipped"
    assert client.get("/api/1/profile").json()["onboarding_status"] == "skipped"
    saved = client.put("/api/1/profile", json={"revision": 1, "profile": staff_profile().model_dump(mode="json")})
    assert saved.status_code == 200
    assert client.get("/api/1/profile").json()["profile"]["basic_info"]["employee_number"] == "0000123"
    assert client.put("/api/1/profile", json={"revision": 1, "profile": {}}).status_code == 409
    assert client.get("/api/1/profile").json()["profile"]["basic_info"]["employee_number"] == "0000123"
    assert client.post("/api/1/profile/skip", json={"revision": 2}).json()["profile"] == saved.json()["profile"]


@pytest.mark.parametrize("method,path,body", [("GET", "/api/2/profile", None), ("PUT", "/api/2/profile", {"profile": {}, "revision": 0}), ("POST", "/api/2/profile/skip", {"revision": 0})])
def test_cross_user_access_denied_before_repository_read(client, method, path, body):
    client, repo = client
    assert client.request(method, path, json=body).status_code == 403
    assert repo.calls == []


def test_api_invalid_relationship_is_rejected_without_logging_private_values(client, caplog):
    client, repo = client
    response = client.put("/api/1/profile", json={"revision": 0, "profile": {
        "basic_info": {"personnel_category": "student", "student_number": "SENSITIVE-STUDENT-NUMBER"},
        "policy_identity": {"staff_grade": 3}}})
    assert response.status_code == 422 and repo.calls == []
    assert "SENSITIVE-STUDENT-NUMBER" not in str([record.__dict__ for record in caplog.records])


@pytest.mark.asyncio
async def test_agent_sees_new_profile_on_next_request_and_preferences_preserve_identity():
    services = FakeServices()
    personal = staff_profile().model_dump(mode="json")
    services.read_personal_profile = lambda scope: deepcopy(personal)
    captured = []
    async def model(messages, **kwargs):
        captured.append(model_payload(messages))
        return {"content": [{"type": "text", "text": "已参考你的报销身份。"}]}
    store = FakeStore(services)
    runtime = Supervisor(model, services, store, CONFIG)
    await runtime.run(SCOPE, "我的科研报销身份是什么")
    personal["policy_identity"]["staff_grade"] = 4
    personal["funding"]["projects"].append(PersonalProfile.model_validate({"funding": {"projects": [
        {"id": "b", "name": "教学经费", "financial_project_code": "000456", "funding_category": "non_research"}]}}).model_dump(mode="json")["funding"]["projects"][0])
    personal["funding"]["default_project_id"] = "b"
    replay = await runtime.run(SCOPE, "我的科研报销身份是什么")
    assert replay["idempotent_replay"] and len(captured) == 1
    assert len(store.rows[(SCOPE.user_id, SCOPE.request_id)]["checkpoint"]["user_profile"]["funding"]["projects"]) == 1
    await runtime.run(SCOPE.model_copy(update={"request_id": "next-personal"}), "再看我的身份")
    assert captured[0]["user_profile"]["identity"]["staff_grade"] == 3
    assert captured[1]["user_profile"]["identity"]["staff_grade"] == 4
    assert captured[1]["user_profile"]["funding"] == personal["funding"]
    turn = Turn(runtime, SCOPE, "偏好", "偏好", None)
    turn.state = {"user_profile": captured[1]["user_profile"]}
    turn.update_user_profile({}, {"home_location": "上海"})
    assert turn.state["user_profile"]["identity"]["staff_grade"] == 4
    assert turn.state["user_profile"]["defaults"]["origin"] == "上海"
    assert turn.state["user_profile"]["funding"] == personal["funding"]
    personal["funding"]["projects"].pop(0)
    await runtime.run(SCOPE.model_copy(update={"request_id": "removed-project"}), "再看我的经费")
    assert captured[2]["user_profile"]["funding"] == personal["funding"]


@pytest.mark.asyncio
async def test_saved_catalog_reaches_reimbursement_specialists_in_actual_runtime(monkeypatch):
    from agent_runtime.profiles import PROFILES
    from settings import RAG_CONFIG
    monkeypatch.setitem(RAG_CONFIG, "search_scopes", ())
    services = FakeServices()
    personal = staff_profile().model_dump(mode="json")
    personal["funding"]["projects"].append(PersonalProfile.model_validate({"funding": {"projects": [
        {"id": "teaching", "name": "教学经费", "funding_category": "non_research"}]}}).model_dump(mode="json")["funding"]["projects"][0])
    services.read_personal_profile = lambda scope: deepcopy(personal)
    seen = {}
    class Model(JourneyModel):
        async def __call__(self, messages, tools, tool_choice):
            if "delegate" not in {t["function"]["name"] for t in tools}:
                role = next(k for k, p in PROFILES.items() if p.instructions in messages[0]["content"])
                seen[role] = json.loads(messages[1]["content"]).get("user_profile", {})
            return await super().__call__(messages, tools, tool_choice)
    result = await Supervisor(Model(), services, FakeStore(services), CONFIG).run(SCOPE, TEXT)
    assert {r["name"] for r in result["agents"]} == set(PROFILES)
    for role in ("policy_rag", "compliance", "trip_planner", "memory"):
        assert seen[role]["funding"] == personal["funding"]
    for role in ("trip_context", "travel_info"):
        assert "funding" not in seen[role]
