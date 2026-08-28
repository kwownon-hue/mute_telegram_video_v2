import unittest
from pathlib import Path

from instagram_uploader import InstagramUploader
from youtube_uploader import DEFAULT_TITLE, TOKEN2_TITLE, YouTubeUploader, static_title_for_token


class UploadersTest(unittest.TestCase):
    def test_token2_uses_old_static_title(self):
        self.assertEqual(
            static_title_for_token(Path("youtube_token2.pickle")),
            TOKEN2_TITLE,
        )

    def test_other_token_uses_old_static_hashtag_title(self):
        self.assertEqual(
            static_title_for_token(Path("youtube_token.pickle")),
            DEFAULT_TITLE,
        )

    def test_instagram_requires_all_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "IG_USER_ID"):
            InstagramUploader("", "token", "cloud", "key", "secret")

    def test_youtube_rejects_unknown_privacy(self):
        uploader = YouTubeUploader(
            Path("missing-client-secrets.json"),
            Path("missing-token.pickle"),
            privacy="friends-only",
        )

        with self.assertRaisesRegex(RuntimeError, "YOUTUBE_PRIVACY"):
            uploader.upload(Path("missing-video.mp4"), "Title", "", [])


if __name__ == "__main__":
    unittest.main()
