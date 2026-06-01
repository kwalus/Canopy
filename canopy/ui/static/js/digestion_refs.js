(function () {
    'use strict';

    const DG_ID_TEXT_RE = /Dg[A-Za-z0-9_-]{6,}/;
    const DG_BRACKET_RE = /\[(?:digestion|digest|corpus)\s*[:=]\s*(Dg[A-Za-z0-9_-]{6,})(?:\s*\|\s*([^\]]{1,160}))?\]/gi;
    const DG_BARE_RE = /(^|[\s([{<>"'`])(?:digestion\s*[:=]\s*)?(Dg[A-Za-z0-9_-]{6,})(?=$|[\s)\]}>.,;:!?;"'`])/g;
    const SCAN_SELECTOR = '.rich-content, .post-content, .message-content, .message-text, [data-post-content="1"], [data-message-content="1"], .agent-run-capsule';
    const SKIP_SELECTOR = 'a,button,textarea,input,select,code,pre,script,style,.canopy-digestion-ref,.canopy-digestion-modal,.canopy-workstream-ref';
    const previewCache = new Map();

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

    function normalizeLabelText(value) {
        return String(value || '').replace(/\s+/g, ' ').trim();
    }

    function isFallbackDigestionLabel(value, id) {
        const label = normalizeLabelText(value);
        const cleanId = String(id || '').trim();
        if (!label) return true;
        if (cleanId && label === cleanId) return true;
        if (cleanId && label.toLowerCase() === `digestion ${cleanId}`.toLowerCase()) return true;
        const compact = label.replace(/\s+/g, '');
        if (/^Dg[A-Za-z0-9_-]{6,}$/.test(compact)) return true;
        if (/^Digestion\s+Dg[A-Za-z0-9_.…-]{6,}$/i.test(label)) return true;
        if (/^Digest(?:ion)?\s*[:=]?\s*Dg[A-Za-z0-9_.…-]{6,}$/i.test(label)) return true;
        return false;
    }

    function shortDate(value) {
        if (!value) return '';
        const date = new Date(value);
        if (Number.isNaN(date.getTime())) return String(value).slice(0, 16);
        return date.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    }

    function numberLabel(value) {
        const num = Number(value || 0);
        return Number.isFinite(num) ? num.toLocaleString() : '0';
    }

    function csrfToken() {
        return document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
    }

    function fetchHeaders() {
        const headers = { 'X-Requested-With': 'XMLHttpRequest' };
        const token = csrfToken();
        if (token) headers['X-CSRFToken'] = token;
        return headers;
    }

    function summarizeAccess(access) {
        const bits = [];
        if (access?.can_manage) bits.push('manage');
        if (access?.can_read_sources) bits.push('source metadata');
        if (access?.can_query) bits.push('query');
        return bits.length ? bits.join(' + ') : 'no live access';
    }

    function summaryFromPayload(id, payload) {
        const digestion = payload?.digestion || {};
        const stats = payload?.stats || digestion.stats || {};
        const access = payload?.access || digestion.access || {};
        return {
            ok: true,
            id,
            name: digestion.name || `Digestion ${shortId(id)}`,
            status: digestion.status || 'unknown',
            purpose: digestion.purpose || digestion.description || 'Reusable source-grounded corpus for humans and agents.',
            updated_at: digestion.updated_at || '',
            built_at: digestion.built_at || '',
            source_count: stats.source_count ?? stats.sources ?? 0,
            chunks: stats.chunks ?? 0,
            datapoints: stats.datapoint_count ?? 0,
            figures: stats.figures ?? 0,
            evidence: stats.evidence_record_count ?? 0,
            outputs: stats.outputs ?? 0,
            retrieval_ready: !!stats.retrieval_ready,
            needs_build: !!stats.needs_build,
            build_state: stats.build_state || '',
            access,
        };
    }

    function fetchDigestionPreview(id) {
        const clean = String(id || '').trim();
        if (!clean) return Promise.resolve(null);
        if (previewCache.has(clean)) return previewCache.get(clean);
        const promise = fetch(`/api/v1/digestions/${encodeURIComponent(clean)}?summary=1`, { headers: fetchHeaders() })
            .then(async (response) => {
                const payload = await response.json().catch(() => ({}));
                if (!response.ok) {
                    return { ok: false, id: clean, error: payload.error || 'Private or unavailable Digestion' };
                }
                return summaryFromPayload(clean, payload);
            })
            .catch((error) => ({ ok: false, id: clean, error: error.message || 'Unable to check Digestion' }));
        previewCache.set(clean, promise);
        return promise;
    }

    function metaText(preview) {
        if (!preview?.ok) return shortId(preview?.id || '');
        const core = [];
        const state = preview.retrieval_ready ? 'ready' : (preview.needs_build ? 'needs build' : (preview.status || 'unknown'));
        core.push(state);
        core.push(`${numberLabel(preview.source_count)} sources`);
        if (Number(preview.chunks || 0) > 0) core.push(`${numberLabel(preview.chunks)} chunks`);
        if (Number(preview.datapoints || 0) > 0) core.push(`${numberLabel(preview.datapoints)} datapoints`);
        if (preview.updated_at) core.push(shortDate(preview.updated_at));
        return core.filter(Boolean).join(' · ');
    }

    function applyPreviewToRef(ref, preview) {
        if (!ref || !preview) return;
        let label = ref.querySelector('[data-canopy-digestion-ref-label="1"]') || ref.querySelector('span');
        if (!label) {
            label = document.createElement('span');
            label.setAttribute('data-canopy-digestion-ref-label', '1');
            ref.appendChild(label);
        }
        let meta = ref.querySelector('[data-canopy-digestion-ref-meta="1"]');
        if (!meta) {
            meta = document.createElement('em');
            meta.setAttribute('data-canopy-digestion-ref-meta', '1');
            meta.className = 'canopy-digestion-ref-meta';
            const textWrap = label.closest('.canopy-digestion-ref-text');
            if (textWrap) textWrap.appendChild(meta);
            else ref.appendChild(meta);
        }
        if (!preview.ok) {
            ref.classList.add('is-unavailable');
            ref.title = `${preview.error || 'Private or unavailable Digestion'} · ${preview.id || ''}`;
            if (!ref.dataset.explicitLabel) label.textContent = 'Private/unavailable Digestion';
            meta.textContent = shortId(preview.id || ref.dataset.canopyDigestionId || '');
            return;
        }
        ref.classList.remove('is-unavailable');
        ref.dataset.canopyDigestionName = preview.name || '';
        ref.dataset.canopyDigestionStatus = preview.status || '';
        ref.dataset.canopyDigestionHydrated = '1';
        ref.title = `${preview.name} · ${metaText(preview)} · ${summarizeAccess(preview.access)}`;
        if (!ref.dataset.explicitLabel || ref.dataset.canopyDigestionLabelFallback === '1' || isFallbackDigestionLabel(label.textContent, preview.id)) {
            label.textContent = preview.name;
            ref.dataset.canopyDigestionLabelFallback = '0';
        }
        meta.textContent = metaText(preview);
    }

    function normalizeExistingRef(ref) {
        if (!(ref instanceof HTMLElement)) return;
        const id = String(ref.dataset.canopyDigestionId || ref.getAttribute('data-canopy-digestion-id') || '').trim();
        if (!id) return;
        ref.classList.add('canopy-digestion-ref');
        ref.setAttribute('data-canopy-digestion-ref', '1');
        if (!ref.getAttribute('href') && ref.tagName.toLowerCase() === 'a') {
            ref.setAttribute('href', `/vault?digestion=${encodeURIComponent(id)}`);
        }
        if (!ref.querySelector('[data-canopy-digestion-ref-label="1"]')) {
            const span = ref.querySelector('span');
            if (span) span.setAttribute('data-canopy-digestion-ref-label', '1');
        }
        const label = ref.querySelector('[data-canopy-digestion-ref-label="1"]');
        if (label && !label.closest('.canopy-digestion-ref-text')) {
            const wrap = document.createElement('span');
            wrap.className = 'canopy-digestion-ref-text';
            label.parentNode.insertBefore(wrap, label);
            wrap.appendChild(label);
        }
        if (label && isFallbackDigestionLabel(label.textContent, id)) {
            ref.dataset.canopyDigestionLabelFallback = '1';
        }
        if (ref.dataset.canopyDigestionHydrating === '1' || ref.dataset.canopyDigestionHydrated === '1') return;
        ref.dataset.canopyDigestionHydrating = '1';
        fetchDigestionPreview(id).then((preview) => applyPreviewToRef(ref, preview));
    }

    function makeDigestionButton(id, label) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'canopy-digestion-ref';
        button.dataset.canopyDigestionId = id;
        button.dataset.canopyDigestionRef = '1';
        if (label) button.dataset.explicitLabel = '1';
        button.dataset.canopyDigestionLabelFallback = isFallbackDigestionLabel(label, id) ? '1' : '0';
        button.title = `Checking Digestion ${id}`;
        button.innerHTML = `<i class="bi bi-diagram-3" aria-hidden="true"></i><span class="canopy-digestion-ref-text"><strong data-canopy-digestion-ref-label="1">${escapeHtml(label || `Digestion ${shortId(id)}`)}</strong><em class="canopy-digestion-ref-meta" data-canopy-digestion-ref-meta="1">${escapeHtml(shortId(id))}</em></span>`;
        normalizeExistingRef(button);
        return button;
    }

    function linkifyTextNode(node) {
        const text = node.nodeValue || '';
        if (!text || !DG_ID_TEXT_RE.test(text)) return;
        const matches = [];
        DG_BRACKET_RE.lastIndex = 0;
        text.replace(DG_BRACKET_RE, (match, id, label, offset) => {
            matches.push({ start: offset, end: offset + match.length, id, label: String(label || '').trim() });
            return match;
        });
        DG_BARE_RE.lastIndex = 0;
        text.replace(DG_BARE_RE, (match, prefix, id, offset) => {
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
            if (match.start > last) frag.appendChild(document.createTextNode(text.slice(last, match.start)));
            frag.appendChild(makeDigestionButton(match.id, match.label));
            last = match.end;
        }
        if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
        node.parentNode.replaceChild(frag, node);
    }

    function renderReader(payload) {
        const preview = summaryFromPayload(payload?.digestion_id || payload?.digestion?.id || '', payload);
        const access = preview.access || {};
        const stats = [
            ['Sources', preview.source_count],
            ['Chunks', preview.chunks],
            ['Datapoints', preview.datapoints],
            ['Figures', preview.figures],
            ['Evidence', preview.evidence],
            ['Outputs', preview.outputs],
        ];
        return `<div class="canopy-digestion-reader">
            <header class="canopy-digestion-reader-head">
                <div>
                    <div class="canopy-digestion-kicker"><i class="bi bi-diagram-3"></i> Digestion</div>
                    <h2>${escapeHtml(preview.name)}</h2>
                    <p>${escapeHtml(preview.purpose)}</p>
                </div>
                <div class="canopy-digestion-status-block">
                    <span>${escapeHtml(preview.retrieval_ready ? 'ready' : (preview.build_state || preview.status || 'unknown'))}</span>
                    <code>${escapeHtml(preview.id)}</code>
                </div>
            </header>
            <section class="canopy-digestion-stats">${stats.map(([label, value]) => `<span><strong>${numberLabel(value)}</strong><em>${escapeHtml(label)}</em></span>`).join('')}</section>
            <section class="canopy-digestion-access">
                <strong>Current access</strong>
                <span>${access.can_manage ? '<i class="bi bi-check-circle"></i> Manage/build' : '<i class="bi bi-dash-circle"></i> No manage'}</span>
                <span>${access.can_query ? '<i class="bi bi-check-circle"></i> Query live index' : '<i class="bi bi-dash-circle"></i> No query'}</span>
                <span>${access.can_read_sources ? '<i class="bi bi-check-circle"></i> Source metadata' : '<i class="bi bi-dash-circle"></i> No source metadata'}</span>
            </section>
            <footer class="canopy-digestion-reader-foot">
                <a class="canopy-digestion-action" href="/vault?digestion=${encodeURIComponent(preview.id)}"><i class="bi bi-box-arrow-up-right"></i> Open in Vault</a>
                <button type="button" class="canopy-digestion-action" data-copy-digestion-ref="${escapeHtml(preview.id)}" data-copy-digestion-name="${escapeHtml(preview.name)}"><i class="bi bi-clipboard"></i> Copy smart ref</button>
            </footer>
        </div>`;
    }

    function ensureModal() {
        let modal = document.querySelector('.canopy-digestion-modal');
        if (modal) return modal;
        modal = document.createElement('div');
        modal.className = 'canopy-digestion-modal';
        modal.innerHTML = `<div class="canopy-digestion-modal-backdrop" data-close-digestion="1"></div>
            <div class="canopy-digestion-modal-card" role="dialog" aria-modal="true" aria-label="Digestion reader">
                <button type="button" class="canopy-digestion-modal-close" data-close-digestion="1" aria-label="Close Digestion reader"><i class="bi bi-x-lg"></i></button>
                <div class="canopy-digestion-modal-content"><div class="canopy-digestion-loading">Loading Digestion…</div></div>
            </div>`;
        document.body.appendChild(modal);
        modal.addEventListener('click', (event) => {
            if (event.target.closest('[data-close-digestion="1"]')) closeModal();
            const copyBtn = event.target.closest('[data-copy-digestion-ref]');
            if (copyBtn) {
                const id = copyBtn.getAttribute('data-copy-digestion-ref') || '';
                const name = copyBtn.getAttribute('data-copy-digestion-name') || '';
                const text = id ? `[digestion:${id}${name ? `|${name}` : ''}]` : '';
                if (text && navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
            }
        });
        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape' && modal.classList.contains('is-open')) closeModal();
        });
        return modal;
    }

    function closeModal() {
        document.querySelector('.canopy-digestion-modal')?.classList.remove('is-open');
    }

    async function openDigestion(id) {
        const clean = String(id || '').trim();
        if (!clean) return;
        const modal = ensureModal();
        const content = modal.querySelector('.canopy-digestion-modal-content');
        modal.classList.add('is-open');
        content.innerHTML = '<div class="canopy-digestion-loading">Loading Digestion…</div>';
        try {
            const response = await fetch(`/api/v1/digestions/${encodeURIComponent(clean)}?summary=1`, { headers: fetchHeaders() });
            const payload = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(payload.error || 'Unable to open Digestion');
            content.innerHTML = renderReader(payload);
        } catch (error) {
            content.innerHTML = `<div class="canopy-digestion-error"><strong>Digestion unavailable</strong><span>${escapeHtml(error.message || error)}</span><em>${escapeHtml(clean)}</em></div>`;
        }
    }

    function scan(root) {
        if (!root) return;
        const scope = root.nodeType === Node.ELEMENT_NODE ? root : document;
        const refs = [];
        if (scope.matches?.('.canopy-digestion-ref[data-canopy-digestion-id], [data-canopy-digestion-ref="1"][data-canopy-digestion-id]')) refs.push(scope);
        scope.querySelectorAll?.('.canopy-digestion-ref[data-canopy-digestion-id], [data-canopy-digestion-ref="1"][data-canopy-digestion-id]').forEach((el) => refs.push(el));
        refs.forEach(normalizeExistingRef);

        const roots = [];
        if (scope.matches?.(SCAN_SELECTOR)) roots.push(scope);
        scope.querySelectorAll?.(SCAN_SELECTOR).forEach((el) => roots.push(el));
        roots.forEach((el) => {
            if (el.dataset.digestionLinkified === '1') return;
            if (!DG_ID_TEXT_RE.test(el.textContent || '')) return;
            const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
                acceptNode(node) {
                    const parent = node.parentElement;
                    if (!parent || parent.closest(SKIP_SELECTOR)) return NodeFilter.FILTER_REJECT;
                    return DG_ID_TEXT_RE.test(node.nodeValue || '') ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
                }
            });
            const nodes = [];
            while (walker.nextNode()) nodes.push(walker.currentNode);
            nodes.forEach(linkifyTextNode);
            el.dataset.digestionLinkified = '1';
        });
    }

    document.addEventListener('click', (event) => {
        const ref = event.target.closest?.('.canopy-digestion-ref[data-canopy-digestion-id]');
        if (!ref) return;
        const isModified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button === 1;
        if (isModified || ref.getAttribute('data-canopy-open-target') === 'blank') return;
        event.preventDefault();
        if (ref.classList.contains('is-unavailable')) return;
        openDigestion(ref.getAttribute('data-canopy-digestion-id') || '');
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

    window.CanopyDigestionRefs = { open: openDigestion, scan, hydrate: normalizeExistingRef };
})();
