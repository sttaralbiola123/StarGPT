import discord
from discord.ext import commands
from discord import app_commands
import os
import io
import re
import time
from collections import defaultdict
from groq import Groq
from flask import Flask
from threading import Thread

# ==========================================
# 1. KEEP-ALIVE WEB SERVER (FOR RENDER)
# ==========================================
app = Flask(__name__)

@app.route('/')
def home():
    return "StarGPT Ultra with Slash Setup and Advanced AutoMod is Live!"

def run_server():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

Thread(target=run_server, daemon=True).start()

# ==========================================
# 2. BOT CONFIGURATION & CORE SETUP
# ==========================================
DISCORD_TOKEN = os.environ.get("DISCORD")
GROQ_API_KEY = os.environ.get("GROQ")
groq_client = Groq(api_key=GROQ_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True 

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================================
# 3. ADVANCED AUTOMOD SETTINGS & CONFIG
# ==========================================
BAD_WORDS = ["badword1", "badword2", "idiot", "spam"]
INVITE_REGEX = re.compile(r"(discord\.gg/|discord\.com/invite/)")
ZALGO_REGEX = re.compile(r"[\u0300-\u036f\u0483-\u0489\u0610-\u0615\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E4\u06E7\u06E8\u06EA-\u06ED]{4,}")

# NEW: Anti-Link configuration (Blocks general links but allows safe ones)
URL_REGEX = re.compile(r"https?://[^\s]+")
ALLOWED_DOMAINS = ["youtube.com", "youtu.be", "github.com", "tenor.com", "giphy.com"]

# NEW: Lookalike character translation mapping (Anti-bypass word filter)
UNICODE_REPLACEMENTS = {
    '𝞪': 'a', '𝞫': 'b', '𝞬': 'c', '𝞭': 'd', '𝞮': 'e', '𝞯': 'f', 'а': 'a', 'е': 'e',
    'о': 'o', 'р': 'p', 'х': 'x', 'ѕ': 's', 'і': 'i', '𝟢': '0', '𝟣': '1', '𝟤': '2'
}

user_messages = defaultdict(list) 
recent_joins = [] 

# AutoMod Limits
SPAM_LIMIT = 5        
SPAM_TIME = 5         
MENTION_LIMIT = 5     
EMOJI_LIMIT = 10      
RAID_LIMIT = 10       
RAID_TIME = 10        

# ==========================================
# 4. MEMORY & CONFIGURATION STORAGE
# ==========================================
ai_memory = defaultdict(list)
MAX_MEMORY_LENGTH = 30 

# Dynamic AI Channel Binding Config: { guild_id: channel_id }
# Kung anong channel ang i-setup mo rito, doon lang papayagang sumagot ang AI
star_channels = {}

def clean_unicode_spoofing(text):
    """Translates lookalike characters back to standard text to catch bypassed words"""
    for spoofed, clean in UNICODE_REPLACEMENTS.items():
        text = text.replace(spoofed, clean)
    return text

# ==========================================
# 5. SLASH COMMANDS & REGULAR COMMANDS
# ==========================================
@bot.tree.command(name="setup", description="Piliin kung saang channel lang pwedeng mag-reply si StarGPT sa server na ito.")
@app_commands.describe(channel="Ang channel kung saan pwedeng makipag-chat kay StarGPT")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    star_channels[interaction.guild_id] = channel.id
    await interaction.response.send_content(f"✨ **StarGPT Setup Success!** Mula ngayon, sa channel na {channel.mention} lang ako sasagot sa mga chat ninyo.")

@setup.error
async def setup_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_content("❌ Paumanhin, tanging mga Server Administrator lamang ang pwedeng gumamit ng command na ito.", ephemeral=True)

# Moderation Command: !clear
@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int):
    """Mabilisang pagbura ng mensahe (e.g., !clear 50)"""
    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🧹 Nabura na ang {amount} na mensahe!", delete_after=3)

# AI Command: !reset
@bot.command(name="reset")
async def reset_memory(ctx):
    """Nililinis ang AI memory ng kasalukuyang channel"""
    channel_id = ctx.channel.id
    if channel_id in ai_memory:
        ai_memory[channel_id].clear()
        await ctx.reply("🧠 **Memory Reset!** Nalimutan ko na ang mga huli nating pinag-usapan sa channel na ito. Pwede na tayong magsimula ng bagong topic.")
    else:
        await ctx.reply("Ang memory sa channel na ito ay malinis na.", delete_after=5)

# ==========================================
# 6. AUTOMOD & ANTI-RAID SYSTEM EVENTS
# ==========================================
@bot.event
async def on_member_join(member):
    global recent_joins
    current_time = time.time()
    recent_joins = [t for t in recent_joins if current_time - t < RAID_TIME]
    recent_joins.append(current_time)

    if len(recent_joins) >= RAID_LIMIT:
        print(f"🚨 RAID DETECTED! StarGPT AutoMod defending.")

@bot.event
async def on_ready():
    # Sync ang slash command sa Discord API servers
    try:
        synced = await bot.tree.sync()
        print(f"✨ Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
        
    print(f'✅ Connected successfully! StarGPT Engine is Online.')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="your mentions ✨"))

# ==========================================
# 7. CHAT LOGIC + HEAVY AUTOMOD CHECKER
# ==========================================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content
    # Clean standard bypass text formatting tricks
    content_lower = clean_unicode_spoofing(content.lower())
    user_id = message.author.id
    channel_id = message.channel.id
    guild_id = message.guild.id if message.guild else None
    current_time = time.time()

    # --- PART A: ADVANCED AUTOMOD ---
    
    # 1. Anti-Invite
    if INVITE_REGEX.search(content_lower):
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, server invites are banned here!", delete_after=5)
        return

    # 2. Anti-Link Protection (With Allowed Safelist domains)
    found_urls = URL_REGEX.findall(content)
    if found_urls:
        for url in found_urls:
            is_allowed = any(domain in url.lower() for domain in ALLOWED_DOMAINS)
            if not is_allowed:
                await message.delete()
                await message.channel.send(f"⚠️ {message.author.mention}, links/URLs are restricted to trusted websites only!", delete_after=5)
                return

    # 3. Word Filter
    for word in BAD_WORDS:
        if word in content_lower:
            await message.delete()
            await message.channel.send(f"⚠️ {message.author.mention}, watch your language!", delete_after=5)
            return

    # 4. Anti-Caps 
    if len(content) > 15:
        caps_count = sum(1 for c in content if c.isupper())
        if caps_count / len(content) > 0.7:
            await message.delete()
            await message.channel.send(f"⚠️ {message.author.mention}, please lower your voice (Too many Caps).", delete_after=5)
            return

    # 5. Anti-Mention Spam
    if len(message.mentions) > MENTION_LIMIT:
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, mass mentions are not allowed!", delete_after=5)
        return

    # 6. Anti-Emoji Spam
    if len(re.findall(r'<a?:\w+:\d+>', content)) > EMOJI_LIMIT:
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, please stop spamming emojis!", delete_after=5)
        return

    # 7. Anti-Zalgo Text
    if ZALGO_REGEX.search(content):
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, corrupted/zalgo text detected.", delete_after=5)
        return

    # 8. Anti-Spam / Flood Tracking
    user_messages[user_id].append({"time": current_time, "content": content_lower})
    user_messages[user_id] = [msg for msg in user_messages[user_id] if current_time - msg["time"] < SPAM_TIME]
    
    if len(user_messages[user_id]) >= SPAM_LIMIT:
        await message.delete()
        await message.channel.send(f"🛑 {message.author.mention}, stop flooding the chat!", delete_after=5)
        return
    
    if len(user_messages[user_id]) >= 3:
        if user_messages[user_id][-1]["content"] == user_messages[user_id][-2]["content"] == user_messages[user_id][-3]["content"]:
            await message.delete()
            await message.channel.send(f"🛑 {message.author.mention}, stop repeating yourself!", delete_after=5)
            return

    # --- PART B: BINDED STARGPT CHAT ENGINE ---
    if bot.user in message.mentions:
        # Check kung na-setup na ang channel para sa server na ito
        allowed_channel_id = star_channels.get(guild_id)
        
        # Kung may na-setup na, at hindi ito ang tamang channel, iba-block ng bot ang response
        if allowed_channel_id and channel_id != allowed_channel_id:
            await message.reply(f"🔒 **StarGPT Access Locked:** Maaari niyo lamang akong kausapin sa itinalagang channel na ito: <#{allowed_channel_id}>", delete_after=8)
            return

        clean_prompt = message.content.replace(f'<@{bot.user.id}>', '').strip()
        
        if not clean_prompt:
            await message.channel.send("Hey there! Mention me along with a prompt, and I'll remember our conversation just like ChatGPT!")
            return

        async with message.channel.typing():
            try:
                ai_memory[channel_id].append({"role": "user", "content": clean_prompt})

                if len(ai_memory[channel_id]) > MAX_MEMORY_LENGTH:
                    ai_memory[channel_id] = ai_memory[channel_id][-MAX_MEMORY_LENGTH:]

                system_prompt = {
                    "role": "system",
                    "content": (
                        "You are StarGPT, a highly capable and intelligent AI assistant and advanced server moderator. "
                        "You process deep technical logic, write flawless code, create engaging stories, "
                        "and think with high-level reasoning. Keep your formatting beautiful and clean using bolding and structures. "
                        "You are conversational and helpful, speaking fluently in English, Tagalog, or Taglish."
                    )
                }
                
                full_conversation_payload = [system_prompt] + ai_memory[channel_id]

                chat_completion = groq_client.chat.completions.create(
                    messages=full_conversation_payload,
                    model="llama-3.1-8b-instant", 
                    max_tokens=3500,
                    temperature=0.7
                )
                
                reply = chat_completion.choices[0].message.content
                ai_memory[channel_id].append({"role": "assistant", "content": reply})

                if len(reply) > 2000:
                    file_stream = io.BytesIO(reply.encode('utf-8'))
                    discord_file = discord.File(fp=file_stream, filename="stargpt_response.txt")
                    await message.reply("📝 The response is long and detailed, so I compiled it into a file for you!", file=discord_file)
                else:
                    await message.reply(reply)

            except Exception as e:
                print(f"StarGPT Core Error: {e}")
                await message.reply("❌ My neural networks got a bit tied up. Try rephrasing that!")

    # Pinoproseso ang !clear at !reset commands
    await bot.process_commands(message)
