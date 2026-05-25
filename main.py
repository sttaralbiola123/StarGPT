import os
import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai
from groq import Groq
import sqlite3
import re
import time
from flask import Flask
import threading

# -------------------------
# 🌐 FLASK KEEP-ALIVE
# -------------------------
app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT is running ⭐", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_flask, daemon=True).start()

# -------------------------
# 🤖 DISCORD BOT SETUP
# -------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# -------------------------
# 🔑 API KEYS
# -------------------------
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

groq_client = Groq(api_key=GROQ_API_KEY)

# -------------------------
# 📦 MEMORY (IN RAM)
# -------------------------
ai_channels = {}
user_msg_times = {}
RAID_MODE = {}

# -------------------------
# 🗄️ DATABASE
# -------------------------
DB_FILE = "data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS warnings (
        user_id INTEGER,
        guild_id INTEGER,
        count INTEGER,
        PRIMARY KEY (user_id, guild_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS punishments (
        user_id INTEGER,
        guild_id INTEGER,
        type TEXT,
        PRIMARY KEY (user_id, guild_id)
    )
    """)

    conn.commit()
    conn.close()

init_db()

# -------------------------
# ⚙️ DB HELPERS
# -------------------------
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

def set_punishment(uid, gid, ptype):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO punishments VALUES (?, ?, ?)", (uid, gid, ptype))
    conn.commit()
    conn.close()

def get_punishment(uid, gid):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT type FROM punishments WHERE user_id=? AND guild_id=?", (uid, gid))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def reset_user(uid, gid):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM warnings WHERE user_id=? AND guild_id=?", (uid, gid))
    cur.execute("DELETE FROM punishments WHERE user_id=? AND guild_id=?", (uid, gid))
    conn.commit()
    conn.close()

# -------------------------
# 🤖 AI FUNCTION
# -------------------------
def get_ai(prompt):
    try:
        if GEMINI_API_KEY:
            model = genai.GenerativeModel("gemini-1.5-flash")
            res = model.generate_content(prompt)
            return res.text or "No response"

        res = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500
        )
        return res.choices[0].message.content

    except:
        return "AI error."

# -------------------------
# 🚀 READY EVENT
# -------------------------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.tree.sync()

# -------------------------
# 🧠 MESSAGE HANDLER
# -------------------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.guild:
        return

    uid = message.author.id
    gid = message.guild.id

    # ---------------- SPAM CHECK ----------------
    now = time.time()
    user_msg_times.setdefault(uid, [])
    user_msg_times[uid] = [t for t in user_msg_times[uid] if now - t < 4]
    user_msg_times[uid].append(now)

    if len(user_msg_times[uid]) > 5:
        await message.delete()
        return

    # ---------------- LINK BLOCK ----------------
    if re.search(r'https?://|discord\.gg', message.content):
        if not message.author.guild_permissions.administrator:
            if ai_channels.get(gid) != message.channel.id:
                await message.delete()
                return

    # ---------------- AI CHANNEL ----------------
    if ai_channels.get(gid) == message.channel.id:

        async with message.channel.typing():
            mod_prompt = f"""
Classify message as SAFE or TOXIC.

Message: {message.content}
Reply ONLY SAFE or TOXIC.
"""

            mod = get_ai(mod_prompt)

            if "TOXIC" in mod.upper():
                await message.delete()

                warns = add_warning(uid, gid)

                if warns >= 3:
                    set_punishment(uid, gid, "KICK")
                    try:
                        await message.author.send("You were kicked for repeated violations.")
                    except:
                        pass
                    await message.guild.kick(message.author, reason="Auto mod")
                return

            reply = get_ai(message.content)
            await message.reply(reply[:1900])

    await bot.process_commands(message)

# -------------------------
# ⚙️ SLASH COMMANDS
# -------------------------
@bot.tree.command(name="setup")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    ai_channels[interaction.guild.id] = channel.id
    await interaction.response.send_message(f"AI channel set to {channel.mention}", ephemeral=True)

# -------------------------
# ▶️ RUN BOT
# -------------------------
bot.run(DISCORD_TOKEN)
