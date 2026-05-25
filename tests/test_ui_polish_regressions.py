"""Regression guards for UI/UX polish tweaks (accessibility, empty states, feedback)."""

from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestUiPolishRegressions(unittest.TestCase):
    def _feed_surface(self) -> str:
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        fragment = (ROOT / "canopy" / "ui" / "templates" / "_feed_posts_fragment.html").read_text(encoding="utf-8")
        return feed + "\n" + fragment

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
        feed = self._feed_surface()
        self.assertIn("Clear search", feed)
        self.assertIn("url_for('ui.feed')", feed)

    def test_feed_empty_state_icons_have_aria_hidden(self) -> None:
        feed = self._feed_surface()
        self.assertIn('bi bi-search fs-1 mb-3 d-block" aria-hidden="true"', feed)
        self.assertIn('bi bi-newspaper fs-1 mb-3 d-block" aria-hidden="true"', feed)

    def test_feed_primary_actions_keep_reply_bookmark_repost_visible(self) -> None:
        feed = self._feed_surface()
        self.assertIn("Reply</span>", feed)
        self.assertIn("React{% endif %}</span>", feed)
        self.assertIn("reaction-palette", feed)
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

    def test_dm_page_conversation_list_uses_compact_timestamps(self) -> None:
        sidebar = (ROOT / "canopy" / "ui" / "templates" / "_messages_sidebar_sections.html").read_text(encoding="utf-8")
        messages = (ROOT / "canopy" / "ui" / "templates" / "messages.html").read_text(encoding="utf-8")
        self.assertEqual(sidebar.count('data-timestamp-format="compact"'), 2)
        self.assertIn("max-width: 3.8rem;", messages)
        self.assertIn("text-overflow: ellipsis;", messages)

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

    def test_repost_media_preserves_single_image_aspect_ratio(self) -> None:
        feed = self._feed_surface()
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('post-repost-thumb-grid{% if em.attachment_images|length == 1 %} is-single{% endif %}', feed)
        self.assertIn('.post-repost-thumb-grid.is-single .post-repost-thumb-link img', feed)
        self.assertIn("post-repost-thumb-grid${attachmentImageCount === 1 ? ' is-single' : ''} mt-2", channels)
        self.assertIn('.post-repost-thumb-grid.is-single .post-repost-thumb-link img', channels)

    def test_channel_reply_context_is_width_safe(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('#reply-context.is-visible', channels)
        self.assertIn('class="reply-context-prefix"', channels)
        self.assertIn("context.classList.add('is-visible');", channels)
        self.assertIn("context.classList.remove('is-visible');", channels)
        self.assertIn('#reply-context .reply-preview', channels)
        self.assertIn('text-overflow: ellipsis;', channels)

    def test_channel_primary_actions_keep_reply_bookmark_repost_visible(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn("Reply</span>", channels)
        self.assertIn("renderChannelReactionControl(message)", channels)
        self.assertIn("reaction-palette", channels)
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
        self.assertIn("Open work builder", channels)
        self.assertIn('aria-controls="channel-structured-builder"', channels)

    def test_primary_composers_offer_expandable_draft_area(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        messages = (ROOT / "canopy" / "ui" / "templates" / "messages.html").read_text(encoding="utf-8")
        messages_composer = (ROOT / "canopy" / "ui" / "templates" / "_messages_composer.html").read_text(encoding="utf-8")
        messages_combined = messages + "\n" + messages_composer
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")

        self.assertIn('id="channel-composer-expand-toggle"', channels)
        self.assertIn("function toggleChannelComposerExpanded", channels)
        self.assertIn(".channel-composer-wrap.composer-expanded #message-input", channels)
        self.assertIn(".channel-composer-wrap.composer-expanded #message-form .input-group > .position-relative.flex-grow-1", channels)
        self.assertIn("flex: 1 1 100%;", channels)
        self.assertIn(".channel-composer-wrap.composer-expanded #message-form .input-group-append", channels)
        self.assertIn("align-items: center !important;", channels)
        self.assertIn("align-self: flex-start;", channels)
        self.assertIn("justify-content: flex-end;", channels)
        self.assertIn("margin-left: auto;", channels)
        self.assertIn("height: auto;", channels)
        self.assertIn("min-height: 36px;", channels)
        self.assertIn("function shouldAutoExpandComposerForPaste(event, textarea)", channels)
        self.assertIn("function maybeAutoExpandChannelComposerForPaste(event)", channels)
        self.assertIn("pastedText.length >= 480 || lineCount >= 6 || projectedLength >= 1200", channels)
        self.assertIn("__canopyChannelPasteHandled", channels)

        self.assertIn("'dm-composer-expand-toggle'", messages_composer)
        self.assertIn("'deck-dm-composer-expand-toggle'", messages_composer)
        self.assertIn("function toggleDmComposerExpanded", messages)
        self.assertIn(".dm-composer.composer-expanded .dm-composer-textarea", messages)
        self.assertIn("function shouldAutoExpandComposerForPaste(event, textarea)", messages)
        self.assertIn("function maybeAutoExpandDmComposerForPaste(event)", messages)
        self.assertIn("pastedText.length >= 480 || lineCount >= 6 || projectedLength >= 1200", messages)
        self.assertIn("__canopyDmPasteHandled", messages)

        self.assertIn('id="feed-composer-expand-toggle"', feed)
        self.assertIn("function toggleFeedDraftExpanded", feed)
        self.assertIn("#post-composer.composer-expanded #postContent", feed)
        self.assertIn("function shouldAutoExpandComposerForPaste(event, textarea)", feed)
        self.assertIn("function maybeAutoExpandFeedComposerForPaste(event)", feed)
        self.assertIn("pastedText.length >= 480 || lineCount >= 6 || projectedLength >= 1200", feed)
        self.assertIn("__canopyFeedPasteHandled", feed)

    def test_structured_work_builder_keeps_composer_controls_reachable(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        feed = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")

        for template in (channels, feed):
            self.assertIn('class="structured-builder-scrollbody"', template)
            self.assertIn("max-height: min(72dvh, 42rem);", template)
            self.assertIn("max-height: min(62dvh, 34rem);", template)
            self.assertIn("max-height: min(58dvh, 30rem);", template)
            self.assertIn("overscroll-behavior: contain;", template)
            self.assertIn("scrollbar-gutter: stable;", template)
            self.assertIn("target.scrollIntoView({ behavior: 'smooth', block: 'end' });", template)

        self.assertIn("function scrollChannelStructuredBuilderComposerIntoView()", channels)
        self.assertIn("input?.closest('#message-form .input-group')", channels)
        self.assertIn("scrollChannelStructuredBuilderComposerIntoView();", channels)
        self.assertIn("function scrollFeedStructuredBuilderComposerIntoView()", feed)
        self.assertIn("document.querySelector('#post-composer .composer-action-row')", feed)
        self.assertIn("scrollFeedStructuredBuilderComposerIntoView();", feed)

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

    def test_channels_dropzone_overlay_uses_canopy_primary_not_bootstrap(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('border: 2px dashed var(--canopy-primary);', channels)
        self.assertNotIn('var(--bs-primary)', channels)

    def test_channels_file_preview_item_uses_canopy_tokens(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertNotIn('var(--bs-gray-100)', channels)
        self.assertIn('background: var(--canopy-bg-tertiary);', channels)

    def test_channel_removal_peer_id_uses_system_monospace_not_bootstrap(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertNotIn('var(--bs-font-monospace)', channels)
        self.assertIn('ui-monospace', channels)

    def test_canopy_llm_status_panel_has_aria_live(self) -> None:
        channels = (ROOT / "canopy" / "ui" / "templates" / "channels.html").read_text(encoding="utf-8")
        self.assertIn('id="channel-canopy-llm-status"', channels)
        self.assertIn('aria-live="polite"', channels)
        self.assertIn('aria-atomic="true"', channels)
        self.assertIn('id="channel-canopy-llm-actions"', channels)
        self.assertIn('id="channel-canopy-llm-generate"', channels)
        self.assertIn("onclick=\"generateCanopyLLMDraftFromComposer(event)\"", channels)
        self.assertIn('id="channel-canopy-llm-dismiss"', channels)
        self.assertIn("onclick=\"dismissCanopyLLMComposePanel()\"", channels)
        self.assertIn('Send as written', channels)
        self.assertIn('Send as normal text', channels)

    def test_mobile_shell_uses_drawer_navigation_and_large_touch_targets(self) -> None:
        base = (ROOT / "canopy" / "ui" / "templates" / "base.html").read_text(encoding="utf-8")
        main_js = (ROOT / "canopy" / "ui" / "static" / "js" / "canopy-main.js").read_text(encoding="utf-8")

        self.assertIn("min-width: 44px;", base)
        self.assertIn("min-height: 44px;", base)
        self.assertIn(".meshspace-switcher-menu {", base)
        self.assertIn("position: fixed !important;", base)
        self.assertIn("top: calc(env(safe-area-inset-top) + 62px) !important;", base)
        self.assertIn("width: min(88vw, 320px);", base)
        self.assertIn("backdrop-filter: blur(4px);", base)

        self.assertIn("const mobileStorageKey = 'sidebar-state-mobile';", main_js)
        self.assertIn("function isMobileSidebarMode() {", main_js)
        self.assertIn("return normalized === 'expanded' ? 'expanded' : 'hidden';", main_js)
        self.assertIn("newState = currentState === 'expanded' ? 'hidden' : 'expanded';", main_js)
        self.assertIn("function collapseMobileSidebarForNavigation() {", main_js)
        self.assertIn("event.target.closest('a[href]')", main_js)
        self.assertIn("collapseMobileSidebarForNavigation();", main_js)
        self.assertIn("if (touchStartedInInteractive) return;", main_js)
        self.assertIn("verticalDistance > Math.abs(swipeDistance) * 0.75", main_js)
        self.assertIn("toggleBtn.setAttribute('aria-expanded', 'true');", main_js)
        self.assertIn("toggleBtn.setAttribute('aria-expanded', 'false');", main_js)
        self.assertIn('id="sidebar-toggle" aria-expanded="false"', base)
        self.assertIn('id="mobile-backdrop" aria-hidden="true"', base)
        self.assertIn("mobileBackdrop.setAttribute('aria-hidden', 'false');", main_js)
        self.assertIn("mobileBackdrop.setAttribute('aria-hidden', 'true');", main_js)
        self.assertIn("if (e.key === 'Escape' && isMobileSidebarMode() && currentState === 'expanded')", main_js)
        self.assertIn("toggleBtn.focus();", main_js)
        self.assertIn("if (e.touches && e.touches.length > 1) {", main_js)
        self.assertIn("if (touchWasMultiTouch) return;", main_js)
        self.assertIn("touch.clientX", main_js)
        self.assertIn("touch.clientY", main_js)
        self.assertNotIn("touch.screenX", main_js)
        self.assertNotIn("touch.screenY", main_js)
        self.assertIn("window.dispatchEvent(new Event('resize'));", main_js)
        self.assertIn(".canopy-media-deck-portal, .sidebar-media-deck", main_js)


if __name__ == "__main__":
    unittest.main()
