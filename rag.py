"""
rag.py — RAG pipeline: ChromaDB search, context assembly, prompt building
"""
import json
import logging
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

import config
from personality import SYSTEM_PROMPT

log = logging.getLogger("robbot.rag")

# ---------------------------------------------------------------------------
# Startup: load ChromaDB + in-memory indexes
# ---------------------------------------------------------------------------

_ef = embedding_functions.SentenceTransformerEmbeddingFunction(
    model_name="all-MiniLM-L6-v2"
)

_client = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
_collection = _client.get_or_create_collection(
    name="rob_videos",
    embedding_function=_ef,
    metadata={"hnsw:space": "cosine"},
)

# Load in-memory indexes for /search and metadata lookups
_compact_index: dict[str, dict] = {}  # video_id -> metadata
_topics_index: dict[str, list[str]] = {}  # topic -> [video_ids]
_materials_index: dict[str, list[str]] = {}  # material -> [video_ids]

def load_indexes():
    """Load index-compact.json and topics.json into memory."""
    global _compact_index, _topics_index, _materials_index

    compact_path = config.DATA_DIR / "index-compact.json"
    topics_path = config.DATA_DIR / "topics.json"

    if compact_path.exists():
        with open(compact_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else data.get("items", data.get("entries", []))
        for item in items:
            vid = item.get("id", "")
            if vid:
                _compact_index[vid] = item
        log.info(f"Loaded {len(_compact_index)} items from index-compact.json")
    else:
        log.warning(f"index-compact.json not found at {compact_path}")

    if topics_path.exists():
        with open(topics_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _topics_index.update(data.get("topics", data.get("key_topics", {})))
        _materials_index.update(data.get("materials", data.get("materials_mentioned", {})))
        log.info(f"Loaded {len(_topics_index)} topics, {len(_materials_index)} materials")
    else:
        log.warning(f"topics.json not found at {topics_path}")

# ---------------------------------------------------------------------------
# Search: ChromaDB vector similarity
# ---------------------------------------------------------------------------

def search(query: str, top_k: int = None) -> list[dict]:
    """
    Embed query, search ChromaDB, deduplicate by video_id.
    Returns list of dicts with: video_id, title, url, date, channel, document (summary text).
    """
    top_k = top_k or config.RAG_TOP_K

    if _collection.count() == 0:
        log.warning("ChromaDB collection is empty — run build_vectordb.py first")
        return []

    results = _collection.query(
        query_texts=[query],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )

    if not results["ids"][0]:
        return []

    # Deduplicate by video_id, keep the best (lowest distance) per video
    seen = {}
    for i, doc_id in enumerate(results["ids"][0]):
        meta = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        vid = meta.get("video_id", doc_id)

        # Skip low-relevance results (cosine distance > threshold means less similar)
        if distance > (1 - config.RAG_MIN_RELEVANCE):
            continue

        if vid not in seen or distance < seen[vid]["distance"]:
            seen[vid] = {
                "video_id": vid,
                "title": meta.get("title", "Untitled"),
                "url": meta.get("url", f"https://www.youtube.com/watch?v={vid}"),
                "date": meta.get("date", ""),
                "channel": meta.get("channel", ""),
                "document": results["documents"][0][i],
                "distance": distance,
            }

    # Sort by relevance (lowest distance first), limit to max videos
    ranked = sorted(seen.values(), key=lambda x: x["distance"])
    return ranked[: config.RAG_MAX_VIDEOS]

# ---------------------------------------------------------------------------
# Topic search: direct lookup, no LLM needed
# ---------------------------------------------------------------------------

def search_topics(query: str, limit: int = 10) -> list[dict]:
    """
    Search topics.json for matching topics/materials.
    Returns metadata dicts from index-compact.json.
    """
    query_lower = query.lower().strip()
    matching_ids = set()

    for topic, vid_ids in _topics_index.items():
        if query_lower in topic.lower():
            matching_ids.update(vid_ids)

    for material, vid_ids in _materials_index.items():
        if query_lower in material.lower():
            matching_ids.update(vid_ids)

    # Look up metadata for matching IDs
    results = []
    for vid_id in matching_ids:
        meta = _compact_index.get(vid_id)
        if meta:
            results.append(meta)

    # Sort by date descending
    results.sort(key=lambda x: x.get("d", ""), reverse=True)
    return results[:limit]

# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def build_messages(question: str, context_docs: list[dict]) -> list[dict]:
    """
    Assemble the message list for the LLM:
    [system_prompt, context, user_question]
    """
    # Build context block from search results
    context_parts = []
    for i, doc in enumerate(context_docs, 1):
        title = doc.get("title", "Untitled")
        url = doc.get("url", "")
        date = doc.get("date", "")
        text = doc.get("document", "")[:1500]  # Truncate long summaries
        context_parts.append(
            f"[Video {i}] {title} ({date})\n"
            f"URL: {url}\n"
            f"{text}\n"
        )

    context_block = "\n---\n".join(context_parts) if context_parts else "(No relevant videos found in the archive.)"

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Here are relevant excerpts from Robert's video archive:\n\n"
                f"{context_block}\n\n"
                f"---\n\n"
                f"User question: {question}"
            ),
        },
    ]

# ---------------------------------------------------------------------------
# Random video
# ---------------------------------------------------------------------------

def get_random_video() -> dict | None:
    """Return a random YouTube video from the compact index."""
    import random
    if not _compact_index:
        return None
    yt_items = [v for v in _compact_index.values() if v.get("type") == "yt"]
    if not yt_items:
        return None
    return random.choice(yt_items)
