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

    def test_identity_modal_treats_local_peer_origin_as_local(self) -> None:
        main_js = (ROOT / 'canopy' / 'ui' / 'static' / 'js' / 'canopy-main.js').read_text(encoding='utf-8')
        self.assertIn("const canopyLocalPeerId = window.CANOPY_VARS ? String(window.CANOPY_VARS.localPeerId || '').trim() : '';", main_js)
        self.assertIn("const originPeer = (canopyLocalPeerId && originPeerRaw === canopyLocalPeerId) ? '' : originPeerRaw;", main_js)


if __name__ == '__main__':
    unittest.main()
