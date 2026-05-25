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
import io  # Important for image handling

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
# 🧠 MEMORY
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
        user_id INTEGER, guild_id INTEGER, count INTEGER,
        PRIMARY KEY (user_id, guild_id)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS ai_channels (
        guild_id INTEGER PRIMARY KEY, channel_id INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS user_memory (
        user_id INTEGER, guild_id INTEGER, memory TEXT,
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
    """, (uid, gid, new_text[-1500:]))
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
# ⚡ AI TEXT FUNCTION
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
                max_tokens=700
            )
            return res.choices[0].message.content
        except:
            return "Sorry, may issue sa AI ngayon. Subukan mo ulit."

async def get_ai(prompt, image=None):
    return await asyncio.to_thread(ai_sync, prompt, image)

# -------------------------
# 🎨 IMAGE GENERATION FUNCTION
# -------------------------
async def generate_image(prompt: str):
    try:
        model = genai.GenerativeModel("gemini-2.5-flash")  # Change if newer model available
        response = await asyncio.to_thread(
            model.generate_content,
            f"Generate a high quality, detailed image: {prompt}",
            generation_config={"response_modalities": ["IMAGE"]}
        )

        for part in response.parts:
            if part.inline_data:
                image_bytes = part.inline_data.data
                return discord.File(io.BytesIO(image_bytes), filename="stargpt_image.png")
        return None
    except Exception as e:
        print(f"Image generation error: {e}")
        return None

# -------------------------
# 🛡️ AUTO MOD
# -------------------------
def is_toxic(content):
    bad_patterns = [r"https?://", r"discord\.gg", r"@everyone", r"@here"]
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

    # Load AI Channels
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT guild_id, channel_id FROM ai_channels")
    for row in cur.fetchall():
        ai_channels[row[0]] = row[1]
    conn.close()
    print(f"Loaded {len(ai_channels)} AI channel(s)")

@bot.event
async def on_message(message):
    if message.author.bot or not message.guild:
        return

    uid = message.author.id
    gid = message.guild.id
    now = time.time()

    # Spam Control
    user_msg_times.setdefault(uid, [])
    user_msg_times[uid] = [t for t in user_msg_times[uid] if now - t < 5]
    user_msg_times[uid].append(now)

    if len(user_msg_times[uid]) > 6:
        await message.delete()
        return

    # Auto Mod
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

    # AI CHANNEL
    if ai_channels.get(gid) == message.channel.id:
        image = None
        if message.attachments:
            att = message.attachments[0]
            if att.content_type and "image" in att.content_type:
                image_bytes = await att.read()
                image = {"mime_type": att.content_type, "data": image_bytes}

        memory = get_memory(uid, gid)
        user_input = message.content.lower()

        # Image Generation Detection
        image_keywords = ["imagine", "gawin mo picture", "gumawa ng picture", "draw", 
                         "larawan", "picture of", "image of", "generate image", 
                         "mag generate ng", "gawa ng larawan"]

        should_generate_image = any(keyword in user_input for keyword in image_keywords)

        prompt = f"""
You are StarGPT, a friendly, witty, and helpful Filipino AI assistant.

User memory:
{memory}

User message: {message.content}

Reply naturally in Taglish when appropriate. Be fun and engaging.
"""

        async with message.channel.typing():
            if should_generate_image:
                reply_text = "Generating image for you... ⭐ Please wait."
                await message.reply(reply_text)

                img_file = await generate_image(message.content)

                if img_file:
                    embed = discord.Embed(
                        title="⭐ StarGPT Image",
                        description=f"**Prompt:** {message.content[:500]}",
                        color=0x00ffaa
                    )
                    await message.reply(embed=embed, file=img_file)
                else:
                    await message.reply("❌ Sorry, hindi ko magenerate ang image ngayon. Subukan mo ulit mamaya.")
            else:
                # Normal AI Reply
                reply = await get_ai(prompt, image=image)
                await message.reply(reply[:1900])

        # Update Memory
        new_memory = (memory + "\nUser: " + message.content)[-1500:]
        update_memory(uid, gid, new_memory)

        return  # Prevent double processing

    await bot.process_commands(message)

# -------------------------
# ⚙️ COMMANDS
# -------------------------
@bot.tree.command(name="setup", description="Set AI channel")
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
    await interaction.response.send_message(f"✅ AI channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="forget", description="Clear your conversation memory")
async def forget(interaction: discord.Interaction):
    update_memory(interaction.user.id, interaction.guild.id, "")
    await interaction.response.send_message("🧹 Na-clear na ang memory mo with StarGPT.", ephemeral=True)

@bot.tree.command(name="status", description="Check bot status")
async def status(interaction: discord.Interaction):
    embed = discord.Embed(title="⭐ StarGPT Status", color=0x00ffaa)
    embed.add_field(name="Ping", value=f"{round(bot.latency*1000)}ms", inline=True)
    embed.add_field(name="Servers", value=len(bot.guilds), inline=True)
    embed.add_field(name="Status", value="Online ✅", inline=True)
    await interaction.response.send_message(embed=embed)

# -------------------------
# 🚀 RUN BOT
# -------------------------
if __name__ == "__main__":
    print("Starting StarGPT v2 with Auto Image Gen...")
    if not DISCORD_TOKEN:
        print("Missing DISCORD_TOKEN")
        exit(1)
    bot.run(DISCORD_TOKEN)
