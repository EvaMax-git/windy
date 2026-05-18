"""Concrete pipeline steps for context assembly.

Each step is a ``PipelineStep`` implementation that performs one
well-defined phase of the assembly process.

Pipeline flow
-------------
1. ResolveStoresStep       — query agent's card-type memory_stores
2. ResolveStrategiesStep   — map card types → injection strategies
3. AllocateBudgetStep      — partition token budget across strategy tiers
4. LoadContentStep         — for each card, use its strategy to fetch & format
5. AssembleTextStep        — combine sections + history into final text
6. WriteAuditStep          — persist context_pack + audit + outbox events
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from mneme.config import get_settings
from mneme.context.pipeline.base import PipelineContext, PipelineStep
from mneme.context.strategies import get_strategy, get_card_strategy_map
from mneme.db.audit import AuditEvent, OutboxEvent, add_audit_event, add_outbox_event
from mneme.db.context_packs import create_context_pack, create_context_pack_item
from mneme.db.transactions import transaction
from mneme.knowledge.token_estimator import estimate_tokens

logger = logging.getLogger(__name__)

# ── SQL templates ───────────────────────────────────────────────────────────

_LIST_AGENT_STORES = text("""
    SELECT store_id, agent_id, name, type, description,
           created_at, updated_at
    FROM memory_stores
    WHERE agent_id = :agent_id
      AND type IN (
          'soul_card', 'identity_card', 'tool_catalog',
          'user_profile', 'tool_detail'
      )
    ORDER BY
        CASE type
            WHEN 'soul_card' THEN 1
            WHEN 'identity_card' THEN 2
            WHEN 'tool_catalog' THEN 3
            WHEN 'user_profile' THEN 4
            WHEN 'tool_detail' THEN 5
        END
""")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _safe_uuid(val: Any) -> UUID | None:
    """Coerce value to UUID, handling string/UUID/None."""
    if val is None:
        return None
    if isinstance(val, UUID):
        return val
    try:
        return UUID(str(val))
    except (ValueError, TypeError):
        return None


# ═════════════════════════════════════════════════════════════════════════════
# Step 1 — Resolve Card Stores
# ═════════════════════════════════════════════════════════════════════════════

class ResolveStoresStep(PipelineStep):
    """Query the agent's card-type memory_stores and group by type."""

    name = "resolve_stores"

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        try:
            store_rows = ctx.db.execute(
                _LIST_AGENT_STORES, {"agent_id": ctx.agent_id}
            ).all()
        except Exception as exc:
            logger.warning("Failed to query agent card stores: %s", exc)
            ctx.degradation_reason = f"store_query_error: {exc}"
            store_rows = []

        if not store_rows:
            ctx.degradation_reason = ctx.degradation_reason or "no_card_stores_found"

        # Group stores by card type
        stores_by_type: dict[str, list[dict]] = {}
        for row in store_rows:
            data = dict(row._mapping)
            ct = data.get("type", "")
            stores_by_type.setdefault(ct, []).append(data)

        ctx.stores_by_type = stores_by_type
        return ctx


# ═════════════════════════════════════════════════════════════════════════════
# Step 2 — Resolve Strategies
# ═════════════════════════════════════════════════════════════════════════════

class ResolveStrategiesStep(PipelineStep):
    """Map each card type to its injection strategy name.

    Uses the strategy registry's ``get_card_strategy_map()``, which
    merges defaults, per-request overrides, and expand_cards.
    """

    name = "resolve_strategies"

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        ctx.strategies = get_card_strategy_map(
            overrides=ctx.strategy_overrides,
            expand_cards=ctx.expand_cards,
        )
        ctx.strategy_summary = dict(ctx.strategies)
        return ctx


# ═════════════════════════════════════════════════════════════════════════════
# Step 3 — Allocate Budget
# ═════════════════════════════════════════════════════════════════════════════

class AllocateBudgetStep(PipelineStep):
    """Partition the usable token budget across strategy tiers.

    Reads each registered strategy's ``budget_ratio`` to determine
    tiers, then splits each tier's budget equally among card types
    assigned to it.
    """

    name = "allocate_budget"

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.settings is None:
            ctx.settings = get_settings()

        total_budget = ctx.max_tokens or getattr(ctx.settings, "context_assembly_max_tokens", 128000)
        output_reserve = getattr(ctx.settings, "context_assembly_output_reserve", 4096)
        system_overhead = getattr(ctx.settings, "context_assembly_system_overhead", 2048)
        usable = max(1, total_budget - output_reserve - system_overhead)

        # Collect distinct strategy names → IInjectionStrategy instances
        strategy_names = set(ctx.strategies.get(ct, "moderate") for ct in ctx.stores_by_type)

        # Group card types by strategy tier
        tiers: dict[str, list[str]] = {}
        for ct, sname in ctx.strategies.items():
            if ct in ctx.stores_by_type:  # only for cards that exist
                tiers.setdefault(sname, []).append(ct)

        # Compute per-tier budgets from strategy budget_ratio
        tier_budgets: dict[str, int] = {}
        for sname in tiers:
            strategy = get_strategy(sname)
            tier_budgets[sname] = int(usable * strategy.budget_ratio)

        # Per-card-type budget (equal split within tier)
        per_type_budgets: dict[str, int] = {}
        for sname, card_types in tiers.items():
            per_type = tier_budgets[sname] // max(1, len(card_types))
            for ct in card_types:
                per_type_budgets[ct] = per_type

        # Budget consumption tracking
        consumed: dict[str, int] = {sname: 0 for sname in strategy_names}

        ctx.budget = {
            "total_available": total_budget,
            "system_overhead": system_overhead,
            "output_reserve": output_reserve,
            "usable": usable,
            "per_type": per_type_budgets,
            "consumed": consumed,
        }
        return ctx


# ═════════════════════════════════════════════════════════════════════════════
# Step 4 — Load Content
# ═════════════════════════════════════════════════════════════════════════════

class LoadContentStep(PipelineStep):
    """For each card type, use its assigned injection strategy to fetch
    and format memory content.

    This is the **strategy dispatch point**: the step itself is
    generic; all card-type-specific behaviour lives in the strategies.
    """

    name = "load_content"

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        sections: list[dict[str, Any]] = []

        for ct, stores in ctx.stores_by_type.items():
            strategy_name = ctx.strategies.get(ct, "moderate")
            strategy = get_strategy(strategy_name)

            type_budget = ctx.budget["per_type"].get(ct, 0)

            # Aggregate content across all stores of this card type
            all_parts: list[str] = []
            all_token_count = 0
            all_memory_ids: list[UUID] = []
            truncated = False
            budget_remaining = type_budget

            for store in stores:
                store_id = store.get("store_id")

                # Fetch memories using the strategy
                memories = strategy.fetch_memories(
                    ctx.db,
                    store_id,
                    ctx.query_text,
                )

                # Build content with remaining budget
                result = strategy.build_content(memories, budget_remaining)

                if result.content and result.content != "[无内容]":
                    all_parts.append(result.content)
                all_token_count += result.token_count
                all_memory_ids.extend(result.memory_ids)
                budget_remaining -= result.token_count

                if result.truncated:
                    truncated = True
                    break

                if budget_remaining <= 0:
                    truncated = True
                    break

            section_content = "\n\n".join(all_parts) if all_parts else "[无内容]"

            sections.append({
                "card_type": ct,
                "store_id": stores[0].get("store_id") if stores else None,
                "store_name": stores[0].get("name") if stores else None,
                "strategy": strategy_name,
                "content": section_content,
                "token_count": all_token_count,
                "memory_ids": all_memory_ids,
                "truncated": truncated,
            })

            # Track consumption
            ctx.budget["consumed"][strategy_name] = (
                ctx.budget["consumed"].get(strategy_name, 0) + all_token_count
            )

        ctx.sections = sections
        return ctx


# ═════════════════════════════════════════════════════════════════════════════
# Step 5 — Assemble Text
# ═════════════════════════════════════════════════════════════════════════════

class AssembleTextStep(PipelineStep):
    """Combine all card sections (in priority order) and optional
    conversation history into the final assembled text.
    """

    name = "assemble_text"

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        sorted_sections = self._order_sections(ctx.sections, ctx.strategies)
        assembled_parts: list[str] = []

        usable = ctx.budget.get("usable", ctx.budget.get("total_available", 128000) // 2)

        # Prepend conversation history if provided
        if ctx.conversation_history:
            hist_tokens = estimate_tokens(ctx.conversation_history)
            if hist_tokens < usable // 2:
                assembled_parts.append(
                    "<!-- 对话历史 -->\n" + ctx.conversation_history + "\n"
                )
            else:
                ctx.degradation_reason = ctx.degradation_reason or "conversation_history_truncated"

        # Add sections in priority order
        for sec in sorted_sections:
            header = f"<!-- {sec['card_type']} ({sec['strategy']}) -->\n"
            assembled_parts.append(header + sec["content"] + "\n")

        ctx.assembled_text = "\n".join(assembled_parts)
        ctx.total_tokens = estimate_tokens(ctx.assembled_text)

        # Replace sections with ordered version
        ctx.sections = sorted_sections
        return ctx

    @staticmethod
    def _order_sections(
        sections: list[dict], strategies: dict[str, str]
    ) -> list[dict]:
        """Sort sections by strategy priority, then card type name."""
        # Build priority lookup
        priority_map: dict[str, int] = {}
        for sname in set(strategies.values()):
            try:
                strategy = get_strategy(sname)
                priority_map[sname] = strategy.priority
            except KeyError:
                priority_map[sname] = 99

        return sorted(
            sections,
            key=lambda s: (priority_map.get(s.get("strategy", ""), 99), s.get("card_type", "")),
        )


# ═════════════════════════════════════════════════════════════════════════════
# Step 6 — Write Audit Trail
# ═════════════════════════════════════════════════════════════════════════════

class WriteAuditStep(PipelineStep):
    """Persist the assembled context as a context_pack with audit +
    outbox events.
    """

    name = "write_audit"

    def execute(self, ctx: PipelineContext) -> PipelineContext:
        pack_status = "failed" if ctx.degradation_reason else "created"

        sections_for_pack = [
            {
                "item_type": f"card_{sec['card_type']}",
                "object_id": _safe_uuid(sec.get("store_id")),
                "source_ref": {
                    "card_type": sec["card_type"],
                    "strategy": sec["strategy"],
                    "memory_ids": [str(mid) for mid in sec.get("memory_ids", [])],
                    "truncated": sec.get("truncated", False),
                },
                "included": True,
                "score": 1.0,
                "token_count": sec["token_count"],
                "reason": f"strategy:{sec['strategy']}",
            }
            for sec in ctx.sections
        ]

        stored_budget = {
            "max_tokens": ctx.budget.get("total_available", 0),
            "reserve_for_output": ctx.budget.get("output_reserve", 0),
            "system_overhead": ctx.budget.get("system_overhead", 0),
            "strategy_budgets": {
                sname: get_strategy(sname).budget_ratio
                for sname in set(ctx.strategies.values())
            },
        }

        with transaction(ctx.db):
            pack = create_context_pack(
                ctx.db,
                ctx.request_ctx,
                agent_id=ctx.agent_id,
                project_id=ctx.project_id,
                compile_mode="full",
                status=pack_status,
                knowledge_version_set=[],
                memory_version_set=[],
                token_budget=stored_budget,
                exclusion_summary={
                    "cards_total": len(ctx.stores_by_type),
                    "cards_truncated": sum(
                        1 for s in ctx.sections if s.get("truncated")
                    ),
                    "strategy_summary": ctx.strategy_summary,
                },
            )

            for idx, sec_data in enumerate(sections_for_pack):
                create_context_pack_item(
                    ctx.db,
                    pack_id=pack["context_pack_id"],
                    item_order=idx,
                    item_type=sec_data["item_type"],
                    object_id=sec_data["object_id"],
                    source_ref=sec_data["source_ref"],
                    included=sec_data["included"],
                    score=sec_data["score"],
                    token_count=sec_data["token_count"],
                    reason=sec_data["reason"],
                    content_digest=_content_hash(
                        json.dumps(sec_data["source_ref"], sort_keys=True, default=str)
                    ),
                )

            # Audit event
            add_audit_event(
                ctx.db,
                ctx.request_ctx,
                AuditEvent(
                    action="context.assemble",
                    result="success" if not ctx.degradation_reason else "failed",
                    object_type="context_pack",
                    object_id=pack["context_pack_id"],
                    project_id=ctx.project_id,
                    metadata_json={
                        "agent_id": str(ctx.agent_id),
                        "query_text": ctx.query_text[:200],
                        "total_tokens": ctx.total_tokens,
                        "sections_count": len(ctx.sections),
                        "degradation_reason": ctx.degradation_reason,
                    },
                ),
            )

            add_outbox_event(
                ctx.db,
                ctx.request_ctx,
                OutboxEvent(
                    event_type="context.assembled",
                    aggregate_type="context_pack",
                    aggregate_id=pack["context_pack_id"],
                    aggregate_version=1,
                    idempotency_key=str(
                        ctx.request_ctx.idempotency_key or str(uuid4())
                    ),
                    payload_json={
                        "agent_id": str(ctx.agent_id),
                        "total_tokens": ctx.total_tokens,
                        "sections_count": len(ctx.sections),
                    },
                ),
            )

        ctx.context_pack = pack
        return ctx
