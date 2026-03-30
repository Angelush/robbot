# RobBot

A Discord bot that channels the spirit of **Robert Murray-Smith** — answering questions about his 2,400+ YouTube videos on DIY science, renewable energy, graphene, batteries, 3D printing, and more.

> Built by fans, for fans. Robert Murray-Smith (1965–2025) was a beloved inventor and educator whose curiosity and generosity inspired thousands.

---

## Features

| Command | Description | Uses LLM? |
|---------|-------------|-----------|
| `/ask <question>` | Ask about Rob's work — answers in his style with video links | Yes |
| `/search <topic>` | Find videos by topic or material (instant) | No |
| `/random` | Get a random video recommendation | No |
| `/3d <query>` | Search Rob's 3D printable designs | No |
| `/about` | About the bot and Robert | No |

Also responds to **@mentions** and **DMs**.

### Special behaviors
- **Grief support:** If someone says "I miss Rob" or similar, the bot breaks character and responds empathetically as a fellow fan
- **Prompt injection protection:** Attempts to manipulate the bot are deflected humorously in Rob's style
- **Off-topic filtering:** Non-Rob-related questions get a friendly redirect

---

## Setup

### 1. Create a Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** — name it "RobBot"
3. Go to **Bot** tab:
   - Click **Reset Token** and copy the token (you'll need it for `.env`)
   - Enable **Message Content Intent** under Privileged Gateway Intents
4. Go to **OAuth2** > **URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot Permissions: `Send Messages`, `Read Message History`, `Use Slash Commands`
   - Copy the generated URL and open it to invite the bot to your server

### 2. Get Free LLM API Keys

**Mistral (primary — 1 billion tokens/month free):**
1. Go to [console.mistral.ai](https://console.mistral.ai)
2. Create an account (no credit card needed)
3. Go to API Keys and create one

**Groq (fallback — fast, free tier):**
1. Go to [console.groq.com](https://console.groq.com)
2. Create an account
3. Go to API Keys and create one

### 3. Clone the Archive Repo

The bot reads from the [Robert Murray-Smith Fan Archive](https://github.com/Angelush/robert-murray-smith-archive). Clone it somewhere on your machine:

```bash
git clone https://github.com/Angelush/robert-murray-smith-archive.git
```

Note the path — you'll need it for the `.env` file.

### 4. Install & Configure

```bash
# Clone the bot repo
git clone https://github.com/Angelush/robbot.git
cd robbot

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env — set your Discord token, API keys, and ARCHIVE_PATH
```

Your `.env` should include:

```
DISCORD_TOKEN=your_discord_token
MISTRAL_API_KEY=your_mistral_key
GROQ_API_KEY=your_groq_key
ARCHIVE_PATH=/path/to/robert-murray-smith-archive
```

### 5. Build the Vector Database (Optional)

For semantic search (`/ask` command), build the ChromaDB index from the archive. This step runs from **the archive repo**, not the bot repo:

```bash
cd /path/to/robert-murray-smith-archive
pip install chromadb sentence-transformers
python build_vectordb.py
```

This creates `chroma_db/` (~100 MB) inside the archive directory. The bot reads it from `ARCHIVE_PATH/chroma_db/`.

### 6. Run the Bot

```bash
python bot.py
```

---

## Deployment (Oracle Cloud Free Tier)

The bot runs comfortably on Oracle Cloud's free ARM instance (24GB RAM, 4 vCPU):

```bash
# Clone both repos on the server
git clone https://github.com/Angelush/robert-murray-smith-archive.git /app/archive
git clone https://github.com/Angelush/robbot.git /app/robbot

# Build the vector DB on the server (or transfer chroma_db/ from local)
cd /app/archive
pip install chromadb sentence-transformers
python build_vectordb.py

# Configure the bot
cd /app/robbot
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set ARCHIVE_PATH=/app/archive and your API keys

# Run
python bot.py

# Or with Docker
docker build -t robbot .
docker run -d --restart unless-stopped --name robbot robbot
```

---

## Architecture

```
User message
    |
    v
[Grief check] --> pre-written empathetic response (no LLM)
    |
[Injection check] --> humorous Rob-style deflection (no LLM)
    |
[Off-topic check] --> friendly redirect (no LLM)
    |
    v
[Embed query with MiniLM]
    |
[ChromaDB similarity search -> top 5 videos]
    |
[Build prompt: system + context + question]
    |
[Mistral API] --> fallback [Groq] --> fallback [Ollama]
    |
[Format response with video links]
    |
    v
Discord reply in Rob's voice
```

**Zero-cost design:** Mistral free tier provides 1B tokens/month. The `/search` command uses direct topic lookups (no LLM at all). Grief, injection, and off-topic detection use regex (no LLM).

---

## Data Sources

This bot is powered by the [Robert Murray-Smith Fan Archive](https://github.com/Angelush/robert-murray-smith-archive):
- 2,261 YouTube videos with transcripts, AI summaries, and comments
- 181 Thingiverse 3D designs
- STL files hosted on [MEGA](https://mega.nz/folder/fbhDSSRK#Wa1i4bl385a5qtcN6kPs7g)

---

## License

[![CC BY-SA 4.0](https://img.shields.io/badge/License-CC%20BY--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-sa/4.0/)

This project is licensed under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
Robert's original content retains its original licenses (CC BY 4.0 for 3D models, YouTube standard for videos).
See [LICENSE](LICENSE) for details.

---

*Built with love by fans, for fans.*
