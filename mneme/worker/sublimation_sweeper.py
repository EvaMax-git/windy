"""P6-11 Sublimation Sweeper — periodic background task that clusters similar
memories and abstracts them into consensus knowledge.

Runs inside the worker main loop (similar to DecaySweeper / SpontaneousRecallSweeper).
Each sweep:
1. Fetches all active memories with ready embeddings.
2. Clusters memories by embedding similarity (greedy single-linkage).
3. Filters clusters with ≥ min_cluster_size members.
4. Sends each qualifying cluster to LLM for abstraction.
5. Creates consensus memory + user_profile card updates.

Configuration
-------------
All tunables from ``mneme.config.Settings``:

* ``worker_sublimation_enabled`` (default ``True``)
* ``worker_sublimation_interval_seconds`` (default 600 = 10 min)
* ``worker_sublimation_min_cluster_size`` (default 5)
* ``worker_sublimation_min_similarity`` (default 0.80)
* ``worker_sublimation_max_clusters`` (default 10)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from mneme.api.context import ActorContext, RequestContext
from mneme.config import get_settings
from mneme.db.base import SessionLocal
from mneme.memory.sublimation import (
    SublimationResult,
    cluster_similar_memories,
    abstract_cluster_with_llm,
    apply_sublimation,
    fetch_active_memories,
)

logger = logging.getLogger(__name__)

_SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"


def _make_system_context() -> RequestContext:
    """Build a minimal RequestContext for system-initiated actions."""
    from uuid import UUID
    return RequestContext(
        request_id=uuid4(),
        correlation_id=uuid4(),
        actor=ActorContext(
            actor_type="system",
            actor_id=UUID(_SYSTEM_USER_ID),
        ),
        idempotency_key=None,
    )


@dataclass
class SublimationSweepResult:
    """Result of a single sublimation sweeper cycle."""

    memories_scanned: int = 0
    clusters_found: int = 0
    clusters_qualified: int = 0
    insights_generated: int = 0
    cards_created: int = 0
    consensus_memories_created: int = 0
    relations_created: int = 0
    errors: int = 0


class SublimationSweeper:
    """Periodic sweeper that runs memory sublimation pipeline.

    Usage::

        sweeper = SublimationSweeper()
        result = sweeper.sweep()
        # result.insights_generated, .cards_created, ...
    """

    def __init__(self) -> None:
        self._settings = get_settings()

    def sweep(self) -> SublimationSweepResult:
        """Execute one full sublimation sweep cycle.

        Returns
        -------
        SublimationSweepResult
            Summary counters for the sweep.
        """
        result = SublimationSweepResult()

        try:
            from mneme.gateway.call import Gateway
            gateway = Gateway()
        except Exception as exc:
            logger.warning(
                "sublimation_sweeper: cannot create Gateway (%s), skipping cycle",
                exc,
            )
            result.errors += 1
            return result

        db = SessionLocal()
        try:
            context = _make_system_context()

            # Phase 1: Fetch + cluster
            snapshots = fetch_active_memories(db)
            result.memories_scanned = len(snapshots)
            logger.info(
                "sublimation_sweeper: scanned %d active memories",
                len(snapshots),
            )

            if len(snapshots) < self._settings.worker_sublimation_min_cluster_size:
                return result

            clusters = cluster_similar_memories(
                snapshots,
                min_similarity=self._settings.worker_sublimation_min_similarity,
                min_cluster_size=self._settings.worker_sublimation_min_cluster_size,
                max_clusters=self._settings.worker_sublimation_max_clusters,
            )
            result.clusters_found = len(clusters)
            result.clusters_qualified = len(clusters)

            if not clusters:
                logger.debug("sublimation_sweeper: no qualified clusters found")
                return result

            logger.info(
                "sublimation_sweeper: found %d clusters (≥%d members, sim≥%.2f)",
                len(clusters),
                self._settings.worker_sublimation_min_cluster_size,
                self._settings.worker_sublimation_min_similarity,
            )

            # Phase 2: LLM abstraction
            for cluster in clusters:
                try:
                    abstract_cluster_with_llm(
                        cluster,
                        gateway=gateway,
                    )
                    if cluster.abstracted_insight:
                        result.insights_generated += 1
                except Exception as exc:
                    logger.error(
                        "sublimation_sweeper: LLM abstraction error for cluster %s: %s",
                        cluster.cluster_id,
                        exc,
                    )
                    result.errors += 1

            # Phase 3: Apply — create consensus memories + profile cards
            for cluster in clusters:
                if not cluster.abstracted_insight:
                    continue
                try:
                    apply_output = apply_sublimation(
                        db,
                        context,
                        cluster=cluster,
                        create_consensus_memory=True,
                        create_profile_card=True,
                        create_notification=False,  # keep it quiet by default
                    )
                    if apply_output.get("consensus_memory_id"):
                        result.consensus_memories_created += 1
                    if apply_output.get("card_id"):
                        result.cards_created += 1
                    result.relations_created += apply_output.get("relations_created", 0)
                except Exception as exc:
                    logger.error(
                        "sublimation_sweeper: apply failed for cluster %s: %s",
                        cluster.cluster_id,
                        exc,
                        exc_info=True,
                    )
                    result.errors += 1

            db.commit()

            logger.info(
                "sublimation_sweeper: cycle complete — scanned=%d clusters=%d "
                "insights=%d cards=%d consensus=%d relations=%d errors=%d",
                result.memories_scanned,
                result.clusters_found,
                result.insights_generated,
                result.cards_created,
                result.consensus_memories_created,
                result.relations_created,
                result.errors,
            )

        except Exception as exc:
            logger.error("sublimation_sweeper: sweep cycle error: %s", exc, exc_info=True)
            db.rollback()
            result.errors += 1
        finally:
            db.close()

        return result


def create_sublimation_sweeper() -> SublimationSweeper:
    """Factory function for creating a SublimationSweeper instance."""
    return SublimationSweeper()
