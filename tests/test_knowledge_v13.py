import json
import sqlite3
from pathlib import Path

import pytest

from evals.knowledge_run import run_knowledge_eval
from knowledge.service import KnowledgeError, KnowledgeSearchService, chunk_markdown, load_manifest, normalize_query


def _fixture_docs(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge_docs"
    root.mkdir()
    (root / "one.md").write_text("# 换货\n\n教学演示规则：收货后 7 天。", encoding="utf-8")
    (root / "manifest.json").write_text(json.dumps({"documents": [{"doc_id": "one", "title": "换货", "version": "1.0", "effective_date": "2026-01-01", "source": "demo://test/one", "path": "one.md"}]}, ensure_ascii=False), encoding="utf-8")
    return root


def test_manifest_rejects_traversal_and_symlink(tmp_path: Path):
    root = _fixture_docs(tmp_path)
    data = json.loads((root / "manifest.json").read_text())
    data["documents"][0]["path"] = "../outside.md"
    (root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(KnowledgeError):
        load_manifest(root)


def test_chunks_are_deterministic_and_keep_sections():
    text = "# 标题\n\n第一段。\n\n## 子节\n\n第二段。"
    assert chunk_markdown(text) == chunk_markdown(text)
    assert {item["section"] for item in chunk_markdown(text)} == {"标题", "子节"}


def test_sync_is_idempotent_and_two_char_search_works(tmp_path: Path):
    root = _fixture_docs(tmp_path)
    service = KnowledgeSearchService(str(tmp_path / "db.sqlite"), str(root))
    service.initialize_and_sync()
    service.initialize_and_sync()
    with sqlite3.connect(tmp_path / "db.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 1
    assert service.search("换货")


def test_same_version_drift_is_rejected_without_partial_index(tmp_path: Path):
    root = _fixture_docs(tmp_path)
    db_path = str(tmp_path / "db.sqlite")
    service = KnowledgeSearchService(db_path, str(root))
    service.initialize_and_sync()
    (root / "one.md").write_text("# 换货\n\n漂移内容。", encoding="utf-8")
    with pytest.raises(KnowledgeError):
        service.initialize_and_sync()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 1


def test_new_version_is_active_and_old_version_remains(tmp_path: Path):
    root = _fixture_docs(tmp_path)
    service = KnowledgeSearchService(str(tmp_path / "db.sqlite"), str(root))
    service.initialize_and_sync()
    data = json.loads((root / "manifest.json").read_text())
    data["documents"][0]["version"] = "2.0"
    (root / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    service.initialize_and_sync()
    with sqlite3.connect(tmp_path / "db.sqlite") as db:
        rows = db.execute("SELECT version,active FROM knowledge_documents ORDER BY version").fetchall()
    assert rows == [("1.0", 0), ("2.0", 1)]


def test_common_synonyms_are_normalized_without_case_specific_mapping():
    assert normalize_query("退钱") == normalize_query("退款")
    assert normalize_query("包裹") == normalize_query("物流")
    assert normalize_query("换新") == normalize_query("换货")


def test_domain_terms_beat_generic_question_words_and_unrelated_abstains(tmp_path: Path):
    service = KnowledgeSearchService(str(tmp_path / "db.sqlite"), "knowledge_docs")
    service.initialize_and_sync()
    assert service.search("退款需要什么条件", 1)[0].doc_id == "refund-demo-policy"
    assert service.search("天气预报") == []


def test_ranking_is_stable_and_match_input_is_parameterized(tmp_path: Path):
    root = _fixture_docs(tmp_path)
    service = KnowledgeSearchService(str(tmp_path / "db.sqlite"), str(root))
    service.initialize_and_sync()
    first = [hit.as_dict() for hit in service.search('" OR * NOT 换货', 3)]
    second = [hit.as_dict() for hit in service.search('" OR * NOT 换货', 3)]
    assert first == second


def test_unanswerable_is_not_in_recall_denominator_and_gate_is_not_lowered(tmp_path: Path):
    root = _fixture_docs(tmp_path)
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([
        {"case_id": "A", "query": "换货", "answerable": True, "relevant": [{"doc_id": "one", "section": "换货"}]},
        {"case_id": "U", "query": "天气预报", "answerable": False, "relevant": []},
    ], ensure_ascii=False), encoding="utf-8")
    report = run_knowledge_eval(cases, root)
    assert report["answerable_total"] == 1
    assert report["unanswerable_total"] == 1
    assert report["recall_at_1"] == 1.0
    assert report["abstention_accuracy"] == 1.0

    failing = tmp_path / "failing-cases.json"
    failing.write_text(json.dumps([
        {"case_id": "F", "query": "不存在的政策", "answerable": True, "relevant": [{"doc_id": "one", "section": "换货"}]},
    ], ensure_ascii=False), encoding="utf-8")
    failed_report = run_knowledge_eval(failing, root)
    assert failed_report["passed"] is False
    assert failed_report["recall_at_1_gate"] == 0.90
