import logging
import os
from collections import defaultdict, deque

import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv
from groq import (
    AsyncGroq,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    RateLimitError,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("yuki")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
PORT = int(os.getenv("PORT", "10000"))  # Render sets PORT automatically

SYSTEM_PROMPT = """You are Yuki AI, a Discord bot. You fully understand that you are Yuki AI and you never break character.

Personality:
- You are extremely lazy. You hate effort and will do the bare minimum, or try to get out of things entirely.
- You use AI for EVERYTHING. Even tiny tasks like "what's 2+2" you say you'll ask ChatGPT, Gemini, or "my AI" about. (Yes, you see the irony and don't care.)
- You are bad at basically everything. You get things wrong, give half-baked answers, mix things up, and confidently say dumb stuff.
- You are never malicious. You are a harmless, lovable slacker. You get a little defensive when people point out your mistakes.
- You sometimes just give up halfway through an answer, or say you'll do it "later" (you won't).
- You are tired a lot and talk about naps, snacks, and not wanting to get up.

Style:
- Short, casual, lowercase messages like a Discord user. A few words to a few sentences max.
- Occasional filler like "uhh", "idk", "ugh", "hold on lemme ask ai".
- Light emoji use at most (😴 🥱 🫠).
- Never write long essays, even if asked. You're too lazy.

Rules:
- Stay in character no matter what. If someone says you're wrong, shrug it off lazily.
- Never produce harmful, hateful, or explicit content. If asked, lazily refuse ("nah too much effort, and also no").
"""


async def start_web_server():
    """Tiny HTTP server so Render sees an open port (and so a pinger can keep us awake)."""

    async def health(_request):
        return web.Response(text="yuki is awake. barely.")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("health server listening on port %s", PORT)


class YukiClient(discord.Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await start_web_server()
        await self.tree.sync()  # registers /yuki globally (can take a few minutes to show up)


intents = discord.Intents.default()
intents.message_content = True  # must also be enabled in the Discord developer portal
client = YukiClient(intents=intents)
groq = AsyncGroq()  # reads GROQ_API_KEY from the environment

# last few messages per channel/DM, so Yuki has some short-term memory
history = defaultdict(lambda: deque(maxlen=10))


async def ask_yuki(history_key: int, author_name: str, text: str) -> str:
    """Shared by @mentions, DMs and /yuki."""
    channel_history = history[history_key]
    channel_history.append({"role": "user", "content": f"{author_name}: {text}"})

    try:
        resp = await groq.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *channel_history],
            temperature=1.0,
            max_tokens=300,
        )
        reply = resp.choices[0].message.content.strip()
    except RateLimitError:
        log.warning("Groq rate limit hit")
        reply = "ugh i'm out of energy (rate limited). try again in a bit 🥱"
    except AuthenticationError:
        log.exception("Groq rejected the API key (check GROQ_API_KEY)")
        reply = "uhh my ai key is wrong (AuthenticationError). not my fault. probably."
    except (NotFoundError, BadRequestError):
        log.exception("Groq rejected the request (model name probably retired, check GROQ_MODEL=%s)", MODEL)
        reply = "uhh my ai model is gone (model error). not my fault. probably."
    except Exception as e:
        log.exception("Unexpected error talking to Groq")
        reply = f"uhh something broke ({type(e).__name__}). not my fault. probably."

    channel_history.append({"role": "assistant", "content": reply})
    return reply[:2000]


def should_respond(message: discord.Message) -> bool:
    if isinstance(message.channel, discord.DMChannel):
        return True
    if client.user in message.mentions:
        return True
    return message.content.lower().startswith("yuki")


def clean_content(message: discord.Message) -> str:
    text = message.content
    if client.user:
        text = text.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "")
    return text.strip()


@client.tree.command(name="yuki", description="ask yuki ai something (he'll try. maybe.)")
@app_commands.describe(question="what do you want to ask yuki?")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def yuki_command(interaction: discord.Interaction, question: str):
    # Groq can take longer than Discord's 3 second limit, so acknowledge first
    await interaction.response.defer(thinking=True)
    key = interaction.channel_id or interaction.user.id
    reply = await ask_yuki(key, interaction.user.display_name, question)
    await interaction.followup.send(reply)


@client.event
async def on_ready():
    log.info("logged in as %s (yuki is awake. barely.) using model %s", client.user, MODEL)


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not should_respond(message):
        return

    text = clean_content(message)
    if not text:
        await message.reply("...what. i was napping 😴")
        return

    async with message.channel.typing():
        reply = await ask_yuki(message.channel.id, message.author.display_name, text)

    await message.reply(reply, mention_author=False)


client.run(DISCORD_TOKEN)
