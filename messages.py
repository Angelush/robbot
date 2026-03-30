"""
messages.py — Build LLM message lists from search results.

Extracted from rag.py so the bot can use archive_search (shared library)
while keeping bot-specific prompt assembly here.
"""

from personality import SYSTEM_PROMPT


def build_messages(question: str, context_docs: list) -> list[dict]:
    """
    Assemble the message list for the LLM:
    [system_prompt, context + user_question]

    context_docs can be:
    - list of VideoResult dataclasses (from archive_search)
    - list of dicts with keys: title, url, date, document/summary_text
    """
    context_parts = []
    for i, doc in enumerate(context_docs, 1):
        # Support both dataclass attrs and dict keys
        if hasattr(doc, "title"):
            title = doc.title
            url = doc.url
            date = doc.date
            text = (doc.summary_text if hasattr(doc, "summary_text") else "")[:1500]
        else:
            title = doc.get("title", "Untitled")
            url = doc.get("url", "")
            date = doc.get("date", "")
            text = doc.get("document", doc.get("summary_text", ""))[:1500]

        context_parts.append(
            f"[Video {i}] {title} ({date})\n"
            f"URL: {url}\n"
            f"{text}\n"
        )

    context_block = (
        "\n---\n".join(context_parts)
        if context_parts
        else "(No relevant videos found in the archive.)"
    )

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
