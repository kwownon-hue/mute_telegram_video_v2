import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

import bot


class FfmpegTest(unittest.TestCase):
    def setUp(self):
        self.ffmpeg = bot.find_ffmpeg()
        self.assertIsNotNone(self.ffmpeg)

    def create_video(self, path: Path) -> None:
        subprocess.run(
            [
                self.ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=321x241:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=1000:duration=1",
                "-c:v",
                "libx264",
                "-vf",
                "scale=322:242",
                "-c:a",
                "aac",
                "-shortest",
                str(path),
            ],
            check=True,
            capture_output=True,
        )

    def probe(self, path: Path) -> str:
        return subprocess.run(
            [self.ffmpeg, "-i", str(path), "-f", "null", "-"],
            check=True,
            capture_output=True,
            text=True,
        ).stderr

    def test_remove_audio_creates_video_without_audio_stream(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.mp4"
            output_path = Path(directory) / "output.mp4"
            self.create_video(input_path)

            asyncio.run(bot.remove_audio(input_path, output_path))

            self.assertTrue(output_path.exists())
            self.assertNotIn("Audio:", self.probe(output_path))

    def test_instagram_conversion_can_preserve_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.mp4"
            output_path = Path(directory) / "output.mp4"
            self.create_video(input_path)

            asyncio.run(bot.prepare_instagram_video(input_path, output_path, mute=False))

            probe = self.probe(output_path)
            self.assertIn("Video: h264", probe)
            self.assertIn("Audio: aac", probe)

    def test_instagram_conversion_can_remove_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = Path(directory) / "input.mp4"
            output_path = Path(directory) / "output.mp4"
            self.create_video(input_path)

            asyncio.run(bot.prepare_instagram_video(input_path, output_path, mute=True))

            probe = self.probe(output_path)
            self.assertIn("Video: h264", probe)
            self.assertNotIn("Audio:", probe)


if __name__ == "__main__":
    unittest.main()
