"""P6-11 Memory Sublimation — abstract similar memory events into consensus knowledge.

"记忆升华" (Memory Sublimation) is the process of detecting semantically similar
memory events that recur ≥N times, then using LLM to abstract them into a single
"consensus" insight — a higher-level generalization that captures the pattern
across all instances.

This is conceptually similar to how the human brain consolidates repeated
experiences into "common sense" or "personality traits" over time.

Algorithm
---------
1. Fetch all active memories with ready embeddings.
2. Compute all-pairs cosine similarity; build clusters using a greedy
   single-linkage algorithm with configurable minimum similarity threshold.
3. Filter clusters to only those with ≥ ``min_cluster_size`` members.
4. For each qualifying cluster, construct an LLM prompt listing all member
   memories and ask the model to generate:
   - ``abstracted_insight`` (中文摘要, ≤ 200 chars)
   - ``category`` (the insight domain: preference, habit, knowledge, belief, skill, etc.)
   - ``confidence`` (0.0-1.0)
5. The resulting "sublimated" insight is stored as:
   - A new ``agent_card`` of type ``user_profile`` (or updates an existing one).
   - A new ``memory`` entry representing the consensus (with relation links
     back to source memories).
   - Optional inbox notification.

Dependencies
------------
* ``_cosine_similarity`` — existing in ``mneme.memory.search``.
* ``Gateway`` — existing in ``mneme.gateway.call``.
* ``create_memory_relation`` — existing in ``mneme.db.memory_relations``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text

from mneme.api.context import ActorContext, RequestContext
from mneme.db.base import SessionLocal

logger = logging.getLogger(__name__)

_SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000000")

# ── LLM Prompt ──────────────────────────────────────────────────────────────────

_SUBLIMATION_SYSTEM = (
    "你是记忆升华引擎。给你一组语义相似的记忆事件，请从其中抽象出更高层次的共识洞察。\n\n"
    "要求：\n"
    "1. 提炼出这些记忆共同指向的**模式、偏好、习惯、知识或信念**。\n"
    "2. 用简洁的中文表述（≤200字）。\n"
    "3. 给出抽象洞察的分类（preference/habit/knowledge/belief/skill/trait/rule/observation）。\n\n"
    "仅返回JSON：\n"
    '{"abstracted_insight": "<中文洞察>", "category": "<分类>", "confidence": 0.0-1.0, '
    '"supporting_count": N, "key_patterns": ["<模式1>", "<模式2>"]}'
)


# ── Data Types ──────────────────────────────────────────────────────────────────


@dataclass
class MemorySnapshot:
    """Lightweight view of a memory for clustering."""

    memory_id: UUID
    project_id: UUID | None = None
    title: str | None = None
    memory_text: str = ""
    canonical_key: str = ""
    embedding: list[float] = field(default_factory=list)


@dataclass
class SublimationCluster:
    """A group of similar memories ready for sublimation."""

    cluster_id: str = ""
    members: list[MemorySnapshot] = field(default_factory=list)
    avg_similarity: float = 0.0
    size: int = 0

    # Populated after LLM abstraction
    abstracted_insight: str | None = None
    category: str | None = None
    confidence: float = 0.0
    key_patterns: list[str] = field(default_factory=list)


@dataclass
class SublimationResult:
    """Aggregated result of a sublimation run."""

    memories_scanned: int = 0
    clusters_found: int = 0
    clusters_qualified: int = 0  # ≥ min_cluster_size
    insights_generated: int = 0
    cards_created: int = 0
    consensus_memories_created: int = 0
    relations_created: int = 0
    errors: int = 0
    clusters: list[SublimationCluster] = field(default_factory=list)


# ── SQL Queries ─────────────────────────────────────────────────────────────────

_FETCH_ACTIVE_MEMORIES = text("""
    SELECT DISTINCT ON (mie.memory_id)
        m.memory_id,
        m.project_id,
        m.title,
        m.memory_text,
        m.canonical_key,
        mie.embedding
    FROM memories m
    JOIN memory_index_entries mie ON mie.memory_id = m.memory_id
    WHERE m.status = 'active'
      AND mie.vector_state = 'ready'
      AND mie.embedding IS NOT NULL
    ORDER BY mie.memory_id, mie.memory_version DESC
""")

_INSERT_CONSENSUS_MEMORY = text("""
    INSERT INTO memories (
        memory_id, project_id, canonical_key,
        title, memory_text,
        store_id,
        current_version, sensitivity_level, status,
        activated_from_candidate_id, activated_by_review_item_id, activated_at
    ) VALUES (
        :memory_id, :project_id, :canonical_key,
        :title, :memory_text,
        NULL,
        1, 'normal', 'active',
        NULL, NULL, NOW()
    )
    RETURNING memory_id, canonical_key
""")


# ── Helpers ─────────────────────────────────────────────────────────────────────


def _parse_stored_embedding(value) -> list[float] | None:
    """Parse an embedding value from the DB."""
    if value is None:
        return None
    if isinstance(value, list):
        raw = value
    elif isinstance(value, str):
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        return None
    try:
        return [float(v) for v in raw]
    except (TypeError, ValueError):
        return None


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    """Compute cosine similarity between two embedding vectors."""
    import math

    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(left[i] * right[i] for i in range(size))
    left_norm = math.sqrt(sum(left[i] * left[i] for i in range(size)))
    right_norm = math.sqrt(sum(right[i] * right[i] for i in range(size)))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _make_system_context() -> RequestContext:
    """Build a minimal RequestContext for system-initiated actions."""
    return RequestContext(
        request_id=uuid4(),
        correlation_id=uuid4(),
        actor=ActorContext(actor_type="system", actor_id=_SYSTEM_USER_ID),
        idempotency_key=None,
    )


def _parse_llm_sublimation_response(raw_content: str) -> dict:
    """Parse LLM JSON response for sublimation."""
    content = raw_content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("sublimation: LLM returned non-JSON: %s", raw_content[:200])
        return {
            "abstracted_insight": "解析LLM响应失败",
            "category": "unknown",
            "confidence": 0.0,
            "supporting_count": 0,
            "key_patterns": [],
        }

    return {
        "abstracted_insight": str(parsed.get("abstracted_insight", "")),
        "category": str(parsed.get("category", "observation")),
        "confidence": float(parsed.get("confidence", 0.0)),
        "supporting_count": int(parsed.get("supporting_count", 0)),
        "key_patterns": list(parsed.get("key_patterns", [])),
    }


# ── Public API — fetch active memories ──────────────────────────────────────────


def fetch_active_memories(
    db,
    *,
    project_id: UUID | None = None,
) -> list[MemorySnapshot]:
    """Return all active memories with ready embeddings.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    project_id : UUID | None
        Optional project filter.

    Returns
    -------
    list[MemorySnapshot]
    """
    rows = db.execute(_FETCH_ACTIVE_MEMORIES).all()

    snapshots: list[MemorySnapshot] = []
    for row in rows:
        data = dict(row._mapping)
        embedding = _parse_stored_embedding(data.get("embedding"))
        if embedding is None:
            continue
        if project_id is not None and data.get("project_id") != project_id:
            continue
        snapshots.append(
            MemorySnapshot(
                memory_id=data["memory_id"],
                project_id=data.get("project_id"),
                title=data.get("title"),
                memory_text=data.get("memory_text", ""),
                canonical_key=data.get("canonical_key", ""),
                embedding=embedding,
            )
        )
    return snapshots


# ── Public API — cluster similar memories ───────────────────────────────────────


def cluster_similar_memories(
    snapshots: list[MemorySnapshot],
    *,
    min_similarity: float = 0.80,
    min_cluster_size: int = 5,
    max_clusters: int = 10,
) -> list[SublimationCluster]:
    """Build clusters of similar memories using greedy single-linkage clustering.

    Parameters
    ----------
    snapshots : list[MemorySnapshot]
        All active memories.
    min_similarity : float
        Minimum cosine similarity for two memories to be in the same cluster.
    min_cluster_size : int
        Minimum members to qualify as a "sublimatable" cluster.
    max_clusters : int
        Maximum clusters to return (sorted by size descending).

    Returns
    -------
    list[SublimationCluster]
        Qualified clusters, sorted by size descending.
    """
    if len(snapshots) < 2:
        return []

    n = len(snapshots)

    # Union-Find for single-linkage clustering
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # Build adjacency
    for i in range(n):
        for j in range(i + 1, n):
            sim = _cosine_similarity(
                snapshots[i].embedding, snapshots[j].embedding,
            )
            if sim >= min_similarity:
                union(i, j)

    # Collect clusters
    clusters_map: dict[int, list[int]] = {}
    for i in range(n):
        root = find(i)
        clusters_map.setdefault(root, []).append(i)

    # Build SublimationCluster objects
    clusters: list[SublimationCluster] = []
    for root, indices in clusters_map.items():
        if len(indices) < min_cluster_size:
            continue
        members = [snapshots[i] for i in indices]

        # Compute average similarity within cluster
        similarities: list[float] = []
        for a_idx in range(len(indices)):
            for b_idx in range(a_idx + 1, len(indices)):
                sim = _cosine_similarity(
                    snapshots[indices[a_idx]].embedding,
                    snapshots[indices[b_idx]].embedding,
                )
                similarities.append(sim)
        avg_sim = sum(similarities) / len(similarities) if similarities else 0.0

        clusters.append(
            SublimationCluster(
                cluster_id=hashlib.sha256(
                    "-".join(str(m.memory_id) for m in members).encode()
                ).hexdigest()[:16],
                members=members,
                avg_similarity=round(avg_sim, 4),
                size=len(members),
            )
        )

    # Sort by size descending, take top N
    clusters.sort(key=lambda c: c.size, reverse=True)
    return clusters[:max_clusters]


# ── Public API — LLM abstraction ────────────────────────────────────────────────


def abstract_cluster_with_llm(
    cluster: SublimationCluster,
    *,
    gateway=None,
    model: str = "deepseek-chat",
) -> SublimationCluster:
    """Use LLM to generate an abstracted insight from a cluster of similar memories.

    Modifies *cluster* in-place by setting ``abstracted_insight``, ``category``,
    ``confidence``, and ``key_patterns``.

    Parameters
    ----------
    cluster : SublimationCluster
        The cluster to abstract.
    gateway : Gateway | None
        Pre-configured Gateway instance.
    model : str
        Model name for LLM calls.

    Returns
    -------
    SublimationCluster
        The same cluster with abstraction fields populated.
    """
    if gateway is None or not cluster.members:
        return cluster

    from mneme.gateway.call import GatewayError

    # Build member list for prompt
    member_lines = []
    for i, mem in enumerate(cluster.members[:20], 1):  # cap at 20 for token budget
        text_preview = (mem.memory_text or "")[:300]
        title = mem.title or "(无标题)"
        member_lines.append(
            f"{i}. [{title}] {text_preview}"
        )
    members_text = "\n".join(member_lines)

    messages = [
        {"role": "system", "content": _SUBLIMATION_SYSTEM},
        {
            "role": "user",
            "content": (
                f"以下是一组语义相似的记忆（共{len(cluster.members)}条，相似度均值={cluster.avg_similarity:.3f}），"
                f"请从这些记忆中抽象出更高层次的共识洞察：\n\n"
                f"{members_text}"
            ),
        },
    ]

    try:
        result = gateway.call(
            capability_code="chat.completion",
            params={
                "model": model,
                "messages": messages,
                "temperature": 0.3,
                "max_tokens": 1024,
            },
            sensitivity="private",
            call_type="memory_sublimation",
        )
        content = (
            result.get("data", {})
            .get("choices", [{}])[0]
            .get("message", {})
            .get("content", "{}")
        )
        parsed = _parse_llm_sublimation_response(content)
        cluster.abstracted_insight = parsed["abstracted_insight"]
        cluster.category = parsed["category"]
        cluster.confidence = parsed["confidence"]
        cluster.key_patterns = parsed["key_patterns"]

        logger.info(
            "sublimation: LLM generated insight for cluster %s (%d members, "
            "category=%s, confidence=%.2f)",
            cluster.cluster_id,
            cluster.size,
            cluster.category,
            cluster.confidence,
        )
    except (GatewayError, Exception) as exc:
        logger.warning(
            "sublimation: LLM abstraction failed for cluster %s: %s",
            cluster.cluster_id,
            exc,
        )

    return cluster


# ── Public API — apply sublimation (create consensus + profile update) ──────────


def apply_sublimation(
    db,
    context: RequestContext,
    *,
    cluster: SublimationCluster,
    create_consensus_memory: bool = True,
    create_profile_card: bool = True,
    create_notification: bool = False,
) -> dict[str, Any]:
    """Create the artifacts for a sublimated cluster.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing/auth context.
    cluster : SublimationCluster
        The LLM-abstracted cluster.
    create_consensus_memory : bool
        Create a new ``memory`` representing the consensus insight.
    create_profile_card : bool
        Create/update a ``user_profile`` agent card with the insight.
    create_notification : bool
        Create an inbox notification.

    Returns
    -------
    dict
        ``{"consensus_memory_id": UUID|None, "card_id": UUID|None,
        "inbox_item_id": UUID|None, "relations_created": int}``
    """
    if not cluster.abstracted_insight:
        return {
            "consensus_memory_id": None,
            "card_id": None,
            "inbox_item_id": None,
            "relations_created": 0,
        }

    result: dict[str, Any] = {
        "consensus_memory_id": None,
        "card_id": None,
        "inbox_item_id": None,
        "relations_created": 0,
    }

    project_id = cluster.members[0].project_id if cluster.members else None

    # 1. Create consensus memory
    if create_consensus_memory and project_id:
        consensus_id = _create_consensus_memory(db, cluster, project_id)
        if consensus_id:
            result["consensus_memory_id"] = consensus_id
            # Create relations from source memories → consensus
            for member in cluster.members:
                try:
                    _create_sublimation_relation(
                        db, context, member.memory_id, consensus_id, cluster,
                    )
                    result["relations_created"] += 1
                except Exception as exc:
                    logger.debug(
                        "sublimation: relation creation skipped for %s->%s: %s",
                        member.memory_id, consensus_id, exc,
                    )

    # 2. Create/update user_profile card
    if create_profile_card and project_id:
        card_id = _upsert_profile_card(db, cluster, project_id)
        if card_id:
            result["card_id"] = card_id

    # 3. Create inbox notification
    if create_notification and project_id:
        inbox_id = _create_sublimation_notification(db, cluster, project_id)
        if inbox_id:
            result["inbox_item_id"] = inbox_id

    return result


# ── Internal helpers ────────────────────────────────────────────────────────────


def _create_consensus_memory(
    db,
    cluster: SublimationCluster,
    project_id: UUID,
) -> UUID | None:
    """Create a new memory entry representing the sublimated consensus insight."""
    try:
        from mneme.db.memories import _fetch_project_code

        # Generate canonical key
        row = db.execute(
            _fetch_project_code,
            {"pid": project_id},
        ).first()
        if row is None:
            logger.warning("sublimation: project %s not found", project_id)
            return None

        project_code = row[0]
        next_num = db.execute(
            text("SELECT count(*) + 1 FROM memories WHERE project_id = :pid"),
            {"pid": project_id},
        ).scalar_one()
        canonical_key = f"{project_code}-sub-{next_num}"

        memory_id = uuid4()
        title = f"💡 {cluster.category or '洞察'}: {cluster.abstracted_insight[:80]}"

        content = (
            f"# 记忆升华洞察\n\n"
            f"**分类**: {cluster.category or 'observation'}\n"
            f"**置信度**: {cluster.confidence:.2f}\n"
            f"**来源记忆数**: {len(cluster.members)}\n"
            f"**关键词模式**: {', '.join(cluster.key_patterns) if cluster.key_patterns else '无'}\n\n"
            f"## 抽象洞察\n\n"
            f"{cluster.abstracted_insight}\n\n"
            f"## 原始记忆\n\n"
        )
        for i, mem in enumerate(cluster.members[:10], 1):
            content += f"{i}. {mem.title or '(无标题)'}: "
            content += f"{(mem.memory_text or '')[:200]}...\n"

        db.execute(
            _INSERT_CONSENSUS_MEMORY,
            {
                "memory_id": memory_id,
                "project_id": project_id,
                "canonical_key": canonical_key,
                "title": title,
                "memory_text": content,
            },
        )

        logger.info(
            "sublimation: created consensus memory %s (%s)",
            memory_id,
            canonical_key,
        )
        return memory_id
    except Exception as exc:
        logger.error(
            "sublimation: failed to create consensus memory: %s", exc,
        )
        return None


def _create_sublimation_relation(
    db,
    context: RequestContext,
    source_memory_id: UUID,
    consensus_memory_id: UUID,
    cluster: SublimationCluster,
) -> None:
    """Create a ``supports`` relation from source memory → consensus."""
    from mneme.db.memory_relations import create_memory_relation
    from mneme.schemas.memory_relations import MemoryRelationCreate, RelationType

    payload = MemoryRelationCreate(
        from_memory_id=source_memory_id,
        to_memory_id=consensus_memory_id,
        relation_type=RelationType.supports,
        reason=f"记忆升华: 原记忆为共识洞察'{cluster.abstracted_insight[:60]}'提供支撑",
        metadata_json={
            "sublimation_cluster_id": cluster.cluster_id,
            "cluster_size": cluster.size,
            "avg_similarity": cluster.avg_similarity,
            "category": cluster.category or "",
            "source": "memory_sublimation",
        },
    )

    create_memory_relation(db, context, payload=payload)


def _upsert_profile_card(
    db,
    cluster: SublimationCluster,
    project_id: UUID,
) -> UUID | None:
    """Create or update a user_profile card with the sublimated insight."""
    try:
        from mneme.db.agent_cards import create_card, list_cards, update_card
        from mneme.schemas.agent_cards import (
            AgentCardCreateRequest,
            AgentCardType,
            AgentCardUpdateRequest,
        )

        # Look for existing profile card for this project
        # (agent_id can be derived or null for project-level profiles)
        existing_cards, _ = list_cards(db, card_type="user_profile")

        insight_entry = {
            "abstracted_insight": cluster.abstracted_insight,
            "category": cluster.category or "observation",
            "confidence": cluster.confidence,
            "supporting_count": len(cluster.members),
            "key_patterns": cluster.key_patterns,
            "source_memory_ids": [str(m.memory_id) for m in cluster.members[:10]],
            "generated_at": "now",
            "source": "memory_sublimation",
        }

        if existing_cards:
            # Update the first profile card
            card = existing_cards[0]
            content = dict(card.content_json) if card.content_json else {}
            insights = content.get("sublimated_insights", [])
            insights.append(insight_entry)
            content["sublimated_insights"] = insights

            update_payload = AgentCardUpdateRequest(content_json=content)
            updated = update_card(
                db, _make_system_context(), card_id=card.card_id, payload=update_payload,
            )
            if updated:
                logger.info(
                    "sublimation: updated profile card %s with new insight",
                    card.card_id,
                )
                return card.card_id
        else:
            # Create new profile card
            create_payload = AgentCardCreateRequest(
                agent_id=None,
                card_type=AgentCardType.user_profile,
                name="用户画像 (自动生成)",
                description="由记忆升华引擎自动生成和更新的用户画像卡片。",
                content_json={
                    "sublimated_insights": [insight_entry],
                    "generation_method": "memory_sublimation",
                    "auto_updated": True,
                },
                display_order=100,
            )
            card = create_card(
                db, _make_system_context(), payload=create_payload,
            )
            logger.info(
                "sublimation: created new profile card %s",
                card.card_id,
            )
            return card.card_id

        return None
    except Exception as exc:
        logger.warning(
            "sublimation: failed to create/update profile card: %s", exc,
        )
        return None


def _create_sublimation_notification(
    db,
    cluster: SublimationCluster,
    project_id: UUID,
) -> UUID | None:
    """Create an inbox notification about the new sublimated insight."""
    try:
        from mneme.db.inbox import create_inbox_item
        from mneme.schemas.storage import InboxItemCreateRequest

        title = (
            f"🧠 记忆升华: 从{len(cluster.members)}条相似记忆中提炼出新洞察 — "
            f"{cluster.abstracted_insight[:80] if cluster.abstracted_insight else '...'}"
        )

        raw = f"sub-inbox-{cluster.cluster_id}"
        ikey = hashlib.sha256(raw.encode()).hexdigest()

        context = RequestContext(
            request_id=uuid4(),
            correlation_id=uuid4(),
            actor=ActorContext(actor_type="system", actor_id=_SYSTEM_USER_ID),
            idempotency_key=ikey,
        )

        payload = InboxItemCreateRequest(
            project_id=project_id,
            inbox_type="alert",
            source="memory_sublimation",
            source_uri=None,
            source_ref=f"sublimation:{cluster.cluster_id}",
            title=title[:200],
            content_hash=None,
            payload_json={
                "alert_type": "memory_sublimation",
                "cluster_id": cluster.cluster_id,
                "cluster_size": cluster.size,
                "avg_similarity": cluster.avg_similarity,
                "abstracted_insight": cluster.abstracted_insight or "",
                "category": cluster.category or "",
                "confidence": cluster.confidence,
                "key_patterns": cluster.key_patterns,
                "member_memory_ids": [str(m.memory_id) for m in cluster.members[:10]],
            },
            metadata_json={
                "source": "memory_sublimation",
            },
        )

        item = create_inbox_item(db, context, payload=payload, status="received")
        return item.inbox_item_id
    except Exception as exc:
        logger.warning(
            "sublimation: failed to create inbox notification: %s", exc,
        )
        return None


# ── Public API — full sublimation pipeline ──────────────────────────────────────


def run_sublimation_pipeline(
    db,
    context: RequestContext,
    *,
    project_id: UUID | None = None,
    gateway=None,
    min_similarity: float = 0.80,
    min_cluster_size: int = 5,
    max_clusters: int = 10,
    model: str = "deepseek-chat",
    dry_run: bool = False,
    create_consensus_memory: bool = True,
    create_profile_card: bool = True,
    create_notification: bool = False,
) -> SublimationResult:
    """Full memory sublimation pipeline: cluster → LLM abstract → apply.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    context : RequestContext
        Tracing/auth context.
    project_id : UUID | None
        Scope to a specific project. None = all projects.
    gateway : Gateway | None
        Gateway instance for LLM calls.
    min_similarity : float
        Minimum cosine similarity for cluster membership.
    min_cluster_size : int
        Minimum members to trigger sublimation (default 5).
    max_clusters : int
        Maximum clusters to process.
    model : str
        LLM model for abstraction.
    dry_run : bool
        If True, detect and abstract but do not write changes.
    create_consensus_memory : bool
        Create consensus memory entries.
    create_profile_card : bool
        Create or update user_profile agent cards.
    create_notification : bool
        Create inbox notifications.

    Returns
    -------
    SublimationResult
    """
    result = SublimationResult()

    # Stage 1: Fetch active memories
    snapshots = fetch_active_memories(db, project_id=project_id)
    result.memories_scanned = len(snapshots)
    logger.info(
        "sublimation: fetched %d active memories with embeddings",
        len(snapshots),
    )

    if len(snapshots) < min_cluster_size:
        return result

    # Stage 2: Cluster similar memories
    clusters = cluster_similar_memories(
        snapshots,
        min_similarity=min_similarity,
        min_cluster_size=min_cluster_size,
        max_clusters=max_clusters,
    )
    result.clusters_found = len(clusters)
    result.clusters_qualified = len(clusters)
    logger.info(
        "sublimation: found %d qualified clusters (≥%d members)",
        len(clusters),
        min_cluster_size,
    )

    if not clusters:
        return result

    # Stage 3: LLM abstraction
    if gateway is not None:
        for cluster in clusters:
            try:
                abstract_cluster_with_llm(
                    cluster,
                    gateway=gateway,
                    model=model,
                )
                if cluster.abstracted_insight:
                    result.insights_generated += 1
            except Exception as exc:
                logger.warning(
                    "sublimation: LLM abstraction failed for cluster %s: %s",
                    cluster.cluster_id,
                    exc,
                )
                result.errors += 1

    # Stage 4: Apply (unless dry_run)
    if not dry_run:
        for cluster in clusters:
            if not cluster.abstracted_insight:
                continue
            try:
                apply_output = apply_sublimation(
                    db,
                    context,
                    cluster=cluster,
                    create_consensus_memory=create_consensus_memory,
                    create_profile_card=create_profile_card,
                    create_notification=create_notification,
                )
                if apply_output.get("consensus_memory_id"):
                    result.consensus_memories_created += 1
                if apply_output.get("card_id"):
                    result.cards_created += 1
                result.relations_created += apply_output.get("relations_created", 0)
            except Exception as exc:
                logger.error(
                    "sublimation: apply failed for cluster %s: %s",
                    cluster.cluster_id,
                    exc,
                    exc_info=True,
                )
                result.errors += 1

    result.clusters = clusters
    logger.info(
        "sublimation: complete — scanned=%d clusters=%d qualified=%d "
        "insights=%d cards=%d consensus=%d relations=%d errors=%d",
        result.memories_scanned,
        result.clusters_found,
        result.clusters_qualified,
        result.insights_generated,
        result.cards_created,
        result.consensus_memories_created,
        result.relations_created,
        result.errors,
    )
    return result


__all__ = [
    "MemorySnapshot",
    "SublimationCluster",
    "SublimationResult",
    "fetch_active_memories",
    "cluster_similar_memories",
    "abstract_cluster_with_llm",
    "apply_sublimation",
    "run_sublimation_pipeline",
]
