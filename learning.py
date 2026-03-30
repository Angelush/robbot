"""
learning.py — SQLite-based interaction logging and auto-learning FAQ module for RobBot.

Tracks every bot interaction and builds data for an auto-learning FAQ system.
Quality is inferred from usage patterns (follow-ups, hit counts) rather than ratings.
"""

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).parent / "robbot_learning.db"

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
    "how", "what", "why", "when", "where", "can", "could", "would", "should",
    "to", "of", "in", "for", "on", "with", "about", "from", "by", "it",
    "i", "he", "she", "his", "her", "rob", "robert", "robbot", "video", "videos",
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_hash TEXT NOT NULL,
    query_raw TEXT NOT NULL,
    query_normalized TEXT NOT NULL,
    videos_used TEXT,
    response_length INTEGER,
    source TEXT,
    followed_up BOOLEAN DEFAULT FALSE,
    cluster_id INTEGER
);

CREATE TABLE IF NOT EXISTS faq (
    id INTEGER PRIMARY KEY,
    query_pattern TEXT UNIQUE NOT NULL,
    cached_response TEXT NOT NULL,
    videos TEXT,
    hit_count INTEGER DEFAULT 0,
    quality_score REAL DEFAULT 0.5,
    created_at TEXT,
    last_hit TEXT,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS query_clusters (
    id INTEGER PRIMARY KEY,
    keywords TEXT NOT NULL,
    query_count INTEGER DEFAULT 0,
    has_faq BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_interactions_normalized ON interactions(query_normalized);
CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_hash, ts);
CREATE INDEX IF NOT EXISTS idx_faq_pattern ON faq(query_pattern);
"""


def _now_iso() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class LearningDB:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        """Open/create SQLite DB, create tables if needed."""
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._apply_schema()

    def _apply_schema(self) -> None:
        """Create tables and indexes if they do not exist."""
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Query normalisation
    # ------------------------------------------------------------------

    def normalize_query(self, text: str) -> str:
        """Lowercase, strip punctuation, remove stopwords, sort remaining words.

        Steps:
        1. Lowercase.
        2. Replace any non-alphanumeric character with a space.
        3. Split into words.
        4. Remove stopwords.
        5. Sort words alphabetically.
        6. Join with single spaces.
        """
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", " ", text)
        words = [w for w in text.split() if w and w not in STOPWORDS]
        words.sort()
        return " ".join(words)

    # ------------------------------------------------------------------
    # Interaction logging
    # ------------------------------------------------------------------

    def log_interaction(
        self,
        user_id: int,
        query_raw: str,
        videos_used: list,
        response_length: int,
        source: str = "ask",
    ) -> int:
        """Log an interaction.

        - user_id is hashed (SHA256[:12]) for privacy.
        - Detects follow-ups: if the same user made a request within the last
          60 seconds, marks that previous interaction as followed_up=True.

        Returns the row id of the newly inserted interaction.
        """
        user_hash = hashlib.sha256(str(user_id).encode()).hexdigest()[:12]
        query_normalized = self.normalize_query(query_raw)
        videos_json = json.dumps(videos_used) if videos_used is not None else None
        ts = _now_iso()

        # Follow-up detection: look for the most recent interaction from this
        # user within 60 seconds of now.
        cur = self._conn.execute(
            """
            SELECT id, ts FROM interactions
            WHERE user_hash = ?
            ORDER BY ts DESC
            LIMIT 1
            """,
            (user_hash,),
        )
        row = cur.fetchone()
        if row:
            try:
                prev_ts = datetime.fromisoformat(row["ts"])
                now_ts = datetime.fromisoformat(ts)
                # Make both offset-aware or both offset-naive for comparison.
                if prev_ts.tzinfo is None:
                    prev_ts = prev_ts.replace(tzinfo=timezone.utc)
                if now_ts.tzinfo is None:
                    now_ts = now_ts.replace(tzinfo=timezone.utc)
                delta = (now_ts - prev_ts).total_seconds()
                if 0 <= delta <= 60:
                    self._conn.execute(
                        "UPDATE interactions SET followed_up = TRUE WHERE id = ?",
                        (row["id"],),
                    )
            except (ValueError, TypeError):
                pass  # Malformed timestamp — skip follow-up detection.

        cur = self._conn.execute(
            """
            INSERT INTO interactions
                (ts, user_hash, query_raw, query_normalized,
                 videos_used, response_length, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                user_hash,
                query_raw,
                query_normalized,
                videos_json,
                response_length,
                source,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    # ------------------------------------------------------------------
    # FAQ retrieval
    # ------------------------------------------------------------------

    def get_faq_match(
        self, query_raw: str, threshold: float = 0.8
    ) -> Optional[dict]:
        """Check if normalized query matches an FAQ entry.

        Uses Jaccard similarity (word overlap) between the normalized input
        and each stored FAQ query_pattern.

        Returns a dict with keys {id, response, videos, quality_score} for
        the best match above threshold, or None if no match found.
        """
        normalized = self.normalize_query(query_raw)
        query_words = set(normalized.split()) if normalized else set()

        if not query_words:
            return None

        cur = self._conn.execute(
            "SELECT id, query_pattern, cached_response, videos, quality_score FROM faq"
        )
        rows = cur.fetchall()

        best_score = -1.0
        best_row = None

        for row in rows:
            pattern_words = set(row["query_pattern"].split()) if row["query_pattern"] else set()
            if not pattern_words:
                continue
            intersection = query_words & pattern_words
            union = query_words | pattern_words
            jaccard = len(intersection) / len(union) if union else 0.0
            if jaccard > best_score:
                best_score = jaccard
                best_row = row

        if best_row is None or best_score < threshold:
            return None

        videos = None
        if best_row["videos"]:
            try:
                videos = json.loads(best_row["videos"])
            except (json.JSONDecodeError, TypeError):
                videos = best_row["videos"]

        return {
            "id": best_row["id"],
            "response": best_row["cached_response"],
            "videos": videos,
            "quality_score": best_row["quality_score"],
        }

    # ------------------------------------------------------------------
    # FAQ management
    # ------------------------------------------------------------------

    def record_faq_hit(self, faq_id: int) -> None:
        """Increment hit_count and update last_hit timestamp for an FAQ entry."""
        self._conn.execute(
            """
            UPDATE faq
            SET hit_count = hit_count + 1,
                last_hit = ?
            WHERE id = ?
            """,
            (_now_iso(), faq_id),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def get_interaction_count(self) -> int:
        """Total number of logged interactions."""
        cur = self._conn.execute("SELECT COUNT(*) FROM interactions")
        return cur.fetchone()[0]

    def get_top_topics(self, limit: int = 10) -> list:
        """Most frequent normalized queries, returned as list of (query, count) tuples."""
        cur = self._conn.execute(
            """
            SELECT query_normalized, COUNT(*) AS cnt
            FROM interactions
            WHERE query_normalized != ''
            GROUP BY query_normalized
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [(row[0], row[1]) for row in cur.fetchall()]

    def get_stats(self) -> dict:
        """Return aggregate statistics about bot usage.

        Keys:
            total_interactions  — total rows in interactions table
            total_faq_entries   — total rows in faq table
            faq_hit_rate        — fraction of interactions that matched an FAQ
                                  (approximated as total FAQ hits / total interactions)
            top_topics          — list of (normalized_query, count) tuples
            follow_up_rate      — fraction of interactions marked as followed_up
        """
        total_interactions: int = self.get_interaction_count()

        cur = self._conn.execute("SELECT COUNT(*) FROM faq")
        total_faq_entries: int = cur.fetchone()[0]

        cur = self._conn.execute("SELECT COALESCE(SUM(hit_count), 0) FROM faq")
        total_faq_hits: int = cur.fetchone()[0]

        faq_hit_rate: float = (
            total_faq_hits / total_interactions if total_interactions > 0 else 0.0
        )

        cur = self._conn.execute(
            "SELECT COUNT(*) FROM interactions WHERE followed_up = TRUE"
        )
        follow_up_count: int = cur.fetchone()[0]
        follow_up_rate: float = (
            follow_up_count / total_interactions if total_interactions > 0 else 0.0
        )

        top_topics = self.get_top_topics(10)

        return {
            "total_interactions": total_interactions,
            "total_faq_entries": total_faq_entries,
            "faq_hit_rate": faq_hit_rate,
            "top_topics": top_topics,
            "follow_up_rate": follow_up_rate,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close DB connection."""
        self._conn.close()

    # Support use as a context manager.
    def __enter__(self) -> "LearningDB":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
