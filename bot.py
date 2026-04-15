import os
import logging
import subprocess
import uuid
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ─── الإعدادات ───────────────────────────────────────────
TOKEN = os.environ.get("BOT_TOKEN", "ضع_توكنك_هنا")
TEMP_DIR = "temp_videos"
MAX_SIZE_MB = 50  # حد تيليجرام للبوتات المجانية

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.makedirs(TEMP_DIR, exist_ok=True)


# ─── أوامر ───────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 أهلاً!\n\n"
        "🎬 أرسل لي أي فيديو وسأعيده إليك *بدون صوت* تماماً.\n\n"
        "⚠️ الحد الأقصى للحجم: 50 ميجابايت.",
        parse_mode="Markdown",
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *كيف تستخدمني؟*\n\n"
        "1️⃣ أرسل الفيديو مباشرةً أو كـ *ملف* (File)\n"
        "2️⃣ انتظر ثوانٍ قليلة\n"
        "3️⃣ استلم الفيديو الصامت ✅\n\n"
        "🔹 الأمر /start — الرسالة الترحيبية",
        parse_mode="Markdown",
    )


# ─── معالجة الفيديو ──────────────────────────────────────

def remove_audio(input_path: str, output_path: str) -> bool:
    """يستخدم ffmpeg لإزالة الصوت بدون إعادة ترميز الفيديو (سريع جداً)."""
    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-an",          # إزالة الصوت
        "-c:v", "copy", # نسخ الفيديو بدون إعادة ترميز (أسرع)
        output_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timeout")
        return False
    except FileNotFoundError:
        logger.error("ffmpeg غير مثبت!")
        return False


async def handle_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    message = update.message

    # ─ تحديد نوع الملف المُرسَل ─
    if message.video:
        file_obj = message.video
        file_size = file_obj.file_size
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("video"):
        file_obj = message.document
        file_size = file_obj.file_size
    else:
        await message.reply_text("❌ الرجاء إرسال ملف فيديو صحيح.")
        return

    # ─ فحص الحجم ─
    if file_size and file_size > MAX_SIZE_MB * 1024 * 1024:
        await message.reply_text(
            f"❌ حجم الفيديو يتجاوز {MAX_SIZE_MB} ميجابايت.\n"
            "تيليجرام لا يسمح للبوتات بتحميل ملفات أكبر من ذلك."
        )
        return

    uid = uuid.uuid4().hex[:8]
    input_path  = os.path.join(TEMP_DIR, f"{uid}_input.mp4")
    output_path = os.path.join(TEMP_DIR, f"{uid}_muted.mp4")

    status_msg = await message.reply_text("⏳ جاري تحميل الفيديو...")

    try:
        # ─ تحميل الفيديو ─
        tg_file = await ctx.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(input_path)

        await status_msg.edit_text("🔇 جاري إزالة الصوت...")

        # ─ إزالة الصوت ─
        success = remove_audio(input_path, output_path)

        if not success:
            await status_msg.edit_text(
                "❌ حدث خطأ أثناء معالجة الفيديو.\n"
                "تأكد أن الملف فيديو صحيح وحاول مجدداً."
            )
            return

        await status_msg.edit_text("📤 جاري إرسال الفيديو الصامت...")

        # ─ إرسال الناتج ─
        with open(output_path, "rb") as f:
            await message.reply_video(
                video=f,
                caption="✅ تم إزالة الصوت بنجاح! 🔇",
                supports_streaming=True,
            )

        await status_msg.delete()

    except Exception as e:
        logger.exception("خطأ غير متوقع")
        await status_msg.edit_text(f"❌ خطأ غير متوقع: {e}")

    finally:
        # ─ حذف الملفات المؤقتة ─
        for path in (input_path, output_path):
            if os.path.exists(path):
                os.remove(path)


# ─── تشغيل البوت ─────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO,
            handle_video,
        )
    )

    logger.info("🤖 البوت يعمل...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
