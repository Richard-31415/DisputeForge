"""Policy RAG tool — LlamaIndex VectorStoreIndex over data/policy/.

Document loading:
  - .pdf files: LlamaParse (agentic tier) if LLAMA_CLOUD_API_KEY is set,
                otherwise falls back to SimpleDirectoryReader
  - .txt files: SimpleDirectoryReader directly

Embeddings: OpenAI text-embedding-ada-002 (requires OPENAI_API_KEY).
If OPENAI_API_KEY is missing, retrieve_policy returns "" and the Planner
falls back to its inline Reg E rules — graceful degradation, no crash.

The VectorStoreIndex is built once per process and cached as a singleton.
"""
from __future__ import annotations

import logging
import os
import pathlib
from typing import Any

from pydantic import BaseModel

from harness.tools.base import tool

log = logging.getLogger(__name__)

POLICY_DIR = pathlib.Path(__file__).resolve().parents[2] / "data" / "policy"

_retriever = None


def _load_documents() -> list[Any]:
    from llama_index.core import SimpleDirectoryReader

    docs = []
    for f in sorted(POLICY_DIR.iterdir()):
        if f.suffix == ".pdf" and os.environ.get("LLAMA_CLOUD_API_KEY"):
            try:
                from llama_parse import LlamaParse
                parser = LlamaParse(result_type="markdown", verbose=False)
                parsed = parser.load_data(str(f))
                docs.extend(parsed)
                log.info("policy_rag: parsed %s via LlamaParse (%d docs)", f.name, len(parsed))
                continue
            except Exception as e:
                log.warning("policy_rag: LlamaParse failed for %s: %s — falling back", f.name, e)
        if f.suffix in (".pdf", ".txt", ".md"):
            fallback = SimpleDirectoryReader(input_files=[str(f)]).load_data()
            docs.extend(fallback)
            log.info("policy_rag: loaded %s via SimpleDirectoryReader (%d docs)", f.name, len(fallback))
    return docs


def _build_retriever():
    global _retriever
    if _retriever is not None:
        return _retriever

    if not os.environ.get("OPENAI_API_KEY"):
        log.warning("policy_rag: OPENAI_API_KEY not set — RAG disabled, planner uses inline rules")
        return None

    if not POLICY_DIR.exists():
        log.warning("policy_rag: data/policy/ not found — RAG disabled")
        return None

    try:
        from llama_index.core import VectorStoreIndex
        from llama_index.core.settings import Settings
        from llama_index.embeddings.openai import OpenAIEmbedding

        Settings.embed_model = OpenAIEmbedding(model="text-embedding-ada-002")
        Settings.llm = None  # retrieval only — no LlamaIndex LLM synthesis step

        docs = _load_documents()
        if not docs:
            log.warning("policy_rag: no documents loaded from %s", POLICY_DIR)
            return None

        index = VectorStoreIndex.from_documents(docs)
        _retriever = index.as_retriever(similarity_top_k=3)
        log.info("policy_rag: index built from %d document chunks", len(docs))
    except Exception as e:
        log.warning("policy_rag: failed to build index: %s", e)
        _retriever = None

    return _retriever


class RetrievePolicyInput(BaseModel):
    query: str


@tool("retrieve_policy", RetrievePolicyInput)
def retrieve_policy(query: str) -> str:
    """Retrieve relevant Reg E policy passages for the given query string."""
    retriever = _build_retriever()
    if retriever is None:
        return ""
    try:
        nodes = retriever.retrieve(query)
        return "\n\n---\n\n".join(n.get_content() for n in nodes)
    except Exception as e:
        log.warning("policy_rag: retrieve failed: %s", e)
        return ""
