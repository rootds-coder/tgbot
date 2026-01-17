import os
import re
import asyncio
import json
import textwrap
from pathlib import Path
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from PIL import Image, ImageDraw, ImageFont

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types.input_stream import AudioPiped
from pytgcalls.types.stream import StreamAudioEnded
from pytgcalls.types import Update as CallUpdate

# ================= FILES =================

PLAYLIST_FILE = "playlists.json"

def load_playlists():
    if not os.path.exists(PLAYLIST_FILE):
        return {}
    with open(PLAYLIST_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_playlists(data):
    with open(PLAYLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# ================= CONFIG =================

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SPOTIFY_ID = os.getenv("SPOTIFY_ID")
SPOTIFY_SECRET = os.getenv("SPOTIFY_SECRET")

SPOTIFY_REGEX = r"open\.spotify\.com/track/"

downloads = Path("downloads")
downloads.mkdir(exist_ok=True)

executor = ThreadPoolExecutor(max_workers=4)
queues = defaultdict(deque)
now_playing = {}

# ================= CLIENTS =================

pyro = Client(
    "music",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

tgcalls = PyTgCalls(pyro)

spotify = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=SPOTIFY_ID,
        client_secret=SPOTIFY_SECRET,
    )
) if SPOTIFY_ID and SPOTIFY_SECRET else None

# ================= HELPERS =================

def generate_cover(title, artist="Unknown"):
    img = Image.new("RGB", (800, 800), (20, 20, 20))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 50)
        font_artist = ImageFont.truetype("DejaVuSans.ttf", 36)
    except Exception:
        font_title = font_artist = ImageFont.load_default()

    wrapped = textwrap.fill(title, width=20)
    draw.text((50, 300), wrapped, fill="white", font=font_title)
    draw.text((50, 450), artist, fill=(180, 180, 180), font=font_artist)

    path = downloads / f"cover_{hash(title)}.jpg"
    img.save(path)
    return path

def player_buttons():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏭ Skip", callback_data="skip"),
            InlineKeyboardButton("⏸ Pause", callback_data="pause"),
            InlineKeyboardButton("▶ Resume", callback_data="resume"),
            InlineKeyboardButton("⏹ Stop", callback_data="stop"),
        ]
    ])

def download_audio(query):
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(downloads / "%(id)s.%(ext)s"),
        "quiet": True,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch1:{query}", download=True)["entries"][0]
        return downloads / f"{info['id']}.mp3", info["title"]

async def spotify_to_query(url):
    if not spotify:
        return url
    track = spotify.track(url)
    return f"{track['name']} {track['artists'][0]['name']}"

async def play_next(chat_id):
    if not queues[chat_id]:
        now_playing.pop(chat_id, None)
        await tgcalls.leave_group_call(chat_id)
        return

    song = queues[chat_id].popleft()
    now_playing[chat_id] = song

    cover = generate_cover(song["title"])
    with open(cover, "rb") as img:
        await app.bot.send_photo(
            chat_id,
            img,
            caption=f"🎵 <b>Now Playing</b>\n{song['title']}",
            parse_mode="HTML",
            reply_markup=player_buttons(),
        )

    await tgcalls.change_stream(chat_id, AudioPiped(str(song["file"])))

# ================= COMMANDS =================

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("/play <song or spotify link>")

    chat_id = update.effective_chat.id
    query = " ".join(context.args)

    if re.search(SPOTIFY_REGEX, query):
        query = await spotify_to_query(query)

    loop = asyncio.get_running_loop()
    audio, title = await loop.run_in_executor(executor, download_audio, query)

    queues[chat_id].append({"file": audio, "title": title})

    if chat_id not in now_playing:
        await tgcalls.join_group_call(chat_id, AudioPiped(str(audio)))
        cover = generate_cover(title)
        await update.message.reply_photo(
            open(cover, "rb"),
            caption=f"🎵 <b>Now Playing</b>\n{title}",
            parse_mode="HTML",
            reply_markup=player_buttons(),
        )
    else:
        await update.message.reply_text(f"➕ Added to queue:\n{title}")

async def skip(update: Update, context):
    await play_next(update.effective_chat.id)

async def stop(update: Update, context):
    chat_id = update.effective_chat.id
    queues[chat_id].clear()
    now_playing.pop(chat_id, None)
    await tgcalls.leave_group_call(chat_id)
    await update.message.reply_text("⏹ Stopped")

async def queue_cmd(update: Update, context):
    q = queues[update.effective_chat.id]
    if not q:
        return await update.message.reply_text("Queue empty")
    text = "\n".join(f"{i+1}. {s['title']}" for i, s in enumerate(q))
    await update.message.reply_text(f"📜 Queue:\n{text}")

async def buttons(update: Update, context):
    q = update.callback_query
    await q.answer()
    cid = q.message.chat.id

    if q.data == "skip":
        await play_next(cid)
    elif q.data == "pause":
        await tgcalls.pause_stream(cid)
    elif q.data == "resume":
        await tgcalls.resume_stream(cid)
    elif q.data == "stop":
        queues[cid].clear()
        await tgcalls.leave_group_call(cid)

@tgcalls.on_stream_end()
async def on_end(_, update: CallUpdate):
    if isinstance(update, StreamAudioEnded):
        await play_next(update.chat_id)

# ================= MAIN =================

app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("play", play))
app.add_handler(CommandHandler("skip", skip))
app.add_handler(CommandHandler("stop", stop))
app.add_handler(CommandHandler("queue", queue_cmd))
app.add_handler(CallbackQueryHandler(buttons))

async def main():
    await pyro.start()
    await tgcalls.start()
    await app.initialize()
    await app.start()
    await asyncio.Event().wait()

asyncio.run(main())
