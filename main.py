import os
import re
import time
import asyncio
import datetime
import sqlite3
from threading import Thread
from collections import defaultdict

import discord
import aiosqlite
from flask import Flask
from groq import Groq
from discord.ext import commands
from discord import app_commands

# ==================================================
# CONFIG
# ==================================================

DB_PATH = "stargpt.db"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing")

groq_client = Groq(api_key=GROQ_API_KEY)

# ==================================================
# WEB SERVER
# ==================================================

app = Flask(__name__)

@app.route("/")
def home():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM ai_channels")
        channel_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM memory")
        memory_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM profiles")
        profile_count = cur.fetchone()[0]

        conn.close()
    except:
        channel_count = 0
        memory_count = 0
        profile_count = 0

    return f"""
    <html>
    <head>
        <title>StarGPT Online</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background: #0f172a;
                color: white;
                padding: 40px;
            }}
            .card {{
                background: #1e293b;
                padding: 24px;
                border-radius: 16px;
                max-width: 520px;
            }}
            h1 {{ margin-top: 0; }}
            .muted {{ color: #cbd5e1; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>StarGPT Online</h1>
            <p class="muted">Bot is running.</p>
            <p>Configured AI channels: {channel_count}</p>
            <p>Saved memory rows: {memory_count}</p>
            <p>Saved profiles: {profile_count}</p>
        </div>
    </body>
    </html>
    """

def run_web():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

Thread(target=run_web, daemon=True).start()

# ==================================================
# DISCORD BOT
# ==================================================

intents = discord.Intents.all()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==================================================
# CACHE
# ==================================================

warning_counts = defaultdict(int)
join_tracker = defaultdict(list)
active_chats = set()

setup_channels = defaultdict(set)   # guild_id -> set(channel_id)
guild_styles = {}                   # guild_id -> personality

VALID_PROFILE_KEYS = {"nickname", "language", "favorite_game"}
VALID_STYLES = {"friendly", "serious", "funny", "anime"}

# ==================================================
# DATABASE
# ==================================================

async def setup_database():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            user_id INTEGER,
            guild_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id INTEGER PRIMARY KEY,
            nickname TEXT,
            language TEXT,
            favorite_game TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_channels (
            guild_id INTEGER,
            channel_id INTEGER,
            PRIMARY KEY (guild_id, channel_id)
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            personality TEXT DEFAULT 'friendly'
        )
        """)

        await db.commit()

async def load_cache():
    setup_channels.clear()
    guild_styles.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, channel_id FROM ai_channels") as cur:
            async for guild_id, channel_id in cur:
                setup_channels[int(guild_id)].add(int(channel_id))

        async with db.execute("SELECT guild_id, personality FROM guild_settings") as cur:
            async for guild_id, personality in cur:
                guild_styles[int(guild_id)] = personality or "friendly"

# ==================================================
# AI HELPERS
# ==================================================

async def groq_chat(messages, temperature=0.7, max_tokens=400):
    def run():
        return groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    return await asyncio.wait_for(asyncio.to_thread(run), timeout=40)

async def generate_ai(messages):
    res = await groq_chat(messages, temperature=0.7, max_tokens=400)
    return res.choices[0].message.content.strip()

async def ai_moderation(text):
    system = """
Classify the message into ONLY ONE label:

SAFE
SPAM
SCAM
TOXIC
HARASSMENT
EXTREME

Return only the label.
"""
    try:
        res = await groq_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=20,
        )
        raw = res.choices[0].message.content.strip().upper()
        for label in ["SAFE", "SPAM", "SCAM", "TOXIC", "HARASSMENT", "EXTREME"]:
            if raw == label or raw.startswith(label):
                return label
        return "SAFE"
    except:
        return "SAFE"

async def ai_appeal_review(reason):
    system = """
Reply ONLY with:
APPROVE
DENY
"""
    try:
        res = await groq_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": reason},
            ],
            temperature=0,
            max_tokens=10,
        )
        raw = res.choices[0].message.content.strip().upper()
        return "APPROVE" if "APPROVE" in raw else "DENY"
    except:
        return "DENY"

# ==================================================
# MEMORY
# ==================================================

async def save_memory(user_id, guild_id, role, content):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO memory (user_id, guild_id, role, content) VALUES (?, ?, ?, ?)",
            (user_id, guild_id, role, content),
        )
        await db.commit()

        # keep only recent 60 rows per user/guild to prevent bloat
        await db.execute("""
        DELETE FROM memory
        WHERE rowid NOT IN (
            SELECT rowid
            FROM memory
            WHERE user_id=? AND guild_id=?
            ORDER BY rowid DESC
            LIMIT 60
        )
        """, (user_id, guild_id))
        await db.commit()

async def load_memory(user_id, guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
        SELECT role, content
        FROM memory
        WHERE user_id=? AND guild_id=?
        ORDER BY rowid DESC
        LIMIT 8
        """, (user_id, guild_id))
        rows = await cur.fetchall()

    rows.reverse()
    return [{"role": role, "content": content} for role, content in rows]

async def load_profile_text(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
        SELECT nickname, language, favorite_game
        FROM profiles
        WHERE user_id=?
        """, (user_id,))
        row = await cur.fetchone()

    if not row:
        return ""

    nickname, language, favorite_game = row
    parts = []
    if nickname:
        parts.append(f"Nickname: {nickname}")
    if language:
        parts.append(f"Language: {language}")
    if favorite_game:
        parts.append(f"Favorite Game: {favorite_game}")

    return "\n".join(parts)

# ==================================================
# RESPONSE SENDER
# ==================================================

async def send_long_message(channel, text, reply_to=None):
    if not text:
        return

    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    if not chunks:
        chunks = ["(empty response)"]

    first = True
    for chunk in chunks:
        if first and reply_to is not None:
            await reply_to.reply(chunk, mention_author=False)
            first = False
        else:
            await channel.send(chunk)

# ==================================================
# PUNISHMENT
# ==================================================

def timeout_minutes_for(category):
    return {
        "SPAM": 5,
        "TOXIC": 10,
        "HARASSMENT": 30,
        "SCAM": 60,
        "EXTREME": 1440,
    }.get(category, 10)

async def punish(member, category):
    warnings_key = (member.guild.id, member.id)
    warning_counts[warnings_key] += 1
    warnings = warning_counts[warnings_key]
    minutes = timeout_minutes_for(category)

    try:
        await member.timeout(
            datetime.timedelta(minutes=minutes),
            reason=f"StarGPT moderation: {category}"
        )
    except:
        pass

    try:
        await member.send(
            f"Moderation Action\n\nCategory: {category}\nWarnings: {warnings}/3\nTimeout: {minutes} minutes"
        )
    except:
        pass

# ==================================================
# COMMANDS
# ==================================================

@bot.tree.command(name="setup")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO guild_settings (guild_id, personality) VALUES (?, 'friendly')",
            (interaction.guild.id,)
        )
        await db.execute(
            "INSERT OR IGNORE INTO ai_channels (guild_id, channel_id) VALUES (?, ?)",
            (interaction.guild.id, channel.id)
        )
        await db.commit()

    setup_channels[interaction.guild.id].add(channel.id)
    await interaction.response.send_message(f"AI channel added: {channel.mention}")

@bot.tree.command(name="removechannel")
@app_commands.checks.has_permissions(administrator=True)
async def removechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM ai_channels WHERE guild_id=? AND channel_id=?",
            (interaction.guild.id, channel.id)
        )
        await db.commit()

    if interaction.guild.id in setup_channels:
        setup_channels[interaction.guild.id].discard(channel.id)

    await interaction.response.send_message(f"AI channel removed: {channel.mention}")

@bot.tree.command(name="personality")
@app_commands.checks.has_permissions(administrator=True)
async def personality(interaction: discord.Interaction, style: str):
    style = style.lower().strip()
    if style not in VALID_STYLES:
        await interaction.response.send_message(
            f"Invalid style. Use one of: {', '.join(sorted(VALID_STYLES))}",
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT INTO guild_settings (guild_id, personality)
        VALUES (?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET personality=excluded.personality
        """, (interaction.guild.id, style))
        await db.commit()

    guild_styles[interaction.guild.id] = style
    await interaction.response.send_message(f"Personality set to: {style}")

@bot.tree.command(name="remember")
async def remember(interaction: discord.Interaction, key: str, value: str):
    key = key.lower().strip()

    if key not in VALID_PROFILE_KEYS:
        await interaction.response.send_message(
            f"Invalid key. Use: {', '.join(sorted(VALID_PROFILE_KEYS))}",
            ephemeral=True
        )
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT user_id FROM profiles WHERE user_id=?",
            (interaction.user.id,)
        )
        exists = await cur.fetchone()

        if exists:
            await db.execute(
                f"UPDATE profiles SET {key}=? WHERE user_id=?",
                (value, interaction.user.id)
            )
        else:
            data = {
                "nickname": None,
                "language": None,
                "favorite_game": None,
            }
            data[key] = value
            await db.execute(
                "INSERT INTO profiles (user_id, nickname, language, favorite_game) VALUES (?, ?, ?, ?)",
                (interaction.user.id, data["nickname"], data["language"], data["favorite_game"])
            )

        await db.commit()

    await interaction.response.send_message(f"Saved {key}: {value}")

@bot.tree.command(name="appeal")
async def appeal(interaction: discord.Interaction, reason: str):
    await interaction.response.defer(ephemeral=True)
    decision = await ai_appeal_review(reason)

    if decision == "APPROVE":
        await interaction.followup.send("Appeal approved.")
    else:
        await interaction.followup.send("Appeal denied.")

# ==================================================
# ANTI RAID
# ==================================================

@bot.event
async def on_member_join(member):
    guild_id = member.guild.id
    now = time.time()

    join_tracker[guild_id].append(now)
    recent = [t for t in join_tracker[guild_id] if now - t < 15]
    join_tracker[guild_id] = recent

    if len(recent) >= 10:
        try:
            await member.guild.edit(verification_level=discord.VerificationLevel.high)
        except:
            pass

    suspicious = False
    account_age_days = (discord.utils.utcnow() - member.created_at).days

    if account_age_days < 7:
        suspicious = True
    if member.avatar is None:
        suspicious = True

    if suspicious:
        try:
            await member.timeout(
                datetime.timedelta(minutes=30),
                reason="Suspicious account"
            )
        except:
            pass

# ==================================================
# MESSAGE EVENT
# ==================================================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.guild:
        return

    if not message.content or not message.content.strip():
        return

    channel_ids = setup_channels.get(message.guild.id)
    if not channel_ids:
        return

    if message.channel.id not in channel_ids:
        return

    chat_key = (message.guild.id, message.author.id, message.channel.id)
    if chat_key in active_chats:
        return

    active_chats.add(chat_key)

    try:
        async with message.channel.typing():
            mod = await ai_moderation(message.content)

            if mod != "SAFE":
                try:
                    await message.delete()
                except:
                    pass

                await punish(message.author, mod)
                return

            personality_style = guild_styles.get(message.guild.id, "friendly")
            profile_text = await load_profile_text(message.author.id)
            memory = await load_memory(message.author.id, message.guild.id)

            style_prompt = {
                "friendly": "Be warm, helpful, and clear.",
                "serious": "Be concise, direct, and professional.",
                "funny": "Be playful, witty, and friendly, but still helpful.",
                "anime": "Be energetic, expressive, and friendly like an anime-style assistant."
            }.get(personality_style, "Be warm, helpful, and clear.")

            system_prompt = f"""
You are StarGPT, a Discord assistant.

Style:
{style_prompt}

Rules:
- Keep replies short unless the user asks for detail.
- Be natural in Discord chat.
- Do not mention that you are an AI unless asked.
"""

            if profile_text:
                system_prompt += f"\n\nUser profile:\n{profile_text}\n"

            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(memory)
            messages.append({"role": "user", "content": message.content})

            response = await generate_ai(messages)

            await save_memory(message.author.id, message.guild.id, "user", message.content)
            await save_memory(message.author.id, message.guild.id, "assistant", response)

            await send_long_message(message.channel, response, reply_to=message)

    finally:
        active_chats.discard(chat_key)

# ==================================================
# READY
# ==================================================

_ready_done = False

@bot.event
async def on_ready():
    global _ready_done

    await setup_database()
    await load_cache()

    if not _ready_done:
        try:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} commands")
        except Exception as e:
            print(f"Command sync error: {e}")
        _ready_done = True

    print(f"Logged in as {bot.user}")

# ==================================================
# RUN
# ==================================================

bot.run(DISCORD_TOKEN)
