from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from grounded_answer import GroundedAnswer
from knowledge import KnowledgeHit
from schemas import ConversationState, IntentResult
from workflow import process_message


ABSTENTION_MESSAGE = "当前知识库中没有找到足够依据，请换一种方式描述或联系人工客服。"


def _hit(citation_id: str, domain: str) -> KnowledgeHit:
    return KnowledgeHit(citation_id, domain, domain, "1.0", "2026-01-01", "政策", "本地演示政策片段", 5.0, f"demo://{domain}")


@dataclass
class GroundedEvalReport:
    total: int
    passed: int
    citation_validity: float
    tool_boundary_pass_rate: float
    abstention_pass_rate: float

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def run_grounded_workflow_eval() -> dict[str, Any]:
    queries = [
        ("换货期限多久", "exchange"),
        ("退款条件是什么", "refund"),
        ("已签收是什么意思", "logistics"),
        ("投诉怎么处理", "complaint"),
    ]
    calls: dict[str, int] = {"search": 0, "answer": 0, "business": 0}

    def search(query: str, top_k: int) -> list[KnowledgeHit]:
        calls["search"] += 1
        return [_hit("cite-" + str(calls["search"]), query)] if query != "无依据" else []

    def answer(query: str, hits: list[KnowledgeHit]) -> GroundedAnswer:
        calls["answer"] += 1
        return GroundedAnswer(answer=f"依据政策回答：{query}", citation_ids=[hits[0].citation_id])

    passed = 0
    for query, domain in queries:
        state = process_message(
            ConversationState(),
            query,
            lambda _message, domain=domain: IntentResult(intent=domain, request_kind="information"),
            lambda _order: calls.__setitem__("business", calls["business"] + 1),
            lambda _order, _reason: calls.__setitem__("business", calls["business"] + 1),
            lambda _order, _reason: calls.__setitem__("business", calls["business"] + 1),
            search,
            answer,
        )
        passed += state.status == "answered" and len(state.knowledge_citations) == 1

    abstained = process_message(
        ConversationState(),
        "无依据",
        lambda _message: IntentResult(intent="complaint", request_kind="information"),
        knowledge_search=search,
        knowledge_answerer=answer,
    )
    abstention_passed = abstained.assistant_message == ABSTENTION_MESSAGE and calls["answer"] == 4
    report = GroundedEvalReport(
        total=5,
        passed=passed + abstention_passed,
        citation_validity=1.0 if passed == 4 else 0.0,
        tool_boundary_pass_rate=1.0 if calls["business"] == 0 else 0.0,
        abstention_pass_rate=1.0 if abstention_passed else 0.0,
    )
    result = report.as_dict()
    result["passed_all"] = report.passed == report.total
    return result


def main() -> int:
    report = run_grounded_workflow_eval()
    print("grounded workflow eval:")
    for key, value in report.items():
        print(f"  {key}: {value:.2%}" if isinstance(value, float) else f"  {key}: {value}")
    return 0 if report["passed_all"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
