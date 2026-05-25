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
from flask import Flask
import threading

# --- 🌐 1. FLASK SETUP (for Render Free Tier) ---
app = Flask('')

@app.route('/')
def home():
    return "StarGPT is running online on Render Free Tier! ⭐", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

print("🌐 Starting Flask Web Server...")
flask_thread = threading.Thread(target=run_flask)
flask_thread.daemon = True
flask_thread.start()

# --- 🤖 2. DISCORD BOT CONFIGURATION ---
intents = discord.Intents.default()
intents.message_content = True  
intents.members = True          

bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize AI Clients
ai_client = genai.Client()
groq_client = Groq(api_key=os.environ.get("GROQ_API_KEY"))

# In-Memory Storage
ai_channels = {}     
user_memories = {}   
user_msg_times = {}

# Anti-Raid Tracker
recent_joins = []
RAID_MODE_ACTIVE = False

# --- 🗄️ DATABASE SETUP ---
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
    print(f'⭐ StarGPT is now officially connected to Discord! Logged in as {bot.user.name}')
    try:
        await bot.tree.sync()
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

@bot.event
async def on_member_join(member):
    global RAID_MODE_ACTIVE
    current_time = time.time()
    recent_joins[:] = [t for t in recent_joins if current_time - t < 10]
    recent_joins.append(current_time)
    
    if len(recent_joins) >= 5 or RAID_MODE_ACTIVE:
        if not RAID_MODE_ACTIVE:
            RAID_MODE_ACTIVE = True
        try:
            await member.send(f"🚨 **Anti-Raid Mode Active** sa {member.guild.name}. Pansamantala ka munang tinanggal para sa seguridad.")
            await member.kick(reason="Anti-Raid Security Lockdown.")
        except Exception:
            pass

# --- 🧠 UNIFIED AI GATEWAY ---
def get_ai_response(prompt_text, system_instruction, full_context_list=None):
    try:
        contents = full_context_list if full_context_list else [prompt_text]
        response = ai_client.models.generate_content(
            model='gemini-1.5-flash',
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_instruction, max_output_tokens=800)
        )
        return response.text, "Gemini 1.5 Flash"
    except Exception as e:
        print(f"⚠️ Gemini Limit Hit: {e}. Switching to Groq API...")
        try:
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
            print(f"❌ Both Engines Failed: {groq_err}")
            return "Sorry, I am having trouble connecting to my AI engines right now.", "None"

# --- 🛡️ MAIN MESSAGE HANDLER ---
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # 1. ANTI-SPAM
    user_id = message.author.id
    current_time = time.time()
    if user_id not in user_msg_times:
        user_msg_times[user_id] = []
    user_msg_times[user_id] = [t for t in user_msg_times[user_id] if current_time - t < 4]
    user_msg_times[user_id].append(current_time)
    
    if len(user_msg_times[user_id]) > 4:
        try:
            await message.delete()
            return
        except Exception:
            pass

    # 2. ANTI-LINKS
    url_pattern = re.compile(r'https?://[^\s]+|discord\.gg/[^\s]+')
    if url_pattern.search(message.content):
        if not message.author.guild_permissions.administrator:
            try:
                await message.delete()
                return
            except Exception:
                pass

    # 3. AI CHAT CHANNEL PROCESSING
    guild_id = message.guild.id if message.guild else None
    if guild_id and ai_channels.get(guild_id) == message.channel.id:
        async with message.channel.typing():
            try:
                # Toxicity Check
                mod_prompt = f"Analyze this message. Reply ONLY with 'TOXIC' if it contains extreme profanity, severe insults, or heavy harassment. Reply 'SAFE' if normal: \"{message.content}\""
                mod_reply, _ = get_ai_response(mod_prompt, "You are a strict server auto-moderator.")
                
                if "TOXIC" in mod_reply.upper():
                    await message.delete()
                    warnings = add_warning(user_id, guild_id)
                    
                    if warnings == 1:
                        await message.channel.send(f"⚠️ {message.author.mention}, [Warning 1/3] Clean up your language!", delete_after=10)
                    elif warnings == 2:
                        await message.channel.send(f"⚠️ {message.author.mention}, [Warning 2/3] Final warning!", delete_after=10)
                    elif warnings >= 3:
                        try:
                            if warnings == 3:  # KICK
                                set_punishment(user_id, guild_id, "KICK")
                                
                                dm_message = (
                                    f"👢 **You have been kicked** from **{message.guild.name}**\n\n"
                                    f"**Reason:** You received 3 warnings for toxic / inappropriate behavior.\n"
                                    f"You can rejoin and use `/appeal` if you want to appeal this decision."
                                )
                                try:
                                    await message.author.send(dm_message)
                                except:
                                    pass  # DMs closed
                                
                                await message.channel.send(f"👢 {message.author.mention} has been **KICKED** (3 warnings).")
                                await message.guild.kick(message.author, reason="3 Auto-Mod Warnings.")
                                
                            else:  # BAN
                                set_punishment(user_id, guild_id, "BAN")
                                
                                dm_message = (
                                    f"🔨 **You have been banned** from **{message.guild.name}**\n\n"
                                    f"**Reason:** Repeated toxic behavior after multiple warnings.\n"
                                    f"You may still submit an appeal using `/appeal` command."
                                )
                                try:
                                    await message.author.send(dm_message)
                                except:
                                    pass
                                
                                await message.channel.send(f"🔨 {message.author.mention} has been permanently **BANNED**.")
                                await message.guild.ban(message.author, reason="Continuous Toxicity.")
                        except Exception as e:
                            print(f"Error during punishment: {e}")
                    return 

                # Normal AI Response
                if user_id not in user_memories:
                    user_memories[user_id] = []

                system_instruction = (
                    "Your name is StarGPT. You are a highly intelligent, witty, and adaptive AI assistant. "
                    "CRITICAL RULE: Mirror and adapt to the user's language, slang, and style completely. "
                    "Match their energy—be cool, friendly, and engaging. "
                    f"You are speaking to '{message.author.display_name}' in the server '{message.guild.name}'."
                )

                contents = []
                if message.attachments:
                    for attachment in message.attachments:
                        if any(attachment.filename.lower().endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.webp']):
                            img_bytes = await attachment.read()
                            image_part = types.Part.from_bytes(data=img_bytes, mime_type=attachment.content_type)
                            contents.append(image_part)

                prompt_text = f"User ({message.author.display_name}): {message.content}"
                contents.append(prompt_text)

                full_context = user_memories[user_id] + contents
                response_text, provider_used = get_ai_response(prompt_text, system_instruction, full_context)

                user_memories[user_id].append(prompt_text)
                user_memories[user_id].append(f"AI: {response_text}")
                if len(user_memories[user_id]) > 12:  
                    user_memories[user_id] = user_memories[user_id][-12:]

                if len(response_text) > 2000:
                    for i in range(0, len(response_text), 2000):
                        await message.reply(response_text[i:i+2000])
                else:
                    await message.reply(response_text)

            except Exception as e:
                print(f"Error handling message: {e}")

    await bot.process_commands(message)

# --- 🛠️ SLASH COMMANDS ---
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

@bot.tree.command(name="appeal", description="Appeal your kick or ban.")
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
        "Reply exactly with 'ACCEPT' or 'REJECT' at the start, followed by an explanation."
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
        await interaction.followup.send(f"✅ **Appeal Approved!**\n\n{feedback}", ephemeral=True)
    else:
        feedback = ai_reply.replace("REJECT", "").strip()
        await interaction.followup.send(f"❌ **Appeal Denied.**\n\n{feedback}", ephemeral=True)

# --- 🚀 BOT STARTUP ---
if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if TOKEN:
        print("🚀 Connecting to Discord...")
        bot.run(TOKEN)
    else:
        print("CRITICAL ERROR: DISCORD_TOKEN environment variable is missing!")
