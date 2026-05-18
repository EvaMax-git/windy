"""Temporal shape clustering — fuzzy temporal expression → concept cloud → weighted recall.

Parses natural-language temporal expressions ("那个夏天", "last week", "yesterday",
"上个月", etc.) and maps them to concrete time ranges. Memories falling in those
ranges are then clustered into concept clouds by keyword/embedding proximity and
returned with weighted ranks.

Temporal Expression Patterns
----------------------------
- Absolute: "2024-03-15", "March 2024", "2024年3月"
- Relative: "yesterday", "last week", "上周", "3天前" (3 days ago)
- Fuzzy: "那个夏天" (that summer), "那年冬天" (that winter), "recently"
- Seasonal: "summer 2023", "去年夏天" (last summer)

Concept Cloud
-------------
Memories in the temporal window are clustered by:
1. Keyword co-occurrence (Jaccard / TF-IDF style)
2. Embedding proximity (cosine similarity) when available
3. Each cluster forms a "concept" → returned with a label and aggregated weight

Integration
-----------
Called from ``global_search`` when the query contains temporal expressions.
Results are merged with direct search results, weighted by temporal relevance.

Usage
-----
.. code-block:: python

    from mneme.memory.temporal_cluster import temporal_cluster_search

    result = temporal_cluster_search(
        db,
        query="那个夏天发生的事情",
        project_id=pid,
        top_k=8,
    )
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

MAX_TEMPORAL_RESULTS = 12
DEFAULT_TEMPORAL_WEIGHT = 0.55
RECENCY_DECAY_DAYS = 90.0  # Memories older than this get lower weight


# ═══════════════════════════════════════════════════════════════════════════
# Temporal Expression Parser
# ═══════════════════════════════════════════════════════════════════════════

# Chinese seasons
_SEASON_ZH = {
    "春": (3, 5),   # spring: Mar-May
    "夏": (6, 8),   # summer: Jun-Aug
    "秋": (9, 11),  # autumn: Sep-Nov
    "冬": (12, 2),  # winter: Dec-Feb (wraps year)
}

_ENGLISH_SEASONS = {
    "spring": (3, 5),
    "summer": (6, 8),
    "autumn": (9, 11),
    "fall": (9, 11),
    "winter": (12, 2),
}

# Relative time patterns
_RELATIVE_PATTERNS = [
    # English
    (re.compile(r"today", re.I), lambda now: (now.replace(hour=0, minute=0, second=0), now)),
    (re.compile(r"yesterday", re.I), lambda now: (now - timedelta(days=1), now - timedelta(days=1))),
    (re.compile(r"last\s+night", re.I), lambda now: (now - timedelta(days=1), now)),
    (re.compile(r"last\s+week", re.I),
     lambda now: (now - timedelta(days=now.weekday() + 7), now - timedelta(days=now.weekday()))),
    (re.compile(r"last\s+month", re.I),
     lambda now: (_month_start(now, -1), _month_end(now, -1))),
    (re.compile(r"last\s+year", re.I),
     lambda now: (now.replace(year=now.year - 1, month=1, day=1),
                  now.replace(year=now.year - 1, month=12, day=31))),
    (re.compile(r"this\s+week", re.I),
     lambda now: (now - timedelta(days=now.weekday()), now)),
    (re.compile(r"this\s+month", re.I),
     lambda now: (now.replace(day=1), now)),
    (re.compile(r"this\s+year", re.I),
     lambda now: (now.replace(month=1, day=1), now)),
    (re.compile(r"recently", re.I), lambda now: (now - timedelta(days=7), now)),
    (re.compile(r"(\d+)\s*days?\s*ago", re.I),
     lambda now, m: (now - timedelta(days=int(m.group(1))), now)),
    (re.compile(r"(\d+)\s*weeks?\s*ago", re.I),
     lambda now, m: (now - timedelta(weeks=int(m.group(1))), now)),
    (re.compile(r"(\d+)\s*months?\s*ago", re.I),
     lambda now, m: (_month_start(now, -int(m.group(1))), now)),

    # Chinese
    (re.compile(r"今天"), lambda now: (now.replace(hour=0, minute=0, second=0), now)),
    (re.compile(r"昨天"), lambda now: (now - timedelta(days=1), now - timedelta(days=1))),
    (re.compile(r"前天"), lambda now: (now - timedelta(days=2), now - timedelta(days=2))),
    (re.compile(r"上周"), lambda now: (now - timedelta(days=now.weekday() + 7),
                                        now - timedelta(days=now.weekday()))),
    (re.compile(r"本周"), lambda now: (now - timedelta(days=now.weekday()), now)),
    (re.compile(r"上个月"), lambda now: (_month_start(now, -1), _month_end(now, -1))),
    (re.compile(r"这个月"), lambda now: (now.replace(day=1), now)),
    (re.compile(r"去年"), lambda now: (now.replace(year=now.year - 1, month=1, day=1),
                                        now.replace(year=now.year - 1, month=12, day=31))),
    (re.compile(r"今年"), lambda now: (now.replace(month=1, day=1), now)),
    (re.compile(r"最近"), lambda now: (now - timedelta(days=7), now)),
    (re.compile(r"(\d+)\s*天前"), lambda now, m: (now - timedelta(days=int(m.group(1))), now)),
    (re.compile(r"(\d+)\s*周前"), lambda now, m: (now - timedelta(weeks=int(m.group(1))), now)),
    (re.compile(r"(\d+)\s*个月前"), lambda now, m: (_month_start(now, -int(m.group(1))), now)),
    (re.compile(r"(\d+)\s*年前"), lambda now, m: (
        now.replace(year=now.year - int(m.group(1))), now)),
]


def _month_start(now: datetime, offset_months: int) -> datetime:
    """Get start of month with given offset."""
    year = now.year
    month = now.month + offset_months
    while month < 1:
        year -= 1
        month += 12
    while month > 12:
        year += 1
        month -= 12
    return now.replace(year=year, month=month, day=1, hour=0, minute=0, second=0)


def _month_end(now: datetime, offset_months: int) -> datetime:
    """Get end of month with given offset."""
    start = _month_start(now, offset_months)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1, day=1)
    else:
        end = start.replace(month=start.month + 1, day=1)
    return end - timedelta(seconds=1)


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class TemporalRange:
    """Parsed temporal range from a natural-language query."""

    start: datetime
    end: datetime
    expression: str  # The matched expression text
    confidence: float = 0.8  # How confident we are in this parse


def parse_temporal_expressions(
    query: str,
    now: datetime | None = None,
) -> list[TemporalRange]:
    """Extract temporal ranges from a natural-language query.

    Parameters
    ----------
    query : str
        Natural language query (supports Chinese and English).
    now : datetime | None
        Reference time. Defaults to UTC now.

    Returns
    -------
    list[TemporalRange]
        Detected temporal ranges, sorted by confidence descending.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    else:
        now = _ensure_utc(now)

    results: list[TemporalRange] = []

    # ── Fuzzy seasonal: "那个夏天", "那年冬天", "去年夏天", "last summer" ──
    fuzzy_season_zh = re.compile(
        r"(那个|那年|去年|今年|前年|某[个年])\s*"
        r"(春[天季]?|夏[天季]?|秋[天季]?|冬[天季]?)"
    )
    for m in fuzzy_season_zh.finditer(query):
        prefix = m.group(1)
        season_word = m.group(2)
        season_char = season_word[0]  # 春/夏/秋/冬

        if season_char in _SEASON_ZH:
            sm, em = _SEASON_ZH[season_char]

            # Determine year from prefix
            if prefix in ("去年",):
                year = now.year - 1
            elif prefix in ("今年",):
                year = now.year
            elif prefix in ("前年",):
                year = now.year - 2
            else:
                # "那个夏天", "那年冬天" — fuzzy, use current year with lower confidence
                year = now.year
                # For winter (12,2), if we're in Jan-Feb and season is winter, use prev year start
                if season_char == "冬" and now.month <= 2:
                    year = now.year - 1
                # For all fuzzy ones, also try previous year
                results.append(TemporalRange(
                    start=_ensure_utc(datetime(year - 1, sm, 1)),
                    end=_ensure_utc(datetime(year - 1, em, 28, 23, 59, 59)),
                    expression=m.group(0),
                    confidence=0.55,  # lower confidence for fuzzy "那个"
                ))

            if season_char == "冬":
                # Winter spans Dec-Feb
                if em == 2:
                    end_year = year + 1
                else:
                    end_year = year
                start_dt = datetime(year, sm, 1)
                end_dt = datetime(end_year, em, 28, 23, 59, 59)
            else:
                start_dt = datetime(year, sm, 1)
                end_dt = datetime(year, em, 28, 23, 59, 59)

            results.append(TemporalRange(
                start=_ensure_utc(start_dt),
                end=_ensure_utc(end_dt),
                expression=m.group(0),
                confidence=0.7 if prefix in ("去年", "今年") else 0.6,
            ))

    # English fuzzy seasons
    fuzzy_en_season = re.compile(
        r"(that|last|this|the)\s+(spring|summer|autumn|fall|winter)", re.I
    )
    for m in fuzzy_en_season.finditer(query):
        prefix = m.group(1).lower()
        season = m.group(2).lower()
        if season not in _ENGLISH_SEASONS:
            continue
        sm, em = _ENGLISH_SEASONS[season]

        if prefix in ("last",):
            year = now.year - 1
        elif prefix in ("this", "the"):
            year = now.year
        else:
            year = now.year

        if season == "winter" and em == 2:
            end_year = year + 1
        else:
            end_year = year

        start_dt = datetime(year, sm, 1)
        end_dt = datetime(end_year, em, 28, 23, 59, 59)
        results.append(TemporalRange(
            start=_ensure_utc(start_dt),
            end=_ensure_utc(end_dt),
            expression=m.group(0),
            confidence=0.75,
        ))

    # ── Exact dates ──
    exact_ymd = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})[日号]?")
    for m in exact_ymd.finditer(query):
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = datetime(y, mo, d)
            results.append(TemporalRange(
                start=_ensure_utc(dt.replace(hour=0, minute=0, second=0)),
                end=_ensure_utc(dt.replace(hour=23, minute=59, second=59)),
                expression=m.group(0),
                confidence=0.95,
            ))
        except ValueError:
            pass

    # Year-month patterns
    exact_ym = re.compile(r"(\d{4})[-/年](\d{1,2})[月]?")
    for m in exact_ym.finditer(query):
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            start_dt = datetime(y, mo, 1)
            end_dt = _month_end(start_dt, 0)
            results.append(TemporalRange(
                start=_ensure_utc(start_dt),
                end=_ensure_utc(end_dt),
                expression=m.group(0),
                confidence=0.9,
            ))
        except ValueError:
            pass

    # ── Relative time expressions ──
    for pattern, time_func in _RELATIVE_PATTERNS:
        m = pattern.search(query)
        if m:
            try:
                if pattern.groups > 0 and "(" in pattern.pattern:
                    # Has capture groups for numbers
                    start, end = time_func(now, m)
                else:
                    start, end = time_func(now)
                results.append(TemporalRange(
                    start=_ensure_utc(start),
                    end=_ensure_utc(end),
                    expression=m.group(0),
                    confidence=0.85,
                ))
            except Exception:
                pass

    # Sort by confidence descending
    results.sort(key=lambda r: r.confidence, reverse=True)
    return results


# ═══════════════════════════════════════════════════════════════════════════
# Temporal Cluster Search
# ═══════════════════════════════════════════════════════════════════════════

_MEMORIES_IN_RANGE = text("""
    SELECT
        m.memory_id,
        m.title,
        m.canonical_key,
        m.memory_text,
        m.sensitivity_level,
        m.status,
        m.project_id,
        m.node_type,
        m.activated_at,
        m.created_at,
        m.updated_at,
        m.current_version
    FROM memories m
    WHERE m.status IN ('active', 'draft')
      AND (:project_id IS NULL OR m.project_id = :project_id)
      AND (
          (m.activated_at IS NOT NULL AND m.activated_at >= :t_start AND m.activated_at <= :t_end)
          OR
          (m.created_at >= :t_start AND m.created_at <= :t_end)
      )
    ORDER BY m.activated_at DESC NULLS LAST, m.created_at DESC
    LIMIT :limit
""")


def _extract_concept_keywords(text: str, top_n: int = 5) -> list[str]:
    """Extract meaningful keywords from memory text for concept labeling."""
    import re as _re
    # Split on word boundaries for Chinese and spaces for English
    words = _re.findall(r'[一-鿿]+|[a-zA-Z]+', text.lower())
    stopwords = {
        "this", "that", "with", "from", "have", "been", "were", "they",
        "will", "would", "could", "should", "about", "there", "their",
        "which", "when", "what", "where", "also", "than", "then", "just",
        "some", "only", "over", "into", "such", "more", "very", "much",
        "other", "after", "still",
        "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都",
        "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你",
        "会", "着", "没有", "看", "好", "自己", "这",
    }
    candidates = [w for w in words if len(w) >= 2 and w not in stopwords]
    unique = list(dict.fromkeys(candidates))
    return sorted(unique, key=len, reverse=True)[:top_n]


def _cluster_memories(
    memories: list[dict[str, Any]],
    query: str,
    max_clusters: int = 6,
) -> list[dict[str, Any]]:
    """Cluster memories into concept groups by keyword overlap.

    Parameters
    ----------
    memories : list[dict]
        Memory rows from temporal range search.
    query : str
        Original query for context-aware weighting.
    max_clusters : int
        Maximum number of concept clusters.

    Returns
    -------
    list[dict]
        Each dict = {"concept_label": str, "memories": [...], "weight": float}
    """
    if not memories:
        return []

    # Extract keywords from each memory
    mem_keywords: dict[int, set[str]] = {}
    for i, mem in enumerate(memories):
        text = (mem.get("title") or "") + " " + (mem.get("memory_text") or "")
        keywords = set(_extract_concept_keywords(text, top_n=8))
        mem_keywords[i] = keywords

    # Simple agglomerative clustering by Jaccard overlap
    clusters: list[list[int]] = []  # each cluster is list of memory indices
    assigned: set[int] = set()

    for i in range(len(memories)):
        if i in assigned:
            continue
        cluster = [i]
        assigned.add(i)
        for j in range(i + 1, len(memories)):
            if j in assigned:
                continue
            overlap = mem_keywords[i] & mem_keywords[j]
            union = mem_keywords[i] | mem_keywords[j]
            jaccard = len(overlap) / len(union) if union else 0.0
            if jaccard >= 0.2:
                cluster.append(j)
                assigned.add(j)
        clusters.append(cluster)
        if len(clusters) >= max_clusters:
            break

    # Build concept cloud results
    results: list[dict[str, Any]] = []
    for cluster in clusters:
        cluster_mems = [memories[i] for i in cluster]
        # Determine concept label: most common keyword across cluster
        all_kw: list[str] = []
        for i in cluster:
            all_kw.extend(mem_keywords[i])
        kw_counts: dict[str, int] = defaultdict(int)
        for kw in all_kw:
            kw_counts[kw] += 1
        sorted_kw = sorted(kw_counts.items(), key=lambda x: x[1], reverse=True)
        concept_label = " / ".join([kw for kw, _ in sorted_kw[:3]]) if sorted_kw else "memory"

        # Weight: temporal recency × cluster size
        latest_ts = max(
            (mem.get("activated_at") or mem.get("created_at") or datetime.min)
            for mem in cluster_mems
        )
        if isinstance(latest_ts, str):
            latest_ts = datetime.fromisoformat(latest_ts.replace("Z", "+00:00"))
        days_ago = max(0.0, (datetime.now(timezone.utc) - _ensure_utc(latest_ts)).total_seconds() / 86400.0)
        recency_weight = max(0.3, 1.0 - (days_ago / RECENCY_DECAY_DAYS))
        cluster_weight = min(1.0, len(cluster) / 5.0) * recency_weight

        results.append({
            "concept_label": concept_label,
            "memories": cluster_mems,
            "weight": round(cluster_weight, 4),
            "size": len(cluster),
        })

    results.sort(key=lambda c: c["weight"], reverse=True)
    return results


@dataclass
class TemporalClusterResult:
    """Aggregated result from temporal clustering search."""

    temporal_expressions: list[TemporalRange] = field(default_factory=list)
    memories_found: int = 0
    clusters: list[dict[str, Any]] = field(default_factory=list)
    all_memory_details: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0
    has_temporal_match: bool = False


def temporal_cluster_search(
    db: Session,
    *,
    query: str,
    project_id: UUID | None = None,
    top_k: int = MAX_TEMPORAL_RESULTS,
    now: datetime | None = None,
) -> TemporalClusterResult:
    """Detect temporal expressions in query, cluster memories in the time range.

    Parameters
    ----------
    db : Session
        Active SQLAlchemy session.
    query : str
        User query (may contain temporal expressions).
    project_id : UUID | None
        Optional project scope.
    top_k : int
        Maximum memories to return.
    now : datetime | None
        Reference time for relative expressions.

    Returns
    -------
    TemporalClusterResult
    """
    t0 = time.monotonic()
    result = TemporalClusterResult()

    # 1. Parse temporal expressions
    temporal_ranges = parse_temporal_expressions(query, now=now)
    result.temporal_expressions = temporal_ranges

    if not temporal_ranges:
        return result

    result.has_temporal_match = True

    # 2. Query memories in the union of all temporal ranges
    all_memories: dict[UUID, dict] = {}
    for tr in temporal_ranges[:2]:  # Use at most 2 ranges to avoid over-fetching
        rows = db.execute(
            _MEMORIES_IN_RANGE,
            {
                "project_id": project_id,
                "t_start": tr.start,
                "t_end": tr.end,
                "limit": top_k * 2,
            },
        ).mappings().all()

        for row in rows:
            item = dict(row)
            mid = item["memory_id"]
            if isinstance(mid, str):
                mid = UUID(mid)
            if mid not in all_memories:
                # Compute temporal relevance: how close to the range midpoint
                ts = item.get("activated_at") or item.get("created_at")
                if ts:
                    if isinstance(ts, str):
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    ts = _ensure_utc(ts)
                    range_mid = tr.start + (tr.end - tr.start) / 2
                    distance_secs = abs((ts - range_mid).total_seconds())
                    # Normalize: closer = higher relevance
                    max_dist = max(1.0, (tr.end - tr.start).total_seconds() / 2)
                    temporal_score = max(0.3, 1.0 - (distance_secs / max_dist))
                else:
                    temporal_score = 0.4
                item["temporal_score"] = round(temporal_score * tr.confidence, 4)
                item["temporal_expression"] = tr.expression
                all_memories[mid] = item

    # Sort by temporal score descending
    mem_list = sorted(all_memories.values(), key=lambda m: m.get("temporal_score", 0.0), reverse=True)
    mem_list = mem_list[:top_k]
    result.memories_found = len(mem_list)
    result.all_memory_details = mem_list

    # 3. Cluster into concept clouds
    result.clusters = _cluster_memories(mem_list, query)
    result.elapsed_ms = (time.monotonic() - t0) * 1000.0

    logger.info(
        "temporal_cluster: query has %d temporal ranges → %d memories → %d clusters (%.1fms)",
        len(temporal_ranges), result.memories_found, len(result.clusters),
        result.elapsed_ms,
    )

    return result
