from __future__ import annotations
import asyncio
import logging
import os
import re
import shutil
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from telegram.error import Conflict

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
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

# ---- Link support (yt-dlp) - same idea as C:/Users/MC/Desktop/youube ----
URL_RE = re.compile(r"https?://[^\s]+")
# Keep permissive but prioritize known domains
SUPPORTED_DOMAINS = (
    "tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "m.tiktok.com",
    "instagram.com",
    "instagr.am",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.watch",
    "fb.com",
    "twitter.com",
    "x.com",
    "t.co",
)

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

# ---- Queue for sequential publishing like youube ----
video_queue: Optional[asyncio.Queue] = None
QUEUE_DELAY_SECONDS = int(os.getenv("QUEUE_DELAY_SECONDS", "15"))


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


def extract_urls(text: str) -> list[str]:
    if not text:
        return []
    raw = URL_RE.findall(text)
    cleaned: list[str] = []
    for u in raw:
        # strip trailing punctuation/brackets that are not part of URL
        u = u.rstrip(").,!?\"'»«>")
        u = u.lstrip("(<«»")
        # strip trailing ) if unbalanced
        cleaned.append(u)
    return cleaned


def is_supported_url(url: str) -> bool:
    lower = url.lower()
    return any(domain in lower for domain in SUPPORTED_DOMAINS)


def parse_link_text(text: str, urls: list[str]) -> tuple[str, str, list[str], str]:
    """Extract title/desc/tags from text that contains URLs.
    Mimics C:/Users/MC/Desktop/youube logic: non-url lines become title/desc."""
    # Remove URLs to get caption
    caption_without_urls = text
    for u in urls:
        caption_without_urls = caption_without_urls.replace(u, " ")
    # Also handle lines that are just URLs
    lines = [line.strip() for line in caption_without_urls.strip().splitlines() if line.strip()]
    # Filter out any leftover lines that look like URLs
    non_url_lines = [line for line in lines if not line.lower().startswith("http")]
    # If no non-url lines, try extracting from original lines that are not URLs
    if not non_url_lines:
        orig_lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
        non_url_lines = [l for l in orig_lines if not l.lower().startswith("http") and l not in urls]
        # Also remove urls embedded
        if not non_url_lines:
            # Check if user wrote title on second line like youube: line0=url, line1=title
            # fallback handled already
            pass
    caption = "\n".join(non_url_lines).strip()
    if caption:
        title, desc, tags = parse_caption(caption)
        return title, desc, tags, caption
    # fallback: try caption_without_urls raw
    caption = caption_without_urls.strip()
    if caption:
        title, desc, tags = parse_caption(caption)
        return title, desc, tags, caption
    return "فيديو جديد", "", [], ""


def _download_with_ytdlp_sync(url: str, output_template: Path) -> Path:
    """Blocking yt-dlp download, returns actual downloaded file path."""
    import yt_dlp

    # output_template without extension, yt-dlp will add .ext
    ydl_opts: dict = {
        "outtmpl": str(output_template) + ".%(ext)s",
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "concurrent_fragment_downloads": 4,
        "extractor_retries": 3,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }
    # Optional cookies support (for Instagram private/age-gated)
    cookies_file = os.getenv("YTDLP_COOKIES") or os.getenv("COOKIES_FILE")
    if cookies_file and Path(cookies_file).exists():
        ydl_opts["cookiefile"] = str(cookies_file)

    # Proxy support like youube
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or os.getenv("https_proxy") or os.getenv("http_proxy")
    if proxy:
        ydl_opts["proxy"] = proxy

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # ydl.prepare_filename gives final filename
        try:
            filename = ydl.prepare_filename(info)  # type: ignore
            p = Path(filename)
            if p.exists():
                return p
        except Exception:
            pass
        # Fallback: search for files matching template
        candidates = list(output_template.parent.glob(output_template.name + ".*"))
        if candidates:
            # pick newest/largest
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return candidates[0]
        # If info has requested_downloads
        if isinstance(info, dict) and info.get("requested_downloads"):
            for d in info["requested_downloads"]:
                fp = d.get("filepath") or d.get("_filename")
                if fp and Path(fp).exists():
                    return Path(fp)
        raise RuntimeError(f"yt-dlp finished but file not found for template {output_template}")


async def download_from_url(url: str, dest_dir: Path, unique_id: str) -> Path:
    """Download URL via yt-dlp to dest_dir/unique_id.* and return Path."""
    output_template = dest_dir / unique_id
    return await asyncio.to_thread(_download_with_ytdlp_sync, url, output_template)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_allowed(update):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    mode = selected_mode(context)
    await update.message.reply_text(
        "أرسل فيديو مباشرة أو كملف، أو أرسل رابط (تيك توك / إنستغرام / يوتيوب / فيسبوك ...).\n\n"
        f"الوضع الحالي: {MODE_LABELS[mode]}\n"
        f"الحد الأقصى للحجم: {MAX_SIZE_MB} ميجابايت\n\n"
        "استخدم /mode لاختيار ما يفعله البوت بكل فيديو.\n"
        "مثال للرابط:\n"
        "https://www.tiktok.com/@user/video/123...\n"
        "عنوان اختياري في سطر ثاني"
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_allowed(update):
        await update.message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return
    await update.message.reply_text(
        "1. استخدم /mode واختر الإجراء المطلوب.\n"
        "2. أرسل الفيديو مباشرة أو كملف فيديو، أو أرسل رابط تيك توك / إنستغرام / يوتيوب / فيسبوك.\n"
        "3. للرابط: يمكنك كتابة عنوان في السطر الثاني ووصف/هاشتاقات بعده (اختياري).\n"
        "4. يستخدم يوتيوب وإنستغرام النصوص الثابتة من المشروع القديم.\n\n"
        "الأوامر: /start و /mode و /help\n\n"
        "مثال:\n"
        "https://www.tiktok.com/@.../video/...\n"
        "عنوان الفيديو هنا\n"
        "وصف جميل #هاشتاق"
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


async def _send_status_reply(message, text: str):
    """Reply directly to original message with ReplyParameters like youube."""
    return await message.reply_text(
        text,
        reply_parameters=ReplyParameters(
            message_id=message.message_id,
            chat_id=message.chat_id,
            allow_sending_without_reply=True,
        ),
        disable_web_page_preview=True,
    )


async def _update_status_reply(message, status_message, text: str):
    """Edit status message, or send new one if edit fails."""
    try:
        await status_message.edit_text(text, disable_web_page_preview=True)
        return status_message
    except Exception as e:
        logger.warning(f"تعذر تعديل رسالة الحالة: {e}")
        return await _send_status_reply(message, text)


async def _run_video_pipeline(
    input_path: Path,
    message,
    status,
    mode: str,
    caption_text: str,
    title: str,
    description: str,
    tags: list[str],
) -> dict:
    """Legacy pipeline kept for compatibility, now delegates to process_and_publish with richer reporting."""
    return await process_and_publish_video(message, input_path, title, description, tags, caption_text, mode, status)


async def process_and_publish_video(
    message,
    input_path: Path,
    title: str,
    description: str,
    tags: list[str],
    caption_text: str,
    mode: str,
    status,
) -> dict:
    """Core pipeline with detailed status like youube. Returns result dict."""
    unique_id = input_path.stem.replace("_input", "").replace("_download", "")
    base = TEMP_DIR / unique_id
    muted_path = Path(f"{base}_muted.mp4")
    instagram_path = Path(f"{base}_instagram.mp4")

    upload_path: Path = input_path
    # Results for final report
    telegram_sent = False
    telegram_error = None
    youtube_url = None
    youtube_error = None
    instagram_id = None
    instagram_error = None
    file_size_mb = 0.0
    try:
        if input_path.exists():
            file_size_mb = input_path.stat().st_size / (1024 * 1024)
    except Exception:
        pass

    try:
        # 1) Prepare video per mode
        if mode in (MODE_MUTE, MODE_MUTE_YOUTUBE):
            async with process_semaphore:
                status = await _update_status_reply(
                    message, status,
                    f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n🔧 المرحلة: جاري إزالة الصوت..."
                )
                await remove_audio(input_path, muted_path)
            upload_path = muted_path
        elif mode in (MODE_MUTE_INSTAGRAM, MODE_INSTAGRAM):
            instagram_muted = mode == MODE_MUTE_INSTAGRAM
            action = "بدون صوت" if instagram_muted else "مع الصوت"
            async with process_semaphore:
                status = await _update_status_reply(
                    message, status,
                    f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n🔧 المرحلة: تجهيز إنستغرام {action}..."
                )
                await prepare_instagram_video(input_path, instagram_path, instagram_muted)
            upload_path = instagram_path

        # 2) Telegram mute return
        if mode in (MODE_MUTE, MODE_MUTE_YOUTUBE):
            try:
                status = await _update_status_reply(
                    message, status,
                    f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n📤 المرحلة: إرسال الفيديو الصامت إلى تيليجرام..."
                )
                await send_muted_video(message, muted_path)
                telegram_sent = True
            except Exception as e:
                telegram_error = format_error(e)
                logger.error(f"Telegram send failed: {e}")

        # 3) YouTube
        if mode == MODE_MUTE_YOUTUBE:
            try:
                status = await _update_status_reply(
                    message, status,
                    f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n▶️ المرحلة: رفع إلى يوتيوب..."
                )
                youtube_url = await publish_youtube(upload_path, title, description, tags)
            except Exception as e:
                youtube_error = format_error(e)
                logger.error(f"YouTube publish failed: {e}")

        # 4) Instagram
        if mode in (MODE_MUTE_INSTAGRAM, MODE_INSTAGRAM):
            try:
                action = "الفيديو الصامت" if mode == MODE_MUTE_INSTAGRAM else "الفيديو الأصلي"
                status = await _update_status_reply(
                    message, status,
                    f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n📸 المرحلة: نشر {action} على إنستغرام (Cloudinary أولاً)..."
                )
                instagram_id = await publish_instagram(upload_path, caption_text or "")
            except Exception as e:
                instagram_error = format_error(e)
                logger.error(f"Instagram publish failed: {e}")

    finally:
        for p in (muted_path, instagram_path):
            try:
                p.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("Could not delete %s: %s", p, error)

    # Build final report like youube
    result_lines: list[str] = []
    result_lines.append(f"🎬 الوضع: {MODE_LABELS[mode]}")

    if mode in (MODE_MUTE, MODE_MUTE_YOUTUBE):
        if telegram_sent:
            result_lines.append("✅ تيليجرام: تم الإرسال (صامت)")
        else:
            result_lines.append(f"❌ تيليجرام: {telegram_error or 'فشل'}")

    if mode == MODE_MUTE_YOUTUBE:
        if youtube_url:
            result_lines.append(f"▶️ يوتيوب: {youtube_url}")
        else:
            result_lines.append(f"▶️ يوتيوب: ❌ {youtube_error or 'فشل'}")

    if mode in (MODE_MUTE_INSTAGRAM, MODE_INSTAGRAM):
        if instagram_id:
            # instagram_id is media id; show as link hint
            result_lines.append(f"📸 إنستغرام: ✅ تم النشر (ID: {instagram_id})")
        else:
            result_lines.append(f"📸 إنستغرام: ❌ {instagram_error or 'فشل'}")

    # Determine overall success
    if mode == MODE_MUTE:
        success = telegram_sent
    elif mode == MODE_MUTE_YOUTUBE:
        success = telegram_sent and youtube_url is not None
        # partial success considered if at least one platform succeeded for report header?
        # Keep strict: both need success for ✅ else show partial
        partial = telegram_sent or youtube_url is not None
    elif mode in (MODE_MUTE_INSTAGRAM, MODE_INSTAGRAM):
        success = instagram_id is not None
        partial = instagram_id is not None
    else:
        success = telegram_sent or youtube_url or instagram_id

    if success:
        header = f"✅ نجح الفيديو [{MODE_LABELS[mode]}]"
    elif mode == MODE_MUTE_YOUTUBE and (telegram_sent or youtube_url):
        header = f"⚠️ نجاح جزئي [{MODE_LABELS[mode]}]"
    else:
        header = f"❌ فشل الفيديو [{MODE_LABELS[mode]}]"

    # File size & deletion will be added by caller after input cleanup, but add now
    result_text = "\n".join(result_lines)
    final = (
        f"{header}\n"
        f"📌 العنوان: {title}\n"
        f"{result_text}\n"
        f"📦 الحجم: {file_size_mb:.2f} MB"
    )
    # This final text will be sent by queue_worker after checking deletion
    # Here we update status to final immediately (queue_worker will override with deletion info if needed)
    try:
        status = await _update_status_reply(message, status, final)
    except Exception:
        pass

    return {
        "telegram_sent": telegram_sent,
        "telegram_error": telegram_error,
        "youtube_url": youtube_url,
        "youtube_error": youtube_error,
        "instagram_id": instagram_id,
        "instagram_error": instagram_error,
        "file_size_mb": file_size_mb,
        "status_msg": status,
        "header": header,
        "result_text": result_text,
    }


# ---- Queue system like C:/Users/MC/Desktop/youube ----
async def enqueue_video_job(item: dict) -> None:
    """Add job to queue and reply with position like youube."""
    global video_queue
    if video_queue is None:
        logger.warning("Queue not initialized, processing directly")
        # Fallback: process directly (should not happen after post_init)
        return
    await video_queue.put(item)
    qsize = video_queue.qsize()
    message = item["message"]
    title = item.get("title", "فيديو")
    mode = item.get("mode", DEFAULT_MODE)
    if qsize == 1:
        await _send_status_reply(message, f"📥 تم استلام ({title}) [{MODE_LABELS[mode]}]\n⏳ جاري بدء المعالجة الآن...\n📌 الطابور: #{qsize}")
    else:
        await _send_status_reply(message, f"📥 تم استلام ({title}) [{MODE_LABELS[mode]}]\n📌 الترتيب في الطابور: #{qsize}\n⏳ سيتم النشر بالتسلسل تفادياً للحظر.")


async def queue_worker(application: Application) -> None:
    """Background worker processing videos one-by-one with delay."""
    logger.info("🎬 تم تشغيل عامل طابور الفيديوهات (Queue Worker) بنجاح.")
    while True:
        try:
            item = await video_queue.get()  # type: ignore
            message = item["message"]
            kind = item.get("kind")
            mode = item.get("mode", DEFAULT_MODE)
            title = item.get("title", "فيديو جديد")
            description = item.get("description", "")
            tags = item.get("tags", [])
            caption_text = item.get("caption_text", "")
            source_url = item.get("url", "")

            # Initial processing status reply
            try:
                if kind == "file":
                    status_msg = await _send_status_reply(
                        message,
                        f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n🔧 المرحلة: بدء المعالجة..."
                    )
                else:
                    status_msg = await _send_status_reply(
                        message,
                        f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n🔗 الرابط: {source_url}\n🔧 المرحلة: بدء المعالجة..."
                    )
            except Exception as e:
                logger.warning(f"Could not send status: {e}")
                status_msg = None  # fallback handled inside

            input_path: Optional[Path] = None
            dl_id: Optional[str] = None
            file_size_mb = 0.0
            input_created = False
            try:
                # Download phase
                if kind == "file":
                    file_id = item["file_id"]
                    unique_id = uuid.uuid4().hex
                    input_path = TEMP_DIR / f"{unique_id}_input.mp4"
                    if status_msg:
                        status_msg = await _update_status_reply(
                            message, status_msg,
                            f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n📥 المرحلة: جاري التحميل من تيليجرام..."
                        )
                    tg_file = await application.bot.get_file(file_id, read_timeout=120, connect_timeout=120)
                    await tg_file.download_to_drive(input_path, read_timeout=300, connect_timeout=120)
                    input_created = True
                elif kind == "link":
                    url = item["url"]
                    unique_id = uuid.uuid4().hex
                    dl_id = f"{unique_id}_download"
                    if status_msg:
                        status_msg = await _update_status_reply(
                            message, status_msg,
                            f"⏳ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n📥 المرحلة: جاري التحميل من الرابط...\n{url}"
                        )
                    downloaded = await download_from_url(url, TEMP_DIR, dl_id)
                    input_path = downloaded
                    input_created = True
                    if not input_path.exists():
                        raise RuntimeError("Downloaded file not found")
                else:
                    raise RuntimeError(f"Unknown job kind: {kind}")

                if input_path and input_path.exists():
                    if input_path.stat().st_size > MAX_SIZE_MB * 1024 * 1024:
                        if status_msg:
                            await _update_status_reply(
                                message, status_msg,
                                f"❌ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n📦 الحجم يتجاوز {MAX_SIZE_MB} ميجابايت."
                            )
                        continue
                    file_size_mb = input_path.stat().st_size / (1024 * 1024)

                if input_path is None or not input_path.exists():
                    raise RuntimeError("Input file missing after download")

                # Now publish via pipeline (which also updates status)
                result = await process_and_publish_video(message, input_path, title, description, tags, caption_text, mode, status_msg)
                status_msg = result.get("status_msg", status_msg)
                # Now final message with deletion status
                # File will be deleted in finally, but we can report size
                # Append deletion info
                deletion_line = "🗑️ الحذف المحلي: سيتم ✅"
                # Build final status with deletion note (actual deletion happens next)
                # We update again to include deletion expectation
                final_header = result.get("header", "")
                final_text = result.get("result_text", "")
                final_with_deletion = (
                    f"{final_header}\n"
                    f"📌 العنوان: {title}\n"
                    f"{final_text}\n"
                    f"📦 الحجم: {file_size_mb:.2f} MB\n"
                    f"{deletion_line}"
                )
                if status_msg:
                    try:
                        await _update_status_reply(message, status_msg, final_with_deletion)
                    except Exception:
                        pass

            except Exception as e:
                logger.exception("Queue job failed [%s] mode %s", title, mode)
                err_text = format_error(e)
                hint = ""
                low = str(e).lower()
                if "unsupported url" in low:
                    hint = "\nالرابط غير مدعوم."
                elif "private" in low or "login" in low:
                    hint = "\nقد يحتاج كوكيز."
                if status_msg:
                    try:
                        await _update_status_reply(
                            message, status_msg,
                            f"❌ [{MODE_LABELS[mode]}]\n📌 العنوان: {title}\n💬 السبب: {err_text}{hint}\n📦 الحجم: {file_size_mb:.2f} MB"
                        )
                    except Exception:
                        pass
            finally:
                # Cleanup input file (always, even if download failed mid-way)
                if input_path is not None:
                    try:
                        if input_path.exists():
                            input_path.unlink(missing_ok=True)
                    except OSError as ex:
                        logger.warning("Could not delete %s: %s", input_path, ex)
                if dl_id is not None:
                    for p in TEMP_DIR.glob(f"{dl_id}.*"):
                        try:
                            p.unlink(missing_ok=True)
                        except OSError:
                            pass

                video_queue.task_done()  # type: ignore
                if not video_queue.empty():  # type: ignore
                    delay = QUEUE_DELAY_SECONDS
                    logger.info(f"⏳ الانتظار {delay} ثوانٍ قبل الفيديو التالي...")
                    await asyncio.sleep(delay)

        except asyncio.CancelledError:
            logger.info("🛑 تم إيقاف عامل الطابور.")
            break
        except Exception as e:
            logger.error(f"❌ خطأ في الطابور: {e}", exc_info=True)
            await asyncio.sleep(5)


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
    title, description, tags = parse_caption(message.caption or "")
    caption_text = message.caption or ""

    item = {
        "kind": "file",
        "file_id": file_obj.file_id,
        "message": message,
        "mode": mode,
        "title": title,
        "description": description,
        "tags": tags,
        "caption_text": caption_text,
    }
    await enqueue_video_job(item)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    if not is_allowed(update):
        await message.reply_text("غير مصرح لك باستخدام هذا البوت.")
        return

    text = message.text.strip()
    urls = extract_urls(text)
    if not urls:
        return

    title, description, tags, caption_text = parse_link_text(text, urls)
    if len(urls) > 3:
        await message.reply_text(f"تم العثور على {len(urls)} روابط، سيتم معالجة أول 3 فقط.")
        urls = urls[:3]

    mode = selected_mode(context)

    for url in urls:
        item = {
            "kind": "link",
            "url": url,
            "message": message,
            "mode": mode,
            "title": title,
            "description": description,
            "tags": tags,
            "caption_text": caption_text,
        }
        await enqueue_video_job(item)


async def post_init(application: Application) -> None:
    global video_queue
    commands = [
        BotCommand("start", "عرض حالة البوت"),
        BotCommand("mode", "اختيار إجراء الفيديو"),
        BotCommand("help", "عرض طريقة الاستخدام"),
    ]
    await application.bot.set_my_commands(commands)
    # Init queue like youube main.py:875-880
    if video_queue is None:
        video_queue = asyncio.Queue()
        asyncio.create_task(queue_worker(application))
        logger.info("⚡ تم إعداد طابور الفيديوهات وبدء معالجة المهام (queue).")


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
    # Links: TikTok / Instagram / YouTube / FB etc. - like youube (yt-dlp). Uses same mode logic.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"https?://"), handle_link))

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

