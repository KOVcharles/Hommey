"""Infer policy categories from source filenames."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional


DEFAULT_CATEGORY_MAPPING = {
    "财务报销": "reimbursement_policy",
    "policy_authority": "policy_governance",
    "travel_standards": "travel_policy",
    "international_travel": "international_travel_policy",
    "exception_approval": "exception_policy",
    "rag_validation": "validation_scenarios",
    "reimbursement_policy": "reimbursement_policy",
    "booking_guide": "booking_guide",
    "faq": "faq",
    "emergency_procedures": "emergency_procedures",
    "platform_guide": "platform_guide",
    "city_specific_tips": "city_guide",
    "environmental_initiatives": "environmental_initiatives",
}


def infer_category(path: Path, mapping: Optional[Dict[str, str]] = None) -> str:
    mapping = mapping or DEFAULT_CATEGORY_MAPPING
    stem = path.stem
    for key, category in mapping.items():
        if key in stem:
            return category
    return "business_travel"
