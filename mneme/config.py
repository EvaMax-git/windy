from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: str = Field(default="local", alias="MNEME_ENV")
    log_level: str = Field(default="INFO", alias="MNEME_LOG_LEVEL")
    database_url: str = Field(alias="DATABASE_URL")
    redis_url: str = Field(alias="REDIS_URL")
    session_ttl_hours: int = Field(default=24, alias="MNEME_SESSION_TTL_HOURS", ge=1)
    session_cookie_name: str = Field(default="mneme_session", alias="MNEME_SESSION_COOKIE_NAME")
    session_cookie_secure: bool = Field(default=False, alias="MNEME_SESSION_COOKIE_SECURE")
    frontend_origins: str = Field(
        default="http://localhost:5173,http://localhost:5174,http://127.0.0.1:5173,http://127.0.0.1:5174,http://192.168.31.87:5173,http://192.168.31.87:5174",
        alias="MNEME_FRONTEND_ORIGINS",
        description="Comma-separated list of allowed CORS origins for the frontend.",
    )

    @property
    def frontend_origin_list(self) -> list[str]:
        return [o.strip() for o in self.frontend_origins.split(",") if o.strip()]
    bootstrap_owner_username: str = Field(default="owner", alias="MNEME_BOOTSTRAP_OWNER_USERNAME")
    bootstrap_owner_email: str | None = Field(default=None, alias="MNEME_BOOTSTRAP_OWNER_EMAIL")
    bootstrap_owner_password: SecretStr | None = Field(default=None, alias="MNEME_BOOTSTRAP_OWNER_PASSWORD")

    # ── Worker / Lease ─────────────────────────────────────────────────────────
    worker_lease_ttl_seconds: int = Field(
        default=30, alias="MNEME_WORKER_LEASE_TTL_SECONDS", ge=5
    )
    worker_lease_heartbeat_interval_seconds: int = Field(
        default=10, alias="MNEME_WORKER_LEASE_HEARTBEAT_INTERVAL_SECONDS", ge=1
    )
    worker_lease_name: str = Field(
        default="dispatcher", alias="MNEME_WORKER_LEASE_NAME"
    )

    # ── Worker / Retry Sweeper (P2-02) ────────────────────────────────────────
    worker_retry_base_delay_seconds: int = Field(
        default=5, alias="MNEME_WORKER_RETRY_BASE_DELAY_SECONDS", ge=1,
        description="Base backoff delay in seconds for retry sweeper"
    )
    worker_retry_max_delay_seconds: int = Field(
        default=3600, alias="MNEME_WORKER_RETRY_MAX_DELAY_SECONDS", ge=1,
        description="Maximum backoff ceiling in seconds"
    )
    worker_retry_max_attempts: int = Field(
        default=5, alias="MNEME_WORKER_RETRY_MAX_ATTEMPTS", ge=1,
        description="Maximum dispatch attempts before promotion to dead_letters"
    )
    worker_retry_sweeper_interval_seconds: int = Field(
        default=10, alias="MNEME_WORKER_RETRY_SWEEPER_INTERVAL_SECONDS", ge=1,
        description="Interval in seconds between retry sweeper scan cycles"
    )

    # ── Worker / Recovery Sweeper (P2-02 sub-task) ────────────────────────────
    worker_recovery_sweeper_interval_seconds: int = Field(
        default=30, alias="MNEME_WORKER_RECOVERY_SWEEPER_INTERVAL_SECONDS", ge=1,
        description="Interval in seconds between recovery sweeper scan cycles"
    )
    worker_dispatching_timeout_seconds: int = Field(
        default=120, alias="MNEME_WORKER_DISPATCHING_TIMEOUT_SECONDS", ge=10,
        description="Seconds after which a 'dispatching' event is considered stuck"
    )

    # ── Worker / Review Timeout Checker (P2-07) ───────────────────────────────
    worker_review_timeout_check_interval_seconds: int = Field(
        default=60, alias="MNEME_WORKER_REVIEW_TIMEOUT_CHECK_INTERVAL_SECONDS", ge=10,
        description="Interval in seconds between review timeout checker cycles"
    )

    # ── Worker / Spontaneous Recall (P6-10) ───────────────────────────────────
    worker_spontaneous_recall_enabled: bool = Field(
        default=True, alias="MNEME_WORKER_SPONTANEOUS_RECALL_ENABLED",
        description="Enable the spontaneous recall sweeper that scans for memory contradictions"
    )
    worker_spontaneous_recall_interval_seconds: int = Field(
        default=300, alias="MNEME_WORKER_SPONTANEOUS_RECALL_INTERVAL_SECONDS", ge=30,
        description="Interval in seconds between spontaneous recall scan cycles"
    )
    worker_spontaneous_recall_min_confidence: float = Field(
        default=0.65, alias="MNEME_WORKER_SPONTANEOUS_RECALL_MIN_CONFIDENCE",
        ge=0.0, le=1.0,
        description="Minimum LLM confidence threshold for creating conflict alerts"
    )
    worker_spontaneous_recall_max_pairs: int = Field(
        default=20, alias="MNEME_WORKER_SPONTANEOUS_RECALL_MAX_PAIRS", ge=1, le=200,
        description="Maximum conflict candidate pairs to evaluate per sweep"
    )

    # ── Worker / Memory Sublimation (P6-11) ───────────────────────────────────
    worker_sublimation_enabled: bool = Field(
        default=True, alias="MNEME_WORKER_SUBLIMATION_ENABLED",
        description="Enable the memory sublimation sweeper that abstracts similar events into consensus"
    )
    worker_sublimation_interval_seconds: int = Field(
        default=600, alias="MNEME_WORKER_SUBLIMATION_INTERVAL_SECONDS", ge=60,
        description="Interval in seconds between sublimation scan cycles"
    )
    worker_sublimation_min_cluster_size: int = Field(
        default=5, alias="MNEME_WORKER_SUBLIMATION_MIN_CLUSTER_SIZE", ge=2,
        description="Minimum number of similar memories required to trigger sublimation"
    )
    worker_sublimation_min_similarity: float = Field(
        default=0.80, alias="MNEME_WORKER_SUBLIMATION_MIN_SIMILARITY",
        ge=0.0, le=1.0,
        description="Minimum cosine similarity threshold for clustering memories"
    )
    worker_sublimation_max_clusters: int = Field(
        default=10, alias="MNEME_WORKER_SUBLIMATION_MAX_CLUSTERS", ge=1, le=50,
        description="Maximum number of clusters to evaluate per sweep"
    )

    # ── Gateway (P2-12) ───────────────────────────────────────────────────────
    gateway_call_timeout_seconds: int = Field(
        default=120, alias="MNEME_GATEWAY_CALL_TIMEOUT_SECONDS", ge=5,
        description="Default HTTP timeout for Gateway provider calls in seconds.",
    )
    gateway_max_retries: int = Field(
        default=1, alias="MNEME_GATEWAY_MAX_RETRIES", ge=0, le=5,
        description="Maximum automatic retries for Gateway provider calls.",
    )
    gateway_retry_backoff_base_seconds: float = Field(
        default=1.0, alias="MNEME_GATEWAY_RETRY_BACKOFF_BASE_SECONDS", ge=0.1,
        description="Base backoff seconds between Gateway retries.",
    )

    # ── Vault / Envelope Encryption (P2-08) ───────────────────────────────────
    vault_kek: str = Field(
        default="",
        alias="MNEME_VAULT_KEK",
        description="Base64-encoded 256-bit Key Encryption Key for Vault envelope encryption. "
                    "If empty, a random key is generated at startup (NOT for production).",
    )
    vault_key_version: str = Field(
        default="v1",
        alias="MNEME_VAULT_KEY_VERSION",
        description="Default key version string used for new credentials.",
    )

    # ── Backup / Restore (P2-14 / P2-15) ─────────────────────────────────────
    backup_root: str = Field(
        default="",
        alias="MNEME_BACKUP_ROOT",
        description="Root directory for backup output. "
                    "If empty, defaults to MnemeData/backups relative to CWD.",
    )

    # ── Memory Time Decay (P4-11) ──────────────────────────────────────────
    worker_memory_decay_enabled: bool = Field(
        default=True,
        alias="MNEME_WORKER_MEMORY_DECAY_ENABLED",
        description="Enable periodic memory time-decay sweeper.",
    )
    worker_memory_decay_interval_seconds: int = Field(
        default=300,
        alias="MNEME_WORKER_MEMORY_DECAY_INTERVAL_SECONDS",
        ge=10,
        description="Interval in seconds between decay sweeper cycles.",
    )
    decay_rate_per_day: float = Field(
        default=0.05,
        alias="MNEME_DECAY_RATE_PER_DAY",
        ge=0.0,
        le=1.0,
        description="Daily linear decay rate for decay_score (0.05 = 5% per day).",
    )
    decay_active_threshold: float = Field(
        default=0.7,
        alias="MNEME_DECAY_ACTIVE_THRESHOLD",
        ge=0.0,
        le=1.0,
        description="decay_score >= this → decay_state='active'.",
    )
    decay_silent_threshold: float = Field(
        default=0.3,
        alias="MNEME_DECAY_SILENT_THRESHOLD",
        ge=0.0,
        le=1.0,
        description="decay_score >= this → decay_state='decaying' (below active).",
    )
    decay_archive_threshold: float = Field(
        default=0.1,
        alias="MNEME_DECAY_ARCHIVE_THRESHOLD",
        ge=0.0,
        le=1.0,
        description="decay_score < this → decay_state='archived'.",
    )
    decay_reinforcement_bonus: float = Field(
        default=0.15,
        alias="MNEME_DECAY_REINFORCEMENT_BONUS",
        ge=0.0,
        le=1.0,
        description="Bonus added to decay_score on memory access/reinforcement.",
    )
    decay_max_batch_size: int = Field(
        default=500,
        alias="MNEME_DECAY_MAX_BATCH_SIZE",
        ge=1,
        description="Maximum memories to decay per sweeper batch.",
    )

    # ── Emotion Inference (P4-12) ──────────────────────────────────────────
    worker_emotion_infer_enabled: bool = Field(
        default=True,
        alias="MNEME_WORKER_EMOTION_INFER_ENABLED",
        description="Enable periodic emotion inference sweeper.",
    )
    worker_emotion_infer_interval_seconds: int = Field(
        default=600,
        alias="MNEME_WORKER_EMOTION_INFER_INTERVAL_SECONDS",
        ge=60,
        description="Interval in seconds between emotion inference sweeper cycles.",
    )
    emotion_infer_batch_size: int = Field(
        default=200,
        alias="MNEME_EMOTION_INFER_BATCH_SIZE",
        ge=1,
        description="Maximum memories to infer emotion for per batch.",
    )
    emotion_min_signal_threshold: float = Field(
        default=0.5,
        alias="MNEME_EMOTION_MIN_SIGNAL_THRESHOLD",
        ge=0.0,
        description="Minimum total signal strength to make a non-neutral classification.",
    )
    emotion_strong_signal_threshold: float = Field(
        default=5.0,
        alias="MNEME_EMOTION_STRONG_SIGNAL_THRESHOLD",
        ge=0.0,
        description="Signal strength at which uncertainty approaches 0.",
    )
    emotion_reinfer_uncertainty_threshold: float = Field(
        default=0.6,
        alias="MNEME_EMOTION_REINFER_UNCERTAINTY_THRESHOLD",
        ge=0.0,
        le=1.0,
        description="Re-infer emotion if uncertainty_score is above this threshold.",
    )

    # ── Context Assembly (P8-01) ──────────────────────────────────────────────
    context_assembly_max_tokens: int = Field(
        default=128000,
        alias="MNEME_CONTEXT_ASSEMBLY_MAX_TOKENS",
        ge=512,
        description="Default max tokens for context assembly.",
    )
    context_assembly_output_reserve: int = Field(
        default=4096,
        alias="MNEME_CONTEXT_ASSEMBLY_OUTPUT_RESERVE",
        ge=256,
        description="Tokens reserved for model output.",
    )
    context_assembly_system_overhead: int = Field(
        default=2048,
        alias="MNEME_CONTEXT_ASSEMBLY_SYSTEM_OVERHEAD",
        ge=0,
        description="Tokens reserved for system prompt overhead.",
    )
    context_assembly_always_ratio: float = Field(
        default=0.50,
        alias="MNEME_CONTEXT_ASSEMBLY_ALWAYS_RATIO",
        ge=0.0,
        le=1.0,
        description="Fraction of usable budget for 'always' cards.",
    )
    context_assembly_moderate_ratio: float = Field(
        default=0.30,
        alias="MNEME_CONTEXT_ASSEMBLY_MODERATE_RATIO",
        ge=0.0,
        le=1.0,
        description="Fraction of usable budget for 'moderate' cards.",
    )
    context_assembly_on_demand_ratio: float = Field(
        default=0.20,
        alias="MNEME_CONTEXT_ASSEMBLY_ON_DEMAND_RATIO",
        ge=0.0,
        le=1.0,
        description="Fraction of usable budget for 'on_demand' cards.",
    )

    # ── PPR Graph Search (P7-10) ─────────────────────────────────────────────
    ppr_search_enabled: bool = Field(
        default=True,
        alias="MNEME_PPR_SEARCH_ENABLED",
        description="Enable PPR graph traversal to boost global search recall.",
    )
    ppr_teleport_alpha: float = Field(
        default=0.85,
        alias="MNEME_PPR_TELEPORT_ALPHA",
        ge=0.0, le=1.0,
        description="PPR teleport probability (higher = more graph exploration).",
    )
    ppr_max_seeds: int = Field(
        default=8,
        alias="MNEME_PPR_MAX_SEEDS",
        ge=1, le=50,
        description="Maximum seed nodes for PPR traversal (from FTS results).",
    )
    ppr_top_k: int = Field(
        default=12,
        alias="MNEME_PPR_TOP_K",
        ge=1, le=100,
        description="Maximum PPR-discovered nodes to return.",
    )

    # ── Temporal Cluster Search (P7-11) ──────────────────────────────────────
    temporal_cluster_enabled: bool = Field(
        default=True,
        alias="MNEME_TEMPORAL_CLUSTER_ENABLED",
        description="Enable temporal shape clustering for fuzzy time queries.",
    )
    temporal_cluster_top_k: int = Field(
        default=8,
        alias="MNEME_TEMPORAL_CLUSTER_TOP_K",
        ge=1, le=50,
        description="Maximum temporally-clustered memories to return.",
    )

    # ── Feature Flags ─────────────────────────────────────────────────────────
    feature_legacy_redirects: bool = Field(
        default=True,
        alias="MNEME_FEATURE_LEGACY_REDIRECTS",
        description="Enable legacy URL redirects (30-day grace period). "
                    "Set to false to immediately return 404 for old routes.",
    )

    # ── Storage (P3-01) ─────────────────────────────────────────────────────
    storage_root: str = Field(
        default="mneme_data",
        alias="MNEME_STORAGE_ROOT",
        description="Root directory for file storage (staging + assets). "
                    "Defaults to 'mneme_data' relative to CWD.",
    )
    staging_subdir: str = Field(
        default="staging",
        alias="MNEME_STAGING_SUBDIR",
        description="Subdirectory name under storage_root for staging files.",
    )
    max_upload_size_bytes: int = Field(
        default=104_857_600,  # 100 MB
        alias="MNEME_MAX_UPLOAD_SIZE_BYTES",
        ge=1,
        description="Maximum allowed file upload size in bytes (default 100 MB).",
    )
    allowed_mime_types: str = Field(
        default=(
            "text/plain,text/csv,text/markdown,text/html,text/x-python,"
            "application/json,application/xml,application/pdf,"
            "application/msword,"
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
            "application/vnd.ms-excel,"
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
            "application/vnd.ms-powerpoint,"
            "application/vnd.openxmlformats-officedocument.presentationml.presentation,"
            "image/png,image/jpeg,image/gif,image/webp,image/svg+xml,"
            "audio/mpeg,audio/wav,audio/ogg,audio/flac,"
            "video/mp4,video/webm,"
            "application/zip,application/gzip,application/x-tar"
        ),
        alias="MNEME_ALLOWED_MIME_TYPES",
        description="Comma-separated list of allowed MIME types for file upload.",
    )
    storage_backend: str = Field(
        default="mneme_data",
        alias="MNEME_STORAGE_BACKEND",
        description="Storage backend identifier (currently only 'mneme_data' supported).",
    )

    @property
    def allowed_mime_types_list(self) -> list[str]:
        return [m.strip() for m in self.allowed_mime_types.split(",") if m.strip()]

    @property
    def staging_path(self) -> str:
        """Absolute path to the staging directory."""
        return f"{self.storage_root.rstrip('/')}/{self.staging_subdir.strip('/')}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
