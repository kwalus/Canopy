"""Lightweight frontend regression guards for template/script logic."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestFrontendRegressions(unittest.TestCase):
    def test_channel_reply_button_uses_dataset_helper(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn("function setReplyFromButton(button)", channels_template)
        self.assertIn("onclick=\"setReplyFromButton(this)\"", channels_template)
        self.assertNotIn("onclick=\"setReplyTo('${message.id}'", channels_template)

    def test_miniplayer_no_longer_eagerly_docks_youtube_on_update(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertNotIn("if (type === 'youtube' && !isDocked && miniVideoHost) {", main_js)
        self.assertIn("} else if (type === 'youtube') {", main_js)
        self.assertIn("if (type === 'youtube' && miniVideoHost && !isDockedInMiniHost(el) && isOffscreen(el)) {", main_js)
        self.assertIn("autoDockYouTube(entry.target);", main_js)
        self.assertIn("el.__canopyMiniYTDockResumeAt = getYouTubeCurrentTimeSafe(el);", main_js)
        self.assertIn("player.seekTo(resumeAt, true);", main_js)
        self.assertIn("maybeRestoreYouTubeDockState(el);", main_js)
        self.assertIn("Object.prototype.hasOwnProperty.call(el, '__canopyMiniYTDockResumeAt')", main_js)
        self.assertIn("const shouldResume = el.__canopyMiniYTDockShouldResume === true;", main_js)
        self.assertIn("url.searchParams.set('start', String(Math.max(0, Math.floor(resumeAt))));", main_js)
        self.assertIn("el.__canopyMiniYTLastTime = cur;", main_js)
        self.assertIn("function shouldPersistActiveYouTube(el) {", main_js)
        self.assertIn("if (!shouldPersistActiveYouTube(el)) {", main_js)
        self.assertIn("clearYouTubeDockResumeState(el);", main_js)

    def test_identity_modal_treats_local_peer_origin_as_local(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("const canopyLocalPeerId = window.CANOPY_VARS ? String(window.CANOPY_VARS.localPeerId || '').trim() : '';", main_js)
        self.assertIn("const originPeer = (canopyLocalPeerId && originPeerRaw === canopyLocalPeerId) ? '' : originPeerRaw;", main_js)

    def test_structured_composer_shared_helper_wraps_plain_text_and_appends_to_existing_blocks(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("function applyTemplateToDraft(toolType, currentText)", main_js)
        self.assertIn("if (hasStructuredToolBlock(trimmed)) {", main_js)
        self.assertIn("return `${trimmed}\\n\\n${buildToolBlock(toolType, '')}`;", main_js)
        self.assertIn("return buildToolBlock(toolType, trimmed);", main_js)

    def test_rich_content_supports_inline_file_image_anchors(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        base_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn(r'!\[([^\]]*)\]\(file:([A-Za-z0-9_-]+)\)', main_js)
        self.assertIn("channel-inline-image-block", main_js)
        self.assertIn(".channel-inline-image-block", base_template)
        self.assertIn(".channel-inline-image-full", base_template)

    def test_attachment_layout_hints_are_rendered_across_surfaces(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        feed_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'feed.html').read_text(encoding='utf-8')
        messages_macros = (ROOT / 'canopy' / 'ui' / 'templates' / '_messages_macros.html').read_text(encoding='utf-8')
        base_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn("function resolveAttachmentLayoutHint(images)", channels_template)
        self.assertIn('data-layout="${normalizedLayout}"', channels_template)
        self.assertIn("{% from \"_messages_macros.html\" import render_image_gallery %}", feed_template)
        self.assertIn("render_image_gallery(images, 'feed-' ~ post.id, image_layout.value)", feed_template)
        self.assertIn("{% macro render_dm_attachments(attachments, message_id) -%}", messages_macros)
        self.assertIn("render_image_gallery(images, 'dm-' ~ message_id, ns.layout_hint)", messages_macros)
        self.assertIn('.media-grid[data-layout="hero"]', base_template)
        self.assertIn('.media-grid[data-layout="strip"]', base_template)

    def test_channel_thread_polling_has_snapshot_fallback(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn("// Fall back to a direct snapshot refresh if the event poll fails.", channels_template)
        self.assertIn("requestChannelThreadRefresh();", channels_template)
        self.assertIn("}, 10000);", channels_template)

    def test_active_channel_refreshes_when_sidebar_receives_new_message_event(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn("if (currentChannelId && channelId === currentChannelId) {", channels_template)
        self.assertIn("requestChannelThreadRefresh();", channels_template)
        self.assertIn("if (data && data.marked_read && typeof window.requestCanopySidebarAttentionRefresh === 'function') {", channels_template)
        self.assertIn("window.requestCanopySidebarAttentionRefresh({ force: true }).catch(() => {});", channels_template)

    def test_notification_bell_uses_attention_snapshot_and_peer_polling_stays_separate(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        base_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn("function initCanopyAttentionCenter()", main_js)
        self.assertIn("window.renderCanopyAttentionBell = function(items) {", main_js)
        self.assertIn("const endpoint = ((window.CANOPY_VARS && window.CANOPY_VARS.urls) || {}).peerActivity || '/ajax/peer_activity';", main_js)
        self.assertIn("const endpoint = routes.sidebarAttentionSnapshot || '/ajax/sidebar_attention_snapshot';", main_js)
        self.assertIn("startCanopySidebarPeerPolling();", main_js)
        self.assertIn("const avatarUrl = _safeImageSrc(item.avatar_url || '');", main_js)
        self.assertIn("img.src = avatarUrl;", main_js)
        self.assertIn("iconWrap.textContent = fallbackInitial;", main_js)
        self.assertIn("const canopyAttentionDismissStorageKey = (() => {", main_js)
        self.assertIn("const canopyAttentionSeenStorageKey = (() => {", main_js)
        self.assertIn("window.localStorage.setItem(canopyAttentionDismissStorageKey, String(normalized));", main_js)
        self.assertIn("window.localStorage.setItem(canopyAttentionSeenStorageKey, String(normalized));", main_js)
        self.assertIn("function filterCanopyAttentionItems(items) {", main_js)
        self.assertIn("function countUnseenCanopyAttentionItems(items) {", main_js)
        self.assertIn("window.renderCanopyAttentionBell(filterCanopyAttentionItems(canopySidebarAttentionState.items));", main_js)
        self.assertIn("saveCanopyAttentionDismissCursor(canopySidebarAttentionState.currentEventCursor);", main_js)
        self.assertIn("saveCanopyAttentionSeenCursor(canopySidebarAttentionState.currentEventCursor);", main_js)
        self.assertIn("const CANOPY_ATTENTION_FILTER_DEFS = [", main_js)
        self.assertIn("const canopyAttentionFilterStorageKey = (() => {", main_js)
        self.assertIn("function renderFilterBar() {", main_js)
        self.assertIn("saveCanopyAttentionFilters(next);", main_js)
        self.assertIn("const filterBar = document.getElementById('notificationFilterBar');", main_js)
        self.assertIn("const filterResetBtn = document.getElementById('notificationFilterReset');", main_js)
        self.assertIn("class=\"notification-filter-wrap\"", base_template)
        self.assertIn("id=\"notificationFilterBar\"", base_template)
        self.assertIn("id=\"notificationFilterReset\"", base_template)

    def test_channel_focus_uses_context_window_and_container_scroll(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn("function clearInitialChannelFocusFromUrl()", channels_template)
        self.assertIn("function scrollMessageIntoContainer(msgEl, options = {})", channels_template)
        self.assertIn("query.set('focus_message', focusMessageId);", channels_template)
        self.assertIn("selectChannel(focusChannelId, channelName, { focusMessageId: initialFocusMessageId || '', forceScroll: false });", channels_template)

    def test_stream_owner_controls_drive_real_lifecycle_endpoints(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn("function _setStreamLifecycle(streamId, action, slotId)", channels_template)
        self.assertIn("`/ajax/streams/${encodeURIComponent(streamId)}/${action}`", channels_template)
        self.assertIn("function stopStreamOwner(streamId, slotId)", channels_template)
        self.assertIn("data-stream-status-chip=\"1\"", channels_template)
        self.assertIn("data-stream-status-value=\"1\"", channels_template)
        self.assertIn("const _previewBroadcasters = {};", channels_template)
        self.assertIn("function _stopPreviewStream(streamId)", channels_template)
        self.assertIn("_stopPreviewStream(streamId);", channels_template)
        self.assertIn("const permissionStream = await navigator.mediaDevices.getUserMedia", channels_template)
        self.assertIn("permissionStream.getTracks().forEach((t) => t.stop())", channels_template)

    def test_channels_route_does_not_shadow_template_config(self) -> None:
        ui_routes = (ROOT / 'canopy' / 'ui' / 'routes.py').read_text(encoding='utf-8')
        self.assertIn("return render_template('channels.html',", ui_routes)
        self.assertNotIn("config=config,", ui_routes)

    def test_structured_validation_ignores_plain_unknown_section_headers(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("const suggestedTag = TAG_SUGGESTIONS[tag] || null;", main_js)
        self.assertIn("if (!suggestedTag) {", main_js)
        self.assertIn("return;", main_js)

    def test_feed_and_channel_composers_render_structured_validation_and_results(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        feed_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'feed.html').read_text(encoding='utf-8')
        self.assertIn('id="channel-structured-validation"', channels_template)
        self.assertIn('id="channel-structured-result"', channels_template)
        self.assertIn('id="channel-structured-tools-toggle"', channels_template)
        self.assertIn("function updateChannelStructuredTriggerState(result)", channels_template)
        self.assertIn("support.applyTemplateToDraft(toolType, raw)", channels_template)
        self.assertIn('id="feed-structured-validation"', feed_template)
        self.assertIn('id="feed-structured-result"', feed_template)
        self.assertIn('id="feed-structured-tools-toggle"', feed_template)
        self.assertIn("function updateFeedStructuredTriggerState(result)", feed_template)
        self.assertIn("function updateFeedStructuredValidation()", feed_template)
        self.assertIn("const structuredValidation = updateFeedStructuredValidation();", feed_template)
        self.assertIn("error && error.structured_validation", feed_template)
        self.assertIn("error && error.structured_validation", channels_template)
    def test_show_alert_null_checks_flash_messages_container(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        # showAlert must guard against a missing .flash-messages container
        self.assertIn("if (!container) return;", main_js)
        # The null-check must appear before the appendChild call
        null_check_pos = main_js.index("if (!container) return;")
        append_pos = main_js.index("container.appendChild(alertDiv);")
        self.assertLess(null_check_pos, append_pos)

    def test_channel_list_element_has_id_for_sidebar_badge_polling(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        # The channel list container must carry id="channel-list" so that
        # setSidebarChannelUnreadCount, incrementSidebarChannelUnreadCount, and
        # pollChannelSidebarEvents (all using getElementById) can find it.
        self.assertIn('id="channel-list"', channels_template)

    def test_sidebar_navigation_renders_unread_badges_and_attention_refresh(self) -> None:
        base_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn('id="sidebar-nav-messages-badge"', base_template)
        self.assertIn('id="sidebar-nav-channels-badge"', base_template)
        self.assertIn('id="sidebar-nav-feed-badge"', base_template)
        self.assertIn('attentionSummary:', base_template)
        self.assertIn('attentionItems:', base_template)
        self.assertIn('attentionEventCursor:', base_template)
        self.assertIn('sidebarAttentionSummary:', base_template)
        self.assertIn('sidebarAttentionSnapshot:', base_template)
        self.assertIn('function setSidebarNavUnreadBadge(kind, count)', main_js)
        self.assertIn('function requestCanopySidebarAttentionRefresh(options)', main_js)
        self.assertIn('function startCanopyWorkspaceAttentionPolling()', main_js)
        self.assertIn('function pollCanopyWorkspaceAttentionEvents()', main_js)
        self.assertIn("requestCanopySidebarDmRefresh({ force: false }).catch(() => {});", main_js)

    def test_sidebar_cards_support_three_states_and_mini_player_placement(self) -> None:
        base_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn('id="sidebar-dm-card"', base_template)
        self.assertIn('id="sidebar-dm-toggle"', base_template)
        self.assertIn('id="sidebar-dm-expand-btn"', base_template)
        self.assertIn('id="sidebar-peers-card"', base_template)
        self.assertIn('id="sidebar-peers-toggle"', base_template)
        self.assertIn('id="sidebar-peers-expand-btn"', base_template)
        self.assertIn('id="sidebar-peers-open-modal"', base_template)
        self.assertIn('id="sidebar-media-mini-slot-top"', base_template)
        self.assertIn('id="sidebar-media-mini-slot-bottom"', base_template)
        self.assertIn('id="sidebar-media-mini-pin"', base_template)
        self.assertIn("const SIDEBAR_CARD_PEEK_LIMIT = 5;", main_js)
        self.assertIn("function toggleSidebarCardCollapsed(kind)", main_js)
        self.assertIn("function toggleSidebarCardExpansion(kind, totalCount)", main_js)
        self.assertIn("function updateSidebarCardChrome(kind, totalCount)", main_js)
        self.assertIn("function setCanopySidebarMiniPosition(nextPosition)", main_js)
        self.assertIn("setCanopySidebarMiniPosition(canopySidebarRailState.miniPosition);", main_js)

    def test_dm_search_uses_explicit_search_state_to_suspend_live_refresh(self) -> None:
        messages_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'messages.html').read_text(encoding='utf-8')
        self.assertIn("const DM_SEARCH_QUERY = ", messages_template)
        self.assertIn("function isDmSearchActive() {", messages_template)
        self.assertIn("if (dmEventPollInFlight || isDmSearchActive()) {", messages_template)
        self.assertIn("if (isDmSearchActive()) {\n            window.location.reload();\n            return;\n        }", messages_template)
        self.assertIn("if (!document.hidden && !isDmSearchActive()) {", messages_template)
        self.assertIn("return window.location.search.includes('search=');", messages_template)

    def test_channel_search_preserves_search_view_and_scrolls_to_top(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn("let currentChannelSearchQuery = '';", channels_template)
        self.assertIn("if (isSearchActive) {\n        return;\n    }", channels_template)
        self.assertIn("scrollToBottom: false,", channels_template)
        self.assertIn("forceScroll: opts.scrollToBottom !== false,", channels_template)
        self.assertIn("function rerunActiveChannelSearch(options = {}) {", channels_template)
        self.assertNotIn("if (isSearchActive) {\n        loadChannelMessages(currentChannelId, { forceScroll });", channels_template)

    def test_curated_channel_creation_and_member_policy_controls_exist(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn('name="create-channel-post-policy"', channels_template)
        self.assertIn('id="create-post-policy-curated"', channels_template)
        self.assertIn('Use curated posting when the channel should stay high-signal', channels_template)
        self.assertIn("const postPolicy = document.querySelector('input[name=\"create-channel-post-policy\"]:checked')?.value || 'open';", channels_template)
        self.assertIn('post_policy: postPolicy,', channels_template)
        self.assertIn("function renderChannelPostingPolicySummary(policy)", channels_template)
        self.assertIn("function grantChannelPoster(userId)", channels_template)
        self.assertIn("function revokeChannelPoster(userId)", channels_template)
        self.assertIn('Allow top-level posts', channels_template)

    def test_channel_header_responsive_layout_and_landscape_compaction_exist(self) -> None:
        channels_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'channels.html').read_text(encoding='utf-8')
        self.assertIn('@media (min-width: 768px) and (max-width: 1199.98px)', channels_template)
        self.assertIn('@media (max-width: 1024px) and (orientation: landscape) and (max-height: 520px)', channels_template)
        self.assertIn(".channel-post-policy-btn .privacy-label", channels_template)
        self.assertIn("label.textContent = curated ? 'Curated' : 'Open';", channels_template)
        self.assertIn("open: { text: 'Open', icon: 'bi-wifi', cls: 'btn-outline-secondary' },", channels_template)
        self.assertIn("#channel-posting-badge.open {", channels_template)
        self.assertIn("display: flex !important;", channels_template)

    def test_dashboard_flash_messages_null_check(self) -> None:
        dashboard_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'dashboard.html').read_text(encoding='utf-8')
        # Must guard against missing .flash-messages before injecting new API key alert
        self.assertIn("if (flashContainer) flashContainer.innerHTML += keyAlert;", dashboard_template)
        self.assertNotIn("document.querySelector('.flash-messages').innerHTML += keyAlert;", dashboard_template)
