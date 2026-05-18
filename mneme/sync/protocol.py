"""Sync protocol data types and building blocks for federation (L7-02).

This module defines the protocol-level types used for inter-instance sync.
It is *pure data* — no database dependencies — so it can be imported by API
routes and workers alike.

Protocol
--------
* Transport: HTTPS REST (mutual TLS recommended in production).
* Encoding: JSON.
* Authentication: Bearer token + node public-key signature (optional).
* Idempotency: ``(node_id, stream_type, stream_id, stream_version)``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


SYNC_PROTOCOL_VERSION = "1.0"
SYNC_PROTOCOL_MIN_VERSION = "1.0"


# ═══════════════════════════════════════════════════════════════════════
# Protocol Data Types
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SyncPayload:
    """One sync-payload unit — mirrors an event_log row for transfer."""

    stream_type: str
    stream_id: UUID
    stream_version: int
    event_type: str
    payload_json: dict[str, Any] = field(default_factory=dict)
    metadata_json: dict[str, Any] = field(default_factory=dict)
    committed_at: str  # ISO-8601
    project_id: UUID | None = None
    actor_type: str | None = None
    actor_id: UUID | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "stream_type": self.stream_type,
            "stream_id": str(self.stream_id),
            "stream_version": self.stream_version,
            "event_type": self.event_type,
            "payload_json": self.payload_json,
            "metadata_json": self.metadata_json,
            "committed_at": self.committed_at,
        }
        if self.project_id:
            d["project_id"] = str(self.project_id)
        if self.actor_type:
            d["actor_type"] = self.actor_type
        if self.actor_id:
            d["actor_id"] = str(self.actor_id)
        if self.correlation_id:
            d["correlation_id"] = str(self.correlation_id)
        if self.causation_id:
            d["causation_id"] = str(self.causation_id)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SyncPayload:
        return cls(
            stream_type=d["stream_type"],
            stream_id=UUID(d["stream_id"]),
            stream_version=d["stream_version"],
            event_type=d["event_type"],
            payload_json=d.get("payload_json", {}),
            metadata_json=d.get("metadata_json", {}),
            committed_at=d["committed_at"],
            project_id=UUID(d["project_id"]) if d.get("project_id") else None,
            actor_type=d.get("actor_type"),
            actor_id=UUID(d["actor_id"]) if d.get("actor_id") else None,
            correlation_id=UUID(d["correlation_id"]) if d.get("correlation_id") else None,
            causation_id=UUID(d["causation_id"]) if d.get("causation_id") else None,
        )

    def content_hash(self) -> str:
        """SHA-256 content hash for dedup / integrity check."""
        raw = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConflictEntry:
    """Describes one conflict detected during sync."""

    stream_type: str
    stream_id: UUID
    stream_version: int
    local_version: int
    remote_version: int
    reason: str = "version_conflict"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stream_type": self.stream_type,
            "stream_id": str(self.stream_id),
            "stream_version": self.stream_version,
            "local_version": self.local_version,
            "remote_version": self.remote_version,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SyncResult:
    """Result of processing one sync payload on the receiver side."""

    accepted: bool
    stream_type: str
    stream_id: UUID
    stream_version: int
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "accepted": self.accepted,
            "stream_type": self.stream_type,
            "stream_id": str(self.stream_id),
            "stream_version": self.stream_version,
        }
        if self.reason:
            d["reason"] = self.reason
        return d


# ═══════════════════════════════════════════════════════════════════════
# Batch Protocol
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SyncBatchRequest:
    """Request body for a sync push from source to target."""

    source_node_id: UUID
    protocol_version: str = SYNC_PROTOCOL_VERSION
    payloads: list[SyncPayload] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_node_id": str(self.source_node_id),
            "protocol_version": self.protocol_version,
            "payloads": [p.to_dict() for p in self.payloads],
            "timestamp": self.timestamp,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class SyncBatchResponse:
    """Response from target after processing a sync push."""

    accepted_count: int = 0
    conflict_count: int = 0
    skipped_count: int = 0
    errors: list[SyncResult] = field(default_factory=list)
    conflicts: list[ConflictEntry] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted_count": self.accepted_count,
            "conflict_count": self.conflict_count,
            "skipped_count": self.skipped_count,
            "errors": [e.to_dict() for e in self.errors],
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


# ═══════════════════════════════════════════════════════════════════════
# Node Handshake
# ═══════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class NodeHandshakeRequest:
    """Sent by a joining node to establish trust."""

    node_code: str
    display_name: str
    instance_url: str
    api_version: str = SYNC_PROTOCOL_VERSION
    public_key: str | None = None
    nonce: str = field(default_factory=lambda: hashlib.sha256(str(time.time()).encode()).hexdigest()[:16])
    signature: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_code": self.node_code,
            "display_name": self.display_name,
            "instance_url": self.instance_url,
            "api_version": self.api_version,
            "public_key": self.public_key,
            "nonce": self.nonce,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class NodeHandshakeResponse:
    """Response to a handshake request."""

    accepted: bool
    node_id: UUID | None = None
    remote_node_code: str | None = None
    protocol_version: str = SYNC_PROTOCOL_VERSION
    challenge: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "accepted": self.accepted,
            "protocol_version": self.protocol_version,
        }
        if self.node_id:
            d["node_id"] = str(self.node_id)
        if self.remote_node_code:
            d["remote_node_code"] = self.remote_node_code
        if self.challenge:
            d["challenge"] = self.challenge
        if self.error:
            d["error"] = self.error
        return d


# ═══════════════════════════════════════════════════════════════════════
# Protocol Helpers
# ═══════════════════════════════════════════════════════════════════════


def build_sync_payload(
    *,
    stream_type: str,
    stream_id: UUID,
    stream_version: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    committed_at: str,
    project_id: UUID | None = None,
    actor_type: str | None = None,
    actor_id: UUID | None = None,
    correlation_id: UUID | None = None,
    causation_id: UUID | None = None,
) -> SyncPayload:
    """Convenience factory for ``SyncPayload``."""
    return SyncPayload(
        stream_type=stream_type,
        stream_id=stream_id,
        stream_version=stream_version,
        event_type=event_type,
        payload_json=payload or {},
        metadata_json=metadata or {},
        committed_at=committed_at,
        project_id=project_id,
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
        causation_id=causation_id,
    )


def build_handshake(
    node_code: str,
    display_name: str,
    instance_url: str,
    api_version: str = SYNC_PROTOCOL_VERSION,
    public_key: str | None = None,
    secret: str | None = None,
) -> NodeHandshakeRequest:
    """Build a signed handshake request.

    If *secret* is provided, the nonce is HMAC-signed for authenticity.
    """
    req = NodeHandshakeRequest(
        node_code=node_code,
        display_name=display_name,
        instance_url=instance_url,
        api_version=api_version,
        public_key=public_key,
    )
    if secret:
        mac = hmac.new(
            secret.encode("utf-8"),
            req.nonce.encode("utf-8"),
            hashlib.sha256,
        )
        object.__setattr__(req, "signature", mac.hexdigest())
    return req


def verify_node_identity(
    handshake: NodeHandshakeRequest,
    *,
    expected_secret: str | None = None,
    expected_public_key: str | None = None,
) -> bool:
    """Verify a handshake request's identity.

    Supports two modes:
    1. HMAC secret verification (symmetric).
    2. Public key verification (asymmetric) — stub for future RSA/Ed25519.
    """
    if expected_secret and handshake.signature:
        mac = hmac.new(
            expected_secret.encode("utf-8"),
            handshake.nonce.encode("utf-8"),
            hashlib.sha256,
        )
        return hmac.compare_digest(mac.hexdigest(), handshake.signature)

    if expected_public_key and handshake.public_key:
        # TODO(L7): Implement asymmetric signature verification
        # For now, accept if public keys match
        return handshake.public_key == expected_public_key

    # No verification configured — trust by network (dev mode)
    return True
