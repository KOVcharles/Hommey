"""User-entered background facts, independent of authentication and travel memory."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ShortText = Annotated[str, Field(max_length=120)]
Identifier = Annotated[str, Field(max_length=64, min_length=1)]


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BasicInfo(ProfileModel):
    real_name: ShortText | None = None
    institution: ShortText | None = "重庆大学"
    department: ShortText | None = None
    personnel_category: Literal["staff", "student", "external", "other"] | None = None
    employee_number: Identifier | None = None
    student_number: Identifier | None = None
    gender: Literal["male", "female", "other", "undisclosed"] | None = None
    birth_date: date | None = None
    nationality: ShortText | None = None

    @model_validator(mode="after")
    def check_birth_date(self):
        today = datetime.now(timezone(timedelta(hours=8))).date()
        if self.birth_date and (self.birth_date > today or self.birth_date.year < 1900):
            raise ValueError("出生日期应在 1900 年至今天之间")
        return self


class PolicyIdentity(ProfileModel):
    professional_title_level: Literal["senior", "associate_senior", "intermediate", "junior", "none"] | None = None
    professional_position_grade: Annotated[int, Field(strict=True, ge=1, le=13)] | None = None
    staff_grade: Annotated[int, Field(strict=True, ge=1, le=10)] | None = None
    administrative_rank: Literal["provincial_ministerial", "department_bureau", "other"] | None = None
    academician_status: Literal["academician", "equivalent", "none"] | None = None
    nationally_recognized_expert: Annotated[bool, Field(strict=True)] | None = None

    @model_validator(mode="after")
    def check_position(self):
        if self.professional_title_level == "none" and self.professional_position_grade is not None:
            raise ValueError("无专业技术职称时无需填写专业技术岗位等级")
        return self


class FundingProject(ProfileModel):
    id: Identifier | None = None
    name: ShortText | None = None
    financial_project_code: Identifier | None = None
    funding_category: Literal["research", "non_research"] | None = None
    research_type: Literal["vertical", "horizontal"] | None = None
    program_type: Literal["national_science_technology", "national_social_science", "other"] | None = None
    funding_source: Literal["fiscal", "non_fiscal"] | None = None
    is_military_project: Annotated[bool, Field(strict=True)] | None = None
    user_project_role: Literal["principal", "member", "handler"] | None = None

    @model_validator(mode="after")
    def check_research(self):
        if self.funding_category != "research" and any(value is not None for value in
                (self.research_type, self.program_type, self.is_military_project)):
            raise ValueError("科研项目选项仅适用于科研经费")
        return self


class Funding(ProfileModel):
    default_project_id: Identifier | None = None
    projects: list[FundingProject] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_default(self):
        ids = [project.id for project in self.projects if project.id]
        if len(ids) != len(set(ids)):
            raise ValueError("项目标识不能重复")
        if self.default_project_id and self.default_project_id not in ids:
            raise ValueError("默认经费必须来自常用项目列表")
        return self


class Settlement(ProfileModel):
    has_official_card: Annotated[bool, Field(strict=True)] | None = None


class PersonalProfile(ProfileModel):
    schema_version: Literal[1] = 1
    basic_info: BasicInfo = Field(default_factory=BasicInfo)
    policy_identity: PolicyIdentity = Field(default_factory=PolicyIdentity)
    funding: Funding = Field(default_factory=Funding)
    settlement: Settlement = Field(default_factory=Settlement)

    @model_validator(mode="after")
    def check_personnel(self):
        category = self.basic_info.personnel_category
        identity = self.policy_identity.model_dump()
        if category in {None, "student", "other"} and any(v is not None for v in identity.values()):
            raise ValueError("学生、其他或未选择身份时无需填写职称及职员等级")
        if category != "staff" and any(identity[key] is not None for key in ("staff_grade", "administrative_rank")):
            raise ValueError("职员等级与行政级别仅适用于教职工")
        return self


class SavePersonalProfile(ProfileModel):
    profile: PersonalProfile
    revision: int = Field(ge=0, strict=True)


class SkipPersonalProfile(ProfileModel):
    revision: int = Field(ge=0, strict=True)
