/* Canopy common UI JavaScript — extracted from base.html */
        // Common utility functions
        function parseCanopyTimestamp(timestamp) {
            if (timestamp === null || timestamp === undefined || timestamp === '') return null;
            if (timestamp instanceof Date) {
                return Number.isNaN(timestamp.getTime()) ? null : timestamp;
            }
            if (typeof timestamp === 'number') {
                const value = timestamp > 1e12 ? timestamp : timestamp * 1000;
                const date = new Date(value);
                return Number.isNaN(date.getTime()) ? null : date;
            }

            const raw = String(timestamp).trim();
            if (!raw) return null;

            if (/^\d+$/.test(raw)) {
                const numeric = Number(raw);
                const value = numeric > 1e12 ? numeric : numeric * 1000;
                const fromNumeric = new Date(value);
                if (!Number.isNaN(fromNumeric.getTime())) return fromNumeric;
            }

            let normalized = raw;
            if (/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$/.test(raw)) {
                normalized = raw.replace(' ', 'T') + 'Z';
            } else if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$/.test(raw)) {
                normalized = raw + 'Z';
            } else if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) {
                normalized = raw + 'T00:00:00Z';
            }

            const parsed = new Date(normalized);
            if (!Number.isNaN(parsed.getTime())) return parsed;

            const fallback = new Date(raw);
            return Number.isNaN(fallback.getTime()) ? null : fallback;
        }

        function formatTimestamp(timestamp) {
            const date = parseCanopyTimestamp(timestamp);
            if (!date) return String(timestamp || '');

            const now = new Date();
            const diff = now.getTime() - date.getTime();

            if (diff < -60000) return date.toLocaleString();
            if (diff < 60000) return 'Just now';
            if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
            if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
            return date.toLocaleString();
        }
        window.parseCanopyTimestamp = parseCanopyTimestamp;
        window.formatCanopyTimestamp = formatTimestamp;
        
        function showAlert(message, type = 'info') {
            const alertDiv = document.createElement('div');
            alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
            alertDiv.innerHTML = `
                ${message}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            `;
            
            const container = document.querySelector('.flash-messages');
            container.appendChild(alertDiv);
            
            // Auto-dismiss after 5 seconds
            setTimeout(() => {
                if (alertDiv.parentNode) {
                    alertDiv.remove();
                }
            }, 5000);
	        }

        // --- Peer/user avatar helpers (stacked avatars) ---
        const canopyPeerProfiles = window.CANOPY_VARS ? window.CANOPY_VARS.peerProfiles : {};
        window.canopyPeerProfiles = canopyPeerProfiles || {};

        function canopyInitial(label) {
            const text = (label || '?').trim();
            return text ? text[0].toUpperCase() : '?';
        }

        window.canopyPeerDisplayName = function(peerId) {
            if (!peerId) return '';
            const profile = window.canopyPeerProfiles ? window.canopyPeerProfiles[peerId] : null;
            if (profile && profile.display_name) return profile.display_name;
            const nameEl = document.querySelector(`.sidebar-peer[data-peer-id="${peerId}"] .sidebar-peer-name`);
            if (nameEl && nameEl.textContent) return nameEl.textContent.trim();
            return peerId.slice(0, 12);
        };

        window.canopyPeerAvatarSrc = function(peerId) {
            if (!peerId) return null;
            const profile = window.canopyPeerProfiles ? window.canopyPeerProfiles[peerId] : null;
            if (profile && profile.avatar_b64) {
                const mime = profile.avatar_mime || 'image/png';
                return `data:${mime};base64,${profile.avatar_b64}`;
            }
            const imgEl = document.querySelector(`.sidebar-peer[data-peer-id="${peerId}"] .sidebar-peer-avatar img`);
            const src = imgEl ? imgEl.getAttribute('src') : null;
            return src || null;
        };

        window.renderAvatarStack = function(container, options) {
            if (!container || !options) return;
            const userLabel = options.userLabel || options.userId || 'User';
            const userAvatarUrl = options.userAvatarUrl || null;
            const peerId = options.peerId || null;

            container.innerHTML = '';

            const stack = document.createElement('div');
            stack.className = 'avatar-stack';

            const userEl = document.createElement('div');
            userEl.className = 'avatar-user';
            if (userAvatarUrl) {
                const img = document.createElement('img');
                img.src = userAvatarUrl;
                img.alt = userLabel;
                userEl.appendChild(img);
            } else {
                userEl.textContent = canopyInitial(userLabel);
            }
            stack.appendChild(userEl);

            if (peerId) {
                const peerEl = document.createElement('div');
                peerEl.className = 'avatar-peer';
                const peerLabel = window.canopyPeerDisplayName ? window.canopyPeerDisplayName(peerId) : peerId;
                const peerSrc = window.canopyPeerAvatarSrc ? window.canopyPeerAvatarSrc(peerId) : null;
                if (peerSrc) {
                    const img = document.createElement('img');
                    img.src = peerSrc;
                    img.alt = peerLabel || peerId;
                    peerEl.appendChild(img);
                } else {
                    peerEl.textContent = canopyInitial(peerLabel || peerId);
                }
                stack.appendChild(peerEl);
            }

            container.appendChild(stack);
        };

        window.applyInlineTaskAvatars = function(root) {
            const scope = root || document;
            const nodes = scope.querySelectorAll('.inline-task-avatar');
            nodes.forEach(node => {
                const userId = node.dataset.userId || '';
                const userName = node.dataset.userName || userId || 'User';
                const userAvatar = node.dataset.userAvatar || null;
                const peerId = node.dataset.peerId || null;
                if (window.renderAvatarStack) {
                    window.renderAvatarStack(node, {
                        userId: userId,
                        userLabel: userName,
                        userAvatarUrl: userAvatar,
                        peerId: peerId
                    });
                }
            });
        };

        // --- Inline task helpers ---
        window.findTaskBlocks = function(text) {
            if (!text) return [];
            const blocks = [];
            const regex = /\[task\]([\s\S]*?)\[\/task\]/gi;
            let match;
            while ((match = regex.exec(text)) !== null) {
                const body = match[1] || '';
                const confirmMatch = body.match(/^\s*confirm\s*:\s*(.+)$/gim);
                let confirmed = true;
                if (confirmMatch && confirmMatch.length) {
                    const raw = confirmMatch[confirmMatch.length - 1];
                    const value = (raw.split(':')[1] || '').trim().toLowerCase();
                    if (['false', 'no', 'off', '0'].includes(value)) confirmed = false;
                    if (['true', 'yes', 'on', '1'].includes(value)) confirmed = true;
                }
                blocks.push({
                    raw: match[0],
                    body: body,
                    confirmed: confirmed
                });
            }
            return blocks;
        };

        window.applyTaskConfirmationToContent = function(text, confirmed) {
            if (!text) return text;
            const regex = /\[task\]([\s\S]*?)\[\/task\]/gi;
            return text.replace(regex, (match, body) => {
                const hasConfirm = /^\s*confirm\s*:/gim.test(body);
                let updated = body;
                if (hasConfirm) {
                    updated = body.replace(/^\s*confirm\s*:.*$/gim, `confirm: ${confirmed ? 'true' : 'false'}`);
                } else if (!confirmed) {
                    updated = `${body.trim()}\nconfirm: false\n`;
                }
                return `[task]\n${updated.trim()}\n[/task]`;
            });
        };
	        
	        // --- Rich link embed rendering ---
	        function canopyEmbedTheme() {
	            // Twitter/X widgets only support "light" or "dark". Map the app theme.
	            const theme = document.documentElement.getAttribute('data-theme') || 'dark';
	            return theme === 'light' ? 'light' : 'dark';
	        }

	        function ensureTwitterWidgetsLoaded() {
	            // Load https://platform.twitter.com/widgets.js only when needed.
	            if (window.twttr && window.twttr.widgets) {
	                return Promise.resolve(window.twttr);
	            }
	            if (window.__canopyTwitterWidgetsPromise) {
	                return window.__canopyTwitterWidgetsPromise;
	            }

	            window.__canopyTwitterWidgetsPromise = new Promise((resolve, reject) => {
	                const waitForTwttr = (maxMs = 8000) => {
	                    const start = Date.now();
	                    (function poll() {
	                        if (window.twttr && window.twttr.widgets) {
	                            resolve(window.twttr);
	                            return;
	                        }
	                        if (Date.now() - start > maxMs) {
	                            reject(new Error('Twitter widgets load timeout'));
	                            return;
	                        }
	                        setTimeout(poll, 50);
	                    })();
	                };

	                const existing = document.querySelector('script[data-canopy-twitter-widgets="1"]');
	                if (existing) {
	                    waitForTwttr();
	                    return;
	                }

	                const s = document.createElement('script');
	                s.src = 'https://platform.twitter.com/widgets.js';
	                s.async = true;
	                s.defer = true;
	                s.charset = 'utf-8';
	                s.setAttribute('data-canopy-twitter-widgets', '1');
	                s.onload = () => waitForTwttr();
	                s.onerror = reject;
	                document.head.appendChild(s);
	            });

	            return window.__canopyTwitterWidgetsPromise;
	        }

	        function hydrateXEmbeds(root = document) {
	            // Upgrade fallback cards into real embedded posts (best-effort).
	            try {
	                const scope = (root instanceof Element || root instanceof Document) ? root : document;
	                const embeds = scope.querySelectorAll('.x-embed[data-x-status-id]:not([data-canopy-x-processed])');
	                if (!embeds.length) return;

	                embeds.forEach(el => el.setAttribute('data-canopy-x-processed', '1'));

	                ensureTwitterWidgetsLoaded()
	                    .then(() => {
	                        if (!(window.twttr && window.twttr.widgets && typeof window.twttr.widgets.createTweet === 'function')) {
	                            return;
	                        }
	                        embeds.forEach(container => {
	                            const statusId = container.getAttribute('data-x-status-id');
	                            const renderTarget = container.querySelector('.x-embed-render');
	                            if (!statusId || !renderTarget) return;
	                            if (container.classList.contains('is-rendered')) return;

	                            const theme = container.getAttribute('data-x-theme') || canopyEmbedTheme();
	                            renderTarget.innerHTML = '';
	                            window.twttr.widgets.createTweet(statusId, renderTarget, {
	                                theme: theme,
	                                dnt: true,
	                                conversation: 'none',
	                            }).then(() => {
	                                container.classList.add('is-rendered');
	                            }).catch(() => {
	                                // Leave the fallback card visible
	                            });
	                        });
	                    })
	                    .catch(() => {
	                        // Leave the fallback cards visible
	                    });
	            } catch (e) {
	                // no-op
	            }
	        }

	        function linkifyMentions(text) {
	            if (!text || text.indexOf('@') === -1) return text;
	            const map = (typeof window !== 'undefined' && window.mentionDisplayMap) || {};
	            const mentionRegex = /(^|[^A-Za-z0-9_.\-@])@([A-Za-z0-9](?:[A-Za-z0-9_.\-]{0,47}[A-Za-z0-9]))/g;
	            return text.replace(mentionRegex, function(match, prefix, handle) {
	                var display = map[handle];
	                if (display == null) display = handle;
	                var div = document.createElement('div');
	                div.textContent = display;
	                var escaped = div.innerHTML;
	                return (prefix || '') + '<span class="mention-tag" data-mention="' + handle + '" title="@' + handle + '">@' + escaped + '</span>';
	            });
	        }

            function _escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text == null ? '' : String(text);
                return div.innerHTML;
            }

            let _channelIndexPromise = null;
            let _channelIndexMap = null;
            let _channelIndexList = null;

            function setChannelIndex(channels) {
                _channelIndexList = Array.isArray(channels) ? channels : [];
                const map = {};
                _channelIndexList.forEach(ch => {
                    const name = (ch.name || '').trim();
                    if (!name) return;
                    map[name.toLowerCase()] = { id: ch.id, name: name };
                });
                _channelIndexMap = map;
                if (typeof window !== 'undefined') {
                    window.channelIndexMap = map;
                    window.channelIndexList = _channelIndexList;
                }
                return map;
            }

            function ensureChannelIndex() {
                if (_channelIndexMap) return Promise.resolve(_channelIndexMap);
                if (_channelIndexPromise) return _channelIndexPromise;
                _channelIndexPromise = apiCall('/ajax/channel_suggestions')
                    .then(data => {
                        if (data && data.success && Array.isArray(data.channels)) {
                            return setChannelIndex(data.channels);
                        }
                        return setChannelIndex([]);
                    })
                    .catch(() => setChannelIndex([]));
                return _channelIndexPromise;
            }

            function linkifyChannels(text) {
                if (!text || text.indexOf('#') === -1) return text;
                const map = _channelIndexMap || (typeof window !== 'undefined' && window.channelIndexMap) || {};
                if (!map || Object.keys(map).length === 0) return text;
                const channelRegex = /(^|[\s\(\[\{<>"'.,;:!?])#([A-Za-z0-9][A-Za-z0-9_.-]{0,79})/g;
                return text.replace(channelRegex, function(match, prefix, name) {
                    const key = (name || '').toLowerCase();
                    const info = map[key];
                    if (!info || !info.id) return match;
                    const safeName = _escapeHtml(info.name || name);
                    const href = '/channels?channel=' + encodeURIComponent(info.id);
                    return (prefix || '') + '<a class="channel-tag" href="' + href + '" data-channel-id="' +
                        _escapeHtml(info.id) + '" data-channel-name="' + safeName + '">#' + safeName + '</a>';
                });
            }

	        function renderRichContent(text) {
	            if (!text) return '';

	            // Escape HTML first to prevent XSS
            const div = document.createElement('div');
            div.textContent = text;
            let html = div.innerHTML;

            // Preserve code blocks without parsing mentions/links inside them
            const codeBlocks = [];
            const CODE_PLACEHOLDER = '\x00CODE_';
            html = html.replace(/```[\s\S]*?```/g, function(match) {
                var inner = match.slice(3, -3).trim();
                const idx = codeBlocks.length;
                codeBlocks.push('<div class="channel-code-wrap position-relative mb-2">' +
                    '<button type="button" class="channel-code-copy-btn btn btn-sm position-absolute top-0 end-0 m-1" title="Copy to clipboard" aria-label="Copy">' +
                    '<i class="bi bi-clipboard"></i></button>' +
                    '<pre class="channel-code p-2 pe-4 rounded mb-0" style="background:var(--canopy-bg-tertiary); border:1px solid var(--canopy-border); overflow-x:auto;"><code>' + inner + '</code></pre></div>');
                return CODE_PLACEHOLDER + idx + '\x00';
            });

            html = linkifyMentions(html);
            html = linkifyChannels(html);
            // Post links: [post:POST_ID] → link to feed view (humans and agents can share direct post links)
            html = html.replace(/\[post:([A-Za-z0-9_-]+)\]/g, function(match, postId) {
                var safeId = ('' + postId).replace(/"/g, '&quot;');
                return '<a href="/feed?post=' + encodeURIComponent(postId) + '" class="post-link" data-post-id="' + safeId + '" ' +
                    'style="color:var(--canopy-primary,#22c55e);">View post</a>';
            });
            // Channel message links: [msg:MESSAGE_ID] → open channel and scroll to that message (e.g. "see my message above")
            html = html.replace(/\[msg:([A-Za-z0-9_-]+)\]/g, function(match, messageId) {
                var safeId = ('' + messageId).replace(/"/g, '&quot;');
                return '<a href="/channels/locate?message_id=' + encodeURIComponent(messageId) + '" class="msg-link" data-message-id="' + safeId + '" ' +
                    'style="color:var(--canopy-primary,#22c55e);">View message</a>';
            });
            // Markdown-style image: ![alt](path) — allow /static/, /files/<id>, and /custom_emojis/
            html = html.replace(/!\[([^\]]*)\]\((\/static\/[^)]+)\)/g, function(match, alt, src) {
                return '<img src="' + src + '" alt="' + (alt || '').replace(/"/g, '&quot;') + '" class="channel-inline-image" style="max-width:120px;height:auto;vertical-align:middle;border-radius:8px;">';
            });
            html = html.replace(/!\[([^\]]*)\]\((\/files\/[A-Za-z0-9_-]+)\)/g, function(match, alt, src) {
                var altEsc = (alt || '').replace(/"/g, '&quot;');
                return '<span class="channel-inline-image-wrap"><img src="' + src + '" alt="' + altEsc + '" class="channel-inline-image" style="max-width:100%;max-height:400px;height:auto;vertical-align:middle;border-radius:8px;" onerror="this.onerror=null;this.style.display=\'none\';var s=this.nextElementSibling;if(s)s.classList.remove(\'d-none\');"><span class="d-none small text-muted">Image unavailable</span></span>';
            });
            html = html.replace(/!\[([^\]]*)\]\((\/custom_emojis\/[^)]+)\)/g, function(match, alt, src) {
                var altEsc = (alt || '').replace(/"/g, '&quot;');
                return '<img src="' + src + '" alt="' + altEsc + '" class="inline-emoji" style="width:1.2em;height:1.2em;vertical-align:-0.2em;border-radius:6px;">';
            });
            // Markdown-style links: [text](url) — convert to clickable <a> tags
            // Must come BEFORE newline-to-br and URL linkification so the URL isn't double-processed
            html = html.replace(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/g, function(match, text, url) {
                var safeText = text.replace(/</g, '&lt;').replace(/>/g, '&gt;');
                var safeUrl = url.replace(/"/g, '&quot;');
                return '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer" ' +
                    'style="color:var(--canopy-primary,#22c55e);">' + safeText + '</a>';
            });
            html = html.replace(/\n/g, '<br>');

            // Collect embeds separately so we can grid them if >1
            const embeds = [];
            const EMBED_PLACEHOLDER = '\x00EMB_';

            // --- YouTube embeds ---
            const ytRegex = /(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/)([\w-]{11})(?:[&?]\S*)?/g;
            html = html.replace(ytRegex, function(match, videoId) {
                const idx = embeds.length;
                embeds.push('<div class="embed-preview youtube-embed" data-video-id="' + videoId + '">' +
                    '<iframe src="https://www.youtube-nocookie.com/embed/' + videoId + '?enablejsapi=1&playsinline=1&rel=0&origin=' + encodeURIComponent(window.location.origin) + '" ' +
                    'data-video-id="' + videoId + '" title="YouTube video ' + videoId + '" ' +
                    'frameborder="0" allowfullscreen ' +
                    'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" ' +
                    'loading="lazy"></iframe></div>');
                return EMBED_PLACEHOLDER + idx + '\x00';
	            });

	            // --- X / Twitter embeds ---
	            const xRegex = /https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)\/(?:([\w]+)\/status\/|i\/web\/status\/|i\/status\/)(\d+)(?:\?\S*)?/g;
	            html = html.replace(xRegex, function(match, username, statusId) {
	                const url = username
	                    ? ('https://x.com/' + username + '/status/' + statusId)
	                    : ('https://x.com/i/web/status/' + statusId);
	                const label = username ? ('@' + username) : 'X post';
                    const idx = embeds.length;
	                embeds.push('<div class="embed-preview x-embed" data-x-status-id="' + statusId + '" ' +
	                    'data-x-username="' + (username || '') + '" data-x-theme="' + canopyEmbedTheme() + '">' +
	                    '<div class="x-embed-card" onclick="window.open(\'' + url + '\',\'_blank\')">' +
	                    '<div class="d-flex align-items-center gap-2">' +
	                    '<i class="bi bi-twitter-x x-icon"></i>' +
	                    '<div class="flex-grow-1">' +
	                    '<strong>' + label + '</strong>' +
	                    '<div class="text-muted small">View post on X</div>' +
	                    '</div>' +
	                    '<i class="bi bi-box-arrow-up-right x-link-arrow"></i>' +
	                    '</div>' +
	                    '</div>' +
	                    '<div class="x-embed-render"></div>' +
	                    '</div>');
                    return EMBED_PLACEHOLDER + idx + '\x00';
	            });

	            // --- Generic URL linkification ---
            // Strip trailing punctuation that's likely not part of the URL (e.g. trailing ) , . ; : ! ?)
            html = html.replace(/(?<!src="|href="|onclick="window\.open\(')(https?:\/\/[^\s<"]+)/g, function(match) {
                // Don't linkify URLs already inside an href (from markdown link conversion above)
                // Check if we're already inside an <a> tag
                var url = match;
                // Strip trailing punctuation that is almost never part of a URL
                var trailingPunct = '';
                while (url.length > 10 && /[).,;:!?\]}>]$/.test(url)) {
                    // Keep ) if there's a matching ( in the URL (e.g. Wikipedia links)
                    if (url.endsWith(')')) {
                        var opens = (url.match(/\(/g) || []).length;
                        var closes = (url.match(/\)/g) || []).length;
                        if (opens >= closes) break; // balanced or more opens — keep the )
                    }
                    trailingPunct = url.slice(-1) + trailingPunct;
                    url = url.slice(0, -1);
                }
                return '<a href="' + url + '" target="_blank" rel="noopener noreferrer" ' +
                    'style="color:var(--canopy-primary,#22c55e);">' + url + '</a>' + trailingPunct;
            });

            // Re-insert embeds — if multiple, wrap in a grid
            if (embeds.length > 1) {
                // Remove placeholders from inline text
                let textPart = html;
                for (let i = 0; i < embeds.length; i++) {
                    textPart = textPart.replace(EMBED_PLACEHOLDER + i + '\x00', '');
                }
                textPart = textPart.trim();
                let out = '';
                if (textPart) {
                    const hasCode = textPart.includes(CODE_PLACEHOLDER);
                    if (hasCode) {
                        out += '<div class="mb-1">' + textPart + '</div>';
                    } else {
                        out += '<p class="mb-1">' + textPart + '</p>';
                    }
                }
                out += '<div class="embed-grid">' + embeds.join('') + '</div>';
                html = out;
            } else if (embeds.length === 1) {
                // Single embed: put it back inline
                html = html.replace(EMBED_PLACEHOLDER + '0\x00', embeds[0]);
            }

            if (codeBlocks.length) {
                for (let i = 0; i < codeBlocks.length; i++) {
                    html = html.replace(CODE_PLACEHOLDER + i + '\x00', codeBlocks[i]);
                }
            }

            // Wrap in paragraph or div (div if we have block elements like <pre> so markup stays valid)
            if (!html.includes('embed-preview') && !html.includes('embed-grid')) {
                if (html.includes('<pre') || html.includes('<pre ')) {
                    html = '<div class="rich-content">' + html + '</div>';
                } else {
                    html = '<p class="mb-0">' + html + '</p>';
                }
            } else if (!html.startsWith('<p') && !html.startsWith('<div')) {
                html = '<div class="rich-content">' + html + '</div>';
            }

            return html;
	        }

	        // Helper to process all elements matching a selector through renderRichContent
	        function processRichEmbeds(selector) {
	            document.querySelectorAll(selector).forEach(function(el) {
	                const rawText = el.textContent.trim();
	                if (!rawText) return;
	                // Process if it contains a URL worth embedding, markdown image, or code blocks
	                var shouldProcess = /https?:\/\//.test(rawText) || /\]\(\/files\//.test(rawText) || /!\[/.test(rawText) || /```/.test(rawText);
	                if (shouldProcess) {
	                    const rendered = renderRichContent(rawText);
	                    if (el.tagName === 'P') {
	                        // Avoid invalid <p><div>... nesting once we add embeds.
	                        const repl = document.createElement('div');
	                        repl.className = el.className;
	                        repl.innerHTML = rendered;
	                        el.replaceWith(repl);
	                    } else {
	                        el.innerHTML = rendered;
	                    }
	                }
	            });

	            // After replacing HTML, hydrate any X/Twitter embeds.
	            if (typeof hydrateXEmbeds === 'function') {
	                hydrateXEmbeds(document);
	            }
	        }

        // --- Global Lightbox for full-res image viewing ---
        let _lightboxUrls = [];
        let _lightboxIdx = 0;
        let _lightboxEl = null;

        function openLightbox(index, encodedUrls) {
            try {
                _lightboxUrls = JSON.parse(decodeURIComponent(encodedUrls));
            } catch (e) {
                _lightboxUrls = [];
            }
            _lightboxIdx = index;
            _showLightbox();
        }

        function viewImage(imageUrl) {
            _lightboxUrls = [imageUrl];
            _lightboxIdx = 0;
            _showLightbox();
        }

        function _showLightbox() {
            if (_lightboxEl) _closeLightbox();

            const lb = document.createElement('div');
            lb.className = 'canopy-lightbox';
            _lightboxEl = lb;

            const url = _lightboxUrls[_lightboxIdx] || '';
            const multi = _lightboxUrls.length > 1;

            lb.innerHTML = `
                <button class="lb-close" title="Close"><i class="bi bi-x-lg"></i></button>
                <a class="lb-download" href="${url}" download title="Download full resolution"><i class="bi bi-download"></i></a>
                ${multi ? `<button class="lb-prev" title="Previous"><i class="bi bi-chevron-left"></i></button>` : ''}
                ${multi ? `<button class="lb-next" title="Next"><i class="bi bi-chevron-right"></i></button>` : ''}
                <img src="${url}" alt="Full resolution">
                ${multi ? `<div class="lb-counter">${_lightboxIdx + 1} / ${_lightboxUrls.length}</div>` : ''}
            `;

            document.body.appendChild(lb);
            requestAnimationFrame(() => lb.classList.add('show'));

            lb.querySelector('.lb-close').addEventListener('click', _closeLightbox);
            lb.addEventListener('click', (e) => { if (e.target === lb) _closeLightbox(); });

            if (multi) {
                lb.querySelector('.lb-prev').addEventListener('click', () => _lightboxNav(-1));
                lb.querySelector('.lb-next').addEventListener('click', () => _lightboxNav(1));
            }

            document.addEventListener('keydown', _lightboxKeyHandler);
        }

        function _lightboxNav(dir) {
            _lightboxIdx = (_lightboxIdx + dir + _lightboxUrls.length) % _lightboxUrls.length;
            if (!_lightboxEl) return;
            const url = _lightboxUrls[_lightboxIdx];
            _lightboxEl.querySelector('img').src = url;
            _lightboxEl.querySelector('.lb-download').href = url;
            const counter = _lightboxEl.querySelector('.lb-counter');
            if (counter) counter.textContent = `${_lightboxIdx + 1} / ${_lightboxUrls.length}`;
        }

        function _lightboxKeyHandler(e) {
            if (!_lightboxEl) return;
            if (e.key === 'Escape') _closeLightbox();
            else if (e.key === 'ArrowLeft') _lightboxNav(-1);
            else if (e.key === 'ArrowRight') _lightboxNav(1);
        }

        function _closeLightbox() {
            document.removeEventListener('keydown', _lightboxKeyHandler);
            if (_lightboxEl) {
                _lightboxEl.classList.remove('show');
                setTimeout(() => {
                    if (_lightboxEl && _lightboxEl.parentNode) {
                        _lightboxEl.parentNode.removeChild(_lightboxEl);
                    }
                    _lightboxEl = null;
                }, 200);
            }
        }

        // --- User identity card (clickable avatars) ---
        let _userIdentityModal = null;
        const _userIdentityCache = {};
        let _userIdentityRequestSeq = 0;

        function _safeImageSrc(value) {
            const src = String(value || '').trim();
            if (!src) return '';
            if (src.startsWith('/')) return src;
            if (src.startsWith('data:image/')) return src;
            if (src.startsWith('https://') || src.startsWith('http://')) return src;
            return '';
        }

        function _avatarSrcFromElement(el) {
            if (!el) return '';
            const userImg = el.querySelector('.avatar-user img');
            if (userImg) return _safeImageSrc(userImg.getAttribute('src') || userImg.src);
            const anyImg = el.querySelector('img');
            if (anyImg) return _safeImageSrc(anyImg.getAttribute('src') || anyImg.src);
            return '';
        }

        function _copyTextToClipboard(value, label) {
            const text = String(value || '').trim();
            if (!text) {
                showAlert(`No ${label || 'value'} available to copy`, 'warning');
                return;
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(() => {
                    showAlert(`Copied ${label || 'value'} to clipboard`, 'success');
                }).catch(() => {
                    const ta = document.createElement('textarea');
                    ta.value = text;
                    ta.style.position = 'fixed';
                    ta.style.opacity = '0';
                    document.body.appendChild(ta);
                    ta.select();
                    document.execCommand('copy');
                    document.body.removeChild(ta);
                    showAlert(`Copied ${label || 'value'} to clipboard`, 'success');
                });
                return;
            }
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
            showAlert(`Copied ${label || 'value'} to clipboard`, 'success');
        }

        function _ensureUserIdentityModal() {
            if (_userIdentityModal) return _userIdentityModal;
            let modalEl = document.getElementById('userIdentityModal');
            if (!modalEl) {
                const modalHtml = `
                    <div class="modal fade" id="userIdentityModal" tabindex="-1" aria-labelledby="userIdentityModalLabel" aria-hidden="true">
                        <div class="modal-dialog modal-dialog-centered modal-sm">
                            <div class="modal-content user-identity-modal-content">
                                <div class="modal-header py-2">
                                    <h5 class="modal-title" id="userIdentityModalLabel">
                                        <i class="bi bi-person-badge me-2"></i>Identity
                                    </h5>
                                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>
                                </div>
                                <div class="modal-body">
                                    <div class="user-identity-hero">
                                        <div id="user-identity-avatar" class="user-identity-avatar"></div>
                                        <div class="user-identity-meta">
                                            <div id="user-identity-display" class="user-identity-display">User</div>
                                            <div id="user-identity-subtitle" class="user-identity-subtitle">Loading...</div>
                                        </div>
                                    </div>
                                    <div id="user-identity-fields" class="user-identity-fields"></div>
                                    <div class="user-identity-actions">
                                        <button type="button" class="btn btn-outline-secondary btn-sm" id="user-identity-copy-user-id">
                                            <i class="bi bi-clipboard me-1"></i>Copy User ID
                                        </button>
                                        <button type="button" class="btn btn-outline-success btn-sm" id="user-identity-copy-mention">
                                            <i class="bi bi-at me-1"></i>Copy @mention
                                        </button>
                                    </div>
                                </div>
                            </div>
                        </div>
                    </div>`;
                document.body.insertAdjacentHTML('beforeend', modalHtml);
                modalEl = document.getElementById('userIdentityModal');
            }
            if (!modalEl) return null;
            if (modalEl.dataset.boundIdentity !== '1') {
                modalEl.dataset.boundIdentity = '1';
                const copyIdBtn = modalEl.querySelector('#user-identity-copy-user-id');
                const copyMentionBtn = modalEl.querySelector('#user-identity-copy-mention');
                if (copyIdBtn) {
                    copyIdBtn.addEventListener('click', () => {
                        _copyTextToClipboard(copyIdBtn.getAttribute('data-copy-value') || '', 'user ID');
                    });
                }
                if (copyMentionBtn) {
                    copyMentionBtn.addEventListener('click', () => {
                        _copyTextToClipboard(copyMentionBtn.getAttribute('data-copy-value') || '', '@mention');
                    });
                }
            }
            _userIdentityModal = modalEl;
            return modalEl;
        }

        function _appendIdentityField(parent, label, value, copyValue, monospace) {
            const text = String(value || '').trim();
            if (!text || !parent) return;

            const row = document.createElement('div');
            row.className = 'user-identity-field-row';

            const left = document.createElement('div');
            left.className = 'user-identity-field-left';

            const labelEl = document.createElement('div');
            labelEl.className = 'user-identity-field-label';
            labelEl.textContent = label;

            const valueEl = document.createElement(monospace ? 'code' : 'div');
            valueEl.className = monospace ? 'user-identity-field-value mono' : 'user-identity-field-value';
            valueEl.textContent = text;

            left.appendChild(labelEl);
            left.appendChild(valueEl);

            const copyBtn = document.createElement('button');
            copyBtn.type = 'button';
            copyBtn.className = 'btn btn-outline-secondary btn-sm user-identity-copy-btn';
            copyBtn.innerHTML = '<i class="bi bi-clipboard"></i>';
            copyBtn.title = `Copy ${label}`;
            copyBtn.addEventListener('click', () => {
                _copyTextToClipboard(copyValue || text, label);
            });

            row.appendChild(left);
            row.appendChild(copyBtn);
            parent.appendChild(row);
        }

        function _userIdentityInfoFromPayload(userId, displayName, triggerEl, info) {
            const source = (info && typeof info === 'object') ? info : {};
            const originPeerFromEl = (triggerEl && triggerEl.dataset) ? (triggerEl.dataset.originPeer || '') : '';
            const usernameRaw = String(source.username || '').trim();
            const username = usernameRaw || userId;
            const accountTypeRaw = String(source.account_type || '').trim().toLowerCase();
            const accountType = accountTypeRaw || 'human';
            const statusRaw = String(source.status || '').trim().toLowerCase();
            const status = statusRaw || (accountType === 'agent' ? 'active' : 'active');
            const originPeer = String(source.origin_peer || originPeerFromEl || '').trim();
            const isRemote = originPeer ? true : !!source.is_remote;

            const display = (
                String(source.display_name || '').trim()
                || String(displayName || '').trim()
                || username
                || userId
            );

            const avatar = _safeImageSrc(source.avatar_url || _avatarSrcFromElement(triggerEl));
            const peerDisplay = originPeer
                ? ((window.canopyPeerDisplayName && window.canopyPeerDisplayName(originPeer)) || `${originPeer.slice(0, 12)}...`)
                : '';
            const peerAvatar = originPeer
                ? _safeImageSrc((window.canopyPeerAvatarSrc && window.canopyPeerAvatarSrc(originPeer)) || '')
                : '';

            return {
                user_id: userId,
                display_name: display,
                username: username,
                avatar_url: avatar,
                account_type: accountType,
                status: status,
                origin_peer: originPeer || '',
                is_remote: isRemote,
                peer_display_name: peerDisplay,
                peer_avatar_url: peerAvatar,
            };
        }

        function _renderUserIdentityModal(info, loading) {
            const modalEl = _ensureUserIdentityModal();
            if (!modalEl) return;

            const avatarWrap = modalEl.querySelector('#user-identity-avatar');
            const displayEl = modalEl.querySelector('#user-identity-display');
            const subtitleEl = modalEl.querySelector('#user-identity-subtitle');
            const fieldsEl = modalEl.querySelector('#user-identity-fields');
            const copyIdBtn = modalEl.querySelector('#user-identity-copy-user-id');
            const copyMentionBtn = modalEl.querySelector('#user-identity-copy-mention');

            if (displayEl) displayEl.textContent = info.display_name || info.username || info.user_id || 'User';
            const subtitleParts = [];
            if (info.username) subtitleParts.push(`@${info.username}`);
            if (info.account_type) subtitleParts.push(info.account_type);
            if (info.status) subtitleParts.push(info.status);
            subtitleParts.push(info.is_remote ? 'remote' : 'local');
            if (subtitleEl) {
                subtitleEl.textContent = loading
                    ? 'Loading profile details...'
                    : subtitleParts.join(' · ');
            }

            if (avatarWrap) {
                if (window.renderAvatarStack) {
                    window.renderAvatarStack(avatarWrap, {
                        userId: info.user_id,
                        userLabel: info.display_name || info.username || info.user_id,
                        userAvatarUrl: info.avatar_url || null,
                        peerId: info.origin_peer || null
                    });
                } else {
                    avatarWrap.innerHTML = '';
                    const fallback = document.createElement('div');
                    fallback.className = 'avatar-stack';
                    const userEl = document.createElement('div');
                    userEl.className = 'avatar-user';
                    if (info.avatar_url) {
                        const img = document.createElement('img');
                        img.src = info.avatar_url;
                        img.alt = info.display_name || info.user_id;
                        userEl.appendChild(img);
                    } else {
                        const initial = String(info.display_name || info.username || info.user_id || '?').slice(0, 1).toUpperCase();
                        userEl.textContent = initial || '?';
                    }
                    fallback.appendChild(userEl);
                    if (info.origin_peer) {
                        const peerEl = document.createElement('div');
                        peerEl.className = 'avatar-peer';
                        if (info.peer_avatar_url) {
                            const peerImg = document.createElement('img');
                            peerImg.src = info.peer_avatar_url;
                            peerImg.alt = info.peer_display_name || info.origin_peer;
                            peerEl.appendChild(peerImg);
                        } else {
                            const pInitial = String(info.peer_display_name || info.origin_peer || '?').slice(0, 1).toUpperCase();
                            peerEl.textContent = pInitial || '?';
                        }
                        fallback.appendChild(peerEl);
                    }
                    avatarWrap.appendChild(fallback);
                }
            }

            if (copyIdBtn) {
                copyIdBtn.setAttribute('data-copy-value', info.user_id || '');
                copyIdBtn.disabled = !info.user_id;
            }
            if (copyMentionBtn) {
                const mention = info.username ? `@${info.username}` : '';
                copyMentionBtn.setAttribute('data-copy-value', mention);
                copyMentionBtn.disabled = !mention;
            }

            if (fieldsEl) {
                fieldsEl.innerHTML = '';
                _appendIdentityField(fieldsEl, 'User ID', info.user_id || '', info.user_id || '', true);
                _appendIdentityField(fieldsEl, 'Display Name', info.display_name || '', info.display_name || '', false);
                _appendIdentityField(fieldsEl, 'Username', info.username || '', info.username || '', true);
                if (info.username) {
                    _appendIdentityField(fieldsEl, 'Mention', `@${info.username}`, `@${info.username}`, true);
                }
                _appendIdentityField(fieldsEl, 'Account Type', info.account_type || '', info.account_type || '', false);
                _appendIdentityField(fieldsEl, 'Status', info.status || '', info.status || '', false);
                if (info.origin_peer) {
                    _appendIdentityField(fieldsEl, 'Origin Peer ID', info.origin_peer, info.origin_peer, true);
                    _appendIdentityField(fieldsEl, 'Origin Peer Name', info.peer_display_name || info.origin_peer, info.peer_display_name || info.origin_peer, false);
                }
            }
        }

        function _fetchUserIdentityInfo(userId) {
            if (!userId) return Promise.resolve(null);
            if (_userIdentityCache[userId]) {
                return Promise.resolve(_userIdentityCache[userId]);
            }
            return apiCall(`/ajax/get_user_display_info?user_ids=${encodeURIComponent(userId)}`)
                .then(data => {
                    const users = (data && data.users && typeof data.users === 'object') ? data.users : {};
                    const info = users[userId] || null;
                    if (info) _userIdentityCache[userId] = info;
                    return info;
                });
        }

        function copyUserId(userId, displayName, triggerEl) {
            const uid = String(userId || '').trim();
            if (!uid) return;

            const modalEl = _ensureUserIdentityModal();
            if (!modalEl || typeof bootstrap === 'undefined' || !bootstrap.Modal) {
                _copyTextToClipboard(uid, 'user ID');
                return;
            }

            const initialInfo = _userIdentityInfoFromPayload(uid, displayName, triggerEl, null);
            _renderUserIdentityModal(initialInfo, true);

            const requestSeq = ++_userIdentityRequestSeq;
            modalEl.setAttribute('data-request-seq', String(requestSeq));

            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();

            _fetchUserIdentityInfo(uid)
                .then(payload => {
                    if (modalEl.getAttribute('data-request-seq') !== String(requestSeq)) return;
                    const resolved = _userIdentityInfoFromPayload(uid, displayName, triggerEl, payload || {});
                    _renderUserIdentityModal(resolved, false);
                })
                .catch(() => {
                    if (modalEl.getAttribute('data-request-seq') !== String(requestSeq)) return;
                    _renderUserIdentityModal(initialInfo, false);
                    showAlert('User details lookup is unavailable; showing local details only.', 'warning');
                });
        }

        function apiCall(url, options = {}) {
            console.log('apiCall:', url, options);
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
            const method = String(options.method || 'GET').toUpperCase();
            const isSafeMethod = ['GET', 'HEAD', 'OPTIONS'].includes(method);
            const headers = { ...(options.headers || {}) };
            const hasHeader = (name) => Object.keys(headers).some((k) => k.toLowerCase() === name.toLowerCase());
            const isFormData = typeof FormData !== 'undefined' && options.body instanceof FormData;

            if (!isFormData && !hasHeader('Content-Type')) {
                headers['Content-Type'] = 'application/json';
            }
            if (!isSafeMethod && !hasHeader('X-CSRFToken') && csrfToken) {
                headers['X-CSRFToken'] = csrfToken;
            }

            const fetchOptions = {
                ...options,
                method,
                headers,
            };

            return fetch(url, fetchOptions)
                .then(async (response) => {
                    console.log('apiCall response status:', response.status, 'for URL:', url);

                    const contentType = (response.headers.get('content-type') || '').toLowerCase();
                    let payload = {};
                    if (contentType.includes('application/json')) {
                        try {
                            payload = await response.json();
                        } catch (_) {
                            payload = {};
                        }
                    } else {
                        const textPayload = await response.text().catch(() => '');
                        payload = textPayload ? { error: textPayload, message: textPayload } : {};
                    }

                    if (!response.ok) {
                        const errObj = (payload && typeof payload === 'object') ? payload : {};
                        if (!errObj.error && !errObj.message) {
                            errObj.error = `Request failed (${response.status})`;
                        }
                        errObj.status = response.status;
                        console.error('apiCall error response:', errObj);
                        return Promise.reject(errObj);
                    }

                    console.log('apiCall success data:', payload, 'for URL:', url);
                    return (payload && typeof payload === 'object') ? payload : {};
                });
        }

        // --- Signal (structured data) helpers ---
        function promptSignalTTL(signalId) {
            if (!signalId) return;
            const hint = "Set signal TTL (e.g. 30d, 2w, 1q, 2026-03-15T12:00:00Z) or 'none' for no expiry:";
            const input = window.prompt(hint);
            if (input === null) return;
            const value = String(input || '').trim();
            if (!value) return;
            updateSignal(signalId, { ttl: value });
        }

        function toggleSignalLock(signalId, locked) {
            if (!signalId) return;
            updateSignal(signalId, { locked: !!locked });
        }

        function updateSignal(signalId, payload) {
            if (!signalId) return;
            apiCall(`/ajax/signals/${signalId}`, {
                method: 'POST',
                body: JSON.stringify(payload || {})
            })
                .then(data => {
                    if (!data || !data.success) {
                        showAlert((data && data.error) || 'Failed to update signal', 'danger');
                        return;
                    }
                    if (data.proposal) {
                        showAlert('Proposal submitted for signal update.', 'info');
                        return;
                    }
                    if (data.signal) {
                        applySignalUpdate(data.signal);
                    }
                    showAlert('Signal updated.', 'success');
                })
                .catch(err => {
                    showAlert((err && (err.error || err.message)) || 'Failed to update signal', 'danger');
                });
        }

        function applySignalUpdate(signal) {
            if (!signal || !signal.id) return;
            const card = document.querySelector(`.signal-card[data-signal-id="${signal.id}"]`);
            if (!card) return;
            const status = (signal.status || 'active');
            card.dataset.status = status;

            const statusEl = card.querySelector('.signal-status');
            if (statusEl) {
                statusEl.textContent = signal.status_label || status;
                statusEl.className = `signal-status ${status}`;
            }

            const expiryEl = card.querySelector('.signal-expiry');
            if (expiryEl) {
                if (signal.expires_label) {
                    expiryEl.innerHTML = `<i class="bi bi-hourglass-split me-1"></i>${signal.expires_label}`;
                } else {
                    expiryEl.innerHTML = '<i class="bi bi-infinity me-1"></i>No expiry';
                }
            }

            const lockBtn = card.querySelector('[data-signal-lock]');
            if (lockBtn) {
                const locked = status === 'locked';
                lockBtn.innerHTML = `<i class="bi bi-${locked ? 'unlock' : 'lock'} me-1"></i>${locked ? 'Unlock' : 'Lock'}`;
                lockBtn.setAttribute('onclick', `toggleSignalLock('${signal.id}', ${locked ? 'false' : 'true'})`);
            }
        }

        // --- Contract (deterministic coordination) helpers ---
        function updateContractStatus(contractId, status) {
            if (!contractId || !status) return;
            updateContract(contractId, { status });
        }

        function updateContract(contractId, payload) {
            if (!contractId) return;
            apiCall(`/ajax/contracts/${contractId}`, {
                method: 'POST',
                body: JSON.stringify(payload || {})
            })
                .then(data => {
                    if (!data || !data.success) {
                        showAlert((data && data.error) || 'Failed to update contract', 'danger');
                        return;
                    }
                    if (data.contract) {
                        applyContractUpdate(data.contract);
                    }
                    showAlert('Contract updated.', 'success');
                })
                .catch(err => {
                    showAlert((err && (err.error || err.message)) || 'Failed to update contract', 'danger');
                });
        }

        function _contractActionsMarkup(contract) {
            if (!contract || !contract.id || !contract.can_participate) return '';
            const status = String(contract.status || 'proposed');
            const buttons = [];
            if (status === 'proposed') {
                buttons.push(`<button type="button" class="btn btn-sm btn-outline-primary" onclick="updateContractStatus('${contract.id}', 'accepted')">Accept</button>`);
            } else if (status === 'accepted') {
                buttons.push(`<button type="button" class="btn btn-sm btn-outline-primary" onclick="updateContractStatus('${contract.id}', 'active')">Activate</button>`);
            } else if (status === 'active') {
                buttons.push(`<button type="button" class="btn btn-sm btn-outline-success" onclick="updateContractStatus('${contract.id}', 'fulfilled')">Fulfill</button>`);
                buttons.push(`<button type="button" class="btn btn-sm btn-outline-danger" onclick="updateContractStatus('${contract.id}', 'breached')">Breach</button>`);
            }
            if (contract.can_manage && status !== 'void' && status !== 'archived') {
                buttons.push(`<button type="button" class="btn btn-sm btn-outline-secondary" onclick="updateContractStatus('${contract.id}', 'void')">Void</button>`);
            }
            return buttons.join('');
        }

        function applyContractUpdate(contract) {
            if (!contract || !contract.id) return;
            const cards = document.querySelectorAll(`.contract-card[data-contract-id="${contract.id}"]`);
            if (!cards.length) return;
            const status = (contract.status || 'proposed');
            cards.forEach(card => {
                card.dataset.status = status;
                const statusEl = card.querySelector('.contract-status');
                if (statusEl) {
                    statusEl.textContent = contract.status_label || status.replace('_', ' ');
                    statusEl.className = `contract-status ${status}`;
                }
                const expiryEl = card.querySelector('.contract-expiry');
                if (expiryEl) {
                    if (contract.expires_label) {
                        expiryEl.innerHTML = `<i class="bi bi-hourglass-split me-1"></i>${contract.expires_label}`;
                    } else {
                        expiryEl.innerHTML = '<i class="bi bi-infinity me-1"></i>No expiry';
                    }
                }
                const actionsEl = card.querySelector('.contract-actions');
                if (actionsEl) {
                    actionsEl.innerHTML = _contractActionsMarkup(contract);
                }
            });
        }

        // --- Request (structured asks) helpers ---
        function updateRequestStatus(requestId, status) {
            if (!requestId || !status) return;
            updateRequest(requestId, { status });
        }

        function updateRequest(requestId, payload) {
            if (!requestId) return;
            apiCall(`/ajax/requests/${requestId}`, {
                method: 'POST',
                body: JSON.stringify(payload || {})
            })
                .then(data => {
                    if (!data || !data.success) {
                        showAlert((data && data.error) || 'Failed to update request', 'danger');
                        return;
                    }
                    if (data.request) {
                        applyRequestUpdate(data.request);
                    }
                    showAlert('Request updated.', 'success');
                })
                .catch(err => {
                    showAlert((err && (err.error || err.message)) || 'Failed to update request', 'danger');
                });
        }

        function applyRequestUpdate(request) {
            if (!request || !request.id) return;
            const cards = document.querySelectorAll(`.request-card[data-request-id="${request.id}"]`);
            if (!cards.length) return;
            const status = (request.status || 'open');
            const priority = (request.priority || 'normal');
            cards.forEach(card => {
                card.dataset.status = status;
                const statusEl = card.querySelector('.request-status');
                if (statusEl) {
                    statusEl.textContent = request.status_label || status.replace('_', ' ');
                    statusEl.className = `request-status ${status}`;
                }
                const priorityEl = card.querySelector('.request-priority');
                if (priorityEl) {
                    priorityEl.textContent = request.priority_label || priority.replace('_', ' ');
                    priorityEl.className = `request-priority ${priority}`;
                }
                const dueEl = card.querySelector('.request-due');
                if (dueEl) {
                    if (request.due_label) {
                        dueEl.innerHTML = `<i class="bi bi-calendar-event me-1"></i>${request.due_label}`;
                    } else {
                        dueEl.innerHTML = '';
                    }
                }
            });
        }

        // --- Circle (structured deliberation) helpers ---
        let activeCircleId = null;
        let circleEditingEntryId = null;
        let circleEditingType = null;
        let circleModalState = null;

        function _circleEscape(text) {
            if (text === null || text === undefined) return '';
            return String(text)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/\"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        function openCircleModal(circleId) {
            if (!circleId) return;
            activeCircleId = circleId;
            circleEditingEntryId = null;
            circleEditingType = null;
            const modalEl = document.getElementById('circleModal');
            if (!modalEl) return;
            const modal = bootstrap.Modal.getOrCreateInstance(modalEl);
            modal.show();
            loadCircleModal(circleId);
        }

        function loadCircleModal(circleId) {
            apiCall(`/ajax/circle/${circleId}`)
                .then(data => {
                    if (!data || !data.success) {
                        showAlert(data.error || 'Failed to load Circle', 'danger');
                        return;
                    }
                    renderCircleModal(data);
                })
                .catch(err => {
                    showAlert(err.error || 'Failed to load Circle', 'danger');
                });
        }

        function renderCircleModal(data) {
            circleModalState = data;
            const circle = data.circle || {};
            const entries = data.entries || [];
            const perms = data.permissions || {};
            const votes = data.votes || null;

            const topicEl = document.getElementById('circleModalTopic');
            const phaseEl = document.getElementById('circleModalPhase');
            const metaEl = document.getElementById('circleModalMeta');
            const entriesEl = document.getElementById('circleEntriesList');
            const entryText = document.getElementById('circleEntryText');
            const entryHint = document.getElementById('circleEntryHint');
            const submitBtn = document.getElementById('circleSubmitBtn');
            const cancelBtn = document.getElementById('circleCancelEditBtn');
            const phaseControls = document.getElementById('circlePhaseControls');
            const phaseSelect = document.getElementById('circlePhaseSelect');
            const voteBlock = document.getElementById('circleVoteBlock');
            const voteOptions = document.getElementById('circleVoteOptions');

            if (topicEl) topicEl.textContent = circle.topic || 'Circle';
            if (phaseEl) phaseEl.textContent = `Phase: ${(circle.phase || 'opinion').replace('_', ' ')}`;
            if (metaEl) {
                const parts = [];
                if (circle.facilitator_name || circle.facilitator_id) parts.push(`Facilitator: ${circle.facilitator_name || circle.facilitator_id}`);
                if (circle.mode) parts.push(`Mode: ${circle.mode}`);
                parts.push(`Opinions: ${circle.opinion_limit || 1}`);
                parts.push(`Clarify: ${circle.clarify_limit || 1}`);
                if (circle.decision_mode) parts.push(`Decision: ${circle.decision_mode}`);
                if (circle.ends_at) parts.push(`Ends: ${circle.ends_at}`);
                metaEl.textContent = parts.join(' • ');
            }

            // --- Circle description ---
            const descEl = document.getElementById('circleModalDescription');
            if (descEl) {
                if (circle.description) {
                    descEl.style.display = '';
                    descEl.innerHTML = `<div class="circle-modal-desc">${_circleEscape(circle.description)}</div>`;
                } else {
                    descEl.style.display = 'none';
                }
            }

            // --- Circle options (always show if present, not just for vote mode) ---
            const optionsEl = document.getElementById('circleModalOptions');
            if (optionsEl) {
                const options = circle.options || [];
                if (options.length) {
                    optionsEl.style.display = '';
                    optionsEl.innerHTML = `
                        <div class="circle-modal-options-label small text-muted mb-1">Options</div>
                        <div class="circle-modal-options-list">
                            ${options.map((opt, idx) => `
                                <div class="circle-modal-option">
                                    <span class="circle-option-badge">${String.fromCharCode(65 + idx)}</span>
                                    <span>${_circleEscape(opt)}</span>
                                </div>
                            `).join('')}
                        </div>
                    `;
                } else {
                    optionsEl.style.display = 'none';
                }
            }

            // --- Decision banner ---
            const decisionEl = document.getElementById('circleModalDecision');
            if (decisionEl) {
                if (circle.decision) {
                    decisionEl.style.display = '';
                    decisionEl.innerHTML = `
                        <div class="circle-modal-decision-banner">
                            <div class="circle-decision-label">Decision</div>
                            <div class="circle-decision-text">${_circleEscape(circle.decision).replace(/\n/g, '<br>')}</div>
                        </div>
                    `;
                } else if (circle.summary) {
                    decisionEl.style.display = '';
                    decisionEl.innerHTML = `
                        <div class="circle-modal-summary-banner">
                            <div class="circle-summary-label">Summary</div>
                            <div class="circle-summary-text">${_circleEscape(circle.summary).replace(/\n/g, '<br>')}</div>
                        </div>
                    `;
                } else {
                    decisionEl.style.display = 'none';
                }
            }

            // --- Entries separator (show only when there are entries) ---
            const separatorEl = document.getElementById('circleEntrySeparator');
            if (separatorEl) {
                separatorEl.style.display = entries.length ? '' : 'none';
            }

            // Phase controls (facilitator/admin)
            if (phaseControls && phaseSelect) {
                if (perms.can_moderate) {
                    phaseControls.style.display = '';
                    phaseSelect.value = circle.phase || 'opinion';
                } else {
                    phaseControls.style.display = 'none';
                }
            }

            // Entry hint + controls
            if (entryHint) {
                const allowed = perms.allowed_entry_type || '';
                if (perms.can_post && allowed) {
                    const remaining = allowed === 'opinion' ? perms.remaining_opinions : allowed === 'clarify' ? perms.remaining_clarify : null;
                    entryHint.textContent = remaining !== null
                        ? `You can submit ${allowed}. Remaining: ${remaining}.`
                        : `You can submit ${allowed}.`;
                } else {
                    entryHint.textContent = 'Posting is closed for this phase.';
                }
            }

            if (entryText && submitBtn) {
                entryText.disabled = !perms.can_post && !circleEditingEntryId;
                submitBtn.disabled = !perms.can_post && !circleEditingEntryId;
            }
            if (cancelBtn) {
                cancelBtn.style.display = circleEditingEntryId ? '' : 'none';
            }

            // Entries list
            if (entriesEl) {
                entriesEl.innerHTML = entries.map(entry => {
                    const created = entry.created_at ? new Date(entry.created_at).toLocaleString() : '';
                    const edited = entry.edited_at ? ` (edited ${new Date(entry.edited_at).toLocaleString()})` : '';
                    const canEdit = entry.can_edit ? `
                        <button type="button" class="btn btn-sm btn-outline-secondary" onclick="startCircleEdit('${entry.id}', '${_circleEscape(entry.content)}', '${entry.entry_type}')">
                            Edit
                        </button>` : '';
                    return `
                        <div class="circle-entry">
                            <div class="circle-entry-header">
                                <div class="circle-entry-meta">
                                    <span class="circle-entry-type">${_circleEscape(entry.entry_type)}</span>
                                    <span>${_circleEscape(entry.display_name || entry.user_id || '')}</span>
                                    <span>${_circleEscape(created)}${_circleEscape(edited)}</span>
                                </div>
                                ${canEdit}
                            </div>
                            <div class="circle-entry-content">${_circleEscape(entry.content || '')}</div>
                        </div>
                    `;
                }).join('');
            }

            // Vote block
            if (voteBlock && voteOptions) {
                if (circle.decision_mode === 'vote') {
                    voteBlock.style.display = '';
                    const options = circle.options || [];
                    voteOptions.innerHTML = options.length ? options.map((opt, idx) => {
                        const count = votes && votes.counts ? (votes.counts[idx] || 0) : 0;
                        const selected = (data.user_vote !== null && data.user_vote === idx);
                        return `
                            <button type="button" class="btn btn-sm ${selected ? 'btn-primary' : 'btn-outline-primary'}"
                                    onclick="voteCircle(${idx})">
                                ${_circleEscape(opt)} <span class="ms-2 text-muted">${count}</span>
                            </button>
                        `;
                    }).join('') : '<div class="text-muted small">No vote options defined.</div>';
                } else {
                    voteBlock.style.display = 'none';
                }
            }
        }

        function startCircleEdit(entryId, content, entryType) {
            circleEditingEntryId = entryId;
            circleEditingType = entryType;
            const entryText = document.getElementById('circleEntryText');
            const submitBtn = document.getElementById('circleSubmitBtn');
            const cancelBtn = document.getElementById('circleCancelEditBtn');
            if (entryText) {
                entryText.value = content || '';
                entryText.focus();
            }
            if (submitBtn) submitBtn.textContent = 'Update';
            if (cancelBtn) cancelBtn.style.display = '';
        }

        function cancelCircleEdit() {
            circleEditingEntryId = null;
            circleEditingType = null;
            const entryText = document.getElementById('circleEntryText');
            const submitBtn = document.getElementById('circleSubmitBtn');
            const cancelBtn = document.getElementById('circleCancelEditBtn');
            if (entryText) entryText.value = '';
            if (submitBtn) submitBtn.textContent = 'Submit';
            if (cancelBtn) cancelBtn.style.display = 'none';
        }

        function submitCircleEntry() {
            if (!activeCircleId) return;
            const entryText = document.getElementById('circleEntryText');
            const content = entryText ? entryText.value.trim() : '';
            if (!content) {
                showAlert('Entry content required', 'warning');
                return;
            }
            const entryType = circleEditingType || (circleModalState && circleModalState.permissions ? circleModalState.permissions.allowed_entry_type : 'opinion');
            const url = circleEditingEntryId
                ? `/ajax/circle/${activeCircleId}/entries/${circleEditingEntryId}`
                : `/ajax/circle/${activeCircleId}/entries`;
            const payload = { content, entry_type: entryType };

            apiCall(url, { method: 'POST', body: JSON.stringify(payload) })
                .then(() => {
                    cancelCircleEdit();
                    loadCircleModal(activeCircleId);
                })
                .catch(err => {
                    showAlert(err.error || 'Failed to submit entry', 'danger');
                });
        }

        function submitCirclePhase() {
            if (!activeCircleId) return;
            const phaseSelect = document.getElementById('circlePhaseSelect');
            const phase = phaseSelect ? phaseSelect.value : '';
            if (!phase) return;
            apiCall(`/ajax/circle/${activeCircleId}/phase`, {
                method: 'POST',
                body: JSON.stringify({ phase })
            }).then(() => {
                loadCircleModal(activeCircleId);
            }).catch(err => {
                showAlert(err.error || 'Failed to update phase', 'danger');
            });
        }

        function voteCircle(optionIndex) {
            if (!activeCircleId) return;
            apiCall(`/ajax/circle/${activeCircleId}/vote`, {
                method: 'POST',
                body: JSON.stringify({ option_index: optionIndex })
            }).then(() => {
                loadCircleModal(activeCircleId);
            }).catch(err => {
                showAlert(err.error || 'Failed to vote', 'danger');
            });
        }

        // --- Poll helpers (feed + channels) ---
        function applyPollUpdate(card, pollData) {
            if (!card || !pollData) return;
            const totalEl = card.querySelector('.poll-total');
            const statusEl = card.querySelector('.poll-status');
            if (totalEl) {
                totalEl.textContent = `${pollData.total_votes || 0} votes`;
            }
            if (statusEl && pollData.status_label) {
                statusEl.textContent = pollData.status_label;
            }
            const options = card.querySelectorAll('.poll-option');
            options.forEach(btn => {
                const idx = parseInt(btn.getAttribute('data-option-index') || '0', 10);
                const opt = (pollData.options || []).find(o => o.index === idx);
                const countEl = btn.querySelector('.poll-option-count');
                const barEl = btn.querySelector('.poll-option-bar');
                if (opt) {
                    if (countEl) countEl.textContent = opt.count;
                    if (barEl) barEl.style.width = `${opt.percent}%`;
                }
                btn.classList.toggle('selected', pollData.user_vote === idx);
                if (pollData.is_closed) {
                    btn.classList.add('closed');
                    btn.setAttribute('disabled', 'disabled');
                } else {
                    btn.classList.remove('closed');
                    btn.removeAttribute('disabled');
                }
            });
        }

        function votePoll(pollId, itemType, optionIndex) {
            if (!pollId || !itemType) return;
            apiCall('/ajax/vote_poll', {
                method: 'POST',
                body: JSON.stringify({
                    poll_id: pollId,
                    item_type: itemType,
                    option_index: optionIndex
                })
            })
            .then(data => {
                if (!data || !data.success || !data.poll) return;
                const card = document.querySelector(`.poll-card[data-poll-id="${pollId}"][data-poll-kind="${itemType}"]`);
                applyPollUpdate(card, data.poll);
            })
            .catch(err => {
                if (err && err.error && typeof showAlert === 'function') {
                    showAlert(err.error, 'warning');
                }
            });
        }

        // Update navbar avatar and username
        function updateNavbarProfile() {
            const currentUserId = (window.CANOPY_VARS && window.CANOPY_VARS.userId) || 'local_user';
            
            fetch(`/ajax/get_user_display_info?user_ids=${currentUserId}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success && data.users[currentUserId]) {
                        const user = data.users[currentUserId];
                        
                        // Update avatar
                        const avatarElement = document.getElementById('navbar-avatar');
                        if (avatarElement && user.avatar_url) {
                            avatarElement.innerHTML = `<img src="${user.avatar_url}" alt="${user.display_name}" class="rounded-circle" style="width: 100%; height: 100%; object-fit: cover;">`;
                        }
                        
                        // Update username
                        const usernameElement = document.getElementById('navbar-username');
                        if (usernameElement && user.display_name) {
                            usernameElement.textContent = user.display_name;
                        }
                    }
                })
                .catch(error => {
                    console.error('Error updating navbar profile:', error);
                });
        }

        // Update navbar on page load
        document.addEventListener('DOMContentLoaded', function() {
            updateNavbarProfile();
        });
        
        // Theme Management
        function applyTheme(theme) {
            document.documentElement.setAttribute('data-theme', theme);
            localStorage.setItem('canopy-theme', theme);
            
            // Enhanced logging
            console.log('Applied theme:', theme);
            console.log('Document data-theme attribute:', document.documentElement.getAttribute('data-theme'));
            console.log('All CSS classes on document:', document.documentElement.className);
            
            // Force a style recalculation
            document.documentElement.offsetHeight;
            
            // Log some test elements to see if styling is applied
            setTimeout(() => {
                const testCard = document.querySelector('.card');
                const testBtn = document.querySelector('.btn');
                if (testCard) {
                    const cardStyles = window.getComputedStyle(testCard);
                    console.log('🃏 Card background:', cardStyles.backgroundColor);
                    console.log('🃏 Card color:', cardStyles.color);
                }
                if (testBtn) {
                    const btnStyles = window.getComputedStyle(testBtn);
                    console.log('Button background:', btnStyles.backgroundColor);
                    console.log('Button color:', btnStyles.color);
                }
            }, 100);
        }

        function loadSavedTheme() {
            // Try to get theme from profile first (server-side), then localStorage
            const profileTheme = (window.CANOPY_VARS && window.CANOPY_VARS.profileTheme) || 'dark';
            const savedTheme = localStorage.getItem('canopy-theme') || profileTheme || 'dark';
            
            // Handle auto theme - detect system preference
            if (savedTheme === 'auto') {
                const systemPrefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
                applyTheme(systemPrefersDark ? 'dark' : 'light');
                
                // Watch for system theme changes
                window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e => {
                    if (localStorage.getItem('canopy-theme') === 'auto') {
                        applyTheme(e.matches ? 'dark' : 'light');
                    }
                });
            } else {
                applyTheme(savedTheme);
            }
        }

        // Load theme on page load
        document.addEventListener('DOMContentLoaded', function() {
            loadSavedTheme();
        });

        // Apply theme immediately (before DOM content loaded)
        (function() {
            const savedTheme = localStorage.getItem('canopy-theme') || 'dark';
            console.log('Early theme application:', savedTheme);
            if (savedTheme !== 'auto') {
                document.documentElement.setAttribute('data-theme', savedTheme);
                console.log('Set data-theme attribute to:', savedTheme);
            }
        })();
        
        // Debug function to find elements with light backgrounds
        function debugLightElements() {
            const lightElements = [];
            const allElements = document.querySelectorAll('*');
            
            allElements.forEach(el => {
                const styles = window.getComputedStyle(el);
                const bgColor = styles.backgroundColor;
                const color = styles.color;
                
                // Check for light backgrounds (white, light gray, etc.)
                if (bgColor && (
                    bgColor.includes('rgb(255, 255, 255)') || 
                    bgColor.includes('rgba(255, 255, 255') ||
                    bgColor.includes('#fff') ||
                    bgColor.includes('#ffffff') ||
                    bgColor.includes('white') ||
                    (bgColor.includes('rgb(') && 
                     bgColor.split(',').every(part => {
                        const num = parseInt(part.replace(/[^\d]/g, ''));
                        return num > 200; // Light colors
                     }))
                )) {
                    lightElements.push({
                        element: el,
                        tagName: el.tagName,
                        className: el.className,
                        backgroundColor: bgColor,
                        color: color,
                        id: el.id || 'no-id'
                    });
                }
            });
            
            console.log('Found', lightElements.length, 'elements with light backgrounds:');
            lightElements.forEach((item, index) => {
                console.log(`${index + 1}. ${item.tagName}.${item.className} (${item.id})`, 
                    `bg: ${item.backgroundColor}`, item.element);
            });
            
            return lightElements;
        }
        
        // Add debug function to window for manual testing
        window.debugLightElements = debugLightElements;
        
        // Quick theme switching for testing
        window.testTheme = function(theme) {
            console.log('Testing theme:', theme);
            applyTheme(theme);
            setTimeout(() => {
                console.log('Checking for light elements after theme change...');
                debugLightElements();
            }, 500);
        };
        
        // Quick test all themes
        window.testAllThemes = function() {
            const themes = ['dark', 'light', 'liquid-glass', 'auto'];
            let index = 0;
            
            function nextTheme() {
                if (index < themes.length) {
                    console.log(`Testing theme ${index + 1}/${themes.length}: ${themes[index]}`);
                    testTheme(themes[index]);
                    index++;
                    setTimeout(nextTheme, 3000); // Wait 3 seconds between themes
                } else {
                    console.log('All themes tested.');
                }
            }
            
            nextTheme();
        };
        
        // Test responsive image sizing
        window.testResponsiveImages = function() {
            const images = document.querySelectorAll('.message-image, .post-image, .channel-image, .comment-image img');
            console.log(`Found ${images.length} responsive images`);
            
            images.forEach((img, index) => {
                const computedStyle = window.getComputedStyle(img);
                const maxWidth = computedStyle.maxWidth;
                const maxHeight = computedStyle.maxHeight;
                const actualWidth = img.offsetWidth;
                const actualHeight = img.offsetHeight;
                
                console.log(`${index + 1}. ${img.className}:`, {
                    maxWidth,
                    maxHeight,
                    actualSize: `${actualWidth}x${actualHeight}px`,
                    element: img
                });
            });
            
            // Test different screen sizes simulation
            const viewportWidth = window.innerWidth;
            console.log(`Current viewport: ${viewportWidth}px`);
            if (viewportWidth <= 375) {
                console.log('Small phone mode active');
            } else if (viewportWidth <= 576) {
                console.log('Mobile phone mode active');
            } else if (viewportWidth <= 768) {
                console.log('Tablet mode active');
            } else {
                console.log('Desktop mode active');
            }
        };
        
        // Auto-run debug after theme is loaded
        document.addEventListener('DOMContentLoaded', function() {
            setTimeout(() => {
                console.log('Running automatic light element detection...');
                debugLightElements();
            }, 1000);
        });

        // Auto-refresh timestamps
        setInterval(() => {
            document.querySelectorAll('[data-timestamp]').forEach(el => {
                const timestamp = el.getAttribute('data-timestamp');
                el.textContent = formatTimestamp(timestamp);
                if (typeof window.parseCanopyTimestamp === 'function') {
                    const parsed = window.parseCanopyTimestamp(timestamp);
                    if (parsed) {
                        el.title = parsed.toLocaleString();
                    }
                }
            });
        }, 30000);

        // --- Peer Activity Notifications (Phase 1) ---
        function initPeerActivityNotifications() {
	            const bellBtn = document.getElementById('notificationBell');
	            const badgeEl = document.getElementById('notificationBadge');
	            const listEl = document.getElementById('notificationList');
	            const emptyWrap = document.getElementById('notificationEmptyWrap');
	            const clearBtn = document.getElementById('notificationClear');

            if (!bellBtn || !badgeEl || !listEl) {
                return;
            }

	            let notificationCount = 0;
	            let events = [];
	            const seenEventIds = new Set();
            const mentionRefs = new Set();
	            let initialized = false;
	            const localUserId = (window.CANOPY_VARS && window.CANOPY_VARS.localUserId) || null;
	            const routes = {
	                feed: (window.CANOPY_VARS && window.CANOPY_VARS.urls && window.CANOPY_VARS.urls.feed) || '/feed',
	                channels: (window.CANOPY_VARS && window.CANOPY_VARS.urls && window.CANOPY_VARS.urls.channels) || '/channels',
	                messages: (window.CANOPY_VARS && window.CANOPY_VARS.urls && window.CANOPY_VARS.urls.messages) || '/messages',
	            };

            function peerDisplayName(peerId) {
                if (window.canopyPeerDisplayName) {
                    return window.canopyPeerDisplayName(peerId);
                }
                const nameEl = document.querySelector(`.sidebar-peer[data-peer-id="${peerId}"] .sidebar-peer-name`);
                if (nameEl && nameEl.textContent) {
                    return nameEl.textContent.trim();
                }
                return (peerId || '').slice(0, 12);
            }

            function peerAvatarSrc(peerId) {
                if (window.canopyPeerAvatarSrc) {
                    return window.canopyPeerAvatarSrc(peerId);
                }
                const imgEl = document.querySelector(`.sidebar-peer[data-peer-id="${peerId}"] .sidebar-peer-avatar img`);
                const src = imgEl ? imgEl.getAttribute('src') : null;
                return src || null;
            }

            const userDisplayCache = {};
            let pendingUserIds = new Set();
            let userFetchTimer = null;

            function scheduleUserInfoFetch(userIds) {
                if (!userIds || !userIds.length) return;
                userIds.forEach(uid => {
                    if (!uid || userDisplayCache[uid]) return;
                    pendingUserIds.add(uid);
                });
                if (!pendingUserIds.size) return;
                if (userFetchTimer) return;
                userFetchTimer = setTimeout(() => {
                    const ids = Array.from(pendingUserIds);
                    pendingUserIds = new Set();
                    userFetchTimer = null;
                    fetch(`/ajax/get_user_display_info?user_ids=${ids.join(',')}`)
                        .then(r => r.json())
                        .then(data => {
                            if (data && data.success && data.users) {
                                Object.keys(data.users).forEach(uid => {
                                    userDisplayCache[uid] = data.users[uid];
                                });
                                renderMenu();
                            }
                        })
                        .catch(() => {});
                }, 150);
            }

            function getUserRefId(evt) {
                if (!evt) return null;
                const ref = evt.ref || {};
                return ref.user_id || ref.author_id || ref.sender_id || null;
            }

            function formatKind(kind) {
                if (!kind) return '';
                if (kind === 'feed_post') return 'feed post';
                if (kind === 'channel_message') return 'channel message';
                if (kind === 'direct_message') return 'direct message';
                if (kind === 'interaction') return 'interaction';
                if (kind === 'mention') return 'mention';
                return String(kind).replace(/_/g, ' ');
            }

            function cleanPreview(text) {
                const s = String(text || '').replace(/\s+/g, ' ').trim();
                // Light markdown cleanup for readability in a 1-2 line preview.
                return s
                    .replace(/\*\*(.*?)\*\*/g, '$1')
                    .replace(/__(.*?)__/g, '$1')
                    .replace(/`([^`]*)`/g, '$1')
                    .replace(/~~(.*?)~~/g, '$1');
            }

            function setBadge(count) {
                if (count > 0) {
                    badgeEl.style.display = 'inline-flex';
                    badgeEl.textContent = count > 99 ? '99+' : String(count);
                } else {
                    badgeEl.style.display = 'none';
                }
            }

            function markPeerActive(peerId) {
                const peerEl = document.querySelector(`.sidebar-peer[data-peer-id="${peerId}"]`);
                if (!peerEl) return;
                peerEl.classList.add('activity');
	                setTimeout(() => peerEl.classList.remove('activity'), 4500);
	            }

            function otherUserIdFromDirectMessage(ref) {
	                if (!ref) return null;
	                const sender = ref.sender_id;
	                const recipient = ref.recipient_id;
	                if (localUserId && sender && recipient) {
	                    return sender === localUserId ? recipient : sender;
	                }
	                return sender || recipient || null;
	            }

            function eventRefKey(evt) {
                if (!evt) return null;
                const ref = evt.ref || {};
                if (ref.message_id) return `msg:${ref.message_id}`;
                if (ref.post_id) return `post:${ref.post_id}`;
                return null;
            }

	            function navigateToActivity(evt) {
	                if (!evt) return;
	                const kind = evt.kind || '';
	                const ref = evt.ref || {};

	                try {
	                    if (kind === 'mention') {
	                        if (ref.channel_id) {
	                            const url = new URL(routes.channels, window.location.origin);
	                            url.searchParams.set('focus_channel', ref.channel_id);
	                            if (ref.message_id) url.searchParams.set('focus_message', ref.message_id);
	                            window.location.href = url.toString();
	                            return;
	                        }
	                        if (ref.post_id) {
	                            const url = new URL(routes.feed, window.location.origin);
	                            url.searchParams.set('focus_post', ref.post_id);
	                            window.location.href = url.toString();
	                            return;
	                        }
	                    }
	                    if (kind === 'feed_post' && ref.post_id) {
	                        const url = new URL(routes.feed, window.location.origin);
	                        url.searchParams.set('focus_post', ref.post_id);
	                        window.location.href = url.toString();
	                        return;
	                    }
	                    if (kind === 'channel_message' && ref.channel_id) {
	                        const url = new URL(routes.channels, window.location.origin);
	                        url.searchParams.set('focus_channel', ref.channel_id);
	                        if (ref.message_id) url.searchParams.set('focus_message', ref.message_id);
	                        window.location.href = url.toString();
	                        return;
	                    }
	                    if (kind === 'direct_message') {
	                        const otherUserId = otherUserIdFromDirectMessage(ref);
	                        const url = new URL(routes.messages, window.location.origin);
	                        if (otherUserId) url.searchParams.set('with', otherUserId);
	                        window.location.href = url.toString();
	                        return;
	                    }
	                    if (kind === 'interaction') {
	                        // Best-effort: route to the affected post when possible.
	                        if (ref.item_type === 'post' && ref.item_id) {
	                            const url = new URL(routes.feed, window.location.origin);
	                            url.searchParams.set('focus_post', ref.item_id);
	                            window.location.href = url.toString();
	                            return;
	                        }
	                        if (ref.item_type === 'poll') {
	                            const pollId = ref.poll_id || ref.item_id;
	                            const pollKind = ref.poll_kind || 'feed';
	                            if (pollKind === 'channel' && ref.channel_id) {
	                                const url = new URL(routes.channels, window.location.origin);
	                                url.searchParams.set('focus_channel', ref.channel_id);
	                                if (pollId) url.searchParams.set('focus_message', pollId);
	                                window.location.href = url.toString();
	                                return;
	                            }
	                            if (pollId) {
	                                const url = new URL(routes.feed, window.location.origin);
	                                url.searchParams.set('focus_post', pollId);
	                                window.location.href = url.toString();
	                                return;
	                            }
	                        }
	                    }
	                } catch (_) {
	                    // Fallback: no-op; keep menu open.
	                }
	            }

	            function renderMenu() {
	                listEl.innerHTML = '';

                if (!events.length) {
                    if (emptyWrap) emptyWrap.style.display = 'block';
                    return;
                }

                if (emptyWrap) emptyWrap.style.display = 'none';

                events.forEach(evt => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'dropdown-item notification-item';

                    const peerName = peerDisplayName(evt.peer_id);
                    const userId = getUserRefId(evt);
                    const userInfo = userId ? userDisplayCache[userId] : null;
                    const userLabel = userInfo ? (userInfo.display_name || userInfo.username || userId) : null;
                    const userHandle = userInfo ? (userInfo.username || userId) : (userId || null);
                    const timeStr = new Date(evt.timestamp * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
                    const shortId = `${(evt.peer_id || '').slice(0, 12)}...`;
                    const kind = formatKind(evt.kind || '');
                    const preview = cleanPreview(evt.preview || '');

                    const row = document.createElement('div');
                    row.className = 'activity-row';

                    let avatar;
                    if (userId) {
                        avatar = document.createElement('div');
                        avatar.className = 'activity-avatar-stack avatar-stack';

                        if (window.renderAvatarStack) {
                            window.renderAvatarStack(avatar, {
                                userId: userId,
                                userLabel: userLabel || userId,
                                userAvatarUrl: userInfo ? userInfo.avatar_url : null,
                                peerId: evt.peer_id
                            });
                        } else {
                            const userAvatar = document.createElement('div');
                            userAvatar.className = 'avatar-user';
                            if (userInfo && userInfo.avatar_url) {
                                const img = document.createElement('img');
                                img.src = userInfo.avatar_url;
                                img.alt = userLabel || userId;
                                userAvatar.appendChild(img);
                            } else {
                                const letter = (userLabel || userId || '?').slice(0, 1).toUpperCase();
                                userAvatar.textContent = letter;
                            }
                            const peerAvatar = document.createElement('div');
                            peerAvatar.className = 'avatar-peer';
                            const peerSrc = peerAvatarSrc(evt.peer_id);
                            if (peerSrc) {
                                const img = document.createElement('img');
                                img.src = peerSrc;
                                img.alt = peerName;
                                peerAvatar.appendChild(img);
                            } else {
                                const letter = (peerName || '?').slice(0, 1).toUpperCase();
                                peerAvatar.textContent = letter;
                            }
                            avatar.appendChild(userAvatar);
                            avatar.appendChild(peerAvatar);
                        }
                    } else {
                        avatar = document.createElement('div');
                        avatar.className = 'activity-avatar';
                        const avatarSrc = peerAvatarSrc(evt.peer_id);
                        if (avatarSrc) {
                            const img = document.createElement('img');
                            img.src = avatarSrc;
                            img.alt = peerName;
                            avatar.appendChild(img);
                        } else {
                            const letter = (peerName || '?').slice(0, 1).toUpperCase();
                            avatar.textContent = letter;
                        }
                    }

                    const body = document.createElement('div');
                    body.className = 'activity-body';

                    const top = document.createElement('div');
                    top.className = 'activity-top';

                    const nameEl = document.createElement('span');
                    nameEl.className = 'activity-name';
                    if (userLabel) {
                        nameEl.textContent = userLabel;
                        if (userHandle) {
                            const handleEl = document.createElement('span');
                            handleEl.className = 'activity-handle';
                            handleEl.textContent = '@' + userHandle;
                            nameEl.appendChild(handleEl);
                        }
                    } else {
                        nameEl.textContent = peerName;
                    }

                    const timeEl = document.createElement('span');
                    timeEl.className = 'activity-time';
                    timeEl.textContent = timeStr;

                    top.appendChild(nameEl);
                    top.appendChild(timeEl);

                    const sub = document.createElement('div');
                    sub.className = 'activity-sub';
                    if (userLabel) {
                        sub.textContent = `via ${peerName} \u2022 ${kind} \u2022 ${preview || shortId}`;
                    } else {
                        sub.textContent = `${kind} \u2022 ${preview || shortId}`;
                    }

                    body.appendChild(top);
                    body.appendChild(sub);

                    row.appendChild(avatar);
                    row.appendChild(body);
                    btn.appendChild(row);

	                    btn.addEventListener('click', () => {
	                        markPeerActive(evt.peer_id);
	                        navigateToActivity(evt);
	                    });
	                    listEl.appendChild(btn);
                });
	            }

            function recordEvent(evt) {
                const refKey = eventRefKey(evt);
                if (evt.kind === 'mention' && refKey) {
                    mentionRefs.add(refKey);
                    events = events.filter(e => eventRefKey(e) !== refKey || e.kind === 'mention');
                } else if (refKey && mentionRefs.has(refKey)) {
                    return;
                }
                const uid = getUserRefId(evt);
                if (uid) scheduleUserInfoFetch([uid]);
                notificationCount += 1;
                events.unshift(evt);
                events = events.slice(0, 12);
                setBadge(notificationCount);
                renderMenu();
                markPeerActive(evt.peer_id);
            }

            function poll() {
                fetch('/ajax/peer_activity')
                    .then(r => r.json())
                    .then(data => {
                        if (!data || data.success === false) return;
                        const incoming = data.events || [];
                        if (!initialized) {
                            incoming.forEach(evt => {
                                const eventId = evt.id || `${evt.peer_id}:${evt.kind}:${evt.timestamp}`;
                                seenEventIds.add(eventId);
                            });
                            initialized = true;
                            return;
                        }

                        incoming.forEach(evt => {
                            const eventId = evt.id || `${evt.peer_id}:${evt.kind}:${evt.timestamp}`;
                            if (seenEventIds.has(eventId)) return;
                            seenEventIds.add(eventId);

                            // Connection events belong on the Connect page timeline,
                            // not in the global notification bell.
                            if (evt.kind === 'connection') return;

                            // Skip notifications about our own activity
                            const ref = evt.ref || {};
                            const originUser = ref.user_id || ref.sender_id || ref.author_id || '';
                            if (localUserId && originUser === localUserId) return;

                            recordEvent(evt);
                        });
                        const newUserIds = incoming.map(evt => getUserRefId(evt)).filter(Boolean);
                        scheduleUserInfoFetch(newUserIds);
                    })
                    .catch(() => {});
            }

            bellBtn.addEventListener('click', () => {
                // Opening the bell acknowledges current count, but keeps the history in the menu.
                notificationCount = 0;
                setBadge(0);
            });

            if (clearBtn) {
                clearBtn.addEventListener('click', () => {
                    events = [];
                    mentionRefs.clear();
                    notificationCount = 0;
                    setBadge(0);
                    renderMenu();
                });
            }

            setBadge(0);
            renderMenu();
            poll();
            setInterval(poll, 2500);
        }

        // --- Sidebar media mini player (audio/video/youtube off-screen helper) ---
        function initSidebarMediaMiniPlayer() {
            const mini = document.getElementById('sidebar-media-mini');
            if (!mini) return;

            const icon = document.getElementById('sidebar-media-mini-icon');
            const titleEl = document.getElementById('sidebar-media-mini-title');
            const subtitleEl = document.getElementById('sidebar-media-mini-subtitle');
            const progressWrap = document.getElementById('sidebar-media-mini-progress');
            const progressBar = document.getElementById('sidebar-media-mini-progress-bar');
            const playBtn = document.getElementById('sidebar-media-mini-play');
            const jumpBtn = document.getElementById('sidebar-media-mini-jump');
            const pipBtn = document.getElementById('sidebar-media-mini-pip');
            const closeBtn = document.getElementById('sidebar-media-mini-close');
            const timeEl = document.getElementById('sidebar-media-mini-time');
            const mainScroller = document.querySelector('.main-content');

            const state = {
                current: null,
                dismissedEl: null,
                observer: null,
                mutationObserver: null,
                tickHandle: null,
                ytApiPromise: null
            };

            function mediaTypeFor(el) {
                if (!el || !el.tagName) return '';
                const tag = el.tagName.toLowerCase();
                if (tag === 'audio') return 'audio';
                if (tag === 'video') return 'video';
                if (tag === 'iframe' && el.closest('.youtube-embed')) return 'youtube';
                return '';
            }

            function mediaIcon(type) {
                if (type === 'audio') return 'bi-music-note-beamed';
                if (type === 'video') return 'bi-camera-video';
                if (type === 'youtube') return 'bi-youtube';
                return 'bi-play-circle';
            }

            function isYouTubePlayingState(ytState) {
                if (!Number.isFinite(ytState)) return false;
                // PLAYING (1) and BUFFERING (3) should keep the mini player active.
                return ytState === 1 || ytState === 3;
            }

            function ensureYouTubeIframeApi() {
                if (window.YT && typeof window.YT.Player === 'function') {
                    return Promise.resolve(window.YT);
                }
                if (state.ytApiPromise) return state.ytApiPromise;

                state.ytApiPromise = new Promise((resolve) => {
                    const previousReady = window.onYouTubeIframeAPIReady;
                    window.onYouTubeIframeAPIReady = function onYouTubeIframeAPIReady() {
                        if (typeof previousReady === 'function') {
                            try { previousReady(); } catch (_) {}
                        }
                        resolve(window.YT);
                    };

                    const existing = document.querySelector('script[src*="youtube.com/iframe_api"]');
                    if (existing) return;

                    const script = document.createElement('script');
                    script.src = 'https://www.youtube.com/iframe_api';
                    script.async = true;
                    script.onerror = () => resolve(null);
                    document.head.appendChild(script);
                });

                return state.ytApiPromise;
            }

            function ensureYouTubeEmbedParams(el) {
                if (!el || el.__canopyMiniYTNormalized) return;
                try {
                    const src = el.getAttribute('src') || '';
                    if (!src) return;
                    const url = new URL(src, window.location.origin);
                    let changed = false;

                    if (url.searchParams.get('enablejsapi') !== '1') {
                        url.searchParams.set('enablejsapi', '1');
                        changed = true;
                    }
                    if (!url.searchParams.get('origin')) {
                        url.searchParams.set('origin', window.location.origin);
                        changed = true;
                    }
                    if (!url.searchParams.get('playsinline')) {
                        url.searchParams.set('playsinline', '1');
                        changed = true;
                    }
                    if (!url.searchParams.get('rel')) {
                        url.searchParams.set('rel', '0');
                        changed = true;
                    }

                    if (changed) {
                        el.src = url.toString();
                    }
                    el.__canopyMiniYTNormalized = true;
                } catch (_) {}
            }

            function initYouTubePlayer(el) {
                if (!el || el.__canopyMiniYTReadyInit || el.__canopyMiniYTFailed) return;
                el.__canopyMiniYTReadyInit = true;
                ensureYouTubeEmbedParams(el);

                ensureYouTubeIframeApi().then((YT) => {
                    if (!YT || !YT.Player || !el.isConnected) return;
                    if (el.__canopyMiniYTPlayer) return;
                    try {
                        const player = new YT.Player(el, {
                            events: {
                                onReady: () => {
                                    el.__canopyMiniYTReady = true;
                                },
                                onStateChange: (event) => {
                                    el.__canopyMiniYTState = event && Number.isFinite(event.data) ? event.data : -1;
                                    if (isYouTubePlayingState(el.__canopyMiniYTState)) {
                                        state.dismissedEl = null;
                                        setCurrent(el, 'youtube');
                                        return;
                                    }
                                    setTimeout(updateMini, 50);
                                }
                            }
                        });
                        el.__canopyMiniYTPlayer = player;
                    } catch (_) {
                        el.__canopyMiniYTFailed = true;
                    }
                }).catch(() => {
                    el.__canopyMiniYTFailed = true;
                });
            }

            function formatTime(seconds) {
                if (!Number.isFinite(seconds) || seconds < 0) return '--:--';
                const s = Math.floor(seconds);
                const h = Math.floor(s / 3600);
                const m = Math.floor((s % 3600) / 60);
                const sec = s % 60;
                if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
                return `${m}:${String(sec).padStart(2, '0')}`;
            }

            function sourceContainer(el) {
                return el.closest('.post-card[data-post-id], .message-item[data-message-id], .card');
            }

            function sourceSubtitle(el) {
                const post = el.closest('.post-card[data-post-id]');
                if (post) {
                    const author = post.querySelector('[data-user-display]');
                    const authorText = author ? author.textContent.trim() : '';
                    return authorText ? `Feed - ${authorText}` : 'Feed post';
                }
                const message = el.closest('.message-item[data-message-id]');
                if (message) {
                    const author = message.querySelector('[data-user-display]');
                    const authorText = author ? author.textContent.trim() : '';
                    const onChannels = window.location.pathname.indexOf('/channels') === 0;
                    if (onChannels) return authorText ? `Channel - ${authorText}` : 'Channel message';
                    return authorText ? `Messages - ${authorText}` : 'Direct message';
                }
                return 'Media off-screen';
            }

            function titleFromMedia(el, type) {
                if (!el) return 'Now Playing';
                if (type === 'youtube') {
                    const player = el.__canopyMiniYTPlayer;
                    if (player && typeof player.getVideoData === 'function') {
                        try {
                            const data = player.getVideoData() || {};
                            if (data.title && data.title.trim()) {
                                return data.title.trim();
                            }
                        } catch (_) {}
                    }
                    const vid = el.getAttribute('data-video-id') ||
                        (el.closest('.youtube-embed') && el.closest('.youtube-embed').getAttribute('data-video-id')) || '';
                    return vid ? `YouTube ${vid}` : 'YouTube video';
                }

                const attachment = el.closest('.attachment-item');
                if (attachment) {
                    const filename = attachment.querySelector('[data-file-name], .fw-semibold, strong');
                    if (filename && filename.textContent.trim()) return filename.textContent.trim();
                }

                const source = el.currentSrc || el.src || '';
                if (source) {
                    try {
                        const url = new URL(source, window.location.origin);
                        const last = (url.pathname.split('/').filter(Boolean).pop() || '').trim();
                        if (last) return decodeURIComponent(last);
                    } catch (_) {}
                }

                if (type === 'audio') return 'Audio clip';
                if (type === 'video') return 'Video clip';
                return 'Media';
            }

            function isElementPlaying(el, type) {
                if (!el) return false;
                if (type === 'audio' || type === 'video') {
                    try {
                        return !el.paused && !el.ended && el.currentTime > 0;
                    } catch (_) {
                        return false;
                    }
                }
                if (type === 'youtube') {
                    return isYouTubePlayingState(Number(el.__canopyMiniYTState));
                }
                return false;
            }

            function isOffscreen(el) {
                if (!el || !el.isConnected) return true;
                if (typeof el.__canopyMiniVisible === 'boolean') {
                    return !el.__canopyMiniVisible;
                }
                const rect = el.getBoundingClientRect();
                const rootRect = mainScroller ? mainScroller.getBoundingClientRect() : {
                    top: 0,
                    left: 0,
                    right: window.innerWidth,
                    bottom: window.innerHeight
                };
                const visible = rect.bottom > rootRect.top + 8 &&
                    rect.top < rootRect.bottom - 8 &&
                    rect.right > rootRect.left &&
                    rect.left < rootRect.right;
                return !visible;
            }

            function hideMini() {
                mini.classList.remove('is-visible');
                if (pipBtn) pipBtn.style.display = 'none';
            }

            function showMini() {
                mini.classList.add('is-visible');
            }

            function supportsPictureInPicture(videoEl) {
                if (!videoEl || !videoEl.isConnected) return false;
                if (mediaTypeFor(videoEl) !== 'video') return false;
                if (!document || document.pictureInPictureEnabled !== true) return false;
                return typeof videoEl.requestPictureInPicture === 'function';
            }

            function isPictureInPictureActiveFor(videoEl) {
                if (!videoEl || !document) return false;
                return document.pictureInPictureElement === videoEl;
            }

            function updatePiPButton(el, type) {
                if (!pipBtn) return;
                if (type !== 'video' || !supportsPictureInPicture(el)) {
                    pipBtn.style.display = 'none';
                    return;
                }
                const inPiP = isPictureInPictureActiveFor(el);
                pipBtn.style.display = '';
                pipBtn.innerHTML = `<i class="bi bi-pip me-1"></i><span>${inPiP ? 'Exit PiP' : 'PiP'}</span>`;
                pipBtn.title = inPiP ? 'Exit Picture-in-Picture' : 'Open Picture-in-Picture';
            }

            function setCurrent(el, forcedType) {
                if (!el) return;
                const type = forcedType || mediaTypeFor(el);
                if (!type) return;

                if (state.current && state.current.el === el && state.current.type === type) {
                    updateMini();
                    return;
                }

                state.current = {
                    el: el,
                    type: type,
                    sourceEl: sourceContainer(el),
                    activatedAt: Date.now()
                };
                state.dismissedEl = null;
                updateMini();
            }

            function findPlayingElement() {
                const media = document.querySelectorAll('audio, video');
                for (const el of media) {
                    const type = mediaTypeFor(el);
                    if ((type === 'audio' || type === 'video') && isElementPlaying(el, type)) {
                        return el;
                    }
                }
                const ytEmbeds = document.querySelectorAll('.youtube-embed iframe');
                for (const el of ytEmbeds) {
                    if (isElementPlaying(el, 'youtube')) {
                        return el;
                    }
                }
                return null;
            }

            function applyFocusFlash(target) {
                if (!target) return;
                target.classList.add('canopy-mini-focus-flash');
                setTimeout(() => {
                    target.classList.remove('canopy-mini-focus-flash');
                }, 1700);
            }

            function updateMini() {
                if (!state.current || !state.current.el || !state.current.el.isConnected) {
                    const fallback = findPlayingElement();
                    if (fallback) {
                        setCurrent(fallback);
                        return;
                    }
                    hideMini();
                    return;
                }

                const current = state.current;
                const type = current.type;
                const el = current.el;
                const isResumablePause = (type === 'audio' || type === 'video') && !!el.paused && !el.ended;

                if (state.dismissedEl && state.dismissedEl === el) {
                    hideMini();
                    return;
                }

                if (!isElementPlaying(el, type) && !isResumablePause) {
                    const fallback = findPlayingElement();
                    if (fallback && fallback !== el) {
                        setCurrent(fallback);
                        return;
                    }
                    hideMini();
                    return;
                }

                if (!isOffscreen(el)) {
                    hideMini();
                    return;
                }

                const mediaTitle = titleFromMedia(el, type);
                const subtitle = sourceSubtitle(el);
                titleEl.textContent = mediaTitle;
                subtitleEl.textContent = subtitle;
                icon.innerHTML = `<i class="bi ${mediaIcon(type)}"></i>`;

                if (type === 'audio' || type === 'video') {
                    playBtn.style.display = '';
                    const paused = !!el.paused;
                    playBtn.innerHTML = `<i class="bi bi-${paused ? 'play-fill' : 'pause-fill'} me-1"></i><span>${paused ? 'Play' : 'Pause'}</span>`;

                    const duration = Number(el.duration || 0);
                    const currentTime = Number(el.currentTime || 0);
                    if (duration > 0.1) {
                        const pct = Math.max(0, Math.min(100, (currentTime / duration) * 100));
                        progressWrap.classList.add('show');
                        progressBar.style.width = `${pct}%`;
                        timeEl.textContent = `${formatTime(currentTime)} / ${formatTime(duration)}`;
                    } else {
                        progressWrap.classList.remove('show');
                        progressBar.style.width = '0%';
                        timeEl.textContent = formatTime(currentTime);
                    }
                    updatePiPButton(el, type);
                } else {
                    playBtn.style.display = 'none';
                    progressWrap.classList.remove('show');
                    progressBar.style.width = '0%';
                    timeEl.textContent = 'YouTube';
                    updatePiPButton(null, '');
                }

                showMini();
            }

            function registerMediaNode(el) {
                if (!el || !el.tagName || el.__canopyMiniRegistered) return;
                const type = mediaTypeFor(el);
                if (!type) return;
                el.__canopyMiniRegistered = true;

                if (state.observer) {
                    state.observer.observe(el);
                }

                if (type === 'audio' || type === 'video') {
                    el.addEventListener('play', () => {
                        // Clear dismiss on every new play so the mini player
                        // reappears when the user starts a clip (including the
                        // same audio/video element reused for a different track).
                        state.dismissedEl = null;
                        setCurrent(el, type);
                    });
                    el.addEventListener('pause', () => setTimeout(updateMini, 60));
                    el.addEventListener('ended', () => setTimeout(updateMini, 60));
                    el.addEventListener('timeupdate', updateMini);
                    el.addEventListener('seeking', updateMini);
                    if (type === 'video') {
                        el.addEventListener('enterpictureinpicture', () => setTimeout(updateMini, 20));
                        el.addEventListener('leavepictureinpicture', () => setTimeout(updateMini, 20));
                    }
                } else if (type === 'youtube') {
                    initYouTubePlayer(el);
                    const activate = () => setCurrent(el, 'youtube');
                    el.addEventListener('pointerdown', activate, true);
                    el.addEventListener('focus', activate, true);
                    const wrap = el.closest('.youtube-embed');
                    if (wrap) {
                        wrap.addEventListener('pointerdown', activate, true);
                    }
                }
            }

            function scanForMedia(scope) {
                const root = scope || document;
                root.querySelectorAll('audio, video, .youtube-embed iframe').forEach(registerMediaNode);
            }

            function jumpToCurrentSource() {
                if (!state.current || !state.current.el) return;
                const target = state.current.sourceEl && state.current.sourceEl.isConnected
                    ? state.current.sourceEl
                    : state.current.el;
                target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
                applyFocusFlash(target);
            }

            if (playBtn) {
                playBtn.addEventListener('click', () => {
                    if (!state.current || !state.current.el) return;
                    const el = state.current.el;
                    const type = state.current.type;
                    if (!(type === 'audio' || type === 'video')) return;
                    try {
                        if (el.paused) {
                            el.play();
                        } else {
                            el.pause();
                        }
                    } catch (_) {}
                    setTimeout(updateMini, 50);
                });
            }

            if (jumpBtn) {
                jumpBtn.addEventListener('click', () => {
                    jumpToCurrentSource();
                });
            }

            if (pipBtn) {
                pipBtn.addEventListener('click', async () => {
                    if (!state.current || !state.current.el || state.current.type !== 'video') {
                        jumpToCurrentSource();
                        return;
                    }
                    const el = state.current.el;
                    if (!supportsPictureInPicture(el)) {
                        if (typeof showAlert === 'function') {
                            showAlert('Picture-in-Picture is not available for this video here.', 'info');
                        }
                        jumpToCurrentSource();
                        return;
                    }
                    try {
                        if (isPictureInPictureActiveFor(el)) {
                            if (typeof document.exitPictureInPicture === 'function') {
                                await document.exitPictureInPicture();
                            }
                        } else {
                            if (document.pictureInPictureElement && typeof document.exitPictureInPicture === 'function') {
                                try { await document.exitPictureInPicture(); } catch (_) {}
                            }
                            await el.requestPictureInPicture();
                        }
                    } catch (_) {
                        if (typeof showAlert === 'function') {
                            showAlert('Picture-in-Picture could not be started.', 'info');
                        }
                    }
                    setTimeout(updateMini, 50);
                });
            }

            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    if (state.current && state.current.el) {
                        state.dismissedEl = state.current.el;
                    }
                    hideMini();
                });
            }

            const observerRoot = mainScroller || null;
            state.observer = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    const visible = entry.isIntersecting && entry.intersectionRatio > 0.2;
                    entry.target.__canopyMiniVisible = visible;
                });
                updateMini();
            }, {
                root: observerRoot,
                threshold: [0, 0.2, 0.4, 0.7, 1]
            });

            scanForMedia(document);

            const mutationRoot = mainScroller || document.body;
            state.mutationObserver = new MutationObserver((mutations) => {
                mutations.forEach((mutation) => {
                    mutation.addedNodes.forEach((node) => {
                        if (!(node instanceof Element)) return;
                        if (node.matches && (node.matches('audio') || node.matches('video') || node.matches('.youtube-embed iframe'))) {
                            registerMediaNode(node);
                        }
                        if (node.querySelectorAll) {
                            scanForMedia(node);
                        }
                    });
                });
                updateMini();
            });
            state.mutationObserver.observe(mutationRoot, { childList: true, subtree: true });

            window.addEventListener('resize', updateMini);
            document.addEventListener('visibilitychange', updateMini);
            state.tickHandle = setInterval(updateMini, 700);
            updateMini();
        }
        
        // Three-state Sidebar Toggle Functionality with Mobile Support
        function initSidebarToggle() {
            const toggleBtn = document.getElementById('sidebar-toggle');
            const sidebarContainer = document.getElementById('sidebar-container');
            const sidebar = document.getElementById('main-sidebar');
            const contentContainer = document.getElementById('content-container');
            const mobileBackdrop = document.getElementById('mobile-backdrop');
            
            if (!toggleBtn || !sidebarContainer || !sidebar || !contentContainer) {
                return;
            }
            
            // Sidebar states: 'expanded', 'collapsed', 'hidden'
            let currentState = localStorage.getItem('sidebar-state') || 'expanded';
            
            // Check if mobile and adjust initial state
            if (window.innerWidth < 576 && currentState === 'expanded') {
                currentState = 'collapsed';
            }
            
            applySidebarState(currentState);
            
            // Toggle button click handler - cycles through states
            toggleBtn.addEventListener('click', function() {
                let newState;
                const isMobile = window.innerWidth < 576;
                
                if (isMobile) {
                    // Mobile: expanded -> collapsed -> hidden -> expanded
                    switch(currentState) {
                        case 'expanded':
                            newState = 'collapsed';
                            break;
                        case 'collapsed':
                            newState = 'hidden';
                            break;
                        case 'hidden':
                            newState = 'expanded';
                            break;
                        default:
                            newState = 'collapsed';
                    }
                } else {
                    // Desktop: expanded -> collapsed -> hidden -> expanded
                    switch(currentState) {
                        case 'expanded':
                            newState = 'collapsed';
                            break;
                        case 'collapsed':
                            newState = 'hidden';
                            break;
                        case 'hidden':
                            newState = 'expanded';
                            break;
                        default:
                            newState = 'expanded';
                    }
                }
                
                currentState = newState;
                applySidebarState(newState);
                localStorage.setItem('sidebar-state', newState);
            });
            
            // Mobile backdrop click handler
            if (mobileBackdrop) {
                mobileBackdrop.addEventListener('click', function() {
                    if (currentState === 'expanded') {
                        currentState = 'collapsed';
                        applySidebarState('collapsed');
                        localStorage.setItem('sidebar-state', 'collapsed');
                    }
                });
            }
            
            function applySidebarState(state) {
                const toggleIcon = toggleBtn.querySelector('i');
                const isMobile = window.innerWidth < 576;
                
                // Clear all state classes
                sidebarContainer.classList.remove('expanded', 'collapsed', 'hidden');
                sidebar.classList.remove('expanded', 'collapsed', 'hidden');
                
                // Handle mobile backdrop
                if (mobileBackdrop) {
                    mobileBackdrop.classList.remove('show');
                }
                
                switch(state) {
                    case 'expanded':
                        // Full sidebar with text
                        sidebarContainer.classList.add('expanded');
                        sidebar.classList.add('expanded');
                        toggleIcon.className = 'bi bi-list';
                        toggleBtn.setAttribute('title', 'Collapse to Icons');
                        
                        // Show backdrop on mobile
                        if (isMobile && mobileBackdrop) {
                            mobileBackdrop.classList.add('show');
                        }
                        break;
                        
                    case 'collapsed':
                        // Icon-only sidebar
                        sidebarContainer.classList.add('collapsed');
                        sidebar.classList.add('collapsed');
                        toggleIcon.className = 'bi bi-chevron-left';
                        toggleBtn.setAttribute('title', 'Hide Sidebar');
                        break;
                        
                    case 'hidden':
                        // No sidebar
                        sidebarContainer.classList.add('hidden');
                        sidebar.classList.add('hidden');
                        toggleIcon.className = 'bi bi-chevron-right';
                        toggleBtn.setAttribute('title', 'Show Sidebar');
                        break;
                }
                
                // Update current state
                currentState = state;
            }
            
            // Handle window resize for responsive behavior
            window.addEventListener('resize', function() {
                const windowWidth = window.innerWidth;
                
                // Auto-adjust on screen size change
                if (windowWidth < 576 && currentState === 'expanded') {
                    // On mobile, collapse expanded sidebar
                    applySidebarState(currentState);
                } else if (windowWidth >= 576) {
                    // On desktop, hide backdrop
                    if (mobileBackdrop) {
                        mobileBackdrop.classList.remove('show');
                    }
                    applySidebarState(currentState);
                }
            });
            
            // Touch and swipe gestures for mobile
            if ('ontouchstart' in window) {
                let touchStartX = 0;
                let touchEndX = 0;
                
                document.addEventListener('touchstart', function(e) {
                    touchStartX = e.changedTouches[0].screenX;
                });
                
                document.addEventListener('touchend', function(e) {
                    touchEndX = e.changedTouches[0].screenX;
                    handleSwipe();
                });
                
                function handleSwipe() {
                    const swipeThreshold = 50;
                    const swipeDistance = touchEndX - touchStartX;
                    
                    if (Math.abs(swipeDistance) > swipeThreshold) {
                        if (swipeDistance > 0 && touchStartX < 20) {
                            // Swipe right from left edge - show sidebar
                            if (currentState !== 'expanded') {
                                currentState = 'expanded';
                                applySidebarState('expanded');
                                localStorage.setItem('sidebar-state', 'expanded');
                            }
                        } else if (swipeDistance < 0 && currentState === 'expanded') {
                            // Swipe left - hide sidebar
                            currentState = 'collapsed';
                            applySidebarState('collapsed');
                            localStorage.setItem('sidebar-state', 'collapsed');
                        }
                    }
                }
            }
            
            // Add keyboard shortcuts
            document.addEventListener('keydown', function(e) {
                // Ctrl+B or Cmd+B - cycle through states
                if ((e.ctrlKey || e.metaKey) && e.key === 'b') {
                    e.preventDefault();
                    toggleBtn.click();
                }
                
                // Ctrl+Shift+B or Cmd+Shift+B - toggle between expanded and hidden
                if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key === 'B') {
                    e.preventDefault();
                    const newState = currentState === 'expanded' ? 'hidden' : 'expanded';
                    currentState = newState;
                    applySidebarState(newState);
                    localStorage.setItem('sidebar-state', newState);
                }
            });
            
            // Return the state management functions for external use
            return {
                getCurrentState: () => currentState,
                setState: (state) => {
                    if (['expanded', 'collapsed', 'hidden'].includes(state)) {
                        currentState = state;
                        applySidebarState(state);
                        localStorage.setItem('sidebar-state', state);
                    }
                }
            };
        }
        
        // Copy-to-clipboard for channel code blocks (delegated so it works for dynamic content)
        document.addEventListener('click', function(e) {
            var btn = e.target.closest('.channel-code-copy-btn');
            if (!btn) return;
            var wrap = btn.closest('.channel-code-wrap');
            var codeEl = wrap ? wrap.querySelector('pre code') : null;
            if (!codeEl) return;
            var text = codeEl.textContent;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(function() {
                    var icon = btn.querySelector('i');
                    if (icon) { icon.classList.remove('bi-clipboard'); icon.classList.add('bi-check'); }
                    btn.setAttribute('title', 'Copied!');
                    setTimeout(function() {
                        if (icon) { icon.classList.remove('bi-check'); icon.classList.add('bi-clipboard'); }
                        btn.setAttribute('title', 'Copy to clipboard');
                    }, 2000);
                });
            }
        });

        let _contentContextModal = null;
        const _contentContextState = {
            sourceType: '',
            sourceId: '',
            sourceUrl: '',
            context: null,
            contextId: '',
            noteBaseline: '',
            busy: false,
        };

        function _ctxEl(id) {
            return document.getElementById(id);
        }

        function _ctxSourceLabel(sourceType) {
            const token = String(sourceType || '').trim().toLowerCase();
            if (token === 'feed_post') return 'Feed Post';
            if (token === 'channel_message') return 'Channel Message';
            if (token === 'direct_message') return 'Direct Message';
            if (token === 'url') return 'URL';
            return token || 'Source';
        }

        function _ctxShortId(id) {
            const txt = String(id || '').trim();
            if (!txt) return '';
            if (txt.length <= 18) return txt;
            return `${txt.slice(0, 9)}...${txt.slice(-6)}`;
        }

        function _ctxJsonFetch(url, options = {}) {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
            const merged = {
                ...options,
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': csrfToken,
                    ...(options.headers || {})
                }
            };
            return fetch(url, merged).then(async (resp) => {
                const raw = await resp.text();
                let data = {};
                if (raw) {
                    try {
                        data = JSON.parse(raw);
                    } catch (_) {
                        data = {};
                    }
                }
                if (!resp.ok || (typeof data === 'object' && data && data.success === false)) {
                    const msg = (data && (data.error || data.message)) || raw || `Request failed (${resp.status})`;
                    throw new Error(msg);
                }
                return data || {};
            });
        }

        function _ctxTextFetch(url) {
            const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
            return fetch(url, {
                method: 'GET',
                headers: {
                    'X-CSRFToken': csrfToken,
                }
            }).then(async (resp) => {
                const raw = await resp.text();
                if (!resp.ok) {
                    let msg = raw || `Request failed (${resp.status})`;
                    try {
                        const data = raw ? JSON.parse(raw) : null;
                        if (data && (data.error || data.message)) msg = data.error || data.message;
                    } catch (_) {}
                    throw new Error(msg);
                }
                return raw || '';
            });
        }

        function _ctxSetStatus(message, tone = 'muted') {
            const statusEl = _ctxEl('content-context-status');
            if (!statusEl) return;
            statusEl.textContent = message || '';
            statusEl.style.color = '';
            if (tone === 'error') statusEl.style.color = '#fca5a5';
            if (tone === 'ok') statusEl.style.color = '#86efac';
            if (tone === 'warn') statusEl.style.color = '#fcd34d';
            if (tone === 'info') statusEl.style.color = '#93c5fd';
        }

        function _ctxSetBusy(busy) {
            _contentContextState.busy = !!busy;
            const extractBtn = _ctxEl('content-context-extract-btn');
            const refreshBtn = _ctxEl('content-context-refresh-btn');
            const saveBtn = _ctxEl('content-context-save-note-btn');
            const copyBtn = _ctxEl('content-context-copy-btn');
            const urlInput = _ctxEl('content-context-url-input');
            if (extractBtn) extractBtn.disabled = !!busy;
            if (refreshBtn) refreshBtn.disabled = !!busy;
            if (urlInput) urlInput.disabled = !!busy;
            if (saveBtn && busy) saveBtn.disabled = true;
            if (copyBtn && busy) copyBtn.disabled = true;
        }

        function _ctxResetModal() {
            _contentContextState.context = null;
            _contentContextState.contextId = '';
            _contentContextState.noteBaseline = '';
            const meta = _ctxEl('content-context-meta');
            const summary = _ctxEl('content-context-summary');
            const transcript = _ctxEl('content-context-transcript');
            const extracted = _ctxEl('content-context-extracted');
            const note = _ctxEl('content-context-note');
            const noteStatus = _ctxEl('content-context-note-status');
            const txtBlob = _ctxEl('content-context-text-blob');
            const updated = _ctxEl('content-context-updated-at');
            const sourceLink = _ctxEl('content-context-source-link');
            const copyBtn = _ctxEl('content-context-copy-btn');
            const saveBtn = _ctxEl('content-context-save-note-btn');
            const empty = _ctxEl('content-context-empty');
            if (meta) meta.innerHTML = '';
            if (summary) summary.value = '';
            if (transcript) transcript.value = '';
            if (extracted) extracted.value = '';
            if (note) {
                note.value = '';
                note.disabled = true;
            }
            if (noteStatus) noteStatus.textContent = '';
            if (txtBlob) txtBlob.value = '';
            if (updated) updated.textContent = 'Updated —';
            if (sourceLink) {
                sourceLink.href = '#';
                sourceLink.textContent = 'Open source';
            }
            if (copyBtn) copyBtn.disabled = true;
            if (saveBtn) saveBtn.disabled = true;
            if (empty) empty.classList.remove('d-none');
        }

        function _ctxAddChip(label, value, extraClass = '') {
            const meta = _ctxEl('content-context-meta');
            if (!meta) return;
            const chip = document.createElement('span');
            chip.className = `content-context-chip ${extraClass || ''}`.trim();
            chip.textContent = value ? `${label}: ${value}` : label;
            meta.appendChild(chip);
        }

        function _ctxUpdateNoteState() {
            const note = _ctxEl('content-context-note');
            const saveBtn = _ctxEl('content-context-save-note-btn');
            const noteStatus = _ctxEl('content-context-note-status');
            const context = _contentContextState.context || {};
            if (!note || !saveBtn || !noteStatus) return;

            if (!context.can_edit_note) {
                note.disabled = true;
                saveBtn.disabled = true;
                noteStatus.textContent = context.id ? 'Read-only (owner/admin only).' : '';
                return;
            }

            note.disabled = false;
            const dirty = (note.value || '').trim() !== (_contentContextState.noteBaseline || '');
            saveBtn.disabled = !dirty || _contentContextState.busy;
            noteStatus.textContent = dirty ? 'Unsaved changes' : 'Saved';
        }

        function _ctxRenderContext(context) {
            _contentContextState.context = context || null;
            _contentContextState.contextId = (context && context.id) ? context.id : '';
            const summary = _ctxEl('content-context-summary');
            const transcript = _ctxEl('content-context-transcript');
            const extracted = _ctxEl('content-context-extracted');
            const note = _ctxEl('content-context-note');
            const txtBlob = _ctxEl('content-context-text-blob');
            const updated = _ctxEl('content-context-updated-at');
            const sourceLink = _ctxEl('content-context-source-link');
            const copyBtn = _ctxEl('content-context-copy-btn');
            const empty = _ctxEl('content-context-empty');
            const meta = _ctxEl('content-context-meta');
            if (meta) meta.innerHTML = '';

            if (!context) {
                _ctxResetModal();
                return;
            }

            const status = String(context.status || 'partial').toLowerCase();
            const statusClass = status === 'ready' ? 'is-ready' : (status === 'error' ? 'is-error' : 'is-partial');

            _ctxAddChip('Provider', context.provider || 'unknown');
            _ctxAddChip('Status', status, statusClass);
            if (context.transcript_lang) _ctxAddChip('Transcript', context.transcript_lang);
            if (context.source_type) _ctxAddChip('Source', _ctxSourceLabel(context.source_type));

            if (summary) {
                summary.value = (context.summary_text || '').trim() || 'No summary extracted yet.';
            }
            if (transcript) {
                transcript.value = (context.transcript_text || '').trim() || 'Transcript unavailable for this source.';
            }
            if (extracted) {
                extracted.value = (context.extracted_text || '').trim() || 'No additional extracted text.';
            }
            if (note) {
                note.value = context.owner_note || '';
            }
            _contentContextState.noteBaseline = ((context.owner_note || '').trim());
            if (txtBlob) {
                txtBlob.value = (context.text_blob || '').trim();
            }
            if (updated) {
                const updatedAt = context.updated_at || context.created_at;
                updated.textContent = updatedAt ? `Updated ${formatTimestamp(updatedAt)}` : 'Updated —';
            }
            if (sourceLink) {
                const href = (context.source_url || '').trim();
                sourceLink.href = href || '#';
                sourceLink.textContent = href || 'Open source';
            }
            if (copyBtn) {
                const hasText = !!((context.text_blob || '').trim());
                copyBtn.disabled = !hasText || _contentContextState.busy;
            }
            if (empty) {
                empty.classList.toggle('d-none', !!((context.text_blob || '').trim()));
            }

            _ctxUpdateNoteState();
        }

        function _ctxExtract(forceRefresh) {
            const sourceType = (_contentContextState.sourceType || '').trim();
            const sourceId = (_contentContextState.sourceId || '').trim();
            const sourceUrl = (_ctxEl('content-context-url-input')?.value || '').trim();
            if (!sourceType) {
                _ctxSetStatus('Missing source type.', 'error');
                return Promise.resolve(null);
            }
            if (sourceType !== 'url' && !sourceId) {
                _ctxSetStatus('Missing source ID.', 'error');
                return Promise.resolve(null);
            }
            if (sourceType === 'url' && !sourceUrl) {
                _ctxSetStatus('Provide a URL to extract.', 'warn');
                return Promise.resolve(null);
            }

            const body = {
                source_type: sourceType,
                force_refresh: !!forceRefresh,
            };
            if (sourceId) body.source_id = sourceId;
            if (sourceUrl) body.url = sourceUrl;

            _ctxSetBusy(true);
            _ctxSetStatus(forceRefresh ? 'Refreshing context…' : 'Extracting context…', 'info');
            return _ctxJsonFetch('/ajax/content_contexts/extract', {
                method: 'POST',
                body: JSON.stringify(body),
            })
                .then((data) => {
                    if (!data || !data.context) {
                        throw new Error('No context returned');
                    }
                    _ctxRenderContext(data.context);
                    _ctxSetStatus(data.cached ? 'Loaded cached context.' : 'Context extracted.', 'ok');
                    return data.context;
                })
                .catch((err) => {
                    _ctxSetStatus(err.message || 'Failed to extract context.', 'error');
                    const empty = _ctxEl('content-context-empty');
                    if (empty) empty.classList.remove('d-none');
                    return null;
                })
                .finally(() => {
                    _ctxSetBusy(false);
                    _ctxUpdateNoteState();
                });
        }

        function _ctxSaveNote() {
            const contextId = _contentContextState.contextId;
            const noteEl = _ctxEl('content-context-note');
            if (!contextId || !noteEl) return;
            const noteVal = (noteEl.value || '').trim();
            _ctxSetBusy(true);
            _ctxSetStatus('Saving note…', 'info');
            _ctxJsonFetch(`/ajax/content_contexts/${encodeURIComponent(contextId)}/note`, {
                method: 'POST',
                body: JSON.stringify({ owner_note: noteVal }),
            })
                .then((data) => {
                    if (!data || !data.context) {
                        throw new Error('No context returned');
                    }
                    _ctxRenderContext(data.context);
                    _ctxSetStatus('Owner note saved.', 'ok');
                })
                .catch((err) => {
                    _ctxSetStatus(err.message || 'Failed to save note.', 'error');
                })
                .finally(() => {
                    _ctxSetBusy(false);
                    _ctxUpdateNoteState();
                });
        }

        function _ctxCopyTextBlob() {
            const context = _contentContextState.context || {};
            const contextId = _contentContextState.contextId;
            const existingText = (context.text_blob || '').trim();
            const copy = (text) => {
                if (!text) {
                    showAlert('No context text to copy yet.', 'warning');
                    return;
                }
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    navigator.clipboard.writeText(text)
                        .then(() => showAlert('Context text copied.', 'success'))
                        .catch(() => showAlert('Failed to copy context text.', 'danger'));
                    return;
                }
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                showAlert('Context text copied.', 'success');
            };

            if (existingText) {
                copy(existingText);
                return;
            }
            if (!contextId) {
                showAlert('No context selected.', 'warning');
                return;
            }
            _ctxTextFetch(`/ajax/content_contexts/${encodeURIComponent(contextId)}/text`)
                .then(copy)
                .catch((err) => showAlert(err.message || 'Failed to copy context text.', 'danger'));
        }

        function openContentContextModalFromAction(sourceType, sourceId, sourceUrl = '') {
            const modalEl = _ctxEl('contentContextModal');
            if (!modalEl) return;
            _contentContextModal = _contentContextModal || bootstrap.Modal.getOrCreateInstance(modalEl);

            _contentContextState.sourceType = String(sourceType || '').trim();
            _contentContextState.sourceId = String(sourceId || '').trim();
            _contentContextState.sourceUrl = String(sourceUrl || '').trim();

            const subtitle = _ctxEl('content-context-modal-subtitle');
            if (subtitle) {
                const label = _ctxSourceLabel(_contentContextState.sourceType);
                const sid = _ctxShortId(_contentContextState.sourceId);
                subtitle.textContent = sid ? `Source: ${label} • ${sid}` : `Source: ${label}`;
            }
            const urlInput = _ctxEl('content-context-url-input');
            if (urlInput) urlInput.value = _contentContextState.sourceUrl || '';

            _ctxResetModal();
            _contentContextModal.show();
            _ctxExtract(false);
        }

        function initContentContextModal() {
            const modalEl = _ctxEl('contentContextModal');
            if (!modalEl || modalEl.dataset.initialized === '1') return;
            modalEl.dataset.initialized = '1';
            _contentContextModal = bootstrap.Modal.getOrCreateInstance(modalEl);

            const extractBtn = _ctxEl('content-context-extract-btn');
            const refreshBtn = _ctxEl('content-context-refresh-btn');
            const saveBtn = _ctxEl('content-context-save-note-btn');
            const copyBtn = _ctxEl('content-context-copy-btn');
            const noteEl = _ctxEl('content-context-note');
            const urlInput = _ctxEl('content-context-url-input');

            if (extractBtn) {
                extractBtn.addEventListener('click', function() { _ctxExtract(false); });
            }
            if (refreshBtn) {
                refreshBtn.addEventListener('click', function() { _ctxExtract(true); });
            }
            if (saveBtn) {
                saveBtn.addEventListener('click', _ctxSaveNote);
            }
            if (copyBtn) {
                copyBtn.addEventListener('click', _ctxCopyTextBlob);
            }
            if (noteEl) {
                noteEl.addEventListener('input', _ctxUpdateNoteState);
            }
            if (urlInput) {
                urlInput.addEventListener('keydown', function(e) {
                    if (e.key === 'Enter') {
                        e.preventDefault();
                        _ctxExtract(false);
                    }
                });
            }
            modalEl.addEventListener('hidden.bs.modal', function() {
                _ctxSetStatus('');
                _ctxResetModal();
            });
        }

        window.openContentContextModalFromAction = openContentContextModalFromAction;

        let _fileAccessModal = null;

        function _extractFileIdFromInput(fileOrUrl) {
            if (!fileOrUrl) return null;
            const raw = String(fileOrUrl).trim();
            if (!raw) return null;
            if (!raw.includes('/')) return raw;
            const match = raw.match(/\/files\/([^\/?#]+)/i);
            if (!match || !match[1]) return null;
            try {
                return decodeURIComponent(match[1]);
            } catch (_) {
                return match[1];
            }
        }

        function openFileAccessInspector(fileOrUrl) {
            const fileId = _extractFileIdFromInput(fileOrUrl);
            if (!fileId) {
                if (typeof showAlert === 'function') showAlert('Invalid file reference', 'warning');
                return;
            }

            const modalEl = document.getElementById('fileAccessInspectorModal');
            if (!modalEl) return;
            if (!_fileAccessModal) {
                _fileAccessModal = new bootstrap.Modal(modalEl);
            }
            document.getElementById('file-access-title').textContent = fileId;
            document.getElementById('file-access-summary').textContent = 'Checking access…';
            document.getElementById('file-access-evidence').innerHTML = '<div class="small text-muted">Loading evidence…</div>';
            _fileAccessModal.show();

            fetch(`/ajax/files/${encodeURIComponent(fileId)}/access`)
                .then((r) => r.json())
                .then((data) => {
                    if (!data.success) {
                        throw new Error(data.error || 'Could not load file access');
                    }
                    const access = data.access || {};
                    const allowed = !!access.allowed;
                    const reason = access.reason || 'unknown';
                    const evidences = Array.isArray(access.evidence) ? access.evidence : [];
                    const summary = document.getElementById('file-access-summary');
                    summary.innerHTML = `
                        <span class="badge ${allowed ? 'bg-success' : 'bg-danger'} me-2">${allowed ? 'Allowed' : 'Denied'}</span>
                        <span class="text-muted">Reason: ${reason}</span>
                    `;
                    const evidenceEl = document.getElementById('file-access-evidence');
                    if (!evidences.length) {
                        evidenceEl.innerHTML = '<div class="small text-muted">No referencing content found on this node.</div>';
                        return;
                    }
                    evidenceEl.innerHTML = evidences.map((ev) => `
                        <div class="border rounded p-2 mb-2">
                            <div class="d-flex justify-content-between align-items-center">
                                <strong>${ev.source_type || 'source'}</strong>
                                <span class="badge ${ev.can_view ? 'bg-success' : 'bg-secondary'}">${ev.can_view ? 'visible' : 'hidden'}</span>
                            </div>
                            <div class="small text-muted">${ev.detail || ''}</div>
                            <div class="small text-muted">ref: ${ev.source_id || '—'}</div>
                        </div>
                    `).join('');
                })
                .catch((err) => {
                    document.getElementById('file-access-summary').innerHTML =
                        `<span class="badge bg-danger me-2">Error</span><span class="text-muted">${err.message || 'Could not inspect file access'}</span>`;
                    document.getElementById('file-access-evidence').innerHTML =
                        '<div class="small text-muted">Try again after the file syncs.</div>';
                });
        }

        // Initialize sidebar toggle when DOM is ready
        document.addEventListener('DOMContentLoaded', function() {
            initSidebarMediaMiniPlayer();
            initContentContextModal();
            initSidebarToggle();
            initMobileOptimizations();
            initPeerActivityNotifications();
        });
        
        // Mobile-specific optimizations
        function initMobileOptimizations() {
            // iOS Safari viewport height fix
            function setVH() {
                let vh = window.innerHeight * 0.01;
                document.documentElement.style.setProperty('--vh', `${vh}px`);
            }
            
            setVH();
            window.addEventListener('resize', setVH);
            window.addEventListener('orientationchange', function() {
                setTimeout(setVH, 100);
            });
            
            // Prevent elastic scrolling on iOS
            document.addEventListener('touchmove', function(e) {
                if (e.target.closest('.main-content') || e.target.closest('.sidebar')) {
                    return; // Allow scrolling in content areas
                }
                e.preventDefault();
            }, { passive: false });
            
            // Handle iOS safe areas
            if (CSS.supports('padding: max(0px)')) {
                document.documentElement.style.setProperty('--safe-top', 'env(safe-area-inset-top)');
                document.documentElement.style.setProperty('--safe-bottom', 'env(safe-area-inset-bottom)');
                document.documentElement.style.setProperty('--safe-left', 'env(safe-area-inset-left)');
                document.documentElement.style.setProperty('--safe-right', 'env(safe-area-inset-right)');
            }
            
            // PWA install prompt
            let deferredPrompt;
            window.addEventListener('beforeinstallprompt', (e) => {
                e.preventDefault();
                deferredPrompt = e;
                showInstallButton();
            });
            
            function showInstallButton() {
                // Add install button to navbar (optional)
                const navbar = document.querySelector('.navbar .container-fluid');
                if (navbar && !document.getElementById('install-btn')) {
                    const installBtn = document.createElement('button');
                    installBtn.id = 'install-btn';
                    installBtn.className = 'btn btn-outline-primary btn-sm me-2';
                    installBtn.innerHTML = '<i class="bi bi-download"></i>';
                    installBtn.title = 'Install Canopy';
                    installBtn.onclick = installApp;
                    
                    const toggleBtn = document.getElementById('sidebar-toggle');
                    navbar.insertBefore(installBtn, toggleBtn);
                }
            }
            
            async function installApp() {
                if (deferredPrompt) {
                    deferredPrompt.prompt();
                    const { outcome } = await deferredPrompt.userChoice;
                    console.log(`User response to install prompt: ${outcome}`);
                    deferredPrompt = null;
                    
                    const installBtn = document.getElementById('install-btn');
                    if (installBtn) {
                        installBtn.remove();
                    }
                }
            }
            
            // Handle PWA installation
            window.addEventListener('appinstalled', (evt) => {
                console.log('Canopy was installed successfully');
                const installBtn = document.getElementById('install-btn');
                if (installBtn) {
                    installBtn.remove();
                }
            });
        }
