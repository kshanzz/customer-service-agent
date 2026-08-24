from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


DEFAULT_KNOWLEDGE_DIR = "/app/knowledge_docs"
MAX_DOCUMENT_BYTES = 256 * 1024
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")

# This is deliberately small and transparent: it normalizes common customer
# wording to the vocabulary used by the demo policy documents.
SYNONYMS: tuple[tuple[str, str], ...] = (
    ("退回换新", "换货"),
    ("返钱", "退款"),
    ("退钱", "退款"),
    ("换新", "换货"),
    ("更换", "换货"),
    ("包裹", "物流"),
    ("快递", "物流"),
)
GENERIC_TERMS = {"什么", "怎么", "可以", "是否", "能否", "哪里", "如何", "多久", "意思", "说明"}
DOMAIN_TERMS = ("换货", "退款", "物流", "投诉", "未收货", "已收货", "已取消", "签收", "运输", "路上", "期限", "条件", "转交", "反馈")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class KnowledgeError(ValueError):
    pass


@dataclass(frozen=True)
class KnowledgeHit:
    citation_id: str
    doc_id: str
    title: str
    version: str
    effective_date: str
    section: str
    excerpt: str
    score: float
    source: str

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_query(query: str) -> str:
    """Normalize user wording without making a case-specific answer mapping."""
    normalized = unicodedata.normalize("NFKC", query).casefold()
    normalized = re.sub(r"[\s\u3000]+", "", normalized)
    normalized = re.sub(r"[，。！？、；：,.!?;:_-]+", "", normalized)
    for source, target in SYNONYMS:
        normalized = normalized.replace(source, target)
    return normalized


def _meaningful_bigrams(normalized: str) -> list[str]:
    return [
        item
        for item in dict.fromkeys(normalized[i : i + 2] for i in range(len(normalized) - 1))
        if item not in GENERIC_TERMS
    ]


def _safe_path(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise KnowledgeError("manifest path must stay inside knowledge_docs")
    lexical = root / relative
    if lexical.is_symlink() or any(part.is_symlink() for part in lexical.parents if part != root.parent):
        raise KnowledgeError("symbolic links are not allowed")
    candidate = lexical.resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise KnowledgeError("manifest path must stay inside knowledge_docs")
    return candidate


def load_manifest(root: str | Path) -> list[dict[str, str]]:
    root_path = Path(root).resolve()
    manifest_path = root_path / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise KnowledgeError("manifest.json is required")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise KnowledgeError("invalid knowledge manifest") from exc
    entries = raw.get("documents") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise KnowledgeError("manifest must contain a documents list")
    required = {"doc_id", "title", "version", "effective_date", "source", "path"}
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required or not all(isinstance(entry[k], str) for k in required):
            raise KnowledgeError("manifest document fields are invalid")
        key = (entry["doc_id"], entry["version"])
        if key in seen:
            raise KnowledgeError("duplicate doc_id and version")
        seen.add(key)
        if not entry["doc_id"].strip() or not entry["title"].strip() or not _VERSION_RE.fullmatch(entry["version"]):
            raise KnowledgeError("invalid document identity or version")
        try:
            date.fromisoformat(entry["effective_date"])
        except ValueError as exc:
            raise KnowledgeError("invalid effective_date") from exc
        if not entry["source"].startswith("demo://"):
            raise KnowledgeError("knowledge source must use demo://")
        path = _safe_path(root_path, entry["path"])
        if not path.is_file() or path.is_symlink():
            raise KnowledgeError("knowledge document is missing or is a symbolic link")
        if path.stat().st_size > MAX_DOCUMENT_BYTES:
            raise KnowledgeError("knowledge document is too large")
        try:
            path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise KnowledgeError("knowledge document must be UTF-8") from exc
        result.append(entry.copy())
    return result


def chunk_markdown(text: str, *, max_chars: int = 700, overlap: int = 60) -> list[dict[str, str | int]]:
    """Deterministically split markdown by headings, then paragraphs."""
    if max_chars < 80 or overlap < 0 or overlap >= max_chars:
        raise ValueError("invalid chunk configuration")
    current = "文档"
    sections: list[tuple[str, str]] = []
    paragraphs: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line.startswith("#") and line.lstrip().startswith("#"):
            if paragraphs:
                sections.append((current, "\n\n".join(paragraphs).strip()))
                paragraphs = []
            current = line.strip().lstrip("#").strip() or current
        elif line.strip():
            paragraphs.append(line.rstrip())
        elif paragraphs:
            sections.append((current, "\n\n".join(paragraphs).strip()))
            paragraphs = []
    if paragraphs:
        sections.append((current, "\n\n".join(paragraphs).strip()))
    chunks: list[dict[str, str | int]] = []
    for section, body in sections:
        if not body:
            continue
        start = 0
        while start < len(body):
            end = min(len(body), start + max_chars)
            if end < len(body):
                boundary = max(body.rfind("\n\n", start, end), body.rfind("。", start, end) + 1)
                if boundary > start + max_chars // 3:
                    end = boundary
            content = body[start:end].strip()
            index = len(chunks)
            chunks.append({"section": section, "content": content, "chunk_index": index})
            if end >= len(body):
                break
            start = max(start + 1, end - overlap)
    return chunks


def initialize_knowledge_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_chunks_fts USING fts5(title, section, content, tokenize='trigram')")
    except sqlite3.OperationalError as exc:
        raise RuntimeError("knowledge search requires SQLite FTS5 trigram support") from exc
    definition = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'knowledge_chunks_fts'").fetchone()
    if definition is None or "trigram" not in (definition[0] or "").lower():
        raise RuntimeError("knowledge search requires SQLite FTS5 trigram support")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS knowledge_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL,
            title TEXT NOT NULL,
            version TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            source TEXT NOT NULL,
            path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(doc_id, version)
        );
        CREATE TABLE IF NOT EXISTS knowledge_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES knowledge_documents(id),
            chunk_id TEXT NOT NULL UNIQUE,
            section TEXT NOT NULL,
            content TEXT NOT NULL,
            chunk_index INTEGER NOT NULL
        );
    """)


class KnowledgeSearchService:
    def __init__(self, db_path: str, knowledge_dir: str = DEFAULT_KNOWLEDGE_DIR, *, max_chars: int = 700, overlap: int = 60) -> None:
        if not db_path or not Path(db_path).name:
            raise ValueError("AGENT_DB_PATH is required when knowledge is enabled")
        self.db_path, self.knowledge_dir = db_path, knowledge_dir
        self.max_chars, self.overlap = max_chars, overlap

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize_and_sync(self) -> None:
        entries = load_manifest(self.knowledge_dir)
        conn = self._connect()
        try:
            with conn:
                conn.execute("BEGIN")
                initialize_knowledge_schema(conn)
                root = Path(self.knowledge_dir).resolve()
                for entry in entries:
                    path = _safe_path(root, entry["path"])
                    content_bytes = path.read_bytes()
                    content_hash = _sha256(content_bytes)
                    existing = conn.execute("SELECT * FROM knowledge_documents WHERE doc_id = ? AND version = ?", (entry["doc_id"], entry["version"])).fetchone()
                    if existing is not None:
                        if existing["content_hash"] != content_hash or any(existing[k] != entry[k] for k in ("title", "effective_date", "source", "path")):
                            raise KnowledgeError("same version content or metadata changed; bump version")
                        continue
                    cur = conn.execute("INSERT INTO knowledge_documents (doc_id,title,version,effective_date,source,path,content_hash,active) VALUES (?,?,?,?,?,?,?,1)", (entry["doc_id"], entry["title"], entry["version"], entry["effective_date"], entry["source"], entry["path"], content_hash))
                    document_id = cur.lastrowid
                    for item in chunk_markdown(content_bytes.decode("utf-8"), max_chars=self.max_chars, overlap=self.overlap):
                        chunk_id = _sha256(f"{entry['doc_id']}\0{entry['version']}\0{item['chunk_index']}\0{item['section']}\0{item['content']}".encode())
                        cur = conn.execute("INSERT INTO knowledge_chunks (document_id,chunk_id,section,content,chunk_index) VALUES (?,?,?,?,?)", (document_id, chunk_id, item["section"], item["content"], item["chunk_index"]))
                        conn.execute("INSERT INTO knowledge_chunks_fts (rowid,title,section,content) VALUES (?,?,?,?)", (cur.lastrowid, entry["title"], item["section"], item["content"]))
                    versions = conn.execute("SELECT id, version FROM knowledge_documents WHERE doc_id = ?", (entry["doc_id"],)).fetchall()
                    active_id = max(versions, key=lambda row: _version_key(row["version"]))["id"]
                    conn.execute("UPDATE knowledge_documents SET active = CASE WHEN id = ? THEN 1 ELSE 0 END WHERE doc_id = ?", (active_id, entry["doc_id"]))
        finally:
            conn.close()

    @staticmethod
    def _match_expression(query: str) -> str:
        normalized = normalize_query(query)
        terms = [normalized[i:i + 3] for i in range(len(normalized) - 2)]
        if not terms:
            return '"' + normalized.replace('"', '""') + '"'
        return " OR ".join('"' + term.replace('"', '""') + '"' for term in dict.fromkeys(terms))

    def search(self, query: str, top_k: int = 3) -> list[KnowledgeHit]:
        query = query.strip()
        if not 2 <= len(query) <= 500:
            raise ValueError("query length must be between 2 and 500")
        top_k = max(1, min(5, int(top_k)))
        normalized = normalize_query(query)
        if len(normalized) < 2:
            raise ValueError("query length must be between 2 and 500")
        conn = self._connect()
        try:
            if len(query) < 3:
                rows = conn.execute("SELECT c.chunk_id,d.doc_id,d.title,d.version,d.effective_date,c.section,c.content,c.chunk_index,d.source,0.0 AS rank FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id WHERE d.active=1 AND (d.title LIKE ? OR c.section LIKE ? OR c.content LIKE ?) ORDER BY d.doc_id,d.version,c.chunk_index LIMIT ?", (f"%{normalized}%", f"%{normalized}%", f"%{normalized}%", top_k * 10)).fetchall()
            else:
                rows = conn.execute("SELECT c.chunk_id,d.doc_id,d.title,d.version,d.effective_date,c.section,c.content,c.chunk_index,d.source,bm25(knowledge_chunks_fts, 8.0, 5.0, 1.0) AS rank FROM knowledge_chunks_fts JOIN knowledge_chunks c ON c.id=knowledge_chunks_fts.rowid JOIN knowledge_documents d ON d.id=c.document_id WHERE d.active=1 AND knowledge_chunks_fts MATCH ? ORDER BY rank ASC,d.doc_id ASC,d.version ASC,c.chunk_index ASC LIMIT ?", (self._match_expression(query), top_k * 10)).fetchall()
                # A long natural-language question can contain no complete trigram
                # from a short policy phrase. Use a bounded, parameterized bigram
                # fallback only after FTS returns nothing; it is not a full-table
                # scan and remains restricted to active documents.
                if not rows:
                    bigrams = _meaningful_bigrams(normalized)[:24]
                    if not bigrams:
                        return []
                    clauses = " OR ".join("(d.title LIKE ? OR c.section LIKE ? OR c.content LIKE ?)" for _ in bigrams)
                    params = [value for bigram in bigrams for value in (f"%{bigram}%",) * 3]
                    rows = conn.execute(f"SELECT c.chunk_id,d.doc_id,d.title,d.version,d.effective_date,c.section,c.content,c.chunk_index,d.source,0.0 AS rank FROM knowledge_chunks c JOIN knowledge_documents d ON d.id=c.document_id WHERE d.active=1 AND ({clauses}) ORDER BY d.doc_id,d.version,c.chunk_index LIMIT ?", (*params, top_k * 10)).fetchall()

            query_bigrams = _meaningful_bigrams(normalized)
            query_trigrams = list(dict.fromkeys(normalized[i : i + 3] for i in range(len(normalized) - 2)))
            ranked: list[tuple[float, sqlite3.Row]] = []
            for row in rows:
                title = normalize_query(row["title"])
                section = normalize_query(row["section"])
                content = normalize_query(row["content"])
                fields = ((title, 8.0), (section, 5.0), (content, 1.0))
                field_score = sum(weight for term in query_bigrams for value, weight in fields if term in value)
                domain_score = sum(
                    (24.0 if term in title else 14.0 if term in section else 6.0)
                    for term in DOMAIN_TERMS
                    if term in normalized and term in (title + section + content)
                )
                trigram_coverage = (
                    sum(1 for term in query_trigrams if term in (title + section + content)) / len(query_trigrams)
                    if query_trigrams
                    else 0.0
                )
                bm25_bonus = 1.0 / (1.0 + max(float(row["rank"]), 0.0))
                score = field_score + domain_score + (4.0 * trigram_coverage) + bm25_bonus
                ranked.append((score, row))

            # A score threshold makes abstention explicit for unrelated text;
            # generic interrogatives alone can never create relevance.
            ranked = [item for item in ranked if item[0] >= 2.0]
            ranked.sort(key=lambda item: (-item[0], item[1]["doc_id"], item[1]["version"], item[1]["section"], item[1]["chunk_index"]))
            hits: list[KnowledgeHit] = []
            seen_sections: set[tuple[str, str]] = set()
            for score, row in ranked:
                section_key = (row["doc_id"], row["section"])
                if section_key in seen_sections:
                    continue
                seen_sections.add(section_key)
                hits.append(KnowledgeHit("cite-" + row["chunk_id"][:16], row["doc_id"], row["title"], row["version"], row["effective_date"], row["section"], row["content"], score, row["source"]))
                if len(hits) >= top_k:
                    break
            return hits
        finally:
            conn.close()
