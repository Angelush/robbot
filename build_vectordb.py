"""
build_vectordb.py — One-time script to embed all video summaries into ChromaDB.

Run this locally where the archive lives. The resulting chroma_db/ folder
is all you need to deploy the bot.

Usage:
    python build_vectordb.py
    python build_vectordb.py --archive-path /path/to/archive
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHANNELS = ["@ThinkingandTinkering", "@TnTtalktime", "@TnTOmnibus"]
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
BATCH_SIZE = 500

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def split_summary(text: str) -> tuple[str, str]:
    """
    Split a summary.md into (overview, topics_and_techniques).
    Overview = everything before '## Key Topics'.
    Topics = '## Key Topics' through '## Notable Timestamps' (exclusive).
    """
    # Extract overview
    overview_match = re.search(r"## Overview\s*\n(.*?)(?=\n## )", text, re.DOTALL)
    overview = overview_match.group(1).strip() if overview_match else text[:500]

    # Extract key topics + techniques
    topics_match = re.search(
        r"(## Key Topics.*?)(?=\n## Notable Timestamps|\n## Robert's Own Replies|\Z)",
        text,
        re.DOTALL,
    )
    topics = topics_match.group(1).strip() if topics_match else ""

    return overview, topics


def load_compact_index(archive_path: Path) -> dict:
    """Load index-compact.json and return video_id -> metadata dict."""
    compact_path = archive_path / "index-compact.json"
    if not compact_path.exists():
        print(f"ERROR: {compact_path} not found. Run 05_build_indexes.py first.")
        sys.exit(1)

    with open(compact_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else data.get("items", data.get("entries", []))
    return {item["id"]: item for item in items if "id" in item}

# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------

def build(archive_path: Path, output_dir: Path):
    print(f"Archive: {archive_path}")
    print(f"Output:  {output_dir}")
    print(f"Model:   {EMBEDDING_MODEL}")
    print()

    # Load metadata
    video_meta = load_compact_index(archive_path)
    print(f"Loaded {len(video_meta)} items from index-compact.json")

    # Setup ChromaDB
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name=EMBEDDING_MODEL
    )
    client = chromadb.PersistentClient(path=str(output_dir))
    collection = client.get_or_create_collection(
        name="rob_videos",
        embedding_function=ef,
        metadata={"hnsw:space": "cosine"},
    )

    documents = []
    metadatas = []
    ids = []

    # --- YouTube videos ---
    print("\nProcessing YouTube summaries...")
    video_count = 0
    for channel in CHANNELS:
        channel_path = archive_path / "youtube" / channel
        if not channel_path.exists():
            print(f"  SKIP {channel}: directory not found")
            continue

        for video_dir in sorted(channel_path.iterdir()):
            if not video_dir.is_dir():
                continue

            video_id = video_dir.name
            summary_path = video_dir / "summary.md"
            if not summary_path.exists():
                continue

            summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
            meta = video_meta.get(video_id, {})

            overview, topics = split_summary(summary_text)

            for chunk_type, chunk_text in [("overview", overview), ("topics", topics)]:
                if not chunk_text.strip():
                    continue

                doc_id = f"{video_id}_{chunk_type}"
                documents.append(chunk_text[:2000])  # MiniLM context limit safety
                metadatas.append({
                    "video_id": video_id,
                    "title": str(meta.get("t", "Unknown"))[:200],
                    "channel": str(meta.get("ch", "")),
                    "date": str(meta.get("d", "")),
                    "url": str(meta.get("url", f"https://www.youtube.com/watch?v={video_id}")),
                    "chunk_type": chunk_type,
                    "source": "youtube",
                })
                ids.append(doc_id)

            video_count += 1
            if video_count % 500 == 0:
                print(f"  Processed {video_count} videos...")

    print(f"  Total: {video_count} videos -> {len(documents)} chunks")

    # --- Thingiverse 3D files ---
    print("\nProcessing Thingiverse items...")
    thingiverse_path = archive_path / "3d-files" / "thingiverse"
    thing_count = 0

    if thingiverse_path.exists():
        for thing_dir in sorted(thingiverse_path.iterdir()):
            if not thing_dir.is_dir():
                continue

            metadata_path = thing_dir / "metadata.json"
            if not metadata_path.exists():
                continue

            with open(metadata_path, "r", encoding="utf-8") as f:
                thing_meta = json.load(f)

            thing_id = str(thing_meta.get("thing_id", thing_dir.name))
            name = thing_meta.get("name", "Unknown")
            description = thing_meta.get("description", "")
            tags = ", ".join(thing_meta.get("tags", []))

            chunk_text = f"{name}\n{description}\nTags: {tags}".strip()
            if not chunk_text:
                continue

            doc_id = f"tv_{thing_id}"
            documents.append(chunk_text[:2000])
            metadatas.append({
                "video_id": thing_id,
                "title": name[:200],
                "channel": "thingiverse",
                "date": str(thing_meta.get("added", ""))[:10],
                "url": str(thing_meta.get("url", f"https://www.thingiverse.com/thing:{thing_id}")),
                "chunk_type": "3d_model",
                "source": "thingiverse",
            })
            ids.append(doc_id)
            thing_count += 1

    print(f"  Total: {thing_count} Thingiverse items")

    # --- Batch insert ---
    total = len(documents)
    print(f"\nInserting {total} chunks into ChromaDB...")

    for i in range(0, total, BATCH_SIZE):
        batch_end = min(i + BATCH_SIZE, total)
        collection.add(
            documents=documents[i:batch_end],
            metadatas=metadatas[i:batch_end],
            ids=ids[i:batch_end],
        )
        print(f"  {batch_end}/{total}")

    print(f"\nDone! Collection has {collection.count()} chunks.")
    print(f"ChromaDB stored at: {output_dir}")

    # --- Copy data files for deployment ---
    data_dir = output_dir.parent / "data"
    data_dir.mkdir(exist_ok=True)

    for filename in ["index-compact.json", "topics.json"]:
        src = archive_path / filename
        dst = data_dir / filename
        if src.exists():
            shutil.copy2(src, dst)
            size_mb = dst.stat().st_size / 1024 / 1024
            print(f"Copied {filename} -> data/ ({size_mb:.1f} MB)")

    # Copy 3d-files index too
    thingiverse_index = archive_path / "3d-files" / "index.json"
    if thingiverse_index.exists():
        shutil.copy2(thingiverse_index, data_dir / "3d-index.json")
        print("Copied 3d-files/index.json -> data/3d-index.json")

    print("\nAll done! To deploy, copy chroma_db/ and data/ to your server.")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build ChromaDB vector database from Rob archive")
    parser.add_argument("--archive-path", type=str, default=None,
                        help="Path to the archive (default: from .env ARCHIVE_PATH)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output ChromaDB directory (default: ./chroma_db)")
    args = parser.parse_args()

    # Determine archive path
    if args.archive_path:
        archive = Path(args.archive_path)
    else:
        try:
            from dotenv import load_dotenv
            import os
            load_dotenv()
            archive = Path(os.getenv("ARCHIVE_PATH", r"C:\Users\Angelus\My Drive\IA\Rob"))
        except ImportError:
            archive = Path(r"C:\Users\Angelus\My Drive\IA\Rob")

    output = Path(args.output) if args.output else Path(__file__).parent / "chroma_db"

    if not archive.exists():
        print(f"ERROR: Archive not found at {archive}")
        sys.exit(1)

    build(archive, output)
