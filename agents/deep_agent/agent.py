"""Deployable research agent for Modules 3 (deploy) and 5 (Engine).

Built with deepagents' prebuilt `create_deep_agent` as an orchestrator that
delegates every unit of work to a specialist subagent:

    orchestrator  plans, fans out research, writes the report, owns all writes
    ├── researcher     answers one focused sub-question with cited web findings
    ├── fact-checker   audits the draft report against the research (read-only)
    └── content-writer turns findings into LinkedIn / X / Slack posts via skills

Only the orchestrator can write files, so human-in-the-loop approvals stay in
one place, and only the researcher and fact-checker can search the web.

Exported as a factory `agent(config)` so a LangSmith assistant can pin runtime
config. The defaults reproduce the deployed behavior for Modules 2-4; the
Module 5 `engine-demo` assistant flips on a broken search tool and weak
Context Hub guidance so Engine has a recurring issue to find.
"""

import os
from datetime import date

from deepagents import FilesystemMiddleware, SubAgent, create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StoreBackend
from deepagents.backends.context_hub import ContextHubBackend
from langchain.agents.middleware import TodoListMiddleware
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from tavily import TavilyClient

from utils.models import model
from utils.search import resilient_tavily_search

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
READ_ONLY_FS_TOOLS = ["ls", "read_file", "glob", "grep"]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@tool(parse_docstring=True)
def tavily_search(query: str) -> str:
    """Search the web for information on a given query.

    Args:
        query: Search query to execute.
    """
    return resilient_tavily_search(query, max_retries=2)


@tool(parse_docstring=True)
def easy_search(query: str) -> str:
    """Search the web for information on a given query.

    Args:
        query: Search query to execute.
    """
    # Module 5 demo: a "lightweight" search that drops each result's content,
    # so the agent answers from titles alone — ungrounded by design. A fresh
    # client per call gives each concurrent tool thread its own (non-thread-safe)
    # requests.Session; a shared client races and drops connections.
    hits = TavilyClient().search(query, max_results=3).get("results", [])
    return "\n".join(f"- {h['title']} ({h['url']})" for h in hits)


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #

ORCHESTRATOR_PROMPT = """You are an expert research assistant and the orchestrator \
of a small team of specialist subagents. Today is {today}.

You do not search the web yourself. Break the request down, delegate each piece \
with the `task` tool, then synthesize and write the final output. Follow the \
workflow and rules in your memory (AGENTS.md)."""

RESEARCHER_PROMPT = """You are a focused web researcher. Today is {today}.

You receive ONE sub-question. Answer only that sub-question.

- Use at most 3 search calls. Stop early once you have enough evidence.
- Only state facts the search results actually support. If results lack \
content or contradict each other, say so instead of filling gaps from memory.
- Prefer primary and recent sources.

Return exactly this format and nothing else:

## Findings
- <one fact per bullet, with a citation like [1]>

## Gaps
- <what you could not confirm, or "None">

## Sources
[1] <title> — <url>
[2] <title> — <url>"""

FACT_CHECKER_PROMPT = """You are a meticulous fact-checker. Today is {today}.

You receive the path of a draft report, usually `/final_report.md`, and \
optionally the research findings it was based on. Read the draft with \
`read_file` and audit it:

1. Every factual claim must carry a citation that appears in the Sources section.
2. Each cited source must plausibly support its claim. Use at most 2 search \
calls to spot-check the claims you are least sure about.
3. Citation numbers must be consistent: each unique URL gets exactly one number.
4. Flag anything stated with more certainty than the evidence allows.

You cannot edit files. Return either `PASS` or a numbered list of issues, each \
with the exact quote, the problem, and a concrete fix."""

CONTENT_WRITER_PROMPT = """You are a content writer who turns research into \
platform-native posts.

You receive the target platform(s) and the findings to draw from (or the path of \
a report to read). Before writing, read the matching skill (`linkedin-post`, \
`twitter-post`, or `slack-post`) and follow its format, tone, and length rules \
exactly. Use only facts present in the material you were given; never invent \
statistics or quotes.

You cannot write files. Return only the finished post text for each requested \
platform, under a heading naming the platform."""


# --------------------------------------------------------------------------- #
# Subagents
# --------------------------------------------------------------------------- #

def build_subagents(web_search: BaseTool, backend: CompositeBackend) -> list[SubAgent]:
    """Specialists the orchestrator delegates to. None of them can write files."""
    today = date.today().isoformat()
    read_only_fs = [FilesystemMiddleware(backend=backend, tools=READ_ONLY_FS_TOOLS)]
    return [
        {
            "name": "researcher",
            "description": (
                "Researches ONE focused sub-question on the web and returns cited "
                "findings, gaps, and a numbered source list. Call it several times "
                "in parallel for independent sub-questions. It cannot see the "
                "conversation, so pass a self-contained question with any needed "
                "context (audience, timeframe, scope)."
            ),
            "system_prompt": RESEARCHER_PROMPT.format(today=today),
            "tools": [web_search],
            "middleware": read_only_fs,
            "interrupt_on": {},
        },
        {
            "name": "fact-checker",
            "description": (
                "Audits a drafted report file against its sources and returns PASS "
                "or a list of unsupported claims and citation errors with fixes. "
                "Pass the report path and the research findings it was built from."
            ),
            "system_prompt": FACT_CHECKER_PROMPT.format(today=today),
            "tools": [web_search],
            "middleware": read_only_fs,
            "interrupt_on": {},
        },
        {
            "name": "content-writer",
            "description": (
                "Writes LinkedIn, Twitter/X, or Slack posts from research findings "
                "using the matching skill. Pass the platform(s) and the findings or "
                "report path. Returns the post text; it does not save files."
            ),
            "system_prompt": CONTENT_WRITER_PROMPT,
            "tools": [],
            "skills": ["./skills/"],
            "middleware": read_only_fs,
            "interrupt_on": {},
        },
    ]


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #

def agent(config: RunnableConfig | None = None):
    """Build the agent. A LangSmith assistant can pin `config.configurable`:
    `search_tool` ("tavily" default | "easy"), `interrupts` (default True), and
    `context_repo` (a Context Hub repo mounted at /context/; none by default).
    """
    cfg = (config or {}).get("configurable", {})
    web_search = easy_search if cfg.get("search_tool") == "easy" else tavily_search
    context_repo = cfg.get("context_repo")

    # `namespace` is required as of deepagents 0.7.0 (the old implicit
    # assistant_id scoping is gone); same namespace the notebooks use.
    routes = {"/memories/": StoreBackend(namespace=lambda rt: ("memories", "shared"))}
    if context_repo:
        routes["/context/"] = ContextHubBackend(context_repo)
    backend = CompositeBackend(
        default=FilesystemBackend(root_dir=AGENT_DIR, virtual_mode=True),
        routes=routes,
    )

    return create_deep_agent(
        model=model,
        tools=[],
        middleware=[TodoListMiddleware()],
        system_prompt=ORCHESTRATOR_PROMPT.format(today=date.today().isoformat()),
        memory=["./AGENTS.md"] + (["/context/AGENTS.md"] if context_repo else []),
        subagents=build_subagents(web_search, backend),
        backend=backend,
        interrupt_on={"write_file": True, "edit_file": True} if cfg.get("interrupts", True) else {},
    )
