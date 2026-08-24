from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from knowledge import KnowledgeSearchService


CASES = Path(__file__).with_name("knowledge_cases.json")
DOCS = Path(__file__).parents[1] / "knowledge_docs"
RECALL1_GATE = 0.90
RECALL3_GATE = 1.00
MRR_GATE = 0.90
ABSTENTION_GATE = 1.00


def _expected(case: dict[str, Any]) -> list[dict[str, str]]:
    if "relevant" in case:
        return case["relevant"]
    return [{"doc_id": doc_id, "section": ""} for doc_id in case.get("relevant_doc_ids", [])]


def _failure_reason(answerable: bool, hits: list[dict[str, str]], correct_rank: int | None) -> str | None:
    if not answerable:
        return "false_positive" if hits else None
    if not hits:
        return "no_result"
    if correct_rank is None:
        return "wrong_document_or_section"
    if correct_rank > 1:
        return "ranking"
    return None


def run_knowledge_eval(cases_path: Path = CASES, docs_path: Path = DOCS) -> dict[str, Any]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="knowledge-eval-") as directory:
        service = KnowledgeSearchService(str(Path(directory) / "eval.db"), str(docs_path))
        service.initialize_and_sync()
        audits: list[dict[str, Any]] = []
        answerable_ranks: list[int | None] = []
        unanswerable_total = 0
        abstentions = 0
        for case in cases:
            expected = _expected(case)
            answerable = bool(case["answerable"])
            hits = service.search(case["query"], 3)
            actual = [{"doc_id": hit.doc_id, "section": hit.section} for hit in hits]
            correct_rank = next((index for index, hit in enumerate(actual, 1) if hit in expected), None)
            if answerable:
                answerable_ranks.append(correct_rank)
            else:
                unanswerable_total += 1
                if not hits:
                    abstentions += 1
            failure_reason = _failure_reason(answerable, actual, correct_rank)
            audits.append({"case_id": case["case_id"], "query": case["query"], "answerable": answerable, "expected": expected, "actual_top3": actual, "correct_rank": correct_rank, "failure_reason": failure_reason, "passed": failure_reason is None})

        answerable_total = len(answerable_ranks)
        recall1 = sum(rank == 1 for rank in answerable_ranks) / answerable_total if answerable_total else 0.0
        recall3 = sum(rank is not None and rank <= 3 for rank in answerable_ranks) / answerable_total if answerable_total else 0.0
        mrr = sum(1 / rank for rank in answerable_ranks if rank is not None) / answerable_total if answerable_total else 0.0
        zero_result_rate = sum(not audit["actual_top3"] for audit in audits) / len(audits) if audits else 0.0
        abstention_accuracy = abstentions / unanswerable_total if unanswerable_total else 1.0
        passed = recall1 >= RECALL1_GATE and recall3 >= RECALL3_GATE and mrr >= MRR_GATE and abstention_accuracy >= ABSTENTION_GATE
        return {"total": len(audits), "answerable_total": answerable_total, "unanswerable_total": unanswerable_total, "recall_at_1": recall1, "recall_at_3": recall3, "mrr": mrr, "zero_result_rate": zero_result_rate, "abstention_accuracy": abstention_accuracy, "recall_at_1_gate": RECALL1_GATE, "recall_at_3_gate": RECALL3_GATE, "mrr_gate": MRR_GATE, "abstention_accuracy_gate": ABSTENTION_GATE, "passed": passed, "failures": [audit for audit in audits if not audit["passed"]], "cases": audits}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic knowledge retrieval evaluation")
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_knowledge_eval(args.cases)
    print("knowledge eval audit:")
    for case in report["cases"]:
        print(f" - {case['case_id']} query={case['query']} expected={case['expected']} actual_top3={case['actual_top3']} correct_rank={case['correct_rank']} failure_reason={case['failure_reason'] or 'none'}")
    print("knowledge eval summary:")
    for key in ("answerable_total", "unanswerable_total"):
        print(f"  {key}: {report[key]}")
    for key in ("recall_at_1", "recall_at_3", "mrr", "abstention_accuracy", "zero_result_rate"):
        print(f"  {key}: {report[key]:.2%}")
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
