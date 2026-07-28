import os, operator, psycopg
from pathlib import Path
from psycopg.rows import dict_row
from typing import TypedDict, List, Optional, Annotated

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langgraph.checkpoint.postgres import PostgresSaver

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
llm = ChatGroq(model="openai/gpt-oss-120b", temperature=0.5, streaming=True)


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
            # Skip a failing query instead of crashing the whole run.
            # If ALL queries fail, raw_results stays empty and we
            # gracefully fall back to no evidence below.
            continue

    if not raw_results:
        return {"evidence": []}
    trimmed_results = _trim_raw_results(raw_results)

    extractor = llm.with_structured_output(EvidencePack)
    try:
        pack = extractor.invoke(
            [
                SystemMessage(content=RESEARCH_SYSTEM),
                HumanMessage(content=f"Raw results:\n{trimmed_results}"),
            ]
        )
    except Exception as error:
        # If it's still too large (or Groq rate-limits us) don't
        # crash the whole run — fall back to no evidence instead.
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
def orchestrator_node(state: State) -> dict:
    planner = llm.with_structured_output(Plan)
    evidence = state.get("evidence", [])
    mode = state.get("mode", "closed_book")
    plan = planner.invoke(
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
        ]
    )
    return {"plan": plan}


## Fanout
def fanout(state: State):
    plan = state.get("plan")
    if plan is None or not plan.tasks:
        # No tasks planned — avoid a crash from an empty Send() list
        # by short-circuiting straight into the reducer with nothing
        # to merge. Adjust to your own graph if this case shouldn't
        # be possible in practice.
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
                    f"Evidence (ONLY use these URLs when citing):\n{evidence_text}\n"
                )
            ),
        ]
    ).content.strip()
    return {"sections": [(task.id, section_md)]}


## ReducerWithImages (subgraph): merge_content -> decide_images -> generate_and_place_images
def merge_content(state: State) -> dict:

    plan = state["plan"]
    ordered_sections = [md for _, md in sorted(state["sections"], key=lambda x: x[0])]
    body = "\n\n".join(ordered_sections).strip()
    merged_md = f"# {plan.blog_title}\n\n{body}\n"
    return {"merged_md": merged_md}


# Decide Images
def decide_images(state: State) -> dict:
    planner = llm.with_structured_output(GlobalImagePlan)
    merged_md = state["merged_md"]
    plan = state["plan"]
    assert plan is not None

    image_plan = planner.invoke(
        [
            SystemMessage(content=DECIDE_IMAGES_SYSTEM),
            HumanMessage(
                content=(
                    f"Blog kind: {plan.blog_kind}\n"
                    f"Topic: {state['topic']}\n\n"
                    "Insert placeholders + propose image prompts.\n\n"
                    f"{merged_md}"
                )
            ),
        ]
    )

    return {
        "md_with_placeholders": image_plan.md_with_placeholders,
        "image_specs": [img.model_dump() for img in image_plan.images],
    }


## Generate Images
def gemini_generate_image_bytes(prompt: str) -> bytes:
    """
    Returns raw image bytes generated by Gemini.
    Requires: pip install google-genai
    Env var: GOOGLE_API_KEY
    """
    from google import genai
    from google.genai import types

    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set.")

    client = genai.Client(api_key=api_key)

    resp = client.models.generate_content(
        model="gemini-2.5-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            safety_settings=[
                types.SafetySetting(
                    category="HARM_CATEGORY_DANGEROUS_CONTENT",
                    threshold="BLOCK_ONLY_HIGH",
                )
            ],
        ),
    )

    # Depending on SDK version, parts may hang off resp.candidates[0].content.parts
    parts = getattr(resp, "parts", None)
    if not parts and getattr(resp, "candidates", None):
        try:
            parts = resp.candidates[0].content.parts
        except Exception:
            parts = None

    if not parts:
        raise RuntimeError("No image content returned (safety/quota/SDK change).")

    for part in parts:
        inline = getattr(part, "inline_data", None)
        if inline and getattr(inline, "data", None):
            return inline.data

    raise RuntimeError("No inline image bytes found in response.")


## Generate + Place Images
def generate_and_place_images(state: State) -> dict:

    plan = state["plan"]
    assert plan is not None
    md = state.get("md_with_placeholders") or state["merged_md"]
    image_specs = state.get("image_specs", []) or []

    # If no images requested, just write merged markdown
    if not image_specs:
        filename = f"{plan.blog_title}.md"
        Path(filename).write_text(md, encoding="utf-8")
        return {"final": md}

    images_dir = Path("images")
    images_dir.mkdir(exist_ok=True)

    for spec in image_specs:
        placeholder = spec["placeholder"]
        filename = spec["filename"]
        out_path = images_dir / filename

        # generate only if needed
        if not out_path.exists():
            try:
                img_bytes = gemini_generate_image_bytes(spec["prompt"])
                out_path.write_bytes(img_bytes)
            except Exception as e:
                # graceful fallback: keep doc usable
                prompt_block = (
                    f"> **[IMAGE GENERATION FAILED]** {spec.get('caption','')}\n>\n"
                    f"> **Alt:** {spec.get('alt','')}\n>\n"
                    f"> **Prompt:** {spec.get('prompt','')}\n>\n"
                    f"> **Error:** {e}\n"
                )
                md = md.replace(placeholder, prompt_block)
                continue

        img_md = f"![{spec['alt']}](images/{filename})\n*{spec['caption']}*"
        md = md.replace(placeholder, img_md)

    filename = f"{plan.blog_title}.md"
    Path(filename).write_text(md, encoding="utf-8")
    return {"final": md}


## Build Reducer Subgraph
reducer_graph = StateGraph(State)
reducer_graph.add_node("merge_content", merge_content)
reducer_graph.add_node("decide_images", decide_images)
reducer_graph.add_node("generate_and_place_images", generate_and_place_images)
reducer_graph.add_edge(START, "merge_content")
reducer_graph.add_edge("merge_content", "decide_images")
reducer_graph.add_edge("decide_images", "generate_and_place_images")
reducer_graph.add_edge("generate_and_place_images", END)
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