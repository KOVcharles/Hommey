"""Render clarification; conversation carries questions and replies."""
from .contracts import Finish
from .render import render

CLARIFICATION = "我还不太确定你的意思。你是想查询差旅政策、规划行程，还是查询交通天气？"


def clarification_output(question=CLARIFICATION):
    return {**render(Finish(kind="clarify", question=question), []), "outcome": "waiting_input"}
