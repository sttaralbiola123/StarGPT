import os
import re
import json
import asyncio
import logging
import threading
import sqlite3
from typing import Optional

from flask import Flask

import discord
from discord.ext import commands
from discord import app_commands

import google.generativeai as genai

# =========================================================
# CONFIG
# =========================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("StarGPT")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not GEMINI_API_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY")

genai.configure(api_key=GEMINI_API_KEY)

MODEL_MAP = {
    "fast": "gemini-2.5-flash",
    "thinking": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
}

DEFAULT_MODEL_KEY = "fast"
DEFAULT_MODE_TEXT = "normal"

# =========================================================
# FLASK KEEP-ALIVE
# =========================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT Online", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=run_flask, daemon=True).start()

# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.moderation = True

class StarGPT(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        logger.info("Slash commands synced.")

bot = StarGPT()

# =========================================================
# SQLITE
# =========================================================

db = sqlite3.connect("stargpt.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db_lock = threading.Lock()

def db_execute(query: str, params: tuple = (), commit: bool = True):
    with db_lock:
        cur = db.execute(query, params)
        if commit:
            db.commit()
        return cur

def db_fetchone(query: str, params: tuple = ()):
    with db_lock:
        cur = db.execute(query, params)
        return cur.fetchone()

def db_fetchall(query: str, params: tuple = ()):
    with db_lock:
        cur = db.execute(query, params)
        return cur.fetchall()

def init_db():
    db_execute("""
    CREATE TABLE IF NOT EXISTS memory (
        user_id TEXT,
        message TEXT,
        response TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    db_execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        user_id TEXT PRIMARY KEY,
        model_key TEXT NOT NULL DEFAULT 'fast',
        mode_text TEXT NOT NULL DEFAULT 'normal'
    )
    """)

    db_execute("""
    CREATE TABLE IF NOT EXISTS punishments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        channel_id TEXT,
        action TEXT NOT NULL,
        reason TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    db_execute("""
    CREATE TABLE IF NOT EXISTS appeals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        guild_id TEXT NOT NULL,
        punishment_action TEXT NOT NULL,
        reason TEXT NOT NULL,
        ai_result TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

init_db()

# =========================================================
# USER SETTINGS
# =========================================================

def ensure_user_settings(user_id: int):
    db_execute(
        "INSERT OR IGNORE INTO user_settings (user_id, model_key, mode_text) VALUES (?, ?, ?)",
        (str(user_id), DEFAULT_MODEL_KEY, DEFAULT_MODE_TEXT)
    )

def get_user_settings(user_id: int):
    ensure_user_settings(user_id)
    row = db_fetchone(
        "SELECT model_key, mode_text FROM user_settings WHERE user_id=?",
        (str(user_id),)
    )
    return {
        "model_key": row["model_key"] if row else DEFAULT_MODEL_KEY,
        "mode_text": row["mode_text"] if row else DEFAULT_MODE_TEXT,
    }

def set_user_model(user_id: int, model_key: str):
    ensure_user_settings(user_id)
    db_execute(
        "UPDATE user_settings SET model_key=? WHERE user_id=?",
        (model_key, str(user_id))
    )

def set_user_mode(user_id: int, mode_text: str):
    ensure_user_settings(user_id)
    db_execute(
        "UPDATE user_settings SET mode_text=? WHERE user_id=?",
        (mode_text, str(user_id))
    )

# =========================================================
# MEMORY
# =========================================================

def save_memory(user_id: int, message: str, response: str):
    db_execute(
        "INSERT INTO memory (user_id, message, response) VALUES (?, ?, ?)",
        (str(user_id), message, response)
    )

def get_memory(user_id: int):
    rows = db_fetchall(
        """
        SELECT message, response
        FROM memory
        WHERE user_id=?
        ORDER BY rowid DESC
        LIMIT 5
        """,
        (str(user_id),)
    )
    return rows

def clear_memory(user_id: int):
    db_execute(
        "DELETE FROM memory WHERE user_id=?",
        (str(user_id),)
    )

# =========================================================
# PUNISHMENTS
# =========================================================

def save_punishment(
    user_id: int,
    guild_id: int,
    channel_id: Optional[int],
    action: str,
    reason: str
):
    db_execute(
        """
        INSERT INTO punishments (user_id, guild_id, channel_id, action, reason, active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (
            str(user_id),
            str(guild_id),
            str(channel_id) if channel_id else None,
            action,
            reason
        )
    )

def get_active_punishment(user_id: int, guild_id: int):
    return db_fetchone(
        """
        SELECT *
        FROM punishments
        WHERE user_id=? AND guild_id=? AND active=1
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(user_id), str(guild_id))
    )

def resolve_punishment(user_id: int, guild_id: int):
    db_execute(
        """
        UPDATE punishments
        SET active=0
        WHERE user_id=? AND guild_id=? AND active=1
        """,
        (str(user_id), str(guild_id))
    )

# =========================================================
# JSON HELPERS
# =========================================================

def extract_json(text: str):
    if not text:
        return None

    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None

    try:
        return json.loads(match.group(0))
    except Exception:
        return None

# =========================================================
# MODE PARSER
# =========================================================

def interpret_mode(mode_text: str):
    t = mode_text.lower().strip()

    if any(x in t for x in ["anime", "kawaii", "waifu"]):
        return "expressive anime-style tone, playful and friendly"
    if any(x in t for x in ["strict", "guardian", "admin", "moderator", "no mercy"]):
        return "strict, formal, no-nonsense tone"
    if any(x in t for x in ["sad", "lonely", "soft", "gentle"]):
        return "empathetic, calm, supportive tone"
    if any(x in t for x in ["funny", "meme", "joke", "clown"]):
        return "funny, light, comedic tone"
    if any(x in t for x in ["hacker", "elite", "boss", "pro"]):
        return "confident, precise, high-skill tone"

    return mode_text

# =========================================================
# GEMINI MODEL BUILDERS
# =========================================================

def build_chat_model(model_key: str):
    model_name = MODEL_MAP.get(model_key, MODEL_MAP[DEFAULT_MODEL_KEY])

    system_instruction = (
        "You are StarGPT, a Discord AI assistant. "
        "Be helpful, natural, and concise unless the user asks for detail."
    )

    if model_key == "thinking":
        system_instruction += " Think carefully and give a deeper answer."
    elif model_key == "pro":
        system_instruction += " Be precise, structured, and thorough."

    return genai.GenerativeModel(
        model_name=model_name,
        system_instruction=system_instruction
    )

def build_moderation_model():
    return genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=(
            "You are an AI moderation system for Discord.\n"
            "You must inspect the message and decide whether it breaks rules.\n"
            "Return JSON ONLY with exactly this shape:\n"
            '{"flagged": true/false, "action": "none/warn/delete/kick/ban", "reason": "short reason"}\n\n'
            "Important:\n"
            "- If the user's mode text sounds strict, guardian, admin, or no mercy, you may be stricter.\n"
            "- If the user's mode text sounds chill, anime, or playful, you may be slightly more lenient.\n"
            "- Do not allow clear hate, scams, spam, sexual content, or threats.\n"
            "- If unsure, choose false.\n"
        )
    )

def build_appeal_model():
    return genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=(
            "You are a Discord appeal reviewer.\n"
            "Review the user's explanation and decide if the punishment should be reversed.\n"
            "Return JSON ONLY with exactly this shape:\n"
            '{"approve": true/false, "reason": "short reason"}\n\n'
            "Approve only if the appeal sounds sincere, reasonable, and safe."
        )
    )

# =========================================================
# ASYNC GEMINI CALLS
# =========================================================

async def generate_text(model, prompt: str, mime_type: Optional[str] = None):
    def _call():
        if mime_type:
            return model.generate_content(
                prompt,
                generation_config={"response_mime_type": mime_type}
            )
        return model.generate_content(prompt)

    return await asyncio.to_thread(_call)

# =========================================================
# RESPONSE SENDING
# =========================================================

async def send_long_message(channel: discord.abc.Messageable, text: str, reply_to: Optional[discord.Message] = None):
    text = text or " "

    chunks = [text[i:i + 1900] for i in range(0, len(text), 1900)]
    if not chunks:
        chunks = [" "]

    first = chunks[0]
    if reply_to:
        await reply_to.reply(first, mention_author=False)
    else:
        await channel.send(first)

    for chunk in chunks[1:]:
        await channel.send(chunk)

# =========================================================
# MODERATION ENGINE
# =========================================================

async def moderate_message(message: discord.Message, mode_text: str):
    model = build_moderation_model()
    prompt = f"""
User mode text:
{mode_text}

Message to analyze:
{message.content}
"""
    res = await generate_text(model, prompt, mime_type="application/json")
    data = extract_json(getattr(res, "text", "") or "")
    if not data:
        return {"flagged": False, "action": "none", "reason": ""}

    flagged = bool(data.get("flagged", False))
    action = str(data.get("action", "none")).lower().strip()
    reason = str(data.get("reason", "")).strip()

    if action not in {"none", "warn", "delete", "kick", "ban"}:
        action = "none"

    return {
        "flagged": flagged,
        "action": action,
        "reason": reason or "Rule violation"
    }

# =========================================================
# HELPERS
# =========================================================

def get_invite_channel(guild: discord.Guild, preferred_channel_id: Optional[int]):
    candidates = []

    if preferred_channel_id:
        ch = guild.get_channel(preferred_channel_id)
        if isinstance(ch, discord.TextChannel):
            candidates.append(ch)

    if guild.system_channel and guild.system_channel not in candidates:
        candidates.append(guild.system_channel)

    for ch in guild.text_channels:
        if ch not in candidates:
            candidates.append(ch)

    me = guild.me or guild.get_member(bot.user.id) if bot.user else None
    if me is None:
        return None

    for ch in candidates:
        perms = ch.permissions_for(me)
        if perms.create_instant_invite:
            return ch

    return None

async def reply_simple(interaction: discord.Interaction, content: str, ephemeral: bool = True):
    if interaction.guild and ephemeral:
        await interaction.response.send_message(content, ephemeral=True)
    else:
        await interaction.response.send_message(content)

# =========================================================
# READY
# =========================================================

@bot.event
async def on_ready():
    logger.info("Logged in as %s", bot.user)
    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="the server | StarGPT"
            )
        )
    except Exception:
        pass

# =========================================================
# MESSAGE EVENT
# =========================================================

@bot.event
async def on_message(message: discord.Message):

    if message.author.bot:
        return

    if not message.guild:
        return

    if message.author.guild_permissions.manage_messages or message.author.guild_permissions.administrator:
        await bot.process_commands(message)
        return

    try:
        settings = get_user_settings(message.author.id)
        mode_text = settings["mode_text"]

        if message.content:
            verdict = await moderate_message(message, mode_text)

            if verdict["flagged"]:
                action = verdict["action"]
                reason = verdict["reason"]

                try:
                    await message.delete()
                except Exception:
                    pass

                if action == "warn":
                    await message.channel.send(
                        f"⚠️ {message.author.mention} warning: {reason}",
                        delete_after=10
                    )
                    return

                if action in {"kick", "ban"}:
                    if action == "ban":
                        try:
                            await message.guild.ban(
                                message.author,
                                reason=reason,
                                delete_message_days=0
                            )
                        except TypeError:
                            await message.guild.ban(message.author, reason=reason)
                        except Exception as e:
                            logger.error("Ban failed: %s", e)

                    elif action == "kick":
                        try:
                            await message.guild.kick(message.author, reason=reason)
                        except Exception as e:
                            logger.error("Kick failed: %s", e)

                    save_punishment(
                        user_id=message.author.id,
                        guild_id=message.guild.id,
                        channel_id=message.channel.id,
                        action=action,
                        reason=reason
                    )

                    await message.channel.send(
                        f"🚨 AutoMod {action.upper()} for {message.author.mention}\nReason: {reason}\nUse `/appeal` to review.",
                        delete_after=15
                    )
                    return

                await message.channel.send(
                    f"🗑️ Message removed from {message.author.mention}.\nReason: {reason}",
                    delete_after=10
                )
                return

        if bot.user and bot.user.mentioned_in(message):
            clean = message.content
            clean = clean.replace(f"<@{bot.user.id}>", "")
            clean = clean.replace(f"<@!{bot.user.id}>", "")
            clean = clean.strip()

            if not clean and not message.attachments:
                await message.reply("Hi! I am StarGPT 🤖", mention_author=False)
                return

            async with message.channel.typing():
                model_key = settings["model_key"]
                mode_text = settings["mode_text"]
                mode_style = interpret_mode(mode_text)

                memory_rows = get_memory(message.author.id)
                memory_text = ""
                for row in memory_rows:
                    memory_text += f"User: {row['message']}\nAI: {row['response']}\n\n"

                prompt = f"""
User mode text:
{mode_text}

Interpreted style:
{mode_style}

Recent memory:
{memory_text}

User message:
{clean}
"""

                model = build_chat_model(model_key)
                res = await generate_text(model, prompt)
                response_text = (getattr(res, "text", "") or "").strip() or "I couldn't generate a reply."

                save_memory(message.author.id, clean, response_text)
                await send_long_message(message.channel, response_text, reply_to=message)
                return

    except Exception as e:
        logger.error("on_message error: %s", e)
        try:
            await message.channel.send("⚠️ StarGPT encountered an error.")
        except Exception:
            pass

    await bot.process_commands(message)

# =========================================================
# /MODEL
# =========================================================

@bot.tree.command(name="model", description="Set your AI model")
@app_commands.describe(model_type="fast, thinking, or pro")
async def model_cmd(interaction: discord.Interaction, model_type: str):
    mt = model_type.lower().strip()

    if mt not in MODEL_MAP:
        await reply_simple(
            interaction,
            "❌ Invalid model. Use: fast, thinking, pro",
            ephemeral=True
        )
        return

    set_user_model(interaction.user.id, mt)
    await reply_simple(
        interaction,
        f"✅ Model set to **{mt}**",
        ephemeral=True
    )

# =========================================================
# /MODE
# =========================================================

@bot.tree.command(name="mode", description="Set your AI mood/personality using any text")
@app_commands.describe(text="Type anything you want the AI to feel like")
async def mode_cmd(interaction: discord.Interaction, text: str):
    text = text.strip()
    if not text:
        await reply_simple(
            interaction,
            "❌ Please type a mode description.",
            ephemeral=True
        )
        return

    set_user_mode(interaction.user.id, text)
    await reply_simple(
        interaction,
        f"🎭 Mode set to: **{text}**",
        ephemeral=True
    )

# =========================================================
# /CLEAR
# =========================================================

@bot.tree.command(name="clear", description="Clear your AI memory")
async def clear_cmd(interaction: discord.Interaction):
    clear_memory(interaction.user.id)
    await reply_simple(
        interaction,
        "🧹 Your AI memory has been cleared.",
        ephemeral=True
    )

# =========================================================
# /APPEAL
# ==
