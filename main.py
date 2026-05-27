# =========================================================
# StarGPT Ultimate AI Discord Bot
# Features:
# - AI Chat
# - AI Auto Moderation
# - Image Understanding
# - Web Search
# - Memory / Chat History
# - Slash Commands
# - Flask Keep Alive
# - SQLite Database
# =========================================================

import os
import json
import logging
import threading
import sqlite3
import aiohttp

from flask import Flask

import discord
from discord.ext import commands
from discord import app_commands

from duckduckgo_search import DDGS

import google.generativeai as genai

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s:%(levelname)s:%(name)s: %(message)s"
)

logger = logging.getLogger("StarGPT")

# =========================================================
# ENV VARIABLES
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
PORT = int(os.getenv("PORT", 8080))

# =========================================================
# GEMINI SETUP
# =========================================================

genai.configure(api_key=GEMINI_API_KEY)

chat_model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",

    system_instruction=(
        "You are StarGPT, an advanced Discord AI assistant. "
        "You help with coding, chatting, explanations, "
        "storytelling, translation, web search analysis, "
        "and image understanding."
    )
)

moderator_model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",

    system_instruction=(
        "You are an AI moderation system.\n"
        "Detect:\n"
        "- hate speech\n"
        "- severe toxicity\n"
        "- scams\n"
        "- phishing\n"
        "- NSFW\n"
        "- malicious spam\n\n"

        "Reply ONLY in JSON:\n"
        '{"flagged": true/false, "reason": "short reason"}'
    )
)

# =========================================================
# SQLITE DATABASE
# =========================================================

db = sqlite3.connect(
    "stargpt.db",
    check_same_thread=False
)

cursor = db.cursor()

# CONFIG TABLE

cursor.execute("""
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id TEXT PRIMARY KEY,
    ai_channel_id TEXT,
    mod_logs_channel_id TEXT,
    auto_mod_enabled INTEGER
)
""")

# MEMORY TABLE

cursor.execute("""
CREATE TABLE IF NOT EXISTS memory (
    user_id TEXT,
    message TEXT,
    response TEXT
)
""")

db.commit()

# =========================================================
# FLASK SERVER
# =========================================================

app = Flask(__name__)

@app.route("/")
def home():
    return "StarGPT Ultimate is online!", 200

@app.route("/health")
def health():
    return {"status": "healthy"}, 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(
    target=run_flask,
    daemon=True
).start()

# =========================================================
# DISCORD BOT
# =========================================================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class StarGPT(commands.Bot):

    def __init__(self):

        super().__init__(
            command_prefix="!",
            intents=intents
        )

    async def setup_hook(self):

        logger.info("Syncing commands...")

        await self.tree.sync()

        logger.info("Commands synced!")

bot = StarGPT()

# =========================================================
# DATABASE HELPERS
# =========================================================

def get_guild_config(guild_id: str):

    cursor.execute(
        "SELECT * FROM guild_config WHERE guild_id=?",
        (guild_id,)
    )

    result = cursor.fetchone()

    if result:

        return {
            "guild_id": result[0],
            "ai_channel_id": result[1],
            "mod_logs_channel_id": result[2],
            "auto_mod_enabled": bool(result[3])
        }

    cursor.execute(
        """
        INSERT INTO guild_config
        VALUES (?, ?, ?, ?)
        """,
        (
            guild_id,
            None,
            None,
            1
        )
    )

    db.commit()

    return {
        "guild_id": guild_id,
        "ai_channel_id": None,
        "mod_logs_channel_id": None,
        "auto_mod_enabled": True
    }

def update_config(
    guild_id,
    ai_channel_id,
    mod_logs_channel_id,
    auto_mod_enabled
):

    cursor.execute(
        """
        UPDATE guild_config
        SET ai_channel_id=?,
            mod_logs_channel_id=?,
            auto_mod_enabled=?
        WHERE guild_id=?
        """,

        (
            ai_channel_id,
            mod_logs_channel_id,
            int(auto_mod_enabled),
            guild_id
        )
    )

    db.commit()

# =========================================================
# MEMORY SYSTEM
# =========================================================

def save_memory(user_id, message, response):

    cursor.execute(
        """
        INSERT INTO memory
        VALUES (?, ?, ?)
        """,

        (
            str(user_id),
            message,
            response
        )
    )

    db.commit()

def get_memory(user_id):

    cursor.execute(
        """
        SELECT message, response
        FROM memory
        WHERE user_id=?
        ORDER BY ROWID DESC
        LIMIT 5
        """,

        (str(user_id),)
    )

    return cursor.fetchall()

# =========================================================
# WEB SEARCH
# =========================================================

def web_search(query):

    try:

        results = []

        with DDGS() as ddgs:

            search_results = ddgs.text(
                query,
                max_results=5
            )

            for result in search_results:

                title = result.get("title", "No Title")
                body = result.get("body", "No Description")

                results.append(
                    f"{title}\n{body}"
                )

        return "\n\n".join(results)

    except Exception as e:

        logger.error(f"Search Error: {e}")

        return "Web search failed."

# =========================================================
# READY EVENT
# =========================================================

@bot.event
async def on_ready():

    logger.info(
        f"Logged in as {bot.user}"
    )

    await bot.change_presence(

        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="the universe | StarGPT"
        )
    )

# =========================================================
# MESSAGE EVENT
# =========================================================

@bot.event
async def on_message(message: discord.Message):

    if message.author.bot:
        return

    if not message.guild:
        return

    guild_id = str(message.guild.id)

    config = get_guild_config(guild_id)

    # =====================================================
    # AI AUTOMOD
    # =====================================================

    if config["auto_mod_enabled"]:

        if message.content:

            if not message.author.guild_permissions.manage_messages:

                try:

                    mod_response = (
                        moderator_model.generate_content(

                            f"Analyze:\n{message.content}",

                            generation_config={
                                "response_mime_type": "application/json"
                            }
                        )
                    )

                    verdict = json.loads(
                        mod_response.text.strip()
                    )

                    if verdict.get("flagged"):

                        reason = verdict.get(
                            "reason",
                            "Rule violation"
                        )

                        await message.delete()

                        embed = discord.Embed(
                            title="⚠️ Message Removed",
                            description=(
                                f"{message.author.mention}, "
                                "your message violated rules."
                            ),
                            color=discord.Color.red()
                        )

                        embed.add_field(
                            name="Reason",
                            value=reason,
                            inline=False
                        )

                        embed.set_footer(
                            text="Powered by StarGPT ✨"
                        )

                        await message.channel.send(
                            embed=embed,
                            delete_after=10
                        )

                        return

                except Exception as e:

                    logger.error(f"AutoMod Error: {e}")

    # =====================================================
    # AI CHAT
    # =====================================================

    ai_channel_id = config["ai_channel_id"]

    is_ai_channel = (
        ai_channel_id and
        str(message.channel.id) == str(ai_channel_id)
    )

    is_mentioned = bot.user.mentioned_in(message)

    if is_ai_channel or is_mentioned:

        clean_content = (
            message.content
            .replace(f"<@{bot.user.id}>", "")
            .replace(f"<@!{bot.user.id}>", "")
            .strip()
        )

        if not clean_content and not message.attachments:

            await message.reply(
                "✨ Hello! I am StarGPT."
            )

            return

        async with message.channel.typing():

            try:

                # =============================================
                # IMAGE UNDERSTANDING
                # =============================================

                if message.attachments:

                    attachment = message.attachments[0]

                    allowed = (
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".webp"
                    )

                    if attachment.filename.lower().endswith(
                        allowed
                    ):

                        async with aiohttp.ClientSession() as session:

                            async with session.get(
                                attachment.url
                            ) as response:

                                if response.status == 200:

                                    image_data = await response.read()

                                    image_part = {
                                        "mime_type": attachment.content_type,
                                        "data": image_data
                                    }

                                    prompt = (
                                        clean_content
                                        if clean_content
                                        else "Analyze this image."
                                    )

                                    ai_response = (
                                        chat_model.generate_content(
                                            [prompt, image_part]
                                        )
                                    )

                                    await send_split_response(
                                        message,
                                        ai_response.text
                                    )

                                    return

                # =============================================
                # MEMORY CONTEXT
                # =============================================

                memory = get_memory(message.author.id)

                memory_context = ""

                for old_msg, old_reply in memory:

                    memory_context += (
                        f"User: {old_msg}\n"
                        f"AI: {old_reply}\n\n"
                    )

                # =============================================
                # WEB SEARCH DETECTION
                # =============================================

                search_keywords = [
                    "search",
                    "latest",
                    "news",
                    "who is",
                    "what is",
                    "web"
                ]

                web_context = ""

                if any(
                    keyword in clean_content.lower()
                    for keyword in search_keywords
                ):

                    web_results = web_search(clean_content)

                    web_context = (
                        f"\nWEB SEARCH RESULTS:\n"
                        f"{web_results}\n"
                    )

                # =============================================
                # FINAL AI PROMPT
                # =============================================

                final_prompt = f"""
                Previous Memory:
                {memory_context}

                {web_context}

                User Message:
                {clean_content}
                """

                ai_response = chat_model.generate_content(
                    final_prompt
                )

                response_text = ai_response.text

                save_memory(
                    message.author.id,
                    clean_content,
                    response_text
                )

                await send_split_response(
                    message,
                    response_text
                )

            except Exception as e:

                logger.error(f"AI Error: {e}")

                await message.reply(
                    "⚠️ StarGPT encountered an error."
                )

    await bot.process_commands(message)

# =========================================================
# RESPONSE SPLITTER
# =========================================================

async def send_split_response(
    message,
    text
):

    if len(text) <= 2000:

        await message.reply(text)

        return

    chunks = [
        text[i:i+1900]
        for i in range(
            0,
            len(text),
            1900
        )
    ]

    for chunk in chunks:

        await message.reply(chunk)

# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(
    name="setup",
    description="Configure StarGPT."
)

@app_commands.default_permissions(
    manage_guild=True
)

@app_commands.describe(
    ai_channel="AI chat channel",
    mod_logs_channel="Mod logs channel",
    auto_mod="Enable or disable automod"
)

async def setup(

    interaction: discord.Interaction,

    ai_channel: discord.TextChannel = None,

    mod_logs_channel: discord.TextChannel = None,

    auto_mod: bool = None
):

    guild_id = str(interaction.guild.id)

    config = get_guild_config(guild_id)

    ai_channel_id = (
        ai_channel.id
        if ai_channel
        else config["ai_channel_id"]
    )

    mod_logs_id = (
        mod_logs_channel.id
        if mod_logs_channel
        else config["mod_logs_channel_id"]
    )

    auto_mod_enabled = (
        auto_mod
        if auto_mod is not None
        else config["auto_mod_enabled"]
    )

    update_config(
        guild_id,
        ai_channel_id,
        mod_logs_id,
        auto_mod_enabled
    )

    embed = discord.Embed(
        title="🔧 StarGPT Configuration Updated",
        color=discord.Color.green()
    )

    embed.add_field(
        name="AI Channel",
        value=(
            ai_channel.mention
            if ai_channel
            else "Unchanged"
        ),
        inline=False
    )

    embed.add_field(
        name="Mod Logs",
        value=(
            mod_logs_channel.mention
            if mod_logs_channel
            else "Unchanged"
        ),
        inline=False
    )

    embed.add_field(
        name="AutoMod",
        value=str(auto_mod_enabled),
        inline=False
    )

    embed.set_footer(
        text="Powered by StarGPT ✨"
    )

    await interaction.response.send_message(
        embed=embed
    )

# =========================================================
# RUN BOT
# =========================================================

if __name__ == "__main__":

    if not DISCORD_TOKEN:

        logger.critical(
            "Missing DISCORD_TOKEN"
        )

    elif not GEMINI_API_KEY:

        logger.critical(
            "Missing GEMINI_API_KEY"
        )

    else:

        try:

            bot.run(DISCORD_TOKEN)

        except Exception as e:

            logger.critical(
                f"Startup Error: {e}"
)
