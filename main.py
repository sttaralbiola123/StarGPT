import os
import re
import time
import asyncio
import datetime
from threading import Thread
from collections import defaultdict

import discord
import aiosqlite

from flask import Flask
from groq import Groq
from discord.ext import commands
from discord import app_commands

# ==================================================
# WEB SERVER
# ==================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT AI Moderation Online"

def run_web():
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8080))
    )

Thread(target=run_web, daemon=True).start()

# ==================================================
# ENV
# ==================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

groq_client = Groq(api_key=GROQ_API_KEY)

# ==================================================
# BOT
# ==================================================

intents = discord.Intents.all()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# ==================================================
# STORAGE
# ==================================================

warning_counts = defaultdict(int)
setup_channels = {}
join_tracker = defaultdict(list)

# FIX DUPLICATE CHAT
active_chats = set()

# ==================================================
# DATABASE
# ==================================================

async def setup_database():

    async with aiosqlite.connect("stargpt.db") as db:

        await db.execute("""
        CREATE TABLE IF NOT EXISTS memory (
            user_id INTEGER,
            guild_id INTEGER,
            role TEXT,
            content TEXT
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
        CREATE TABLE IF NOT EXISTS appeals (
            user_id INTEGER,
            guild_id INTEGER,
            reason TEXT
        )
        """)

        await db.commit()

# ==================================================
# AI CHAT
# ==================================================

SYSTEM_PROMPT = """
You are StarGPT.

You are intelligent, friendly, safe,
and helpful.
"""

async def generate_ai(messages):

    def run():
        return groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.7,
            max_tokens=1000,
        )

    response = await asyncio.to_thread(run)

    return response.choices[0].message.content

# ==================================================
# MEMORY
# ==================================================

async def save_memory(
    user_id,
    guild_id,
    role,
    content
):

    async with aiosqlite.connect("stargpt.db") as db:

        await db.execute(
            """
            INSERT INTO memory
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                guild_id,
                role,
                content
            )
        )

        await db.commit()

async def load_memory(
    user_id,
    guild_id
):

    async with aiosqlite.connect("stargpt.db") as db:

        cursor = await db.execute("""
        SELECT role, content
        FROM memory
        WHERE user_id=? AND guild_id=?
        ORDER BY rowid DESC
        LIMIT 10
        """, (
            user_id,
            guild_id
        ))

        rows = await cursor.fetchall()

    rows.reverse()

    memory = []

    for role, content in rows:

        memory.append({
            "role": role,
            "content": content
        })

    return memory

# ==================================================
# AI MODERATION
# ==================================================

async def ai_moderation(text):

    def run():
        return groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": """
You are a moderation AI.

Classify the message into ONLY ONE:

SAFE
SPAM
SCAM
TOXIC
HARASSMENT
EXTREME
"""
                },
                {
                    "role": "user",
                    "content": text
                }
            ],
            temperature=0
        )

    result = await asyncio.to_thread(run)

    return result.choices[0].message.content.strip().upper()

# ==================================================
# AI APPEAL REVIEW
# ==================================================

async def ai_appeal_review(reason):

    def run():
        return groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {
                    "role": "system",
                    "content": """
Reply ONLY with:
APPROVE
DENY
"""
                },
                {
                    "role": "user",
                    "content": reason
                }
            ],
            temperature=0
        )

    result = await asyncio.to_thread(run)

    return result.choices[0].message.content.strip().upper()

# ==================================================
# STREAM RESPONSE
# ==================================================

async def stream_response(
    message,
    response
):

    msg = await message.channel.send("Thinking...")

    partial = ""

    for word in response.split():

        partial += word + " "

        try:
            await msg.edit(
                content=partial[:1900]
            )
        except:
            pass

        await asyncio.sleep(0.03)

# ==================================================
# PUNISHMENT SYSTEM
# ==================================================

async def punish(member, category):

    key = (
        member.guild.id,
        member.id
    )

    warning_counts[key] += 1

    warnings = warning_counts[key]

    if category == "SAFE":
        return

    elif category == "SPAM":
        duration = 5

    elif category == "TOXIC":
        duration = 10

    elif category == "HARASSMENT":
        duration = 30

    elif category == "SCAM":
        duration = 60

    elif category == "EXTREME":
        duration = 1440

    else:
        duration = 10

    try:
        await member.timeout(
            datetime.timedelta(minutes=duration),
            reason=category
        )
    except:
        pass

    try:
        await member.send(
            f"""
Moderation Action

Category: {category}
Warnings: {warnings}/3
Timeout: {duration} minutes
"""
        )
    except:
        pass

# ==================================================
# ANTI RAID
# ==================================================

@bot.event
async def on_member_join(member):

    guild_id = member.guild.id

    join_tracker[guild_id].append(
        time.time()
    )

    recent = [
        t for t in join_tracker[guild_id]
        if time.time() - t < 15
    ]

    if len(recent) >= 10:

        try:
            await member.guild.edit(
                verification_level=discord.VerificationLevel.high
            )
        except:
            pass

# ==================================================
# SETUP COMMAND
# ==================================================

@bot.tree.command(name="setup")
@app_commands.checks.has_permissions(
    administrator=True
)
async def setup(
    interaction: discord.Interaction,
    channel: discord.TextChannel
):

    setup_channels[
        interaction.guild.id
    ] = channel.id

    await interaction.response.send_message(
        f"AI channel set to {channel.mention}"
    )

# ==================================================
# MESSAGE EVENT
# ==================================================

@bot.event
async def on_message(message):

    # Ignore bots
    if message.author.bot:
        return

    # Ignore DMs
    if not message.guild:
        return

    allowed_channel = setup_channels.get(
        message.guild.id
    )

    # ==================================================
    # AI TRIGGER
    # ==================================================

    should_reply = False

    if bot.user in message.mentions:
        should_reply = True

    elif (
        allowed_channel
        and message.channel.id == allowed_channel
    ):
        should_reply = True

    if not should_reply:
        return

    # ==================================================
    # FIX DUPLICATE REPLIES
    # ==================================================

    chat_key = (
        message.guild.id,
        message.author.id
    )

    if chat_key in active_chats:
        return

    active_chats.add(chat_key)

    try:

        # ==================================================
        # MODERATION
        # ==================================================

        moderation_result = await ai_moderation(
            message.content
        )

        if moderation_result != "SAFE":

            try:
                await message.delete()
            except:
                pass

            await punish(
                message.author,
                moderation_result
            )

            return

        # ==================================================
        # CLEAN PROMPT
        # ==================================================

        prompt = message.content

        if bot.user in message.mentions:

            prompt = re.sub(
                rf"<@!?{bot.user.id}>",
                "",
                prompt
            ).strip()

        # ==================================================
        # PROFILE
        # ==================================================

        profile_text = ""

        async with aiosqlite.connect(
            "stargpt.db"
        ) as db:

            cursor = await db.execute("""
            SELECT nickname,
            language,
            favorite_game
            FROM profiles
            WHERE user_id=?
            """, (
                message.author.id,
            ))

            profile = await cursor.fetchone()

        if profile:

            nickname, language, favorite_game = profile

            profile_text = f"""
Nickname: {nickname}
Language: {language}
Favorite Game: {favorite_game}
"""

        # ==================================================
        # MEMORY
        # ==================================================

        memory = await load_memory(
            message.author.id,
            message.guild.id
        )

        messages = [
            {
                "role": "system",
                "content":
                SYSTEM_PROMPT + "\n" + profile_text
            }
        ]

        messages.extend(memory)

        messages.append({
            "role": "user",
            "content": prompt
        })

        # ==================================================
        # AI RESPONSE
        # ==================================================

        response = await generate_ai(
            messages
        )

        # ==================================================
        # SAVE MEMORY
        # ==================================================

        await save_memory(
            message.author.id,
            message.guild.id,
            "user",
            prompt
        )

        await save_memory(
            message.author.id,
            message.guild.id,
            "assistant",
            response
        )

        # ==================================================
        # SEND RESPONSE
        # ==================================================

        await stream_response(
            message,
            response
        )

    finally:

        active_chats.discard(chat_key)

# ==================================================
# READY EVENT
# ==================================================

@bot.event
async def on_ready():

    await setup_database()

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(e)

    print(f"Logged in as {bot.user}")

# ==================================================
# START BOT
# ==================================================

bot.run(DISCORD_TOKEN)
