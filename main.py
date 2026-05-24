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
    return "StarGPT is running flawlessly!"

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
# 3. AUTOMOD SETTINGS & CONFIG
# ==========================================
BAD_WORDS = ["badword1", "badword2", "idiot", "spam"]
INVITE_REGEX = re.compile(r"(discord\.gg/|discord\.com/invite/)")
ZALGO_REGEX = re.compile(r"[\u0300-\u036f\u0483-\u0489\u0610-\u0615\u064B-\u065F\u0670\u06D6-\u06DC\u06DF-\u06E4\u06E7\u06E8\u06EA-\u06ED]{4,}")
URL_REGEX = re.compile(r"https?://[^\s]+")
ALLOWED_DOMAINS = ["youtube.com", "youtu.be", "github.com", "tenor.com", "giphy.com"]

UNICODE_REPLACEMENTS = {
    '𝞪': 'a', '𝞫': 'b', '𝞬': 'c', '𝞭': 'd', '𝞮': 'e', '𝞯': 'f', 'а': 'a', 'е': 'e',
    'о': 'o', 'р': 'p', 'х': 'x', 'ѕ': 's', 'і': 'i', '𝟢': '0', '𝟣': '1', '𝟤': '2'
}

user_messages = defaultdict(list) 
recent_joins = [] 

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
star_channels = {}

def clean_unicode_spoofing(text):
    for spoofed, clean in UNICODE_REPLACEMENTS.items():
        text = text.replace(spoofed, clean)
    return text

# ==========================================
# 5. SLASH COMMANDS & REGULAR COMMANDS
# ==========================================
@bot.tree.command(name="setup", description="Piliin kung saang channel lang pwedeng mag-reply si StarGPT.")
@app_commands.describe(channel="Ang channel kung saan pwedeng makipag-chat kay StarGPT")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    star_channels[interaction.guild_id] = channel.id
    await interaction.response.send_message(f"✨ **StarGPT Setup Success!** Mula ngayon, pwede niyo na akong kausapin nang direkta sa {channel.mention} nang hindi na kailangang i-tag!")

@setup.error
async def setup_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("❌ Paumanhin, tanging mga Server Administrator lamang ang pwedeng gumamit ng command na ito.", ephemeral=True)

@bot.command(name="clear")
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int):
    await ctx.channel.purge(limit=amount + 1)
    await ctx.send(f"🧹 Nabura na ang {amount} na mensahe!", delete_after=3)

@bot.command(name="reset")
async def reset_memory(ctx):
    channel_id = ctx.channel.id
    if channel_id in ai_memory:
        ai_memory[channel_id].clear()
        await ctx.reply("🧠 **Memory Reset!** Nalimutan ko na ang mga huli nating pinag-usapan sa channel na ito.")
    else:
        await ctx.reply("Malinis na ang memory sa channel na ito.", delete_after=5)

# ==========================================
# 6. AUTOMOD & SYSTEM EVENTS
# ==========================================
@bot.event
async def on_member_join(member):
    global recent_joins
    current_time = time.time()
    recent_joins = [t for t in recent_joins if current_time - t < RAID_TIME]
    recent_joins.append(current_time)

    if len(recent_joins) >= RAID_LIMIT:
        print(f"🚨 RAID DETECTED!")

@bot.event
async def on_ready():
    try:
        await bot.tree.sync()
        print("✨ Synced slash commands successfully.")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
        
    print(f'✅ Connected successfully! StarGPT Online.')
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="your text channels ✨"))

# ==========================================
# 7. CHAT LOGIC + HEAVY AUTOMOD CHECKER
# ==========================================
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    content = message.content
    content_lower = clean_unicode_spoofing(content.lower())
    user_id = message.author.id
    channel_id = message.channel.id
    guild_id = message.guild.id if message.guild else None
    current_time = time.time()

    # --- AUTOMOD ---
    if INVITE_REGEX.search(content_lower):
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, server invites are banned!", delete_after=5)
        return

    found_urls = URL_REGEX.findall(content)
    if found_urls:
        for url in found_urls:
            is_allowed = any(domain in url.lower() for domain in ALLOWED_DOMAINS)
            if not is_allowed:
                await message.delete()
                await message.channel.send(f"⚠️ {message.author.mention}, links are restricted!", delete_after=5)
                return

    for word in BAD_WORDS:
        if word in content_lower:
            await message.delete()
            await message.channel.send(f"⚠️ {message.author.mention}, watch your language!", delete_after=5)
            return

    if len(content) > 15:
        caps_count = sum(1 for c in content if c.isupper())
        if caps_count / len(content) > 0.7:
            await message.delete()
            await message.channel.send(f"⚠️ {message.author.mention}, don't use ALL CAPS.", delete_after=5)
            return

    if len(message.mentions) > MENTION_LIMIT:
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, no mass mentions!", delete_after=5)
        return

    if len(re.findall(r'<a?:\w+:\d+>', content)) > EMOJI_LIMIT:
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, stop spamming emojis!", delete_after=5)
        return

    if ZALGO_REGEX.search(content):
        await message.delete()
        await message.channel.send(f"⚠️ {message.author.mention}, zalgo text detected.", delete_after=5)
        return

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

    # --- AI CHAT ENGINE (MULTILINGUAL MATCH) ---
    allowed_channel_id = star_channels.get(guild_id)
    should_respond = False

    if allowed_channel_id and channel_id == allowed_channel_id:
        should_respond = True
    elif bot.user in message.mentions:
        if allowed_channel_id and channel_id != allowed_channel_id:
            await message.reply(f"🔒 **StarGPT Locked:** Pwede mo lang akong kausapin sa channel na ito: <#{allowed_channel_id}>", delete_after=8)
            return
        should_respond = True

    if should_respond:
        clean_prompt = message.content.replace(f'<@{bot.user.id}>', '').strip()
        
        if not clean_prompt:
            if bot.user in message.mentions:
                await message.channel.send("Mabuhay! I-type mo lang ang tanong mo dito sa channel na ito at sasagutin kita agad!")
            return

        async with message.channel.typing():
            try:
                ai_memory[channel_id].append({"role": "user", "content": clean_prompt})

                if len(ai_memory[channel_id]) > MAX_MEMORY_LENGTH:
                    ai_memory[channel_id] = ai_memory[channel_id][-MAX_MEMORY_LENGTH:]

                # UPDATED: Sasagot na siya depende sa kung anong gamit na wika ng user (Dynamic Multilingual)
                system_prompt = {
                    "role": "system",
                    "content": (
                        "You are StarGPT, a highly advanced and versatile AI assistant. "
                        "DYNAMIC LANGUAGE RULE: You must automatically match the language and style of the user. "
                        "If they talk to you in English, respond in English. If they use Tagalog, respond in Tagalog. "
                        "If they talk in Taglish, comfortably reply in Taglish. Be conversational, direct, and completely fluid."
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
                    await message.reply("📝 Sobrang haba ng response ko, nilagay ko muna sa file na ito:", file=discord_file)
                else:
                    await message.reply(reply)

            except Exception as e:
                print(f"StarGPT Error: {e}")
                await message.reply("❌ May kaunting aberya ang system ko. Pakisuyong ulitin!")
        return

    await bot.process_commands(message)

# ==========================================
# 8. EXECUTOR
# ==========================================
if __name__ == "__main__":
    if not DISCORD_TOKEN or not GROQ_API_KEY:
        print("❌ Environment variables are missing.")
    else:
        bot.run(DISCORD_TOKEN)
