"""Tenant Operations Assistant: tenant-scoped RAG over internal docs, answered through OpenRouter.

Retrieval: BM25 over the caller's own chunks plus global chunks. The tenant filter runs before scoring.
Guardrails: cross-tenant mention check, figure check (every % or time figure must appear in retrieved text),
and a per-tenant JSONL log.

Run:  python tenant_ops_agent.py --tenant acme "What is the Enterprise uptime commitment?"
"""
import argparse
import datetime as dt
import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

HERE = Path(__file__).resolve().parent
KB_PATH = HERE / "data" / "knowledge_base.json"
LOG_DIR = HERE / "logs"
ENV_PATH = HERE / ".env"
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"

NOT_COVERED = "The documentation does not cover this question for your tenant."
NOT_DOCUMENTED = "The documentation does not state that. Check your contract or ask your account team."
SYSTEM_PROMPT = (
    "You are the Tenant Operations Assistant for a SaaS operations team. "
    "Answer only from the documentation excerpts provided. "
    f'If they do not answer the question, reply exactly: "{NOT_COVERED}" '
    "Never state an uptime figure, credit, response time, or provisioning time unless an excerpt states it. "
    "If the excerpts do not state it, say the documentation does not state it and point to the contract or account team. "
    "If two excerpts conflict, report both with their IDs. "
    "Say when an excerpt is a draft and not approved. "
    "Cite each claim with its excerpt ID in brackets, for example [sla-01]. "
    "Do not mention any tenant other than the one named in the request."
)

TENANT_ID_RE = re.compile(r"[a-z0-9-]{1,40}")
SCOPE_RE = re.compile(r"tenant:([a-z0-9-]{1,40})")
STATUSES = {"current", "draft"}
STOPWORDS = {"a", "an", "and", "are", "at", "by", "do", "does", "for", "from", "how", "i", "in", "is", "it",
             "me", "my", "of", "on", "or", "the", "to", "what", "which", "with"}
TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
FIGURE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(%|business\s+days?|hours?|minutes?|days?)", re.IGNORECASE)

LLM = Callable[[List[dict]], str]


@dataclass(frozen=True)
class Chunk:
    id: str
    title: str
    section: str
    scope: str
    status: str
    effective: str
    text: str

    @property
    def tenant(self) -> Optional[str]:
        match = SCOPE_RE.fullmatch(self.scope)
        return match.group(1) if match else None


@dataclass
class Hit:
    chunk: Chunk
    score: float


@dataclass
class Answer:
    text: str
    chunk_ids: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


def validate_tenant(tenant_id: str) -> str:
    if not TENANT_ID_RE.fullmatch(tenant_id or ""):
        raise ValueError("tenant_id must be 1 to 40 lowercase letters, digits, or hyphens")
    return tenant_id


def load_chunks(path: Path) -> Tuple[List[Chunk], int]:
    """Keep only chunks with a valid scope and status. Returns (kept, dropped_count)."""
    kept, dropped = [], 0
    for raw in json.loads(path.read_text(encoding="utf-8")):
        chunk = Chunk(**{name: str(raw.get(name, "")) for name in Chunk.__dataclass_fields__})
        valid_scope = chunk.scope == "global" or SCOPE_RE.fullmatch(chunk.scope)
        if not (valid_scope and chunk.status in STATUSES and chunk.text.strip()):
            dropped += 1
            continue
        kept.append(chunk)
    return kept, dropped


def _tokens(text: str) -> List[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS]


def bm25(query: List[str], docs: List[List[str]], k1: float = 1.5, b: float = 0.75) -> List[float]:
    if not docs:
        return []
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df = Counter()
    for d in docs:
        df.update(set(d))
    scores = []
    for d in docs:
        tf = Counter(d)
        s = 0.0
        for t in set(query):
            if tf[t] == 0:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(d) / avgdl))
        scores.append(s)
    return scores


def _figures(text: str) -> Set[Tuple[float, str]]:
    found = set()
    for m in FIGURE_RE.finditer(text):
        unit = re.sub(r"\s+", " ", m.group(2).lower())
        found.add((float(m.group(1)), unit if unit == "%" else unit.rstrip("s")))
    return found


class TenantOpsAgent:
    def __init__(self, chunks: List[Chunk], llm: LLM, log_dir: Path = LOG_DIR):
        self.chunks = chunks
        self.llm = llm
        self.log_dir = log_dir
        self.known_tenants = {c.tenant for c in chunks if c.tenant}

    def visible(self, tenant_id: str) -> List[Chunk]:
        """Global chunks plus this tenant's own chunks. Nothing else is ever considered."""
        return [c for c in self.chunks if c.tenant is None or c.tenant == tenant_id]

    def retrieve(self, question: str, tenant_id: str, k: int = 4) -> List[Hit]:
        validate_tenant(tenant_id)
        pool = self.visible(tenant_id)
        # IDF comes from the visible pool only, so other tenants' term counts cannot shift scores.
        docs = [_tokens(f"{c.title} {c.section} {c.text}") for c in pool]
        scores = bm25(_tokens(question), docs)
        hits = [Hit(c, s) for c, s in zip(pool, scores) if s > 0]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    def answer(self, question: str, tenant_id: str) -> Answer:
        question = question.strip()
        if not 3 <= len(question) <= 1000:
            raise ValueError("question must be 3 to 1000 characters")
        hits = self.retrieve(question, tenant_id)
        if not hits:
            result = Answer(NOT_COVERED, [], ["no_evidence"])
        else:
            result = self._guard(self.llm(self._messages(question, tenant_id, hits)).strip(), hits, tenant_id)
        self._log(tenant_id, question, result)
        return result

    def _messages(self, question: str, tenant_id: str, hits: List[Hit]) -> List[dict]:
        excerpts = "\n\n".join(
            f"[{h.chunk.id}] {h.chunk.title}, {h.chunk.section} "
            f"(status: {h.chunk.status}, effective {h.chunk.effective})\n{h.chunk.text}"
            for h in hits
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Tenant: {tenant_id}\n\nDocumentation excerpts:\n{excerpts}\n\nQuestion: {question}"},
        ]

    def _guard(self, text: str, hits: List[Hit], tenant_id: str) -> Answer:
        chunk_ids = [h.chunk.id for h in hits]
        for other in self.known_tenants - {tenant_id}:
            if re.search(rf"\b{re.escape(other)}\b", text, re.IGNORECASE):
                # Same reply as an unknown tenant, so the answer does not confirm the other tenant exists.
                return Answer(NOT_COVERED, [], ["cross_tenant_mention"])
        context = " ".join(h.chunk.text for h in hits)
        if _figures(text) - _figures(context):
            return Answer(NOT_DOCUMENTED, chunk_ids, ["unsupported_figure"])
        flags = ["draft_source"] if any(h.chunk.status == "draft" for h in hits) else []
        return Answer(text, chunk_ids, flags)

    def _log(self, tenant_id: str, question: str, result: Answer) -> None:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "tenant_id": tenant_id,
            "question": question,
            "chunk_ids": result.chunk_ids,
            "flags": result.flags,
        }
        with open(self.log_dir / f"{tenant_id}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def openrouter_llm(model: str, api_key: str, base_url: str) -> LLM:
    def call(messages: List[dict]) -> str:
        body = json.dumps({"model": model, "messages": messages, "temperature": 0}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "Tenant Operations Assistant",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"OpenRouter returned HTTP {e.code}: {detail}") from e
        return payload["choices"][0]["message"]["content"]

    return call


def main() -> None:
    parser = argparse.ArgumentParser(description="Ask the Tenant Operations Assistant a question.")
    parser.add_argument("--tenant", required=True, help="tenant ID from the calling session")
    parser.add_argument("question")
    args = parser.parse_args()

    load_env()
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is not set. Add it to .env.")
    chunks, dropped = load_chunks(KB_PATH)
    if dropped:
        print(f"warning: {dropped} chunk(s) dropped for invalid scope or status")
    llm = openrouter_llm(
        os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL),
        api_key,
        os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL),
    )
    agent = TenantOpsAgent(chunks, llm)
    try:
        result = agent.answer(args.question, args.tenant)
    except ValueError as e:
        raise SystemExit(str(e))
    print(result.text)
    print(f"\nsources: {', '.join(result.chunk_ids) or 'none'}")
    if result.flags:
        print(f"flags: {', '.join(result.flags)}")


if __name__ == "__main__":
    main()
