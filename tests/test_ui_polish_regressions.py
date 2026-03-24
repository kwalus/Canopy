"""Regression guards for UI/UX polish tweaks (accessibility, empty states, feedback)."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestUiPolishRegressions(unittest.TestCase):
    def test_feed_share_post_button_has_id_for_loading_state(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn('id="share-post-btn"', feed)

    def test_feed_share_post_button_loading_state_in_createpost(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn("function setShareButtonState(loading)", feed)
        self.assertIn("setShareButtonState(true)", feed)
        self.assertIn("setShareButtonState(false)", feed)
        self.assertIn("spinner-border", feed)
        self.assertEqual(feed.count("setShareButtonState(false)"), 2)

    def test_feed_mention_builder_close_has_aria_label(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Close Team Mention Builder"', feed)

    def test_feed_empty_state_has_clear_search_link(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn("Clear search", feed)
        self.assertIn("url_for('ui.feed')", feed)

    def test_feed_empty_state_icons_have_aria_hidden(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn('bi bi-search fs-1 mb-3 d-block" aria-hidden="true"', feed)
        self.assertIn('bi bi-newspaper fs-1 mb-3 d-block" aria-hidden="true"', feed)

    def test_dm_sidebar_empty_states_have_icons(self) -> None:
        sidebar = (ROOT / "canopy" / "ui" / "templates" / "_messages_sidebar_sections.html").read_text(encoding="utf-8")
        self.assertIn("bi bi-chat-dots", sidebar)
        self.assertIn("bi bi-people", sidebar)

    def test_dm_thread_empty_state_has_icon(self) -> None:
        thread_body = (ROOT / "canopy" / "ui" / "templates" / "_messages_thread_body.html").read_text(encoding="utf-8")
        self.assertIn("bi bi-chat-square-text", thread_body)
        self.assertIn("bi bi-send", thread_body)

    def test_dm_thread_active_empty_state_is_friendly(self) -> None:
        thread_body = (ROOT / "canopy" / "ui" / "templates" / "_messages_thread_body.html").read_text(encoding="utf-8")
        self.assertIn("Say hello!", thread_body)

    def test_channels_cancel_reply_button_has_aria_label(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Cancel reply"', channels)

    def test_profile_avatar_container_has_role_button(self) -> None:
        profile = (ROOT / "canopy" / "ui" / "templates" / "profile.html").read_text(encoding="utf-8")
        self.assertIn('role="button"', profile)

    def test_profile_avatar_container_has_aria_label(self) -> None:
        profile = (ROOT / "canopy" / "ui" / "templates" / "profile.html").read_text(encoding="utf-8")
        self.assertIn('aria-label="Change profile picture"', profile)

    def test_profile_avatar_container_has_keyboard_handler(self) -> None:
        profile = (ROOT / "canopy" / "ui" / "templates" / "profile.html").read_text(encoding="utf-8")
        self.assertIn('onkeydown="avatarContainerKeydown(event)"', profile)
        self.assertIn("function avatarContainerKeydown(event)", profile)
        self.assertIn("triggerAvatarUpload()", profile)

    def test_profile_avatar_overlay_is_aria_hidden(self) -> None:
        profile = (ROOT / "canopy" / "ui" / "templates" / "profile.html").read_text(encoding="utf-8")
        self.assertIn('avatar-upload-overlay" aria-hidden="true"', profile)

    def test_profile_avatar_image_has_meaningful_alt_text(self) -> None:
        profile = (ROOT / "canopy" / "ui" / "templates" / "profile.html").read_text(encoding="utf-8")
        self.assertIn("Profile picture of", profile)


if __name__ == "__main__":
    unittest.main()
