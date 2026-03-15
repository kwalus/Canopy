"""Regression guards for the shared rich embed provider surface."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TestEmbedFrontendRegressions(unittest.TestCase):
    def test_main_js_has_shared_provider_registry_for_new_embeds(self) -> None:
        main_js = (ROOT / "canopy" / "ui" / "static" / "js" / "canopy-main.js").read_text(encoding="utf-8")
        self.assertIn("const RICH_EMBED_PROVIDERS = [", main_js)
        self.assertIn("key: 'vimeo'", main_js)
        self.assertIn("key: 'loom'", main_js)
        self.assertIn("key: 'spotify'", main_js)
        self.assertIn("key: 'soundcloud'", main_js)
        self.assertIn("key: 'google_maps'", main_js)
        self.assertIn("key: 'openstreetmap'", main_js)
        self.assertIn("key: 'tradingview'", main_js)
        self.assertIn("key: 'direct_video'", main_js)
        self.assertIn("key: 'direct_audio'", main_js)
        self.assertIn("function collectProviderEmbeds(html)", main_js)
        self.assertIn("function isEmbedMatchInsideHtmlTag(html, matchIndex)", main_js)
        self.assertIn("if (isEmbedMatchInsideHtmlTag(html, matchIndex)) {", main_js)
        self.assertIn("function buildGoogleMapsEmbedUrl(rawUrl)", main_js)
        self.assertIn("function buildOpenStreetMapEmbedUrl(rawUrl)", main_js)
        self.assertIn("function buildTradingViewEmbedUrl(rawUrl)", main_js)
        self.assertIn("function parseTradingViewSymbol(rawUrl)", main_js)
        self.assertIn("buildIframeEmbedPreview(", main_js)
        self.assertIn("buildNativeMediaEmbed(", main_js)
        self.assertIn("buildProviderCardEmbed(", main_js)
        self.assertIn("const referrerPolicy = escapeEmbedAttr(options.referrerPolicy || 'strict-origin-when-cross-origin');", main_js)
        self.assertIn("google\\.[^\\/]+\\/maps(?:[/?#][^\\s<\"]*)?", main_js)
        self.assertIn("maps\\.app\\.goo\\.gl\\/?[^\\s<\"]*", main_js)
        self.assertIn("s.tradingview.com/widgetembed/?", main_js)
        self.assertIn("www.google.com/maps/embed/v1/search?key=", main_js)
        self.assertIn("referrerPolicy: 'no-referrer-when-downgrade'", main_js)

    def test_base_template_styles_support_iframe_cards_and_native_media(self) -> None:
        base_template = (ROOT / "canopy" / "ui" / "templates" / "base.html").read_text(encoding="utf-8")
        self.assertIn("googleMapsEmbedApiKey:", base_template)
        self.assertIn(".embed-preview::before", base_template)
        self.assertIn(".iframe-embed iframe,", base_template)
        self.assertIn(".native-media-embed video,", base_template)
        self.assertIn(".map-service-embed iframe", base_template)
        self.assertIn(".chart-service-embed iframe", base_template)
        self.assertIn(".provider-card-embed", base_template)
        self.assertIn(".provider-embed-card", base_template)
        self.assertIn(".provider-embed-card:focus-visible", base_template)
        self.assertIn(".embed-provider-pill", base_template)
        self.assertIn(".embed-provider-caption", base_template)
        self.assertIn(".spotify-embed { --embed-accent: #1db954; }", base_template)
        self.assertIn(".tradingview-card-embed,", base_template)
        self.assertIn(".tradingview-embed { --embed-accent: #4f8cff; }", base_template)

    def test_feed_template_uses_shared_provider_preview_language(self) -> None:
        feed_template = (ROOT / "canopy" / "ui" / "templates" / "feed.html").read_text(encoding="utf-8")
        self.assertIn(".embed-preview.iframe-embed iframe,", feed_template)
        self.assertIn(".embed-preview.map-service-embed iframe {", feed_template)
        self.assertIn(".embed-preview.chart-service-embed iframe {", feed_template)
        self.assertIn("embed shared Canopy provider previews", feed_template)
        self.assertIn(".provider-embed-card { padding: 12px 14px !important; }", feed_template)

    def test_math_rendering_only_enables_inline_dollars_for_likely_math(self) -> None:
        main_js = (ROOT / "canopy" / "ui" / "static" / "js" / "canopy-main.js").read_text(encoding="utf-8")
        self.assertIn("function hasExplicitMathDelimiters(text)", main_js)
        self.assertIn("function isLikelyMathInlineContent(content)", main_js)
        self.assertIn("function hasLikelyInlineMath(text)", main_js)
        self.assertIn("function buildMathDelimitersForText(text)", main_js)
        self.assertIn("const delimiters = buildMathDelimitersForText(sourceText);", main_js)
        self.assertIn("if (!delimiters.length) return false;", main_js)
        self.assertIn("if (hasLikelyInlineMath(value)) {", main_js)
        self.assertNotIn("{ left: '$', right: '$', display: false }\n                        ],", main_js)


if __name__ == "__main__":
    unittest.main()
