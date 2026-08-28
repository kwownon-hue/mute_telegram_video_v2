import asyncio
import logging
import os
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from telegram.error import Conflict

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from instagram_uploader import InstagramUploader
from youtube_uploader import YouTubeUploader

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

default_publisher_dir = BASE_DIR.parent / "youube"
PUBLISHER_CONFIG_DIR = Path(
    os.getenv(
        "PUBLISHER_CONFIG_DIR",
        str(default_publisher_dir if default_publisher_dir.is_dir() else BASE_DIR),
    )
)
if PUBLISHER_CONFIG_DIR != BASE_DIR:
    load_dotenv(PUBLISHER_CONFIG_DIR / ".env", override=False)
os.environ.setdefault("PUBLISHER_CONFIG_DIR", str(PUBLISHER_CONFIG_DIR))

TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
TEMP_DIR = Path(os.getenv("TEMP_DIR", "temp_videos"))
if not TEMP_DIR.is_absolute():
    TEMP_DIR = BASE_DIR / TEMP_DIR
MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB", "50"))
CONCURRENT_LIMIT = int(os.getenv("CONCURRENT_LIMIT", "3"))

MODE_MUTE = "mute"
MODE_MUTE_YOUTUBE = "mute_youtube"
MODE_MUTE_INSTAGRAM = "mute_instagram"
MODE_INSTAGRAM = "instagram"
DEFAULT_MODE = MODE_MUTE

MODE_LABELS = {
    MODE_MUTE: "كتم الصوت وإعادة الفيديو",
    MODE_MUTE_YOUTUBE: "كتم الصوت، إعادة الفيديو، والنشر على يوتيوب",
    MODE_MUTE_INSTAGRAM: "كتم الصوت والنشر على إنستغرام",
    MODE_INSTAGRAM: "نشر الفيديو الأصلي على إنستغرام",
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

TEMP_DIR.mkdir(parents=True, exist_ok=True)
process_semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
youtube_upload_lock = asyncio.Lock()
instagram_upload_lock = asyncio.Lock()


def is_allowed(update: Update) -> bool:
    configured_ids = (
        os.getenv("ALLOWED_USER_IDS") or os.getenv("ALLOWED_USER_ID", "")
    ).strip()
    if not configured_ids:
        return True

    allowed_ids = {item.strip() for item in configured_ids.split(",") if item.strip()}
    return bool(update.effective_user and str(update.effective_user.id) in allowed_ids)


def selected_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", DEFAULT_MODE)


def mode_keyboard(current_mode: str) -> InlineKeyboardMarkup:
    buttons = []
    for mode, label in MODE_LABELS.items():
        marker = "[x]" if mode == current_mode else "[ ]"
        buttons.append(
            [InlineKeyboardButton(f"{marker} {label}", callback_data=f"mode:{mode}")]
        )
    return InlineKeyboardMarkup(buttons)


def parse_caption(caption: str) -> tuple[str, str, list[str]]:
    lines = [line.strip() for line in caption.strip().splitlines()]
    title = lines[0] if lines else "فيديو جديد"
    description = "\n".join(lines[1:]).strip() if lines else ""
    tags = [word[1:] for word in caption.split() if word.startswith("#") and len(word) > 1]
    return title[:100], description, tags


def format_error(error: object) -> str:
    text = " ".join(str(error).split()) or "Unknown error"
    return text if len(text) <= 250 else f"{text[:247]}..."


def find_ffmpeg() -> Optional[str]:
    configured_path = os.getenv("FFMPEG_PATH")
    if configured_path:
        resolved_path = shutil.which(configured_path)
        if resolved_path:
            return resolved_path
        if Path(configured_path).is_file():
            return configured_path

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_allowed(update):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    mode = selected_mode(context)
    await update.message.reply_text(
        "أرسل فيديو مباشرة أو كملف.\n\n"
        f"الوضع الحالي: {MODE_LABELS[mode]}\n"
        f"الحد الأقصى للحجم: {MAX_SIZE_MB} ميجابايت\n\n"
        "استخدم /mode لاختيار ما يفعله البوت بكل فيديو."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_allowed(update):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return
    await update.message.reply_text(
        "1. استخدم /mode واختر الإجراء المطلوب.\n"
        "2. أرسل الفيديو مباشرة أو كملف فيديو.\n"
        "3. يستخدم يوتيوب وإنستغرام النصوص الثابتة من المشروع القديم.\n\n"
        "الأوامر: /start و /mode و /help"
    )


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_allowed(update):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    mode = selected_mode(context)
    await update.message.reply_text(
        f"اختر وضع العمل. الوضع الحالي: {MODE_LABELS[mode]}",
        reply_markup=mode_keyboard(mode),
    )


async def handle_mode_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_allowed(update):
        await query.edit_message_text("غير مصرح لك باستخدام هذا البوت.")
        return

    mode = query.data.removeprefix("mode:")
    if mode not in MODE_LABELS:
        await query.edit_message_text("اختيار غير صالح.")
        return

    context.user_data["mode"] = mode
    await query.edit_message_text(
        f"تم اختيار الوضع: {MODE_LABELS[mode]}",
        reply_markup=mode_keyboard(mode),
    )


async def remove_audio(input_path: Path, output_path: Path) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
    except asyncio.TimeoutError as error:
        process.kill()
        await process.wait()
        raise RuntimeError("Video processing timed out") from error

    if process.returncode != 0:
        details = stderr.decode(errors="replace")[-1000:]
        raise RuntimeError(f"ffmpeg failed: {details}")


async def prepare_instagram_video(
    input_path: Path,
    output_path: Path,
    mute: bool,
) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg is unavailable")

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
    ]
    if mute:
        command.append("-an")
    else:
        command.extend(["-map", "0:a:0?", "-c:a", "aac", "-b:a", "128k"])
    command.extend(
        [
            "-vf",
            "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=900)
    except asyncio.TimeoutError as error:
        process.kill()
        await process.wait()
        raise RuntimeError("Instagram video conversion timed out") from error

    if process.returncode != 0:
        details = stderr.decode(errors="replace")[-1000:]
        raise RuntimeError(f"Instagram video conversion failed: {details}")


async def send_muted_video(message, path: Path) -> None:
    with path.open("rb") as video:
        await message.reply_video(
            video=video,
            caption="تمت إزالة الصوت بنجاح.",
            supports_streaming=True,
            read_timeout=300,
            write_timeout=300,
        )


async def publish_youtube(path: Path, title: str, description: str, tags: list[str]) -> str:
    async with youtube_upload_lock:
        uploader = YouTubeUploader.from_environment()
        return await asyncio.to_thread(uploader.upload, path, title, description, tags)


async def publish_instagram(path: Path, caption: str) -> str:
    async with instagram_upload_lock:
        uploader = InstagramUploader.from_environment()
        return await asyncio.to_thread(uploader.publish_reel, path, caption)


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return
    if not is_allowed(update):
        await message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    file_obj = message.video
    if not file_obj and message.document:
        mime_type = message.document.mime_type or ""
        if mime_type.startswith("video/"):
            file_obj = message.document

    if not file_obj:
        await message.reply_text("الرجاء إرسال ملف فيديو صحيح.")
        return
    if file_obj.file_size and file_obj.file_size > MAX_SIZE_MB * 1024 * 1024:
        await message.reply_text(f"حجم الفيديو يتجاوز {MAX_SIZE_MB} ميجابايت.")
        return

    mode = selected_mode(context)
    unique_id = uuid.uuid4().hex
    input_path = TEMP_DIR / f"{unique_id}_input.mp4"
    muted_path = TEMP_DIR / f"{unique_id}_muted.mp4"
    instagram_path = TEMP_DIR / f"{unique_id}_instagram.mp4"
    status = await message.reply_text(
        f"جاري تحميل الفيديو...\nالوضع: {MODE_LABELS[mode]}"
    )
    title, description, tags = parse_caption(message.caption or "")

    try:
        telegram_file = await context.bot.get_file(
            file_obj.file_id,
            read_timeout=120,
            connect_timeout=120,
        )
        await telegram_file.download_to_drive(
            input_path,
            read_timeout=300,
            connect_timeout=120,
        )

        upload_path = input_path
        if mode in (MODE_MUTE, MODE_MUTE_YOUTUBE):
            async with process_semaphore:
                await status.edit_text("جاري إزالة الصوت...")
                await remove_audio(input_path, muted_path)
            upload_path = muted_path
        elif mode in (MODE_MUTE_INSTAGRAM, MODE_INSTAGRAM):
            instagram_muted = mode == MODE_MUTE_INSTAGRAM
            action = "بدون صوت" if instagram_muted else "مع الصوت"
            async with process_semaphore:
                await status.edit_text(
                    f"جاري تجهيز فيديو متوافق مع إنستغرام {action}..."
                )
                await prepare_instagram_video(input_path, instagram_path, instagram_muted)
            upload_path = instagram_path

        if mode in (MODE_MUTE, MODE_MUTE_YOUTUBE):
            await status.edit_text("جاري إرسال الفيديو الصامت إلى تيليجرام...")
            await send_muted_video(message, muted_path)

        if mode == MODE_MUTE_YOUTUBE:
            await status.edit_text("جاري رفع الفيديو الصامت إلى يوتيوب...")
            url = await publish_youtube(upload_path, title, description, tags)
            await status.edit_text(
                f"تمت إعادة الفيديو الصامت ونشره على يوتيوب:\n{url}"
            )
        elif mode in (MODE_MUTE_INSTAGRAM, MODE_INSTAGRAM):
            action = "الفيديو الصامت" if mode == MODE_MUTE_INSTAGRAM else "الفيديو الأصلي"
            await status.edit_text(f"جاري نشر {action} على إنستغرام...")
            media_id = await publish_instagram(upload_path, message.caption or "")
            await status.edit_text(
                f"تم نشر {action} على إنستغرام.\nمعرف المنشور: {media_id}"
            )
        else:
            await status.delete()
    except Exception as error:
        logger.exception("Video workflow failed in mode %s", mode)
        await status.edit_text(f"فشلت معالجة الفيديو: {format_error(error)}")
    finally:
        for path in (input_path, muted_path, instagram_path):
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("Could not delete %s: %s", path, error)


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "عرض حالة البوت"),
        BotCommand("mode", "اختيار إجراء الفيديو"),
        BotCommand("help", "عرض طريقة الاستخدام"),
    ]
    await application.bot.set_my_commands(commands)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Conflict = another instance is polling with same token. Don't spam stacktrace,
    # just log a clean warning (Updater will retry automatically).
    if isinstance(context.error, Conflict):
        logger.warning(
            "Conflict: another getUpdates request is running with this token. "
            "Make sure only ONE bot instance is running (stop local bot if deployed on Render)."
        )
        return
    logger.error("Exception while handling an update:", exc_info=context.error)


def _start_health_server(port: int) -> None:
    """Tiny HTTP server so Render Web Service health checks pass when using polling."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK - bot is running (polling mode)")

        def do_HEAD(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()

        def log_message(self, format, *args):  # noqa: A002
            return

    try:
        server = HTTPServer(("0.0.0.0", port), Handler)
    except OSError as exc:
        logger.warning("Health server could not bind to port %s: %s", port, exc)
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health-server")
    thread.start()
    logger.info("Health check server listening on 0.0.0.0:%s", port)


def main() -> None:
    if not TOKEN:
        raise RuntimeError("Set BOT_TOKEN (or TELEGRAM_BOT_TOKEN) before starting the bot")
    if not find_ffmpeg():
        raise RuntimeError("ffmpeg is required and must be available in PATH")

    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Event loop is closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    application = Application.builder().token(TOKEN).post_init(post_init).build()
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("mode", mode_command))
    application.add_handler(CallbackQueryHandler(handle_mode_choice, pattern=r"^mode:"))
    application.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video))

    # ---- Render deployment fix ----
    # Telegram allows ONLY ONE concurrent getUpdates per token.
    # Causes of "Conflict: terminated by other getUpdates request":
    # 1) Bot running locally + on Render at same time (most common)
    # 2) Two Render services/containers with same BOT_TOKEN
    # 3) Old webhook still set (run_polling will delete it if drop_pending_updates=True,
    #    but explicit delete + health server helps).
    # For Render: use either
    #   - Background Worker + polling (recommended for bots) OR
    #   - Web Service   + webhook  (set WEBHOOK_URL)
    webhook_url = (os.getenv("WEBHOOK_URL") or "").strip().rstrip("/")
    use_webhook_flag = os.getenv("USE_WEBHOOK", "").strip().lower() in ("1", "true", "yes", "on")
    render_external_url = (os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")

    # Allow RENDER_EXTERNAL_URL to act as WEBHOOK_URL when USE_WEBHOOK is enabled
    if not webhook_url and use_webhook_flag and render_external_url:
        webhook_url = render_external_url

    port_env = os.getenv("PORT", "").strip()
    try:
        port = int(port_env) if port_env else 0
    except ValueError:
        logger.warning("Invalid PORT value %r, ignoring", port_env)
        port = 0

    if webhook_url:
        # Webhook mode - correct for Render Web Service (free tier)
        webhook_path = os.getenv("WEBHOOK_PATH", "webhook").strip().strip("/")
        if not webhook_path:
            webhook_path = "webhook"
        # Use first 12 chars of token as simple secret path if user didn't customize
        # (keeps URL unguessable). User can override via WEBHOOK_PATH.
        full_webhook_url = f"{webhook_url}/{webhook_path}"
        listen_port = port if port else 10000
        logger.info("Starting in WEBHOOK mode: %s on 0.0.0.0:%s", full_webhook_url, listen_port)
        logger.info("If you want polling instead, unset WEBHOOK_URL/USE_WEBHOOK and deploy as Background Worker")
        application.run_webhook(
            listen="0.0.0.0",
            port=listen_port,
            url_path=webhook_path,
            webhook_url=full_webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # Polling mode - correct for local dev and Render Background Worker
        if port and os.getenv("RENDER", "").lower() == "true":
            # Render Web Service expects PORT to be bound, otherwise it restarts the
            # container -> 2 containers briefly overlap -> Conflict. Start dummy server.
            logger.warning(
                "PORT=%s is set and RENDER=true but WEBHOOK_URL is empty. "
                "You are running POLLING on a Render Web Service. "
                "This works but you MUST add a health server (starting one now). "
                "Recommended: either (a) switch this service to Background Worker (paid) "
                "or (b) set WEBHOOK_URL=https://<your-app>.onrender.com and USE_WEBHOOK=true",
                port,
            )
            _start_health_server(port)
        elif port and not webhook_url:
            # Also handle local PORT set without RENDER flag (e.g., manual test)
            _start_health_server(port)

        logger.info("Bot is running in POLLING mode (drop_pending_updates=True)")
        logger.info("If you see 'Conflict' errors, stop ALL other instances using this token (local PC, old Render deploy, @BotFather webhook)")
        application.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)



if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        pass

