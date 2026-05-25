import os
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
import threading
import sqlite3
import asyncio
import time
import re
import io

# =========================
# 🌐 KEEP ALIVE
# =========================
app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT vCourt running ⚖️", 200

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run, daemon=True).start()


# =========================
# 🤖 BOT
# =========================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# =========================
# 🔑 KEYS
# =========================
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

try:
    from google import genai
    from google.genai import types
    gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
except:
    gemini = None

groq = None
if GROQ_API_KEY:
    from groq import Groq
    groq = Groq(api_key=GROQ_API_KEY)


# =========================
# 🗄️ DATABASE
# =========================
DB = "data.db"

def init_db():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS memory (
        user_id INTEGER,
        guild_id INTEGER,
        text TEXT,
        PRIMARY KEY(user_id, guild_id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS personality (
        user_id INTEGER,
        guild_id INTEGER,
        traits TEXT,
        PRIMARY KEY(user_id, guild_id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS warnings (
        user_id INTEGER,
        guild_id INTEGER,
        count INTEGER,
        PRIMARY KEY(user_id, guild_id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS ai_channel (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS court_cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        guild_id INTEGER,
        reason TEXT,
        report TEXT,
        verdict TEXT,
        timestamp INTEGER
    )""")

    conn.commit()
    conn.close()

init_db()


# =========================
# 🧠 MEMORY
# =========================
def get_memory(u, g):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT text FROM memory WHERE user_id=? AND guild_id=?", (u, g))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ""

def save_memory(u, g, t):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO memory VALUES (?, ?, ?)
    ON CONFLICT(user_id, guild_id)
    DO UPDATE SET text=excluded.text
    """, (u, g, t[-1500:]))
    conn.commit()
    conn.close()


# =========================
# 🧠 PERSONALITY
# =========================
def get_personality(u, g):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT traits FROM personality WHERE user_id=? AND guild_id=?", (u, g))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else ""

def update_personality(u, g, text):
    existing = get_personality(u, g)

    prompt = f"""
Extract stable personality traits.

Existing: {existing}
New message: {text}

Return only short traits.
"""

    try:
        result = ai_sync(prompt)

        conn = sqlite3.connect(DB)
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO personality VALUES (?, ?, ?)
        ON CONFLICT(user_id, guild_id)
        DO UPDATE SET traits=excluded.traits
        """, (u, g, result[:200]))

        conn.commit()
        conn.close()
    except:
        pass


# =========================
# ⚠️ WARNINGS
# =========================
def get_warn(u, g):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT count FROM warnings WHERE user_id=? AND guild_id=?", (u, g))
    r = cur.fetchone()
    conn.close()
    return r[0] if r else 0

def add_warn(u, g):
    c = get_warn(u, g) + 1
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO warnings VALUES (?, ?, ?)
    ON CONFLICT(user_id, guild_id)
    DO UPDATE SET count=excluded.count
    """, (u, g, c))
    conn.commit()
    conn.close()
    return c


# =========================
# 🛡️ MOD CHECK
# =========================
def toxic(t):
    return any(re.search(p, t.lower()) for p in [r"https?://", r"discord\.gg", r"@everyone", r"@here"])


# =========================
# 🤖 AI
# =========================
def ai_sync(prompt, image=None):
    try:
        if gemini:
            contents = [prompt]
            if image:
                contents.append(types.Part.from_bytes(image["data"], image["mime_type"]))
            r = gemini.models.generate_content(model="gemini-2.5-flash", contents=contents)
            return r.text or ""
    except:
        pass

    try:
        if groq:
            r = groq.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=700
            )
            return r.choices[0].message.content
    except:
        pass

    return "AI error."

async def get_ai(p, img=None):
    return await asyncio.to_thread(ai_sync, p, img)


# =========================
# ⚖️ COURT AI SYSTEM
# =========================
def court_ai(user_msg, warn, reason):
    prompt = f"""
You are an AI COURT SYSTEM.

PROSECUTOR: explain guilt
DEFENDER: defend user
JUDGE: choose one -> WARN / TIMEOUT / KICK / BAN / DISMISS

User: {user_msg}
Reason: {reason}
Warnings: {warn}
"""

    res = ai_sync(prompt)

    verdict = "DISMISS"
    if "BAN" in res:
        verdict = "BAN"
    elif "KICK" in res:
        verdict = "KICK"
    elif "TIMEOUT" in res:
        verdict = "TIMEOUT"
    elif "WARN" in res:
        verdict = "WARN"

    return res, verdict


# =========================
# GLOBALS
# =========================
ai_channels = {}
spam = {}


# =========================
# 🚀 EVENTS
# =========================
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await bot.tree.sync()


@bot.event
async def on_message(m):
    if m.author.bot or not m.guild:
        return

    u = m.author.id
    g = m.guild.id
    now = time.time()

    spam.setdefault(u, [])
    spam[u] = [x for x in spam[u] if now - x < 5]
    spam[u].append(now)

    if len(spam[u]) > 6:
        await m.delete()
        return

    # AUTO MOD
    if toxic(m.content) and not m.author.guild_permissions.administrator:
        await m.delete()
        w = add_warn(u, g)

        if w >= 3:
            try:
                await m.author.kick(reason="AutoMod")
            except:
                pass
            return

    # AI CHANNEL
    if ai_channels.get(g) == m.channel.id:

        mem = get_memory(u, g)
        per = get_personality(u, g)

        prompt = f"""
You are StarGPT.

PERSONALITY: {per}
MEMORY: {mem}

User: {m.content}
"""

        async with m.channel.typing():

            if any(x in m.content.lower() for x in ["draw", "picture", "imagine"]):
                await m.reply("Generating...")
                return

            reply = await get_ai(prompt)
            await m.reply(reply[:1900])

        save_memory(u, g, (mem + "\n" + m.content)[-1500:])
        update_personality(u, g, m.content)

        return

    await bot.process_commands(m)


# =========================
# ⚙️ SETUP
# =========================
@bot.tree.command(name="setup")
async def setup(i: discord.Interaction, ch: discord.TextChannel):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO ai_channel VALUES (?, ?)
    ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id
    """, (i.guild.id, ch.id))
    conn.commit()
    conn.close()

    ai_channels[i.guild.id] = ch.id
    await i.response.send_message("AI channel set", ephemeral=True)


# =========================
# ⚖️ COURT COMMAND
# =========================
@bot.tree.command(name="court")
@app_commands.checks.has_permissions(administrator=True)
async def court(i: discord.Interaction, user: discord.Member, reason: str):

    mem = get_memory(user.id, i.guild.id)
    warn = get_warn(user.id, i.guild.id)

    report, verdict = court_ai(mem, warn, reason)

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO court_cases(user_id, guild_id, reason, report, verdict, timestamp)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (user.id, i.guild.id, reason, report, verdict, int(time.time())))
    conn.commit()
    conn.close()

    try:
        if verdict == "BAN":
            await user.ban(reason="AI Court")
        elif verdict == "KICK":
            await user.kick(reason="AI Court")
        elif verdict == "WARN":
            add_warn(user.id, i.guild.id)
    except:
        pass

    embed = discord.Embed(title="⚖️ COURT VERDICT")
    embed.add_field(name="User", value=user.mention)
    embed.add_field(name="Verdict", value=verdict)
    embed.add_field(name="Report", value=report[:1000])

    await i.response.send_message(embed=embed)


# =========================
# 🚀 RUN
# =========================
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        exit("Missing token")
    bot.run(DISCORD_TOKEN)
