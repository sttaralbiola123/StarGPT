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

EXTREME includes:
- severe harassment
- hate speech
- threats
- dangerous scams
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
You are an appeal review AI.

Reply ONLY with:
APPROVE
DENY

Approve if:
- user appears honest
- punishment was too harsh
- accidental behavior

Deny if:
- repeated abuse
- scams
- severe toxicity
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
# STREAMING RESPONSE
# ==================================================
async def stream_response(
    message,
    response
):

    msg = await message.reply("Thinking...")

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

Use:
/appeal your_reason
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

    account_age = (
        discord.utils.utcnow()
        - member.created_at
    ).days

    suspicious = False

    if account_age < 7:
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
# REMEMBER COMMAND
# ==================================================
@bot.tree.command(name="remember")
async def remember(
    interaction: discord.Interaction,
    key: str,
    value: str
):

    async with aiosqlite.connect("stargpt.db") as db:

        cursor = await db.execute("""
        SELECT *
        FROM profiles
        WHERE user_id=?
        """, (
            interaction.user.id,
        ))

        exists = await cursor.fetchone()

        if exists:

            if key == "nickname":

                await db.execute("""
                UPDATE profiles
                SET nickname=?
                WHERE user_id=?
                """, (
                    value,
                    interaction.user.id
                ))

            elif key == "language":

                await db.execute("""
                UPDATE profiles
                SET language=?
                WHERE user_id=?
                """, (
                    value,
                    interaction.user.id
                ))

            elif key == "favorite_game":

                await db.execute("""
                UPDATE profiles
                SET favorite_game=?
                WHERE user_id=?
                """, (
                    value,
                    interaction.user.id
                ))

        else:

            await db.execute("""
            INSERT INTO profiles
            VALUES (?, ?, ?, ?)
            """, (
                interaction.user.id,
                value if key == "nickname" else None,
                value if key == "language" else None,
                value if key == "favorite_game" else None,
            ))

        await db.commit()

    await interaction.response.send_message(
        f"Saved {key}: {value}"
    )

# ==================================================
# APPEAL COMMAND
# ==================================================
@bot.tree.command(name="appeal")
async def appeal(
    interaction: discord.Interaction,
    reason: str
):

    await interaction.response.defer(
        ephemeral=True
    )

    decision = await ai_appeal_review(
        reason
    )

    if decision == "APPROVE":

        try:
            await interaction.user.timeout(
                None,
                reason="AI approved appeal"
            )
        except:
            pass

        await interaction.followup.send(
            "Your appeal was approved."
        )

    else:

        await interaction.followup.send(
            "Your appeal was denied."
        )

# ==================================================
# MESSAGE EVENT
# ==================================================
@bot.event
async def on_message(message):

    if message.author.bot:
        return

    await bot.process_commands(message)

    if not message.guild:
        return

    allowed_channel = setup_channels.get(
        message.guild.id
    )

    # Channel Lock
    if allowed_channel:

        if (
            message.channel.id != allowed_channel
            and bot.user in message.mentions
        ):

            await message.reply(
                f"Use <#{allowed_channel}> for AI chat."
            )

            return

    # AI Moderation
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

    # AI Chat
    if (
        bot.user in message.mentions
        or (
            allowed_channel
            and message.channel.id == allowed_channel
        )
    ):

        prompt = message.content

        if bot.user in message.mentions:

            prompt = re.sub(
                rf"<@!?{bot.user.id}>",
                "",
                prompt
            ).strip()

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

            profile_text = f'''
Nickname: {nickname}
Language: {language}
Favorite Game: {favorite_game}
'''

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

        response = await generate_ai(
            messages
        )

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

        await stream_response(
            message,
            response
        )

# ==================================================
# READY EVENT
# ==================================================
@bot.event
async def on_ready():

    await setup_database()

    try:
        await bot.tree.sync()
    except Exception as e:
        print(e)

    print(f"Logged in as {bot.user}")

# ==================================================
# START BOT
# ==================================================
bot.run(DISCORD_TOKEN)
