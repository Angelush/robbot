"""
faq_builder.py — Automatic FAQ builder for RobBot.

Analyzes interaction logs periodically, clusters similar queries, generates
cached FAQ entries, scores them by quality signals, and decays stale entries.

Integration pattern (in bot.py):
    faq_builder = FAQBuilder(learning_db)
    ...
    # after logging each interaction:
    if faq_builder.should_rebuild():
        asyncio.get_event_loop().run_in_executor(None, faq_builder.rebuild)
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from learning import LearningDB

log = logging.getLogger("robbot.faq_builder")

# Sentinel query_pattern value used to persist the rebuild counter in the faq table.
_REBUILD_SENTINEL = "__last_rebuild_count__"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jaccard(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two sets. Returns 0.0 for empty inputs."""
    if not set_a or not set_b:
        return 0.0
    union = set_a | set_b
    return len(set_a & set_b) / len(union)


class FAQBuilder:
    def __init__(self, learning_db: LearningDB):
        """Takes an existing LearningDB instance."""
        self._db = learning_db
        self._conn: sqlite3.Connection = learning_db._conn

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def rebuild(self) -> None:
        """Main entry point. Runs the full rebuild pipeline."""
        log.info("FAQBuilder.rebuild() starting")
        try:
            self.cluster_queries()
            self.generate_faq_entries()
            self.score_entries()
            self.decay_stale()
            self._save_rebuild_count()
            log.info("FAQBuilder.rebuild() complete")
        except Exception:
            log.exception("FAQBuilder.rebuild() failed")

    # ------------------------------------------------------------------
    # Step 1: cluster_queries
    # ------------------------------------------------------------------

    def cluster_queries(self, min_count: int = 5) -> None:
        """Group normalized queries by shared keywords using Jaccard similarity."""
        # Fetch all normalized queries and their occurrence counts.
        cur = self._conn.execute(
            """
            SELECT query_normalized, COUNT(*) AS cnt
            FROM interactions
            WHERE query_normalized != ''
            GROUP BY query_normalized
            ORDER BY cnt DESC
            """
        )
        query_rows = cur.fetchall()

        if not query_rows:
            log.debug("cluster_queries: no interactions found, skipping")
            return

        # Load existing clusters as list of (id, keyword_set, query_count).
        cur = self._conn.execute(
            "SELECT id, keywords, query_count FROM query_clusters"
        )
        existing_clusters: list[dict] = []
        for row in cur.fetchall():
            try:
                kw = set(json.loads(row["keywords"]))
            except (json.JSONDecodeError, TypeError):
                kw = set()
            existing_clusters.append({
                "id": row["id"],
                "keywords": kw,
                "query_count": row["query_count"],
            })

        # For each qualifying normalized query, assign or create a cluster.
        # We work in-memory and flush at the end to avoid repeated writes.
        new_clusters: list[dict] = []  # clusters created this run (no DB id yet)

        # Accumulate counts per cluster_id (existing) or new_cluster index.
        existing_additions: dict[int, int] = {}  # cluster_id -> added count
        new_additions: list[int] = []            # parallel to new_clusters

        for row in query_rows:
            normalized: str = row["query_normalized"]
            count: int = row["cnt"]
            if count < min_count:
                continue

            words = set(normalized.split()) if normalized else set()
            if not words:
                continue

            # Try to find a matching existing cluster.
            best_existing_id: Optional[int] = None
            best_existing_score = 0.0
            for cluster in existing_clusters:
                score = _jaccard(words, cluster["keywords"])
                if score > best_existing_score:
                    best_existing_score = score
                    best_existing_id = cluster["id"]

            if best_existing_score > 0.6 and best_existing_id is not None:
                existing_additions[best_existing_id] = (
                    existing_additions.get(best_existing_id, 0) + count
                )
                continue

            # Try to merge with a cluster created this run.
            best_new_idx: Optional[int] = None
            best_new_score = 0.0
            for idx, nc in enumerate(new_clusters):
                score = _jaccard(words, nc["keywords"])
                if score > best_new_score:
                    best_new_score = score
                    best_new_idx = idx

            if best_new_score > 0.6 and best_new_idx is not None:
                # Expand the new cluster's keywords to be the union.
                new_clusters[best_new_idx]["keywords"] |= words
                new_additions[best_new_idx] += count
            else:
                # Brand-new cluster.
                new_clusters.append({"keywords": words})
                new_additions.append(count)

        # Flush updates to existing clusters.
        for cluster_id, added in existing_additions.items():
            self._conn.execute(
                "UPDATE query_clusters SET query_count = query_count + ? WHERE id = ?",
                (added, cluster_id),
            )

        # Insert new clusters.
        for nc, count in zip(new_clusters, new_additions):
            self._conn.execute(
                "INSERT INTO query_clusters (keywords, query_count, has_faq) VALUES (?, ?, FALSE)",
                (json.dumps(sorted(nc["keywords"])), count),
            )

        self._conn.commit()
        log.debug(
            "cluster_queries: updated %d existing, inserted %d new clusters",
            len(existing_additions),
            len(new_clusters),
        )

    # ------------------------------------------------------------------
    # Step 2: generate_faq_entries
    # ------------------------------------------------------------------

    def generate_faq_entries(self) -> None:
        """Create FAQ entries for clusters that qualify but have none yet."""
        cur = self._conn.execute(
            "SELECT id, keywords FROM query_clusters WHERE has_faq = FALSE AND query_count >= 5"
        )
        clusters = cur.fetchall()

        if not clusters:
            log.debug("generate_faq_entries: no qualifying clusters")
            return

        created = 0
        for cluster in clusters:
            cluster_id: int = cluster["id"]
            try:
                keywords: list[str] = json.loads(cluster["keywords"])
            except (json.JSONDecodeError, TypeError):
                log.warning("Cluster %d has malformed keywords, skipping", cluster_id)
                continue

            kw_set = set(keywords)
            if not kw_set:
                continue

            # Find all interactions that belong to this cluster by normalized query overlap.
            # An interaction matches if Jaccard(kw_set, query_words) > 0.6.
            cur2 = self._conn.execute(
                "SELECT id, query_raw, query_normalized, videos_used, response_length, followed_up "
                "FROM interactions WHERE query_normalized != ''"
            )
            matching_interactions = []
            raw_query_counts: dict[str, int] = {}

            for irow in cur2.fetchall():
                qwords = set(irow["query_normalized"].split()) if irow["query_normalized"] else set()
                if _jaccard(kw_set, qwords) > 0.6:
                    matching_interactions.append(irow)
                    raw = irow["query_raw"].strip().lower()
                    raw_query_counts[raw] = raw_query_counts.get(raw, 0) + 1

            if not matching_interactions:
                continue

            # Find the best interaction: longest response that was NOT followed up.
            best_interaction = None
            for irow in matching_interactions:
                if irow["followed_up"]:
                    continue
                if best_interaction is None or (
                    (irow["response_length"] or 0) > (best_interaction["response_length"] or 0)
                ):
                    best_interaction = irow

            if best_interaction is None:
                # All matching interactions were followed up — skip this cluster.
                log.debug("Cluster %d: all interactions followed up, skipping", cluster_id)
                continue

            # Aggregate the most referenced videos across all matching interactions.
            video_counts: dict[str, int] = {}
            for irow in matching_interactions:
                if not irow["videos_used"]:
                    continue
                try:
                    vids = json.loads(irow["videos_used"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(vids, list):
                    for v in vids:
                        if isinstance(v, str):
                            video_counts[v] = video_counts.get(v, 0) + 1

            # Sort videos by frequency; take top 5.
            top_videos: list[str] = sorted(video_counts, key=lambda v: video_counts[v], reverse=True)[:5]

            # Build cached_response as a quick-reference listing.
            if top_videos:
                video_lines = "\n".join(f"• {v}" for v in top_videos)
                cached_response = (
                    "This topic comes up frequently! Here are Rob's most relevant videos:\n\n"
                    + video_lines
                    + "\n\nUse `/ask [your question]` for a detailed AI-powered answer!"
                )
            else:
                cached_response = (
                    "This topic comes up frequently! "
                    "Use `/ask [your question]` for a detailed AI-powered answer!"
                )

            query_pattern = " ".join(sorted(kw_set))
            now = _now_iso()

            # Upsert: if a matching pattern already exists don't duplicate it.
            try:
                self._conn.execute(
                    """
                    INSERT INTO faq (query_pattern, cached_response, videos, hit_count,
                                     quality_score, created_at, last_hit, last_updated)
                    VALUES (?, ?, ?, 0, 0.5, ?, NULL, ?)
                    """,
                    (
                        query_pattern,
                        cached_response,
                        json.dumps(top_videos) if top_videos else None,
                        now,
                        now,
                    ),
                )
                self._conn.execute(
                    "UPDATE query_clusters SET has_faq = TRUE WHERE id = ?",
                    (cluster_id,),
                )
                created += 1
            except sqlite3.IntegrityError:
                # query_pattern already exists — update the response and videos.
                self._conn.execute(
                    """
                    UPDATE faq SET cached_response = ?, videos = ?, last_updated = ?
                    WHERE query_pattern = ?
                    """,
                    (cached_response, json.dumps(top_videos) if top_videos else None, now, query_pattern),
                )
                self._conn.execute(
                    "UPDATE query_clusters SET has_faq = TRUE WHERE id = ?",
                    (cluster_id,),
                )

        self._conn.commit()
        log.info("generate_faq_entries: created %d new FAQ entries", created)

    # ------------------------------------------------------------------
    # Step 3: score_entries
    # ------------------------------------------------------------------

    def score_entries(self) -> None:
        """Recompute quality_score for every FAQ entry (excluding sentinel row)."""
        cur = self._conn.execute(
            "SELECT id, query_pattern, hit_count, last_hit FROM faq WHERE query_pattern != ?",
            (_REBUILD_SENTINEL,),
        )
        faq_rows = cur.fetchall()

        if not faq_rows:
            log.debug("score_entries: no FAQ entries to score")
            return

        # Compute normalised hit score: 0..1 based on max hits in the table.
        max_hits: int = max((row["hit_count"] or 0) for row in faq_rows) or 1
        now_utc = datetime.now(timezone.utc)

        for row in faq_rows:
            faq_id: int = row["id"]
            pattern: str = row["query_pattern"]
            hit_count: int = row["hit_count"] or 0
            hit_score: float = hit_count / max_hits

            # Follow-up rate: fraction of matching interactions that were followed up.
            kw_set = set(pattern.split()) if pattern else set()
            follow_up_rate = self._follow_up_rate_for_cluster(kw_set)

            # Recency score: 1.0 if hit in the last 7 days, decaying linearly to 0 at 90 days.
            recency_score: float = 0.0
            if row["last_hit"]:
                try:
                    last_hit_dt = datetime.fromisoformat(row["last_hit"])
                    if last_hit_dt.tzinfo is None:
                        last_hit_dt = last_hit_dt.replace(tzinfo=timezone.utc)
                    days_since = (now_utc - last_hit_dt).total_seconds() / 86400.0
                    recency_score = max(0.0, 1.0 - (days_since / 90.0))
                except (ValueError, TypeError):
                    recency_score = 0.0

            quality = (
                0.4 * hit_score
                + 0.4 * (1.0 - follow_up_rate)
                + 0.2 * recency_score
            )
            quality = max(0.0, min(1.0, quality))

            self._conn.execute(
                "UPDATE faq SET quality_score = ?, last_updated = ? WHERE id = ?",
                (round(quality, 4), _now_iso(), faq_id),
            )

        self._conn.commit()
        log.debug("score_entries: scored %d FAQ entries", len(faq_rows))

    def _follow_up_rate_for_cluster(self, kw_set: set) -> float:
        """Return the fraction of matching interactions that were followed up."""
        if not kw_set:
            return 0.0
        cur = self._conn.execute(
            "SELECT query_normalized, followed_up FROM interactions WHERE query_normalized != ''"
        )
        total = 0
        followed = 0
        for row in cur.fetchall():
            qwords = set(row["query_normalized"].split()) if row["query_normalized"] else set()
            if _jaccard(kw_set, qwords) > 0.6:
                total += 1
                if row["followed_up"]:
                    followed += 1
        return followed / total if total > 0 else 0.0

    # ------------------------------------------------------------------
    # Step 4: decay_stale
    # ------------------------------------------------------------------

    def decay_stale(self, days: int = 30) -> None:
        """Remove rarely-used FAQ entries that haven't been hit recently."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Find stale entries: last_hit older than cutoff (or never hit) AND hit_count < 3.
        cur = self._conn.execute(
            """
            SELECT id, query_pattern FROM faq
            WHERE query_pattern != ?
              AND hit_count < 3
              AND (last_hit IS NULL OR last_hit < ?)
            """,
            (_REBUILD_SENTINEL, cutoff),
        )
        stale = cur.fetchall()

        if not stale:
            log.debug("decay_stale: nothing to remove")
            return

        stale_ids = [row["id"] for row in stale]
        stale_patterns = [row["query_pattern"] for row in stale]

        # Delete the stale FAQ rows.
        placeholders = ",".join("?" * len(stale_ids))
        self._conn.execute(
            f"DELETE FROM faq WHERE id IN ({placeholders})",
            stale_ids,
        )

        # Reset has_faq on clusters whose pattern matches a deleted entry.
        # We match by keyword set overlap against the removed patterns.
        cur2 = self._conn.execute("SELECT id, keywords FROM query_clusters WHERE has_faq = TRUE")
        for cluster in cur2.fetchall():
            try:
                ckw = set(json.loads(cluster["keywords"]))
            except (json.JSONDecodeError, TypeError):
                continue
            for pattern in stale_patterns:
                pkw = set(pattern.split()) if pattern else set()
                if _jaccard(ckw, pkw) > 0.6:
                    self._conn.execute(
                        "UPDATE query_clusters SET has_faq = FALSE WHERE id = ?",
                        (cluster["id"],),
                    )
                    break

        self._conn.commit()
        log.info("decay_stale: removed %d stale FAQ entries", len(stale_ids))

    # ------------------------------------------------------------------
    # Rebuild throttle
    # ------------------------------------------------------------------

    def should_rebuild(self, interval: int = 100) -> bool:
        """Return True if at least `interval` new interactions have been logged since the last rebuild."""
        current_count = self._db.get_interaction_count()
        last_rebuild_count = self._load_rebuild_count()
        return (current_count - last_rebuild_count) >= interval

    def _load_rebuild_count(self) -> int:
        """Read the persisted interaction count from the sentinel FAQ row."""
        cur = self._conn.execute(
            "SELECT cached_response FROM faq WHERE query_pattern = ?",
            (_REBUILD_SENTINEL,),
        )
        row = cur.fetchone()
        if row is None:
            return 0
        try:
            return int(row["cached_response"])
        except (ValueError, TypeError):
            return 0

    def _save_rebuild_count(self) -> None:
        """Persist the current interaction count into the sentinel FAQ row."""
        current_count = self._db.get_interaction_count()
        now = _now_iso()
        self._conn.execute(
            """
            INSERT INTO faq (query_pattern, cached_response, videos, hit_count,
                             quality_score, created_at, last_hit, last_updated)
            VALUES (?, ?, NULL, 0, 0.0, ?, NULL, ?)
            ON CONFLICT(query_pattern) DO UPDATE SET
                cached_response = excluded.cached_response,
                last_updated    = excluded.last_updated
            """,
            (_REBUILD_SENTINEL, str(current_count), now, now),
        )
        self._conn.commit()
        log.debug("_save_rebuild_count: stored count=%d", current_count)
