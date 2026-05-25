import os
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
import threading
import time
import asyncio
import aiosqlite

from groq import Groq
from google import genai
from google.genai import types

# =========================
# 🌐 FLASK KEEP ALIVE
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT Ultra Online ⭐", 200

threading.Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080))), daemon=True).start()

# =========================
# 🤖 BOT SETUP
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# =========================
# 🔑 AI CLIENTS
# =========================
groq = Groq(api_key=os.environ.get("GROQ_API_KEY"))
gemini = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# =========================
# 📦 STORAGE
# =========================
ai_channels = {}
user_mode = {}
user_threads = {}
logs_channel = {}

DB = "stargpt.db"

# =========================
# ⚡ REQUEST QUEUE (IMPORTANT FIX)
# =========================
queue_lock = asyncio.Lock()

# =========================
# 🗄️ DATABASE
# =========================
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS chat (
            user_id INTEGER,
            role TEXT,
            content TEXT
        )
        """)
        await db.commit()

async def save_chat(uid, role, content):
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO chat VALUES (?,?,?)", (uid, role, content))
        await db.commit()

async def load_chat(uid, limit=12):
    async with aiosqlite.connect(DB) as db:
        async with db.execute(
            "SELECT role, content FROM chat WHERE user_id=? ORDER BY rowid DESC LIMIT ?",
            (uid, limit)
        ) as c:
            rows = await c.fetchall()
            return list(reversed(rows))

# =========================
# 🤖 AI ENGINE
# =========================
async def ai(prompt, system):
    try:
        res = gemini.models.generate_content(
            model="gemini-1.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=800
            )
        )
        return res.text
    except:
        res = groq.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt}
            ],
            max_tokens=800
        )
        return res.choices[0].message.content

# =========================
# 🧠 SYSTEM
# =========================
def system_prompt(user, guild, mode):
    style = {
        "fast": "Short replies only.",
        "smart": "Balanced helpful answers.",
        "deep": "Very detailed explanations."
    }.get(mode, "Balanced helpful answers.")

    return f"""
You are StarGPT Ultra.
User: {user}
Server: {guild}
Style: {style}
"""

# =========================
# 🚀 THREAD SYSTEM (NEW FEATURE #1)
# =========================
async def get_thread(message):
    if message.author.id in user_threads:
        return user_threads[message.author.id]

    thread = await message.channel.create_thread(
        name=f"chat-{message.author.name}",
        type=discord.ChannelType.public_thread
    )

    user_threads[message.author.id] = thread
    return thread

# =========================
# 🧠 MESSAGE HANDLER (ULTRA FIXED)
# =========================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.guild:
        return

    if ai_channels.get(message.guild.id) != message.channel.id:
        await bot.process_commands(message)
        return

    async with queue_lock:  # ⚡ prevents spam + double replies
        uid = message.author.id

        # moderation light check
        if "http" in message.content:
            await message.delete()
            return

        mode = user_mode.get(uid, "smart")
        system = system_prompt(message.author.display_name, message.guild.name, mode)

        # load memory
        history = await load_chat(uid)
        prompt = "\n".join([f"{r}: {c}" for r, c in history] + [message.content])

        # AI response
        reply = await ai(prompt, system)

        # save memory
        await save_chat(uid, "user", message.content)
        await save_chat(uid, "assistant", reply)

        # thread reply (NEW FEATURE #1)
        thread = await get_thread(message)
        await thread.send(reply)

    await bot.process_commands(message)

# =========================
# ⚙️ SLASH COMMANDS
# =========================

# setup
@bot.tree.command(name="setup")
async def setup(interaction, channel: discord.TextChannel):
    ai_channels[interaction.guild_id] = channel.id
    await interaction.response.send_message("StarGPT Ultra enabled.", ephemeral=True)

# mode
@bot.tree.command(name="mode")
async def mode(interaction, mode: str):
    user_mode[interaction.user.id] = mode
    await interaction.response.send_message(f"Mode: {mode}", ephemeral=True)

# reset (NEW FEATURE)
@bot.tree.command(name="reset")
async def reset(interaction):
    async with aiosqlite.connect(DB) as db:
        await db.execute("DELETE FROM chat WHERE user_id=?", (interaction.user.id,))
        await db.commit()

    await interaction.response.send_message("Memory cleared.", ephemeral=True)

# export chat (NEW FEATURE)
@bot.tree.command(name="export")
async def export(interaction):
    data = await load_chat(interaction.user.id, 50)
    text = "\n".join([f"{r}: {c}" for r, c in data])

    file = discord.File(fp=bytes(text, "utf-8"), filename="chat.txt")
    await interaction.response.send_message(file=file, ephemeral=True)

# logs setup (NEW FEATURE #2)
@bot.tree.command(name="setlogs")
async def setlogs(interaction, channel: discord.TextChannel):
    logs_channel[interaction.guild_id] = channel.id
    await interaction.response.send_message("Logs enabled.", ephemeral=True)

# =========================
# 🚀 READY
# =========================
@bot.event
async def on_ready():
    await init_db()
    print(f"StarGPT Ultra running as {bot.user}")

# =========================
# RUN
# =========================
TOKEN = os.environ.get("DISCORD_TOKEN")

if TOKEN:
    bot.run(TOKEN)
