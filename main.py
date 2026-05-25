import os
import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai
from groq import Groq
import sqlite3
import re
import time
import asyncio
from flask import Flask
import threading

# -------------------------
# 🌐 FLASK KEEP ALIVE
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT running ⭐", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)

threading.Thread(target=run_flask, daemon=True).start()

# -------------------------
# 🤖 BOT SETUP
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# -------------------------
# 🔑 KEYS
# -------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

groq_client = Groq(api_key=GROQ_API_KEY)

# -------------------------
# 🧠 MEMORY (RAM cache)
# -------------------------
ai_channels = {}
user_msg_times = {}

# -------------------------
# 🗄️ DATABASE
# -------------------------
DB_FILE = "data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS warnings (
        user_id INTEGER,
        guild_id INTEGER,
        count INTEGER,
        PRIMARY KEY (user_id, guild_id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS ai_channels (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        user_id INTEGER,
        guild_id INTEGER,
        memory TEXT,
        PRIMARY KEY (user_id, guild_id)
    )""")

    conn.commit()
    conn.close()

init_db()

# -------------------------
# DB HELPERS
# -------------------------
def get_memory(uid, gid):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT memory FROM user_memory WHERE user_id=? AND guild_id=?", (uid, gid))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ""

def update_memory(uid, gid, new_text):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_memory (user_id, guild_id, memory)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id, guild_id)
        DO UPDATE SET memory=excluded.memory
    """, (uid, gid, new_text))
    conn.commit()
    conn.close()

def get_warnings(uid, gid):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT count FROM warnings WHERE user_id=? AND guild_id=?", (uid, gid))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def add_warning(uid, gid):
    val = get_warnings(uid, gid) + 1
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO warnings VALUES (?, ?, ?)", (uid, gid, val))
    conn.commit()
    conn.close()
    return val

# -------------------------
# ⚡ AI FUNCTION (ASYNC FIXED)
# -------------------------
def ai_sync(prompt, image=None):
    try:
        model = genai.GenerativeModel("gemini-1.5-flash")

        if image:
            return model.generate_content([prompt, image]).text

        return model.generate_content(prompt).text

    except Exception:
        try:
            res = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=500
            )
            return res.choices[0].message.content
        except:
            return "AI error."

async def get_ai(prompt, image=None):
    return await asyncio.to_thread(ai_sync, prompt, image)

# -------------------------
# 🛡️ AUTO MOD
# -------------------------
def is_toxic(content):
    bad_patterns = [
        r"https?://",
        r"discord\.gg",
        r"@everyone",
        r"@here"
    ]
    if any(re.search(p, content.lower()) for p in bad_patterns):
        return True

    if len(content) > 250 and content.count("!!!") > 3:
        return True

    return False

# -------------------------
# 🤖 EVENTS
# -------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.tree.sync()

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    uid = message.author.id
    gid = message.guild.id
    now = time.time()

    # -------------------------
    # SPAM CONTROL
    # -------------------------
    user_msg_times.setdefault(uid, [])
    user_msg_times[uid] = [t for t in user_msg_times[uid] if now - t < 5]
    user_msg_times[uid].append(now)

    if len(user_msg_times[uid]) > 6:
        await message.delete()
        return

    # -------------------------
    # AUTO MOD
    # -------------------------
    if is_toxic(message.content):
        if not message.author.guild_permissions.administrator:
            await message.delete()
            warns = add_warning(uid, gid)

            if warns >= 3:
                try:
                    await message.author.kick(reason="AutoMod limit reached")
                except:
                    pass
            return

    # -------------------------
    # IMAGE SCAN
    # -------------------------
    image = None
    if message.attachments:
        att = message.attachments[0]
        if att.content_type and "image" in att.content_type:
            image_bytes = await att.read()
            image = {
                "mime_type": att.content_type,
                "data": image_bytes
            }

    # -------------------------
    # AI CHANNEL
    # -------------------------
    if ai_channels.get(gid) == message.channel.id:

        memory = get_memory(uid, gid)

        prompt = f"""
You are StarGPT AI.

User memory:
{memory}

User message:
{message.content}

Reply naturally and helpfully.
"""

        async with message.channel.typing():
            reply = await get_ai(prompt, image=image)

        # update memory
        new_memory = (memory + "\n" + message.content)[-1500:]
        update_memory(uid, gid, new_memory)

        await message.reply(reply[:1900])

    await bot.process_commands(message)

# -------------------------
# ⚙️ SETUP COMMAND
# -------------------------
@bot.tree.command(name="setup")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):

    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ai_channels (guild_id, channel_id)
        VALUES (?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET channel_id=excluded.channel_id
    """, (interaction.guild.id, channel.id))
    conn.commit()
    conn.close()

    ai_channels[interaction.guild.id] = channel.id

    await interaction.response.send_message(
        f"AI channel set to {channel.mention}",
        ephemeral=True
    )

# -------------------------
# 🚀 RUN BOT
# -------------------------
if __name__ == "__main__":
    print("Starting StarGPT v2...")

    if not DISCORD_TOKEN:
        print("Missing DISCORD_TOKEN")
        exit(1)

    bot.run(DISCORD_TOKEN)
