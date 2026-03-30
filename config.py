"""
config.py — Load settings from .env file
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Paths
BASE_DIR = Path(__file__).parent
ARCHIVE_PATH = Path(os.getenv("ARCHIVE_PATH", r"C:\Users\Angelus\My Drive\IA\Rob"))

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")

# LLM backends
LLM_PRIMARY = os.getenv("LLM_PRIMARY", "mistral")
LLM_FALLBACK = os.getenv("LLM_FALLBACK", "groq")

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b")

# LLM settings
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "600"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.7"))

# Rate limits
USER_COOLDOWN_SECONDS = int(os.getenv("USER_COOLDOWN_SECONDS", "10"))
LLM_MAX_REQUESTS_PER_SECOND = float(os.getenv("LLM_MAX_REQUESTS_PER_SECOND", "0.9"))

# RAG settings
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "8"))
RAG_MAX_VIDEOS = int(os.getenv("RAG_MAX_VIDEOS", "5"))
RAG_MIN_RELEVANCE = float(os.getenv("RAG_MIN_RELEVANCE", "0.3"))
