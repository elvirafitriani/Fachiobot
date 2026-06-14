#!/usr/bin/env python3
"""
Fachio - Media Relay Downloader
Bot Telegram pengunduh video & audio yang menyalurkan file ke
Telegram / Filebin / Gofile.

- Token diisi otomatis oleh setup.sh (mengganti YOUR_BOT_TOKEN_HERE)
- Download non-blocking (run_in_executor), antrian per-user, throttle progress,
  escape Markdown, keterangan masa berlaku link.
"""

import os
import re
import time
import json
import uuid
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import yt_dlp

TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# ==================== BRAND ====================
BRAND_NAME  = "Fachio"
BRAND_FULL  = "Fachio - Media Relay Downloader"
BRAND_TAGLINE = "Media Relay — link masuk, file tersalur."

# ==================== KONFIGURASI ====================
TELEGRAM_LIMIT_MB = 50
DOWNLOAD_DIR = "downloads"

STORAGE_TELEGRAM = "telegram"
STORAGE_FILEBIN  = "filebin"
STORAGE_GOFILE   = "gofile"

HISTORY_FILE = "user_history.json"

# Label tombol storage (konsisten di semua menu)
LABEL_TELEGRAM = "📲 Kirim ke Telegram"
LABEL_FILEBIN  = "📁 Filebin (6 hari)"
LABEL_GOFILE   = "☁️ Gofile (10 hari)"

# State global
pending: dict[str, dict] = {}              # key -> {url, title, duration, est_mb}
user_queues:  dict[str, asyncio.Queue] = {}
queue_workers: dict[str, asyncio.Task] = {}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==================== FUNGSI BANTU ====================
def extract_url(text: str):
    match = re.search(r'https?://[^\s]+', text)
    return match.group(0) if match else None

def format_bytes(size_bytes):
    if not size_bytes:
        return "N/A"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"

def format_duration(seconds):
    if not seconds:
        return "N/A"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"

def esc(text) -> str:
    """Escape karakter spesial Markdown (legacy) untuk teks dinamis."""
    if text is None:
        return "-"
    text = str(text)
    for ch in ['_', '*', '`', '[', ']']:
        text = text.replace(ch, f"\\{ch}")
    return text

def expiry_note(storage: str) -> str:
    """Keterangan masa berlaku link sesuai storage."""
    if storage == STORAGE_FILEBIN:
        return "⏳ *Link berlaku ±6 hari*, setelah itu file otomatis terhapus."
    if storage == STORAGE_GOFILE:
        return "⏳ *Link berlaku ±10 hari* (selama tidak ada aktivitas)."
    return ""  # Telegram = file menetap di chat

def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_history(history: dict):
    with open(HISTORY_FILE, 'w') as f:
        json.dump(history, f, indent=2)

def add_history(user_id: str, filename: str, storage: str, link: str = ""):
    history = load_history()
    history.setdefault(user_id, [])
    history[user_id].append({
        "filename": filename,
        "storage": storage,
        "link": link,
        "time": time.strftime("%Y-%m-%d %H:%M")
    })
    history[user_id] = history[user_id][-30:]  # simpan maksimal 30 terakhir
    save_history(history)

# ==================== UPLOAD ====================
async def upload_to_filebin(file_path: str, bin_name: str) -> str:
    """Upload ke Filebin. bin_name unik per file (privasi lebih baik)."""
    try:
        import httpx
        filename = os.path.basename(file_path)
        async with httpx.AsyncClient(timeout=300) as client:
            with open(file_path, 'rb') as f:
                response = await client.post(
                    f'{{https://filebin.net/{bin_name}}}/{filename}',
                    content=f.read(),
                    headers={'Content-Type': 'application/octet-stream'}
                )
        if response.status_code in (200, 201):
            return f"{{https://filebin.net/{bin_name}}}"
        return None
    except Exception as e:
        print(f"Filebin error: {e}")
        return None

async def upload_to_gofile(file_path: str) -> str:
    """Upload ke Gofile (guest). Tiap file dapat link unik."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=300) as client:
            server_resp = await client.get('https://api.gofile.io/servers')
            server_data = server_resp.json()
            if server_data.get('status') != 'ok':
                return None
            server = server_data['data']['servers'][0]['name']
            with open(file_path, 'rb') as f:
                upload_resp = await client.post(
                    f'{{https://{server}}}.gofile.io/uploadFile',
                    files={'file': (os.path.basename(file_path), f)}
                )
            data = upload_resp.json()
            if data.get('status') == 'ok':
                return data['data']['downloadPage']
        return None
    except Exception as e:
        print(f"Gofile error: {e}")
        return None

# ==================== QUEUE SYSTEM ====================
async def queue_worker(user_id: str, app):
    queue = user_queues[user_id]
    while True:
        try:
            job = await asyncio.wait_for(queue.get(), timeout=300)
        except asyncio.TimeoutError:
            break
        try:
            await _do_download(app=app, **job)
        except Exception as e:
            print(f"Queue worker error (user {user_id}): {e}")
        finally:
            queue.task_done()
    queue_workers.pop(user_id, None)
    user_queues.pop(user_id, None)

def enqueue_download(user_id: str, app, **job_kwargs):
    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()
    job_kwargs["user_id"] = user_id
    user_queues[user_id].put_nowait(job_kwargs)
    if user_id not in queue_workers or queue_workers[user_id].done():
        queue_workers[user_id] = asyncio.create_task(queue_worker(user_id, app))

def get_queue_position(user_id: str) -> int:
    q = user_queues.get(user_id)
    return q.qsize() if q else 0

# ==================== EXTRACT INFO ====================
def _ydl_extract(url: str) -> dict:
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def estimate_mb(info: dict) -> float:
    size = info.get('filesize') or info.get('filesize_approx') or 0
    return size / (1024 * 1024) if size else 0

# ==================== HANDLER: URL MASUK ====================
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    url = extract_url(text)
    if not url:
        await update.message.reply_text("⚠️ Kirim *link video* yang valid.", parse_mode='Markdown')
        return

    status = await update.message.reply_text("🔎 Menganalisis link...")
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(None, _ydl_extract, url)
    except Exception as e:
        await status.edit_text(f"❌ Gagal membaca link.\n`{esc(str(e)[:150])}`", parse_mode='Markdown')
        return

    key = uuid.uuid4().hex[:8]
    pending[key] = {
        "url": url,
        "title": info.get('title', 'video'),
        "duration": info.get('duration'),
        "est_mb": estimate_mb(info),
    }

    title = pending[key]["title"]
    dur = format_duration(pending[key]["duration"])
    keyboard = [
        [InlineKeyboardButton("🎬 1080p", callback_data=f"fmt|{key}|1080"),
         InlineKeyboardButton("🎬 720p",  callback_data=f"fmt|{key}|720")],
        [InlineKeyboardButton("🎬 480p",  callback_data=f"fmt|{key}|480"),
         InlineKeyboardButton("🎵 MP3",   callback_data=f"fmt|{key}|mp3")],
    ]
    await status.edit_text(
        f"✅ *Ditemukan!*\n\n📄 {esc(title)}\n⏱️ Durasi: {dur}\n\n👇 Pilih kualitas:",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ==================== HANDLER: PILIH KUALITAS ====================
async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, key, quality = query.data.split("|")
    if key not in pending:
        await query.edit_message_text("⚠️ Sesi kedaluwarsa, kirim ulang link-nya ya.")
        return
    pending[key]["quality"] = quality
    est_mb = pending[key]["est_mb"]

    # File kecil → boleh Telegram. Estimasi tidak diketahui → tampilkan semua.
    show_telegram = (quality != "mp3" and est_mb and est_mb > TELEGRAM_LIMIT_MB) is False

    keyboard = []
    if show_telegram:
        keyboard.append([InlineKeyboardButton(LABEL_TELEGRAM, callback_data=f"dl|{STORAGE_TELEGRAM}|{key}")])
    keyboard.append([InlineKeyboardButton(LABEL_FILEBIN, callback_data=f"dl|{STORAGE_FILEBIN}|{key}")])
    keyboard.append([InlineKeyboardButton(LABEL_GOFILE,  callback_data=f"dl|{STORAGE_GOFILE}|{key}")])

    est_txt = f" (≈{est_mb:.0f}MB)" if est_mb else ""
    await query.edit_message_text(
        f"📦 *Pilih tujuan penyimpanan*{est_txt}:\n\n"
        f"• 📲 Telegram → file langsung (maks {TELEGRAM_LIMIT_MB}MB)\n"
        f"• 📁 Filebin → link, berlaku ±6 hari\n"
        f"• ☁️ Gofile → link, berlaku ±10 hari",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ==================== HANDLER: PILIH STORAGE → MASUK ANTRIAN ====================
async def storage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, storage, key = query.data.split("|")
    if key not in pending:
        await query.edit_message_text("⚠️ Sesi kedaluwarsa, kirim ulang link-nya ya.")
        return

    user_id = str(query.from_user.id)
    chat_id = query.message.chat_id
    pos = get_queue_position(user_id)
    wait_txt = f"\n⏳ Posisi antrian: {pos + 1}" if pos > 0 else ""
    await query.edit_message_text(f"📥 Masuk antrian...{wait_txt}")

    enqueue_download(
        user_id=user_id,
        app=context.application,
        chat_id=chat_id,
        key=key,
        storage=storage,
        status_message_id=query.message.message_id,
    )

# ==================== PROSES DOWNLOAD ====================
async def _do_download(app, user_id, chat_id, key, storage, status_message_id):
    data = pending.get(key)
    if not data:
        return
    url = data["url"]
    quality = data.get("quality", "720")
    title = data["title"]
    loop = asyncio.get_running_loop()
    bot = app.bot

    async def edit(text):
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=status_message_id,
                                        text=text, parse_mode='Markdown')
        except Exception:
            pass

    await edit("⬇️ *Memulai download...*")

    # Throttle progress (maks tiap 3 detik)
    last_edit = {"t": 0.0}
    def progress_hook(d):
        if d.get('status') == 'downloading':
            now = time.time()
            if now - last_edit["t"] < 3:
                return
            last_edit["t"] = now
            pct = d.get('_percent_str', '').strip()
            spd = d.get('_speed_str', '').strip()
            asyncio.run_coroutine_threadsafe(edit(f"⬇️ Mengunduh... {pct} @ {spd}"), loop)

    def pp_hook(d):
        if d.get('status') == 'started':
            asyncio.run_coroutine_threadsafe(edit("🎵 Mengonversi ke MP3..."), loop)

    outtmpl = os.path.join(DOWNLOAD_DIR, f"{key}_%(title).80s.%(ext)s")
    if quality == "mp3":
        opts = {
            'format': 'bestaudio/best',
            'outtmpl': outtmpl,
            'quiet': True, 'no_warnings': True,
            'progress_hooks': [progress_hook],
            'postprocessor_hooks': [pp_hook],
            'postprocessors': [{'key': 'FFmpegExtractAudio',
                                'preferredcodec': 'mp3',
                                'preferredquality': '192'}],
        }
    else:
        h = {"1080": 1080, "720": 720, "480": 480}.get(quality, 720)
        opts = {
            'format': f'bestvideo[height<={h}]+bestaudio/best[height<={h}]',
            'outtmpl': outtmpl,
            'merge_output_format': 'mp4',
            'quiet': True, 'no_warnings': True,
            'progress_hooks': [progress_hook],
        }

    def _run_ydl():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.prepare_filename(info), info

    try:
        file_path, info = await loop.run_in_executor(None, _run_ydl)
        if quality == "mp3":
            file_path = os.path.splitext(file_path)[0] + ".mp3"
    except Exception as e:
        await edit(f"❌ Gagal download.\n`{esc(str(e)[:150])}`")
        pending.pop(key, None)
        return

    if not os.path.exists(file_path):
        await edit("❌ File hasil tidak ditemukan.")
        pending.pop(key, None)
        return

    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)
    final_title = info.get('title', title)

    # Auto-fallback kalau pilih Telegram tapi file kebesaran
    if storage == STORAGE_TELEGRAM and file_size_mb > TELEGRAM_LIMIT_MB:
        await edit(f"⚠️ File {file_size_mb:.0f}MB > {TELEGRAM_LIMIT_MB}MB, dialihkan ke Filebin...")
        storage = STORAGE_FILEBIN

    try:
        if storage == STORAGE_TELEGRAM:
            await edit("📤 Mengirim ke Telegram...")
            with open(file_path, 'rb') as f:
                if quality == "mp3":
                    await bot.send_audio(chat_id, f, title=final_title[:60],
                        caption=f"✅ {esc(final_title)}\n💾 {format_bytes(file_size)}\n\n🔀 via {BRAND_NAME}",
                        parse_mode='Markdown', read_timeout=300, write_timeout=300)
                else:
                    await bot.send_document(chat_id, f,
                        caption=f"✅ {esc(final_title)}\n💾 {format_bytes(file_size)}\n\n🔀 via {BRAND_NAME}",
                        parse_mode='Markdown', read_timeout=300, write_timeout=300)
            await edit("✅ *Selesai dikirim!*")
            add_history(user_id, final_title, storage)
        else:
            await edit(f"📤 Mengupload ke {storage.title()}...")
            if storage == STORAGE_FILEBIN:
                bin_name = f"dl{key}{uuid.uuid4().hex[:6]}"  # unik per file
                link = await upload_to_filebin(file_path, bin_name)
            else:
                link = await upload_to_gofile(file_path)

            if not link:
                await edit(f"❌ Gagal upload ke {storage.title()}. Coba lagi nanti.")
            else:
                note = expiry_note(storage)
                msg = (
                    "✅ *Download selesai!*\n\n"
                    f"📄 {esc(final_title)}\n"
                    f"💾 Ukuran: {format_bytes(file_size)}\n"
                    f"🔗 Link: {link}"
                )
                if note:
                    msg += f"\n\n{note}"
                msg += f"\n\n🔀 _via {BRAND_NAME}_"
                await edit(msg)
                add_history(user_id, final_title, storage, link)
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass
        pending.pop(key, None)

# ==================== COMMANDS & MENU ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔗 Riwayat Download", callback_data="menu|history")],
        [InlineKeyboardButton("❓ Bantuan", callback_data="menu|help")],
    ]
    await update.message.reply_text(
        f"🔀 *{BRAND_NAME}* — _Media Relay Downloader_\n"
        f"_{BRAND_TAGLINE}_\n\n"
        "Kirim link video, bot akan unduhkan & salurkan otomatis.\n\n"
        f"• ≤{TELEGRAM_LIMIT_MB}MB → Telegram / Filebin / Gofile\n"
        f"• >{TELEGRAM_LIMIT_MB}MB → Filebin / Gofile\n\n"
        "👇 Langsung kirim link videonya!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _send_history(update.effective_user.id, update.message.reply_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(_help_text(), parse_mode='Markdown')

def _help_text() -> str:
    return (
        f"❓ *Bantuan — {BRAND_FULL}*\n\n"
        "1. Kirim link video apa saja.\n"
        "2. Pilih kualitas (1080p/720p/480p/MP3).\n"
        "3. Pilih tujuan: Telegram / Filebin / Gofile.\n\n"
        f"📲 Telegram: maks {TELEGRAM_LIMIT_MB}MB\n"
        "📁 Filebin: link berlaku ±6 hari\n"
        "☁️ Gofile: link berlaku ±10 hari\n\n"
        "Perintah: /start /history /help"
    )

async def _send_history(uid, reply_func):
    user_id = str(uid)
    history = load_history()
    items = history.get(user_id, [])
    if not items:
        await reply_func("📭 *Belum ada riwayat download.*", parse_mode='Markdown')
        return
    text = "*📋 Riwayat Download (10 terakhir):*\n\n"
    for i, item in enumerate(items[-10:], 1):
        line = f"{i}. {esc(item['filename'][:35])} — _{item['storage']}_"
        if item.get('link'):
            line += f"\n   🔗 {item['link']}"
        text += line + "\n"
    await reply_func(text, parse_mode='Markdown')

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, action = query.data.split("|")
    if action == "history":
        await _send_history(query.from_user.id,
                            lambda t, **k: context.bot.send_message(query.message.chat_id, t, **k))
    elif action == "help":
        await context.bot.send_message(query.message.chat_id, _help_text(), parse_mode='Markdown')

# ==================== MAIN ====================
def main():
    if TOKEN == "YOUR_BOT_TOKEN_HERE" or not TOKEN:
        print("❌ BOT_TOKEN belum diisi! Jalankan setup.sh atau set env BOT_TOKEN.")
        return

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(quality_callback, pattern=r"^fmt\|"))
    app.add_handler(CallbackQueryHandler(storage_callback, pattern=r"^dl\|"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    print(f"🤖 {BRAND_FULL} berjalan...")
    app.run_polling()

if __name__ == "__main__":
    main()
