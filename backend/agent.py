import os, operator, psycopg
from datetime import date, timedelta
from pathlib import Path
from pydantic import BaseModel, Field
from psycopg.rows import dict_row
from typing import TypedDict, List, Optional, Literal, Annotated

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_groq import ChatGroq
from dotenv import load_dotenv

load_dotenv()

from backend.db import get_database
from backend.prompt import (
    ROUTER_SYSTEM,
    RESEARCH_SYSTEM,
    ORCH_SYSTEM,
    WORKER_SYSTEM,
    DECIDE_IMAGES_SYSTEM,
)
from backend.schema import (
    Task,
    Plan,
    EvidenceItem,
    RouterDecision,
    EvidencePack,
    ImageSpec,
    GlobalImagePlan,
)

## LLM
llm = ChatGroq(
    model="openai/gpt-oss-20b", 
    temperature=0.5, 
    streaming=True
)


class State(TypedDict):
    topic: str

    # routing / research
    mode: str
    needs_research: bool
    queries: List[str]
    evidence: List[EvidenceItem]
    plan: Optional[Plan]

    # workers
    sections: Annotated[List[tuple[int, str]], operator.add]

    # reducer/image
    merged_md: str
    md_with_placeholders: str
    image_specs: List[dict]
    final: str


## Router Node
def router_node(state: State) -> dict:

    topic = state["topic"]
    decider = llm.with_structured_output(RouterDecision)
    decision = decider.invoke(
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Topic: {topic}"),
        ]
    )

    return {
        "needs_research": decision.needs_research,
        "mode": decision.mode,
        "queries": decision.queries,
    }


def route_next(state: State) -> str:
    return "research" if state["needs_research"] else "orchestrator"


## Tavily Search
def tavily_search(query: str, max_results: int = 5) -> List[dict]:
    tool = TavilySearchResults(max_results=max_results)
    results = tool.invoke({"query": query})
    normalized: List[dict] = []
    for r in results or []:
        normalized.append(
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("content") or r.get("snippet") or "",
                "published_at": r.get("published_date") or r.get("published_at"),
                "source": r.get("source"),
            }
        )
    return normalized


## Research Node
def research_node(state: State) -> dict:
    queries = state.get("queries", []) or []
    max_results = 5
    raw_results = List[dict] = []

    for q in queries:
        raw_results.extend(tavily_search(q, max_results=max_results))

    if not raw_results:
        return {"evidence": []}

    extractor = llm.with_structured_output(EvidencePack)
    pack = extractor.invoke(
        [
            SystemMessage(content=RESEARCH_SYSTEM),
            HumanMessage(content=f"Raw results:\n{raw_results}"),
        ]
    )
    # Deduplicate by URL
    dedup = {}
    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e
    return {"evidence": list(dedup.values())}


## Orchestrator Node 
