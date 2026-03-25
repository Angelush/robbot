"""
bot.py — RobBot Discord bot: channels the spirit of Robert Murray-Smith

Usage:
    python bot.py
"""
import asyncio
import logging
import random
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import config
import rag
from llm import router as llm_router
from personality import (
    is_grief_message,
    is_prompt_injection,
    is_off_topic,
    get_grief_response,
    get_injection_response,
    get_off_topic_response,
    format_response,
)

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


def check_cooldown(user_id: int) -> float | None:
    """Return seconds remaining if user is on cooldown, else None."""
    now = time.time()
    remaining = config.USER_COOLDOWN_SECONDS - (now - _user_cooldowns[user_id])
    if remaining > 0:
        return remaining
    _user_cooldowns[user_id] = now
    return None

# ---------------------------------------------------------------------------
# /ask — main RAG pipeline
# ---------------------------------------------------------------------------

@bot.tree.command(name="ask", description="Ask RobBot about Robert Murray-Smith's work")
@app_commands.describe(question="What would you like to know?")
async def ask(interaction: discord.Interaction, question: str):
    # Rate limit
    cooldown = check_cooldown(interaction.user.id)
    if cooldown:
        await interaction.response.send_message(
            f"Easy there, mate! Give me {cooldown:.0f} more seconds. \U0001F60A",
            ephemeral=True,
        )
        return

    # Grief check (no LLM needed)
    if is_grief_message(question):
        await interaction.response.send_message(get_grief_response())
        return

    # Prompt injection check (no LLM needed)
    if is_prompt_injection(question):
        await interaction.response.send_message(get_injection_response())
        return

    # Off-topic keyword check (no LLM needed)
    if is_off_topic(question):
        await interaction.response.send_message(get_off_topic_response())
        return

    # Defer — LLM call will take a few seconds
    await interaction.response.defer()

    try:
        # RAG search
        context_docs = rag.search(question)

        # If RAG returns nothing useful, it might be off-topic
        if not context_docs:
            await interaction.followup.send(
                "Hmm, I couldn't find anything in Rob's archive about that! \U0001F914 "
                "Try rephrasing, or use `/search` with a specific topic like "
                "graphene, batteries, or supercapacitors. Cheers!"
            )
            return

        # Build prompt and call LLM
        messages = rag.build_messages(question, context_docs)
        answer = await llm_router.generate(messages)

        # Format with video links
        videos = [
            {"title": d["title"], "url": d["url"]}
            for d in context_docs
        ]
        formatted = format_response(answer, videos)
        await interaction.followup.send(formatted)

    except Exception as e:
        log.error(f"Error in /ask: {e}", exc_info=True)
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
    results = rag.search_topics(topic, limit=10)

    if not results:
        await interaction.response.send_message(
            f"No videos found for \"{topic}\". \U0001F914 "
            f"Try a broader term like 'graphene', 'battery', or 'supercapacitor'.",
            ephemeral=True,
        )
        return

    lines = [f"**Found {len(results)} video(s) about \"{topic}\":**\n"]
    for item in results[:8]:  # Limit to 8 to stay under Discord limit
        title = item.get("t", "Untitled")
        vid_id = item.get("id", "")
        date = item.get("d", "")[:10]
        url = item.get("url", f"https://www.youtube.com/watch?v={vid_id}")
        lines.append(f"> **{title}** ({date})\n> {url}")

    if len(results) > 8:
        lines.append(f"\n*...and {len(results) - 8} more. Try `/ask` for a detailed answer!*")

    lines.append("\n-# RobBot | Fan-made tribute bot")

    response = "\n".join(lines)
    if len(response) > 1950:
        response = response[:1940] + "...\n-# RobBot"

    await interaction.response.send_message(response)

# ---------------------------------------------------------------------------
# /random — random video recommendation
# ---------------------------------------------------------------------------

@bot.tree.command(name="random", description="Get a random video recommendation from Rob's archive")
async def random_video(interaction: discord.Interaction):
    try:
        video = rag.get_random_video()
        if not video:
            await interaction.response.send_message(
                "Hmm, my index seems empty. Something's not right! \U0001F914",
                ephemeral=True,
            )
            return

        title = video.get("t", "Untitled")
        vid_id = video.get("id", "")
        date = video.get("d", "")[:10]
        url = video.get("url", f"https://www.youtube.com/watch?v={vid_id}")
        topics = ", ".join(video.get("topics", [])[:5]) if video.get("topics") else "general science"

        await interaction.response.send_message(
            f"\U0001F3B2 **Here's a random pick from Rob's archive!**\n\n"
            f"> **{title}** ({date})\n"
            f"> Topics: {topics}\n"
            f"> {url}\n\n"
            f"-# RobBot | Fan-made tribute bot"
        )
    except Exception as e:
        log.error(f"Error in /random: {e}", exc_info=True)
        await interaction.response.send_message(
            "Oops, something went wrong picking a video! \U0001F527 Try again.",
            ephemeral=True,
        )

# ---------------------------------------------------------------------------
# /3d — search Thingiverse designs
# ---------------------------------------------------------------------------

@bot.tree.command(name="3d", description="Search Rob's 3D printable designs")
@app_commands.describe(query="What kind of 3D design are you looking for?")
async def search_3d(interaction: discord.Interaction, query: str):
    # Search ChromaDB with source filter
    results = rag.search(query, top_k=5)
    thingiverse_results = [r for r in results if "thingiverse" in r.get("channel", "")]

    if not thingiverse_results:
        await interaction.response.send_message(
            f"No 3D designs found for \"{query}\". \U0001F914 "
            f"Try broader terms like 'gear', 'motor', or 'holder'.",
            ephemeral=True,
        )
        return

    lines = [f"**Found {len(thingiverse_results)} 3D design(s) for \"{query}\":**\n"]
    for item in thingiverse_results:
        title = item.get("title", "Untitled")
        url = item.get("url", "")
        lines.append(f"> **{title}**\n> {url}")

    lines.append(
        f"\n\U0001F4E5 [Browse all STL files on MEGA]"
        f"(https://mega.nz/folder/fbhDSSRK#Wa1i4bl385a5qtcN6kPs7g)"
    )
    lines.append("\n-# RobBot | Fan-made tribute bot")

    await interaction.response.send_message("\n".join(lines))

# ---------------------------------------------------------------------------
# /about — static info
# ---------------------------------------------------------------------------

@bot.tree.command(name="about", description="About RobBot and Robert Murray-Smith")
async def about(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**RobBot** \U0001F916\U0001F52C\n\n"
        "A fan-made tribute bot for **Robert Murray-Smith** (1965–2025), "
        "a brilliant inventor, educator, and YouTuber from Scotland.\n\n"
        "Robert made over 2,400 videos about graphene, batteries, supercapacitors, "
        "solar cells, 3D printing, and countless DIY science experiments. "
        "His curiosity and generosity in sharing knowledge inspired thousands.\n\n"
        "**This bot can:**\n"
        "\U0001F50D `/ask` — Answer questions about Rob's work\n"
        "\U0001F4CB `/search` — Find videos by topic or material\n"
        "\U0001F3B2 `/random` — Get a random video recommendation\n"
        "\U0001F528 `/3d` — Search Rob's 3D printable designs\n\n"
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
    # Ignore own messages
    if message.author == bot.user:
        return

    # Let prefix commands process — but don't double-handle
    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Respond to @mentions and DMs only
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mention = bot.user in message.mentions if bot.user else False

    if not is_dm and not is_mention:
        return

    # Strip the mention and punctuation-only leftovers from the message
    question = message.content
    if bot.user:
        question = question.replace(f"<@{bot.user.id}>", "").strip()
        question = question.replace(f"<@!{bot.user.id}>", "").strip()

    # Detect greetings and near-empty messages
    import re
    cleaned = re.sub(r"[^\w\s]", "", question).lower().strip()
    GREETINGS = {"hey", "hi", "hello", "howdy", "sup", "yo", "hola", "oi", "hiya",
                 "greetings", "cheers", "heya", "cheers mate", "hey mate", "hi mate",
                 "hello mate", "whats up", "wassup", "good morning", "good evening",
                 "good afternoon", "morning", "evening", "afternoon"}
    if not cleaned or cleaned in GREETINGS:
        await message.reply(
            "Hey mate! \U0001F44B Ask me anything about Rob's experiments, "
            "or try `/search graphene` to find specific videos. Cheers!"
        )
        return

    # Rate limit
    cooldown = check_cooldown(message.author.id)
    if cooldown:
        await message.reply(
            f"Easy there! Give me {cooldown:.0f} more seconds. \U0001F60A"
        )
        return

    # Grief / injection / off-topic checks
    if is_grief_message(question):
        await message.reply(get_grief_response())
        return
    if is_prompt_injection(question):
        await message.reply(get_injection_response())
        return
    if is_off_topic(question):
        await message.reply(get_off_topic_response())
        return

    # Show typing indicator while processing
    async with message.channel.typing():
        try:
            context_docs = rag.search(question)
            if not context_docs:
                await message.reply(
                    "Hmm, I couldn't find anything in Rob's archive about that! \U0001F914 "
                    "Try a specific topic like graphene, batteries, or supercapacitors."
                )
                return

            messages_list = rag.build_messages(question, context_docs)
            answer = await llm_router.generate(messages_list)
            videos = [{"title": d["title"], "url": d["url"]} for d in context_docs]
            formatted = format_response(answer, videos)
            await message.reply(formatted)

        except Exception as e:
            log.error(f"Error in mention handler: {e}", exc_info=True)
            await message.reply(
                "Oops, something went wrong! \U0001F527 Try `/ask` or `/search` instead."
            )

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Load indexes
    rag.load_indexes()

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")

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
