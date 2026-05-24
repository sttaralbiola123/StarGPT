# ============================================================
#   ⭐ StarGPT — Discord AI Bot
#   Powered by Groq API + Discord.py
#   Deploy on Render | Flask keep-alive included
# ============================================================
# SETUP:
#   1. Copy .env.example -> .env
#   2. Fill in DISCORD_TOKEN and GROQ_API_KEY
#   3. python main.py
# ============================================================

import discord
from discord.ext import commands, tasks
from discord import app_commands
from groq import AsyncGroq
from flask import Flask, jsonify, render_template_string
import os, re, json, random, logging, threading, asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[logging.FileHandler('stargpt.log'), logging.StreamHandler()]
)
log = logging.getLogger('StarGPT')

# ══════════════════════════════════════════════════════════════
#  FLASK KEEP-ALIVE SERVER (prevents Render from sleeping)
# ══════════════════════════════════════════════════════════════
flask_app = Flask(__name__)
BOT_START = datetime.utcnow()

DASHBOARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>⭐ StarGPT</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;
       display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:#161b22;border:1px solid #FFD700;border-radius:16px;
        padding:48px;text-align:center;max-width:500px;width:90%;
        box-shadow:0 0 40px rgba(255,215,0,0.15)}
  .logo{font-size:4rem;margin-bottom:12px}
  h1{color:#FFD700;font-size:2rem;margin-bottom:6px;letter-spacing:2px}
  p{color:#8b949e;margin:6px 0;font-size:.9rem}
  .dot{color:#3fb950;font-size:1.2rem}
  .badge{display:inline-block;background:#238636;color:white;
         padding:6px 16px;border-radius:20px;font-size:.85rem;margin-top:20px}
  .stats{display:flex;gap:16px;justify-content:center;margin-top:20px;flex-wrap:wrap}
  .stat{background:#21262d;border-radius:8px;padding:12px 20px}
  .stat-val{color:#FFD700;font-size:1.3rem;font-weight:bold}
  .stat-label{color:#8b949e;font-size:.75rem;margin-top:2px}
</style></head>
<body><div class="card">
  <div class="logo">⭐</div>
  <h1>StarGPT</h1>
  <p>Discord AI Bot • Powered by Groq</p>
  <p style="margin-top:10px"><span class="dot">●</span> <strong style="color:#3fb950">Online</strong></p>
  <div class="stats">
    <div class="stat"><div class="stat-val">{{ uptime }}</div><div class="stat-label">UPTIME</div></div>
    <div class="stat"><div class="stat-val">Groq</div><div class="stat-label">AI ENGINE</div></div>
  </div>
  <span class="badge">🚀 Running on Render</span>
</div></body></html>"""

@flask_app.route('/')
def home():
    d = datetime.utcnow() - BOT_START
    h, r = divmod(int(d.total_seconds()), 3600)
    m, s = divmod(r, 60)
    return render_template_string(DASHBOARD_HTML, uptime=f'{h}h {m}m {s}s')

@flask_app.route('/health')
def health():
    return jsonify({'status': 'online', 'bot': 'StarGPT',
                    'uptime': int((datetime.utcnow() - BOT_START).total_seconds())})

@flask_app.route('/ping')
def ping():
    return jsonify({'pong': True})

def run_flask():
    port = int(os.getenv('PORT', 8080))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ══════════════════════════════════════════════════════════════
#  GROQ AI CLIENT
# ══════════════════════════════════════════════════════════════
groq_client = AsyncGroq(api_key=os.getenv('GROQ_API_KEY'))
AI_MODEL    = 'llama-3.1-8b-instant'  # Free tier Groq model ✅

SYSTEM_PROMPT = """You are StarGPT — a smart, friendly, and helpful Discord AI assistant.
- You respond clearly and concisely to any question.
- You have a personality — you're not robotic.
- You do NOT provide harmful, illegal, or dangerous content.
- For moderation questions, redirect users to server moderators.
- Use emojis occasionally to keep the chat fun.
- You can speak both English and other languages depending on what the user writes in.
"""

# Per-user conversation memory (max 20 messages = 10 turns)
convo_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))
# Channel IDs with AI auto-reply enabled
ai_channels: set[int] = set()
# Guild -> channel mapping
guild_chat_channels: dict[int, int] = {}

# ══════════════════════════════════════════════════════════════
#  AUTOMOD CONFIG
# ══════════════════════════════════════════════════════════════
AUTOMOD_FILE = 'automod_config.json'

DEFAULT_CFG = {
    'enabled': True, 'log_channel': None,
    'anti_spam': True, 'anti_flood': True, 'anti_caps': True,
    'anti_links': False, 'anti_invite': True, 'anti_mention_spam': True,
    'anti_emoji_spam': True, 'anti_zalgo': True, 'anti_mass_join': True,
    'anti_repeated_text': True, 'word_filter': True,
    'spam_threshold': 5, 'spam_window': 5, 'flood_threshold': 10,
    'caps_percent': 70, 'caps_min_length': 10,
    'max_mentions': 5, 'max_emojis': 10,
    'mass_join_threshold': 10, 'mass_join_window': 30,
    'repeated_text_count': 3,
    'whitelist_roles': [],
    'filtered_words': [
        'nigger', 'nigga', 'faggot', 'retard', 'kys', 'kill yourself',
        'rape', 'molest', 'pedo', 'pedophile', 'cunt', 'chink', 'spic',
    ],
    'allowed_domains': ['tenor.com', 'giphy.com', 'imgur.com', 'discord.com', 'discord.gg'],
}

def load_cfg() -> dict:
    if os.path.exists(AUTOMOD_FILE):
        with open(AUTOMOD_FILE) as f: return json.load(f)
    return {}

def save_cfg(c: dict):
    with open(AUTOMOD_FILE, 'w') as f: json.dump(c, f, indent=2)

def gcfg(guild_id: int, configs: dict) -> dict:
    gid = str(guild_id)
    if gid not in configs:
        configs[gid] = DEFAULT_CFG.copy()
    return configs[gid]

# ══════════════════════════════════════════════════════════════
#  BOT SETUP
# ══════════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.moderation = True

bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)
tree = bot.tree

# AutoMod in-memory state
automod_configs: dict = {}
msg_history:  dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
msg_content:  dict[int, deque] = defaultdict(lambda: deque(maxlen=10))
join_history: dict[int, list]  = defaultdict(list)
warn_counts:  dict[int, int]   = defaultdict(int)

# ══════════════════════════════════════════════════════════════
#  ON READY
# ══════════════════════════════════════════════════════════════
@bot.event
async def on_ready():
    global automod_configs
    automod_configs = load_cfg()
    print("""
╔══════════════════════════════════╗
║       ⭐  S t a r G P T  ⭐     ║
║   Discord AI Bot by Groq API     ║
╚══════════════════════════════════╝""")
    log.info(f'Online as {bot.user} ({bot.user.id})')
    try:
        synced = await tree.sync()
        log.info(f'Synced {len(synced)} slash commands')
    except Exception as e:
        log.error(f'Sync error: {e}')
    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name='⭐ StarGPT | /help'),
        status=discord.Status.online
    )
    cleanup_loop.start()

@bot.event
async def on_guild_join(guild: discord.Guild):
    for ch in guild.text_channels:
        if ch.permissions_for(guild.me).send_messages:
            e = discord.Embed(
                title='⭐ StarGPT has arrived!',
                description=(
                    'Thanks for inviting me!\n\n'
                    '🤖 `/chat` `/ask` — AI chat\n'
                    '🛡️ `/automod status` — AutoMod setup\n'
                    '📖 `/help` — All commands\n\n'
                    '**Set up your AI chat channel:**\n`/setup_chat #channel`'
                ),
                color=0xFFD700
            )
            e.set_footer(text='StarGPT • Powered by Groq AI')
            await ch.send(embed=e)
            break

# ══════════════════════════════════════════════════════════════
#  CLEANUP LOOP
# ══════════════════════════════════════════════════════════════
@tasks.loop(minutes=5)
async def cleanup_loop():
    now = datetime.now(timezone.utc)
    for gid in list(join_history.keys()):
        join_history[gid] = [t for t in join_history[gid]
                             if (now - t).total_seconds() < 120]

# ══════════════════════════════════════════════════════════════
#  AI HELPER
# ══════════════════════════════════════════════════════════════
async def ask_ai(uid: str, message: str, max_tokens: int = 1024) -> str:
    history = list(convo_history[uid])
    history.append({'role': 'user', 'content': message})
    res = await groq_client.chat.completions.create(
        model=AI_MODEL,
        messages=[{'role': 'system', 'content': SYSTEM_PROMPT}] + history,
        max_tokens=max_tokens,
        temperature=0.8
    )
    answer = res.choices[0].message.content
    convo_history[uid].append({'role': 'user', 'content': message})
    convo_history[uid].append({'role': 'assistant', 'content': answer})
    return answer

# ══════════════════════════════════════════════════════════════
#  ⭐ AI COMMANDS
# ══════════════════════════════════════════════════════════════
@tree.command(name='chat', description='Chat with StarGPT (with memory)')
@app_commands.describe(message='Your message')
async def cmd_chat(interaction: discord.Interaction, message: str):
    await interaction.response.defer(thinking=True)
    try:
        answer = await ask_ai(str(interaction.user.id), message)
        turns = len(convo_history[str(interaction.user.id)]) // 2
        e = discord.Embed(color=0xFFD700)
        e.set_author(name='⭐ StarGPT', icon_url=bot.user.display_avatar.url)
        e.add_field(name=f'💬 {interaction.user.display_name}', value=message[:1020], inline=False)
        e.add_field(name='🤖 StarGPT', value=answer[:1020], inline=False)
        e.set_footer(text=f'Memory: {turns}/10 turns | Model: {AI_MODEL}')
        await interaction.followup.send(embed=e)
    except Exception as ex:
        log.error(f'Chat error: {ex}')
        await interaction.followup.send('❌ Something went wrong. Please try again.', ephemeral=True)

@tree.command(name='ask', description='Ask StarGPT a question (no memory)')
@app_commands.describe(question='Your question')
async def cmd_ask(interaction: discord.Interaction, question: str):
    await interaction.response.defer(thinking=True)
    try:
        res = await groq_client.chat.completions.create(
            model=AI_MODEL,
            messages=[{'role': 'system', 'content': SYSTEM_PROMPT},
                      {'role': 'user', 'content': question}],
            max_tokens=1024, temperature=0.7
        )
        answer = res.choices[0].message.content
        e = discord.Embed(title='⭐ StarGPT', description=answer[:4000], color=0xFFD700)
        e.set_footer(text=f'Model: {AI_MODEL} | {interaction.user.display_name}')
        await interaction.followup.send(embed=e)
    except Exception as ex:
        log.error(f'Ask error: {ex}')
        await interaction.followup.send('❌ Something went wrong. Please try again.', ephemeral=True)

@tree.command(name='clear_history', description='Clear your conversation memory with StarGPT')
async def cmd_clear(interaction: discord.Interaction):
    convo_history[str(interaction.user.id)].clear()
    await interaction.response.send_message('✅ Your conversation memory has been cleared!', ephemeral=True)

@tree.command(name='setup_chat', description='Set a channel as the AI chat channel (like a ChatGPT channel)')
@app_commands.describe(channel='The channel to use as AI chat')
@app_commands.default_permissions(manage_channels=True)
async def cmd_setup_chat(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_chat_channels[interaction.guild.id] = channel.id
    ai_channels.add(channel.id)
    e = discord.Embed(
        title='✅ AI Chat Channel Set!',
        description=(
            f'{channel.mention} is now an AI chat channel!\n\n'
            f'Just type anything there and StarGPT will reply.\n'
            f'Works just like ChatGPT, but in Discord! ⭐'
        ),
        color=0xFFD700
    )
    await interaction.response.send_message(embed=e)
    await channel.send(embed=discord.Embed(
        title='⭐ StarGPT Chat Channel',
        description='I\'m ready to answer anything!\nJust type your message here. 🤖\n\n_Tip: `/chat` and `/ask` commands work anywhere!_',
        color=0xFFD700
    ).set_footer(text='StarGPT • Powered by Groq AI'))

@tree.command(name='summarize', description='Summarize recent messages in this channel')
@app_commands.describe(count='Number of messages to summarize (max 50)')
@app_commands.default_permissions(manage_messages=True)
async def cmd_summarize(interaction: discord.Interaction, count: int = 20):
    await interaction.response.defer(thinking=True)
    count = min(max(count, 5), 50)
    msgs = []
    async for m in interaction.channel.history(limit=count):
        if not m.author.bot and m.content:
            msgs.append(f'{m.author.display_name}: {m.content}')
    msgs.reverse()
    if not msgs:
        await interaction.followup.send('❌ No messages to summarize.', ephemeral=True)
        return
    prompt = f'Summarize this Discord conversation in 3-5 sentences:\n\n' + '\n'.join(msgs)
    res = await groq_client.chat.completions.create(
        model=AI_MODEL,
        messages=[{'role': 'system', 'content': 'You are a helpful summarizer. Be concise and clear.'},
                  {'role': 'user', 'content': prompt}],
        max_tokens=512
    )
    e = discord.Embed(title=f'📋 Summary of last {count} messages',
                      description=res.choices[0].message.content, color=0xFFD700)
    e.set_footer(text=f'Requested by {interaction.user.display_name}')
    await interaction.followup.send(embed=e)

@tree.command(name='translate', description='Translate text to another language')
@app_commands.describe(text='The text to translate', language='Target language (e.g. Spanish, Japanese, French)')
async def cmd_translate(interaction: discord.Interaction, text: str, language: str = 'English'):
    await interaction.response.defer(thinking=True)
    res = await groq_client.chat.completions.create(
        model=AI_MODEL,
        messages=[{'role': 'system', 'content': f'You are a translator. Translate the given text to {language}. Reply with the translation only, no explanation.'},
                  {'role': 'user', 'content': text}],
        max_tokens=512
    )
    e = discord.Embed(color=0xFFD700)
    e.add_field(name='📝 Original', value=text[:1020], inline=False)
    e.add_field(name=f'🌐 {language}', value=res.choices[0].message.content[:1020], inline=False)
    await interaction.followup.send(embed=e)

@tree.command(name='roast', description='Have StarGPT roast someone 🔥')
@app_commands.describe(user='Who to roast')
async def cmd_roast(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(thinking=True)
    name = user.display_name
    prompt = f'Write a funny roast for someone named {name}. Keep it fun and lighthearted, not too offensive. 2-3 sentences.'
    res = await groq_client.chat.completions.create(
        model=AI_MODEL,
        messages=[{'role': 'system', 'content': 'You are a comedian who writes fun, lighthearted roasts.'},
                  {'role': 'user', 'content': prompt}],
        max_tokens=256
    )
    e = discord.Embed(title=f'🔥 Roasting {name}',
                      description=res.choices[0].message.content, color=0xFF4500)
    e.set_footer(text=f'Requested by {interaction.user.display_name} • Just for fun!')
    await interaction.followup.send(embed=e)

@tree.command(name='advice', description='Get advice from StarGPT')
@app_commands.describe(situation='Describe your situation')
async def cmd_advice(interaction: discord.Interaction, situation: str):
    await interaction.response.defer(thinking=True)
    res = await groq_client.chat.completions.create(
        model=AI_MODEL,
        messages=[{'role': 'system', 'content': 'You are a wise and empathetic friend who gives thoughtful, practical advice.'},
                  {'role': 'user', 'content': f'I need advice about: {situation}'}],
        max_tokens=512
    )
    e = discord.Embed(title='💡 StarGPT Advice',
                      description=res.choices[0].message.content, color=0x00BFFF)
    e.set_footer(text=f'For {interaction.user.display_name}')
    await interaction.followup.send(embed=e)

# ══════════════════════════════════════════════════════════════
#  🛡️ AUTOMOD HELPERS
# ══════════════════════════════════════════════════════════════
def is_whitelisted(member: discord.Member, cfg: dict) -> bool:
    if member.guild_permissions.administrator: return True
    return any(str(r.id) in cfg.get('whitelist_roles', []) for r in member.roles)

async def log_action(guild: discord.Guild, cfg: dict, embed: discord.Embed):
    cid = cfg.get('log_channel')
    if not cid: return
    ch = guild.get_channel(int(cid))
    if ch:
        try: await ch.send(embed=embed)
        except: pass

async def take_action(message: discord.Message, reason: str, cfg: dict):
    warn_counts[message.author.id] += 1
    warns = warn_counts[message.author.id]
    try: await message.delete()
    except: pass
    try:
        await message.channel.send(
            f'⚠️ {message.author.mention} — **{reason}** (Warning #{warns})', delete_after=8)
    except: pass
    e = discord.Embed(color=0xFF0000, timestamp=datetime.now(timezone.utc))
    e.set_author(name=f'🛡️ AutoMod | {reason}')
    e.add_field(name='User', value=f'{message.author.mention} ({message.author.id})')
    e.add_field(name='Channel', value=message.channel.mention)
    e.add_field(name='Warnings', value=str(warns))
    if message.content: e.add_field(name='Message', value=message.content[:500], inline=False)
    if warns >= 10:
        try: await message.author.ban(reason=f'AutoMod: {warns} warnings — {reason}', delete_message_days=1)
        except: pass
        e.add_field(name='Action', value='🔨 BANNED (10+ warnings)', inline=False)
    elif warns >= 7:
        try: await message.author.kick(reason=f'AutoMod: {warns} warnings — {reason}')
        except: pass
        e.add_field(name='Action', value='👢 KICKED (7+ warnings)', inline=False)
    elif warns >= 3:
        try:
            until = datetime.now(timezone.utc) + timedelta(minutes=10)
            await message.author.timeout(until, reason=f'AutoMod: {reason}')
        except: pass
        e.add_field(name='Action', value='⏳ TIMEOUT 10min (3+ warnings)', inline=False)
    else:
        e.add_field(name='Action', value='🗑️ Message Deleted', inline=False)
    await log_action(message.guild, cfg, e)

# AutoMod check functions
def chk_spam(msg, cfg):
    now = datetime.now(timezone.utc); uid = msg.author.id
    thr = cfg.get('spam_threshold', 5); win = cfg.get('spam_window', 5)
    recent = [t for t in msg_history[uid] if (now - t).total_seconds() <= win]
    return f'Spam detected ({len(recent)} msgs in {win}s)' if len(recent) >= thr else None

def chk_flood(msg, cfg):
    now = datetime.now(timezone.utc); uid = msg.author.id
    thr = cfg.get('flood_threshold', 10)
    recent = [t for t in msg_history[uid] if (now - t).total_seconds() <= 10]
    return f'Message flood ({len(recent)} msgs in 10s)' if len(recent) >= thr else None

def chk_caps(msg, cfg):
    c = msg.content; mn = cfg.get('caps_min_length', 10); pct = cfg.get('caps_percent', 70)
    if len(c) < mn: return None
    letters = [x for x in c if x.isalpha()]
    if not letters: return None
    ratio = sum(1 for x in letters if x.i
