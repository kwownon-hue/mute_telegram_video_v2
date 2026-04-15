import os
import logging
import subprocess
import uuid
import asyncio
import shutil
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# --- Configuration ---
TOKEN = os.environ.get("BOT_TOKEN", "ضع_توكنك_هنا")
TEMP_DIR = "temp_videos"
MAX_SIZE_MB = 50
CONCURRENT_LIMIT = 3  # Maximum number of videos being processed at once

# --- Initialization ---
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.makedirs(TEMP_DIR, exist_ok=True)

# Semaphore to control concurrency and prevent CPU exhaustion
process_semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)

async def check_ffmpeg():
    """Verify that ffmpeg is available in the system path."""
    if shutil.which("ffmpeg") is None:
        logger.error("❌ ffmpeg is NOT installed or not in PATH!")
        return False
    return True

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً!\n\n"
        "🎬 أرسل لي أي فيديو وسأعيده إليك *بدون صوت* تماماً.\n\n"
        "⚠️ الحد الأقصى للحجم: 50 ميجابايت.\n"
        "⚡ جاري العمل بنظام المعالجة المتوازي.",
        parse_mode="Markdown",
    )

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *كيف تستخدمني؟*\n\n"
        "1️⃣ أرسل الفيديو مباشرةً أو كـ *ملف* (File)\n"
        "2️⃣ انتظر ثوانٍ قليلة\n"
        "3️⃣ استلم الفيديو الصامت ✅\n\n"
        "💡 يدعم البوت صيغ MP4, MKV, MOV وغيرها.",
        parse_mode="Markdown",
    )

async def remove_audio(input_path: str, output_path: str) -> bool:
    """Removes audio from a video file using ffmpeg asynchronously."""
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-an",
        "-c:v", "copy",
        output_path,
    ]
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Wait for the process to complete with a timeout (e.g., 5 minutes)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
            if process.returncode == 0:
                return True
            else:
                logger.error(f"FFmpeg failed with code {process.returncode}: {stderr.decode()}")
                return False
        except asyncio.TimeoutError:
            process.kill()
            logger.error("FFmpeg process timed out")
            return False
            
    except Exception as e:
        logger.error(f"Error running FFmpeg: {e}")
        return False

async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    # Identify the video object
    if message.video:
        file_obj = message.video
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("video"):
        file_obj = message.document
    else:
        await message.reply_text("❌ الرجاء إرسال ملف فيديو صحيح.")
        return

    # Check file size
    if file_obj.file_size and file_obj.file_size > MAX_SIZE_MB * 1024 * 1024:
        await message.reply_text(f"❌ حجم الفيديو يتجاوز {MAX_SIZE_MB} ميجابايت.")
        return

    uid = uuid.uuid4().hex[:8]
    input_path  = os.path.join(TEMP_DIR, f"{uid}_input.mp4")
    output_path = os.path.join(TEMP_DIR, f"{uid}_muted.mp4")

    status_msg = await message.reply_text("⏳ جاري تحميل الفيديو...")

    try:
        # Step 1: Download
        tg_file = await ctx.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(input_path)

        # Step 2: Queue and Process
        async with process_semaphore:
            await status_msg.edit_text("🔇 جاري إزالة الصوت ومعالجة الفيديو...")
            success = await remove_audio(input_path, output_path)

        if not success:
            await status_msg.edit_text("❌ حدث خطأ أثناء معالجة الفيديو. قد يكون الملف تالفاً أو برنامج المعالجة غير متوفر.")
            return

        # Step 3: Upload
        await status_msg.edit_text("📤 جاري إرسال الفيديو الصامت...")
        
        with open(output_path, "rb") as f:
            await message.reply_video(
                video=f,
                caption="✅ تم إزالة الصوت بنجاح! 🔇",
                supports_streaming=True,
            )

        await status_msg.delete()

    except Exception as e:
        logger.exception("خطأ غير متوقع في handle_video")
        await status_msg.edit_text(f"❌ حدث خطأ غير متوقع: {str(e)[:100]}")

    finally:
        # Cleanup
        for path in (input_path, output_path):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except Exception as cleanup_err:
                    logger.warning(f"Failed to delete {path}: {cleanup_err}")

async def main():
    if not await check_ffmpeg():
        print("⚠️ Warning: ffmpeg is not installed. The bot will start but video processing will fail.")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            handle_video,
        )
    )

    logger.info("🤖 البوت يعمل...")
    # Use the async native way to run polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    
    # Keep the bot running until interrupted
    # Use an infinite loop to keep the coroutine alive
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
