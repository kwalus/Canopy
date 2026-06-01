(function () {
    'use strict';

    const WS_ID_TEXT_RE = /Ws[A-Fa-f0-9]{12,}/;
    const WS_BRACKET_RE = /\[workstream\s*[:=]\s*(Ws[A-Fa-f0-9]{12,})(?:\s*\|\s*([^\]]{1,140}))?\]/gi;
    const WS_BARE_RE = /(^|[\s([{<>'"`])(?:workstream\s*[:=]\s*)?(Ws[A-Fa-f0-9]{12,})(?=$|[\s)\]}>.,;:!?;'"`])/g;
    const SCAN_SELECTOR = '.rich-content, .post-content, .message-content, .message-text, [data-post-content="1"], [data-message-content="1"]';
    const SKIP_SELECTOR = 'a,button,textarea,input,select,code,pre,script,style,.canopy-workstream-ref,.canopy-workstream-ref-wrap,.canopy-workstream-modal';
    const workstreamPreviewCache = new Map();
    const workstreamPayloadCache = new Map();

    function escapeHtml(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function shortId(value) {
        const id = String(value || '').trim();
        return id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-5)}` : id;
    }

    function shortDate(value) {
        if (!value) return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
        return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    }

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
    }

    function workstreamFetchHeaders() {
        const headers = { 'X-Requested-With': 'XMLHttpRequest' };
        const token = csrfToken();
        if (token) headers['X-CSRFToken'] = token;
        return headers;
    }

    function userAvatar(user) {
        const display = String(user?.display_name || user?.username || user?.id || '?').trim();
        const initials = display.split(/\s+/).map((p) => p.charAt(0)).join('').slice(0, 2).toUpperCase() || '?';
        if (user?.avatar_url) {
            return `<img src="${escapeHtml(user.avatar_url)}" alt="" loading="lazy">`;
        }
        if (user?.avatar_file_id) {
            return `<img src="/files/${encodeURIComponent(user.avatar_file_id)}" alt="" loading="lazy">`;
        }
        return `<span>${escapeHtml(initials)}</span>`;
    }

    function asArray(value) {
        return Array.isArray(value) ? value : [];
    }

    function workstreamStatusLabel(value) {
        return String(value || 'active').replace(/_/g, ' ');
    }

    function workstreamTone(value) {
        const clean = String(value || '').toLowerCase();
        if (clean === 'blocked' || clean === 'critical') return 'danger';
        if (clean === 'review_ready' || clean === 'high') return 'warning';
        if (clean === 'complete' || clean === 'closed' || clean === 'resolved') return 'success';
        return 'active';
    }

    function artifactIcon(artifact) {
        const type = String(artifact?.artifact_type || '').toLowerCase();
        const ref = String(artifact?.ref_id || '').trim();
        if (type === 'digestion' || ref.startsWith('Dg')) return 'bi-diagram-3';
        if (type === 'url' || /^https?:\/\//i.test(ref)) return 'bi-link-45deg';
        if (type === 'figure') return 'bi-image';
        if (type === 'code') return 'bi-code-square';
        if (type === 'report') return 'bi-file-earmark-text';
        if (type === 'message' || ref.startsWith('M')) return 'bi-chat-square-text';
        if (type === 'post') return 'bi-newspaper';
        return 'bi-file-earmark';
    }

    function artifactHref(artifact) {
        const type = String(artifact?.artifact_type || '').toLowerCase();
        const ref = String(artifact?.ref_id || '').trim();
        if (!ref) return '#';
        if (type === 'file' || ref.startsWith('F')) return `/file-ref/${encodeURIComponent(ref)}`;
        if (type === 'digestion' || ref.startsWith('Dg')) return `/vault?digestion=${encodeURIComponent(ref)}`;
        if (type === 'post') return `/feed?post=${encodeURIComponent(ref)}`;
        if (type === 'message') return `/channels?message=${encodeURIComponent(ref)}`;
        if (/^https?:\/\//i.test(ref)) return ref;
        return '#';
    }

    function workstreamSummary(ws) {
        return String(ws.summary || ws.objective || ws.required_output || ws.next_action || '').trim();
    }

    function workstreamLatestEvent(ws) {
        return asArray(ws.events)[0] || null;
    }

    function workstreamMetrics(ws) {
        const events = asArray(ws.events);
        const blockers = events.filter((event) => String(event.event_type || '').toLowerCase() === 'blocker' && !['resolved', 'complete'].includes(String(event.status || event.metadata?.event_state || '').toLowerCase())).length;
        return {
            participants: asArray(ws.participants).length,
            artifacts: asArray(ws.artifacts).length,
            events: events.length,
            blockers,
        };
    }

    function renderParticipantAvatars(participants, limit = 7) {
        const rows = asArray(participants);
        if (!rows.length) return '<span class="canopy-workstream-mini-empty">No people yet</span>';
        const visible = rows.slice(0, limit).map((p) => {
            const user = p.user || { id: p.user_id };
            const name = user.display_name || user.username || p.user_id || 'Participant';
            return `<span class="canopy-workstream-mini-avatar" title="${escapeHtml(name)} · ${escapeHtml(p.role || 'contributor')}">${userAvatar(user)}</span>`;
        }).join('');
        const extra = rows.length > limit ? `<span class="canopy-workstream-mini-count">+${rows.length - limit}</span>` : '';
        return `${visible}${extra}`;
    }

    function renderParticipants(participants) {
        const rows = asArray(participants);
        if (!rows.length) return '<div class="canopy-workstream-empty">No participants listed yet.</div>';
        return `<div class="canopy-workstream-participants">${rows.map((p) => {
            const user = p.user || { id: p.user_id };
            const name = user.display_name || user.username || p.user_id;
            return `<div class="canopy-workstream-person" title="${escapeHtml(name)}">
                <div class="canopy-workstream-avatar">${userAvatar(user)}</div>
                <div><strong>${escapeHtml(name)}</strong><span>${escapeHtml(p.role || 'contributor')} · ${escapeHtml(p.status || 'active')}</span></div>
            </div>`;
        }).join('')}</div>`;
    }

    function renderArtifacts(artifacts) {
        const rows = asArray(artifacts);
        if (!rows.length) return '<div class="canopy-workstream-empty">No artifacts attached yet.</div>';
        return `<div class="canopy-workstream-artifacts">${rows.map((a) => {
            const href = artifactHref(a);
            const title = a.title || a.ref_id || 'Artifact';
            const target = /^https?:\/\//i.test(href) || href.startsWith('/file-ref/') ? ' target="_blank" rel="noopener noreferrer"' : '';
            return `<a class="canopy-workstream-artifact" href="${escapeHtml(href)}"${target}>
                <i class="bi ${artifactIcon(a)}"></i>
                <span><strong>${escapeHtml(title)}</strong><em>${escapeHtml(a.artifact_type || 'artifact')} · ${escapeHtml(shortId(a.ref_id || ''))}</em></span>
            </a>`;
        }).join('')}</div>`;
    }

    function renderEvents(events) {
        const rows = asArray(events);
        if (!rows.length) return '<div class="canopy-workstream-empty">No progress events yet.</div>';
        return `<div class="canopy-workstream-events">${rows.slice(0, 30).map((e) => {
            const actor = e.actor || { id: e.actor_user_id };
            const name = actor.display_name || actor.username || e.actor_user_id || 'Actor';
            const title = e.title || e.event_type || 'Update';
            const eventState = e.metadata?.event_state || e.status || '';
            const tone = workstreamTone(e.event_type === 'blocker' ? 'blocked' : eventState);
            return `<article class="canopy-workstream-event" data-type="${escapeHtml(e.event_type || '')}" data-tone="${escapeHtml(tone)}">
                <div class="canopy-workstream-event-avatar">${userAvatar(actor)}</div>
                <div class="canopy-workstream-event-body">
                    <div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(name)} · ${escapeHtml(shortDate(e.created_at || ''))}${eventState ? ` · ${escapeHtml(eventState)}` : ''}</span></div>
                    ${e.body ? `<p>${escapeHtml(e.body)}</p>` : ''}
                </div>
            </article>`;
        }).join('')}</div>`;
    }

    function renderMetric(label, value, icon) {
        return `<span class="canopy-workstream-metric"><i class="bi ${escapeHtml(icon)}"></i><strong>${escapeHtml(value)}</strong><em>${escapeHtml(label)}</em></span>`;
    }

    function renderWorkstream(payload) {
        const ws = payload?.workstream || payload || {};
        const title = ws.title || ws.id || 'Workstream';
        const status = ws.status || 'active';
        const priority = ws.priority || 'normal';
        const summary = workstreamSummary(ws) || 'Sustained human-agent work with linked evidence and artifacts.';
        const latest = workstreamLatestEvent(ws);
        const metrics = workstreamMetrics(ws);
        const tone = workstreamTone(status);
        return `<div class="canopy-workstream-reader" data-workstream-tone="${escapeHtml(tone)}">
            <header class="canopy-workstream-reader-head">
                <div class="canopy-workstream-reader-main">
                    <div class="canopy-workstream-kicker"><i class="bi bi-kanban"></i> Workstream</div>
                    <h2>${escapeHtml(title)}</h2>
                    <p>${escapeHtml(summary)}</p>
                    <div class="canopy-workstream-avatar-strip" aria-label="Workstream participants">${renderParticipantAvatars(ws.participants, 9)}</div>
                </div>
                <div class="canopy-workstream-status-block">
                    <span class="canopy-workstream-status" data-tone="${escapeHtml(tone)}">${escapeHtml(workstreamStatusLabel(status))}</span>
                    <span>${escapeHtml(priority)} priority</span>
                    <code>${escapeHtml(ws.id || '')}</code>
                    <button type="button" class="canopy-workstream-deck-primary" data-open-workstream-deck="${escapeHtml(ws.id || '')}"><i class="bi bi-window-stack"></i> Open Deck</button>
                </div>
            </header>
            <section class="canopy-workstream-metrics" aria-label="Workstream summary">
                ${renderMetric('people', metrics.participants, 'bi-people')}
                ${renderMetric('artifacts', metrics.artifacts, 'bi-folder2-open')}
                ${renderMetric('events', metrics.events, 'bi-activity')}
                ${renderMetric('blockers', metrics.blockers, metrics.blockers ? 'bi-exclamation-triangle' : 'bi-check-circle')}
            </section>
            <section class="canopy-workstream-brief-grid">
                <div class="canopy-workstream-panel canopy-workstream-brief-card"><h3>Objective</h3><p>${escapeHtml(ws.objective || ws.summary || 'No objective recorded yet.')}</p></div>
                <div class="canopy-workstream-panel canopy-workstream-brief-card"><h3>Required output</h3><p>${escapeHtml(ws.required_output || 'No required output recorded yet.')}</p></div>
                <div class="canopy-workstream-panel canopy-workstream-brief-card"><h3>Next action</h3><p>${escapeHtml(ws.next_action || (latest ? (latest.title || latest.body || 'Review the latest progress event.') : 'No next action recorded yet.'))}</p></div>
            </section>
            <section class="canopy-workstream-grid">
                <div class="canopy-workstream-panel"><h3>Participants</h3>${renderParticipants(ws.participants)}</div>
                <div class="canopy-workstream-panel"><h3>Artifacts and workproducts</h3>${renderArtifacts(ws.artifacts)}</div>
                <div class="canopy-workstream-panel canopy-workstream-panel-wide"><h3>Progress timeline</h3>${renderEvents(ws.events)}</div>
            </section>
            <footer class="canopy-workstream-reader-foot">
                <button type="button" class="canopy-workstream-copy" data-copy-workstream-ref="${escapeHtml(ws.id || '')}"><i class="bi bi-clipboard"></i> Copy agent ref</button>
                <button type="button" class="canopy-workstream-copy" data-open-workstream-deck="${escapeHtml(ws.id || '')}"><i class="bi bi-window-stack"></i> Open in Deck</button>
                ${payload?.agent_reference ? '<span>Agent reference endpoint is available for handoffs.</span>' : ''}
            </footer>
        </div>`;
    }

    function sourceContainerForNode(node) {
        const el = node instanceof Element ? node : null;
        if (!el) return null;
        return el.closest('.post-card[data-post-id], .message-item[data-message-id], .card, .canopy-workstream-modal-card') || el;
    }

    function buildWorkstreamDeckManifest(payload) {
        const ws = payload?.workstream || payload || {};
        const metrics = workstreamMetrics(ws);
        const title = String(ws.title || ws.id || 'Workstream');
        const status = String(ws.status || 'active');
        return {
            version: 1,
            key: `workstream:${String(ws.id || title)}`,
            widget_type: 'workstream',
            render_mode: 'workstream_workspace',
            title,
            subtitle: String(workstreamSummary(ws) || 'Sustained human-agent workstream with people, artifacts, and progress events.'),
            provider_label: 'Workstream',
            icon: 'bi-kanban',
            badges: [
                status,
                String(ws.priority || 'normal'),
                `${metrics.participants} people`,
                `${metrics.artifacts} artifacts`,
                `${metrics.events} events`,
            ],
            details: [
                { label: 'Status', value: status },
                { label: 'Priority', value: String(ws.priority || 'normal') },
                { label: 'Participants', value: String(metrics.participants) },
                { label: 'Artifacts', value: String(metrics.artifacts) },
                { label: 'Updated', value: shortDate(ws.updated_at || ws.created_at || '') || 'unknown' },
            ],
            station_surface: {
                kind: 'station_surface',
                label: 'Workstream Deck workspace',
                summary: 'Larger coordination surface for sustained human-agent work, progress review, and artifact handoff.',
                domain: 'operations',
                scope: 'source',
                recurring: false,
            },
            action_policy: {
                audit_label: 'View and hand off through Workstream ACL',
                max_risk: 'view',
                human_gate: 'none',
            },
            source_binding: {
                binding_type: 'workstream',
                return_label: 'Return to source',
            },
            workstream: ws,
        };
    }

    async function fetchWorkstreamPayload(id) {
        const cleanId = String(id || '').trim();
        if (!cleanId) throw new Error('Missing Workstream ID');
        if (workstreamPayloadCache.has(cleanId)) return workstreamPayloadCache.get(cleanId);
        const promise = fetch(`/api/v1/workstreams/${encodeURIComponent(cleanId)}`, { headers: workstreamFetchHeaders() })
            .then(async (response) => {
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) throw new Error(payload.error || 'Unable to open Workstream');
                return payload;
            })
            .catch((error) => {
                workstreamPayloadCache.delete(cleanId);
                throw error;
            });
        workstreamPayloadCache.set(cleanId, promise);
        return promise;
    }

    async function openWorkstreamDeck(id, anchor) {
        const deckOpener = window && typeof window.canopyOpenMediaDeckForSource === 'function'
            ? window.canopyOpenMediaDeckForSource
            : null;
        if (!deckOpener) {
            if (typeof window.showAlert === 'function') window.showAlert('Canopy Deck is not available on this page yet.', 'warning');
            return;
        }
        try {
            const payload = await fetchWorkstreamPayload(id);
            const ws = payload.workstream || { id };
            const sourceEl = sourceContainerForNode(anchor) || document.body;
            const manifest = buildWorkstreamDeckManifest({ ...payload, workstream: ws });
            const item = {
                key: manifest.key,
                el: anchor instanceof Element ? anchor : sourceEl,
                sourceEl,
                type: 'workstream',
                title: manifest.title,
                subtitle: manifest.subtitle,
                providerLabel: manifest.provider_label,
                icon: manifest.icon,
                manifest,
            };
            try {
                if (window && typeof window.canopySetDeckDesktopMode === 'function') {
                    window.canopySetDeckDesktopMode('large');
                }
            } catch (_) {}
            deckOpener(sourceEl, { explicitItem: item, preferredKey: item.key, play: false });
            closeModal();
        } catch (error) {
            if (typeof window.showAlert === 'function') {
                window.showAlert(error.message || 'Unable to open Workstream in Deck.', 'warning');
            }
        }
    }

    function ensureModal() {
        let modal = document.querySelector('.canopy-workstream-modal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.className = 'canopy-workstream-modal';
        modal.innerHTML = `<div class="canopy-workstream-modal-backdrop" data-close-workstream="1"></div>
            <div class="canopy-workstream-modal-card" role="dialog" aria-modal="true" aria-label="Workstream reader">
                <button type="button" class="canopy-workstream-modal-close" data-close-workstream="1" aria-label="Close Workstream reader"><i class="bi bi-x-lg"></i></button>
                <div class="canopy-workstream-modal-content"><div class="canopy-workstream-loading">Loading Workstream…</div></div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (event) => {
            if (event.target.closest('[data-close-workstream="1"]')) closeModal();
            const deckBtn = event.target.closest('[data-open-workstream-deck]');
            if (deckBtn) {
                event.preventDefault();
                openWorkstreamDeck(deckBtn.getAttribute('data-open-workstream-deck') || '', deckBtn);
                return;
            }
            const copyBtn = event.target.closest('[data-copy-workstream-ref]');
            if (copyBtn) {
                const id = copyBtn.getAttribute('data-copy-workstream-ref') || '';
                const text = id ? `[workstream:${id}]` : '';
                if (text && navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
            }
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && modal.classList.contains('is-open')) closeModal();
        });
        return modal;
    }

    function closeModal() {
        document.querySelector('.canopy-workstream-modal')?.classList.remove('is-open');
    }

    async function openWorkstream(id) {
        const modal = ensureModal();
        const content = modal.querySelector('.canopy-workstream-modal-content');
        modal.classList.add('is-open');
        content.innerHTML = '<div class="canopy-workstream-loading">Loading Workstream…</div>';
        try {
            const payload = await fetchWorkstreamPayload(id);
            content.innerHTML = renderWorkstream(payload);
        } catch (error) {
            content.innerHTML = `<div class="canopy-workstream-error"><strong>Workstream unavailable</strong><span>${escapeHtml(error.message || error)}</span><em>${escapeHtml(id)}</em></div>`;
        }
    }

    async function fetchWorkstreamPreview(id) {
        if (!id) return null;
        if (workstreamPreviewCache.has(id)) return workstreamPreviewCache.get(id);
        const promise = fetch(`/api/v1/workstreams/${encodeURIComponent(id)}?summary=1`, { headers: workstreamFetchHeaders() })
            .then(async (response) => {
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    return { ok: false, id, error: payload.error || 'Private or unavailable Workstream' };
                }
                const ws = payload.workstream || {};
                return {
                    ok: true,
                    id,
                    title: ws.title || id,
                    status: ws.status || 'active',
                    priority: ws.priority || 'normal',
                    updated_at: ws.updated_at || '',
                    channel_id: ws.channel_id || '',
                    summary: workstreamSummary(ws),
                };
            })
            .catch((error) => ({ ok: false, id, error: error.message || 'Unable to check Workstream' }));
        workstreamPreviewCache.set(id, promise);
        return promise;
    }

    function applyPreviewToButton(button, preview) {
        if (!button || !preview) return;
        const wrap = button.closest('.canopy-workstream-ref-wrap');
        const label = button.querySelector('.canopy-workstream-ref-label');
        const meta = button.querySelector('.canopy-workstream-ref-meta');
        if (!preview.ok) {
            button.classList.add('is-unavailable');
            button.setAttribute('aria-disabled', 'true');
            button.title = `${preview.error}. ${preview.id}`;
            if (wrap) wrap.classList.add('is-unavailable');
            if (label) label.textContent = 'Private/unavailable Workstream';
            if (meta) meta.textContent = shortId(preview.id);
            return;
        }
        button.classList.remove('is-unavailable');
        button.removeAttribute('aria-disabled');
        if (wrap) wrap.classList.remove('is-unavailable');
        button.title = `${preview.title} · ${preview.status}${preview.updated_at ? ` · ${shortDate(preview.updated_at)}` : ''}`;
        if (label && !button.dataset.explicitLabel) label.textContent = preview.title;
        if (meta) meta.textContent = `${workstreamStatusLabel(preview.status)}${preview.updated_at ? ` · ${shortDate(preview.updated_at)}` : ''}`;
    }

    function hydrateWorkstreamButton(button) {
        const id = button?.dataset?.workstreamId || '';
        if (!id || button.dataset.workstreamHydrating === '1') return;
        button.dataset.workstreamHydrating = '1';
        fetchWorkstreamPreview(id).then((preview) => applyPreviewToButton(button, preview));
    }

    function makeWorkstreamButton(id, label) {
        const wrap = document.createElement('span');
        wrap.className = 'canopy-workstream-ref-wrap';
        wrap.dataset.workstreamId = id;
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'canopy-workstream-ref';
        button.dataset.workstreamId = id;
        if (label) button.dataset.explicitLabel = '1';
        button.title = `Checking Workstream ${id}`;
        button.innerHTML = `<i class="bi bi-kanban"></i><span class="canopy-workstream-ref-text"><strong class="canopy-workstream-ref-label">${escapeHtml(label || 'Workstream ' + shortId(id))}</strong><em class="canopy-workstream-ref-meta">${escapeHtml(shortId(id))}</em></span>`;
        const deckBtn = document.createElement('button');
        deckBtn.type = 'button';
        deckBtn.className = 'canopy-workstream-ref-deck';
        deckBtn.dataset.workstreamId = id;
        deckBtn.title = 'Open Workstream in Deck';
        deckBtn.setAttribute('aria-label', 'Open Workstream in Deck');
        deckBtn.innerHTML = '<i class="bi bi-window-stack"></i>';
        wrap.appendChild(button);
        wrap.appendChild(deckBtn);
        hydrateWorkstreamButton(button);
        return wrap;
    }

    function linkifyTextNode(node) {
        const text = node.nodeValue || '';
        if (!text || !WS_ID_TEXT_RE.test(text)) return;
        const matches = [];
        WS_BRACKET_RE.lastIndex = 0;
        text.replace(WS_BRACKET_RE, (match, id, label, offset) => {
            matches.push({ start: offset, end: offset + match.length, id, label: String(label || '').trim() });
            return match;
        });
        WS_BARE_RE.lastIndex = 0;
        text.replace(WS_BARE_RE, (match, prefix, id, offset) => {
            const prefixLen = prefix ? prefix.length : 0;
            const start = offset + prefixLen;
            const end = offset + match.length;
            if (!matches.some((item) => start < item.end && end > item.start)) {
                matches.push({ start, end, id, label: '' });
            }
            return match;
        });
        if (!matches.length) return;
        matches.sort((a, b) => a.start - b.start);
        const frag = document.createDocumentFragment();
        let last = 0;
        for (const match of matches) {
            const { start, end, id, label } = match;
            if (start > last) frag.appendChild(document.createTextNode(text.slice(last, start)));
            frag.appendChild(makeWorkstreamButton(id, label));
            last = end;
        }
        if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
        node.parentNode.replaceChild(frag, node);
    }

    function scan(root) {
        if (!root) return;
        const roots = [];
        if (root.nodeType === Node.ELEMENT_NODE && root.matches?.(SCAN_SELECTOR)) roots.push(root);
        if (root.querySelectorAll) root.querySelectorAll(SCAN_SELECTOR).forEach((el) => roots.push(el));
        roots.forEach((el) => {
            if (el.dataset.workstreamLinkified === '1') return;
            if (!WS_ID_TEXT_RE.test(el.textContent || '')) return;
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
                acceptNode(node) {
                    const parent = node.parentElement;
                    if (!parent || parent.closest(SKIP_SELECTOR)) return NodeFilter.FILTER_REJECT;
                    return WS_ID_TEXT_RE.test(node.nodeValue || '') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                }
            });
            const nodes = [];
            while (walker.nextNode()) nodes.push(walker.currentNode);
            nodes.forEach(linkifyTextNode);
            el.dataset.workstreamLinkified = '1';
        });
    }

    document.addEventListener('click', (event) => {
        const deckRef = event.target.closest?.('.canopy-workstream-ref-deck[data-workstream-id]');
        if (deckRef) {
            event.preventDefault();
            event.stopPropagation();
            if (deckRef.closest('.is-unavailable')) return;
            openWorkstreamDeck(deckRef.getAttribute('data-workstream-id') || '', deckRef);
            return;
        }
        const ref = event.target.closest?.('.canopy-workstream-ref[data-workstream-id]');
        if (!ref) return;
        event.preventDefault();
        if (ref.classList.contains('is-unavailable')) return;
        openWorkstream(ref.getAttribute('data-workstream-id') || '');
    });

    document.addEventListener('DOMContentLoaded', () => {
        scan(document.body);
        const observer = new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                mutation.addedNodes.forEach((node) => {
                    if (node.nodeType === Node.ELEMENT_NODE) scan(node);
                });
            }
        });
        observer.observe(document.body, { childList: true, subtree: true });
    });

    window.CanopyWorkstreams = { open: openWorkstream, openDeck: openWorkstreamDeck, scan };
})();
