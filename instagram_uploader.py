import os
import time
from pathlib import Path

import cloudinary
import cloudinary.uploader
import requests


class InstagramUploader:
    def __init__(
        self,
        user_id: str,
        access_token: str,
        cloud_name: str,
        cloudinary_api_key: str,
        cloudinary_api_secret: str,
        default_caption: str = "",
        graph_api_version: str = "v23.0",
    ):
        missing = [
            name
            for name, value in {
                "IG_USER_ID": user_id,
                "IG_ACCESS_TOKEN": access_token,
                "CLOUDINARY_CLOUD_NAME": cloud_name,
                "CLOUDINARY_API_KEY": cloudinary_api_key,
                "CLOUDINARY_API_SECRET": cloudinary_api_secret,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Missing Instagram configuration: {', '.join(missing)}")

        self.user_id = user_id
        self.access_token = access_token
        self.default_caption = default_caption
        self.graph_api_url = f"https://graph.facebook.com/{graph_api_version}"
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=cloudinary_api_key,
            api_secret=cloudinary_api_secret,
            secure=True,
        )

    @classmethod
    def from_environment(cls) -> "InstagramUploader":
        return cls(
            user_id=os.getenv("IG_USER_ID", ""),
            access_token=os.getenv("IG_ACCESS_TOKEN", ""),
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
            cloudinary_api_key=os.getenv("CLOUDINARY_API_KEY", ""),
            cloudinary_api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
            default_caption=os.getenv("INSTAGRAM_CAPTION", ""),
            graph_api_version=os.getenv("INSTAGRAM_GRAPH_API_VERSION", "v23.0"),
        )

    def _graph_request(self, method: str, endpoint: str, **params) -> dict:
        response = requests.request(
            method,
            f"{self.graph_api_url}/{endpoint}",
            params={**params, "access_token": self.access_token},
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError as error:
            raise RuntimeError(f"Instagram returned HTTP {response.status_code}") from error
        if not response.ok or "error" in data:
            details = data.get("error", {}).get("message", data)
            raise RuntimeError(f"Instagram API error: {details}")
        return data

    def publish_reel(self, video_path: Path, caption: str) -> str:
        upload = cloudinary.uploader.upload(
            str(video_path),
            resource_type="video",
            folder="instagram_reels",
            overwrite=False,
        )
        public_id = upload.get("public_id")

        try:
            container = self._graph_request(
                "POST",
                f"{self.user_id}/media",
                video_url=upload["secure_url"],
                caption=self.default_caption or caption,
                media_type="REELS",
                share_to_feed="true",
            )
            container_id = container["id"]

            for _ in range(30):
                time.sleep(10)
                status = self._graph_request(
                    "GET",
                    container_id,
                    fields="status_code,status",
                ).get("status_code")
                if status == "FINISHED":
                    break
                if status in {"ERROR", "EXPIRED"}:
                    raise RuntimeError(f"Instagram video processing ended with {status}")
            else:
                raise RuntimeError("Instagram video processing timed out")

            published = self._graph_request(
                "POST",
                f"{self.user_id}/media_publish",
                creation_id=container_id,
            )
            return published["id"]
        finally:
            if public_id:
                try:
                    cloudinary.uploader.destroy(public_id, resource_type="video")
                except Exception:
                    pass
