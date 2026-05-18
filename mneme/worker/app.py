"""Worker process entry-point.

Phase 1 behaviour
-----------------

1. Loads configuration and sets up structured logging.
2. Resolves the Redis connection (graceful degradation if unavailable).
3. Creates the :class:`~mneme.worker.dispatcher.Dispatcher` and registers the
   :class:`~mneme.worker.dispatcher.NoopConsumer`.
4. Enters the main poll -> dispatch loop:
   a. Poll the outbox for pending events.
   b. Dispatch to consumers.
   c. Sleep for *poll_interval_seconds*.
5. Handles SIGTERM / SIGINT for graceful shutdown.

Phase 2 behaviour (P2-01)
--------------------------

* Redis lease acquisition before polling (leader election via
  :class:`~mneme.worker.lease.LeaseManager`).
* Only the lease holder executes the poll → dispatch loop.
* Standby workers sleep and periodically retry acquisition.
* Heartbeat sent every ``worker_lease_heartbeat_interval_seconds``.
* Lease released on graceful shutdown; automatically expires on crash.

Phase 2 behaviour (P2-02)
--------------------------

* :class:`~mneme.worker.retry_sweeper.RetrySweeper` scans failed
  ``event_deliveries`` and re-queues them with exponential backoff;
  exhausted deliveries are promoted to ``dead_letters``.
* :class:`~mneme.worker.recovery_sweeper.DispatchingRecoverySweeper`
  recovers events stuck in ``'dispatching'`` state after a worker crash.
* Both sweepers run on independent intervals, only when the lease is held.

Phase 2 behaviour (P2-07)
---------------------------

* :class:`~mneme.worker.consumers.review_consumer.ReviewEventConsumer` is
  registered to handle ``review.*`` outbox events, triggering follow-up
  actions (DLQ replay, restore execution, etc.).
* :func:`~mneme.worker.review_timeout_checker.check_expired_reviews` runs
  periodically to detect and expire review items past their ``expires_at``
  timestamp, publishing ``review.expired`` outbox events.

Phase 2+
--------

* Multiple real consumers (notification, webhook, pipeline, ...).
* Health endpoint for worker liveness / readiness probes.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Optional

import redis
from redis.exceptions import RedisError

from mneme.config import get_settings
from mneme.db.base import check_database_connection
from mneme.logging import configure_logging

from .consumers import ReviewEventConsumer, PipelineEventConsumer, MemoryEventConsumer
from .decay_sweeper import DecaySweeper, create_decay_sweeper
from .dispatcher import Dispatcher, NoopConsumer
from .emotion_sweeper import EmotionSweeper, create_emotion_sweeper
from .lease import LeaseManager
from .poller import fetch_pending_events
from .retry_sweeper import create_retry_sweeper
from .recovery_sweeper import create_recovery_sweeper
from .review_timeout_checker import check_expired_reviews
from .spontaneous_recall import SpontaneousRecallSweeper, create_spontaneous_recall_sweeper
from .sublimation_sweeper import SublimationSweeper, create_sublimation_sweeper

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────────

DEFAULT_POLL_INTERVAL_SECONDS = 5
DEFAULT_BATCH_SIZE = 20


# ── Shutdown flag ───────────────────────────────────────────────────────────────

_running = True


def _stop(signum: int, frame: object) -> None:
    global _running
    logger.info("received signal %s – shutting down gracefully", signum)
    _running = False


# ── Redis helper ────────────────────────────────────────────────────────────────


class RedisConnection:
    """Lightweight Redis connection handle with graceful degradation.

    Phase 2 uses Redis for distributed leases.  When Redis is unavailable the
    worker logs an error and enters standby / retry mode since it cannot
    safely process events without a lease.
    """

    def __init__(self, redis_url: str) -> None:
        self._redis_url = redis_url
        self._client: Optional[redis.Redis] = None
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        """Return ``True`` if Redis was reachable at the last probe."""
        if self._available is None:
            self._probe()
        return self._available is True

    @property
    def client(self) -> Optional[redis.Redis]:
        """Return the underlying Redis client, or ``None`` if unavailable."""
        if self._available is None:
            self._probe()
        return self._client

    def _probe(self) -> None:
        try:
            client = redis.Redis.from_url(self._redis_url, socket_connect_timeout=2)
            client.ping()
            self._client = client
            self._available = True
            logger.info("redis connected – url=%s", self._redis_url)
        except RedisError as exc:
            self._client = None
            self._available = False
            logger.warning(
                "redis unavailable (degraded mode) – url=%s error=%s",
                self._redis_url,
                exc,
            )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except RedisError:
                pass
            self._client = None
        self._available = None

    def refresh(self) -> bool:
        """Force a new Redis connectivity probe.

        Unlike :attr:`available`, which returns a cached result, this
        always performs a fresh ``PING``.

        Returns
        -------
        bool
            ``True`` if Redis is reachable.
        """
        self._probe()
        return self._available is True


# ── Main loop ───────────────────────────────────────────────────────────────────


def run_loop(
    *,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> None:
    """Start the worker main loop.

    Blocks until a stop signal is received.

    In Phase 2 the worker MUST hold a Redis-based distributed lease before
    it is allowed to poll the outbox and dispatch events.  If the lease
    cannot be acquired the worker enters standby mode and retries
    periodically.

    Parameters
    ----------
    poll_interval : float
        Seconds to sleep between poll cycles (when lease is held).
    batch_size : int
        Maximum number of pending events to fetch per cycle.
    """
    global _running
    _running = True

    settings = get_settings()
    configure_logging(settings.log_level)

    # ── Signal handlers ─────────────────────────────────────────────────────
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    logger.info(
        "mneme worker starting – env=%s poll_interval_s=%s batch_size=%s",
        settings.environment,
        poll_interval,
        batch_size,
    )

    # ── Database check ──────────────────────────────────────────────────────
    try:
        check_database_connection()
        logger.info("database connection ok")
    except Exception as exc:
        logger.critical("database unreachable – exiting: %s", exc)
        return

    # ── Redis check ─────────────────────────────────────────────────────────
    redis_conn = RedisConnection(settings.redis_url)

    if not redis_conn.available or redis_conn.client is None:
        logger.critical(
            "redis unavailable – worker cannot acquire lease, entering "
            "standby/retry mode"
        )
        _standby_loop(redis_conn, settings, poll_interval, batch_size)
        return

    # ── Dispatcher ───────────────────────────────────────────────────────────
    dispatcher = Dispatcher()
    dispatcher.register(NoopConsumer())
    dispatcher.register(ReviewEventConsumer())
    dispatcher.register(PipelineEventConsumer())
    dispatcher.register(MemoryEventConsumer())
    logger.info(
        "dispatcher initialised – consumers=%s",
        [c.name for c in dispatcher.consumers],
    )

    # ── Sweepers ─────────────────────────────────────────────────────────────
    retry_sweeper = create_retry_sweeper()
    recovery_sweeper = create_recovery_sweeper()
    decay_sweeper = create_decay_sweeper()
    emotion_sweeper = create_emotion_sweeper()
    spontaneous_recall_sweeper = create_spontaneous_recall_sweeper()
    sublimation_sweeper = create_sublimation_sweeper()
    logger.info(
        "sweepers initialised – retry_interval=%ss recovery_interval=%ss "
        "decay_interval=%ss emotion_interval=%ss "
        "spontaneous_recall_interval=%ss sublimation_interval=%ss "
        "retry_base=%ss retry_max=%ss max_attempts=%s timeout_check_interval=%ss",
        settings.worker_retry_sweeper_interval_seconds,
        settings.worker_recovery_sweeper_interval_seconds,
        settings.worker_memory_decay_interval_seconds,
        settings.worker_emotion_infer_interval_seconds,
        settings.worker_spontaneous_recall_interval_seconds,
        settings.worker_sublimation_interval_seconds,
        settings.worker_retry_base_delay_seconds,
        settings.worker_retry_max_delay_seconds,
        settings.worker_retry_max_attempts,
        settings.worker_review_timeout_check_interval_seconds,
    )

    # ── Lease Manager ───────────────────────────────────────────────────────
    lease_mgr = LeaseManager(
        redis_conn.client,
        settings.worker_lease_name,
        ttl_seconds=settings.worker_lease_ttl_seconds,
        heartbeat_interval_seconds=settings.worker_lease_heartbeat_interval_seconds,
    )
    logger.info(
        "lease manager initialised – lease_name=%s ttl_s=%s heartbeat_s=%s "
        "instance=%s",
        settings.worker_lease_name,
        settings.worker_lease_ttl_seconds,
        settings.worker_lease_heartbeat_interval_seconds,
        lease_mgr.instance_id,
    )

    # ── Acquire lease or enter standby ──────────────────────────────────────
    if not lease_mgr.acquire():
        logger.info(
            "lease not acquired – entering standby (another worker is active) "
            "key=%s instance=%s",
            lease_mgr.lease_key,
            lease_mgr.instance_id,
        )
        _standby_loop(
            redis_conn, settings, poll_interval, batch_size, lease_mgr=lease_mgr
        )
        return

    _active_loop(
        redis_conn, settings, poll_interval, batch_size,
        dispatcher, lease_mgr,
        retry_sweeper, recovery_sweeper,
        decay_sweeper, emotion_sweeper,
        spontaneous_recall_sweeper, sublimation_sweeper,
        label="primary",
    )
    redis_conn.close()
    logger.info("mneme worker stopped")


# ── Active dispatch + sweepers loop ──────────────────────────────────────────────


def _active_loop(
    redis_conn: RedisConnection,
    settings,
    poll_interval: float,
    batch_size: int,
    dispatcher: Dispatcher,
    lease_mgr: LeaseManager,
    retry_sweeper,
    recovery_sweeper,
    decay_sweeper: DecaySweeper,
    emotion_sweeper: EmotionSweeper,
    spontaneous_recall_sweeper: SpontaneousRecallSweeper,
    sublimation_sweeper: SublimationSweeper,
    *,
    label: str = "",
) -> None:
    """Run the main poll→dispatch loop with periodic sweeper invocations.

    This is the shared active-state loop used by both the primary
    ``run_loop`` entry point and the standby→active transition path.

    Sweepers run on independent intervals and only when the lease is held.
    """
    global _running

    logger.info("entering poll/dispatch loop as lease holder (%s)", label)

    # Track sweeper last-run timestamps
    last_retry_sweep_at = time.monotonic()
    last_recovery_sweep_at = time.monotonic()
    last_timeout_check_at = time.monotonic()
    last_decay_sweep_at = time.monotonic()
    last_emotion_sweep_at = time.monotonic()
    last_spontaneous_recall_at = time.monotonic()
    last_sublimation_at = time.monotonic()

    cycle = 0
    while _running and lease_mgr.is_held:
        cycle += 1

        # ── Heartbeat ───────────────────────────────────────────────────────
        if _should_heartbeat(lease_mgr, cycle, settings):
            if not lease_mgr.heartbeat():
                logger.critical(
                    "lease lost during heartbeat – stopping dispatch "
                    "cycle=%s instance=%s",
                    cycle,
                    lease_mgr.instance_id,
                )
                break

        # ── Retry sweeper (P2-02) ───────────────────────────────────────────
        if _sweeper_due(
            last_retry_sweep_at,
            settings.worker_retry_sweeper_interval_seconds,
        ):
            _run_retry_sweeper(retry_sweeper, cycle)
            last_retry_sweep_at = time.monotonic()

        # ── Recovery sweeper (P2-02 sub-task) ───────────────────────────────
        if _sweeper_due(
            last_recovery_sweep_at,
            settings.worker_recovery_sweeper_interval_seconds,
        ):
            _run_recovery_sweeper(recovery_sweeper, cycle)
            last_recovery_sweep_at = time.monotonic()

        # ── Review timeout checker (P2-07) ──────────────────────────────────
        if _sweeper_due(
            last_timeout_check_at,
            settings.worker_review_timeout_check_interval_seconds,
        ):
            _run_timeout_checker(cycle)
            last_timeout_check_at = time.monotonic()

        # ── Memory decay sweeper (P4-11) ────────────────────────────────────
        if settings.worker_memory_decay_enabled and _sweeper_due(
            last_decay_sweep_at,
            settings.worker_memory_decay_interval_seconds,
        ):
            _run_decay_sweeper(decay_sweeper, cycle)
            last_decay_sweep_at = time.monotonic()

        # ── Emotion inference sweeper (P4-12) ───────────────────────────────
        if settings.worker_emotion_infer_enabled and _sweeper_due(
            last_emotion_sweep_at,
            settings.worker_emotion_infer_interval_seconds,
        ):
            _run_emotion_sweeper(emotion_sweeper, cycle)
            last_emotion_sweep_at = time.monotonic()

        # ── Spontaneous recall sweeper (P6-10): idle扫描→发矛盾→通知 ──────
        if settings.worker_spontaneous_recall_enabled and _sweeper_due(
            last_spontaneous_recall_at,
            settings.worker_spontaneous_recall_interval_seconds,
        ):
            _run_spontaneous_recall_sweeper(spontaneous_recall_sweeper, cycle)
            last_spontaneous_recall_at = time.monotonic()

        # ── Sublimation sweeper (P6-11): 5次相似事件→抽象共识→进画像 ──────
        if settings.worker_sublimation_enabled and _sweeper_due(
            last_sublimation_at,
            settings.worker_sublimation_interval_seconds,
        ):
            _run_sublimation_sweeper(sublimation_sweeper, cycle)
            last_sublimation_at = time.monotonic()

        # ── Poll & dispatch ─────────────────────────────────────────────────
        try:
            pending = fetch_pending_events(limit=batch_size)
        except Exception as exc:
            logger.error("poll cycle %s – outbox query failed: %s", cycle, exc)
            _sleep_interruptible(poll_interval)
            continue

        if pending:
            logger.info(
                "poll cycle %s – fetched %s pending events",
                cycle,
                len(pending),
            )
            try:
                dispatched = dispatcher.dispatch_pending(pending)
                logger.info(
                    "poll cycle %s – dispatched %s event/s to consumers",
                    cycle,
                    dispatched,
                )
            except Exception as exc:
                logger.error("poll cycle %s – dispatch failed: %s", cycle, exc)

        _sleep_interruptible(poll_interval)

    # ── Cleanup ─────────────────────────────────────────────────────────────
    if lease_mgr.is_held:
        lease_mgr.release()
    logger.info("mneme worker stopped after %s cycles (%s)", cycle, label)


# ── Standby loop ────────────────────────────────────────────────────────────────


def _standby_loop(
    redis_conn: RedisConnection,
    settings,
    poll_interval: float,
    batch_size: int,
    *,
    lease_mgr: Optional[LeaseManager] = None,
) -> None:
    """Run when this worker cannot hold the dispatch lease.

    The standby worker sleeps and periodically re-checks Redis availability
    and attempts to acquire the lease.  If the lease is acquired the worker
    transitions to the active role via :func:`_active_loop`.
    """
    logger.info(
        "entering standby loop – will retry lease acquisition every %s seconds",
        settings.worker_lease_ttl_seconds,
    )

    while _running:
        # Re-check Redis availability (force fresh probe)
        if not redis_conn.available:
            redis_conn.refresh()

        if redis_conn.client is not None:
            if lease_mgr is None:
                lease_mgr = LeaseManager(
                    redis_conn.client,
                    settings.worker_lease_name,
                    ttl_seconds=settings.worker_lease_ttl_seconds,
                    heartbeat_interval_seconds=settings.worker_lease_heartbeat_interval_seconds,
                )

            if lease_mgr.acquire():
                logger.info(
                    "lease acquired in standby – transitioning to active role "
                    "instance=%s",
                    lease_mgr.instance_id,
                )
                # Build dispatcher & sweepers, then enter the shared active loop
                _run_active_with_lease(
                    redis_conn,
                    settings,
                    poll_interval,
                    batch_size,
                    lease_mgr,
                )
                return

        _sleep_interruptible(settings.worker_lease_ttl_seconds)


def _run_active_with_lease(
    redis_conn: RedisConnection,
    settings,
    poll_interval: float,
    batch_size: int,
    lease_mgr: LeaseManager,
) -> None:
    """Enter the active dispatch loop with an already-acquired lease.

    Builds the dispatcher and sweepers, then delegates to :func:`_active_loop`.
    """
    from .consumers import ReviewEventConsumer, PipelineEventConsumer, MemoryEventConsumer
    from .decay_sweeper import create_decay_sweeper
    from .dispatcher import Dispatcher, NoopConsumer
    from .emotion_sweeper import create_emotion_sweeper
    from .spontaneous_recall import create_spontaneous_recall_sweeper
    from .sublimation_sweeper import create_sublimation_sweeper

    dispatcher = Dispatcher()
    dispatcher.register(NoopConsumer())
    dispatcher.register(ReviewEventConsumer())
    dispatcher.register(PipelineEventConsumer())
    dispatcher.register(MemoryEventConsumer())
    logger.info(
        "dispatcher initialised – consumers=%s",
        [c.name for c in dispatcher.consumers],
    )

    retry_sweeper = create_retry_sweeper()
    recovery_sweeper = create_recovery_sweeper()
    decay_sweeper = create_decay_sweeper()
    emotion_sweeper = create_emotion_sweeper()
    spontaneous_recall_sweeper = create_spontaneous_recall_sweeper()
    sublimation_sweeper = create_sublimation_sweeper()
    logger.info(
        "sweepers initialised (from standby) – retry_interval=%ss "
        "recovery_interval=%ss decay_interval=%ss emotion_interval=%ss "
        "spontaneous_recall_interval=%ss sublimation_interval=%ss "
        "timeout_check_interval=%ss",
        settings.worker_retry_sweeper_interval_seconds,
        settings.worker_recovery_sweeper_interval_seconds,
        settings.worker_memory_decay_interval_seconds,
        settings.worker_emotion_infer_interval_seconds,
        settings.worker_spontaneous_recall_interval_seconds,
        settings.worker_sublimation_interval_seconds,
        settings.worker_review_timeout_check_interval_seconds,
    )

    _active_loop(
        redis_conn, settings, poll_interval, batch_size,
        dispatcher, lease_mgr,
        retry_sweeper, recovery_sweeper,
        decay_sweeper, emotion_sweeper,
        spontaneous_recall_sweeper, sublimation_sweeper,
        label="from-standby",
    )


# ── Sweeper helpers (P2-02) ──────────────────────────────────────────────────────


def _sweeper_due(last_run_at: float, interval_seconds: int) -> bool:
    """Return ``True`` if *interval_seconds* have elapsed since *last_run_at*."""
    return (time.monotonic() - last_run_at) >= interval_seconds


def _run_retry_sweeper(sweeper, cycle: int) -> None:
    """Execute one retry sweeper cycle and log results."""
    try:
        result = sweeper.sweep()
        if result["retried"] or result["dead_lettered"]:
            logger.info(
                "retry sweeper cycle %s – retried=%s dead_lettered=%s errors=%s",
                cycle,
                result["retried"],
                result["dead_lettered"],
                result["errors"],
            )
        elif result["errors"]:
            logger.warning(
                "retry sweeper cycle %s – errors=%s",
                cycle,
                result["errors"],
            )
    except Exception as exc:
        logger.error("retry sweeper cycle %s – unexpected error: %s", cycle, exc)


def _run_recovery_sweeper(sweeper, cycle: int) -> None:
    """Execute one recovery sweeper cycle and log results."""
    try:
        recovered = sweeper.sweep()
        if recovered > 0:
            logger.warning(
                "recovery sweeper cycle %s – recovered %s stuck event/s",
                cycle,
                recovered,
            )
    except Exception as exc:
        logger.error("recovery sweeper cycle %s – unexpected error: %s", cycle, exc)


def _run_timeout_checker(cycle: int) -> None:
    """Execute one review timeout checker cycle and log results."""
    try:
        result = check_expired_reviews()
        if result["expired"] or result["errors"]:
            logger.info(
                "review timeout checker cycle %s – expired=%d errors=%d",
                cycle,
                result["expired"],
                result["errors"],
            )
    except Exception as exc:
        logger.error(
            "review timeout checker cycle %s – unexpected error: %s",
            cycle,
            exc,
        )


def _run_decay_sweeper(sweeper: DecaySweeper, cycle: int) -> None:
    """Execute one memory decay sweeper cycle and log results."""
    try:
        result = sweeper.sweep()
        if result.memories_processed or result.state_transitions:
            logger.info(
                "decay sweeper cycle %s – processed=%d scores=%d "
                "transitions=%d errors=%d",
                cycle,
                result.memories_processed,
                result.scores_updated,
                result.state_transitions,
                result.errors,
            )
        elif result.errors:
            logger.warning(
                "decay sweeper cycle %s – errors=%d", cycle, result.errors,
            )
    except Exception as exc:
        logger.error("decay sweeper cycle %s – unexpected error: %s", cycle, exc)


def _run_emotion_sweeper(sweeper: EmotionSweeper, cycle: int) -> None:
    """Execute one emotion inference sweeper cycle and log results."""
    try:
        result = sweeper.sweep()
        if result.emotions_updated:
            logger.info(
                "emotion sweeper cycle %s – processed=%d updated=%d "
                "avg_uncertainty=%.2f errors=%d",
                cycle,
                result.memories_processed,
                result.emotions_updated,
                result.avg_uncertainty,
                result.errors,
            )
        elif result.errors:
            logger.warning(
                "emotion sweeper cycle %s – errors=%d", cycle, result.errors,
            )
    except Exception as exc:
        logger.error("emotion sweeper cycle %s – unexpected error: %s", cycle, exc)


def _run_spontaneous_recall_sweeper(
    sweeper: SpontaneousRecallSweeper, cycle: int,
) -> None:
    """Execute one spontaneous recall sweeper cycle and log results.

    空闲扫描 → 发现矛盾 → 创建通知。
    When conflicts are confirmed, inbox notifications and review items are
    created automatically so users are alerted without manual intervention.
    """
    try:
        result = sweeper.sweep()
        if result.conflicts_confirmed or result.inbox_notifications:
            logger.info(
                "spontaneous_recall cycle %s – scanned=%d pairs=%d "
                "conflicts=%d relations=%d inbox=%d reviews=%d errors=%d",
                cycle,
                result.memories_scanned,
                result.pairs_evaluated,
                result.conflicts_confirmed,
                result.relations_created,
                result.inbox_notifications,
                result.review_items_created,
                result.errors,
            )
        elif result.errors:
            logger.warning(
                "spontaneous_recall cycle %s – errors=%d", cycle, result.errors,
            )
    except Exception as exc:
        logger.error(
            "spontaneous_recall cycle %s – unexpected error: %s", cycle, exc,
        )


def _run_sublimation_sweeper(
    sweeper: SublimationSweeper, cycle: int,
) -> None:
    """Execute one sublimation sweeper cycle and log results.

    5次相似事件 → LLM抽象共识 → 创建共识记忆 + 写入用户画像。
    Repeated similar memory events are detected, clustered, and abstracted
    into higher-level consensus knowledge via LLM, then stored as profile
    cards so the agent develops a richer understanding of the user over time.
    """
    try:
        result = sweeper.sweep()
        if result.insights_generated or result.consensus_memories_created:
            logger.info(
                "sublimation cycle %s – scanned=%d clusters_q=%d "
                "insights=%d consensus=%d cards=%d relations=%d errors=%d",
                cycle,
                result.memories_scanned,
                result.clusters_qualified,
                result.insights_generated,
                result.consensus_memories_created,
                result.cards_created,
                result.relations_created,
                result.errors,
            )
        elif result.errors:
            logger.warning(
                "sublimation cycle %s – errors=%d", cycle, result.errors,
            )
    except Exception as exc:
        logger.error(
            "sublimation cycle %s – unexpected error: %s", cycle, exc,
        )


# ── Heartbeat scheduling ────────────────────────────────────────────────────────


def _should_heartbeat(
    lease_mgr: LeaseManager,
    cycle: int,
    settings,
) -> bool:
    """Return ``True`` if a heartbeat should be sent this cycle.

    Heartbeat is sent every ``worker_lease_heartbeat_interval_seconds``.
    We use a simple time-based check rather than cycle-counting because
    poll+dispatch cycles can vary in duration.
    """
    return lease_mgr.heartbeat_is_due(
        settings.worker_lease_heartbeat_interval_seconds
    )


# ── Sleep helper ────────────────────────────────────────────────────────────────


def _sleep_interruptible(seconds: float) -> None:
    """Sleep in small increments so SIGTERM is handled promptly."""
    if not _running:
        return
    end = time.monotonic() + seconds
    while _running and time.monotonic() < end:
        time.sleep(min(0.5, max(0.1, seconds / 10)))


# ── CLI entry-point ─────────────────────────────────────────────────────────────


def main() -> None:
    """Entry-point for ``python -m mneme.worker`` (used by Docker Compose)."""
    run_loop()


if __name__ == "__main__":
    main()
