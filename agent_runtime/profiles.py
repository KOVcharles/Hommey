"""Role registry; human-editable instructions live in prompts/*.md."""
from dataclasses import dataclass
import hashlib
from pathlib import Path

PROMPT_ROOT = Path(__file__).with_name("prompts")
PROMPT_HASHES = {}


def load_prompt(name):
    content = (PROMPT_ROOT / name).read_bytes()
    PROMPT_HASHES[name] = hashlib.sha256(content).hexdigest()
    return content.decode("utf-8").strip()


@dataclass(frozen=True)
class Profile:
    title: str
    instructions: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]


PROFILES = {
    'trip_context': Profile('行程信息整理', load_prompt("specialists/trip_context.md"), (), ('event-collection',)),
    'policy_rag': Profile('制度检索', load_prompt("specialists/policy_rag.md"), ('search_policy', 'read_source'), ('ask-question',)),
    'memory': Profile('个人差旅记忆', load_prompt("specialists/memory.md"), ('search_memory', 'read_source'), ('memory-query', 'preference')),
    'travel_info': Profile('出行信息查询', load_prompt("specialists/travel_info.md"), ('search_trains', 'get_weather', 'find_hotels', 'search_commute', 'read_source'), ('query-info', 'train-query', 'place-query')),
    'trip_planner': Profile('行程规划', load_prompt("specialists/trip_planner.md"), (), ('plan-trip',)),
    'compliance': Profile('合规检查', load_prompt("specialists/compliance.md"), (), ('check-trip-compliance',)),
}

BASE_RULES = load_prompt("specialist-common.md")
MAIN_RULES = load_prompt("main.md")
