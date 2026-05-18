"""Distributed worker lease via Redis ``SET NX PX``.

P2-01: Only the worker that holds the dispatch lease is allowed to run the
outbox poll → dispatch loop.  This prevents duplicate processing when
multiple worker pods are deployed.

Architecture
------------

* **Lease key** – ``mneme:worker:lease:{lease_name}`` (e.g. ``dispatcher``).
* **Lease value** – a unique *worker instance id* so a worker can recognise
  its own lease during heartbeat / release.
* **Acquisition** – ``SET key value NX PX ttl_ms``.  *NX* guarantees
  atomicity (only succeeds if the key does not exist); *PX* sets the TTL in
  milliseconds so a crashed worker's lease is automatically released.
* **Heartbeat** – Lua script that extends the TTL **only if the current value
  still matches our instance id**.  This prevents a slow / partitioned worker
  from extending a lease that was already taken by another instance.
* **Release** – Lua script that deletes the key **only if the value matches**,
  again avoiding an accidental delete of another worker's lease.

Configuration
-------------

All tunables come from :class:`mneme.config.Settings`:

* ``worker_lease_ttl_seconds`` (default 30)
* ``worker_lease_heartbeat_interval_seconds`` (default 10)
* ``worker_lease_name`` (default ``"dispatcher"``)

Structured logging covers acquire / heartbeat / release / expire events.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from uuid import uuid4

import redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# ── Redis key prefix ─────────────────────────────────────────────────────────

LEASE_KEY_PREFIX = "mneme:worker:lease"

# ── Lua scripts ──────────────────────────────────────────────────────────────

# Atomic heartbeat: extend TTL only if the current value matches our
# instance id.  Returns 1 on success, 0 otherwise (lease lost / stolen).
_HEARTBEAT_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

# Atomic release: delete the key only if the value matches our instance id.
# Returns 1 on success, 0 otherwise.
_RELEASE_LUA = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class LeaseManager:
    """Redis-backed distributed lease for leader election among workers.

    Typical usage inside the worker main loop::

        lease_mgr = LeaseManager(redis_client, "dispatcher")
        if lease_mgr.acquire():
            # I am the leader — run the poll/dispatch loop.
            while running:
                lease_mgr.heartbeat()
                ...
            lease_mgr.release()
        else:
            # Standby — another worker holds the lease.
            while running:
                sleep(lease_ttl)
                if lease_mgr.acquire():
                    ...  # became leader after TTL expired
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        lease_name: str,
        *,
        ttl_seconds: int = 30,
        heartbeat_interval_seconds: int = 10,
    ) -> None:
        self._redis = redis_client
        self._lease_name = lease_name
        self._ttl_ms = ttl_seconds * 1000
        self._heartbeat_interval = heartbeat_interval_seconds

        # Unique identifier for this worker instance.
        # Stored as the lease value so we can verify ownership.
        self._instance_id: str = str(uuid4())

        # Timestamp of last *successful* heartbeat (monotonic seconds).
        self._last_heartbeat_at: float = 0.0

        # Track whether we believe we currently hold the lease.
        self._held: bool = False

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def instance_id(self) -> str:
        """Unique id for this worker instance (the Redis lease value)."""
        return self._instance_id

    @property
    def lease_key(self) -> str:
        """Full Redis key used for this lease."""
        return f"{LEASE_KEY_PREFIX}:{self._lease_name}"

    @property
    def is_held(self) -> bool:
        """Return ``True`` if this instance believes it holds the lease.

        This is a local flag that is set after a successful :meth:`acquire`
        and cleared after :meth:`release` or a failed :meth:`heartbeat`.
        It does **not** perform a round-trip to Redis — use
        :meth:`check_held` for that.
        """
        return self._held

    @property
    def ttl_ms(self) -> int:
        return self._ttl_ms

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_ms // 1000

    @property
    def seconds_since_last_heartbeat(self) -> float:
        """Elapsed monotonic seconds since the last successful heartbeat."""
        return time.monotonic() - self._last_heartbeat_at

    def heartbeat_is_due(self, interval_seconds: float) -> bool:
        """Return ``True`` if a heartbeat should be sent now.

        Convenience wrapper used by the main loop's heartbeat scheduler.
        """
        return self.seconds_since_last_heartbeat >= interval_seconds

    def acquire(self) -> bool:
        """Try to obtain the lease.

        Uses ``SET key value NX PX ttl_ms`` so the operation is atomic:
        either the key is created (and we become the leader) or it already
        exists (another worker is the leader).

        On success sets :attr:`is_held` to ``True`` and records the
        heartbeat timestamp.

        Returns
        -------
        bool
            ``True`` if the lease was acquired, ``False`` if another worker
            holds it or Redis is unreachable.
        """
        try:
            # SET key value NX PX ttl_ms → OK if key did not exist, else nil.
            result = self._redis.set(
                self.lease_key,
                self._instance_id,
                nx=True,
                px=self._ttl_ms,
            )
        except RedisError as exc:
            logger.error(
                "lease acquire failed – redis error key=%s instance=%s error=%s",
                self.lease_key,
                self._instance_id,
                exc,
            )
            return False

        if result is True:  # "OK" response from Redis
            self._held = True
            self._last_heartbeat_at = time.monotonic()
            logger.info(
                "lease acquired – key=%s instance=%s ttl_s=%s",
                self.lease_key,
                self._instance_id,
                self._ttl_ms // 1000,
            )
            return True

        logger.debug(
            "lease not acquired – already held key=%s instance=%s",
            self.lease_key,
            self._instance_id,
        )
        return False

    def heartbeat(self) -> bool:
        """Refresh the lease TTL.

        Uses a Lua script to atomically check that the current lease value
        matches our instance id before extending.  This prevents a worker
        from extending a lease that was already stolen by another instance.

        If the heartbeat fails (lease expired or stolen), :attr:`is_held`
        is set to ``False``.

        Returns
        -------
        bool
            ``True`` if the heartbeat succeeded, ``False`` if the lease was
            lost or Redis is unreachable.
        """
        if not self._held:
            logger.debug(
                "heartbeat skipped – lease not held key=%s instance=%s",
                self.lease_key,
                self._instance_id,
            )
            return False

        try:
            result = self._redis.eval(
                _HEARTBEAT_LUA,
                1,  # num keys
                self.lease_key,
                self._instance_id,
                str(self._ttl_ms),
            )
        except RedisError as exc:
            logger.error(
                "heartbeat failed – redis error key=%s instance=%s error=%s",
                self.lease_key,
                self._instance_id,
                exc,
            )
            self._held = False
            return False

        if result == 1:
            self._last_heartbeat_at = time.monotonic()
            return True

        # Lease was lost (stolen by another worker or expired)
        logger.warning(
            "heartbeat lost – lease stolen/expired key=%s instance=%s",
            self.lease_key,
            self._instance_id,
        )
        self._held = False
        return False

    def release(self) -> bool:
        """Voluntarily release the lease.

        Uses a Lua script to atomically delete the key only if the current
        value matches our instance id.

        Returns
        -------
        bool
            ``True`` if the lease was released, ``False`` if we did not
            hold it or Redis is unreachable.
        """
        if not self._held:
            logger.debug(
                "release skipped – lease not held key=%s instance=%s",
                self.lease_key,
                self._instance_id,
            )
            return False

        try:
            result = self._redis.eval(
                _RELEASE_LUA,
                1,  # num keys
                self.lease_key,
                self._instance_id,
            )
        except RedisError as exc:
            logger.error(
                "release failed – redis error key=%s instance=%s error=%s",
                self.lease_key,
                self._instance_id,
                exc,
            )
            self._held = False
            return False

        if result == 1:
            logger.info(
                "lease released – key=%s instance=%s",
                self.lease_key,
                self._instance_id,
            )
        else:
            logger.warning(
                "release no-op – lease not owned key=%s instance=%s",
                self.lease_key,
                self._instance_id,
            )

        self._held = False
        return result == 1

    def check_held(self) -> bool:
        """Check whether we still hold the lease by querying Redis.

        Unlike :attr:`is_held` this performs a real round-trip to Redis
        and compares the stored value to our instance id.

        Returns
        -------
        bool
            ``True`` if the lease key exists and contains our instance id.
        """
        try:
            current = self._redis.get(self.lease_key)
        except RedisError as exc:
            logger.error(
                "check_held failed – redis error key=%s instance=%s error=%s",
                self.lease_key,
                self._instance_id,
                exc,
            )
            return False

        if current is not None and current.decode("utf-8") == self._instance_id:
            return True
        return False
