from fastapi import APIRouter, Depends

# ── Store access middleware (agent isolation) ─────────────────────────────────────
from mneme.api.dependencies.store_access import check_store_access_middleware

# ── Agent bounded context ────────────────────────────────────────────────────────
from mneme.api.routes.agent.agent_cards import router as agent_cards_router
from mneme.api.routes.agent.agent_context import router as agent_context_router
from mneme.api.routes.agent.agents import router as agents_router
from mneme.api.routes.agent.context import router as context_router
from mneme.api.routes.agent.context_assembly import router as context_assembly_router
from mneme.api.routes.agent.conversations import router as conversations_router
from mneme.api.routes.agent.messages import router as messages_router

# ── Gateway bounded context ──────────────────────────────────────────────────────
from mneme.api.routes.gateway.gateway import router as gateway_router

# ── Knowledge bounded context ────────────────────────────────────────────────────
from mneme.api.routes.knowledge.importer import router as importer_router
from mneme.api.routes.knowledge.import_jobs import router as import_jobs_router
# knowledge_ingest module fully removed — frontend calls /api/v4/pipelines, /api/v4/sub-libraries, /api/v4/import directly
from mneme.api.routes.knowledge.knowledge import router as knowledge_router
from mneme.api.routes.knowledge.knowledge_search import router as knowledge_search_router
from mneme.api.routes.knowledge.knowledge_stores import router as knowledge_stores_router
from mneme.api.routes.knowledge.source_map import router as source_map_router
from mneme.api.routes.knowledge_v2 import router as knowledge_v2_router
from mneme.api.routes.import_v2 import router as import_v2_router
from mneme.api.routes.original_pool import router as original_pool_router

# ── Memory bounded context ───────────────────────────────────────────────────────
from mneme.api.routes.memory.graph import router as graph_router
from mneme.api.routes.memory.inbox import router as inbox_router
from mneme.api.routes.memory.memory import router as memory_router
from mneme.api.routes.memory.memory_candidates import router as memory_candidates_router
from mneme.api.routes.memory.memory_index import router as memory_index_router
from mneme.api.routes.memory.memory_relations import router as memory_relations_router
from mneme.api.routes.memory.memory_stores import router as memory_stores_router
from mneme.api.routes.memory.refine import router as refine_router
from mneme.api.routes.memory.review_items import router as review_router
from mneme.api.routes.memory.review_policy import router as review_policy_router
from mneme.api.routes.memory.neg_space_events import router as neg_space_events_router

# ── System bounded context ───────────────────────────────────────────────────────
from mneme.api.routes.system.admin_audit import router as admin_audit_router
from mneme.api.routes.system.admin_events import router as admin_events_router
from mneme.api.routes.system.admin_logs import router as admin_logs_router
from mneme.api.routes.system.asset_metadata import router as asset_metadata_router
from mneme.api.routes.system.assets import router as assets_router
from mneme.api.routes.system.auth import router as auth_router
from mneme.api.routes.system.backup import router as backup_router
from mneme.api.routes.system.dashboard import router as dashboard_router
from mneme.api.routes.system.dead_letters import router as dead_letters_router
from mneme.api.routes.system.eval import router as eval_router
from mneme.api.routes.system.event_source import router as event_source_router
from mneme.api.routes.system.global_search import router as global_search_router
from mneme.api.routes.system.health import router as health_router
from mneme.api.routes.system.migration import router as migration_router
from mneme.api.routes.system.pipelines import router as pipelines_router
from mneme.api.routes.system.projects import router as projects_router
from mneme.api.routes.system.raw_events import router as raw_events_router
from mneme.api.routes.system.vault import router as vault_router
from mneme.api.routes.system.trust_accounts import router as trust_accounts_router

# ── L7: Event Sourcing + Federation + Graph Triggers ────────────────────────
from mneme.api.routes.system.event_log import router as event_log_router
from mneme.api.routes.system.sync import router as sync_router
from mneme.api.routes.system.graph_triggers import router as graph_triggers_router

from mneme.observability.metrics import metrics_endpoint

# ═══════════════════════════════════════════════════════════════════════════════
# Inject store-access middleware into memory-bounded-context routers.
# This ensures AgentA cannot read/write AgentB's memory_store at the
# framework level, before any route handler executes.
# ═══════════════════════════════════════════════════════════════════════════════

_store_access_dep = Depends(check_store_access_middleware)

for _router in (
    memory_candidates_router,
    memory_router,
    memory_index_router,
    memory_relations_router,
    memory_stores_router,
    graph_router,
    inbox_router,
    refine_router,
    review_router,
    review_policy_router,
    neg_space_events_router,
):
    _router.dependencies.append(_store_access_dep)


api_v4_router = APIRouter(prefix="/api/v4")

# ── Agent ──
api_v4_router.include_router(agent_cards_router)
api_v4_router.include_router(agent_context_router)
api_v4_router.include_router(agents_router)
api_v4_router.include_router(context_router)
api_v4_router.include_router(context_assembly_router)
api_v4_router.include_router(conversations_router)
api_v4_router.include_router(messages_router)

# ── Gateway ──
api_v4_router.include_router(gateway_router)

# ── Knowledge ──
api_v4_router.include_router(importer_router)
api_v4_router.include_router(import_jobs_router)
api_v4_router.include_router(knowledge_router)
api_v4_router.include_router(knowledge_search_router)
api_v4_router.include_router(knowledge_stores_router)
api_v4_router.include_router(source_map_router)
api_v4_router.include_router(knowledge_v2_router)
api_v4_router.include_router(import_v2_router)
api_v4_router.include_router(original_pool_router)

# ── Memory ──
api_v4_router.include_router(graph_router)
api_v4_router.include_router(inbox_router)
api_v4_router.include_router(memory_candidates_router)
api_v4_router.include_router(memory_router)
api_v4_router.include_router(memory_index_router)
api_v4_router.include_router(memory_relations_router)
api_v4_router.include_router(memory_stores_router)
api_v4_router.include_router(refine_router)
api_v4_router.include_router(review_router)
api_v4_router.include_router(review_policy_router)
api_v4_router.include_router(neg_space_events_router)

# ── System ──
api_v4_router.include_router(admin_audit_router)
api_v4_router.include_router(admin_events_router)
api_v4_router.include_router(admin_logs_router)
api_v4_router.include_router(asset_metadata_router)
api_v4_router.include_router(assets_router)
api_v4_router.include_router(auth_router)
api_v4_router.include_router(backup_router)
api_v4_router.include_router(dashboard_router)
api_v4_router.include_router(dead_letters_router)
api_v4_router.include_router(eval_router)
api_v4_router.include_router(event_source_router)
api_v4_router.include_router(global_search_router)
api_v4_router.include_router(health_router)
api_v4_router.include_router(migration_router)
api_v4_router.include_router(pipelines_router)
api_v4_router.include_router(projects_router)
api_v4_router.include_router(raw_events_router)
api_v4_router.include_router(vault_router)
api_v4_router.include_router(trust_accounts_router)

# ── L7: Event Sourcing + Federation + Graph Triggers ──
api_v4_router.include_router(event_log_router)
api_v4_router.include_router(sync_router)
api_v4_router.include_router(graph_triggers_router)

# Minimal metrics at /api/v4/metrics
api_v4_router.add_api_route("/metrics", metrics_endpoint, methods=["GET"], tags=["observability"])
