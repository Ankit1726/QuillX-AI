import os, operator, psycopg
from psycopg.rows import dict_row
from typing import TypedDict, List, Optional, Annotated

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.postgres import PostgresSaver

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.rate_limiters import InMemoryRateLimiter
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
)
from backend.schema import (
    Task,
    Plan,
    EvidenceItem,
    RouterDecision,
    EvidencePack,
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Add it to your .env file before starting the server."
        )
    return value


_require_env("GROQ_API_KEY")
_require_env("TAVILY_API_KEY")


## LLM
_rate_limiter = InMemoryRateLimiter(
    requests_per_second=0.15,
    check_every_n_seconds=0.1,
    max_bucket_size=2,
)

llm = ChatGroq(
    model="openai/gpt-oss-120b",
    temperature=0.5,
    streaming=True,
    rate_limiter=_rate_limiter,
    max_retries=6,
)

llm_structured = ChatGroq(
    model="llama-3.3-70b-versatile",
    temperature=0,
    streaming=False,
    rate_limiter=_rate_limiter,
    max_retries=5,
)


def invoke_structured_with_retry(structured_runnable, messages, attempts: int = 5):
    """
    Even with streaming off, Groq's tool-calling can occasionally still
    return a malformed/incomplete tool call (a transient model issue,
    not something we can fix client-side). Retry a few times before
    giving up, since a repeat call usually succeeds.
    """
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return structured_runnable.invoke(messages)
        except Exception as error:

            last_error = error
            message = str(error)
            is_tool_call_glitch = (
                "Tool call validation failed" in message
                or "Failed to parse tool call arguments" in message
                or "tool_use_failed" in message
            )
            if not is_tool_call_glitch or attempt == attempts:
                raise
            import time

            time.sleep(1.5 * attempt)
    raise last_error  # pragma: no cover


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

    # reducer
    merged_md: str
    final: str


## Router Node
def router_node(state: State) -> dict:

    topic = state["topic"]
    decider = llm_structured.with_structured_output(RouterDecision)
    decision = invoke_structured_with_retry(
        decider,
        [
            SystemMessage(content=ROUTER_SYSTEM),
            HumanMessage(content=f"Topic: {topic}"),
        ],
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
    try:
        tool = TavilySearchResults(max_results=max_results)
        results = tool.invoke({"query": query})
    except Exception as error:
        raise RuntimeError(
            f"Tavily search failed for query '{query}': {error}"
        ) from error

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
def _trim_raw_results(
    raw_results: List[dict],
    max_items: int = 12,
    max_snippet_chars: int = 400,
) -> List[dict]:
    """
    Keep only what the extractor actually needs, and cap snippet
    length, so we don't blow past the model's tokens-per-minute
    limit (Groq free/on-demand tiers are easy to exceed if you
    dump full Tavily snippets from multiple queries verbatim).
    """
    trimmed: List[dict] = []
    for item in raw_results[:max_items]:
        snippet = (item.get("snippet") or "")[:max_snippet_chars]
        trimmed.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": snippet,
                "published_at": item.get("published_at"),
            }
        )
    return trimmed


def research_node(state: State) -> dict:
    queries = state.get("queries", []) or []
    # Fewer results per query + fewer queries = fewer tokens.
    max_results = 3
    raw_results: List[dict] = []

    for q in queries:
        try:
            raw_results.extend(tavily_search(q, max_results=max_results))
        except RuntimeError:
            continue

    if not raw_results:
        return {"evidence": []}
    trimmed_results = _trim_raw_results(raw_results)

    extractor = llm_structured.with_structured_output(EvidencePack)
    try:
        pack = invoke_structured_with_retry(
            extractor,
            [
                SystemMessage(content=RESEARCH_SYSTEM),
                HumanMessage(content=f"Raw results:\n{trimmed_results}"),
            ],
        )
    except Exception as error:
        if "413" in str(error) or "rate_limit_exceeded" in str(error):
            return {"evidence": []}
        raise
    # Deduplicate by URL
    dedup = {}
    for e in pack.evidence:
        if e.url:
            dedup[e.url] = e
    return {"evidence": list(dedup.values())}


## Orchestrator Node
MIN_SECTION_WORDS = 507


def _extend_plan_lengths(plan: Plan) -> Plan:
    for task in plan.tasks:
        if not task.target_words or task.target_words < MIN_SECTION_WORDS:
            task.target_words = MIN_SECTION_WORDS
    return plan


def orchestrator_node(state: State) -> dict:
    planner = llm_structured.with_structured_output(Plan)
    evidence = state.get("evidence", [])
    mode = state.get("mode", "closed_book")
    plan = invoke_structured_with_retry(
        planner,
        [
            SystemMessage(content=ORCH_SYSTEM),
            HumanMessage(
                content=(
                    f"Topic: {state['topic']}\n"
                    f"Mode: {mode}\n\n"
                    f"Evidence (ONLY use for fresh claims; may be empty):\n"
                    f"{[e.model_dump() for e in evidence][:16]}"
                )
            ),
        ],
    )
    plan = _extend_plan_lengths(plan)
    return {"plan": plan}


## Fanout
def fanout(state: State):
    plan = state.get("plan")
    if plan is None or not plan.tasks:
        raise RuntimeError(
            "orchestrator produced a plan with no tasks; cannot fan out."
        )

    return [
        Send(
            "worker",
            {
                "task": task.model_dump(),
                "topic": state["topic"],
                "mode": state.get("mode", "closed_book"),
                "plan": plan.model_dump(),
                "evidence": [e.model_dump() for e in state.get("evidence", [])],
            },
        )
        for task in plan.tasks
    ]


## Worker Node
def worker_node(payload: dict) -> dict:
    task = Task(**payload["task"])
    plan = Plan(**payload["plan"])
    evidence = [EvidenceItem(**e) for e in payload.get("evidence", [])]
    topic = payload["topic"]
    mode = payload.get("mode", "closed_book")

    bullets = task.bullets or []
    bullets_text = (
        "\n- " + "\n- ".join(bullets) if bullets else "\n(no bullets provided)"
    )

    evidence_text = ""
    if evidence:
        evidence_text = "\n".join(
            f"- {e.title} | {e.url} | {e.published_at or 'date:unknown'}".strip()
            for e in evidence[:20]
        )

    section_md = llm.invoke(
        [
            SystemMessage(content=WORKER_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog title: {plan.blog_title}\n"
                    f"Audience: {plan.audience}\n"
                    f"Tone: {plan.tone}\n"
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Constraints: {plan.constraints}\n"
                    f"Topic: {topic}\n"
                    f"Mode: {mode}\n\n"
                    f"Section title: {task.title}\n"
                    f"Goal: {task.goal}\n"
                    f"Target words: {task.target_words}\n"
                    f"Tags: {task.tags}\n"
                    f"requires_research: {task.requires_research}\n"
                    f"requires_citations: {task.requires_citations}\n"
                    f"requires_code: {task.requires_code}\n"
                    f"Bullets:{bullets_text}\n\n"
                    f"Evidence (ONLY use these URLs when citing):\n{evidence_text}\n\n"
                    f"Write this section in full, long-form detail — aim for at "
                    f"least {task.target_words} words. Elaborate with concrete "
                    f"examples, explanations, and (where relevant) code. Do not "
                    f"pad with filler, but do not summarize or truncate either; "
                    f"a short, thin section is a failed section."
                )
            ),
        ]
    ).content.strip()
    return {"sections": [(task.id, section_md)]}


## Reducer (subgraph): merge_content -> final markdown
def merge_content(state: State) -> dict:

    plan = state["plan"]
    assert plan is not None
    ordered_sections = [md for _, md in sorted(state["sections"], key=lambda x: x[0])]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"
    return {"merged_md": merged_md, "final": merged_md}


## Build Reducer Subgraph
reducer_graph = StateGraph(State)
reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", END)
reducer_subgraph = reducer_graph.compile()

## Build Main Graph
g = StateGraph(State)
g.add_node("router", router_node)
g.add_node("research", research_node)
g.add_node("orchestrator", orchestrator_node)
g.add_node("worker", worker_node)
g.add_node("reducer", reducer_subgraph)

g.add_edge(START, "router")
g.add_conditional_edges(
    "router", route_next, {"research": "research", "orchestrator": "orchestrator"}
)
g.add_edge("research", "orchestrator")

g.add_conditional_edges("orchestrator", fanout, ["worker"])
g.add_edge("worker", "reducer")
g.add_edge("reducer", END)


## PostgreSQL Checkpointer
try:
    DATABASE = get_database()
    if not DATABASE:
        raise RuntimeError("get_database() returned an empty connection string.")

    _conn = psycopg.connect(DATABASE, autocommit=True, row_factory=dict_row)

    checkpointer = PostgresSaver(_conn)
    checkpointer.setup()

except Exception as error:
    raise RuntimeError(
        "Could not connect to / initialize the Postgres checkpointer. "
        "Check that Postgres is running and that your DATABASE env var "
        f"(used inside backend.db.get_database) is correct.\nOriginal error: {error}"
    ) from error

app = g.compile(checkpointer=checkpointer)
