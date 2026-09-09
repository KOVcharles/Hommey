"""Display catalog for specialist roles; no classifier or execution plans."""
from typing import FrozenSet
from agent_runtime.profiles import PROFILES

CHITCHAT_EXACT: FrozenSet[str] = frozenset({
    "你好", "您好", "嗨", "哈喽",
    "hi", "hello", "hey",
    "在吗", "在不在", "有人吗",
    "谢谢", "感谢", "多谢",
    "再见", "拜拜", "bye",
    "ok", "okay", "好的",
    "哈哈", "呵呵", "没事", "没什么", "算了",
    "回头见", "明天见", "下次见",
    "thanks", "thank",
})

def intent_api_payload():
    return {name: {"display": profile.title, "description": profile.instructions,
                   "progress_key": "task_running", "skill": profile.skills[0]}
            for name, profile in PROFILES.items()}
