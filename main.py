import os
import discord
from discord import app_commands
from discord.ext import commands
from google import genai
from google.genai import types
from groq import Groq
import sqlite3
import re
import time

# --- CONFIGURATION & INITIALIZATION ---
intents = discord.Intents.default()
intents.message_content = True  
intents.members = True          

# Pangalan ng iyong bot: StarGPT
bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize AI Clients
ai_client = genai.Client()
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# In-Memory Storage
ai_channels = {}     
user_memories = {}   

# Anti-Spam Tracker: {user_id: [timestamps of recent messages]}
user_msg_times = {}

# Anti-Raid Tracker: [timestamps of recently joined members]
recent_joins = []
RAID_MODE_ACTIVE = False

# --- DATABASE SETUP (For Warnings & Punishments) ---
DB_FILE = "server_mod.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS warnings (user_id INTEGER, guild_id INTEGER, count INTEGER, PRIMARY KEY (user_id, guild_id))')
    cursor.execute('CREATE TABLE IF NOT EXISTS punishments (user_id INTEGER, guild_id INTEGER, punish_type TEXT, PRIMARY KEY (user_id, guild_id))')
    conn.commit()
    conn.close()

def get_warnings(user_id, guild_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT count FROM warnings WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

def add_warning(user_id, guild_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    current = get_warnings(user_id, guild_id)
    new_count = current + 1
    cursor.execute("INSERT OR REPLACE INTO warnings (user_id, guild_id, count) VALUES (?, ?, ?)", (user_id, guild_id, new_count))
    conn.commit()
    conn.close()
    return new_count

def reset_warnings(user_id, guild_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM warnings WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    cursor.execute("DELETE FROM punishments WHERE user_id = ? AND guild_id = ?", (user_id, guild_id))
    conn.commit()
    conn.close()

def set_punishment(user_id, guild_id, punish_type):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO punishments (user_id, guild_id, punish_type) VALUES (?, ?, ?)", (user_id, guild_id, punish_type))
    conn.commit()
    conn.close()

def get_punishment(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT guild_id, punish_type FROM punishments WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row if row else (None, None)

init_db()

@bot.event
async def on_ready():
    print(f'⭐ StarGPT is now online! Logged in as {bot.user.name}')
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

# --- ANTI-RAID: GATED JOIN CHECK ---
@bot.event
async def on_member_join(member):
    global RAID_MODE_ACTIVE
    current_time = time.time()
    
    # Linisin ang listahan ng joins na lumagpas na sa 10 seconds
    recent_joins[:] = [t for t in recent_joins if current_time - t < 10]
    recent_joins.append(current_time)
    
    # TRIGGER: Pag may 5 o higit pang accounts na sumali sa loob ng 10 segundo
    if len(recent_joins) >= 5 or RAID_MODE_ACTIVE:
        if not RAID_MODE_ACTIVE:
            RAID_MODE_ACTIVE = True
            print("🚨 ANTI-RAID SYSTEM ACTIVATED! Locking down new members.")
        
        try:
            await member.send(f"🚨 **Anti-Raid Mode Active** sa {member.guild.name}. Pansamantala ka munang tinanggal para sa seguridad ng server. Subukang sumali muli mamaya.")
            await member.kick(reason="Anti-Raid Security Lockdown.")
        except Exception:
            pass

# --- AI FALLBACK CHANGER (Gemini -> Groq llama-3.1-8b-instant) ---
def get_ai_response(prompt_text, system_instruction, full_context_list=None):
    """Subukan si Gemini muna. Pag nag-rate limit (Error 429), lipat agad kay Groq Instant."""
    try:
        # 1. Primary AI: Gemini 1.5 Flash
        contents = full_context_list if full_context_list else [prompt_text]
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_instruction, max_output_tokens=800)
        )
        return response.text, "Gemini 1.5 Flash"
    except Exception as e:
        print(f"⚠️ Gemini Limit Hit or Error: {e}. Switching to Groq API...")
        try:
            # 2. Failsafe AI: Groq llama-3.1-8b-instant
            messages = [{"role": "system", "content": system_instruction}]
            
            if full_context_list:
                for item in full_context_list:
                    if isinstance(item, str):
                        if item.startswith("User"):
                            messages.append({"role": "user", "content": item})
                        elif item.startswith("AI"):
                            messages.append({"role": "assistant", "content": item})
            else:
                messages.append({"role": "user", "content": prompt_text})
                
            response = groq_client.chat.completions.create(
                model="llama-3.1-8b-instant", 
                messages=messages,
                max_tokens=800
            )
            return response.choices[0].message.content, "Groq (Llama 3.1 Instant)"
        except Exception as groq_err:
            print(f"❌ Both Gemini and Groq Failed: {groq_err}")
            return "Sorry, I am having trouble connecting to my AI engines right now. Please try again later.", "None"

# --- SMART TOXICITY INTERPRETER ---
def is_toxic(text: str) -> bool:
    if not text.strip():
        return False
    prompt = f"Analyze this message. Reply ONLY with 'TOXIC' if it contains heavy profanity, harassment, or severe insults. Reply 'SAFE' if normal. Message: \"{text}\""
    reply, _ = get_ai_response(prompt, "You are a strict server moderator.")
    return "TOXIC" in reply.upper()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # 1. ANTI-SPAM SYSTEM
    user_id = message.author.id
    current_time = time.time()
    if user_id not in user_msg_times:
        user_msg_times[user_id] = []
        
    user_msg_times[user_id] = [t for t in user_msg_times[user_id] if current_time - t < 4]
    user_msg_times[user_id].append(current_time)
    
    # Pag nag-send ng higit sa 4 na chat sa loob ng 4 segundo
    if len(user_msg_times[user_id]) > 4:
        try:
            await message.delete()
            await message.channel.send(f"🤫 {message.author.mention}, chill ka lang! Bawal ang spamming dito. (Anti-Spam)", delete_after=5)
            return
        except Exception:
            pass

    # 2. ANTI-LINKS SYSTEM (Bawal discord invites o external links sa mga non-admin)
    url_pattern = re.compile(r'https?://[^\s]+|discord\.gg/[^\s]+')
    if url_pattern.search(message.content):
        if not message.author.guild_permissions.administrator:
            try:
                await message.delete()
                await message.channel.send(f"🚫 {message.author.mention}, bawal mag-send ng links o server invites dito! (Anti-Links)", delete_after=5)
                return
            except Exception:
                pass

    # 3. AI AUTO-MODERATION WITH WARNINGS
    if is_toxic(message.content):
        try:
            await message.delete()
            guild_id = message.guild.id
            warnings = add_warning(user_id, guild_id)
            
            if warnings == 1:
                await message.channel.send(f"⚠️ {message.author.mention}, [Warning 1/3] AI detected toxic language. Keep it clean!", delete_after=10)
            elif warnings == 2:
                await message.channel.send(f"⚠️ {message.author.mention}, [Warning 2/3] Final warning before action is taken!", delete_after=10)
            elif warnings >= 3:
                if warnings == 3:
                    set_punishment(user_id, guild_id, "KICK")
                    await message.channel.send(f"👢 {message.author.mention} has been **KICKED** for getting 3 warnings. Use `/appeal` in DMs to explain your reason.")
                    await message.guild.kick(message.author, reason="3 Auto-Mod Warnings.")
                else:
                    set_punishment(user_id, guild_id, "BAN")
                    await message.channel.send(f"🔨 {message.author.mention} has been permanently **BANNED** for continuous toxicity.")
                    await message.guild.ban(message.author, reason="Continuous Toxicity.")
            return
        except discord.Forbidden:
            print("Missing permissions for moderation action.")

    # 4. AI AUTOMATIC CHAT CHANNEL
    guild_id = message.guild.id if message.guild else None
    if guild_id and ai_channels.get(guild_id) == message.channel.id:
        async with message.channel.typing():
            user_id = message.author.id
            if user_id not in user_memories:
                user_memories[user_id] = []

            # Pinangalanang StarGPT ang Persona ng AI
            system_instruction = (
                "Your name is StarGPT. You are a highly intelligent, witty, and adaptive AI assistant. "
                "CRITICAL RULE: Mirror and adapt to the user's language, slang, and style completely. "
                "Match their energy—be cool, friendly, and engaging. Never reveal you are a bot unless asked. "
                f"You are speaking to '{message.author.display_name}' in the server '{message.guild.name}'."
            )

            contents = []
            
            # Vision check (Image analysis) - exclusive to Gemini
            if message.attachments:
                for attachment in message.attachments:
                    if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                        img_bytes = await attachment.read()
                        image_part = types.Part.from_bytes(data=img_bytes, mime_type=attachment.content_type)
                        contents.append(image_part)

            prompt_text = f"User ({message.author.display_name}): {message.content}"
            contents.append(prompt_text)

            # Pagsamahin ang memory + kasalukuyang context
            user_memories[user_id].append(prompt_text)

            # Tawagin ang AI engine (May built-in instant Groq switch)
            response_text, provider_used = get_ai_response(prompt_text, system_instruction, user_memories[user_id])
            print(f"[LOG] Engine used for StarGPT: {provider_used}") 

            user_memories[user_id].append(f"AI: {response_text}")
            if len(user_memories[user_id]) > 12:  
                user_memories[user_id] = user_memories[user_id][-12:]

            if len(response_text) > 2000:
                for i in range(0, len(response_text), 2000):
                    await message.reply(response_text[i:i+2000])
            else:
                await message.reply(response_text)

    await bot.process_commands(message)

# --- SLASH COMMANDS ---

@bot.tree.command(name="setup", description="Set up the channel where StarGPT will automatically chat.")
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, channel: discord.TextChannel):
    ai_channels[interaction.guild_id] = channel.id
    await interaction.response.send_message(f"✅ Setup complete! **StarGPT** is now active in {channel.mention}.", ephemeral=True)

@bot.tree.command(name="toggle_raid_mode", description="Turn Anti-Raid lockdown mode ON or OFF manually.")
@app_commands.checks.has_permissions(administrator=True)
async def toggle_raid(interaction: discord.Interaction, status: bool):
    global RAID_MODE_ACTIVE
    RAID_MODE_ACTIVE = status
    state = "ENABLED 🔒" if RAID_MODE_ACTIVE else "DISABLED 🔓"
    await interaction.response.send_message(f"🛡️ Anti-Raid Mode has been manually **{state}**.")

@bot.tree.command(name="appeal", description="Appeal your kick or ban. StarGPT AI will evaluate your reason.")
async def appeal(interaction: discord.Interaction, reason: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    guild_id, punish_type = get_punishment(user_id)
    
    if not punish_type or not guild_id:
        await interaction.followup.send("❌ You do not have an active tracked penalty to appeal.", ephemeral=True)
        return
        
    guild = bot.get_guild(guild_id)
    
    ai_prompt = (
        f"Analyze this appeal for a {punish_type}. The user's reason is: \"{reason}\". "
        "If the response is short (like 'sorry' or 'please unban'), low effort, or unrepentant, REJECT it immediately. "
        "If it is thoughtful, explains the context deeply, and holds genuine responsibility, ACCEPT it. "
        "Reply exactly with 'ACCEPT' or 'REJECT' at the start, followed by an explanation directly to the user in their language."
    )
    
    ai_reply, _ = get_ai_response(ai_prompt, "You are StarGPT acting as a fair server judge.")
    
    if ai_reply.startswith("ACCEPT"):
        feedback = ai_reply.replace("ACCEPT", "").strip()
        if punish_type == "BAN" and guild:
            try:
                await guild.unban(discord.Object(id=user_id))
            except Exception:
                pass
        reset_warnings(user_id, guild_id)
        
        invite_msg = ""
        if guild:
            try:
                channels = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
                if channels:
                    invite = await channels[0].create_invite(max_uses=1, unique=True)
                    invite_msg = f"\nRejoin here: {invite.url}"
            except Exception:
                pass
                
        await interaction.followup.send(f"✅ **Appeal Approved by StarGPT!**\n\n{feedback}{invite_msg}", ephemeral=True)
    else:
        feedback = ai_reply.replace("REJECT", "").strip()
        await interaction.followup.send(f"❌ **Appeal Denied.**\n\n{feedback}\n\n*Short/Lazy justifications are auto-rejected by the system.*", ephemeral=True)

# Retrieve Token from Render Config
TOKEN = os.environ.get("DISCORD_TOKEN")
if TOKEN:
    bot.run(TOKEN)
else:
    print("CRITICAL ERROR: DISCORD_TOKEN environment variable is missing!")
