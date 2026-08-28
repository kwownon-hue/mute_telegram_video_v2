import unittest
from types import SimpleNamespace
from unittest.mock import patch

import bot


class BotHelpersTest(unittest.TestCase):
    def test_parse_caption(self):
        title, description, tags = bot.parse_caption(
            "A useful title\nA longer description\n#video #shorts"
        )

        self.assertEqual(title, "A useful title")
        self.assertEqual(description, "A longer description\n#video #shorts")
        self.assertEqual(tags, ["video", "shorts"])

    def test_parse_empty_caption_uses_default_title(self):
        self.assertEqual(bot.parse_caption(""), ("فيديو جديد", "", []))

    def test_parse_caption_ignores_outer_blank_lines(self):
        self.assertEqual(
            bot.parse_caption("\n  Title  \nDescription\n"),
            ("Title", "Description", []),
        )

    def test_mode_keyboard_contains_every_mode(self):
        keyboard = bot.mode_keyboard(bot.MODE_MUTE_YOUTUBE)
        callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]

        self.assertEqual(
            callbacks,
            [
                "mode:mute",
                "mode:mute_youtube",
                "mode:mute_instagram",
                "mode:instagram",
            ],
        )

    def test_singular_allowed_user_id_is_supported(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123))
        with patch.dict(
            "os.environ",
            {"ALLOWED_USER_ID": "123"},
            clear=True,
        ):
            self.assertTrue(bot.is_allowed(update))

    def test_other_user_is_rejected(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=999))
        with patch.dict(
            "os.environ",
            {"ALLOWED_USER_IDS": "123, 456"},
            clear=True,
        ):
            self.assertFalse(bot.is_allowed(update))


if __name__ == "__main__":
    unittest.main()
