"""
Configuration & constants for Molty Royale AI Agent.
All env vars loaded here. Never hardcode secrets.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ── Skill / API version ──────────────────────────────────────────────
SKILL_VERSION = "latest"

# ── URLs ──────────────────────────────────────────────────────────────
API_BASE = "https://cdn.moltyroyale.com/api"
WS_URL = "wss://cdn.moltyroyale.com/ws/agent"

# ── Chain config (CROSS Mainnet) ──────────────────────────────────────
CROSS_CHAIN_ID = 612055
CROSS_RPC = "https://mainnet.crosstoken.io:22001"

# ── Contract addresses ────────────────────────────────────────────────
IDENTITY_REGISTRY = "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
WALLET_FACTORY = "0x378De49F47817D3dF10393851A587e5C2C58EF7C"
WALLET_FACTORY_LEGACY = "0x0713665E4D19fD16e1F09AD77526CC343c6F0223"
MOLTZ_TOKEN = "0xdb99a97d607c5c5831263707E7b746312406ba7E"
ARENA_PAID = "0x8f705417C2a11446e93f94cbe84F476572EE90Ed"
ARENA_FREE = "0xAbC98bBe54e5bc495D97E6A9c51eEf14fd34e77D"
REWARD_VAULT = "0x046a1C632f7e21C215CaF11e1176861567FcB8EE"
FORGE_ROUTER = "0x7aF414e4d373bb332f47769c8d28A446A0C1a1E8"
WCROSS = "0xDdF8AaA3927b8Fd5684dc2edcc7287EcB0A2122d"
REPUTATION_REGISTRY = "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63"

# ── Economy constants (from economy.md) ───────────────────────────────
PAID_ENTRY_FEE_MOLTZ = 500
PAID_ENTRY_FEE_SMOLTZ = 500
FREE_ROOM_POOL = 1000
GUARDIAN_KILL_POOL_SHARE = 0.60  # 60%

# ── Rate limits ───────────────────────────────────────────────────────
REST_RATE_LIMIT = 300   # calls/min per IP
WS_RATE_LIMIT = 120     # messages/min per connection
COOLDOWN_DURATION = 60  # seconds

# ── Credential paths ─────────────────────────────────────────────────
DEV_AGENT_DIR = Path("dev-agent")
CREDENTIALS_FILE = DEV_AGENT_DIR / "credentials.json"
OWNER_INTAKE_FILE = DEV_AGENT_DIR / "owner-intake.json"
AGENT_WALLET_FILE = DEV_AGENT_DIR / "agent-wallet.json"
OWNER_WALLET_FILE = DEV_AGENT_DIR / "owner-wallet.json"
MEMORY_DIR = Path.home() / ".molty-royale"
MEMORY_FILE = MEMORY_DIR / "molty-royale-context.json"

# ── Environment variables ─────────────────────────────────────────────
AGENT_NAME = os.getenv("AGENT_NAME", "")
ADVANCED_MODE = os.getenv("ADVANCED_MODE", "true").lower() == "true"
ROOM_MODE = os.getenv("ROOM_MODE", "free")  # free | auto | paid
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
API_KEY = os.getenv("API_KEY", "")
AGENT_PRIVATE_KEY = os.getenv("AGENT_PRIVATE_KEY", "")
AGENT_WALLET_ADDRESS = os.getenv("AGENT_WALLET_ADDRESS", "")
OWNER_EOA = os.getenv("OWNER_EOA", "")
OWNER_PRIVATE_KEY = os.getenv("OWNER_PRIVATE_KEY", "")

# ── First-Run Intake answers (setup.md lines 29-39) ──────────────────
# These replace the interactive yes/no prompts for Railway/Docker.
# All default to "yes/auto" so zero-config deployment works.
AUTO_WHITELIST = os.getenv("AUTO_WHITELIST", "true").lower() == "true"        # Q4: auto-check + approve
AUTO_SC_WALLET = os.getenv("AUTO_SC_WALLET", "true").lower() == "true"       # Q6: auto-create SC wallet
ENABLE_MEMORY = os.getenv("ENABLE_MEMORY", "true").lower() == "true"         # Q7: cross-game learning
ENABLE_AGENT_TOKEN = os.getenv("ENABLE_AGENT_TOKEN", "false").lower() == "true"  # Q8: agent token
AUTO_IDENTITY = os.getenv("AUTO_IDENTITY", "true").lower() == "true"         # Q9: ERC-8004 auto-register

