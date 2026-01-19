import os
import re
import asyncio
import requests
from pathlib import Path
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials

from telegram import (
    Update,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.request import HTTPXRequest

# ================== CONFIG ==================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8204283180:AAGv_3w6QTcRreZ2TBE3URTsXqvLcez1Oi4")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")

downloads_dir = Path("downloads")
downloads_dir.mkdir(exist_ok=True)

executor = ThreadPoolExecutor(max_workers=4)

music_queues = defaultdict(deque)
now_playing = {}

SPOTIFY_REGEX = r"open\.spotify\.com/track/"

# ================== HELPERS ==================

def format_duration(seconds: int) -> str:
    if not seconds:
        return ""
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

def format_caption(title, duration, user):
    return (
        "🎵 <b>Now Playing</b>\n"
        "━━━━━━━━━━━━━━━━\n"
        f"📀 <b>Title:</b> {title}\n"
        f"⏱ <b>Duration:</b> {format_duration(duration)}\n"
        "━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Requested by:</b> {user}"
    )

async def spotify_to_search(url: str) -> str:
    sp = spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=os.getenv("SPOTIFY_ID"),
            client_secret=os.getenv("SPOTIFY_SECRET"),
        )
    )
    track = sp.track(url)
    return f"{track['name']} {track['artists'][0]['name']}"

# ================== COMMANDS ==================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎵 Music Bot\n\n"
        "/play <song or Spotify link>\n"
        "/help"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use /play <song name or Spotify track link>\n"
        "Queue + inline controls supported."
    )

# ================== PLAYER ==================

async def play_next(chat_id, context):
    if not music_queues[chat_id]:
        now_playing.pop(chat_id, None)
        return

    song = music_queues[chat_id].popleft()
    now_playing[chat_id] = song

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⏸ Pause", callback_data="pause"),
            InlineKeyboardButton("ℹ Info", callback_data="info"),
        ]
    ])

    with open(song["file"], "rb") as audio:
        await context.bot.send_audio(
            chat_id=chat_id,
            audio=audio,
            title=song["title"][:64],
            duration=song["duration"],
            caption=format_caption(
                song["title"],
                song["duration"],
                song["user"]
            ),
            parse_mode="HTML",
            reply_markup=buttons,
        )

# ================== /PLAY ==================

async def play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use /play <song>")
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    user_mention = f'<a href="tg://user?id={user.id}">{user.first_name}</a>'

    query = " ".join(context.args)

    if re.search(SPOTIFY_REGEX, query):
        query = await spotify_to_search(query)

    status = await update.message.reply_text(f"🔍 Searching: {query}")

    loop = asyncio.get_running_loop()

    def download():
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(downloads_dir / "%(id)s.%(ext)s"),
            "quiet": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(f"ytsearch1:{query}", download=True)["entries"][0]

    info = await loop.run_in_executor(executor, download)

    audio_file = downloads_dir / f"{info['id']}.mp3"

    song = {
        "title": info["title"],
        "file": audio_file,
        "duration": int(info.get("duration", 0)),
        "user": user_mention,
    }

    music_queues[chat_id].append(song)

    if chat_id not in now_playing:
        await status.delete()
        await play_next(chat_id, context)
    else:
        await status.edit_text(f"➕ Added to queue:\n<b>{song['title']}</b>", parse_mode="HTML")

# ================== BUTTONS ==================

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "pause":
        now_playing.pop(chat_id, None)
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([
                [InlineKeyboardButton("▶ Resume", callback_data="resume")]
            ])
        )

    elif query.data == "resume":
        await query.edit_message_reply_markup(
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                    InlineKeyboardButton("ℹ Info", callback_data="info"),
                ]
            ])
        )
        await play_next(chat_id, context)

    elif query.data == "info":
        song = now_playing.get(chat_id)
        if song:
            await query.answer(
                f"{song['title']}\n⏱ {format_duration(song['duration'])}",
                show_alert=True,
            )

# ================== APP ==================

request = HTTPXRequest(
    connect_timeout=300,
    read_timeout=900,
    write_timeout=1200,
)

app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .request(request)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_cmd))
app.add_handler(CommandHandler("play", play))
app.add_handler(CallbackQueryHandler(buttons))

print("🎵 Bot running...")
app.run_polling()
