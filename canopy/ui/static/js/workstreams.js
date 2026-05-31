(function () {
    'use strict';

    const WS_ID_RE = /(^|[\s([{<>'"`])(?:workstream\s*[:=]\s*)?(Ws[A-Fa-f0-9]{12,})(?=$|[\s)\]}>.,;:!?;'"`])/g;
    const SCAN_SELECTOR = '.rich-content, .post-content, .message-content, .message-text, [data-post-content="1"], [data-message-content="1"]';
    const SKIP_SELECTOR = 'a,button,textarea,input,select,code,pre,script,style,.canopy-workstream-ref,.canopy-workstream-modal';

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
            return `<article class="canopy-workstream-event" data-type="${escapeHtml(e.event_type || '')}">
                <div class="canopy-workstream-event-avatar">${userAvatar(actor)}</div>
                <div class="canopy-workstream-event-body">
                    <div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(name)} · ${escapeHtml(e.created_at || '')}</span></div>
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
            content.innerHTML = `<div class="canopy-workstream-error"><strong>Workstream unavailable</strong><span>${escapeHtml(error.message || error)}</span></div>`;
        }
    }

    function linkifyTextNode(node) {
        const text = node.nodeValue || '';
        if (!text || !/Ws[A-Fa-f0-9]{12,}/.test(text)) return;
        WS_ID_RE.lastIndex = 0;
        if (!WS_ID_RE.test(text)) return;
        WS_ID_RE.lastIndex = 0;
        const frag = document.createDocumentFragment();
        let last = 0;
        text.replace(WS_ID_RE, (match, prefix, id, offset) => {
            const prefixLen = prefix ? prefix.length : 0;
            const start = offset + prefixLen;
            if (start > last) frag.appendChild(document.createTextNode(text.slice(last, start)));
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'canopy-workstream-ref';
            button.dataset.workstreamId = id;
            button.title = `Open Workstream ${id}`;
            button.innerHTML = `<i class="bi bi-kanban"></i><span>${escapeHtml('Workstream ' + shortId(id))}</span>`;
            frag.appendChild(button);
            last = offset + match.length;
            return match;
        });
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
            if (!/Ws[A-Fa-f0-9]{12,}/.test(el.textContent || '')) return;
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
                acceptNode(node) {
                    const parent = node.parentElement;
                    if (!parent || parent.closest(SKIP_SELECTOR)) return NodeFilter.FILTER_REJECT;
                    return /Ws[A-Fa-f0-9]{12,}/.test(node.nodeValue || '') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
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
