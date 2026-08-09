import asyncio
import json
import re
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from app.config import get_settings
from app.schemas.orchestration import (
    CodexExecutionComplete,
    CodexExecutionHeartbeat,
    CodexExecutionStart,
    ExecutionPlan,
    PlanList,
    RouteRequest,
)
from app.services.ollama_client import OllamaClient, OllamaError, get_ollama_client
from app.services.orchestration_service import OrchestrationService

router = APIRouter(prefix="/api/v1/orchestrator", tags=["orchestrator"])
MAX_LOCAL_DISTANCE = 1.0
LOW_VALUE_DOMAINS = ("csdn.net", "douban.com", "zhihu.com", "bilibili.com", "baidu.com", "toutiao.com", "sohu.com")
PRIMARY_SOURCE_DOMAINS = (".gov", ".edu", "arxiv.org", "openai.com", "anthropic.com", "ai.google", "deepmind.google", "huggingface.co", "github.com", "microsoft.com", "meta.com")
SEARCH_STOP_WORDS = {
    "about", "announcement", "available", "current", "discontinued", "find",
    "latest", "official", "product", "release", "releases", "replacing",
    "research", "source", "sources", "status", "today", "update", "updates",
    "what", "web", "with",
}


def _relevant_sources(matches: list[dict]) -> list[dict]:
    """Keep only meaningful local retrieval results and remove mirrored duplicates."""
    unique: list[dict] = []
    fingerprints: set[str] = set()
    for item in matches:
        if item.get("distance", float("inf")) > MAX_LOCAL_DISTANCE:
            continue
        if str(item.get("source", "")).endswith("/00_Project.md"):
            continue
        if "/Handoffs/" in str(item.get("source", "")):
            continue
        fingerprint = item.get("text", "")
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        unique.append(item)
    return unique


def _research_query(prompt: str, project_name: str) -> str:
    """Remove workflow boilerplate so SearXNG receives a researchable subject."""
    subject = prompt
    for phrase in (
        "\u5206\u6790\u73b0\u6709\u9879\u76ee\u8d44\u6599",
        "\u8d44\u6599\u4e0d\u8db3\u65f6\u8054\u7f51",
        "\u89c6\u60c5\u51b5\u8c03\u7528\u672c\u5730\u5927\u6a21\u578b\u548ccodex",
        "\u89c6\u60c5\u51b5\u8c03\u7528\u672c\u5730\u5927\u6a21\u578b\u548c Codex",
        "\u8054\u7f51",
        "\u4f7f\u7528codex",
        "\u4f7f\u7528 Codex",
        "analyze existing project materials",
        "search online if sources are insufficient",
        "use the local model and codex as appropriate",
        "use the local model and Codex as appropriate",
        "search online",
        "use codex",
        "use Codex",
    ):
        subject = subject.replace(phrase, "")
    subject = subject.strip(" \uff0c,\u3002.\uff1b;\uff1a:")
    if len(subject) < 8:
        subject = project_name
    normalized = prompt.lower()
    scholarly = any(
        word in normalized
        for word in (
            "\u8bba\u6587",
            "\u7814\u7a76\u8bba\u6587",
            "paper",
            "papers",
            "arxiv",
            "doi",
            "academic",
            "research",
        )
    )
    is_cjk = any("\u4e00" <= char <= "\u9fff" for char in subject)
    if is_cjk:
        suffix = (
            "\u5b98\u65b9 \u6700\u65b0 \u8bba\u6587 \u7814\u7a76"
            if scholarly
            else "\u5b98\u65b9 \u6700\u65b0 \u6587\u6863 \u4ea7\u54c1\u53d1\u5e03"
        )
    else:
        suffix = (
            "official latest papers research"
            if scholarly
            else "official latest documentation product release"
        )
    return f"{subject} {suffix}".strip()


def _research_queries(prompt: str, project_name: str, planned_queries: list[str]) -> list[str]:
    """Choose source-aware queries before falling back to broad metasearch."""
    normalized = prompt.lower()
    if "obsidian" in normalized and any(
        word in normalized for word in ("plugin", "\u63d2\u4ef6")
    ):
        # GitHub's own public search is reliable here, unlike general engines that
        # are frequently rate-limited when called from a shared residential IP.
        return ["!github obsidian plugin"]
    queries = [query for query in planned_queries if query.strip()]
    queries.append(_research_query(prompt, project_name))
    return list(dict.fromkeys(queries))[:2]


def _compact_search_query(query: str) -> str:
    if any("\u4e00" <= char <= "\u9fff" for char in query):
        return query.strip()
    site_match = re.search(r"site:([a-z0-9.-]+)", query.lower())
    terms = [
        term
        for term in re.findall(r"[A-Za-z][A-Za-z0-9.+_-]{2,}", query)
        if term.lower() not in SEARCH_STOP_WORDS and term.lower() != "site"
    ]
    if site_match:
        domain = site_match.group(1)
        brand = domain.split(".")[0]
        terms = [brand] + [
            term for term in terms if term.lower() not in {brand, domain}
        ]
    return " ".join(dict.fromkeys(terms)) or query.strip()


def _research_search_requests(query: str) -> list[tuple[str, str]]:
    normalized = query.lower().strip()
    if normalized.startswith("!github "):
        return [(query.split(maxsplit=1)[1], "github")]
    compact = _compact_search_query(query)
    if any(word in normalized for word in ("paper", "papers", "arxiv", "doi", "academic", "scientific study")):
        return [
            (compact, "openalex,crossref,arxiv"),
            (compact, "bing"),
        ]
    return [(compact, "bing")]


def _curate_results(results: list[dict], query: str = "") -> list[dict]:
    """Prefer primary sources while retaining useful independent results."""
    scored: list[tuple[int, dict]] = []
    seen: set[str] = set()
    subject_terms = {
        term
        for term in re.findall(r"[a-z0-9][a-z0-9.+_-]{2,}", query.lower())
        if term not in SEARCH_STOP_WORDS
    }
    official_requested = "official" in query.lower() or "site:" in query.lower()
    requested_site = re.search(r"site:([a-z0-9.-]+)", query.lower())
    for item in results:
        url = item.get("url", "")
        host = urlparse(url).netloc.lower().removeprefix("www.")
        if not url or not host or any(host == domain or host.endswith("." + domain) for domain in LOW_VALUE_DOMAINS) or url in seen:
            continue
        searchable = " ".join(
            str(item.get(field, "")) for field in ("title", "content", "url")
        ).lower()
        overlap = sum(term in searchable for term in subject_terms)
        if subject_terms and overlap == 0:
            continue
        seen.add(url)
        is_primary = any(
            host == domain or host.endswith("." + domain)
            for domain in PRIMARY_SOURCE_DOMAINS
        )
        if requested_site and not (
            host == requested_site.group(1)
            or host.endswith("." + requested_site.group(1))
        ):
            continue
        score = 1 + overlap * 3 + int(is_primary) * 10
        if re.search(r"/[a-z]{2}(?:-[A-Z]{2})?/", urlparse(url).path):
            score -= 4
        scored.append((score, {**item, "_primary": is_primary}))
    if official_requested and any(item.get("_primary") for _, item in scored):
        scored = [(score, item) for score, item in scored if item.get("_primary")]
    curated = []
    for _, item in sorted(scored, key=lambda pair: pair[0], reverse=True)[:5]:
        cleaned = dict(item)
        cleaned.pop("_primary", None)
        curated.append(cleaned)
    return curated


def _planner_decision(content: str, may_search: bool, may_use_codex: bool) -> dict:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    try:
        decision = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        decision = {}
    queries = [item.strip() for item in decision.get("queries", []) if isinstance(item, str) and item.strip()][:3]
    return {
        "needs_web": bool(decision.get("needs_web")) and may_search,
        "needs_codex": bool(decision.get("needs_codex")) and may_use_codex,
        "queries": queries,
        "reason": str(decision.get("reason", ""))[:500],
        "raw": content[:2000],
    }

def get_orchestrator() -> OrchestrationService:
    service = OrchestrationService(get_settings().orchestrator_store_path)
    service.initialize()
    return service

@router.post("/plans", response_model=ExecutionPlan, status_code=status.HTTP_201_CREATED)
def create_plan(request: RouteRequest, service: OrchestrationService = Depends(get_orchestrator)) -> ExecutionPlan:
    return service.create_plan(request)

@router.get("/plans", response_model=PlanList)
def list_plans(service: OrchestrationService = Depends(get_orchestrator)) -> PlanList:
    items = service.list_plans()
    return PlanList(items=items, count=len(items))


@router.get("/plans/{plan_id}", response_model=ExecutionPlan)
def get_plan(plan_id: str, service: OrchestrationService = Depends(get_orchestrator)) -> ExecutionPlan:
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(404, "plan not found")
    return plan


@router.get("/handoffs", response_model=PlanList)
def list_handoffs(service: OrchestrationService = Depends(get_orchestrator)) -> PlanList:
    items = service.list_handoffs()
    return PlanList(items=items, count=len(items))


@router.post("/plans/{plan_id}/codex/start", response_model=ExecutionPlan)
def start_codex(
    plan_id: str,
    request: CodexExecutionStart,
    service: OrchestrationService = Depends(get_orchestrator),
) -> ExecutionPlan:
    try:
        return service.start_handoff(plan_id, request.worker_id)
    except KeyError as exc:
        raise HTTPException(404, "plan not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/plans/{plan_id}/codex/heartbeat", response_model=ExecutionPlan)
def heartbeat_codex(
    plan_id: str,
    request: CodexExecutionHeartbeat,
    service: OrchestrationService = Depends(get_orchestrator),
) -> ExecutionPlan:
    try:
        return service.heartbeat_handoff(
            plan_id, request.worker_id, request.attempt_id
        )
    except KeyError as exc:
        raise HTTPException(404, "plan not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/plans/{plan_id}/codex/complete", response_model=ExecutionPlan)
async def complete_codex(
    plan_id: str,
    request: CodexExecutionComplete,
    service: OrchestrationService = Depends(get_orchestrator),
) -> ExecutionPlan:
    try:
        # Validate the attempt and renew its lease before the bounded knowledge
        # writeback. A stale attempt is rejected before it can cause side effects.
        plan = service.heartbeat_handoff(
            plan_id, request.worker_id, request.attempt_id
        )
    except KeyError as exc:
        raise HTTPException(404, "plan not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if plan.status in {"completed", "failed"}:
        return plan
    execution = request.model_dump()
    handoff_note = (plan.result or {}).get("handoff_note")
    if plan.project_id and handoff_note:
        writeback_error: str | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(15, connect=3)
                ) as knowledge_client:
                    response = await knowledge_client.post(
                        get_settings().knowledge_service_url
                        + f"/projects/{plan.project_id}/handoff-result",
                        json={"handoff_note": handoff_note, **execution},
                    )
                    response.raise_for_status()
                    execution["knowledge_writeback"] = response.json().get("note")
                writeback_error = None
                break
            except (httpx.HTTPError, ValueError) as exc:
                writeback_error = str(exc)[:1000]
                if attempt == 0:
                    await asyncio.sleep(1)
        if writeback_error:
            execution["knowledge_writeback_error"] = writeback_error
    try:
        return service.finish_handoff(plan_id, execution)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

@router.post("/plans/{plan_id}/approve", response_model=ExecutionPlan)
async def approve_and_run(plan_id: str, service: OrchestrationService = Depends(get_orchestrator), client: OllamaClient = Depends(get_ollama_client)) -> ExecutionPlan:
    try:
        plan = service.approve(plan_id)
    except KeyError as exc:
        raise HTTPException(404, "plan not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        sources = []
        system = None
        project_name = plan.project_id.replace("-", " ") if plan.project_id else ""
        if plan.project_id:
            async with httpx.AsyncClient(timeout=60) as knowledge_client:
                response = await knowledge_client.post(get_settings().knowledge_service_url + f"/projects/{plan.project_id}/search", json={"query": plan.prompt, "limit": 5})
                projects = await knowledge_client.get(get_settings().knowledge_service_url + "/projects")
            if projects.is_success:
                project_name = next((item.get("name", project_name) for item in projects.json() if item.get("id") == plan.project_id), project_name)
            if response.is_success:
                sources = _relevant_sources(response.json().get("matches", []))
        initial_context = "\n\n".join(f"[{item['source']}]\n{item['text'][:1200]}" for item in sources)
        try:
            planner_reply = await asyncio.wait_for(client.chat(
                message=plan.prompt,
                system=(
                    "You are the local project planner. Decide whether the task needs web research and whether it needs Codex for real file/code implementation. "
                    "Use web research only when local evidence is insufficient or freshness is required. Use Codex only for work that cannot be completed as an answer or plan. "
                    "Return JSON only: {\"needs_web\":boolean,\"needs_codex\":boolean,\"queries\":[string],\"reason\":string}.\n\n"
                    f"Project: {project_name}\nLocal evidence:\n{initial_context or '(none)'}"
                ),
            ), timeout=45)
            planner_content = planner_reply.get("message", {}).get("content", "")
            decision = _planner_decision(planner_content, "web_research" in plan.connectors, "codex_handoff" in plan.connectors)
        except (asyncio.TimeoutError, OllamaError):
            decision = {"needs_web": not sources and "web_research" in plan.connectors, "needs_codex": False, "queries": [], "reason": "Local planner timed out; used safe research fallback.", "raw": ""}
        research_note = None
        web_sources = []
        research_queries = []
        research_status = {"state": "not_requested", "detail": "The local planner did not require web research."}
        if plan.project_id and decision["needs_web"]:
            research_queries = _research_queries(plan.prompt, project_name, decision["queries"])
            captures: list[dict] = []
            failures: list[str] = []
            engines_used: list[str] = []
            async with httpx.AsyncClient(
                timeout=30,
                headers={
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Real-IP": "127.0.0.1",
                },
            ) as search_client:
                for query in research_queries:
                    for provider_query, engines in _research_search_requests(query):
                        try:
                            search = await search_client.get(
                                get_settings().search_service_url + "/search",
                                params={
                                    "q": provider_query,
                                    "format": "json",
                                    "language": "auto",
                                    "engines": engines,
                                },
                            )
                            if not search.is_success:
                                failures.append(f"{query} [{engines}]: HTTP {search.status_code}")
                                continue
                            results = _curate_results(search.json().get("results", []), query)
                            if not results:
                                failures.append(f"{query} [{engines}]: no results")
                                continue
                            engines_used.append(engines)
                            captures.extend({"title": item.get("title", "Untitled"), "url": item.get("url", ""), "content": item.get("content", "")} for item in results if item.get("url"))
                            break
                        except httpx.HTTPError as exc:
                            failures.append(f"{query} [{engines}]: {exc.__class__.__name__}")
            unique_captures = {item["url"]: item for item in captures}
            captures = list(unique_captures.values())[:5]
            if captures:
                web_sources = [{"title": item["title"], "url": item["url"]} for item in captures]
                research_status = {"state": "verified", "detail": f"Retrieved {len(captures)} curated source(s) through {', '.join(dict.fromkeys(engines_used))}.", "queries": research_queries}
                async with httpx.AsyncClient(timeout=90) as knowledge_client:
                    captured = await knowledge_client.post(get_settings().knowledge_service_url + f"/projects/{plan.project_id}/research", json={"query": " | ".join(research_queries), "sources": captures})
                    captured.raise_for_status()
                    research_note = captured.json().get("note")
                    response = await knowledge_client.post(get_settings().knowledge_service_url + f"/projects/{plan.project_id}/search", json={"query": plan.prompt, "limit": 5})
                if response.is_success:
                    sources = _relevant_sources(response.json().get("matches", []))
            else:
                detail = "No usable sources were returned; the response below is local reasoning only."
                if failures:
                    detail += " " + " | ".join(failures[:2])
                research_status = {"state": "unavailable", "detail": detail, "queries": research_queries}
        if sources:
            context = "\n\n".join(f"[{item['source']}]\n{item['text']}" for item in sources)
            system = "Answer using the supplied local project evidence. Cite its source paths. If evidence is insufficient, say so.\n\n" + context
        elif research_status["state"] == "unavailable":
            system = "No external sources were retrieved successfully. Answer only from local reasoning, and explicitly say that the answer has not been web-verified. Do not invent citations."
        reply = await client.chat(message=plan.prompt, system=system)
        message = reply.get("message", {})
        result = {"model": reply.get("model"), "response": message.get("content", ""), "local_sources": sources, "web_sources": web_sources, "research_queries": research_queries, "research_note": research_note, "research_status": research_status, "local_plan": decision}
        if decision["needs_codex"]:
            if not plan.project_id:
                return service.handoff(plan.id, {"outcome": "codex_handoff_required", "message": "Codex is needed, but no Obsidian project was selected to store a handoff package.", **result})
            handoff_payload = {
                "goal": plan.prompt,
                "local_plan": decision,
                "local_response": message.get("content", ""),
                "research_note": research_note,
                "sources": [{"title": item["title"], "url": item["url"], "content": ""} for item in web_sources],
                "workspace_id": plan.local_workspace_id or plan.project_id,
            }
            async with httpx.AsyncClient(timeout=90) as knowledge_client:
                handoff_response = await knowledge_client.post(get_settings().knowledge_service_url + f"/projects/{plan.project_id}/handoff", json=handoff_payload)
                handoff_response.raise_for_status()
            handoff_note = handoff_response.json().get("note")
            return service.handoff(plan.id, {"outcome": "codex_handoff_pending", "message": "A structured Codex implementation handoff was created and is awaiting execution.", "handoff_note": handoff_note, **result})
        return service.complete(plan.id, {"outcome": "local_model_response", **result})
    except OllamaError as exc:
        raise HTTPException(503, {"code": "local_model_unavailable", "message": str(exc)}) from exc
