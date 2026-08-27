# -*- coding: utf-8 -*-
"""检索冒烟测试：验证知识库能命中测试文档（供 docker exec 调用）。"""
import sys

sys.path.insert(0, "/app")

from rag.config import RAGPipelineConfig
from rag.pipeline import RAGPipeline

QUERIES = [
    "广州的出差标准是什么",
    "住宿限额是多少",
    "餐费补贴标准",
    "报销需要什么票据",
    "国际出差住宿标准",
    "住宿超标怎么审批",
]


def main() -> int:
    config = RAGPipelineConfig.from_settings()
    pipeline = RAGPipeline(config=config)
    try:
        for q in QUERIES:
            print("\n=== 查询:", q, "===")
            results = pipeline.query(q, top_k=3)
            for r in results:
                meta = r.metadata or {}
                title = meta.get("title") or meta.get("filename") or "?"
                cat = meta.get("category") or "?"
                snippet = (r.content or "").replace("\n", " ")[:90]
                print(f"  [{cat}] {title}  | {snippet}")
    finally:
        pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
