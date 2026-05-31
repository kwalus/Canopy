(function () {
    'use strict';

    const WS_ID_TEXT_RE = /Ws[A-Fa-f0-9]{12,}/;
    const WS_BRACKET_RE = /\[workstream\s*[:=]\s*(Ws[A-Fa-f0-9]{12,})(?:\s*\|\s*([^\]]{1,140}))?\]/gi;
    const WS_BARE_RE = /(^|[\s([{<>'"`])(?:workstream\s*[:=]\s*)?(Ws[A-Fa-f0-9]{12,})(?=$|[\s)\]}>.,;:!?;'"`])/g;
    const SCAN_SELECTOR = '.rich-content, .post-content, .message-content, .message-text, [data-post-content="1"], [data-message-content="1"]';
    const SKIP_SELECTOR = 'a,button,textarea,input,select,code,pre,script,style,.canopy-workstream-ref,.canopy-workstream-modal';
    const workstreamPreviewCache = new Map();

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

    function renderParticipants(participants) {
        const rows = Array.isArray(participants) ? participants : [];
        if (!rows.length) return '<div class="canopy-workstream-empty">No participants listed yet.</div>';
        return `<div class="canopy-workstream-participants">${rows.map((p) => {
            const user = p.user || { id: p.user_id };
            const name = user.display_name || user.username || p.user_id;
            return `<div class="canopy-workstream-person" title="${escapeHtml(name)}">
                <div class="canopy-workstream-avatar">${userAvatar(user)}</div>
                <div><strong>${escapeHtml(name)}</strong><span>${escapeHtml(p.role || 'contributor')}</span></div>
            </div>`;
        }).join('')}</div>`;
    }

    function renderArtifacts(artifacts) {
        const rows = Array.isArray(artifacts) ? artifacts : [];
        if (!rows.length) return '<div class="canopy-workstream-empty">No artifacts attached yet.</div>';
        return `<div class="canopy-workstream-artifacts">${rows.map((a) => {
            const href = artifactHref(a);
            const title = a.title || a.ref_id || 'Artifact';
            const target = /^https?:\/\//i.test(href) || href.startsWith('/file-ref/') ? ' target="_blank" rel="noopener noreferrer"' : '';
            return `<a class="canopy-workstream-artifact" href="${escapeHtml(href)}"${target}>
                <i class="bi ${a.artifact_type === 'digestion' ? 'bi-diagram-3' : a.artifact_type === 'url' ? 'bi-link-45deg' : 'bi-file-earmark'}"></i>
                <span><strong>${escapeHtml(title)}</strong><em>${escapeHtml(a.artifact_type || 'artifact')} · ${escapeHtml(shortId(a.ref_id || ''))}</em></span>
            </a>`;
        }).join('')}</div>`;
    }

    function renderEvents(events) {
        const rows = Array.isArray(events) ? events : [];
        if (!rows.length) return '<div class="canopy-workstream-empty">No progress events yet.</div>';
        return `<div class="canopy-workstream-events">${rows.slice(0, 20).map((e) => {
            const actor = e.actor || { id: e.actor_user_id };
            const name = actor.display_name || actor.username || e.actor_user_id || 'Actor';
            const title = e.title || e.event_type || 'Update';
            const eventState = e.metadata?.event_state || e.status || '';
            return `<article class="canopy-workstream-event" data-type="${escapeHtml(e.event_type || '')}">
                <div class="canopy-workstream-event-avatar">${userAvatar(actor)}</div>
                <div class="canopy-workstream-event-body">
                    <div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(name)} · ${escapeHtml(shortDate(e.created_at || ''))}${eventState ? ` · ${escapeHtml(eventState)}` : ''}</span></div>
                    ${e.body ? `<p>${escapeHtml(e.body)}</p>` : ''}
                </div>
            </article>`;
        }).join('')}</div>`;
    }

    function renderWorkstream(payload) {
        const ws = payload?.workstream || payload || {};
        const title = ws.title || ws.id || 'Workstream';
        const status = ws.status || 'active';
        return `<div class="canopy-workstream-reader">
            <header class="canopy-workstream-reader-head">
                <div>
                    <div class="canopy-workstream-kicker"><i class="bi bi-kanban"></i> Workstream</div>
                    <h2>${escapeHtml(title)}</h2>
                    <p>${escapeHtml(ws.objective || ws.summary || 'Sustained human-agent work with linked evidence and artifacts.')}</p>
                </div>
                <div class="canopy-workstream-status-block">
                    <span class="canopy-workstream-status">${escapeHtml(status)}</span>
                    <span>${escapeHtml(ws.priority || 'normal')}</span>
                    <code>${escapeHtml(ws.id || '')}</code>
                </div>
            </header>
            ${ws.required_output ? `<section class="canopy-workstream-required"><strong>Required output</strong><span>${escapeHtml(ws.required_output)}</span></section>` : ''}
            <section class="canopy-workstream-grid">
                <div class="canopy-workstream-panel"><h3>Participants</h3>${renderParticipants(ws.participants)}</div>
                <div class="canopy-workstream-panel"><h3>Artifacts</h3>${renderArtifacts(ws.artifacts)}</div>
                <div class="canopy-workstream-panel canopy-workstream-panel-wide"><h3>Recent progress</h3>${renderEvents(ws.events)}</div>
            </section>
            <footer class="canopy-workstream-reader-foot">
                <button type="button" class="canopy-workstream-copy" data-copy-workstream-ref="${escapeHtml(ws.id || '')}"><i class="bi bi-clipboard"></i> Copy agent ref</button>
                ${payload?.agent_reference ? '<span>Agent reference endpoint is available for handoffs.</span>' : ''}
            </footer>
        </div>`;
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
            const headers = { 'X-Requested-With': 'XMLHttpRequest' };
            const token = csrfToken();
            if (token) headers['X-CSRFToken'] = token;
            const response = await fetch(`/api/v1/workstreams/${encodeURIComponent(id)}`, { headers });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.error || 'Unable to open Workstream');
            content.innerHTML = renderWorkstream(payload);
        } catch (error) {
            content.innerHTML = `<div class="canopy-workstream-error"><strong>Workstream unavailable</strong><span>${escapeHtml(error.message || error)}</span><em>${escapeHtml(id)}</em></div>`;
        }
    }

    function workstreamFetchHeaders() {
        const headers = { 'X-Requested-With': 'XMLHttpRequest' };
        const token = csrfToken();
        if (token) headers['X-CSRFToken'] = token;
        return headers;
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
                };
            })
            .catch((error) => ({ ok: false, id, error: error.message || 'Unable to check Workstream' }));
        workstreamPreviewCache.set(id, promise);
        return promise;
    }

    function applyPreviewToButton(button, preview) {
        if (!button || !preview) return;
        const label = button.querySelector('.canopy-workstream-ref-label');
        const meta = button.querySelector('.canopy-workstream-ref-meta');
        if (!preview.ok) {
            button.classList.add('is-unavailable');
            button.setAttribute('aria-disabled', 'true');
            button.title = `${preview.error}. ${preview.id}`;
            if (label) label.textContent = 'Private/unavailable Workstream';
            if (meta) meta.textContent = shortId(preview.id);
            return;
        }
        button.classList.remove('is-unavailable');
        button.removeAttribute('aria-disabled');
        button.title = `${preview.title} · ${preview.status}${preview.updated_at ? ` · ${shortDate(preview.updated_at)}` : ''}`;
        if (label && !button.dataset.explicitLabel) label.textContent = preview.title;
        if (meta) meta.textContent = `${preview.status}${preview.updated_at ? ` · ${shortDate(preview.updated_at)}` : ''}`;
    }

    function hydrateWorkstreamButton(button) {
        const id = button?.dataset?.workstreamId || '';
        if (!id || button.dataset.workstreamHydrating === '1') return;
        button.dataset.workstreamHydrating = '1';
        fetchWorkstreamPreview(id).then((preview) => applyPreviewToButton(button, preview));
    }

    function makeWorkstreamButton(id, label) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'canopy-workstream-ref';
        button.dataset.workstreamId = id;
        if (label) button.dataset.explicitLabel = '1';
        button.title = `Checking Workstream ${id}`;
        button.innerHTML = `<i class="bi bi-kanban"></i><span class="canopy-workstream-ref-text"><strong class="canopy-workstream-ref-label">${escapeHtml(label || 'Workstream ' + shortId(id))}</strong><em class="canopy-workstream-ref-meta">${escapeHtml(shortId(id))}</em></span>`;
        hydrateWorkstreamButton(button);
        return button;
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

    window.CanopyWorkstreams = { open: openWorkstream, scan };
})();
