import os
import re
import time
import base64
import asyncio
import datetime
import sqlite3
import aiohttp
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

DB_PATH      = "stargpt.db"
MAX_WARNINGS = 3

DISCORD_TOKEN = os.getenv("DISCORD")
GROQ_API_KEY  = os.getenv("GROQ")

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")
if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing")

groq_client = Groq(api_key=GROQ_API_KEY)

VALID_PROFILE_KEYS = {"nickname", "language", "favorite_game", "age", "bio"}
VALID_STYLES       = {"friendly", "serious", "funny", "anime", "sarcastic"}

# StarGPT core personality
STARGPT_SYSTEM = """
You are StarGPT — a sharp, self-aware AI assistant living inside a Discord server.
You have a real personality: curious, witty, occasionally playful, but always genuinely helpful.
You do NOT act like a bot. You talk like a smart friend who happens to know everything.

Rules:
- Never say you are an AI unless the user sincerely asks.
- Never start a reply with "Sure!", "Of course!", "Certainly!" or similar filler phrases.
- Keep replies concise unless the user clearly wants detail.
- Use casual Discord formatting (bold, code blocks) when it helps clarity.
- If someone sends an image, describe and analyze it naturally as part of the conversation.
- Match the user's energy: chill when they're chill, detailed when they ask for depth.
- Always reply in English unless the user's profile language says otherwise.
- You remember context from earlier in the conversation.
- Never be preachy or add unsolicited moral commentary.
"""

# ==================================================
# WEB SERVER
# ==================================================

app = Flask(__name__)

@app.route("/")
def home():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ai_channels"); channels = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM memory");     memories  = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM profiles");   profiles  = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM warnings");   warns     = cur.fetchone()[0]
        conn.close()
    except:
        channels = memories = profiles = warns = 0

    return f"""
    <html><head><title>StarGPT</title>
    <style>
        body {{ font-family: 'Segoe UI', sans-serif; background: #0f172a; color: #f1f5f9; padding: 48px; }}
        .card {{ background: #1e293b; border: 1px solid #334155; padding: 32px; border-radius: 20px; max-width: 540px; }}
        h1 {{ margin: 0 0 8px; font-size: 28px; }}
        .badge {{ display:inline-block; background:#22c55e; color:#fff; border-radius:6px; padding:2px 10px; font-size:13px; margin-bottom:20px; }}
        .stat {{ display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid #334155; }}
        .stat:last-child {{ border-bottom:none; }}
        .val {{ font-weight:600; color:#38bdf8; }}
    </style></head><body>
    <div class="card">
        <h1>⭐ StarGPT</h1>
        <div class="badge">● Online</div>
        <div class="stat"><span>AI Channels</span><span class="val">{channels}</span></div>
        <div class="stat"><span>Memory Rows</span><span class="val">{memories}</span></div>
        <div class="stat"><span>User Profiles</span><span class="val">{profiles}</span></div>
        <div class="stat"><span>Total Warnings</span><span class="val">{warns}</span></div>
    </div></body></html>
    """

Thread(
    target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))),
    daemon=True
).start()

# ==================================================
# BOT SETUP
# ==================================================

intents = discord.Intents.all()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==================================================
# IN-MEMORY CACHE
# ==================================================

setup_channels   = defaultdict(set)   # guild_id -> set of channel_ids
guild_styles     = {}                  # guild_id -> personality string
mod_log_channels = {}                  # guild_id -> channel_id
active_chats     = set()               # (guild_id, user_id, channel_id) concurrency lock
join_tracker     = defaultdict(list)   # guild_id -> list of join timestamps
spam_tracker     = defaultdict(list)   # (guild_id, user_id) -> message timestamps
message_tracker  = defaultdict(list)   # (guild_id, user_id) -> recent message contents

# ==================================================
# AUTOMOD CONSTANTS
# ==================================================

SPAM_THRESHOLD        = 5    # messages within SPAM_WINDOW seconds = spam
SPAM_WINDOW           = 6    # seconds
DUP_THRESHOLD         = 3    # same message N times in a row = spam
MASS_MENTION_THRESHOLD = 5   # mentions in one message = harassment

INVITE_PATTERN = re.compile(r"discord(?:\.gg|app\.com/invite)/\S+", re.IGNORECASE)
ZALGO_PATTERN  = re.compile(r"[\u0300-\u036f\u0489]{4,}")

TIMEOUT_DURATIONS = {
    "SPAM":       5,
    "SCAM":       60,
    "TOXIC":      15,
    "HARASSMENT": 30,
    "NSFW":       120,
    "THREAT":     1440,
    "EXTREME":    1440,
    "GORE":       1440,
    "HATE_SYMBOL":720,
    "ILLEGAL":    1440,
}

# ==================================================
# DATABASE
# ==================================================

async def setup_database():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                user_id INTEGER, guild_id INTEGER, role TEXT, content TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY,
                nickname TEXT, language TEXT, favorite_game TEXT, age TEXT, bio TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ai_channels (
                guild_id INTEGER, channel_id INTEGER,
                PRIMARY KEY (guild_id, channel_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id           INTEGER PRIMARY KEY,
                personality        TEXT    DEFAULT 'friendly',
                mod_log_channel_id INTEGER DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, guild_id INTEGER, reason TEXT,
                moderator_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS automod_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER, user_id INTEGER,
                action TEXT, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


async def load_cache():
    setup_channels.clear()
    guild_styles.clear()
    mod_log_channels.clear()

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT guild_id, channel_id FROM ai_channels") as cur:
            async for gid, cid in cur:
                setup_channels[int(gid)].add(int(cid))
        async with db.execute("SELECT guild_id, personality, mod_log_channel_id FROM guild_settings") as cur:
            async for gid, style, log_cid in cur:
                guild_styles[int(gid)] = style or "friendly"
                if log_cid:
                    mod_log_channels[int(gid)] = int(log_cid)

# ==================================================
# GROQ / AI HELPERS
# ==================================================

async def groq_chat(messages, temperature=0.8, max_tokens=600,
                    vision_b64=None, vision_mime="image/png"):
    def run():
        msgs = list(messages)
        if vision_b64:
            last = msgs[-1]
            if last["role"] == "user":
                msgs[-1] = {
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:{vision_mime};base64,{vision_b64}"}},
                        {"type": "text",
                         "text": last["content"] if isinstance(last["content"], str) else "Analyze this image."}
                    ]
                }
        model = "llama-3.2-11b-vision-preview" if vision_b64 else "llama-3.1-8b-instant"
        return groq_client.chat.completions.create(
            model=model, messages=msgs,
            temperature=temperature, max_tokens=max_tokens
        )

    return await asyncio.wait_for(asyncio.to_thread(run), timeout=45)


async def generate_response(messages, image_b64=None, image_mime="image/png") -> str:
    res = await groq_chat(messages, vision_b64=image_b64, vision_mime=image_mime)
    return res.choices[0].message.content.strip()


async def classify_message(text: str) -> str:
    system = (
        "Classify this Discord message into exactly one label: "
        "SAFE, SPAM, SCAM, TOXIC, HARASSMENT, NSFW, THREAT, EXTREME\n"
        "Reply with only the label."
    )
    try:
        res = await groq_chat(
            [{"role": "system", "content": system},
             {"role": "user",   "content": text}],
            temperature=0, max_tokens=15
        )
        raw = res.choices[0].message.content.strip().upper()
        for label in ["SAFE","SPAM","SCAM","TOXIC","HARASSMENT","NSFW","THREAT","EXTREME"]:
            if raw.startswith(label):
                return label
        return "SAFE"
    except:
        return "SAFE"


async def classify_image(image_b64: str, mime: str) -> str:
    system = (
        "You are a content moderation AI. Classify this image into exactly one label: "
        "SAFE, NSFW, GORE, HATE_SYMBOL, SCAM, ILLEGAL\n"
        "Reply with only the label."
    )
    try:
        res = await groq_chat(
            [{"role": "system", "content": system},
             {"role": "user",   "content": "Classify this image."}],
            temperature=0, max_tokens=15,
            vision_b64=image_b64, vision_mime=mime
        )
        raw = res.choices[0].message.content.strip().upper()
        for label in ["SAFE","NSFW","GORE","HATE_SYMBOL","SCAM","ILLEGAL"]:
            if raw.startswith(label):
                return label
        return "SAFE"
    except:
        return "SAFE"


async def review_appeal(reason: str) -> str:
    system = "Review this moderation appeal. Reply with only APPROVE or DENY."
    try:
        res = await groq_chat(
            [{"role": "system", "content": system},
             {"role": "user",   "content": reason}],
            temperature=0, max_tokens=10
        )
        raw = res.choices[0].message.content.strip().upper()
        return "APPROVE" if "APPROVE" in raw else "DENY"
    except:
        return "DENY"

# ==================================================
# RULE-BASED CHECKS (instant, no AI call)
# ==================================================

def rule_based_check(message: discord.Message):
    content = message.content
    key     = (message.guild.id, message.author.id)
    now     = time.time()

    # Rate spam
    spam_tracker[key].append(now)
    spam_tracker[key] = [t for t in spam_tracker[key] if now - t < SPAM_WINDOW]
    if len(spam_tracker[key]) >= SPAM_THRESHOLD:
        return "SPAM"

    # Duplicate spam
    message_tracker[key].append(content.lower().strip())
    message_tracker[key] = message_tracker[key][-DUP_THRESHOLD:]
    if (len(message_tracker[key]) == DUP_THRESHOLD
            and len(set(message_tracker[key])) == 1):
        return "SPAM"

    if INVITE_PATTERN.search(content):
        return "SCAM"

    if len(message.mentions) >= MASS_MENTION_THRESHOLD:
        return "HARASSMENT"

    if ZALGO_PATTERN.search(content):
        return "TOXIC"

    return None

# ==================================================
# MEMORY & PROFILES
# ==================================================

async def save_memory(user_id, guild_id, role, content):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO memory (user_id, guild_id, role, content) VALUES (?,?,?,?)",
            (user_id, guild_id, role, content)
        )
        await db.commit()
        await db.execute("""
            DELETE FROM memory WHERE rowid NOT IN (
                SELECT rowid FROM memory WHERE user_id=? AND guild_id=?
                ORDER BY rowid DESC LIMIT 80
            )
        """, (user_id, guild_id))
        await db.commit()


async def load_memory(user_id, guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT role, content FROM memory
            WHERE user_id=? AND guild_id=?
            ORDER BY rowid DESC LIMIT 12
        """, (user_id, guild_id))
        rows = await cur.fetchall()
    rows.reverse()
    return [{"role": r, "content": c} for r, c in rows]


async def load_profile(user_id) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT nickname, language, favorite_game, age, bio FROM profiles WHERE user_id=?",
            (user_id,)
        )
        row = await cur.fetchone()
    if not row:
        return ""
    keys = ["Nickname", "Language", "Favorite Game", "Age", "Bio"]
    return "\n".join(f"{k}: {v}" for k, v in zip(keys, row) if v)

# ==================================================
# WARNINGS DB
# ==================================================

async def add_warning(user_id, guild_id, reason, moderator_id) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO warnings (user_id, guild_id, reason, moderator_id) VALUES (?,?,?,?)",
            (user_id, guild_id, reason, moderator_id)
        )
        await db.commit()
        cur = await db.execute(
            "SELECT COUNT(*) FROM warnings WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        )
        return (await cur.fetchone())[0]


async def get_warnings(user_id, guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, reason, moderator_id, created_at FROM warnings "
            "WHERE user_id=? AND guild_id=? ORDER BY id",
            (user_id, guild_id)
        )
        return await cur.fetchall()


async def remove_warning(warning_id, guild_id) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "DELETE FROM warnings WHERE id=? AND guild_id=?", (warning_id, guild_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def clear_warnings(user_id, guild_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM warnings WHERE user_id=? AND guild_id=?", (user_id, guild_id))
        await db.commit()

# ==================================================
# MOD LOG
# ==================================================

async def send_mod_log(guild: discord.Guild, embed: discord.Embed):
    cid = mod_log_channels.get(guild.id)
    if not cid:
        return
    ch = guild.get_channel(cid)
    if ch:
        try:
            await ch.send(embed=embed)
        except:
            pass


def make_embed(title: str, color: discord.Color, fields: dict,
               user: discord.Member = None) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.datetime.utcnow())
    if user:
        embed.set_thumbnail(url=user.display_avatar.url)
    for name, value in fields.items():
        embed.add_field(name=name, value=str(value), inline=len(str(value)) < 40)
    return embed

# ==================================================
# AUTOMOD PUNISHMENTS
# ==================================================

async def log_automod(guild_id, user_id, action, reason):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO automod_log (guild_id, user_id, action, reason) VALUES (?,?,?,?)",
            (guild_id, user_id, action, reason)
        )
        await db.commit()


async def auto_ban(member: discord.Member, reason: str):
    try:
        await member.send(f"You have been **banned** from **{member.guild.name}**.\nReason: {reason}")
    except:
        pass
    try:
        await member.ban(reason=reason)
    except:
        return
    await log_automod(member.guild.id, member.id, "BAN", reason)
    embed = make_embed(
        "🔨 Auto-Ban", discord.Color.red(),
        {"User": f"{member.mention} (`{member.id}`)", "Reason": reason},
        user=member
    )
    await send_mod_log(member.guild, embed)


async def punish(member: discord.Member, category: str):
    minutes = TIMEOUT_DURATIONS.get(category, 10)
    total   = await add_warning(member.id, member.guild.id, category, bot.user.id)

    try:
        await member.timeout(datetime.timedelta(minutes=minutes), reason=f"AutoMod: {category}")
    except:
        pass

    try:
        await member.send(
            f"**⚠️ AutoMod Action — {member.guild.name}**\n"
            f"Violation: `{category}`\n"
            f"Timeout: {minutes} minute(s)\n"
            f"Warnings: `{total}/{MAX_WARNINGS}`"
        )
    except:
        pass

    await log_automod(member.guild.id, member.id, "TIMEOUT", category)

    embed = make_embed(
        "🤖 AutoMod Action", discord.Color.orange(),
        {"User": f"{member.mention} (`{member.id}`)",
         "Violation": category,
         "Timeout": f"{minutes} min",
         "Warnings": f"{total}/{MAX_WARNINGS}"},
        user=member
    )
    await send_mod_log(member.guild, embed)

    if total >= MAX_WARNINGS:
        await auto_ban(member, f"Reached {MAX_WARNINGS} warnings. Last: {category}")

# ==================================================
# IMAGE FETCH
# ==================================================

async def fetch_image_b64(url: str) -> tuple:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.read()
            mime = resp.content_type or "image/png"
            return base64.b64encode(data).decode(), mime

# ==================================================
# MESSAGE SENDER
# ==================================================

async def send_chunked(channel, text, reply_to=None):
    if not text:
        return
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)] or ["…"]
    for i, chunk in enumerate(chunks):
        if i == 0 and reply_to:
            await reply_to.reply(chunk, mention_author=False)
        else:
            await channel.send(chunk)

# ==================================================
# SLASH COMMANDS — SETUP
# ==================================================

@bot.tree.command(name="setup", description="Add a channel where StarGPT will chat.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (interaction.guild.id,))
        await db.execute(
            "INSERT OR IGNORE INTO ai_channels (guild_id, channel_id) VALUES (?,?)",
            (interaction.guild.id, channel.id)
        )
        await db.commit()
    setup_channels[interaction.guild.id].add(channel.id)
    await interaction.response.send_message(f"✅ AI channel added: {channel.mention}")


@bot.tree.command(name="removechannel", description="Remove a StarGPT chat channel.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_removechannel(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM ai_channels WHERE guild_id=? AND channel_id=?",
            (interaction.guild.id, channel.id)
        )
        await db.commit()
    setup_channels[interaction.guild.id].discard(channel.id)
    await interaction.response.send_message(f"✅ AI channel removed: {channel.mention}")


@bot.tree.command(name="setmodlog", description="Set the moderation log channel.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_setmodlog(interaction: discord.Interaction, channel: discord.TextChannel):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO guild_settings (guild_id, mod_log_channel_id) VALUES (?,?)
            ON CONFLICT(guild_id) DO UPDATE SET mod_log_channel_id=excluded.mod_log_channel_id
        """, (interaction.guild.id, channel.id))
        await db.commit()
    mod_log_channels[interaction.guild.id] = channel.id
    await interaction.response.send_message(f"✅ Mod log set to: {channel.mention}")


@bot.tree.command(name="personality", description="Set StarGPT's personality style.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_personality(interaction: discord.Interaction, style: str):
    style = style.lower().strip()
    if style not in VALID_STYLES:
        await interaction.response.send_message(
            f"Invalid style. Options: `{'`, `'.join(sorted(VALID_STYLES))}`", ephemeral=True
        )
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO guild_settings (guild_id, personality) VALUES (?,?)
            ON CONFLICT(guild_id) DO UPDATE SET personality=excluded.personality
        """, (interaction.guild.id, style))
        await db.commit()
    guild_styles[interaction.guild.id] = style
    await interaction.response.send_message(f"✅ Personality set to: **{style}**")

# ==================================================
# SLASH COMMANDS — MODERATION
# ==================================================

@bot.tree.command(name="warn", description="Issue a warning to a member.")
@app_commands.checks.has_permissions(moderate_members=True)
async def cmd_warn(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.bot:
        await interaction.response.send_message("Cannot warn bots.", ephemeral=True)
        return

    total = await add_warning(member.id, interaction.guild.id, reason, interaction.user.id)
    try:
        await member.send(
            f"**⚠️ You were warned** in **{interaction.guild.name}**\n"
            f"Reason: {reason}\nWarnings: `{total}/{MAX_WARNINGS}`"
        )
    except:
        pass

    await interaction.response.send_message(
        f"⚠️ {member.mention} warned — `{total}/{MAX_WARNINGS}` warnings.\nReason: {reason}"
    )
    await send_mod_log(interaction.guild, make_embed(
        "⚠️ Warning Issued", discord.Color.yellow(),
        {"User": f"{member.mention} (`{member.id}`)",
         "Moderator": interaction.user.mention,
         "Warnings": f"{total}/{MAX_WARNINGS}",
         "Reason": reason},
        user=member
    ))

    if total >= MAX_WARNINGS:
        await auto_ban(member, f"Reached {MAX_WARNINGS} warnings. Last: {reason}")
        await interaction.followup.send(f"🔨 {member.mention} auto-banned.")


@bot.tree.command(name="warnings", description="View all warnings for a member.")
@app_commands.checks.has_permissions(moderate_members=True)
async def cmd_warnings(interaction: discord.Interaction, member: discord.Member):
    rows = await get_warnings(member.id, interaction.guild.id)
    if not rows:
        await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)
        return
    embed = discord.Embed(title=f"Warnings — {member.display_name}", color=discord.Color.orange())
    embed.set_thumbnail(url=member.display_avatar.url)
    for wid, reason, mod_id, created_at in rows:
        mod = interaction.guild.get_member(mod_id)
        embed.add_field(
            name=f"#{wid} · {created_at[:10]}",
            value=f"{reason} — by {mod.display_name if mod else mod_id}",
            inline=False
        )
    embed.set_footer(text=f"Total: {len(rows)}/{MAX_WARNINGS}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="removewarn", description="Remove a warning by its ID.")
@app_commands.checks.has_permissions(moderate_members=True)
async def cmd_removewarn(interaction: discord.Interaction, warning_id: int):
    deleted = await remove_warning(warning_id, interaction.guild.id)
    if deleted:
        await interaction.response.send_message(f"✅ Warning `#{warning_id}` removed.")
        await send_mod_log(interaction.guild, make_embed(
            "🗑️ Warning Removed", discord.Color.green(),
            {"Warning ID": f"#{warning_id}", "Moderator": interaction.user.mention}
        ))
    else:
        await interaction.response.send_message(f"Warning `#{warning_id}` not found.", ephemeral=True)


@bot.tree.command(name="clearwarnings", description="Clear all warnings for a member.")
@app_commands.checks.has_permissions(administrator=True)
async def cmd_clearwarnings(interaction: discord.Interaction, member: discord.Member):
    await clear_warnings(member.id, interaction.guild.id)
    await interaction.response.send_message(f"✅ All warnings cleared for {member.mention}.")
    await send_mod_log(interaction.guild, make_embed(
        "🧹 Warnings Cleared", discord.Color.green(),
        {"User": member.mention, "Moderator": interaction.user.mention}
    ))


@bot.tree.command(name="kick", description="Kick a member from the server.")
@app_commands.checks.has_permissions(kick_members=True)
async def cmd_kick(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.bot:
        await interaction.response.send_message("Cannot kick bots.", ephemeral=True)
        return
    try:
        await member.send(f"You were **kicked** from **{interaction.guild.name}**.\nReason: {reason}")
    except:
        pass
    try:
        await member.kick(reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"👢 {member.mention} kicked. Reason: {reason}")
    await send_mod_log(interaction.guild, make_embed(
        "👢 Member Kicked", discord.Color.orange(),
        {"User": f"{member.mention} (`{member.id}`)",
         "Moderator": interaction.user.mention, "Reason": reason},
        user=member
    ))


@bot.tree.command(name="ban", description="Ban a member from the server.")
@app_commands.checks.has_permissions(ban_members=True)
async def cmd_ban(interaction: discord.Interaction, member: discord.Member, reason: str = "No reason provided"):
    if member.bot:
        await interaction.response.send_message("Cannot ban bots.", ephemeral=True)
        return
    try:
        await member.send(f"You were **banned** from **{interaction.guild.name}**.\nReason: {reason}")
    except:
        pass
    try:
        await member.ban(reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔨 {member.mention} banned. Reason: {reason}")
    await send_mod_log(interaction.guild, make_embed(
        "🔨 Member Banned", discord.Color.red(),
        {"User": f"{member.mention} (`{member.id}`)",
         "Moderator": interaction.user.mention, "Reason": reason},
        user=member
    ))


@bot.tree.command(name="mute", description="Timeout a member for N minutes.")
@app_commands.checks.has_permissions(moderate_members=True)
async def cmd_mute(interaction: discord.Interaction, member: discord.Member,
                   minutes: int, reason: str = "No reason provided"):
    if member.bot:
        await interaction.response.send_message("Cannot mute bots.", ephemeral=True)
        return
    try:
        await member.timeout(datetime.timedelta(minutes=minutes), reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔇 {member.mention} muted for {minutes} min. Reason: {reason}")
    await send_mod_log(interaction.guild, make_embed(
        "🔇 Member Muted", discord.Color.orange(),
        {"User": f"{member.mention} (`{member.id}`)",
         "Moderator": interaction.user.mention,
         "Duration": f"{minutes} min", "Reason": reason},
        user=member
    ))


@bot.tree.command(name="unmute", description="Remove timeout from a member.")
@app_commands.checks.has_permissions(moderate_members=True)
async def cmd_unmute(interaction: discord.Interaction, member: discord.Member):
    try:
        await member.timeout(None)
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔊 {member.mention} unmuted.")
    await send_mod_log(interaction.guild, make_embed(
        "🔊 Member Unmuted", discord.Color.green(),
        {"User": member.mention, "Moderator": interaction.user.mention}
    ))


@bot.tree.command(name="purge", description="Delete messages from this channel (max 100).")
@app_commands.checks.has_permissions(manage_messages=True)
async def cmd_purge(interaction: discord.Interaction, amount: int):
    if not 1 <= amount <= 100:
        await interaction.response.send_message("Amount must be 1–100.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=amount)
    await interaction.followup.send(f"🗑️ Deleted {len(deleted)} messages.")
    await send_mod_log(interaction.guild, make_embed(
        "🗑️ Purge", discord.Color.blurple(),
        {"Channel": interaction.channel.mention,
         "Moderator": interaction.user.mention,
         "Deleted": len(deleted)}
    ))


@bot.tree.command(name="slowmode", description="Set slowmode delay in seconds (0 = off).")
@app_commands.checks.has_permissions(manage_channels=True)
async def cmd_slowmode(interaction: discord.Interaction, seconds: int):
    try:
        await interaction.channel.edit(slowmode_delay=max(0, seconds))
    except discord.Forbidden:
        await interaction.response.send_message("Missing permissions.", ephemeral=True)
        return
    msg = f"✅ Slowmode set to {seconds}s." if seconds > 0 else "✅ Slowmode disabled."
    await interaction.response.send_message(msg)


@bot.tree.command(name="lock", description="Prevent everyone from sending messages in this channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def cmd_lock(interaction: discord.Interaction):
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = False
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message("🔒 Channel locked.")
    await send_mod_log(interaction.guild, make_embed(
        "🔒 Channel Locked", discord.Color.red(),
        {"Channel": interaction.channel.mention, "Moderator": interaction.user.mention}
    ))


@bot.tree.command(name="unlock", description="Restore message permissions in this channel.")
@app_commands.checks.has_permissions(manage_channels=True)
async def cmd_unlock(interaction: discord.Interaction):
    ow = interaction.channel.overwrites_for(interaction.guild.default_role)
    ow.send_messages = True
    await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ow)
    await interaction.response.send_message("🔓 Channel unlocked.")
    await send_mod_log(interaction.guild, make_embed(
        "🔓 Channel Unlocked", discord.Color.green(),
        {"Channel": interaction.channel.mention, "Moderator": interaction.user.mention}
    ))

# ==================================================
# SLASH COMMANDS — USER
# ==================================================

@bot.tree.command(name="remember", description="Save a personal detail so StarGPT remembers you.")
async def cmd_remember(interaction: discord.Interaction, key: str, value: str):
    key = key.lower().strip()
    if key not in VALID_PROFILE_KEYS:
        await interaction.response.send_message(
            f"Invalid key. Options: `{'`, `'.join(sorted(VALID_PROFILE_KEYS))}`", ephemeral=True
        )
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM profiles WHERE user_id=?", (interaction.user.id,))
        exists = await cur.fetchone()
        if exists:
            await db.execute(f"UPDATE profiles SET {key}=? WHERE user_id=?", (value, interaction.user.id))
        else:
            data = {k: None for k in VALID_PROFILE_KEYS}
            data[key] = value
            await db.execute(
                "INSERT INTO profiles (user_id, nickname, language, favorite_game, age, bio) VALUES (?,?,?,?,?,?)",
                (interaction.user.id, data["nickname"], data["language"],
                 data["favorite_game"], data["age"], data["bio"])
            )
        await db.commit()
    await interaction.response.send_message(f"✅ Saved `{key}`: {value}", ephemeral=True)


@bot.tree.command(name="appeal", description="Submit an appeal for a moderation action.")
async def cmd_appeal(interaction: discord.Interaction, reason: str):
    await interaction.response.defer(ephemeral=True)
    decision = await review_appeal(reason)
    if decision == "APPROVE":
        await interaction.followup.send("✅ Your appeal has been approved.")
    else:
        await interaction.followup.send("❌ Your appeal was denied.")

# ==================================================
# ANTI-RAID
# ==================================================

@bot.event
async def on_member_join(member: discord.Member):
    gid = member.guild.id
    now = time.time()

    join_tracker[gid].append(now)
    recent = [t for t in join_tracker[gid] if now - t < 15]
    join_tracker[gid] = recent

    if len(recent) >= 10:
        try:
            await member.guild.edit(verification_level=discord.VerificationLevel.high)
        except:
            pass
        await send_mod_log(member.guild, discord.Embed(
            title="🚨 Raid Detected",
            description=f"{len(recent)} members joined in 15 seconds. Verification raised to HIGH.",
            color=discord.Color.red(),
            timestamp=datetime.datetime.utcnow()
        ))

    age_days   = (discord.utils.utcnow() - member.created_at).days
    suspicious = age_days < 7 or member.avatar is None

    if suspicious:
        try:
            await member.timeout(datetime.timedelta(minutes=30), reason="Suspicious account")
        except:
            pass
        await send_mod_log(member.guild, make_embed(
            "⚠️ Suspicious Account", discord.Color.orange(),
            {"User": f"{member.mention} (`{member.id}`)",
             "Account Age": f"{age_days} days",
             "Has Avatar": str(member.avatar is not None),
             "Action": "Timed out 30 min"},
            user=member
        ))

# ==================================================
# MAIN MESSAGE HANDLER
# ==================================================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    content   = message.content.strip() if message.content else ""
    has_image = any(
        a.content_type and a.content_type.startswith("image/")
        for a in message.attachments
    )

    if not content and not has_image:
        return

    is_ai_channel = message.channel.id in setup_channels.get(message.guild.id, set())

    # ── AUTOMOD: text (runs in ALL channels) ──────────────────────────────
    if content:
        violation = rule_based_check(message)
        if not violation:
            violation = await classify_message(content)
            if violation == "SAFE":
                violation = None

        if violation:
            try:
                await message.delete()
            except:
                pass
            await punish(message.author, violation)
            return

    # ── AUTOMOD: images (runs in ALL channels) ────────────────────────────
    if has_image:
        for att in message.attachments:
            if not (att.content_type and att.content_type.startswith("image/")):
                continue
            try:
                img_b64, mime = await fetch_image_b64(att.url)
                img_violation = await classify_image(img_b64, mime)
                if img_violation != "SAFE":
                    try:
                        await message.delete()
                    except:
                        pass
                    await punish(message.author, img_violation)
                    return
            except:
                pass

    # ── AI CHAT (only in configured AI channels) ──────────────────────────
    if not is_ai_channel:
        return

    chat_key = (message.guild.id, message.author.id, message.channel.id)
    if chat_key in active_chats:
        return
    active_chats.add(chat_key)

    try:
        async with message.channel.typing():
            style = guild_styles.get(message.guild.id, "friendly")
            style_notes = {
                "friendly":  "You're warm, encouraging, and approachable.",
                "serious":   "You're precise, professional, and skip small talk.",
                "funny":     "You're witty and love a well-placed joke, but stay helpful.",
                "anime":     "You're expressive, enthusiastic, and slightly dramatic like an anime character.",
                "sarcastic": "You're dry, sarcastic, and deadpan — but never actually mean.",
            }.get(style, "")

            profile_text = await load_profile(message.author.id)
            history      = await load_memory(message.author.id, message.guild.id)

            system = STARGPT_SYSTEM
            if style_notes:
                system += f"\n\nPersonality modifier: {style_notes}"
            if profile_text:
                system += f"\n\nWhat you know about this user:\n{profile_text}"

            msgs = [{"role": "system", "content": system}]
            msgs.extend(history)

            user_text = content if content else "I sent you an image — what do you think?"
            if has_image and content:
                user_text = f"{content}\n[image attached]"

            msgs.append({"role": "user", "content": user_text})

            # Vision: grab first image if present
            image_b64, image_mime = None, "image/png"
            if has_image:
                for att in message.attachments:
                    if att.content_type and att.content_type.startswith("image/"):
                        try:
                            image_b64, image_mime = await fetch_image_b64(att.url)
                        except:
                            pass
                        break

            response = await generate_response(msgs, image_b64=image_b64, image_mime=image_mime)

            await save_memory(message.author.id, message.guild.id, "user", user_text)
            await save_memory(message.author.id, message.guild.id, "assistant", response)

            await send_chunked(message.channel, response, reply_to=message)

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
            print(f"Synced {len(synced)} slash commands")
        except Exception as e:
            print(f"Sync error: {e}")
        _ready_done = True

    print(f"StarGPT online — {bot.user} (ID: {bot.user.id})")

# ==================================================
# RUN
# ==================================================

bot.run(DISCORD_TOKEN)
