"""
bot.py — RobBot Discord bot: channels the spirit of Robert Murray-Smith

Usage:
    python bot.py
"""
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import config
from llm import router as llm_router
from messages import build_messages
from personality import (
    is_grief_message,
    is_prompt_injection,
    is_off_topic,
    get_grief_response,
    get_injection_response,
    get_off_topic_response,
    format_response,
)

# Add archive repo to Python path so we can import archive_search
sys.path.insert(0, str(config.ARCHIVE_PATH))
from archive_search import ArchiveSearch
from learning import LearningDB
from faq_builder import FAQBuilder

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("robbot")

# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Per-user cooldown tracker
_user_cooldowns: dict[int, float] = defaultdict(float)

# Archive search instance (initialized in on_ready)
archive: ArchiveSearch | None = None

# Learning DB instance (initialized in on_ready)
learning_db: LearningDB | None = None

# FAQ builder instance (initialized in on_ready)
faq_builder: FAQBuilder | None = None

GREETINGS = {
    "hey", "hi", "hello", "howdy", "sup", "yo", "hola", "oi", "hiya",
    "greetings", "cheers", "heya", "cheers mate", "hey mate", "hi mate",
    "hello mate", "whats up", "wassup", "good morning", "good evening",
    "good afternoon", "morning", "evening", "afternoon",
}


def check_cooldown(user_id: int) -> float | None:
    """Return seconds remaining if user is on cooldown, else None."""
    now = time.time()
    remaining = config.USER_COOLDOWN_SECONDS - (now - _user_cooldowns[user_id])
    if remaining > 0:
        return remaining
    _user_cooldowns[user_id] = now
    return None


def _parse_faq_videos(faq_match: dict) -> list:
    """Extract video list from FAQ match, handling both str and list."""
    videos = faq_match.get("videos") or []
    if isinstance(videos, str):
        return json.loads(videos) if videos else []
    return videos


# ---------------------------------------------------------------------------
# Shared RAG pipeline
# ---------------------------------------------------------------------------

async def _process_question(question: str, user_id: int, source: str) -> str:
    """
    Run the full RAG pipeline: FAQ check → search → LLM → format.
    Returns the formatted response string, or raises ValueError if no results.
    """
    # Check FAQ cache first
    if learning_db is not None:
        try:
            faq_match = learning_db.get_faq_match(question)
            if faq_match and faq_match["quality_score"] >= 0.7:
                learning_db.record_faq_hit(faq_match["id"])
                learning_db.log_interaction(
                    user_id=user_id,
                    query_raw=question,
                    videos_used=_parse_faq_videos(faq_match),
                    response_length=len(faq_match["response"]),
                    source="faq",
                )
                return faq_match["response"]
        except Exception as e:
            log.warning("FAQ check failed: %s", e)

    # RAG search
    context_docs = await asyncio.to_thread(
        archive.search_videos, question, config.RAG_MAX_VIDEOS
    )
    if not context_docs:
        raise ValueError("no_results")

    # LLM generation
    messages = build_messages(question, context_docs)
    answer = await llm_router.generate(messages)

    # Format with video links
    videos = [{"title": d.title, "url": d.url} for d in context_docs]
    formatted = format_response(answer, videos)

    # Log interaction
    if learning_db is not None:
        try:
            learning_db.log_interaction(
                user_id=user_id,
                query_raw=question,
                videos_used=[d.video_id for d in context_docs],
                response_length=len(formatted),
                source=source,
            )
            asyncio.create_task(maybe_rebuild_faq())
        except Exception as e:
            log.warning("Failed to log interaction: %s", e)

    return formatted


# ---------------------------------------------------------------------------
# FAQ auto-rebuild helper
# ---------------------------------------------------------------------------

async def maybe_rebuild_faq():
    """Check if FAQ rebuild is needed and run in background."""
    try:
        if faq_builder is not None and faq_builder.should_rebuild():
            log.info("Triggering FAQ rebuild...")
            await asyncio.to_thread(faq_builder.rebuild)
            log.info("FAQ rebuild complete")
    except Exception as e:
        log.warning("FAQ rebuild failed: %s", e)

# ---------------------------------------------------------------------------
# /ask — main RAG pipeline
# ---------------------------------------------------------------------------

@bot.tree.command(name="ask", description="Ask RobBot about Robert Murray-Smith's work")
@app_commands.describe(question="What would you like to know?")
async def ask(interaction: discord.Interaction, question: str):
    cooldown = check_cooldown(interaction.user.id)
    if cooldown:
        await interaction.response.send_message(
            f"Easy there, mate! Give me {cooldown:.0f} more seconds. \U0001F60A",
            ephemeral=True,
        )
        return

    if is_grief_message(question):
        await interaction.response.send_message(get_grief_response())
        return
    if is_prompt_injection(question):
        await interaction.response.send_message(get_injection_response())
        return
    if is_off_topic(question):
        await interaction.response.send_message(get_off_topic_response())
        return

    await interaction.response.defer()

    try:
        formatted = await _process_question(question, interaction.user.id, "ask")
        await interaction.followup.send(formatted)
    except ValueError:
        await interaction.followup.send(
            "Hmm, I couldn't find anything in Rob's archive about that! \U0001F914 "
            "Try rephrasing, or use `/search` with a specific topic like "
            "graphene, batteries, or supercapacitors. Cheers!"
        )
    except Exception as e:
        log.error("Error in /ask: %s", e, exc_info=True)
        await interaction.followup.send(
            "Oops, something went wrong in the workshop! \U0001F527 "
            "Try again in a moment, or use `/search` for direct topic lookups."
        )

# ---------------------------------------------------------------------------
# /search — direct topic lookup (no LLM, instant)
# ---------------------------------------------------------------------------

@bot.tree.command(name="search", description="Search Rob's videos by topic or material")
@app_commands.describe(topic="Topic or material to search for (e.g., graphene, batteries)")
async def search(interaction: discord.Interaction, topic: str):
    results = await asyncio.to_thread(archive.search_topics, topic, 10)

    if not results:
        await interaction.response.send_message(
            f"No videos found for \"{topic}\". \U0001F914 "
            f"Try a broader term like 'graphene', 'battery', or 'supercapacitor'.",
            ephemeral=True,
        )
        return

    lines = [f"**Found {len(results)} video(s) about \"{topic}\":**\n"]
    for item in results[:8]:
        lines.append(f"> **{item.title}** ({item.date[:10]})\n> {item.url}")

    if len(results) > 8:
        lines.append(f"\n*...and {len(results) - 8} more. Try `/ask` for a detailed answer!*")

    lines.append("\n-# RobBot | Fan-made tribute bot")

    response = "\n".join(lines)
    if len(response) > 1950:
        response = response[:1940] + "...\n-# RobBot"

    await interaction.response.send_message(response)

    if learning_db is not None:
        try:
            learning_db.log_interaction(
                user_id=interaction.user.id,
                query_raw=topic,
                videos_used=[item.id for item in results[:8]],
                response_length=len(response),
                source="search",
            )
            asyncio.create_task(maybe_rebuild_faq())
        except Exception as e:
            log.warning("Failed to log /search interaction: %s", e)

# ---------------------------------------------------------------------------
# /random — random video recommendation
# ---------------------------------------------------------------------------

@bot.tree.command(name="random", description="Get a random video recommendation from Rob's archive")
async def random_video(interaction: discord.Interaction):
    video = await asyncio.to_thread(archive.get_random_video)
    if not video:
        await interaction.response.send_message(
            "Hmm, my index seems empty. Something's not right! \U0001F914",
            ephemeral=True,
        )
        return

    topics = ", ".join(video.topics[:5]) if video.topics else "general science"

    await interaction.response.send_message(
        f"\U0001F3B2 **Here's a random pick from Rob's archive!**\n\n"
        f"> **{video.title}** ({video.date[:10]})\n"
        f"> Topics: {topics}\n"
        f"> {video.url}\n\n"
        f"-# RobBot | Fan-made tribute bot"
    )

# ---------------------------------------------------------------------------
# /3d — search Thingiverse designs
# ---------------------------------------------------------------------------

@bot.tree.command(name="3d", description="Search Rob's 3D printable designs")
@app_commands.describe(query="What kind of 3D design are you looking for?")
async def search_3d(interaction: discord.Interaction, query: str):
    results = await asyncio.to_thread(archive.search_3d, query, 5)

    if not results:
        await interaction.response.send_message(
            f"No 3D designs found for \"{query}\". \U0001F914 "
            f"Try broader terms like 'gear', 'motor', or 'holder'.",
            ephemeral=True,
        )
        return

    lines = [f"**Found {len(results)} 3D design(s) for \"{query}\":**\n"]
    for item in results:
        lines.append(f"> **{item.name}**\n> {item.url}")

    lines.append(
        f"\n\U0001F4E5 [Browse all STL files on MEGA]"
        f"(https://mega.nz/folder/fbhDSSRK#Wa1i4bl385a5qtcN6kPs7g)"
    )
    lines.append("\n-# RobBot | Fan-made tribute bot")

    await interaction.response.send_message("\n".join(lines))

# ---------------------------------------------------------------------------
# /stats — usage statistics
# ---------------------------------------------------------------------------

@bot.tree.command(name="stats", description="RobBot usage statistics")
async def stats(interaction: discord.Interaction):
    if learning_db is None:
        await interaction.response.send_message(
            "Learning DB is not initialised yet.", ephemeral=True
        )
        return

    s = learning_db.get_stats()
    top = "\n".join(f"> {q} ({c}x)" for q, c in s["top_topics"][:5])
    await interaction.response.send_message(
        f"**RobBot Stats** \U0001F4CA\n\n"
        f"Total interactions: {s['total_interactions']}\n"
        f"FAQ entries: {s['total_faq_entries']}\n"
        f"FAQ hit rate: {s['faq_hit_rate']:.1%}\n"
        f"Follow-up rate: {s['follow_up_rate']:.1%}\n\n"
        f"**Top topics:**\n{top}\n\n"
        f"-# RobBot | Fan-made tribute bot",
        ephemeral=True,
    )

# ---------------------------------------------------------------------------
# /about — static info
# ---------------------------------------------------------------------------

@bot.tree.command(name="about", description="About RobBot and Robert Murray-Smith")
async def about(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**RobBot** \U0001F916\U0001F52C\n\n"
        "A fan-made tribute bot for **Robert Murray-Smith** (1965\u20132025), "
        "a brilliant inventor, educator, and YouTuber.\n\n"
        "Robert made over 2,400 videos about graphene, batteries, supercapacitors, "
        "solar cells, 3D printing, and countless DIY science experiments. "
        "His curiosity and generosity in sharing knowledge inspired thousands.\n\n"
        "**This bot can:**\n"
        "\U0001F50D `/ask` \u2014 Answer questions about Rob's work\n"
        "\U0001F4CB `/search` \u2014 Find videos by topic or material\n"
        "\U0001F3B2 `/random` \u2014 Get a random video recommendation\n"
        "\U0001F528 `/3d` \u2014 Search Rob's 3D printable designs\n\n"
        "**Channels archived:**\n"
        "> [@ThinkingandTinkering](https://www.youtube.com/@ThinkingandTinkering) (2,122 videos)\n"
        "> [@TnTtalktime](https://www.youtube.com/@TnTtalktime) (83 videos)\n"
        "> [@TnTOmnibus](https://www.youtube.com/@TnTOmnibus) (56 videos)\n\n"
        "**Archive:** [GitHub](https://github.com/Angelush/robert-murray-smith-archive)\n"
        "**STL files:** [MEGA](https://mega.nz/folder/fbhDSSRK#Wa1i4bl385a5qtcN6kPs7g)\n\n"
        "-# Built with love by fans, for fans."
    )

# ---------------------------------------------------------------------------
# @mention and DM handler
# ---------------------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Respond to @mentions and DMs only
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user in message.mentions if bot.user else False

    if not is_dm and not is_mention:
        return

    # Strip the mention
    question = message.content
    if bot.user:
        question = question.replace(f"<@{bot.user.id}>", "").strip()
        question = question.replace(f"<@!{bot.user.id}>", "").strip()

    # Detect greetings and near-empty messages
    cleaned = re.sub(r"[^\w\s]", "", question).lower().strip()
    if not cleaned or cleaned in GREETINGS:
        await message.reply(
            "Hey mate! \U0001F44B Ask me anything about Rob's experiments, "
            "or try `/search graphene` to find specific videos. Cheers!"
        )
        return

    cooldown = check_cooldown(message.author.id)
    if cooldown:
        await message.reply(f"Easy there! Give me {cooldown:.0f} more seconds. \U0001F60A")
        return

    if is_grief_message(question):
        await message.reply(get_grief_response())
        return
    if is_prompt_injection(question):
        await message.reply(get_injection_response())
        return
    if is_off_topic(question):
        await message.reply(get_off_topic_response())
        return

    async with message.channel.typing():
        try:
            formatted = await _process_question(question, message.author.id, "mention")
            await message.reply(formatted)
        except ValueError:
            await message.reply(
                "Hmm, I couldn't find anything in Rob's archive about that! \U0001F914 "
                "Try a specific topic like graphene, batteries, or supercapacitors."
            )
        except Exception as e:
            log.error("Error in mention handler: %s", e, exc_info=True)
            await message.reply(
                "Oops, something went wrong! \U0001F527 Try `/ask` or `/search` instead."
            )

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    global archive, learning_db, faq_builder
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)

    chroma_dir = None
    if os.getenv("CHROMA_ENABLED", "").lower() in ("1", "true", "yes"):
        candidate = config.ARCHIVE_PATH / "chroma_db"
        if candidate.exists():
            chroma_dir = candidate
    archive = ArchiveSearch(config.ARCHIVE_PATH, chroma_dir=chroma_dir)
    s = archive.stats
    log.info(
        "Archive loaded: %d videos, %d 3D designs, ChromaDB: %s",
        s.total_videos, s.total_3d_items, "yes" if s.has_chromadb else "no",
    )

    learning_db = LearningDB()
    log.info("Learning DB: %d interactions logged", learning_db.get_interaction_count())

    faq_builder = FAQBuilder(learning_db)

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d slash command(s)", len(synced))
    except Exception as e:
        log.error("Failed to sync commands: %s", e)

    log.info("RobBot is ready! Cheers mate!")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not config.DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN not set in .env file!")
        print("Copy .env.example to .env and fill in your Discord bot token.")
        print("See README.md for setup instructions.")
        exit(1)

    bot.run(config.DISCORD_TOKEN)
