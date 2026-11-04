"""Copy this file to nexa_config.py and edit it to configure Nexa locally.

Configuration never appears in the app. nexa_config.py is excluded from Git
and shared packages; environment variables can override its model and keys.
"""

LOCAL_MODEL = "gemma3:1b"
OLLAMA_URL = "http://127.0.0.1:11434"

GROQ_API_KEY = ""
GEMINI_API_KEY = ""
CLAUDE_API_KEY = ""

# First available provider wins; failures fall through to the next, then local AI.
ONLINE_BRAIN_ORDER = ("groq", "gemini", "claude")
