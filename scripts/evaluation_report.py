#!/usr/bin/env python
"""Print a bounded Markdown evaluation report."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from evaluation.report import build_markdown
from evaluation.repository import EvaluationRepository


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    repository = EvaluationRepository()
    try:
        print(build_markdown(repository.report_rows(days=args.days)), end="")
    finally:
        repository.close()
