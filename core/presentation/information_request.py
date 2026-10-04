"""Bounded, model-selected questions. Answers remain ordinary user messages."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60, pattern=r"^[^\r\n]+$")]


class InformationField(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: ShortText
    input_type: Literal["single_select", "multi_select", "text", "date"]
    options: list[ShortText] = Field(default_factory=list, max_length=8,
        description="选择题的常见选项，2 至 8 个；不填其他/自行填写，由界面提供。文本和日期题留空。")
    required: bool = True
    allow_custom: bool = Field(default=True, description="选择题允许用户补充未列出的答案")
    help_text: str = Field(default="", max_length=160)

    @model_validator(mode="after")
    def valid_options(self):
        if self.input_type in {"single_select", "multi_select"}:
            if len(self.options) < 2 or len(set(self.options)) != len(self.options):
                raise ValueError("选择题需要 2 至 8 个不重复的选项")
        elif self.options:
            raise ValueError("文本和日期题不使用 options")
        return self


class RequestInformation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: ShortText
    description: str = Field(default="", max_length=1200,
        description="先保留已完成的结论，再简述补充信息的用途；可用 Markdown，不臆造制度或资格。")
    fields: list[InformationField] = Field(min_length=1, max_length=5,
        description="只问本次必要的未知信息；不预选用户答案，不收集密码或无关身份资料。")

    @model_validator(mode="after")
    def unique_fields(self):
        if len({field.key for field in self.fields}) != len(self.fields):
            raise ValueError("字段 key 不能重复")
        if len({field.label for field in self.fields}) != len(self.fields):
            raise ValueError("字段名称不能重复")
        return self

    def output(self):
        # Include questions/options in the durable transcript, so plain-text
        # clients and later turns retain the same context as the visible card.
        lines = [self.title]
        if self.description:
            lines.append(self.description)
        for field in self.fields:
            suffix = "（选填）" if not field.required else ""
            choices = " / ".join(field.options)
            if choices and field.allow_custom:
                choices += " / 自行填写"
            lines.append(f"- {field.label}{suffix}" + (f"：{choices}" if choices else ""))
            if field.help_text:
                lines.append(f"  {field.help_text}")
        lines.append("可在卡片中选择后提交，也可以直接回复。")
        text = "\n".join(lines)
        document = {"type": "information_request", **self.model_dump(mode="json"),
            "plain_text": text, "input_placeholder": "也可以直接回复补充信息…"}
        return {"response": text, "answer_document": None,
            "presentation_document": document, "outcome": "waiting_input"}
