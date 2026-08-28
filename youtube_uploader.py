import os
import pickle
from pathlib import Path

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN2_TITLE = "كود خصم نون mar110k"
DEFAULT_TITLE = (
    "#اكسبلور #fypシ #ترند #مالي_خلق_احط_هاشتاقات "
    "#رياكشن #ضحك #shortvideo #تيك_توك #لايك"
)


def static_title_for_token(token_file: Path) -> str:
    if "youtube_token2" in str(token_file):
        return TOKEN2_TITLE
    return DEFAULT_TITLE


class YouTubeUploader:
    def __init__(self, client_secrets_file: Path, token_file: Path, privacy: str = "public"):
        self.client_secrets_file = client_secrets_file
        self.token_file = token_file
        self.privacy = privacy

    @classmethod
    def from_environment(cls) -> "YouTubeUploader":
        config_dir = Path(
            os.getenv("PUBLISHER_CONFIG_DIR", str(Path(__file__).resolve().parent))
        )
        client_secrets_file = Path(
            os.getenv("YOUTUBE_CLIENT_SECRETS", "client_secrets.json")
        )
        token_file = Path(os.getenv("YOUTUBE_TOKEN_FILE", "youtube_token2.pickle"))
        if not client_secrets_file.is_absolute():
            client_secrets_file = config_dir / client_secrets_file
        if not token_file.is_absolute():
            token_file = config_dir / token_file

        return cls(
            client_secrets_file=client_secrets_file,
            token_file=token_file,
            privacy=os.getenv("YOUTUBE_PRIVACY", "public"),
        )

    def _credentials(self):
        credentials = None
        if self.token_file.exists():
            with self.token_file.open("rb") as token:
                credentials = pickle.load(token)

        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        elif not credentials or not credentials.valid:
            if not self.client_secrets_file.exists():
                raise RuntimeError(
                    f"YouTube client secrets file not found: {self.client_secrets_file}"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_file),
                SCOPES,
            )
            credentials = flow.run_local_server(port=0)

        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        with self.token_file.open("wb") as token:
            pickle.dump(credentials, token)
        return credentials

    def upload(
        self,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
    ) -> str:
        if self.privacy not in {"private", "public", "unlisted"}:
            raise RuntimeError("YOUTUBE_PRIVACY must be private, public, or unlisted")

        final_title = static_title_for_token(self.token_file)
        youtube = build("youtube", "v3", credentials=self._credentials())
        request = youtube.videos().insert(
            part="snippet,status",
            body={
                "snippet": {
                    "title": final_title,
                    "description": " ",
                    "tags": tags,
                    "categoryId": "22",
                },
                "status": {
                    "privacyStatus": self.privacy,
                    "selfDeclaredMadeForKids": False,
                },
            },
            media_body=MediaFileUpload(
                str(video_path),
                mimetype="video/mp4",
                resumable=True,
                chunksize=5 * 1024 * 1024,
            ),
        )

        response = None
        while response is None:
            _, response = request.next_chunk()

        return f"https://www.youtube.com/watch?v={response['id']}"
