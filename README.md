# Telegram Video Mute and Publisher Bot

The bot accepts a Telegram video and supports four per-user modes:

1. Remove audio and return the muted video.
2. Remove audio, return the muted video, and publish it to YouTube.
3. Remove audio and publish the muted video to Instagram.
4. Publish the original video to Instagram without removing its audio.

Use `/mode` in Telegram to select the action. Mute and return is the default.

## Requirements

- Python 3.9 or newer
- `ffmpeg` in `PATH`, or the packaged `imageio-ffmpeg` fallback
- A Telegram bot token
- YouTube OAuth credentials for YouTube publishing
- Instagram Graph API and Cloudinary credentials for Instagram publishing

Install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Configuration

Create a `.env` file in this directory. Do not commit it.

```dotenv
BOT_TOKEN=telegram-bot-token
ALLOWED_USER_IDS=123456789
PUBLISHER_CONFIG_DIR=C:/Users/MC/Desktop/youube

MAX_SIZE_MB=50
CONCURRENT_LIMIT=3
TEMP_DIR=temp_videos
FFMPEG_PATH=

YOUTUBE_CLIENT_SECRETS=client_secrets.json
YOUTUBE_TOKEN_FILE=youtube_token2.pickle
YOUTUBE_PRIVACY=public

IG_USER_ID=instagram-business-account-id
IG_ACCESS_TOKEN=instagram-graph-api-token
INSTAGRAM_CAPTION=Default Reel caption
INSTAGRAM_GRAPH_API_VERSION=v23.0
CLOUDINARY_CLOUD_NAME=cloud-name
CLOUDINARY_API_KEY=cloudinary-key
CLOUDINARY_API_SECRET=cloudinary-secret
```

`ALLOWED_USER_IDS` is an optional comma-separated list. The source project's singular `ALLOWED_USER_ID` is also supported. Leave both empty to allow all Telegram users. Restricting access is strongly recommended because platform uploads consume account quotas.

On this machine, the bot automatically detects the sibling `C:/Users/MC/Desktop/youube` directory and loads its `.env`, `client_secrets.json`, and `youtube_token2.pickle`. A local `.env` in this project takes precedence. Set `PUBLISHER_CONFIG_DIR` to use another source directory.

The YouTube secrets and token can also be selected individually with absolute paths. For example:

```dotenv
YOUTUBE_CLIENT_SECRETS=C:/Users/MC/Desktop/youube/client_secrets.json
YOUTUBE_TOKEN_FILE=C:/Users/MC/Desktop/youube/youtube_token2.pickle
```

The first YouTube upload opens a browser for OAuth if the configured token does not already exist. Instagram publishing requires a Business or Creator account linked to a Facebook Page, with `instagram_basic` and `instagram_content_publish` permissions.

## Run

```powershell
python bot.py
```

Publishing preserves the old project's static metadata:

- `youtube_token2.pickle` uses the fixed title `كود خصم نون mar110k`.
- Other YouTube token files use the old fixed hashtag title.
- YouTube uses the old blank description. Hashtags from the Telegram caption can still be sent as tags.
- Instagram always uses `INSTAGRAM_CAPTION` from `C:/Users/MC/Desktop/youube/.env`. It only falls back to the Telegram caption when that variable is empty.
