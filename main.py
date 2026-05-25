import os
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
import threading
import time
import re
import asyncio
import aiosqlite

from groq import Groq
from google import genai
from google.genai import types

# =========================
# 🌐 FLASK (Render Keep Alive)
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT is running ⭐", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# =========================
# 🤖 DISCORD BOT SETUP
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔑 AI CLIENTS
# =========================
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
gemini_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# =========================
# 📦 MEMORY STORAGE
# =========================
ai_channels = {}
user_memories = {}
user_msg_times = {}

recent_joins = []
RAID_MODE = False

DB_FILE = "server_mod.db"

# =========================
# 🗄️ DATABASE (AIOSQLITE FIX)
# =========================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS warnings (
            user_id INTEGER,
            guild_id INTEGER,
            count INTEGER,
            PRIMARY KEY (user_id, guild_id)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS punishments (
            user_id INTEGER,
            guild_id INTEGER,
            punish_type TEXT,
            PRIMARY KEY (user_id, guild_id)
        )
        """)
        await db.commit()

async def get_warnings(user_id, guild_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT count FROM warnings WHERE user_id=? AND guild_id=?",
            (user_id, guild_id)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

async def add_warning(user_id, guild_id):
    current = await get_warnings(user_id, guild_id)
    new = current + 1

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        INSERT OR REPLACE INTO warnings (user_id, guild_id, count)
        VALUES (?, ?, ?)
        """, (user_id, guild_id, new))
        await db.commit()

    return new

async def reset_user(user_id, guild_id):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM warnings WHERE user_id=? AND guild_id=?", (user_id, guild_id))
        await db.execute("DELETE FROM punishments WHERE user_id=? AND guild_id=?", (user_id, guild_id))
        await db.commit()

async def set_punishment(user_id, guild_id, ptype):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
        INSERT OR REPLACE INTO punishments (user_id, guild_id, punish_type)
        VALUES (?, ?, ?)
        """, (user_id, guild_id, ptype))
        await db.commit()

async def get_punishment(user_id):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute(
            "SELECT guild_id, punish_type FROM punishments WHERE user_id=?",
            (user_id,)
        ) as cursor:
            return await cursor.fetchone()

# =========================
# 🤖 AI ENGINE (FIXED)
# =========================
async def get_ai_response(prompt, system):
    try:
        response = gemini_client.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=800
            )
        )
        return response.text

    except Exception as e:
        print("Gemini failed, switching Groq:", e)

        try:
            res = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=800
            )
            return res.choices[0].message.content

        except Exception as e2:
            print("Groq failed:", e2)
            return "AI system is currently unavailable."

# =========================
# 🚀 READY EVENT
# =========================
@bot.event
async def on_ready():
    await init_db()
    print(f"StarGPT online as {bot.user}")

# =========================
# 🛡️ ANTI RAID
# =========================
@bot.event
async def on_member_join(member):
    global RAID_MODE
    now = time.time()

    recent_joins[:] = [t for t in recent_joins if now - t < 10]
    recent_joins.append(now)

    if len(recent_joins) >= 5:
        RAID_MODE = True

    if RAID_MODE:
        try:
            await member.send("Anti-Raid active. You were removed for safety.")
            await member.kick(reason="Anti-raid system")
        except:
            pass

# =========================
# 💬 MESSAGE HANDLER
# =========================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.guild:
        return

    user_id = message.author.id
    guild_id = message.guild.id
    now = time.time()

    # anti spam
    user_msg_times.setdefault(user_id, [])
    user_msg_times[user_id] = [t for t in user_msg_times[user_id] if now - t < 4]
    user_msg_times[user_id].append(now)

    if len(user_msg_times[user_id]) > 4:
        try:
            await message.delete()
            return
        except:
            pass

    # AI CHANNEL CHECK
    if ai_channels.get(guild_id) != message.channel.id:
        await bot.process_commands(message)
        return

    system = f"""
You are StarGPT. Friendly AI assistant.
User: {message.author.display_name}
Server: {message.guild.name}
"""

    # MODERATION CHECK
    mod_prompt = f"Say TOXIC or SAFE only: {message.content}"
    mod_result = await get_ai_response(mod_prompt, "moderator")

    if "TOXIC" in mod_result.upper():
        await message.delete()

        warnings = await add_warning(user_id, guild_id)

        if warnings >= 3:
            await set_punishment(user_id, guild_id, "BAN")
            await message.guild.ban(message.author, reason="Auto Mod")

        return

    # AI RESPONSE
    reply = await get_ai_response(message.content, system)

    if len(reply) > 2000:
        for i in range(0, len(reply), 2000):
            await message.reply(reply[i:i+2000])
    else:
        await message.reply(reply)

    await bot.process_commands(message)

# =========================
# ⚙️ SLASH COMMANDS
# =========================
@bot.tree.command(name="setup")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    ai_channels[interaction.guild_id] = channel.id
    await interaction.response.send_message("Setup complete!", ephemeral=True)

@bot.tree.command(name="raid")
@app_commands.checks.has_permissions(administrator=True)
async def raid(interaction: discord.Interaction, status: bool):
    global RAID_MODE
    RAID_MODE = status
    await interaction.response.send_message(f"Raid mode: {status}")

# =========================
# 🚀 RUN BOT
# =========================
TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
    bot.run(TOKEN)
else:
    print("Missing DISCORD_TOKEN")
