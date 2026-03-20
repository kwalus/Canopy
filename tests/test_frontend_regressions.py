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
        self.assertIn("if (type === 'youtube' && miniVideoHost && !state.deckOpen && !isDockedInMiniHost(el) && isOffscreen(el)) {", main_js)
        self.assertIn("autoDockYouTube(entry.target);", main_js)
        self.assertIn("el.__canopyMiniYTDockResumeAt = getYouTubeCurrentTimeSafe(el);", main_js)
        self.assertIn("player.seekTo(resumeAt, true);", main_js)
        self.assertIn("maybeRestoreYouTubeDockState(el);", main_js)
        self.assertIn("Object.prototype.hasOwnProperty.call(el, '__canopyMiniYTDockResumeAt')", main_js)
        self.assertIn("const shouldResume = el.__canopyMiniYTDockShouldResume === true;", main_js)
        self.assertIn("url.searchParams.set('start', String(Math.max(0, Math.floor(resumeAt))));", main_js)
        self.assertIn("ytIframe.__canopyMiniYTLastTime = cur;", main_js)
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

    def test_sidebar_media_deck_expands_miniplayer_with_queue_and_stage(self) -> None:
        base_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn('id="sidebar-media-mini-expand"', base_template)
        self.assertIn('id="sidebar-media-deck"', base_template)
        self.assertIn('id="sidebar-media-deck-stage"', base_template)
        self.assertIn('id="sidebar-media-deck-queue"', base_template)
        self.assertIn('id="sidebar-media-deck-seek"', base_template)
        self.assertIn('id="sidebar-media-deck-prev"', base_template)
        self.assertIn('id="sidebar-media-deck-next"', base_template)
        self.assertIn('id="sidebar-media-deck-mini-player"', base_template)
        self.assertIn('id="sidebar-media-deck-minimize-footer"', base_template)
        self.assertIn('id="sidebar-media-deck-mini-player-footer"', base_template)
        self.assertIn(".sidebar-media-deck-shell", base_template)
        self.assertIn(".sidebar-media-deck-queue {", base_template)
        self.assertIn("overflow-x: auto;", base_template)
        self.assertIn("scroll-snap-type: x proximity;", base_template)
        self.assertIn("function buildRelatedMediaList(sourceEl, activeEl) {", main_js)
        self.assertIn("function openMediaDeck() {", main_js)
        self.assertIn("function closeMediaDeck(options = {}) {", main_js)
        self.assertIn("function renderDeckQueue() {", main_js)
        self.assertIn("function moveDockedMediaToHost(el, host) {", main_js)
        self.assertIn("function seekCurrentMediaTo(ratio) {", main_js)
        self.assertIn("deckQueue.addEventListener('click'", main_js)
        self.assertIn("deckSeek.addEventListener('change'", main_js)

    def test_media_deck_switching_uses_central_deactivation_and_disconnect_cleanup(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("function clearOrphanedDockedMedia(el, type, sourceEl) {", main_js)
        self.assertIn("function deactivateMediaEntry(entry, options = {}) {", main_js)
        self.assertIn("pauseMediaElement(el, type);", main_js)
        self.assertIn("// Re-assert pause after restoration so a switched-away item cannot", main_js)
        self.assertIn("clearOrphanedDockedMedia(el, type, entry.sourceEl || sourceContainer(el));", main_js)
        self.assertIn("deactivateMediaEntry(state.current);", main_js)
        self.assertIn("const staleCurrent = state.current;", main_js)
        self.assertIn("deactivateMediaEntry(staleCurrent);", main_js)

    def test_media_deck_adds_source_level_launcher_for_playable_posts_and_messages(self) -> None:
        base_template = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn(".canopy-media-deck-source-slot", base_template)
        self.assertIn(".canopy-media-deck-launcher", base_template)
        self.assertIn(".canopy-media-mini-launcher", base_template)
        self.assertIn(".canopy-media-deck-launcher-count", base_template)
        self.assertIn("touch-action: manipulation", base_template)
        self.assertIn("const mqCoarseOrNarrow = window.matchMedia('(max-width: 640px), (pointer: coarse)');", main_js)
        self.assertIn("function resolveSourceMediaDeckLauncherHost(sourceEl) {", main_js)
        self.assertIn("function openMediaDeckForSource(sourceEl) {", main_js)
        self.assertIn("function syncSourceMediaDeckLauncher(sourceEl) {", main_js)
        self.assertIn("function syncSourceMediaDeckLaunchersInScope(scope) {", main_js)
        self.assertIn("btnDeck.setAttribute('data-open-media-deck', '1');", main_js)
        self.assertIn("const actionsHost = sourceEl.querySelector('[data-post-actions] .d-flex.gap-2.flex-wrap, .message-actions .d-flex.gap-2.flex-wrap');", main_js)
        self.assertIn("slot.className = 'canopy-media-deck-source-slot';", main_js)
        self.assertIn("btnDeck.innerHTML = `<i class=\"bi bi-collection-play\"></i><span class=\"canopy-media-deck-launcher-label\">Media deck</span><span class=\"canopy-media-deck-launcher-count\">${countLabel}</span>`;", main_js)
        self.assertIn("state.deckSelectedKey = preferred.key;", main_js)
        self.assertIn("state.deckOpen = true;", main_js)
        self.assertIn("selectDeckItem(preferred, { play: false });", main_js)
        self.assertIn("function materializeYouTubeFacade(facade, options = {}) {", main_js)
        self.assertIn("url.searchParams.set('autoplay', options.autoplay === true ? '1' : '0');", main_js)
        self.assertIn("materializeYouTubeFacade(facade, { autoplay: true });", main_js)
        self.assertIn("sourceEl.querySelectorAll('audio, video, .youtube-embed').forEach(pushCandidate);", main_js)
        self.assertIn("const ytEl = resolveYouTubeMediaElement(el, { activate: true, autoplay: true });", main_js)
        self.assertIn("deferYouTubeMaterialize: true", main_js)
        self.assertIn("activate: !defer, autoplay: false", main_js)
        self.assertIn("function openMiniPlayerForSource(sourceEl) {", main_js)
        self.assertIn("function switchDeckToMiniPlayer() {", main_js)
        self.assertIn("btnMini.setAttribute('data-open-mini-player', '1');", main_js)
        self.assertIn("forceDockMini:", main_js)
        self.assertIn("function isYouTubeFacadeOnly(el) {", main_js)
        self.assertIn("function repairMediaCurrentReference() {", main_js)
        self.assertIn("function reconcileDeckStageMediaPlacement() {", main_js)
        self.assertIn("function prepareYouTubeEmbedForHostMove(el, opts) {", main_js)
        self.assertIn("function isSidebarDeckOrMiniHost(node) {", main_js)
        self.assertIn("skipResumeUrlRewrite:", main_js)
        self.assertIn("function resetYouTubePlayerBridge(iframe) {", main_js)
        self.assertIn("el.src = next;\n                        return true;", main_js)
        self.assertIn("syncSourceMediaDeckLaunchersInScope(root);", main_js)

    def test_media_deck_prefers_post_or_message_source_over_nested_card(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("const postOrMessage = el.closest('.post-card[data-post-id], .message-item[data-message-id]');", main_js)
        self.assertIn("return postOrMessage || el.closest('.card');", main_js)
        self.assertNotIn("return el.closest('.post-card[data-post-id], .message-item[data-message-id], .card');", main_js)

    def test_media_deck_optimizes_refresh_and_queue_rerenders(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("deckQueueSignature: ''", main_js)
        self.assertIn("deckSelectedKey: ''", main_js)
        self.assertIn("miniUpdateFrame: 0", main_js)
        self.assertIn("miniUpdateTimer: null", main_js)
        self.assertIn("function scheduleMiniUpdate(delay = 0) {", main_js)
        self.assertIn("state.miniUpdateFrame = window.requestAnimationFrame(() => {", main_js)
        self.assertIn("const nextSignature = `${activeKey}::${items.map((item) => `${item.key}:${item.type}`).join('|')}`;", main_js)
        self.assertIn("if (state.deckQueueSignature === nextSignature && deckQueue.childElementCount) {", main_js)
        self.assertIn("btn.setAttribute('aria-expanded', isActive && state.deckOpen ? 'true' : 'false');", main_js)
        self.assertIn("state.tickHandle = setInterval(scheduleMiniUpdate, 700);", main_js)
        self.assertNotIn("state.tickHandle = setInterval(updateMini, 700);", main_js)

    def test_media_deck_source_launch_and_return_use_source_first_behavior(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        base_html = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn("state.returnUrl = null;", main_js)
        self.assertIn("state.dockedSubtitle = null;", main_js)
        self.assertNotIn("const keepDeckVisible", main_js)
        self.assertIn("closeMediaDeck({ forceClose: true });", main_js)
        self.assertIn("<span>Show source</span>", base_html)
        self.assertIn("<span>Return to source</span>", base_html)

    def test_media_deck_open_state_is_authoritative_over_miniplayer_auto_logic(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("if (state.deckOpen) {", main_js)
        self.assertIn("updateDeckPanel();", main_js)
        self.assertIn("hideMini();", main_js)
        self.assertNotIn("const shouldKeepDeckSelection = !!(state.deckOpen && !state.dismissedEl && current.el);", main_js)

    def test_media_deck_selection_is_decoupled_from_immediate_playback(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("function getDeckSelectedItem() {", main_js)
        self.assertIn("function selectDeckItem(item, options = {}) {", main_js)
        self.assertIn("ensureMediaIdentity(state.current.el)", main_js)
        self.assertIn("pauseMediaElement(state.current.el, state.current.type);", main_js)
        self.assertIn("selectDeckItem(nextItem, { play: false });", main_js)
        self.assertIn("scrollDeckSelectionIntoView();", main_js)

    def test_media_deck_first_click_hardening(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        base_html = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn("if (e.defaultPrevented) return;", main_js)
        self.assertIn("!state.deckOpen &&", main_js)
        self.assertIn("if (state.deckOpen && state.current && state.current.sourceEl === source) return;", main_js)
        self.assertIn("[data-post-actions]", base_html)
        self.assertIn("z-index: 2;", base_html)

    def test_media_deck_mobile_viewport_fit(self) -> None:
        base_html = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn("body.canopy-media-deck-modal", base_html)
        self.assertIn("top: 0;", base_html)
        self.assertIn("height: 100dvh;", base_html)
        self.assertIn("max-height: 100dvh;", base_html)
        self.assertIn("overflow-y: auto;", base_html)
        self.assertIn("overscroll-behavior-y: contain;", base_html)
        self.assertIn(".sidebar-media-deck-action-label", base_html)
        self.assertIn("<span class=\"sidebar-media-deck-action-label\">Minimize</span>", base_html)
        self.assertIn("<span class=\"sidebar-media-deck-action-label\">Mini player</span>", base_html)
        self.assertIn("<span class=\"sidebar-media-deck-action-label\">Close</span>", base_html)
        self.assertIn("(min-height: 541px) and (max-height: 720px) and (orientation: landscape)", base_html)
        self.assertNotIn("min-height: min(86vh, 860px);", base_html)

    def test_media_deck_portal_is_body_level_for_ios_fixed_positioning(self) -> None:
        base_html = (ROOT / 'canopy' / 'ui' / 'templates' / 'base.html').read_text(encoding='utf-8')
        self.assertIn('data-canopy-deck-portal="1"', base_html)
        self.assertIn('canopy-media-deck-portal', base_html)
        self.assertGreater(
            base_html.find('data-canopy-deck-portal="1"'),
            base_html.find('<!-- Main Content -->'),
            'Deck portal should render after main layout (outside scroll/overflow sidebar stack)',
        )

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
