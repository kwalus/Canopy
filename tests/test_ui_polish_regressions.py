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

    def test_feed_primary_actions_keep_reply_bookmark_repost_visible(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn("Reply</span>", feed)
        self.assertIn("Like{% endif %}</span>", feed)
        self.assertIn('data-bookmark-label', feed)
        self.assertIn("Repost</span>", feed)
        self.assertIn('aria-label="More post actions"', feed)

    def test_feed_mobile_uses_collapsible_composer(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn('id="feed-mobile-composer-toggle"', feed)
        self.assertIn('#post-composer.mobile-collapsed .card-body', feed)
        self.assertIn("function syncFeedComposerLayout(options = {})", feed)
        self.assertIn("function toggleFeedComposer(forceOpen)", feed)
        self.assertIn("syncFeedComposerLayout({ forceExpand: true });", feed)

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

    def test_channel_primary_actions_keep_reply_bookmark_repost_visible(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn("Reply</span>", channels)
        self.assertIn("Like'}</span>", channels)
        self.assertIn("Repost</span>", channels)
        self.assertIn('data-bookmark-label', channels)
        self.assertIn('aria-label="More message actions"', channels)

    def test_channel_header_uses_more_menu_for_secondary_tools(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('id="channel-header-more-toggle"', channels)
        self.assertIn('id="copy-channel-id-btn"', channels)
        self.assertIn("Refresh messages", channels)
        self.assertIn(">Members", channels)

    def test_channel_mobile_header_and_composer_use_overflow_menus(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn(".channel-header-mobile-only", channels)
        self.assertIn("Open privacy", channels)
        self.assertIn('id="channel-header-search-toggle"', channels)
        self.assertIn(".channel-header-search.mobile-open", channels)
        self.assertIn("function toggleChannelHeaderSearch(forceOpen)", channels)
        self.assertIn("return window.innerWidth <= 767;", channels)
        self.assertIn('id="channel-composer-more-toggle"', channels)
        self.assertIn("More compose tools", channels)
        self.assertIn(".channel-composer-advanced-tool", channels)

    def test_create_channel_form_has_compact_narrow_sidebar_styles(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn("--channel-sidebar-width: clamp(236px, 24vw, 260px);", channels)
        self.assertIn("#create-channel-inline {\n        flex: 0 0 auto;", channels)
        self.assertIn("padding: 0.75rem 0.7rem !important;", channels)
        self.assertIn("Private by default.", channels)
        self.assertIn(".create-channel-member-entry {\n        display: grid;\n        grid-template-columns: 1fr;", channels)
        self.assertIn(".create-channel-member-add-btn {\n        width: 100%;", channels)
        self.assertIn(".create-channel-privacy-row {\n        display: grid;\n        grid-template-columns: 1fr;", channels)
        self.assertIn('placeholder="Add members"', channels)
        self.assertIn("width: var(--channel-sidebar-width);", channels)
        self.assertIn("margin-left: calc(var(--channel-sidebar-width) * -1);", channels)

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

    def test_dm_backdrop_has_aria_hidden_initially(self) -> None:
        messages = (ROOT / "canopy" / "ui" / "templates" / "messages.html").read_text(encoding="utf-8")
        self.assertIn('class="dm-mobile-sidebar-backdrop"', messages)
        self.assertIn('aria-hidden="true"', messages)

    def test_dm_mobile_sidebar_updates_backdrop_aria_hidden(self) -> None:
        messages = (ROOT / "canopy" / "ui" / "templates" / "messages.html").read_text(encoding="utf-8")
        self.assertIn("backdrop.setAttribute('aria-hidden', isSidebarOpen ? 'false' : 'true');", messages)
        fn_start = messages.index("function toggleDmMobileSidebar(forceOpen)")
        fn_end = messages.index("\n    function ", fn_start)
        fn_body = messages[fn_start:fn_end]
        self.assertIn("button.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');", fn_body)
        self.assertIn("backdrop.setAttribute('aria-hidden', shouldOpen ? 'false' : 'true');", fn_body)

    def test_feed_composer_toggle_has_initial_aria_binding(self) -> None:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn('id="feed-mobile-composer-toggle"', feed)
        self.assertIn('aria-expanded="false"', feed)
        self.assertIn('aria-controls="post-composer"', feed)


if __name__ == "__main__":
    unittest.main()
