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

        function formatTimestamps(scope) {
            const root = scope && typeof scope.querySelectorAll === 'function' ? scope : document;
            root.querySelectorAll('[data-timestamp]').forEach(el => {
                const timestamp = el.getAttribute('data-timestamp');
                el.textContent = formatTimestamp(timestamp);
                const parsed = parseCanopyTimestamp(timestamp);
                if (parsed) {
                    el.title = parsed.toLocaleString();
                }
            });
        }

        window.parseCanopyTimestamp = parseCanopyTimestamp;
        window.formatCanopyTimestamp = formatTimestamp;
        window.formatTimestamps = formatTimestamps;
        
        function showAlert(message, type = 'info') {
            const alertDiv = document.createElement('div');
            alertDiv.className = `alert alert-${type} alert-dismissible fade show`;
            alertDiv.innerHTML = `
                ${message}
                <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
            `;
            
            const container = document.querySelector('.flash-messages');
            if (!container) return;
            container.appendChild(alertDiv);
            
            // Auto-dismiss after 5 seconds
            setTimeout(() => {
                if (alertDiv.parentNode) {
                    alertDiv.remove();
                }
            }, 5000);
	        }

        function canopyEscapeHtml(value) {
            const div = document.createElement('div');
            div.textContent = value == null ? '' : String(value);
            return div.innerHTML;
        }

        (function initCanopyDraftStore(global) {
            const PREFIX = 'canopy.composer_draft.v1';
            const MAX_DRAFT_LENGTH = 20000;
            const DRAFT_TTL_MS = 14 * 24 * 60 * 60 * 1000;

            function storageAvailable() {
                try {
                    return !!global.localStorage;
                } catch (_) {
                    return false;
                }
            }

            function normalizePart(value) {
                return encodeURIComponent(String(value == null || value === '' ? 'none' : value));
            }

            function key(parts) {
                const cleanParts = Array.isArray(parts) ? parts : [parts];
                return `${PREFIX}.${cleanParts.map(normalizePart).join('.')}`;
            }

            function readByKey(storageKey) {
                if (!storageAvailable() || !storageKey) return '';
                try {
                    const raw = global.localStorage.getItem(storageKey);
                    if (!raw) return '';
                    const payload = JSON.parse(raw);
                    const updatedAt = Number(payload && payload.updatedAt || 0);
                    if (updatedAt && Date.now() - updatedAt > DRAFT_TTL_MS) {
                        global.localStorage.removeItem(storageKey);
                        return '';
                    }
                    return String((payload && payload.value) || '');
                } catch (_) {
                    return '';
                }
            }

            function writeByKey(storageKey, value) {
                if (!storageAvailable() || !storageKey) return false;
                const text = String(value || '').slice(0, MAX_DRAFT_LENGTH);
                try {
                    if (!text.trim()) {
                        global.localStorage.removeItem(storageKey);
                    } else {
                        global.localStorage.setItem(storageKey, JSON.stringify({
                            value: text,
                            updatedAt: Date.now()
                        }));
                    }
                    return true;
                } catch (_) {
                    return false;
                }
            }

            function clearByKey(storageKey) {
                if (!storageAvailable() || !storageKey) return false;
                try {
                    global.localStorage.removeItem(storageKey);
                    return true;
                } catch (_) {
                    return false;
                }
            }

            function bindTextarea(textarea, keyParts, options = {}) {
                if (!textarea) return null;
                const storageKey = typeof keyParts === 'function'
                    ? () => key(keyParts())
                    : () => key(keyParts);
                const delay = Number(options.delayMs || 180);
                let timer = null;

                function currentKey() {
                    try {
                        return storageKey();
                    } catch (_) {
                        return '';
                    }
                }

                function save() {
                    const activeKey = currentKey();
                    if (!activeKey) return false;
                    return writeByKey(activeKey, textarea.value || '');
                }

                function clear() {
                    const activeKey = currentKey();
                    if (!activeKey) return false;
                    return clearByKey(activeKey);
                }

                function restore({ replace = false } = {}) {
                    const activeKey = currentKey();
                    const draft = readByKey(activeKey);
                    if (!draft || (!replace && String(textarea.value || '').trim())) {
                        return false;
                    }
                    textarea.value = draft;
                    textarea.dispatchEvent(new Event('input', { bubbles: true }));
                    if (typeof options.onRestore === 'function') {
                        options.onRestore(textarea, draft);
                    }
                    return true;
                }

                textarea.addEventListener('input', () => {
                    const activeKey = currentKey();
                    const draftValue = textarea.value || '';
                    clearTimeout(timer);
                    timer = global.setTimeout(() => {
                        if (!activeKey) return false;
                        return writeByKey(activeKey, draftValue);
                    }, delay);
                });
                global.addEventListener('pagehide', save);

                if (options.restore !== false) {
                    restore({ replace: !!options.replace });
                }

                return { save, clear, restore, key: currentKey };
            }

            global.CanopyDraftStore = {
                key,
                read: (parts) => readByKey(key(parts)),
                write: (parts, value) => writeByKey(key(parts), value),
                clear: (parts) => clearByKey(key(parts)),
                readKey: readByKey,
                writeKey: writeByKey,
                clearKey: clearByKey,
                bindTextarea,
            };
        })(window);

        (function initCanopyVault(global) {
            const DEFAULT_LIMIT = 80;
            const CATEGORY_LABELS = {
                images: 'Images',
                videos: 'Video',
                video: 'Video',
                audio: 'Audio',
                documents: 'Docs',
                document: 'Docs',
                other: 'Other'
            };
            const VAULT_VIEW_STORAGE_KEY = 'canopy:vault:viewMode';
            const VAULT_VIEW_MODES = new Set(['list', 'icons', 'preview']);
            const VAULT_FILE_DRAG_TYPE = 'application/x-canopy-vault-file-id';

            function vaultUrls() {
                const urls = (global.CANOPY_VARS && global.CANOPY_VARS.urls) || {};
	                return {
	                    files: urls.vaultFiles || '/ajax/vault/files',
	                    upload: urls.vaultUpload || '/ajax/vault/upload',
	                    folders: urls.vaultFolders || '/ajax/vault/folders',
	                    saveAttachment: urls.vaultSaveAttachment || '/ajax/vault/save_attachment',
	                    digestions: urls.digestions || '/ajax/digestions',
	                    page: urls.vault || '/vault'
	                };
	            }

            function vaultEscape(value) {
                return typeof canopyEscapeHtml === 'function' ? canopyEscapeHtml(value) : String(value == null ? '' : value)
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#39;');
            }

            function formatBytes(bytes) {
                const value = Number(bytes || 0);
                if (!Number.isFinite(value) || value <= 0) return '0 B';
                const units = ['B', 'KB', 'MB', 'GB', 'TB'];
                let size = value;
                let unitIndex = 0;
                while (size >= 1024 && unitIndex < units.length - 1) {
                    size /= 1024;
                    unitIndex += 1;
                }
                const precision = size >= 10 || unitIndex === 0 ? 0 : 1;
                return `${size.toFixed(precision)} ${units[unitIndex]}`;
            }

            function fileIcon(file) {
                const type = String((file && (file.type || file.content_type)) || '').toLowerCase();
                const name = String((file && (file.name || file.filename)) || '').toLowerCase();
                if (type.startsWith('image/')) return 'bi-file-earmark-image';
                if (type.startsWith('video/')) return 'bi-file-earmark-play';
                if (type.startsWith('audio/')) return 'bi-file-earmark-music';
                if (type.includes('pdf') || name.endsWith('.pdf')) return 'bi-file-earmark-pdf';
                if (type.includes('spreadsheet') || type.includes('excel') || /\.(csv|tsv|xls|xlsx|xlsm|xlsb|ods|numbers)$/.test(name)) return 'bi-file-earmark-spreadsheet';
                if (type.includes('presentation') || type.includes('powerpoint') || /\.(ppt|pptx|pptm|pps|ppsx|pot|potx|odp|key)$/.test(name)) return 'bi-file-earmark-slides';
                if (type.includes('word') || /\.(doc|docx|docm|odt|rtf|pages)$/.test(name)) return 'bi-file-earmark-richtext';
                if (/\.(zip|tar|gz|tgz|bz2|xz|7z|rar)$/.test(name)) return 'bi-file-earmark-zip';
                if (/\.(py|js|ts|json|xml|html|css|sh|toml|yaml|yml|md|tex)$/.test(name)) return 'bi-file-earmark-code';
                return 'bi-file-earmark';
            }

	            function normalizedAttachment(file) {
	                const source = (file && file.attachment) ? file.attachment : (file || {});
	                const id = String(source.id || source.file_id || file?.id || '').trim();
                if (!id) return null;
                return {
                    id,
                    vault_file_id: id,
                    name: source.name || source.filename || file?.name || file?.filename || id,
                    type: source.type || source.content_type || file?.type || file?.content_type || 'application/octet-stream',
                    size: Number(source.size || file?.size || 0) || 0,
                    url: source.url || file?.url || `/files/${encodeURIComponent(id)}`,
	                    source: 'vault'
	                };
	            }

	            function normalizedAttachmentForVaultSave(attachment) {
	                if (!attachment) return null;
	                if (typeof attachment === 'string') {
	                    const raw = attachment.trim();
	                    return raw ? { url: raw } : null;
	                }
	                if (typeof attachment !== 'object') return null;
	                const payload = { ...attachment };
	                const id = String(payload.id || payload.file_id || payload.vault_file_id || '').trim();
	                if (id) {
	                    payload.id = payload.id || id;
	                    payload.file_id = payload.file_id || id;
	                }
	                const hasLocalRef = !!(payload.id || payload.file_id || payload.vault_file_id || payload.url);
	                const hasRemoteRef = !!(
	                    (payload.origin_file_id || payload.remote_file_id)
	                    && (payload.source_peer_id || payload.origin_peer)
	                );
	                if (!hasLocalRef && !hasRemoteRef) return null;
	                if (!payload.source_peer_id && payload.origin_peer) payload.source_peer_id = payload.origin_peer;
	                return payload;
	            }

	            async function saveAttachmentToVault(attachment, triggerEl) {
	                const payload = normalizedAttachmentForVaultSave(attachment);
	                if (!payload) {
	                    if (typeof showAlert === 'function') showAlert('Attachment metadata is incomplete.', 'warning');
	                    return false;
	                }
	                const button = triggerEl instanceof HTMLElement ? triggerEl : null;
	                const originalHtml = button ? button.innerHTML : '';
	                const originalTitle = button ? button.getAttribute('title') : '';
	                if (button) {
	                    button.disabled = true;
	                    button.setAttribute('aria-busy', 'true');
	                    button.innerHTML = '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>';
	                }
	                try {
	                    const data = await apiCall(vaultUrls().saveAttachment, {
	                        method: 'POST',
	                        body: JSON.stringify({ attachment: payload }),
	                    });
	                    const message = String(data.message || '').trim();
		                    if (typeof showAlert === 'function') {
		                        if (data.queued) {
		                            showAlert(message || 'Attachment fetch queued. Canopy will save it to your Vault when the local copy arrives.', 'info');
		                        } else if (data.already_saved) {
	                            showAlert(message || 'This attachment is already in your File Vault.', 'success');
	                        } else {
	                            showAlert(message || 'Attachment saved to your File Vault.', 'success');
	                        }
	                    }
	                    try {
	                        global.dispatchEvent(new CustomEvent('canopy:vault-file-saved', { detail: data }));
	                    } catch (_) {}
	                    return true;
	                } catch (error) {
	                    const message = (error && (error.error || error.message)) || 'Could not save attachment to Vault.';
	                    if (typeof showAlert === 'function') showAlert(message, 'danger');
	                    return false;
	                } finally {
	                    if (button) {
	                        button.disabled = false;
	                        button.removeAttribute('aria-busy');
	                        button.innerHTML = originalHtml;
	                        if (originalTitle) button.setAttribute('title', originalTitle);
	                    }
	                }
	            }

            function renderVaultPreview(file) {
                const type = String((file && (file.type || file.content_type)) || '').toLowerCase();
                const thumb = file && (file.thumb_url || (type.startsWith('image/') ? file.url : ''));
                if (thumb && type.startsWith('image/')) {
                    return `<img src="${vaultEscape(thumb)}" alt="${vaultEscape(file.name || 'File preview')}" loading="lazy">`;
                }
                return `<i class="bi ${fileIcon(file)}"></i>`;
            }

            async function copyText(text, label) {
                try {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        await navigator.clipboard.writeText(text);
                    } else {
                        const ta = document.createElement('textarea');
                        ta.value = text;
                        ta.style.position = 'fixed';
                        ta.style.opacity = '0';
                        document.body.appendChild(ta);
                        ta.select();
                        document.execCommand('copy');
                        ta.remove();
                    }
                    if (typeof showAlert === 'function') showAlert(`${label || 'Text'} copied.`, 'success');
                    return true;
                } catch (_) {
                    if (typeof showAlert === 'function') showAlert('Could not copy to clipboard.', 'warning');
                    return false;
                }
            }

            async function fetchVaultFiles({ q = '', category = '', offset = 0, limit = DEFAULT_LIMIT, folderId = null } = {}) {
                const urls = vaultUrls();
                const params = new URLSearchParams();
                params.set('limit', String(limit));
                params.set('offset', String(offset));
                if (q) params.set('q', q);
                if (category) params.set('category', category);
                if (folderId !== null && folderId !== undefined) params.set('folder_id', String(folderId || ''));
                return apiCall(`${urls.files}?${params.toString()}`);
            }

            function ensureVaultStyle() {
                if (document.getElementById('canopy-vault-picker-style')) return;
                const style = document.createElement('style');
                style.id = 'canopy-vault-picker-style';
                style.textContent = `
                    .canopy-vault-picker .modal-content {
                        border: 1px solid var(--canopy-border);
                        border-radius: 24px;
                        background:
                            radial-gradient(circle at 12% 8%, color-mix(in srgb, var(--canopy-primary) 16%, transparent), transparent 34%),
                            linear-gradient(135deg, color-mix(in srgb, var(--canopy-bg-card) 96%, transparent), var(--canopy-bg-secondary));
                        color: var(--canopy-text-primary);
                        box-shadow: 0 28px 70px rgba(0,0,0,0.38);
                    }
                    .canopy-vault-picker .modal-header,
                    .canopy-vault-picker .modal-footer {
                        border-color: var(--canopy-border);
                    }
                    .canopy-vault-picker-search {
                        display: grid;
                        grid-template-columns: minmax(0, 1fr) auto;
                        gap: 0.6rem;
                        align-items: center;
                        margin-bottom: 0.75rem;
                    }
                    .canopy-vault-picker-search .form-control {
                        border-radius: 14px;
                        background: color-mix(in srgb, var(--canopy-bg-secondary) 86%, transparent);
                        border-color: var(--canopy-border);
                        color: var(--canopy-text-primary);
                    }
                    .canopy-vault-picker-filters {
                        display: flex;
                        gap: 0.4rem;
                        flex-wrap: wrap;
                        margin-bottom: 0.75rem;
                    }
                    .canopy-vault-picker-filter {
                        border: 1px solid var(--canopy-border);
                        border-radius: 999px;
                        background: color-mix(in srgb, var(--canopy-bg-tertiary) 82%, transparent);
                        color: var(--canopy-text-secondary);
                        padding: 0.35rem 0.65rem;
                        font-weight: 750;
                        font-size: 0.8rem;
                    }
                    .canopy-vault-picker-filter.active {
                        background: var(--canopy-gradient-primary);
                        color: #fff;
                        border-color: transparent;
                    }
                    .canopy-vault-picker-path {
                        display: flex;
                        flex-wrap: wrap;
                        align-items: center;
                        gap: 0.32rem;
                        margin-bottom: 0.75rem;
                    }
                    .canopy-vault-picker-crumb {
                        border: 1px solid transparent;
                        border-radius: 999px;
                        background: transparent;
                        color: var(--canopy-text-secondary);
                        padding: 0.28rem 0.5rem;
                        font-size: 0.82rem;
                        font-weight: 750;
                        max-width: 190px;
                        overflow: hidden;
                        text-overflow: ellipsis;
                        white-space: nowrap;
                    }
                    .canopy-vault-picker-crumb:hover,
                    .canopy-vault-picker-crumb.active {
                        color: var(--canopy-text-primary);
                        border-color: var(--canopy-border);
                        background: color-mix(in srgb, var(--canopy-bg-tertiary) 78%, transparent);
                    }
                    .canopy-vault-picker-grid {
                        display: grid;
                        grid-template-columns: repeat(auto-fill, minmax(188px, 1fr));
                        gap: 0.65rem;
                        max-height: min(56vh, 560px);
                        overflow: auto;
                        padding-right: 0.2rem;
                    }
                    .canopy-vault-picker-file {
                        display: grid;
                        grid-template-columns: 52px minmax(0, 1fr);
                        gap: 0.62rem;
                        align-items: center;
                        width: 100%;
                        min-width: 0;
                        text-align: left;
                        border: 1px solid var(--canopy-border);
                        border-radius: 16px;
                        padding: 0.58rem;
                        background: color-mix(in srgb, var(--canopy-bg-card) 88%, transparent);
                        color: var(--canopy-text-primary);
                    }
                    .canopy-vault-picker-file.is-folder {
                        border-style: dashed;
                    }
                    .canopy-vault-picker-file:hover,
                    .canopy-vault-picker-file.is-selected {
                        border-color: color-mix(in srgb, var(--canopy-primary) 70%, var(--canopy-border));
                        background: color-mix(in srgb, var(--canopy-primary) 14%, var(--canopy-bg-card));
                    }
                    .canopy-vault-picker-thumb {
                        width: 52px;
                        height: 52px;
                        border-radius: 13px;
                        display: flex;
                        align-items: center;
                        justify-content: center;
                        overflow: hidden;
                        color: var(--canopy-secondary);
                        background: color-mix(in srgb, var(--canopy-bg-tertiary) 86%, transparent);
                        font-size: 1.4rem;
                    }
                    .canopy-vault-picker-thumb img {
                        width: 100%;
                        height: 100%;
                        object-fit: cover;
                    }
                    .canopy-vault-picker-name {
                        font-weight: 850;
                        line-height: 1.15;
                        overflow: hidden;
                        display: -webkit-box;
                        -webkit-line-clamp: 2;
                        -webkit-box-orient: vertical;
                    }
                    .canopy-vault-picker-meta {
                        color: var(--canopy-text-muted);
                        font-size: 0.76rem;
                        margin-top: 0.22rem;
                    }
                    .canopy-vault-picker-empty {
                        border: 1px dashed var(--canopy-border);
                        border-radius: 18px;
                        padding: 1.4rem;
                        text-align: center;
                        color: var(--canopy-text-secondary);
                    }
                    @media (max-width: 560px) {
                        .canopy-vault-picker-search {
                            grid-template-columns: 1fr;
                        }
                        .canopy-vault-picker-grid {
                            grid-template-columns: 1fr;
                        }
                    }
                `;
                document.head.appendChild(style);
            }

            function ensurePickerModal() {
                ensureVaultStyle();
                let modal = document.getElementById('canopy-vault-picker-modal');
                if (modal) return modal;
                modal = document.createElement('div');
                modal.className = 'modal fade canopy-vault-picker';
                modal.id = 'canopy-vault-picker-modal';
                modal.tabIndex = -1;
                modal.setAttribute('aria-labelledby', 'canopy-vault-picker-title');
                modal.setAttribute('aria-hidden', 'true');
                modal.innerHTML = `
                    <div class="modal-dialog modal-xl modal-dialog-centered modal-dialog-scrollable">
                        <div class="modal-content">
                            <div class="modal-header">
                                <div>
                                    <h5 class="modal-title" id="canopy-vault-picker-title">Attach from File Vault</h5>
                                    <div class="text-muted small" data-vault-picker-subtitle>Private files become shared only when this composer sends them.</div>
                                </div>
                                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                            </div>
                            <div class="modal-body">
                                <div class="canopy-vault-picker-search">
                                    <input class="form-control" type="search" data-vault-picker-search placeholder="Search your vault" aria-label="Search your File Vault">
                                    <a class="btn btn-outline-secondary" data-vault-picker-open-page href="${vaultEscape(vaultUrls().page)}">
                                        <i class="bi bi-folder2-open"></i> Open Vault
                                    </a>
                                </div>
                                <div class="canopy-vault-picker-filters" data-vault-picker-filters>
                                    <button type="button" class="canopy-vault-picker-filter active" data-vault-picker-category="">All</button>
                                    <button type="button" class="canopy-vault-picker-filter" data-vault-picker-category="documents">Docs</button>
                                    <button type="button" class="canopy-vault-picker-filter" data-vault-picker-category="images">Images</button>
                                    <button type="button" class="canopy-vault-picker-filter" data-vault-picker-category="video">Video</button>
                                    <button type="button" class="canopy-vault-picker-filter" data-vault-picker-category="audio">Audio</button>
                                    <button type="button" class="canopy-vault-picker-filter" data-vault-picker-category="other">Other</button>
                                </div>
                                <div class="canopy-vault-picker-path" data-vault-picker-path aria-label="Current Vault folder"></div>
                                <div class="canopy-vault-picker-grid" data-vault-picker-grid></div>
                                <div class="canopy-vault-picker-empty" data-vault-picker-empty hidden>
                                    <i class="bi bi-folder2-open d-block fs-2 mb-2"></i>
                                    <strong>No vault files found.</strong>
                                    <div>Open the Vault to upload files, then return to this composer.</div>
                                </div>
                            </div>
                            <div class="modal-footer d-flex justify-content-between flex-wrap gap-2">
                                <div class="text-muted small" data-vault-picker-count>0 selected</div>
                                <div class="d-flex gap-2">
                                    <button type="button" class="btn btn-outline-secondary" data-bs-dismiss="modal">Cancel</button>
                                    <button type="button" class="btn btn-primary" data-vault-picker-confirm disabled>
                                        <i class="bi bi-paperclip"></i> Attach
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                `;
                document.body.appendChild(modal);
                return modal;
            }

            const pickerState = {
                files: [],
                folders: [],
                path: [],
                selected: new Map(),
                onSelect: null,
                multiple: true,
                query: '',
                category: '',
                currentFolderId: '',
                actionLabel: 'Attach',
                title: 'Attach from File Vault',
                loading: false,
                timer: null,
                requestSeq: 0
            };

            function selectedFilesArray() {
                return Array.from(pickerState.selected.values()).map(file => normalizedAttachment(file)).filter(Boolean);
            }

            function updatePickerSelectionUi(modal) {
                const count = pickerState.selected.size;
                const countEl = modal.querySelector('[data-vault-picker-count]');
                const confirm = modal.querySelector('[data-vault-picker-confirm]');
                if (countEl) countEl.textContent = count ? `${count} selected` : 'No files selected';
                if (confirm) {
                    confirm.disabled = count === 0;
                    confirm.innerHTML = `<i class="bi bi-paperclip"></i> ${vaultEscape(pickerState.actionLabel || 'Attach')}${count > 1 ? ` (${count})` : ''}`;
                }
                modal.querySelectorAll('[data-vault-picker-file]').forEach((btn) => {
                    const id = btn.getAttribute('data-vault-picker-file') || '';
                    btn.classList.toggle('is-selected', pickerState.selected.has(id));
                    btn.setAttribute('aria-pressed', pickerState.selected.has(id) ? 'true' : 'false');
                });
            }

            function renderPickerFiles(modal) {
                const grid = modal.querySelector('[data-vault-picker-grid]');
                const empty = modal.querySelector('[data-vault-picker-empty]');
                const pathEl = modal.querySelector('[data-vault-picker-path]');
                if (!grid || !empty) return;
                if (pathEl) {
                    const crumbs = [
                        `<button class="canopy-vault-picker-crumb${pickerState.currentFolderId ? '' : ' active'}" type="button" data-vault-picker-folder-target=""><i class="bi bi-house-door me-1"></i>Vault Home</button>`
                    ];
                    pickerState.path.forEach((folder) => {
                        const id = vaultEscape(folder.id || '');
                        const name = vaultEscape(folder.name || 'Folder');
                        const active = String(folder.id || '') === String(pickerState.currentFolderId || '') ? ' active' : '';
                        crumbs.push(`<span class="text-muted">/</span><button class="canopy-vault-picker-crumb${active}" type="button" data-vault-picker-folder-target="${id}">${name}</button>`);
                    });
                    pathEl.innerHTML = crumbs.join('');
                }
                if (!pickerState.files.length && !pickerState.folders.length) {
                    grid.innerHTML = '';
                    empty.hidden = false;
                    updatePickerSelectionUi(modal);
                    return;
                }
                empty.hidden = true;
                const folderHtml = pickerState.folders.map((folder) => {
                    const id = vaultEscape(folder.id || '');
                    const name = vaultEscape(folder.name || 'Folder');
                    return `
                        <button type="button" class="canopy-vault-picker-file is-folder" data-vault-picker-folder="${id}">
                            <span class="canopy-vault-picker-thumb"><i class="bi bi-folder-fill"></i></span>
                            <span class="min-w-0">
                                <span class="canopy-vault-picker-name">${name}</span>
                                <span class="canopy-vault-picker-meta">Folder · Open to choose files</span>
                            </span>
                        </button>
                    `;
                }).join('');
                const fileHtml = pickerState.files.map((file) => {
                    const id = vaultEscape(file.id || '');
                    const name = vaultEscape(file.name || file.filename || 'File');
                    const type = vaultEscape(CATEGORY_LABELS[file.category] || file.category || file.type || 'File');
                    const size = vaultEscape(formatBytes(file.size));
                    const selected = pickerState.selected.has(String(file.id || '')) ? ' is-selected' : '';
                    return `
                        <button type="button" class="canopy-vault-picker-file${selected}" data-vault-picker-file="${id}" aria-pressed="${selected ? 'true' : 'false'}">
                            <span class="canopy-vault-picker-thumb">${renderVaultPreview(file)}</span>
                            <span class="min-w-0">
                                <span class="canopy-vault-picker-name">${name}</span>
                                <span class="canopy-vault-picker-meta">${type} · ${size}</span>
                            </span>
                        </button>
                    `;
                }).join('');
                grid.innerHTML = folderHtml + fileHtml;
                updatePickerSelectionUi(modal);
            }

            async function navigatePickerFolder(modal, folderId) {
                pickerState.currentFolderId = String(folderId || '');
                await loadPickerFiles(modal);
            }

            async function loadPickerFiles(modal) {
                const requestSeq = pickerState.requestSeq + 1;
                pickerState.requestSeq = requestSeq;
                pickerState.loading = true;
                const query = pickerState.query;
                const category = pickerState.category;
                const folderId = pickerState.currentFolderId;
                const grid = modal.querySelector('[data-vault-picker-grid]');
                if (grid) {
                    grid.innerHTML = '<div class="canopy-vault-picker-empty"><span class="spinner-border spinner-border-sm me-2"></span>Loading vault files...</div>';
                }
                try {
                    const data = await fetchVaultFiles({
                        q: query,
                        category,
                        limit: DEFAULT_LIMIT,
                        folderId
                    });
                    if (requestSeq !== pickerState.requestSeq) return;
                    pickerState.files = Array.isArray(data.files) ? data.files : [];
                    pickerState.folders = Array.isArray(data.folders) ? data.folders : [];
                    pickerState.path = Array.isArray(data.path) ? data.path : [];
                    pickerState.currentFolderId = String(data.current_folder_id || folderId || '');
                    renderPickerFiles(modal);
                } catch (error) {
                    if (requestSeq !== pickerState.requestSeq) return;
                    console.error('Vault picker load failed:', error);
                    if (typeof showAlert === 'function') showAlert(error.error || 'Could not load File Vault.', 'danger');
                    pickerState.files = [];
                    pickerState.folders = [];
                    pickerState.path = [];
                    renderPickerFiles(modal);
                } finally {
                    if (requestSeq === pickerState.requestSeq) {
                        pickerState.loading = false;
                    }
                }
            }

            function bindPickerModal(modal) {
                if (modal.dataset.vaultPickerBound === '1') return;
                modal.dataset.vaultPickerBound = '1';
                const search = modal.querySelector('[data-vault-picker-search]');
                if (search) {
                    search.addEventListener('input', () => {
                        clearTimeout(pickerState.timer);
                        pickerState.timer = global.setTimeout(() => {
                            pickerState.query = search.value.trim();
                            loadPickerFiles(modal);
                        }, 180);
                    });
                }
                modal.addEventListener('click', (event) => {
                    const filter = event.target.closest('[data-vault-picker-category]');
                    if (filter && modal.contains(filter)) {
                        pickerState.category = filter.getAttribute('data-vault-picker-category') || '';
                        modal.querySelectorAll('[data-vault-picker-category]').forEach(btn => {
                            btn.classList.toggle('active', btn === filter);
                        });
                        loadPickerFiles(modal);
                        return;
                    }
                    const folderTarget = event.target.closest('[data-vault-picker-folder-target]');
                    if (folderTarget && modal.contains(folderTarget)) {
                        navigatePickerFolder(modal, folderTarget.getAttribute('data-vault-picker-folder-target') || '');
                        return;
                    }
                    const folder = event.target.closest('[data-vault-picker-folder]');
                    if (folder && modal.contains(folder)) {
                        navigatePickerFolder(modal, folder.getAttribute('data-vault-picker-folder') || '');
                        return;
                    }
                    const item = event.target.closest('[data-vault-picker-file]');
                    if (item && modal.contains(item)) {
                        const id = item.getAttribute('data-vault-picker-file') || '';
                        const file = pickerState.files.find(candidate => String(candidate.id || '') === id);
                        if (!file) return;
                        if (!pickerState.multiple) {
                            pickerState.selected.clear();
                            pickerState.selected.set(id, file);
                        } else if (pickerState.selected.has(id)) {
                            pickerState.selected.delete(id);
                        } else {
                            pickerState.selected.set(id, file);
                        }
                        updatePickerSelectionUi(modal);
                    }
                });
                const confirm = modal.querySelector('[data-vault-picker-confirm]');
                if (confirm) {
                    confirm.addEventListener('click', () => {
                        const selected = selectedFilesArray();
                        if (!selected.length) return;
                        if (typeof pickerState.onSelect === 'function') {
                            pickerState.onSelect(selected);
                        }
                        const instance = bootstrap.Modal.getOrCreateInstance(modal);
                        instance.hide();
                    });
                }
                modal.addEventListener('hidden.bs.modal', () => {
                    pickerState.selected.clear();
                    pickerState.onSelect = null;
                    updatePickerSelectionUi(modal);
                });
            }

            function openPicker(options = {}) {
                const modal = ensurePickerModal();
                bindPickerModal(modal);
                pickerState.files = [];
                pickerState.folders = [];
                pickerState.path = [];
                pickerState.selected.clear();
                pickerState.onSelect = typeof options.onSelect === 'function' ? options.onSelect : null;
                pickerState.multiple = options.multiple !== false;
                pickerState.query = '';
                pickerState.category = '';
                pickerState.currentFolderId = '';
                pickerState.actionLabel = options.actionLabel || 'Attach';
                pickerState.title = options.title || 'Attach from File Vault';
                const titleEl = modal.querySelector('#canopy-vault-picker-title');
                const search = modal.querySelector('[data-vault-picker-search]');
                if (titleEl) titleEl.textContent = pickerState.title;
                if (search) search.value = '';
                modal.querySelectorAll('[data-vault-picker-category]').forEach((btn) => {
                    btn.classList.toggle('active', !btn.getAttribute('data-vault-picker-category'));
                });
                updatePickerSelectionUi(modal);
                bootstrap.Modal.getOrCreateInstance(modal).show(options.anchor || null);
                loadPickerFiles(modal);
            }

            function renderVaultFolderCard(folder) {
                const id = vaultEscape(folder.id || '');
                const name = vaultEscape(folder.name || 'Folder');
                return `
                    <article class="vault-card vault-folder-card" data-vault-folder-id="${id}" data-vault-folder-drop="${id}" data-vault-open-folder="${id}" role="button" tabindex="0" aria-label="Open folder ${name}">
                        <span class="vault-select-spacer" aria-hidden="true"></span>
                        <button type="button" class="vault-card-preview vault-folder-preview border-0" data-vault-open-folder="${id}" aria-label="Open folder ${name}">
                            <i class="bi bi-folder-fill"></i>
                        </button>
                        <div class="vault-card-body">
                            <div class="vault-file-name">${name}</div>
                            <div class="vault-file-meta">
                                <span class="vault-file-pill"><i class="bi bi-folder"></i> Folder</span>
                                <span class="vault-file-pill">Private</span>
                                <span class="vault-drag-help"><i class="bi bi-arrows-move"></i> Drop files here</span>
                            </div>
                        </div>
                        <div class="vault-card-actions">
                            <button class="btn btn-sm btn-outline-secondary" type="button" data-vault-folder-action="rename" data-vault-folder-id="${id}">
                                <i class="bi bi-pencil"></i> Rename
                            </button>
                            <button class="btn btn-sm btn-outline-danger ms-auto" type="button" data-vault-folder-action="delete" data-vault-folder-id="${id}">
                                <i class="bi bi-trash"></i>
                            </button>
                        </div>
                    </article>
                `;
            }

            function renderVaultPageCard(file, selected = false) {
                const id = vaultEscape(file.id || '');
                const name = vaultEscape(file.name || file.filename || 'File');
                const type = vaultEscape(CATEGORY_LABELS[file.category] || file.category || file.type || 'File');
                const size = vaultEscape(formatBytes(file.size));
                const uploaded = vaultEscape(formatTimestamp(file.uploaded_at || ''));
                const url = vaultEscape(file.url || `/files/${encodeURIComponent(file.id || '')}`);
                const isInFolder = !!String(file.folder_id || '').trim();
                return `
                    <article class="vault-card${selected ? ' is-selected' : ''}" data-vault-file-id="${id}" data-vault-open-url="${url}" draggable="true" role="button" tabindex="0" aria-label="Open ${name}">
                        <label class="vault-select-wrap" title="Select ${name}" aria-label="Select ${name}">
                            <input class="vault-select-box" type="checkbox" data-vault-select-file="${id}" ${selected ? 'checked' : ''}>
                        </label>
                        <div class="vault-card-preview">${renderVaultPreview(file)}</div>
                        <div class="vault-card-body">
                            <div class="vault-file-name">${name}</div>
                            <div class="vault-file-meta">
                                <span class="vault-file-pill"><i class="bi ${fileIcon(file)}"></i>${type}</span>
                                <span class="vault-file-pill">${size}</span>
                                <span class="vault-file-pill">${uploaded || 'Recently'}</span>
                                <span class="vault-file-pill vault-file-location${file.folder_id ? ' is-visible' : ''}"><i class="bi bi-folder"></i> Organized</span>
                            </div>
                        </div>
                        <div class="vault-card-actions">
                            <button class="btn btn-sm btn-outline-secondary" type="button" data-vault-action="copy" data-vault-id="${id}">
                                <i class="bi bi-markdown"></i> <span>Copy link</span>
                            </button>
                            <a class="btn btn-sm btn-outline-secondary" href="${url}" target="_blank" rel="noopener">
                                <i class="bi bi-box-arrow-up-right"></i> <span>Open</span>
                            </a>
                            ${isInFolder ? `
                                <button class="btn btn-sm btn-outline-secondary" type="button" data-vault-action="move-root" data-vault-id="${id}">
                                    <i class="bi bi-house-door"></i> <span>Move Home</span>
                                </button>
                            ` : ''}
                            <button class="btn btn-sm btn-outline-danger ms-auto" type="button" data-vault-action="delete" data-vault-id="${id}">
                                <i class="bi bi-trash"></i>
                            </button>
                        </div>
                    </article>
                `;
            }

            function initVaultPage() {
                const page = document.querySelector('[data-vault-page]');
                if (!page) return;
                function readVaultViewMode() {
                    try {
                        const saved = String(global.localStorage && global.localStorage.getItem(VAULT_VIEW_STORAGE_KEY) || '').trim();
                        return VAULT_VIEW_MODES.has(saved) ? saved : 'preview';
                    } catch (_) {
                        return 'preview';
                    }
                }
                const state = {
                    files: Array.isArray(global.CANOPY_VAULT_INITIAL?.files) ? global.CANOPY_VAULT_INITIAL.files.slice() : [],
                    folders: Array.isArray(global.CANOPY_VAULT_INITIAL?.folders) ? global.CANOPY_VAULT_INITIAL.folders.slice() : [],
                    digestions: Array.isArray(global.CANOPY_VAULT_INITIAL?.digestions) ? global.CANOPY_VAULT_INITIAL.digestions.slice() : [],
                    path: Array.isArray(global.CANOPY_VAULT_INITIAL?.path) ? global.CANOPY_VAULT_INITIAL.path.slice() : [],
                    currentFolderId: String(global.CANOPY_VAULT_INITIAL?.currentFolderId || ''),
                    stats: global.CANOPY_VAULT_INITIAL?.stats || {},
                    query: '',
                    category: '',
                    offset: 0,
                    limit: 48,
                    loading: false,
                    timer: null,
                    viewMode: readVaultViewMode(),
                    selectedIds: new Set()
                };
                const grid = document.getElementById('vault-grid');
                const empty = document.getElementById('vault-empty');
                const search = document.getElementById('vault-search');
                const loadMore = document.getElementById('vault-load-more');
                const uploadInput = document.getElementById('vault-upload-input');
                const dropzone = document.getElementById('vault-dropzone');
                const breadcrumb = document.getElementById('vault-breadcrumb');
                const newFolderBtn = document.getElementById('vault-new-folder-btn');
                const newFolderForm = document.getElementById('vault-new-folder-form');
                const newFolderName = document.getElementById('vault-new-folder-name');
                const newFolderCancel = document.getElementById('vault-new-folder-cancel');
                const newFolderError = document.getElementById('vault-new-folder-error');
                const rootBtn = document.getElementById('vault-root-btn');
                const selectionBar = document.getElementById('vault-selection-bar');
                const selectionCount = document.getElementById('vault-selection-count');
                const selectionOpen = document.getElementById('vault-selection-open');
                const selectionCopy = document.getElementById('vault-selection-copy');
                const selectionDigest = document.getElementById('vault-selection-digest');
                const selectionClear = document.getElementById('vault-selection-clear');
                const digestionCreateForm = document.getElementById('vault-digestion-create-form');
                const digestionCreateName = document.getElementById('vault-digestion-create-name');
                const digestionCreatePurpose = document.getElementById('vault-digestion-create-purpose');
                const digestionCreateCancel = document.getElementById('vault-digestion-create-cancel');
                const digestionList = document.getElementById('vault-digestion-list');
                const digestionRefresh = document.getElementById('vault-digestion-refresh');

                function vaultFileUrl(file) {
                    return String(file && (file.url || `/files/${encodeURIComponent(file.id || '')}`) || '');
                }

                function openVaultFile(file) {
                    const url = vaultFileUrl(file);
                    if (!url) return;
                    const opened = global.open(url, '_blank');
                    if (opened) {
                        try { opened.opener = null; } catch (_) {}
                    }
                    if (!opened) {
                        global.location.href = url;
                    }
                }

                function selectedVaultFiles() {
                    return state.files.filter(file => state.selectedIds.has(String(file.id || '')));
                }

                function defaultDigestionName(selected) {
                    if (selected.length === 1) {
                        return `${selected[0].name || 'Vault file'} Digestion`;
                    }
                    return `Vault Digestion (${selected.length} files)`;
                }

                function showDigestionCreateForm() {
                    const selected = selectedVaultFiles();
                    if (!digestionCreateForm || !digestionCreateName || !selected.length) return;
                    digestionCreateForm.hidden = false;
                    digestionCreateForm.classList.add('is-visible');
                    digestionCreateName.value = defaultDigestionName(selected);
                    if (digestionCreatePurpose) {
                        digestionCreatePurpose.value = `Reusable context for ${selected.length} selected source${selected.length === 1 ? '' : 's'}`;
                    }
                    global.setTimeout(() => {
                        digestionCreateName.focus();
                        digestionCreateName.select();
                    }, 0);
                }

                function hideDigestionCreateForm() {
                    if (!digestionCreateForm) return;
                    digestionCreateForm.classList.remove('is-visible');
                    digestionCreateForm.hidden = true;
                }

                function updateSelectionUi() {
                    const selected = selectedVaultFiles();
                    if (selectionBar) selectionBar.hidden = selected.length === 0;
                    if (selectionCount) selectionCount.textContent = `${selected.length} selected`;
                    if (selectionOpen) selectionOpen.disabled = selected.length === 0;
                    if (selectionCopy) selectionCopy.disabled = selected.length === 0;
                    if (selectionDigest) selectionDigest.disabled = selected.length === 0;
                    if (selectionClear) selectionClear.disabled = selected.length === 0;
                    if (!selected.length) hideDigestionCreateForm();
                    if (grid) {
                        grid.querySelectorAll('[data-vault-file-id]').forEach((card) => {
                            const id = card.getAttribute('data-vault-file-id') || '';
                            card.classList.toggle('is-selected', state.selectedIds.has(id));
                        });
                    }
                }

                function applyVaultViewMode() {
                    if (!grid) return;
                    grid.classList.remove('view-list', 'view-icons', 'view-preview');
                    grid.classList.add(`view-${state.viewMode}`);
                    page.querySelectorAll('[data-vault-view-mode]').forEach((btn) => {
                        const active = btn.getAttribute('data-vault-view-mode') === state.viewMode;
                        btn.classList.toggle('active', active);
                        btn.setAttribute('aria-pressed', active ? 'true' : 'false');
                    });
                }

                function setVaultViewMode(mode) {
                    const nextMode = VAULT_VIEW_MODES.has(String(mode || '')) ? String(mode) : 'preview';
                    state.viewMode = nextMode;
                    try {
                        if (global.localStorage) global.localStorage.setItem(VAULT_VIEW_STORAGE_KEY, nextMode);
                    } catch (_) {}
                    applyVaultViewMode();
                }

                function updateStats(stats) {
                    const safeStats = stats || {};
                    const byCategory = safeStats.by_category || {};
                    const docs = Number(byCategory.documents?.count || 0);
                    const media = Number(byCategory.images?.count || 0)
                        + Number(byCategory.videos?.count || 0)
                        + Number(byCategory.video?.count || 0)
                        + Number(byCategory.audio?.count || 0);
                    const countEl = document.getElementById('vault-stat-count');
                    const bytesEl = document.getElementById('vault-stat-bytes');
                    const docsEl = document.getElementById('vault-stat-docs');
                    const mediaEl = document.getElementById('vault-stat-media');
                    if (countEl) countEl.textContent = String(safeStats.count || 0);
                    if (bytesEl) bytesEl.textContent = formatBytes(safeStats.bytes || 0);
                    if (docsEl) docsEl.textContent = String(docs);
                    if (mediaEl) mediaEl.textContent = String(media);
                }

                function digestionStatusLabel(digestion) {
                    const status = String(digestion && digestion.status || 'draft').replace(/_/g, ' ');
                    return status.charAt(0).toUpperCase() + status.slice(1);
                }

                function vaultCssEscape(value) {
                    if (global.CSS && typeof global.CSS.escape === 'function') return global.CSS.escape(String(value || ''));
                    return String(value || '').replace(/["\\]/g, '\\$&');
                }

                function renderDigestionCard(digestion) {
                    const id = vaultEscape(digestion.id || '');
                    const name = vaultEscape(digestion.name || 'Untitled Digestion');
                    const status = vaultEscape(digestionStatusLabel(digestion));
                    const stats = digestion.stats || {};
                    const chunks = Number(stats.chunks || 0);
                    const tokens = Number(stats.token_estimate || 0);
                    const sourceCount = Array.isArray(digestion.sources) ? digestion.sources.length : 0;
                    const provider = vaultEscape(digestion.provider || 'local');
                    const purpose = vaultEscape(digestion.purpose || digestion.description || '');
                    const queryDisabled = chunks <= 0;
                    const queryTitle = queryDisabled
                        ? 'Build this Digestion before querying; no indexed chunks are available yet.'
                        : 'Query indexed chunks with cited retrieval.';
                    return `
                        <article class="vault-digestion-card" data-vault-digestion-id="${id}">
                            <div class="vault-digestion-card-title">${name}</div>
                            ${purpose ? `<div class="small text-muted mt-1">${purpose}</div>` : ''}
                            <div class="vault-digestion-meta">
                                <span class="vault-digestion-pill"><i class="bi bi-activity"></i>${status}</span>
                                <span class="vault-digestion-pill"><i class="bi bi-files"></i>${sourceCount} source${sourceCount === 1 ? '' : 's'}</span>
                                <span class="vault-digestion-pill"><i class="bi bi-braces"></i>${chunks} chunks</span>
                                <span class="vault-digestion-pill"><i class="bi bi-file-earmark-text"></i>${tokens} tokens</span>
                                <span class="vault-digestion-pill"><i class="bi bi-cpu"></i>${provider}</span>
                            </div>
                            <div class="vault-digestion-actions">
                                <button class="btn btn-sm btn-primary" type="button" data-vault-digestion-action="build" data-vault-digestion-id="${id}">
                                    <i class="bi bi-hammer"></i> Build
                                </button>
                                <button class="btn btn-sm btn-outline-secondary" type="button" data-vault-digestion-action="outputs" data-vault-digestion-id="${id}">
                                    <i class="bi bi-journal-richtext"></i> Outputs
                                </button>
                            </div>
                            <form class="vault-digestion-query" data-vault-digestion-query-form="${id}">
                                <input class="form-control form-control-sm"
                                       type="search"
                                       data-vault-digestion-query="${id}"
                                       placeholder="${queryDisabled ? 'Build before querying...' : 'Ask this Digestion...'}"
                                       aria-label="Query ${name}"
                                       title="${queryTitle}"
                                       ${queryDisabled ? 'disabled' : ''}>
                                <button class="btn btn-sm btn-outline-secondary" type="submit" title="${queryTitle}" ${queryDisabled ? 'disabled' : ''}>
                                    <i class="bi bi-search"></i> Query
                                </button>
                            </form>
                            <div class="vault-digestion-results" data-vault-digestion-results="${id}"></div>
                            <div class="vault-digestion-output-list" data-vault-digestion-outputs="${id}"></div>
                        </article>
                    `;
                }

                function renderDigestions() {
                    if (!digestionList) return;
                    if (!state.digestions.length) {
                        digestionList.innerHTML = `
                            <div class="vault-digestion-card">
                                <div class="vault-digestion-card-title">No Digestions yet</div>
                                <div class="vault-digestion-meta">
                                    Select files below, then choose <strong>New Digestion</strong> to build a queryable research corpus.
                                </div>
                            </div>
                        `;
                        return;
                    }
                    digestionList.innerHTML = state.digestions.map(renderDigestionCard).join('');
                }

                async function loadDigestions() {
                    if (!digestionList) return;
                    try {
                        const data = await apiCall(`${vaultUrls().digestions}?include_sources=1`);
                        state.digestions = Array.isArray(data.digestions) ? data.digestions : [];
                        renderDigestions();
                    } catch (error) {
                        console.error('Digestion load failed:', error);
                        if (typeof showAlert === 'function') showAlert(error.error || 'Could not load Digestions.', 'warning');
                    }
                }

                async function createDigestionFromSelection() {
                    const selected = selectedVaultFiles();
                    if (!selected.length) return;
                    const name = String(digestionCreateName && digestionCreateName.value || defaultDigestionName(selected)).trim();
                    const purpose = String(digestionCreatePurpose && digestionCreatePurpose.value || '').trim();
                    if (!name) {
                        if (digestionCreateName) digestionCreateName.focus();
                        return;
                    }
                    if (selectionDigest) selectionDigest.disabled = true;
                    try {
                        const data = await apiCall(vaultUrls().digestions, {
                            method: 'POST',
                            body: JSON.stringify({
                                name: name.trim(),
                                purpose,
                                source_file_ids: selected.map(file => file.id).filter(Boolean)
                            })
                        });
                        if (typeof showAlert === 'function') {
                            showAlert('Digestion created. Build it when you are ready to index the selected files.', 'success');
                        }
                        hideDigestionCreateForm();
                        if (data.digestion) {
                            state.digestions.unshift(data.digestion);
                            renderDigestions();
                        } else {
                            await loadDigestions();
                        }
                    } catch (error) {
                        if (typeof showAlert === 'function') showAlert(error.error || 'Could not create Digestion.', 'danger');
                    } finally {
                        if (selectionDigest) selectionDigest.disabled = selectedVaultFiles().length === 0;
                    }
                }

                async function buildDigestion(digestionId, button) {
                    if (!digestionId) return;
                    const original = button ? button.innerHTML : '';
                    if (button) {
                        button.disabled = true;
                        button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Building';
                    }
                    try {
                        const result = await apiCall(`${vaultUrls().digestions}/${encodeURIComponent(digestionId)}/build`, {
                            method: 'POST',
                            body: JSON.stringify({ rebuild: false })
                        });
                        if (typeof showAlert === 'function') {
                            showAlert(result.success ? 'Digestion built.' : 'Digestion build completed with issues.', result.success ? 'success' : 'warning');
                        }
                        await loadDigestions();
                    } catch (error) {
                        if (typeof showAlert === 'function') showAlert(error.error || 'Could not build Digestion.', 'danger');
                    } finally {
                        if (button) {
                            button.disabled = false;
                            button.innerHTML = original;
                        }
                    }
                }

                async function loadDigestionOutputs(digestionId, button) {
                    if (!digestionId) return;
                    const outputsEl = document.querySelector(`[data-vault-digestion-outputs="${vaultCssEscape(digestionId)}"]`);
                    if (!outputsEl) return;
                    const visible = outputsEl.classList.contains('is-visible');
                    if (visible && outputsEl.dataset.loaded === '1') {
                        outputsEl.classList.remove('is-visible');
                        return;
                    }
                    const original = button ? button.innerHTML : '';
                    if (button) {
                        button.disabled = true;
                        button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Outputs';
                    }
                    outputsEl.classList.add('is-visible');
                    outputsEl.innerHTML = '<div class="small text-muted"><span class="spinner-border spinner-border-sm me-1"></span>Loading reusable outputs...</div>';
                    try {
                        let data = await apiCall(`${vaultUrls().digestions}/${encodeURIComponent(digestionId)}/outputs`);
                        let outputs = Array.isArray(data.outputs) ? data.outputs : [];
                        if (!outputs.length) {
                            data = await apiCall(`${vaultUrls().digestions}/${encodeURIComponent(digestionId)}/outputs`, {
                                method: 'POST',
                                body: JSON.stringify({})
                            });
                            outputs = Array.isArray(data.outputs) ? data.outputs : [];
                        }
                        outputsEl.dataset.loaded = '1';
                        outputsEl.innerHTML = outputs.length
                            ? outputs.map(output => `
                                <div class="vault-digestion-output">
                                    <div class="vault-digestion-output-head">
                                        <div class="vault-digestion-output-title">${vaultEscape(output.title || output.output_kind || 'Output')}</div>
                                        <button class="btn btn-sm btn-outline-primary"
                                                type="button"
                                                data-vault-digestion-action="export-output"
                                                data-vault-digestion-id="${vaultEscape(digestionId)}"
                                                data-vault-output-ref="${vaultEscape(output.id || output.output_kind || '')}">
                                            <i class="bi bi-save"></i> Vault
                                        </button>
                                    </div>
                                    <div>${vaultEscape(output.preview || '').slice(0, 420)}</div>
                                </div>
                            `).join('')
                            : '<div class="small text-muted">No reusable outputs are available yet. Build this Digestion first.</div>';
                    } catch (error) {
                        outputsEl.innerHTML = `<div class="small text-danger">${vaultEscape(error.error || 'Could not load Digestion outputs.')}</div>`;
                        if (typeof showAlert === 'function') showAlert(error.error || 'Could not load Digestion outputs.', 'danger');
                    } finally {
                        if (button) {
                            button.disabled = false;
                            button.innerHTML = original;
                        }
                    }
                }

                async function exportDigestionOutput(digestionId, outputRef, button) {
                    if (!digestionId || !outputRef) return;
                    const original = button ? button.innerHTML : '';
                    if (button) {
                        button.disabled = true;
                        button.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Saving';
                    }
                    try {
                        const data = await apiCall(`${vaultUrls().digestions}/${encodeURIComponent(digestionId)}/outputs/${encodeURIComponent(outputRef)}/export`, {
                            method: 'POST',
                            body: JSON.stringify({})
                        });
                        if (typeof showAlert === 'function') {
                            const name = data.file && (data.file.original_name || data.file.id) || 'Digestion output';
                            showAlert(`${name} saved to Vault.`, 'success');
                        }
                        await loadVaultFiles({ reset: true });
                    } catch (error) {
                        if (typeof showAlert === 'function') showAlert(error.error || 'Could not export Digestion output.', 'danger');
                    } finally {
                        if (button) {
                            button.disabled = false;
                            button.innerHTML = original;
                        }
                    }
                }

                async function queryDigestion(digestionId, queryText = '') {
                    if (!digestionId) return;
                    const query = String(
                        queryText
                        || document.querySelector(`[data-vault-digestion-query="${vaultCssEscape(digestionId)}"]`)?.value
                        || ''
                    );
                    if (!query || !query.trim()) return;
                    const resultsEl = document.querySelector(`[data-vault-digestion-results="${vaultCssEscape(digestionId)}"]`);
                    if (resultsEl) {
                        resultsEl.classList.add('is-visible');
                        resultsEl.innerHTML = '<div class="small text-muted"><span class="spinner-border spinner-border-sm me-1"></span>Searching...</div>';
                    }
                    try {
                        const data = await apiCall(`${vaultUrls().digestions}/${encodeURIComponent(digestionId)}/query`, {
                            method: 'POST',
                            body: JSON.stringify({ query: query.trim(), top_k: 5 })
                        });
                        const results = Array.isArray(data.results) ? data.results : [];
                        const warning = String(data.warning || '').trim();
                        if (resultsEl) {
                            resultsEl.classList.add('is-visible');
                            resultsEl.innerHTML = results.length
                                ? `${warning ? `<div class="small text-warning mb-2">${vaultEscape(warning)}</div>` : ''}${results.map((item) => `
                                    <div class="vault-digestion-result">
                                        <strong>${vaultEscape(item.file_name || item.file_id || 'Source')}</strong>
                                        ${item.page_label ? `<span class="text-muted"> ${vaultEscape(item.page_label)}</span>` : ''}
                                        ${Number(item.score || 0) ? `<span class="text-muted small ms-1">score ${Number(item.score || 0).toFixed(3)}</span>` : ''}
                                        <div>${vaultEscape(item.snippet || '').slice(0, 900)}</div>
                                    </div>
                                `).join('')}`
                                : `<div class="small ${warning ? 'text-warning' : 'text-muted'}">${vaultEscape(warning || 'No matching snippets found.')}</div>`;
                        }
                    } catch (error) {
                        if (resultsEl) {
                            resultsEl.classList.add('is-visible');
                            resultsEl.innerHTML = `<div class="small text-danger">${vaultEscape(error.error || 'Could not query Digestion.')}</div>`;
                        }
                        if (typeof showAlert === 'function') showAlert(error.error || 'Could not query Digestion.', 'danger');
                    }
                }

                function render() {
                    if (!grid || !empty) return;
                    const visibleIds = new Set(state.files.map(file => String(file.id || '')).filter(Boolean));
                    state.selectedIds.forEach((id) => {
                        if (!visibleIds.has(id)) state.selectedIds.delete(id);
                    });
                    if (!state.files.length && !state.folders.length) {
                        grid.innerHTML = '';
                        empty.style.display = 'block';
                    } else {
                        empty.style.display = 'none';
                        grid.innerHTML = state.folders.map(renderVaultFolderCard).join('')
                            + state.files.map(file => renderVaultPageCard(file, state.selectedIds.has(String(file.id || '')))).join('');
                    }
                    applyVaultViewMode();
                    updateSelectionUi();
                    updateStats(state.stats);
                    renderDigestions();
                    if (breadcrumb) {
                        const crumbs = [
                            `<button class="vault-crumb${state.currentFolderId ? '' : ' active'}" type="button" data-vault-folder-target="" data-vault-folder-drop=""><i class="bi bi-house-door me-1"></i>Vault Home</button>`
                        ];
                        state.path.forEach((folder) => {
                            const id = vaultEscape(folder.id || '');
                            const name = vaultEscape(folder.name || 'Folder');
                            const active = String(folder.id || '') === String(state.currentFolderId || '') ? ' active' : '';
                            crumbs.push(`<span class="text-muted">/</span><button class="vault-crumb${active}" type="button" data-vault-folder-target="${id}" data-vault-folder-drop="${id}">${name}</button>`);
                        });
                        breadcrumb.innerHTML = crumbs.join('');
                    }
                    if (rootBtn) rootBtn.hidden = !state.currentFolderId;
                    if (loadMore) {
                        loadMore.hidden = !(state.files.length && state.files.length % state.limit === 0);
                    }
                }

                function folderEndpoint(folderId) {
                    return `${vaultUrls().folders}/${encodeURIComponent(folderId)}`;
                }

                function summarizeVaultUploadError(error) {
                    const failed = Array.isArray(error && error.failed) ? error.failed : [];
                    if (failed.length) {
                        const first = failed[0] || {};
                        const name = first.name ? `"${first.name}"` : 'Upload';
                        const reason = first.error || error.error || error.message || 'failed';
                        const extra = failed.length > 1 ? ` (${failed.length} files failed)` : '';
                        return `${name}: ${reason}${extra}`;
                    }
                    const message = String((error && (error.error || error.message || error.name)) || '').trim();
                    if (/failed to fetch|networkerror|notreadable|0x80070194|cloud/i.test(message)) {
                        return 'The browser could not read or send this file. If it is stored in OneDrive, Dropbox, iCloud, or another cloud folder, mark it "Always keep on this device" or copy it to a normal local folder, then try again.';
                    }
                    return message || 'Upload failed.';
                }

                async function prepareVaultUploadFile(file) {
                    if (!file || typeof file.arrayBuffer !== 'function') return file;
                    try {
                        const bytes = await file.arrayBuffer();
                        return new Blob([bytes], { type: file.type || 'application/octet-stream' });
                    } catch (error) {
                        const err = new Error(summarizeVaultUploadError(error));
                        err.name = error && error.name ? error.name : 'FileReadError';
                        throw err;
                    }
                }

                function setNewFolderError(message) {
                    if (!newFolderError) return;
                    const text = String(message || '').trim();
                    newFolderError.textContent = text;
                    newFolderError.hidden = !text;
                }

                function showNewFolderForm() {
                    if (!newFolderForm) return;
                    newFolderForm.hidden = false;
                    newFolderForm.classList.add('is-visible');
                    setNewFolderError('');
                    if (newFolderName) {
                        newFolderName.value = '';
                        window.setTimeout(() => newFolderName.focus(), 0);
                    }
                }

                function hideNewFolderForm() {
                    if (!newFolderForm) return;
                    newFolderForm.classList.remove('is-visible');
                    newFolderForm.hidden = true;
                    setNewFolderError('');
                    if (newFolderName) newFolderName.value = '';
                }

                async function navigateToFolder(folderId) {
                    state.currentFolderId = String(folderId || '');
                    state.offset = 0;
                    await loadFiles({ append: false });
                }

                async function loadFiles({ append = false } = {}) {
                    if (state.loading) return;
                    state.loading = true;
                    if (loadMore) {
                        loadMore.disabled = true;
                        loadMore.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span> Loading...';
                    }
                    try {
                        const offset = append ? state.files.length : 0;
                        const data = await fetchVaultFiles({
                            q: state.query,
                            category: state.category,
                            offset,
                            limit: state.limit,
                            folderId: state.currentFolderId
                        });
                        const nextFiles = Array.isArray(data.files) ? data.files : [];
                        state.files = append ? state.files.concat(nextFiles) : nextFiles;
                        if (!append) {
                            state.folders = Array.isArray(data.folders) ? data.folders : [];
                            state.path = Array.isArray(data.path) ? data.path : [];
                            state.currentFolderId = String(data.current_folder_id || state.currentFolderId || '');
                        }
                        state.stats = data.stats || state.stats || {};
                        render();
                    } catch (error) {
                        console.error('Vault load failed:', error);
                        if (typeof showAlert === 'function') showAlert(error.error || 'Could not load vault files.', 'danger');
                    } finally {
                        state.loading = false;
                        if (loadMore) {
                            loadMore.disabled = false;
                            loadMore.innerHTML = '<i class="bi bi-chevron-down"></i> Load more';
                        }
                    }
                }

                async function uploadFiles(fileList) {
                    const files = Array.from(fileList || []).filter(Boolean);
                    if (!files.length) return;
                    const savedFiles = [];
                    const failedFiles = [];
                    if (dropzone) dropzone.classList.add('is-dragging');
                    try {
                        for (const file of files) {
                            const form = new FormData();
                            try {
                                const uploadFile = await prepareVaultUploadFile(file);
                                form.append('file', uploadFile, file.name || 'upload');
                                form.append('folder_id', state.currentFolderId || '');
                                const data = await apiCall(vaultUrls().upload, {
                                    method: 'POST',
                                    body: form
                                });
                                const nextSaved = Array.isArray(data.files) ? data.files : [];
                                const nextFailed = Array.isArray(data.failed) ? data.failed : [];
                                savedFiles.push(...nextSaved);
                                if (nextFailed.length) {
                                    failedFiles.push(...nextFailed);
                                } else if (!nextSaved.length) {
                                    failedFiles.push({
                                        name: file.name || 'upload',
                                        error: data.error || data.message || 'Upload failed',
                                    });
                                }
                            } catch (error) {
                                failedFiles.push({
                                    name: file.name || 'upload',
                                    error: summarizeVaultUploadError(error),
                                });
                            }
                        }
                        const saved = savedFiles.length;
                        const failed = failedFiles.length;
                        if (typeof showAlert === 'function') {
                            if (saved) {
                                showAlert(`${saved} file${saved === 1 ? '' : 's'} added to your vault${failed ? `; ${failed} failed` : ''}.`, failed ? 'warning' : 'success');
                            } else if (failed) {
                                const first = failedFiles[0] || {};
                                showAlert(`${first.name || 'Upload'}: ${first.error || 'Upload failed.'}`, 'danger');
                            }
                        }
                        if (uploadInput) uploadInput.value = '';
                        await loadFiles({ append: false });
                    } catch (error) {
                        console.error('Vault upload failed:', error);
                        if (typeof showAlert === 'function') showAlert(summarizeVaultUploadError(error), 'danger');
                    } finally {
                        if (dropzone) dropzone.classList.remove('is-dragging');
                    }
                }

                render();
                if (breadcrumb) {
                    breadcrumb.addEventListener('click', (event) => {
                        const target = event.target.closest('[data-vault-folder-target]');
                        if (!target || !breadcrumb.contains(target)) return;
                        navigateToFolder(target.getAttribute('data-vault-folder-target') || '');
                    });
                }
                if (rootBtn) rootBtn.addEventListener('click', () => navigateToFolder(''));
                if (newFolderBtn) {
                    newFolderBtn.addEventListener('click', showNewFolderForm);
                }
                if (newFolderCancel) {
                    newFolderCancel.addEventListener('click', hideNewFolderForm);
                }
                if (newFolderForm) {
                    newFolderForm.addEventListener('submit', async (event) => {
                        event.preventDefault();
                        const name = String(newFolderName && newFolderName.value || '').trim();
                        if (!name) {
                            setNewFolderError('Folder name is required.');
                            if (newFolderName) newFolderName.focus();
                            return;
                        }
                        const submitBtn = newFolderForm.querySelector('button[type="submit"]');
                        if (submitBtn) submitBtn.disabled = true;
                        try {
                            await apiCall(vaultUrls().folders, {
                                method: 'POST',
                                body: JSON.stringify({ name, parent_id: state.currentFolderId || '' })
                            });
                            hideNewFolderForm();
                            if (typeof showAlert === 'function') showAlert('Folder created.', 'success');
                            await loadFiles({ append: false });
                        } catch (error) {
                            const message = (error && (error.error || error.message)) || 'Could not create folder.';
                            setNewFolderError(message);
                            if (typeof showAlert === 'function') showAlert(message, 'warning');
                        } finally {
                            if (submitBtn) submitBtn.disabled = false;
                        }
                    });
                }
                if (search) {
                    search.addEventListener('input', () => {
                        clearTimeout(state.timer);
                        state.timer = global.setTimeout(() => {
                            state.query = search.value.trim();
                            loadFiles({ append: false });
                        }, 180);
                    });
                }
                page.querySelectorAll('[data-vault-filter]').forEach((btn) => {
                    btn.addEventListener('click', () => {
                        state.category = btn.getAttribute('data-vault-filter') || '';
                        page.querySelectorAll('[data-vault-filter]').forEach((other) => {
                            other.classList.toggle('active', other === btn);
                        });
                        loadFiles({ append: false });
                    });
                });
                page.querySelectorAll('[data-vault-view-mode]').forEach((btn) => {
                    btn.addEventListener('click', () => setVaultViewMode(btn.getAttribute('data-vault-view-mode') || 'preview'));
                });
                const refreshBtn = document.getElementById('vault-refresh-btn');
                if (refreshBtn) refreshBtn.addEventListener('click', () => loadFiles({ append: false }));
                if (loadMore) loadMore.addEventListener('click', () => loadFiles({ append: true }));
                if (selectionOpen) {
                    selectionOpen.addEventListener('click', () => {
                        const first = selectedVaultFiles()[0];
                        if (first) openVaultFile(first);
                    });
                }
                if (selectionCopy) {
                    selectionCopy.addEventListener('click', async () => {
                        const links = selectedVaultFiles().map(file => file.markdown_link || `[${file.name || file.id}](/files/${file.id})`);
                        if (!links.length) return;
                        await copyText(links.join('\n'), 'Vault links');
                    });
                }
                if (selectionDigest) {
                    selectionDigest.addEventListener('click', showDigestionCreateForm);
                }
                if (digestionCreateCancel) {
                    digestionCreateCancel.addEventListener('click', hideDigestionCreateForm);
                }
                if (digestionCreateForm) {
                    digestionCreateForm.addEventListener('submit', (event) => {
                        event.preventDefault();
                        createDigestionFromSelection();
                    });
                }
                if (digestionRefresh) {
                    digestionRefresh.addEventListener('click', loadDigestions);
                }
                if (digestionList) {
                    digestionList.addEventListener('click', (event) => {
                        const actionBtn = event.target.closest('[data-vault-digestion-action]');
                        if (!actionBtn || !digestionList.contains(actionBtn)) return;
                        const digestionId = actionBtn.getAttribute('data-vault-digestion-id') || '';
                        const action = actionBtn.getAttribute('data-vault-digestion-action') || '';
                        if (action === 'build') {
                            buildDigestion(digestionId, actionBtn);
                        } else if (action === 'outputs') {
                            loadDigestionOutputs(digestionId, actionBtn);
                        } else if (action === 'export-output') {
                            exportDigestionOutput(
                                digestionId,
                                actionBtn.getAttribute('data-vault-output-ref') || '',
                                actionBtn,
                            );
                        }
                    });
                    digestionList.addEventListener('submit', (event) => {
                        const form = event.target.closest('[data-vault-digestion-query-form]');
                        if (!form || !digestionList.contains(form)) return;
                        event.preventDefault();
                        const digestionId = form.getAttribute('data-vault-digestion-query-form') || '';
                        const input = form.querySelector('[data-vault-digestion-query]');
                        queryDigestion(digestionId, input ? input.value : '');
                    });
                }
                if (selectionClear) {
                    selectionClear.addEventListener('click', () => {
                        state.selectedIds.clear();
                        render();
                    });
                }
                if (dropzone && uploadInput) {
                    dropzone.addEventListener('keydown', (event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                            event.preventDefault();
                            uploadInput.click();
                        }
                    });
                    uploadInput.addEventListener('change', () => uploadFiles(uploadInput.files));
                    ['dragenter', 'dragover'].forEach((eventName) => {
                        dropzone.addEventListener(eventName, (event) => {
                            if (isVaultFileDragEvent(event)) return;
                            event.preventDefault();
                            dropzone.classList.add('is-dragging');
                        });
                    });
                    ['dragleave', 'drop'].forEach((eventName) => {
                        dropzone.addEventListener(eventName, (event) => {
                            if (isVaultFileDragEvent(event)) return;
                            event.preventDefault();
                            if (eventName === 'drop') uploadFiles(event.dataTransfer && event.dataTransfer.files);
                            dropzone.classList.remove('is-dragging');
                        });
                    });
                }
                if (grid) {
                    let draggingFileId = '';
                    let suppressNextCardOpen = false;
                    function clearVaultDropTargets() {
                        page.querySelectorAll('.is-drop-target').forEach(el => el.classList.remove('is-drop-target'));
                    }
                    function vaultDropTargetFromEvent(event) {
                        const target = event.target.closest('[data-vault-folder-drop]');
                        return target && page.contains(target) ? target : null;
                    }
                    function currentFolderForVaultFile(fileId) {
                        const file = state.files.find(candidate => String(candidate.id || '') === String(fileId || ''));
                        return file ? String(file.folder_id || '') : '';
                    }
                    async function moveVaultFileToFolder(fileId, folderId) {
                        const safeFileId = String(fileId || '').trim();
                        const safeFolderId = String(folderId || '').trim();
                        if (!safeFileId) return;
                        if (currentFolderForVaultFile(safeFileId) === safeFolderId) {
                            if (typeof showAlert === 'function') showAlert('File is already in that folder.', 'info');
                            return;
                        }
                        await apiCall(`/ajax/vault/files/${encodeURIComponent(safeFileId)}/folder`, {
                            method: 'PATCH',
                            body: JSON.stringify({ folder_id: safeFolderId })
                        });
                        if (typeof showAlert === 'function') {
                            showAlert(safeFolderId ? 'File moved.' : 'File moved to Vault Home.', 'success');
                        }
                        state.selectedIds.delete(safeFileId);
                        await loadFiles({ append: false });
                    }
                    function markVaultDropTarget(target) {
                        clearVaultDropTargets();
                        if (!target) return;
                        target.classList.add('is-drop-target');
                        const explorer = target.closest('.vault-explorer-bar');
                        if (explorer) explorer.classList.add('is-drop-target');
                    }
                    grid.addEventListener('dragstart', (event) => {
                        const card = event.target.closest('[data-vault-file-id]');
                        if (!card) return;
                        draggingFileId = card.getAttribute('data-vault-file-id') || '';
                        suppressNextCardOpen = false;
                        card.classList.add('is-dragging');
                        card.setAttribute('aria-grabbed', 'true');
                        if (event.dataTransfer) {
                            event.dataTransfer.effectAllowed = 'move';
                            event.dataTransfer.setData('text/plain', draggingFileId);
                            event.dataTransfer.setData(VAULT_FILE_DRAG_TYPE, draggingFileId);
                        }
                    });
                    grid.addEventListener('dragend', () => {
                        const hadDrag = !!draggingFileId;
                        draggingFileId = '';
                        page.querySelectorAll('[aria-grabbed="true"]').forEach(el => el.setAttribute('aria-grabbed', 'false'));
                        grid.querySelectorAll('.is-dragging').forEach(el => el.classList.remove('is-dragging'));
                        clearVaultDropTargets();
                        if (hadDrag) {
                            suppressNextCardOpen = true;
                            global.setTimeout(() => { suppressNextCardOpen = false; }, 80);
                        }
                    });
                    page.addEventListener('dragover', (event) => {
                        const target = vaultDropTargetFromEvent(event);
                        if (!target || !draggingFileId) return;
                        event.preventDefault();
                        markVaultDropTarget(target);
                        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
                    });
                    page.addEventListener('dragleave', (event) => {
                        const target = vaultDropTargetFromEvent(event);
                        if (!target) return;
                        const related = event.relatedTarget;
                        if (related && target.contains(related)) return;
                        target.classList.remove('is-drop-target');
                        const explorer = target.closest('.vault-explorer-bar');
                        if (explorer && !explorer.contains(related)) explorer.classList.remove('is-drop-target');
                    });
                    page.addEventListener('drop', async (event) => {
                        const target = vaultDropTargetFromEvent(event);
                        if (!target || !draggingFileId) return;
                        event.preventDefault();
                        const folderId = target.getAttribute('data-vault-folder-drop') || '';
                        clearVaultDropTargets();
                        try {
                            await moveVaultFileToFolder(draggingFileId, folderId);
                        } catch (error) {
                            if (typeof showAlert === 'function') showAlert(error.error || 'Could not move file.', 'warning');
                        }
                    });
                    grid.addEventListener('click', async (event) => {
                        const selectBox = event.target.closest('[data-vault-select-file]');
                        if (selectBox && grid.contains(selectBox)) {
                            const id = selectBox.getAttribute('data-vault-select-file') || '';
                            if (selectBox.checked) {
                                state.selectedIds.add(id);
                            } else {
                                state.selectedIds.delete(id);
                            }
                            updateSelectionUi();
                            return;
                        }
                        const folderActionBtn = event.target.closest('[data-vault-folder-action]');
                        if (folderActionBtn && grid.contains(folderActionBtn)) {
                            const folderId = folderActionBtn.getAttribute('data-vault-folder-id') || '';
                            const folder = state.folders.find(candidate => String(candidate.id || '') === folderId);
                            if (!folder) return;
                            const action = folderActionBtn.getAttribute('data-vault-folder-action') || '';
                            if (action === 'rename') {
                                const nextName = global.prompt('Rename folder', folder.name || 'Folder');
                                if (!nextName || !nextName.trim() || nextName.trim() === folder.name) return;
                                try {
                                    await apiCall(folderEndpoint(folderId), {
                                        method: 'PATCH',
                                        body: JSON.stringify({ name: nextName.trim() })
                                    });
                                    if (typeof showAlert === 'function') showAlert('Folder renamed.', 'success');
                                    await loadFiles({ append: false });
                                } catch (error) {
                                    if (typeof showAlert === 'function') showAlert(error.error || 'Could not rename folder.', 'warning');
                                }
                            } else if (action === 'delete') {
                                const ok = global.confirm(`Delete empty folder "${folder.name || 'Folder'}"?`);
                                if (!ok) return;
                                try {
                                    await apiCall(folderEndpoint(folderId), { method: 'DELETE' });
                                    if (typeof showAlert === 'function') showAlert('Folder deleted.', 'success');
                                    await loadFiles({ append: false });
                                } catch (error) {
                                    if (typeof showAlert === 'function') showAlert(error.error || 'Could not delete folder.', 'warning');
                                }
                            }
                            return;
                        }
                        const actionBtn = event.target.closest('[data-vault-action]');
                        if (actionBtn && grid.contains(actionBtn)) {
                            const id = actionBtn.getAttribute('data-vault-id') || '';
                            const file = state.files.find(candidate => String(candidate.id || '') === id);
                            const action = actionBtn.getAttribute('data-vault-action') || '';
                            if (!file) return;
                            if (action === 'copy') {
                                await copyText(file.markdown_link || `[${file.name || id}](/files/${id})`, 'Vault link');
                            } else if (action === 'move-root') {
                                try {
                                    await moveVaultFileToFolder(id, '');
                                } catch (error) {
                                    if (typeof showAlert === 'function') showAlert(error.error || 'Could not move vault file.', 'warning');
                                }
                            } else if (action === 'delete') {
                                const ok = global.confirm(`Delete "${file.name || id}" from your local vault? This is only allowed before it is attached to content.`);
                                if (!ok) return;
                                try {
                                    await apiCall(`/ajax/vault/files/${encodeURIComponent(id)}`, { method: 'DELETE' });
                                    state.files = state.files.filter(candidate => String(candidate.id || '') !== id);
                                    state.selectedIds.delete(id);
                                    if (typeof showAlert === 'function') showAlert('Vault file deleted.', 'success');
                                    await loadFiles({ append: false });
                                } catch (error) {
                                    if (typeof showAlert === 'function') showAlert(error.error || 'Could not delete vault file.', 'warning');
                                }
                            }
                            return;
                        }
                        const openTarget = event.target.closest('[data-vault-file-id], [data-vault-open-folder]');
                        if (suppressNextCardOpen && openTarget && grid.contains(openTarget)) {
                            event.preventDefault();
                            suppressNextCardOpen = false;
                            return;
                        }
                        const openFolder = event.target.closest('[data-vault-open-folder]');
                        if (openFolder && grid.contains(openFolder)) {
                            await navigateToFolder(openFolder.getAttribute('data-vault-open-folder') || '');
                            return;
                        }
                        if (event.target.closest('a[href], button, input, label')) return;
                        const fileCard = event.target.closest('[data-vault-file-id]');
                        if (fileCard && grid.contains(fileCard)) {
                            const id = fileCard.getAttribute('data-vault-file-id') || '';
                            const file = state.files.find(candidate => String(candidate.id || '') === id);
                            if (file) openVaultFile(file);
                        }
                    });
                    grid.addEventListener('keydown', async (event) => {
                        if (event.key !== 'Enter' && event.key !== ' ') return;
                        const card = event.target.closest('[data-vault-file-id], [data-vault-open-folder]');
                        if (!card || !grid.contains(card)) return;
                        if (event.target.closest('a[href], button, input, label')) return;
                        event.preventDefault();
                        const folderId = card.getAttribute('data-vault-open-folder') || '';
                        if (folderId || card.hasAttribute('data-vault-folder-id')) {
                            await navigateToFolder(folderId);
                            return;
                        }
                        const fileId = card.getAttribute('data-vault-file-id') || '';
                        const file = state.files.find(candidate => String(candidate.id || '') === fileId);
                        if (file) openVaultFile(file);
                    });
                }
            }

	            global.CanopyVaultPicker = {
	                open: openPicker,
	                fetchFiles: fetchVaultFiles,
	                formatBytes,
	                normalizedAttachment,
	                saveAttachment: saveAttachmentToVault
	            };
	            global.saveAttachmentToVault = saveAttachmentToVault;

	            document.addEventListener('DOMContentLoaded', initVaultPage);
	        })(window);

        (function initCanopyDmRecipientDisclosure(global) {
            function parseRecipients(raw) {
                if (!raw) return [];
                try {
                    const parsed = JSON.parse(String(raw));
                    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
                } catch (error) {
                    console.warn('Could not parse DM recipient list:', error);
                    return [];
                }
            }

            function recipientLabel(rec) {
                return String(
                    (rec && (rec.display_name || rec.username || rec.user_id || rec.id)) ||
                    'Unknown recipient'
                ).trim();
            }

            function recipientUserId(rec) {
                return String((rec && (rec.user_id || rec.id || rec.username)) || '').trim();
            }

            function recipientInitials(label) {
                const parts = String(label || '')
                    .trim()
                    .split(/\s+/)
                    .filter(Boolean);
                if (parts.length >= 2) {
                    return (parts[0][0] + parts[1][0]).toUpperCase();
                }
                return String(label || 'U').slice(0, 2).toUpperCase();
            }

            function ensureModal() {
                let modal = document.getElementById('canopy-dm-recipient-modal');
                if (modal) return modal;
                modal = document.createElement('div');
                modal.className = 'modal fade canopy-dm-recipient-modal';
                modal.id = 'canopy-dm-recipient-modal';
                modal.tabIndex = -1;
                modal.setAttribute('aria-labelledby', 'canopy-dm-recipient-modal-title');
                modal.setAttribute('aria-hidden', 'true');
                modal.innerHTML = `
                    <div class="modal-dialog modal-dialog-centered modal-lg modal-dialog-scrollable">
                        <div class="modal-content">
                            <div class="modal-header">
                                <div>
                                    <h5 class="modal-title" id="canopy-dm-recipient-modal-title">Group recipients</h5>
                                    <div class="text-muted small" data-dm-recipient-modal-subtitle></div>
                                </div>
                                <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
                            </div>
                            <div class="modal-body">
                                <div class="dm-recipient-list" data-dm-recipient-list></div>
                            </div>
                        </div>
                    </div>`;
                modal.addEventListener('click', (event) => {
                    const item = event.target && event.target.closest
                        ? event.target.closest('[data-dm-recipient-user-id]')
                        : null;
                    if (!item || !modal.contains(item)) return;
                    const userId = item.getAttribute('data-dm-recipient-user-id') || '';
                    const label = item.getAttribute('data-dm-recipient-label') || userId || 'recipient';
                    if (userId && typeof global.copyUserId === 'function') {
                        global.copyUserId(userId, label, item);
                    }
                });
                document.body.appendChild(modal);
                return modal;
            }

            function renderRecipient(rec) {
                const label = recipientLabel(rec);
                const userId = recipientUserId(rec);
                const username = String((rec && rec.username) || '').trim();
                const accountType = String((rec && rec.account_type) || '').trim();
                const idLine = username && username !== userId
                    ? `${username} · ${userId || 'no user id'}`
                    : (userId || 'No user id available');
                const meta = accountType ? `${idLine} · ${accountType}` : idLine;
                const avatarUrl = String((rec && rec.avatar_url) || '').trim();
                const avatar = avatarUrl
                    ? `<div class="dm-avatar"><img src="${canopyEscapeHtml(avatarUrl)}" alt="${canopyEscapeHtml(label)}"></div>`
                    : `<div class="dm-avatar"><span>${canopyEscapeHtml(recipientInitials(label))}</span></div>`;
                return `
                    <button type="button"
                            class="dm-recipient-list-item"
                            data-dm-recipient-user-id="${canopyEscapeHtml(userId)}"
                            data-dm-recipient-label="${canopyEscapeHtml(label)}">
                        ${avatar}
                        <span class="dm-recipient-list-copy">
                            <span class="dm-recipient-list-name">${canopyEscapeHtml(label)}</span>
                            <span class="dm-recipient-list-id">${canopyEscapeHtml(meta)}</span>
                        </span>
                    </button>`;
            }

            function showRecipients(recipients, options = {}) {
                const cleanRecipients = Array.isArray(recipients) ? recipients.filter(Boolean) : [];
                if (!cleanRecipients.length) {
                    if (typeof global.showAlert === 'function') {
                        global.showAlert('No recipient list is available for this group DM yet.', 'info');
                    }
                    return;
                }
                const modal = ensureModal();
                const title = modal.querySelector('#canopy-dm-recipient-modal-title');
                const subtitle = modal.querySelector('[data-dm-recipient-modal-subtitle]');
                const list = modal.querySelector('[data-dm-recipient-list]');
                if (title) title.textContent = options.title || 'Group recipients';
                if (subtitle) {
                    subtitle.textContent = `${cleanRecipients.length} recipient${cleanRecipients.length === 1 ? '' : 's'} in this conversation`;
                }
                if (list) {
                    list.innerHTML = cleanRecipients.map(renderRecipient).join('');
                }
                if (global.bootstrap && global.bootstrap.Modal) {
                    global.bootstrap.Modal.getOrCreateInstance(modal).show();
                } else {
                    modal.classList.add('show');
                    modal.style.display = 'block';
                    modal.removeAttribute('aria-hidden');
                }
            }

            function handleRecipientDisclosureClick(event) {
                const actionEl = event.target && event.target.closest
                    ? event.target.closest('[data-dm-action="show-recipients"]')
                    : null;
                if (!actionEl) return;
                event.preventDefault();
                event.stopPropagation();
                showRecipients(parseRecipients(actionEl.getAttribute('data-dm-recipients') || '[]'));
            }

            document.addEventListener('click', handleRecipientDisclosureClick, true);
            global.CanopyDmRecipients = {
                show: showRecipients,
                parse: parseRecipients,
            };
        })(window);

        (function initCanopyLLMCompose(global) {
            function hasTrigger(value) {
                return /(^|\s)@canopy\b/i.test(String(value || ''));
            }

            async function expandDraft(payload) {
                return apiCall('/ajax/canopy_llm/expand', {
                    method: 'POST',
                    body: JSON.stringify(payload || {}),
                });
            }

            function parseSseBuffer(buffer, onEvent) {
                let rest = String(buffer || '');
                function findSeparator() {
                    const lf = rest.indexOf('\n\n');
                    const crlf = rest.indexOf('\r\n\r\n');
                    if (lf === -1) return crlf;
                    if (crlf === -1) return lf;
                    return Math.min(lf, crlf);
                }
                let separator = findSeparator();
                while (separator !== -1) {
                    const rawEvent = rest.slice(0, separator);
                    const skip = rest.startsWith('\r\n\r\n', separator) ? 4 : 2;
                    rest = rest.slice(separator + skip);
                    const rawData = rawEvent
                        .split(/\r?\n/)
                        .filter(line => line.startsWith('data:'))
                        .map(line => line.slice(5).trimStart())
                        .join('\n')
                        .trim();
                    if (rawData) {
                        const event = JSON.parse(rawData);
                        onEvent(event);
                    }
                    separator = findSeparator();
                }
                return rest;
            }

            async function streamDraft(payload, handlers = {}) {
                if (!global.TextDecoder || !global.ReadableStream) {
                    throw { fallback: true };
                }
                const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
                const response = await fetch('/ajax/canopy_llm/expand_stream', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        ...(csrfToken ? { 'X-CSRFToken': csrfToken } : {}),
                    },
                    body: JSON.stringify(payload || {}),
                });
                if (!response.ok || !response.body) {
                    let errorPayload = {};
                    try { errorPayload = await response.json(); } catch (_) { errorPayload = {}; }
                    throw errorPayload.error ? errorPayload : { fallback: true };
                }
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';
                let finalPayload = null;
                while (true) {
                    const { value, done } = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, { stream: true });
                    buffer = parseSseBuffer(buffer, (event) => {
                        if (!event || typeof event !== 'object') return;
                        if (event.type === 'delta' && typeof handlers.onDelta === 'function') {
                            handlers.onDelta(String(event.delta || ''));
                        } else if (event.type === 'status' && typeof handlers.onStatus === 'function') {
                            handlers.onStatus(String(event.message || ''));
                        } else if (event.type === 'done') {
                            finalPayload = event;
                        } else if (event.type === 'error') {
                            throw event;
                        }
                    });
                }
                buffer += decoder.decode();
                parseSseBuffer(buffer, (event) => {
                    if (event && event.type === 'done') finalPayload = event;
                    if (event && event.type === 'error') throw event;
                });
                if (!finalPayload || !String(finalPayload.content || '').trim()) {
                    throw { error: 'Canopy AI Compose returned an empty draft.' };
                }
                return finalPayload;
            }

            global.CanopyLLMCompose = {
                hasTrigger,
                expandDraft,
                streamDraft,
            };
        })(window);

        (function initStructuredComposerSupport(global) {
            const SUPPORTED_TAGS = new Set([
                'task',
                'objective',
                'request',
                'signal',
                'handoff',
                'circle',
                'contract',
                'skill',
            ]);
            const CANONICAL_TEMPLATE_TYPES = ['task', 'request', 'objective', 'handoff', 'signal'];
            const TOOL_LABELS = {
                task: 'Task',
                request: 'Request',
                objective: 'Objective',
                handoff: 'Handoff',
                signal: 'Signal',
                circle: 'Circle',
                contract: 'Contract',
                skill: 'Skill',
            };
            const TAG_SUGGESTIONS = {
                artifact: 'signal',
                findings: 'signal',
                finding: 'signal',
                status: 'signal',
                update: 'signal',
                request_accepted: 'handoff',
                'request-accepted': 'handoff',
                accepted_request: 'handoff',
            };

            function normalizeToolBody(text) {
                if (!text) return '';
                return String(text)
                    .replace(/\r\n?/g, '\n')
                    .replace(/\n{3,}/g, '\n\n')
                    .trim();
            }

            function maskCodeFences(text) {
                return String(text || '').replace(/```[\s\S]*?```/g, (match) => '\u0000'.repeat(match.length));
            }

            function toSingleLineSummary(text, maxLen = 360) {
                const clean = normalizeToolBody(text)
                    .replace(/\s*\n+\s*/g, ' / ')
                    .replace(/\s{2,}/g, ' ')
                    .trim();
                if (!clean) return '';
                return clean.length > maxLen ? `${clean.slice(0, maxLen - 3).trim()}...` : clean;
            }

            function deriveToolTitle(text, fallback) {
                const clean = normalizeToolBody(text);
                if (!clean) return fallback;
                const first = clean.split('\n')[0] || '';
                const compact = first
                    .replace(/^(?:[@#][\w.\-]+\s*)+/, '')
                    .replace(/^[>\-*0-9.\s]+/, '')
                    .replace(/\s+/g, ' ')
                    .trim();
                if (!compact) return fallback;
                return compact.length > 90 ? `${compact.slice(0, 87).trim()}...` : compact;
            }

            function extractMentionHandles(text, limit = 8) {
                if (!text || limit <= 0) return [];
                const matches = String(text).matchAll(/(?:^|\s)@([A-Za-z0-9_.-]{1,64})/g);
                const seen = new Set();
                const handles = [];
                for (const match of matches) {
                    const raw = String(match && match[1] ? match[1] : '').trim();
                    if (!raw) continue;
                    const key = raw.toLowerCase();
                    if (seen.has(key)) continue;
                    seen.add(key);
                    handles.push(`@${raw}`);
                    if (handles.length >= limit) break;
                }
                return handles;
            }

            function deriveSignalTags(text) {
                const lc = String(text || '').toLowerCase();
                const tags = [];
                const addTag = (tag) => {
                    if (tags.indexOf(tag) === -1) tags.push(tag);
                };

                if (/\b(latency|benchmark|p50|p95|p99|slo|kpi|metric|throughput|mean|median|ci|confidence)\b/.test(lc)) addTag('metrics');
                if (/\b(security|access|forbidden|auth|permission|private|governance|trust)\b/.test(lc)) addTag('security');
                if (/\b(relay|mesh|peer|catchup|sync|connect|ws:|websocket|nat|turn|stun)\b/.test(lc)) addTag('network');
                if (/\b(fix|bug|error|regression|issue|failed|failure|incident)\b/.test(lc)) addTag('incident');
                if (/\b(experiment|dataset|csv|json|evidence|figure|chart|plot|artifact)\b/.test(lc)) addTag('evidence');
                if (!tags.length) addTag('update');
                return tags.slice(0, 3);
            }

            function buildToolBlock(toolType, sourceText) {
                const body = toSingleLineSummary(sourceText);
                const mentions = extractMentionHandles(sourceText);
                const leadMention = mentions[0] || '';
                const assigneesCsv = mentions.join(', ');
                const objectiveMembersCsv = mentions.map((handle, idx) => (idx === 0 ? `${handle} (lead)` : handle)).join(', ');
                const defaults = {
                    task: 'Action item',
                    request: 'Coordination request',
                    objective: 'Execution objective',
                    handoff: 'Ownership handoff',
                    signal: 'Operational finding',
                };
                const fallback = defaults[toolType] || 'Structured item';
                const title = deriveToolTitle(sourceText, fallback);
                const assigneeLine = leadMention ? `\nassignee: ${leadMention}` : '';
                const requestMemberLine = assigneesCsv ? `\nassignees: ${assigneesCsv}` : '';
                const objectiveMemberLine = objectiveMembersCsv ? `\nmembers: ${objectiveMembersCsv}` : '';
                const handoffOwnerLine = leadMention ? `\nowner: ${leadMention}` : '';
                const signalOwnerLine = leadMention ? `\nowner: ${leadMention}` : '';
                const signalTags = deriveSignalTags(sourceText).join(', ');

                if (toolType === 'task') {
                    return `[task]\ntitle: ${title}\ndescription: ${body || 'Define the work to execute.'}${assigneeLine}\npriority: normal\n[/task]`;
                }
                if (toolType === 'request') {
                    return `[request]\ntitle: ${title}\nrequest: ${body || 'Please complete this request.'}${requestMemberLine}\nrequired_output: Reply with owner, status, and evidence.\npriority: normal\n[/request]`;
                }
                if (toolType === 'objective') {
                    return `[objective]\ntitle: ${title}\ndescription: ${body || 'Track this as a multi-step objective.'}${objectiveMemberLine}\ntasks:\n- [ ] Confirm owner\n- [ ] Execute\n- [ ] Report results\n[/objective]`;
                }
                if (toolType === 'handoff') {
                    return `[handoff]\ntitle: ${title}\nsummary: ${body || 'Transfer ownership with clear next steps.'}${handoffOwnerLine}\nnext:\n- Confirm owner\n- Execute and report back\n[/handoff]`;
                }
                if (toolType === 'signal') {
                    return `[signal]\ntype: finding\ntitle: ${title}\nsummary: ${body || 'Record this as durable structured context.'}${signalOwnerLine}\ntags: ${signalTags}\n[/signal]`;
                }
                return sourceText || '';
            }

            function applyTemplateToDraft(toolType, currentText) {
                const raw = String(currentText || '');
                const trimmed = raw.trim();
                if (!trimmed) {
                    return buildToolBlock(toolType, '');
                }
                if (hasStructuredToolBlock(trimmed)) {
                    return `${trimmed}\n\n${buildToolBlock(toolType, '')}`;
                }
                return buildToolBlock(toolType, trimmed);
            }

            function hasStructuredToolBlock(text) {
                const masked = maskCodeFences(text);
                return /(\[(task|objective|request|signal|handoff|circle|contract|skill)\]|\[\/(task|objective|request|signal|handoff|circle|contract|skill)\]|::(task|objective|request|signal|handoff|circle|contract|skill)\b)/i.test(masked);
            }

            function replaceStructuredTagAlias(text, fromTag, toTag) {
                if (!text || !fromTag || !toTag) return String(text || '');
                const escapedFrom = String(fromTag).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
                const openPattern = new RegExp(`(^|\\n)(\\s*)\\[${escapedFrom}\\](?=\\s|$)`, 'gi');
                const closePattern = new RegExp(`(^|\\n)(\\s*)\\[\\/${escapedFrom}\\](?=\\s|$)`, 'gi');
                return String(text)
                    .replace(openPattern, (_, prefix, indent) => `${prefix}${indent}[${toTag}]`)
                    .replace(closePattern, (_, prefix, indent) => `${prefix}${indent}[/${toTag}]`);
            }

            function normalizeDecoratedStructuredTags(text) {
                if (!text) return '';
                return String(text).replace(
                    /^(\s*)(?:\*\*|__|\*|_|>+)\s*(\[(?:\/)?(?:task|objective|request|signal|handoff|circle|contract|skill)\])/gim,
                    '$1$2'
                );
            }

            function validateStructuredComposerText(text) {
                const raw = String(text || '');
                const masked = maskCodeFences(raw);
                const issues = [];
                const lines = raw.split(/\r?\n/);
                const openCounts = Object.create(null);
                const closeCounts = Object.create(null);

                lines.forEach((line, index) => {
                    const trimmed = line.trim();
                    if (!trimmed) return;

                    const decorated = trimmed.match(/^(?:\*\*|__|\*|_|>+)\s*(\[(?:\/)?([A-Za-z][A-Za-z0-9_-]*)\])/);
                    if (decorated && SUPPORTED_TAGS.has(String(decorated[2] || '').toLowerCase())) {
                        issues.push({
                            kind: 'decorated_tag',
                            line: index + 1,
                            tag: String(decorated[2] || '').toLowerCase(),
                            message: `Line ${index + 1}: remove markdown decoration so the block starts directly with [${String(decorated[2] || '').toLowerCase()}].`,
                        });
                    }

                    const maskedLine = masked.split(/\r?\n/)[index] || '';
                    if (maskedLine.indexOf('\u0000') !== -1) return;

                    const tagMatch = trimmed.match(/^\[(\/?)([A-Za-z][A-Za-z0-9_-]*)\]/);
                    if (!tagMatch) return;
                    const isClose = tagMatch[1] === '/';
                    const rawTag = String(tagMatch[2] || '').trim();
                    const tag = rawTag.toLowerCase();
                    if (!SUPPORTED_TAGS.has(tag)) {
                        const suggestedTag = TAG_SUGGESTIONS[tag] || null;
                        if (!suggestedTag) {
                            return;
                        }
                        issues.push({
                            kind: 'unknown_tag',
                            line: index + 1,
                            tag,
                            suggestedTag,
                            message: `Line ${index + 1}: [${rawTag}] is not a canonical block. Use [${suggestedTag}] instead.`,
                        });
                        return;
                    }
                    if (isClose) closeCounts[tag] = (closeCounts[tag] || 0) + 1;
                    else openCounts[tag] = (openCounts[tag] || 0) + 1;
                });

                SUPPORTED_TAGS.forEach((tag) => {
                    const openCount = openCounts[tag] || 0;
                    const closeCount = closeCounts[tag] || 0;
                    if (openCount > closeCount) {
                        issues.push({
                            kind: 'missing_close',
                            tag,
                            line: null,
                            message: `Add [/${tag}] before sending so the ${TOOL_LABELS[tag] || tag} block closes cleanly.`,
                        });
                    } else if (closeCount > openCount) {
                        issues.push({
                            kind: 'missing_open',
                            tag,
                            line: null,
                            message: `Remove the extra [/${tag}] or add the missing [${tag}] block opener.`,
                        });
                    }
                });

                return {
                    issues,
                    blocking: issues.length > 0,
                };
            }

            global.canopyStructuredComposer = {
                supportedTags: Array.from(SUPPORTED_TAGS),
                templateTypes: CANONICAL_TEMPLATE_TYPES.slice(),
                labels: Object.assign({}, TOOL_LABELS),
                buildToolBlock,
                applyTemplateToDraft,
                hasStructuredToolBlock,
                validate: validateStructuredComposerText,
                replaceStructuredTagAlias,
                normalizeDecoratedStructuredTags,
            };
        })(window);

        // --- Peer/user avatar helpers (stacked avatars) ---
        const canopyPeerProfiles = window.CANOPY_VARS ? window.CANOPY_VARS.peerProfiles : {};
        const canopyPeerTrust = window.CANOPY_VARS ? (window.CANOPY_VARS.peerTrust || {}) : {};
        const canopyInitialConnectedPeers = window.CANOPY_VARS ? (window.CANOPY_VARS.connectedPeers || []) : [];
        const canopyInitialPeerRev = window.CANOPY_VARS ? (window.CANOPY_VARS.peerRev || '') : '';
        const canopyInitialRecentDmContacts = window.CANOPY_VARS ? (window.CANOPY_VARS.recentDmContacts || []) : [];
        const canopyInitialDmRev = window.CANOPY_VARS ? (window.CANOPY_VARS.dmRev || '') : '';
        const canopyInitialDmEventCursor = window.CANOPY_VARS ? Number(window.CANOPY_VARS.dmEventCursor || 0) : 0;
        const canopyInitialAttentionSummary = window.CANOPY_VARS ? (window.CANOPY_VARS.attentionSummary || { messages: 0, channels: 0, feed: 0, total: 0 }) : { messages: 0, channels: 0, feed: 0, total: 0 };
        const canopyInitialAttentionRev = window.CANOPY_VARS ? (window.CANOPY_VARS.attentionRev || '') : '';
        const canopyInitialAttentionItems = window.CANOPY_VARS ? (window.CANOPY_VARS.attentionItems || []) : [];
        const canopyInitialAttentionActivityRev = window.CANOPY_VARS ? (window.CANOPY_VARS.attentionActivityRev || '') : '';
        const canopyInitialAttentionEventCursor = window.CANOPY_VARS ? Number(window.CANOPY_VARS.attentionEventCursor || 0) : 0;
        const canopyLocalPeerId = window.CANOPY_VARS ? String(window.CANOPY_VARS.localPeerId || '').trim() : '';
        const SIDEBAR_CARD_PEEK_LIMIT = 5;
        const canopySidebarRailStoragePrefix = (() => {
            const userId = window.CANOPY_VARS ? String(window.CANOPY_VARS.userId || 'local_user').trim() : 'local_user';
            return `canopy.sidebar.rail.${userId || 'local_user'}`;
        })();
        window.canopyPeerProfiles = canopyPeerProfiles || {};
        window.canopyPeerTrust = canopyPeerTrust || {};
        window.canopyInitialConnectedPeers = canopyInitialConnectedPeers || [];

        function normalizeSidebarCardState(state) {
            const raw = String(state || '').trim().toLowerCase();
            if (raw === 'collapsed' || raw === 'expanded') return raw;
            return 'peek';
        }

        function loadSidebarRailPreference(key, fallback) {
            try {
                if (!window.localStorage) return fallback;
                const raw = window.localStorage.getItem(`${canopySidebarRailStoragePrefix}.${key}`);
                return raw == null ? fallback : raw;
            } catch (_) {
                return fallback;
            }
        }

        function saveSidebarRailPreference(key, value) {
            try {
                if (window.localStorage) {
                    window.localStorage.setItem(`${canopySidebarRailStoragePrefix}.${key}`, String(value));
                }
            } catch (_) {}
        }

        const canopySidebarRailState = {
            cards: {
                dm: normalizeSidebarCardState(loadSidebarRailPreference('dmCardState', 'peek')),
                peers: normalizeSidebarCardState(loadSidebarRailPreference('peerCardState', 'peek')),
            },
            miniPosition: String(loadSidebarRailPreference('miniPosition', 'top') || 'top').trim().toLowerCase() === 'bottom' ? 'bottom' : 'top',
        };

        function getSidebarCardState(kind) {
            return canopySidebarRailState.cards[kind] || 'peek';
        }

        function setSidebarCardState(kind, nextState) {
            const normalized = normalizeSidebarCardState(nextState);
            canopySidebarRailState.cards[kind] = normalized;
            saveSidebarRailPreference(kind === 'dm' ? 'dmCardState' : 'peerCardState', normalized);
            if (kind === 'dm') {
                canopyRenderSidebarDmContacts(canopySidebarDmState.contacts);
            } else if (kind === 'peers') {
                renderSidebarPeers();
            }
        }

        function toggleSidebarCardCollapsed(kind) {
            const current = getSidebarCardState(kind);
            setSidebarCardState(kind, current === 'collapsed' ? 'peek' : 'collapsed');
        }

        function toggleSidebarCardExpansion(kind, totalCount) {
            const current = getSidebarCardState(kind);
            const normalizedTotal = Math.max(0, Number(totalCount) || 0);
            if (normalizedTotal <= SIDEBAR_CARD_PEEK_LIMIT) {
                if (current === 'collapsed') {
                    setSidebarCardState(kind, 'peek');
                }
                return;
            }
            setSidebarCardState(kind, current === 'expanded' ? 'peek' : 'expanded');
        }

        function visibleSidebarCardItems(kind, items) {
            const normalized = Array.isArray(items) ? items.filter(Boolean) : [];
            const state = getSidebarCardState(kind);
            if (state === 'collapsed') return [];
            if (state === 'expanded') return normalized;
            return normalized.slice(0, SIDEBAR_CARD_PEEK_LIMIT);
        }

        function updateSidebarCardChrome(kind, totalCount) {
            const state = getSidebarCardState(kind);
            const safeTotal = Math.max(0, Number(totalCount) || 0);
            const prefix = kind === 'dm' ? 'sidebar-dm' : 'sidebar-peers';
            const card = document.getElementById(`${prefix}-card`);
            const modeLabel = document.getElementById(`${prefix}-mode-label`);
            const toggleBtn = document.getElementById(`${prefix}-toggle`);
            const footer = document.getElementById(`${prefix}-footer`);
            const summary = document.getElementById(`${prefix}-summary`);
            const expandBtn = document.getElementById(`${prefix}-expand-btn`);
            const hasOverflow = safeTotal > SIDEBAR_CARD_PEEK_LIMIT;
            if (card) {
                card.setAttribute('data-view-state', state);
            }
            if (modeLabel) {
                modeLabel.textContent = state === 'collapsed' ? '' : (state === 'expanded' ? 'All' : 'Top 5');
                modeLabel.hidden = state === 'collapsed';
            }
            if (toggleBtn) {
                const icon = toggleBtn.querySelector('i');
                if (icon) {
                    icon.className = `bi bi-chevron-${state === 'collapsed' ? 'down' : 'up'}`;
                }
                toggleBtn.setAttribute('aria-label', `${state === 'collapsed' ? 'Expand' : 'Collapse'} ${kind === 'dm' ? 'recent direct messages' : 'connected peers'}`);
            }
            if (summary) {
                if (safeTotal <= 0) {
                    summary.textContent = kind === 'dm' ? 'No recent conversations' : 'No active peers';
                } else if (state === 'expanded' && hasOverflow) {
                    summary.textContent = `Showing all ${safeTotal}`;
                } else if (hasOverflow) {
                    summary.textContent = `Showing top ${SIDEBAR_CARD_PEEK_LIMIT} of ${safeTotal}`;
                } else {
                    summary.textContent = `Showing all ${safeTotal}`;
                }
            }
            if (footer) {
                footer.hidden = state === 'collapsed' || safeTotal <= 0;
            }
            if (expandBtn) {
                expandBtn.hidden = state === 'collapsed' || !hasOverflow;
                expandBtn.innerHTML = state === 'expanded'
                    ? '<i class="bi bi-arrows-collapse"></i><span>Show less</span>'
                    : `<i class="bi bi-arrows-angle-expand"></i><span>View ${safeTotal - SIDEBAR_CARD_PEEK_LIMIT} more</span>`;
                expandBtn.setAttribute('aria-label', state === 'expanded'
                    ? `Show fewer ${kind === 'dm' ? 'direct messages' : 'connected peers'}`
                    : `Show more ${kind === 'dm' ? 'direct messages' : 'connected peers'}`);
            }
        }

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

        function canopyPeerTrustMeta(peerId) {
            if (!peerId || !window.canopyPeerTrust) return null;
            const raw = window.canopyPeerTrust[peerId];
            if (raw === null || raw === undefined || raw === '') return null;
            const score = Number(raw);
            if (!Number.isFinite(score)) return null;
            if (score >= 80) return { score, className: 'safe', label: 'Trusted' };
            if (score >= 60) return { score, className: 'guarded', label: 'Guarded' };
            if (score >= 40) return { score, className: 'restricted', label: 'Limited' };
            return { score, className: 'quarantine', label: 'Untrusted' };
        }

        function syncCanopyPeerTrust(peerTrust) {
            if (!peerTrust || typeof peerTrust !== 'object' || !window.canopyPeerTrust) return;
            Object.keys(peerTrust).forEach(peerId => {
                window.canopyPeerTrust[peerId] = peerTrust[peerId];
            });
        }

        function syncCanopyPeerProfiles(peerProfiles) {
            if (!peerProfiles || typeof peerProfiles !== 'object' || !window.canopyPeerProfiles) return;
            Object.keys(peerProfiles).forEach(peerId => {
                window.canopyPeerProfiles[peerId] = peerProfiles[peerId];
            });
        }

        function sidebarPeerDisplayName(peerId) {
            return window.canopyPeerDisplayName ? window.canopyPeerDisplayName(peerId) : (peerId || '').slice(0, 12);
        }

        function sidebarPeerTrustHref(peerId) {
            const routes = (window.CANOPY_VARS && window.CANOPY_VARS.urls) || {};
            const base = routes.trustNetwork || '/trust';
            const cleanPeerId = String(peerId || '').trim();
            if (!cleanPeerId) return base;
            return `${base}#trust-peer-${encodeURIComponent(cleanPeerId)}`;
        }

        function createSidebarPeerElement(peerRecord) {
            const peerId = (peerRecord && peerRecord.peerId) ? peerRecord.peerId : peerRecord;
            const displayName = (peerRecord && peerRecord.displayName)
                ? peerRecord.displayName
                : (sidebarPeerDisplayName(peerId) || (peerId || '').slice(0, 12));
            const trustMeta = canopyPeerTrustMeta(peerId);
            const peerEl = document.createElement('a');
            peerEl.className = 'sidebar-peer';
            peerEl.setAttribute('data-peer-id', peerId);
            peerEl.href = sidebarPeerTrustHref(peerId);
            peerEl.title = `Open ${displayName} in Trust Network`;
            peerEl.setAttribute('aria-label', `Open ${displayName} in Trust Network`);

            const avatarWrap = document.createElement('div');
            avatarWrap.className = 'sidebar-peer-avatar';
            const avatarSrc = (peerRecord && peerRecord.avatarSrc)
                ? peerRecord.avatarSrc
                : (window.canopyPeerAvatarSrc ? window.canopyPeerAvatarSrc(peerId) : null);
            if (avatarSrc) {
                const img = document.createElement('img');
                img.src = avatarSrc;
                img.alt = displayName;
                avatarWrap.appendChild(img);
            } else {
                const initial = document.createElement('span');
                initial.textContent = canopyInitial(displayName);
                avatarWrap.appendChild(initial);
            }

            const meta = document.createElement('div');
            meta.className = 'sidebar-peer-meta';
            const name = document.createElement('div');
            name.className = 'sidebar-peer-name';
            name.textContent = displayName;
            const peerIdEl = document.createElement('div');
            peerIdEl.className = 'sidebar-peer-id';
            peerIdEl.textContent = peerId ? `${peerId.slice(0, 12)}...` : '';
            meta.appendChild(name);
            meta.appendChild(peerIdEl);

            peerEl.appendChild(avatarWrap);
            peerEl.appendChild(meta);

            if (trustMeta) {
                const pill = document.createElement('span');
                pill.className = `trust-pill ${trustMeta.className}`;
                pill.textContent = trustMeta.label;
                peerEl.appendChild(pill);
            }

            return peerEl;
        }

        function setSidebarPeerCount(count) {
            const headerBadge = document.getElementById('header-peer-count');
            const sidebarCount = document.getElementById('sidebar-peer-count');
            const normalized = Math.max(0, Number(count) || 0);
            if (sidebarCount) sidebarCount.textContent = String(normalized);
            if (headerBadge) {
                headerBadge.textContent = String(normalized);
                headerBadge.classList.toggle('bg-success', normalized > 0);
                headerBadge.classList.toggle('bg-secondary', normalized <= 0);
            }
        }

        const canopySidebarPeerState = {
            seeded: false,
            peers: new Map(),
            totalCount: 0,
            currentRev: canopyInitialPeerRev || '',
        };
        const canopySidebarPeerPollState = {
            failureCount: 0,
            nextDelayMs: 2500,
            timerId: null,
        };

        function seedSidebarPeerState() {
            if (canopySidebarPeerState.seeded) return;
            const nowSeconds = Date.now() / 1000;
            const domPeerIds = Array.from(document.querySelectorAll('.sidebar-peer[data-peer-id]'))
                .map((el, index) => ({
                    peerId: el.getAttribute('data-peer-id') || '',
                    order: index,
                    displayName: ((el.querySelector('.sidebar-peer-name') || {}).textContent || '').trim(),
                    avatarSrc: (el.querySelector('.sidebar-peer-avatar img') || {}).getAttribute
                        ? (el.querySelector('.sidebar-peer-avatar img').getAttribute('src') || '')
                        : '',
                }))
                .filter(item => item.peerId);
            if (domPeerIds.length) {
                domPeerIds.forEach(item => {
                    canopySidebarPeerState.peers.set(item.peerId, {
                        peerId: item.peerId,
                        active: true,
                        missCount: 0,
                        lastSeenAt: nowSeconds,
                        order: item.order,
                        displayName: item.displayName || item.peerId.slice(0, 12),
                        avatarSrc: item.avatarSrc || null,
                    });
                });
            } else if (Array.isArray(window.canopyInitialConnectedPeers)) {
                window.canopyInitialConnectedPeers.forEach((peerId, index) => {
                    if (!peerId) return;
                    canopySidebarPeerState.peers.set(peerId, {
                        peerId,
                        active: true,
                        missCount: 0,
                        lastSeenAt: nowSeconds,
                        order: index,
                        displayName: sidebarPeerDisplayName(peerId) || peerId.slice(0, 12),
                        avatarSrc: window.canopyPeerAvatarSrc ? window.canopyPeerAvatarSrc(peerId) : null,
                    });
                });
            }
            canopySidebarPeerState.seeded = true;
        }

        function renderSidebarPeers() {
            const listEl = document.getElementById('sidebar-peer-list');
            if (!listEl) return;
            const activePeers = Array.from(canopySidebarPeerState.peers.values())
                .filter(record => record && record.active)
                .sort((a, b) => {
                    const orderDiff = (a.order || 0) - (b.order || 0);
                    if (orderDiff !== 0) return orderDiff;
                    return String(a.peerId || '').localeCompare(String(b.peerId || ''));
                });

            listEl.innerHTML = '';
            if (!activePeers.length) {
                const empty = document.createElement('div');
                empty.className = 'sidebar-peer-empty';
                empty.textContent = 'No active peers';
                listEl.appendChild(empty);
                setSidebarPeerCount(0);
                updateSidebarCardChrome('peers', 0);
                return;
            }

            const visiblePeers = visibleSidebarCardItems('peers', activePeers);
            const peerFrag = document.createDocumentFragment();
            visiblePeers.forEach(record => {
                peerFrag.appendChild(createSidebarPeerElement(record));
            });
            listEl.appendChild(peerFrag);
            setSidebarPeerCount(activePeers.length);
            updateSidebarCardChrome('peers', activePeers.length);
            renderSidebarPeerModalList();
        }

        function setSidebarPeerPollingStatus(message, isDegraded = false) {
            const summaryEl = document.getElementById('sidebar-peers-summary');
            const subtitleEl = document.getElementById('sidebar-peers-modal-subtitle');
            const text = String(message || '').trim() || 'Showing up to five peers';
            if (summaryEl) {
                summaryEl.textContent = text;
                summaryEl.classList.toggle('text-warning', !!isDegraded);
            }
            if (subtitleEl) {
                subtitleEl.textContent = isDegraded ? text : 'Mesh connections on this node';
                subtitleEl.classList.toggle('text-warning', !!isDegraded);
            }
        }

        window.syncCanopySidebarPeers = function(payload) {
            seedSidebarPeerState();
            if (!payload || typeof payload !== 'object') {
                renderSidebarPeers();
                return;
            }
            if (payload.peer_changed === false) {
                if (payload.peer_rev) {
                    canopySidebarPeerState.currentRev = String(payload.peer_rev || '');
                }
                if (typeof payload.connected_peer_count === 'number') {
                    canopySidebarPeerState.totalCount = Math.max(0, Number(payload.connected_peer_count) || 0);
                }
                const hasPeerSnapshot = Array.isArray(payload.connected_peer_ids)
                    || (payload.peers && typeof payload.peers === 'object' && Object.keys(payload.peers).length > 0);
                if (!hasPeerSnapshot) {
                    return;
                }
            }

            syncCanopyPeerTrust(payload.peer_trust);
            syncCanopyPeerProfiles(payload.peer_profiles);
            if (payload.peer_rev) {
                canopySidebarPeerState.currentRev = String(payload.peer_rev || '');
            }

            const nowSeconds = Number(payload.server_time) || (Date.now() / 1000);
            const connectedPeerIds = Array.isArray(payload.connected_peer_ids)
                ? payload.connected_peer_ids.filter(Boolean)
                : Object.keys(payload.peers || {});
            canopySidebarPeerState.totalCount = Math.max(
                0,
                Number(payload.connected_peer_count || connectedPeerIds.length) || connectedPeerIds.length
            );

            const seenNow = new Set(connectedPeerIds);

            connectedPeerIds.forEach((peerId, index) => {
                const existing = canopySidebarPeerState.peers.get(peerId) || { peerId };
                existing.active = true;
                existing.missCount = 0;
                existing.lastSeenAt = nowSeconds;
                existing.order = index;
                if (!existing.displayName) {
                    existing.displayName = sidebarPeerDisplayName(peerId) || peerId.slice(0, 12);
                }
                if (!existing.avatarSrc && window.canopyPeerAvatarSrc) {
                    existing.avatarSrc = window.canopyPeerAvatarSrc(peerId);
                }
                canopySidebarPeerState.peers.set(peerId, existing);
            });

            canopySidebarPeerState.peers.forEach((record, peerId) => {
                if (seenNow.has(peerId)) return;
                record.missCount = (record.missCount || 0) + 1;
                const secondsMissing = Math.max(0, nowSeconds - Number(record.lastSeenAt || 0));
                if ((record.missCount >= 3) && secondsMissing >= 5) {
                    record.active = false;
                }
                canopySidebarPeerState.peers.set(peerId, record);
            });

            renderSidebarPeers();
        };

        document.addEventListener('DOMContentLoaded', function() {
            seedSidebarPeerState();
            canopyRenderSidebarDmContacts(canopySidebarDmState.contacts);
            renderSidebarPeers();
            const dmToggleBtn = document.getElementById('sidebar-dm-toggle');
            if (dmToggleBtn) {
                dmToggleBtn.addEventListener('click', () => toggleSidebarCardCollapsed('dm'));
            }
            const dmExpandBtn = document.getElementById('sidebar-dm-expand-btn');
            if (dmExpandBtn) {
                dmExpandBtn.addEventListener('click', () => toggleSidebarCardExpansion('dm', canopySidebarDmState.contacts.length));
            }
            const deckInboxBtn = document.getElementById('deckInboxNavButton');
            if (deckInboxBtn) {
                deckInboxBtn.addEventListener('click', (event) => {
                    event.preventDefault();
                    if (window.location.pathname === '/messages') {
                        const thread = document.getElementById('dm-thread-scroll') || document.querySelector('.messages-page');
                        if (thread && typeof thread.scrollIntoView === 'function') {
                            thread.scrollIntoView({ behavior: 'smooth', block: 'start' });
                        }
                        const textarea = document.getElementById('messageContent');
                        if (textarea && typeof textarea.focus === 'function') {
                            window.setTimeout(() => textarea.focus(), 180);
                        }
                        return;
                    }
                    if (typeof window.openDeckInbox === 'function') {
                        window.openDeckInbox({});
                    } else {
                        window.location.href = ((window.CANOPY_VARS || {}).urls || {}).messages || '/messages';
                    }
                });
            }
            const peersToggleBtn = document.getElementById('sidebar-peers-toggle');
            if (peersToggleBtn) {
                peersToggleBtn.addEventListener('click', () => toggleSidebarCardCollapsed('peers'));
            }
            const peersExpandBtn = document.getElementById('sidebar-peers-expand-btn');
            if (peersExpandBtn) {
                peersExpandBtn.addEventListener('click', () => toggleSidebarCardExpansion('peers', getActiveSidebarPeers().length));
            }
            const peersOpenModalBtn = document.getElementById('sidebar-peers-open-modal');
            if (peersOpenModalBtn) {
                peersOpenModalBtn.addEventListener('click', openSidebarPeersModal);
            }
            const peerSearch = document.getElementById('sidebar-peers-search');
            if (peerSearch) {
                peerSearch.addEventListener('input', function() {
                    renderSidebarPeerModalList(this.value || '');
                });
            }
        });

        function getActiveSidebarPeers() {
            return Array.from(canopySidebarPeerState.peers.values())
                .filter(record => record && record.active)
                .sort((a, b) => {
                    const orderDiff = (a.order || 0) - (b.order || 0);
                    if (orderDiff !== 0) return orderDiff;
                    return String(a.displayName || a.peerId || '').localeCompare(String(b.displayName || b.peerId || ''));
                });
        }

        function renderSidebarPeerModalList(filterText) {
            const listEl = document.getElementById('sidebar-peers-modal-list');
            const countEl = document.getElementById('sidebar-peers-modal-count');
            const overflowEl = document.getElementById('sidebar-peers-modal-overflow');
            if (!listEl) return;
            const needle = String(filterText || '').trim().toLowerCase();
            const activePeers = getActiveSidebarPeers();
            const matches = needle
                ? activePeers.filter(record => {
                    const name = String(record.displayName || '').toLowerCase();
                    const peerId = String(record.peerId || '').toLowerCase();
                    return name.includes(needle) || peerId.includes(needle);
                })
                : activePeers;
            listEl.innerHTML = '';
            if (countEl) {
                countEl.textContent = `${activePeers.length} connected`;
            }
            if (overflowEl) {
                overflowEl.textContent = needle ? `${matches.length} match${matches.length === 1 ? '' : 'es'}` : '';
            }
            if (!matches.length) {
                const empty = document.createElement('div');
                empty.className = 'sidebar-peer-empty';
                empty.textContent = needle ? 'No peers match this search' : 'No active peers';
                listEl.appendChild(empty);
                return;
            }
            matches.forEach(record => {
                listEl.appendChild(createSidebarPeerElement(record));
            });
        }

        function openSidebarPeersModal() {
            const modalEl = document.getElementById('sidebarPeersModal');
            if (!modalEl || typeof bootstrap === 'undefined' || !bootstrap.Modal) return;
            const search = document.getElementById('sidebar-peers-search');
            if (search) search.value = '';
            renderSidebarPeerModalList('');
            bootstrap.Modal.getOrCreateInstance(modalEl).show();
        }

        function canopySidebarDmHref(contact) {
            const routes = (window.CANOPY_VARS && window.CANOPY_VARS.urls) || {};
            const base = routes.messages || '/messages';
            const explicitHref = contact && contact.href ? String(contact.href).trim() : '';
            if (explicitHref) return explicitHref;
            const groupId = contact && (contact.canonical_group_key || contact.group_id)
                ? String(contact.canonical_group_key || contact.group_id).trim()
                : '';
            if (groupId) {
                const url = new URL(base, window.location.origin);
                url.searchParams.set('group', groupId);
                const targetMessageId = contact && contact.target_message_id ? String(contact.target_message_id).trim() : '';
                return `${url.pathname}${url.search}${targetMessageId ? `#message-${targetMessageId}` : ''}`;
            }
            const userId = contact && contact.user_id ? String(contact.user_id).trim() : '';
            if (!userId) return base;
            const url = new URL(base, window.location.origin);
            url.searchParams.set('with', userId);
            const targetMessageId = contact && contact.target_message_id ? String(contact.target_message_id).trim() : '';
            return `${url.pathname}${url.search}${targetMessageId ? `#message-${targetMessageId}` : ''}`;
        }

        const canopySidebarDmState = {
            contacts: Array.isArray(canopyInitialRecentDmContacts) ? canopyInitialRecentDmContacts.slice(0) : [],
            currentRev: canopyInitialDmRev || '',
            currentEventCursor: Number.isFinite(canopyInitialDmEventCursor) ? canopyInitialDmEventCursor : 0,
            snapshotInFlight: false,
            queuedSnapshot: false,
        };

        function updateDeckInboxUnreadBadge(count) {
            const badge = document.getElementById('deckInboxUnreadBadge');
            if (!badge) return;
            const normalized = Math.max(0, Number(count) || 0);
            badge.textContent = normalized > 99 ? '99+' : String(normalized);
            badge.style.display = normalized > 0 ? '' : 'none';
        }

        function canopyRenderSidebarDmContacts(contacts) {
            const listEl = document.getElementById('sidebar-dm-list');
            const totalEl = document.getElementById('sidebar-dm-unread-total');
            if (!listEl) return;

            const normalized = Array.isArray(contacts) ? contacts.filter(Boolean) : [];
            const visibleContacts = visibleSidebarCardItems('dm', normalized);
            const totalUnread = normalized.reduce((sum, contact) => sum + Math.max(0, Number(contact && contact.unread_count) || 0), 0);
            const globalUnread = canopySidebarAttentionState && canopySidebarAttentionState.summary
                ? Math.max(0, Number(canopySidebarAttentionState.summary.messages || 0) || 0)
                : 0;
            if (totalEl) totalEl.textContent = String(totalUnread);
            updateDeckInboxUnreadBadge(Math.max(totalUnread, globalUnread));

            // Render-key diffing: skip DOM writes when data is unchanged
            const dmRenderKey = visibleContacts.map(c =>
                `${c.kind || 'direct'}:${c.user_id || ''}:${c.canonical_group_key || c.group_id || ''}:${c.unread_count}:${c.status_state}:${c.latest_preview}:${c.latest_message_at}`
            ).join('|');
            if (listEl.__canopyDmRenderKey === dmRenderKey && listEl.childElementCount > 0) {
                updateSidebarCardChrome('dm', normalized.length);
                return;
            }
            listEl.__canopyDmRenderKey = dmRenderKey;

            listEl.innerHTML = '';
            if (!normalized.length) {
                const empty = document.createElement('div');
                empty.className = 'sidebar-peer-empty';
                empty.textContent = 'No recent messages';
                listEl.appendChild(empty);
                updateSidebarCardChrome('dm', 0);
                return;
            }

            const dmFrag = document.createDocumentFragment();
            visibleContacts.forEach(contact => {
                const link = document.createElement('a');
                link.className = 'sidebar-dm-contact';
                if (Number(contact.unread_count) > 0) {
                    link.classList.add('unread');
                }
                link.href = canopySidebarDmHref(contact);
                link.setAttribute('data-dm-user-id', contact.user_id || '');
                link.setAttribute('data-dm-kind', contact.kind || 'direct');
                link.setAttribute('data-dm-group-id', contact.canonical_group_key || contact.group_id || '');
                link.setAttribute('data-dm-target-message-id', contact.target_message_id || '');
                link.setAttribute('data-dm-open-deck', '1');
                link.title = contact.display_name || contact.username || contact.user_id || 'Message';

                const avatarWrap = document.createElement('div');
                avatarWrap.className = 'sidebar-dm-avatar-wrap';

                const avatar = document.createElement('div');
                avatar.className = 'sidebar-dm-avatar';
                if (contact.avatar_url) {
                    const img = document.createElement('img');
                    img.src = contact.avatar_url;
                    img.alt = contact.display_name || contact.username || contact.user_id || 'User';
                    avatar.appendChild(img);
                } else {
                    const initial = document.createElement('span');
                    initial.textContent = canopyInitial(contact.display_name || contact.username || contact.user_id || '?');
                    avatar.appendChild(initial);
                }

                const statusDot = document.createElement('span');
                statusDot.className = `sidebar-dm-status-dot ${contact.status_state || 'offline'}`;
                statusDot.title = contact.status_label || 'Offline';

                avatarWrap.appendChild(avatar);
                avatarWrap.appendChild(statusDot);

                if (Number(contact.unread_count) > 0) {
                    const unread = document.createElement('span');
                    unread.className = 'sidebar-dm-unread';
                    unread.textContent = String(Number(contact.unread_count));
                    avatarWrap.appendChild(unread);
                }

                const meta = document.createElement('div');
                meta.className = 'sidebar-dm-meta';

                const nameRow = document.createElement('div');
                nameRow.className = 'sidebar-dm-name-row';
                const name = document.createElement('div');
                name.className = 'sidebar-dm-name';
                name.textContent = contact.display_name || contact.username || contact.user_id || 'User';
                nameRow.appendChild(name);

                const preview = document.createElement('div');
                preview.className = 'sidebar-dm-preview';
                preview.textContent = contact.latest_preview || 'Message';

                meta.appendChild(nameRow);
                meta.appendChild(preview);

                link.appendChild(avatarWrap);
                link.appendChild(meta);

                const time = document.createElement('div');
                time.className = 'sidebar-dm-time';
                if (contact.latest_message_at) {
                    time.textContent = formatTimestamp(contact.latest_message_at);
                    time.setAttribute('data-timestamp', contact.latest_message_at);
                }
                link.appendChild(time);

                dmFrag.appendChild(link);
            });
            listEl.appendChild(dmFrag);

            updateSidebarCardChrome('dm', normalized.length);
        }

        window.syncCanopySidebarDmContacts = function(payload) {
            if (!payload || typeof payload !== 'object') {
                canopyRenderSidebarDmContacts(canopySidebarDmState.contacts);
                return;
            }
            if (Number(payload.workspace_event_cursor || 0) > Number(canopySidebarDmState.currentEventCursor || 0)) {
                canopySidebarDmState.currentEventCursor = Number(payload.workspace_event_cursor || 0);
            }
            if (payload && payload.dm_rev) {
                canopySidebarDmState.currentRev = String(payload.dm_rev || '');
            }
            if (payload.dm_changed === false) {
                return;
            }
            if (Array.isArray(payload.recent_dm_contacts) && payload.recent_dm_contacts.length) {
                canopySidebarDmState.contacts = payload.recent_dm_contacts.slice(0);
            } else if (Array.isArray(payload.recent_dm_contacts) && payload.recent_dm_contacts.length === 0) {
                canopySidebarDmState.contacts = [];
            }
            canopyRenderSidebarDmContacts(canopySidebarDmState.contacts);
        };

        function requestCanopySidebarDmRefresh(options) {
            const opts = options || {};
            if (canopySidebarDmState.snapshotInFlight) {
                canopySidebarDmState.queuedSnapshot = true;
                return Promise.resolve({ queued: true });
            }

            canopySidebarDmState.snapshotInFlight = true;
            const query = new URLSearchParams();
            if (!opts.force && canopySidebarDmState.currentRev) {
                query.set('dm_rev', String(canopySidebarDmState.currentRev || ''));
            }

            return fetch(`/ajax/sidebar_dm_snapshot${query.toString() ? `?${query.toString()}` : ''}`, {
                headers: {
                    'X-Requested-With': 'XMLHttpRequest'
                }
            })
                .then((res) => {
                    if (!res.ok) {
                        throw new Error(`Sidebar DM snapshot failed (${res.status})`);
                    }
                    return res.json();
                })
                .then((data) => {
                    if (!data || data.success === false) return data || null;
                    if (window.syncCanopySidebarDmContacts) {
                        window.syncCanopySidebarDmContacts(data);
                    }
                    return data;
                })
                .catch(() => null)
                .finally(() => {
                    canopySidebarDmState.snapshotInFlight = false;
                    if (canopySidebarDmState.queuedSnapshot) {
                        canopySidebarDmState.queuedSnapshot = false;
                        window.setTimeout(() => {
                            requestCanopySidebarDmRefresh({ force: false }).catch(() => {});
                        }, 0);
                    }
                });
        }

        window.requestCanopySidebarDmRefresh = requestCanopySidebarDmRefresh;

        document.addEventListener('click', function(event) {
            const link = event.target && event.target.closest ? event.target.closest('.sidebar-dm-contact[data-dm-open-deck]') : null;
            if (!link) return;
            if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            if (window.location.pathname === '/messages') return;
            event.preventDefault();
            const href = link.getAttribute('href') || '';
            const userId = String(link.getAttribute('data-dm-user-id') || '').trim();
            const groupId = String(link.getAttribute('data-dm-group-id') || '').trim();
            const targetMessageId = String(link.getAttribute('data-dm-target-message-id') || '').trim();
            if (typeof window.openDeckInbox === 'function') {
                window.openDeckInbox({ userId, groupId, targetMessageId, targetUrl: href });
            } else {
                window.location.href = href || (((window.CANOPY_VARS || {}).urls || {}).messages || '/messages');
            }
        });

        function formatSidebarUnreadCount(count) {
            const normalized = Math.max(0, Number(count) || 0);
            return normalized > 99 ? '99+' : String(normalized);
        }

        function setSidebarNavUnreadBadge(kind, count) {
            const badge = document.getElementById(`sidebar-nav-${kind}-badge`);
            if (!badge) return;
            const normalized = Math.max(0, Number(count) || 0);
            if (normalized <= 0) {
                badge.hidden = true;
                badge.textContent = '0';
                badge.removeAttribute('aria-label');
                return;
            }
            badge.hidden = false;
            badge.textContent = formatSidebarUnreadCount(normalized);
            badge.setAttribute('aria-label', `${normalized} unread ${kind}`);
        }

        function renderSidebarAttentionSummary(summary) {
            const safeSummary = summary && typeof summary === 'object' ? summary : {};
            setSidebarNavUnreadBadge('messages', safeSummary.messages || 0);
            setSidebarNavUnreadBadge('channels', safeSummary.channels || 0);
            setSidebarNavUnreadBadge('feed', safeSummary.feed || 0);
            updateDeckInboxUnreadBadge(safeSummary.messages || 0);
        }

        const CANOPY_TITLE_ATTENTION_PREFIX_RE = /^\(\d{1,4}\+?\)\s+/;
        const canopyBaseDocumentTitle = (() => {
            const rawTitle = String(document.title || 'Canopy').trim() || 'Canopy';
            return rawTitle.replace(CANOPY_TITLE_ATTENTION_PREFIX_RE, '') || 'Canopy';
        })();

        function countCanopyAttentionSummaryTotal(summary) {
            const safeSummary = summary && typeof summary === 'object' ? summary : {};
            const explicitTotal = Math.max(0, Number(safeSummary.total || 0) || 0);
            // Match the aggregate unread buckets. Mentions/inbox/review are tracked separately
            // in the attention center and can already be reflected inside these totals.
            const componentTotal = [
                safeSummary.messages,
                safeSummary.channels,
                safeSummary.feed,
            ].reduce((sum, value) => sum + Math.max(0, Number(value || 0) || 0), 0);
            return Math.max(explicitTotal, componentTotal);
        }

        function syncCanopyAttentionDocumentTitle(items) {
            const summaryCount = countCanopyAttentionSummaryTotal(canopySidebarAttentionState.summary);
            const itemCount = countUnseenCanopyAttentionItems(
                Array.isArray(items) ? items : canopySidebarAttentionState.items
            );
            const totalCount = Math.max(summaryCount, itemCount);
            if (totalCount > 0) {
                const label = totalCount > 999 ? '999+' : String(totalCount);
                document.title = `(${label}) ${canopyBaseDocumentTitle}`;
            } else {
                document.title = canopyBaseDocumentTitle;
            }
        }
        window.syncCanopyAttentionDocumentTitle = syncCanopyAttentionDocumentTitle;

        const canopySidebarAttentionState = {
            currentSummaryRev: canopyInitialAttentionRev || '',
            currentActivityRev: canopyInitialAttentionActivityRev || '',
            summary: {
                messages: Math.max(0, Number(canopyInitialAttentionSummary.messages || 0)),
                channels: Math.max(0, Number(canopyInitialAttentionSummary.channels || 0)),
                feed: Math.max(0, Number(canopyInitialAttentionSummary.feed || 0)),
                mention_count: Math.max(0, Number(canopyInitialAttentionSummary.mention_count || 0)),
                pending_review_count: Math.max(0, Number(canopyInitialAttentionSummary.pending_review_count || 0)),
                total: Math.max(0, Number(canopyInitialAttentionSummary.total || 0)),
            },
            items: Array.isArray(canopyInitialAttentionItems) ? canopyInitialAttentionItems.slice(0) : [],
            currentEventCursor: Math.max(
                Number.isFinite(canopyInitialAttentionEventCursor) ? canopyInitialAttentionEventCursor : 0,
                Number.isFinite(canopyInitialDmEventCursor) ? canopyInitialDmEventCursor : 0
            ),
            inFlight: false,
            queued: false,
            pollInFlight: false,
            pollHandle: null,
            safetyHandle: null,
            listenersAttached: false,
            visibilityFocusDebounceHandle: null,
        };

        const canopyAttentionDismissStorageKey = (() => {
            const userId = window.CANOPY_VARS ? String(window.CANOPY_VARS.userId || 'local_user').trim() : 'local_user';
            return `canopy.attention.dismissedThrough.${userId || 'local_user'}`;
        })();
        const canopyAttentionSeenStorageKey = (() => {
            const userId = window.CANOPY_VARS ? String(window.CANOPY_VARS.userId || 'local_user').trim() : 'local_user';
            return `canopy.attention.seenThrough.${userId || 'local_user'}`;
        })();

        const CANOPY_ATTENTION_FILTER_DEFS = [
            { key: 'mention', label: 'Mentions', icon: 'bi-at' },
            { key: 'inbox', label: 'Inbox', icon: 'bi-inbox' },
            { key: 'dm', label: 'DMs', icon: 'bi-chat-dots' },
            { key: 'channel', label: 'Channels', icon: 'bi-hash' },
            { key: 'feed', label: 'Feed', icon: 'bi-rss' },
        ];
        const CANOPY_ATTENTION_FILTER_MAP = CANOPY_ATTENTION_FILTER_DEFS.reduce((acc, def) => {
            acc[def.key] = def;
            return acc;
        }, {});
        const canopyAttentionFilterStorageKey = (() => {
            const userId = window.CANOPY_VARS ? String(window.CANOPY_VARS.userId || 'local_user').trim() : 'local_user';
            return `canopy.attention.filters.${userId || 'local_user'}`;
        })();

        function defaultCanopyAttentionFilters() {
            return {
                mention: false,
                inbox: true,
                dm: false,
                channel: false,
                feed: false,
            };
        }

        function normalizeCanopyAttentionFilters(raw) {
            const defaults = defaultCanopyAttentionFilters();
            const normalized = {};
            const source = raw && typeof raw === 'object' ? raw : null;
            CANOPY_ATTENTION_FILTER_DEFS.forEach((def) => {
                if (source && Object.prototype.hasOwnProperty.call(source, def.key)) {
                    normalized[def.key] = source[def.key] !== false;
                } else {
                    normalized[def.key] = defaults[def.key] === true;
                }
            });
            return normalized;
        }

        function isDefaultCanopyAttentionFilters(filters) {
            const defaults = defaultCanopyAttentionFilters();
            const normalized = normalizeCanopyAttentionFilters(filters);
            return CANOPY_ATTENTION_FILTER_DEFS.every((def) => normalized[def.key] === defaults[def.key]);
        }

        function loadCanopyAttentionFilters() {
            try {
                const raw = window.localStorage ? window.localStorage.getItem(canopyAttentionFilterStorageKey) : null;
                if (!raw) return normalizeCanopyAttentionFilters(null);
                return normalizeCanopyAttentionFilters(JSON.parse(raw));
            } catch (_) {
                return normalizeCanopyAttentionFilters(null);
            }
        }

        function saveCanopyAttentionFilters(filters) {
            const normalized = normalizeCanopyAttentionFilters(filters);
            canopySidebarAttentionState.filters = normalized;
            try {
                if (window.localStorage) {
                    window.localStorage.setItem(canopyAttentionFilterStorageKey, JSON.stringify(normalized));
                }
            } catch (_) {}
            return normalized;
        }

        function canopyAttentionFilterKeyForItem(item) {
            const kind = String(item && item.kind || '').trim();
            if (kind === 'channel-state') return 'channel';
            return kind;
        }

        function loadCanopyAttentionDismissCursor() {
            try {
                const raw = window.localStorage ? window.localStorage.getItem(canopyAttentionDismissStorageKey) : null;
                return Math.max(0, Number(raw || 0) || 0);
            } catch (_) {
                return 0;
            }
        }

        function saveCanopyAttentionDismissCursor(value) {
            const normalized = Math.max(0, Number(value || 0) || 0);
            canopySidebarAttentionState.dismissedThroughCursor = normalized;
            try {
                if (window.localStorage) {
                    window.localStorage.setItem(canopyAttentionDismissStorageKey, String(normalized));
                }
            } catch (_) {}
            return normalized;
        }

        function loadCanopyAttentionSeenCursor() {
            try {
                const raw = window.localStorage ? window.localStorage.getItem(canopyAttentionSeenStorageKey) : null;
                return Math.max(0, Number(raw || 0) || 0);
            } catch (_) {
                return 0;
            }
        }

        function saveCanopyAttentionSeenCursor(value) {
            const normalized = Math.max(0, Number(value || 0) || 0);
            canopySidebarAttentionState.seenThroughCursor = normalized;
            try {
                if (window.localStorage) {
                    window.localStorage.setItem(canopyAttentionSeenStorageKey, String(normalized));
                }
            } catch (_) {}
            return normalized;
        }

        function filterCanopyAttentionItems(items) {
            const dismissedThrough = Math.max(0, Number(canopySidebarAttentionState.dismissedThroughCursor || 0) || 0);
            const normalized = Array.isArray(items) ? items.filter(Boolean) : [];
            const filters = normalizeCanopyAttentionFilters(canopySidebarAttentionState.filters);
            return normalized.filter((item) => {
                const seq = Math.max(0, Number(item && item.seq || 0) || 0);
                if (dismissedThrough > 0 && seq <= dismissedThrough) return false;
                const filterKey = canopyAttentionFilterKeyForItem(item);
                if (filterKey && Object.prototype.hasOwnProperty.call(filters, filterKey)) {
                    return filters[filterKey] !== false;
                }
                return true;
            });
        }

        function countUnseenCanopyAttentionItems(items) {
            const seenThrough = Math.max(
                0,
                Number(canopySidebarAttentionState.seenThroughCursor || 0) || 0,
                Number(canopySidebarAttentionState.dismissedThroughCursor || 0) || 0
            );
            const normalized = Array.isArray(items) ? items.filter(Boolean) : [];
            return normalized.reduce((sum, item) => {
                const seq = Math.max(0, Number(item && item.seq || 0) || 0);
                return sum + (seq > seenThrough ? 1 : 0);
            }, 0);
        }

        function attentionItemSortValue(item) {
            const created = String(item && item.created_at || '').trim();
            const seq = Math.max(0, Number(item && item.seq || 0) || 0);
            return { created, seq };
        }

        function compareAttentionItemsDesc(a, b) {
            const left = attentionItemSortValue(a);
            const right = attentionItemSortValue(b);
            if (left.created !== right.created) return right.created.localeCompare(left.created);
            return right.seq - left.seq;
        }

        function groupCanopyAttentionItems(items) {
            const normalized = Array.isArray(items) ? items.filter(Boolean) : [];
            const groups = [];
            const seenKeys = new Set();
            CANOPY_ATTENTION_FILTER_DEFS.forEach((def) => {
                const sectionItems = normalized
                    .filter((item) => canopyAttentionFilterKeyForItem(item) === def.key)
                    .sort(compareAttentionItemsDesc);
                if (!sectionItems.length) return;
                groups.push({
                    key: def.key,
                    label: def.label,
                    icon: def.icon,
                    items: sectionItems,
                    latestCreatedAt: String(sectionItems[0] && sectionItems[0].created_at || ''),
                    latestSeq: Math.max(0, Number(sectionItems[0] && sectionItems[0].seq || 0) || 0),
                });
                seenKeys.add(def.key);
            });
            const otherItems = normalized
                .filter((item) => !seenKeys.has(canopyAttentionFilterKeyForItem(item)))
                .sort(compareAttentionItemsDesc);
            if (otherItems.length) {
                groups.push({
                    key: 'other',
                    label: 'Other',
                    icon: 'bi-bell',
                    items: otherItems,
                    latestCreatedAt: String(otherItems[0] && otherItems[0].created_at || ''),
                    latestSeq: Math.max(0, Number(otherItems[0] && otherItems[0].seq || 0) || 0),
                });
            }
            groups.sort((a, b) => {
                if (a.latestCreatedAt !== b.latestCreatedAt) return b.latestCreatedAt.localeCompare(a.latestCreatedAt);
                return b.latestSeq - a.latestSeq;
            });
            return groups;
        }

        canopySidebarAttentionState.dismissedThroughCursor = loadCanopyAttentionDismissCursor();
        canopySidebarAttentionState.seenThroughCursor = Math.max(
            canopySidebarAttentionState.dismissedThroughCursor,
            loadCanopyAttentionSeenCursor()
        );
        canopySidebarAttentionState.filters = loadCanopyAttentionFilters();

        const SIDEBAR_ATTENTION_EVENT_TYPES = [
            'dm.message.created',
            'dm.message.edited',
            'dm.message.deleted',
            'dm.message.read',
            'channel.message.created',
            'channel.message.edited',
            'channel.message.deleted',
            'channel.message.read',
            'channel.state.updated',
            'mention.created',
            'mention.acknowledged',
            'inbox.item.created',
            'inbox.item.updated',
            'feed.post.created',
            'feed.post.updated',
            'feed.post.deleted',
        ];

        function requestCanopySidebarAttentionRefresh(options) {
            if (canopySidebarAttentionState.inFlight) {
                canopySidebarAttentionState.queued = true;
                return Promise.resolve({ queued: true });
            }

            canopySidebarAttentionState.inFlight = true;
            const routes = (window.CANOPY_VARS && window.CANOPY_VARS.urls) || {};
            const endpoint = routes.sidebarAttentionSnapshot || '/ajax/sidebar_attention_snapshot';

            return fetch(endpoint, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            })
                .then((res) => {
                    if (!res.ok) throw new Error(`Sidebar attention snapshot failed (${res.status})`);
                    return res.json();
                })
                .then((data) => {
                    if (!data || data.success === false) return data || null;
                    if (data.summary_rev) canopySidebarAttentionState.currentSummaryRev = String(data.summary_rev || '');
                    if (data.activity_rev) canopySidebarAttentionState.currentActivityRev = String(data.activity_rev || '');
                    if (Number(data.workspace_event_cursor || 0) > Number(canopySidebarAttentionState.currentEventCursor || 0)) {
                        canopySidebarAttentionState.currentEventCursor = Number(data.workspace_event_cursor || 0);
                    }
                    const summary = data.summary && typeof data.summary === 'object' ? data.summary : {};
                    canopySidebarAttentionState.summary = {
                        messages: Math.max(0, Number(summary.messages || 0)),
                        channels: Math.max(0, Number(summary.channels || 0)),
                        feed: Math.max(0, Number(summary.feed || 0)),
                        mention_count: Math.max(0, Number(summary.mention_count || 0)),
                        pending_review_count: Math.max(0, Number(summary.pending_review_count || 0)),
                        total: Math.max(0, Number(summary.total || 0)),
                    };
                    canopySidebarAttentionState.items = Array.isArray(data.items) ? data.items.slice(0) : [];
                    renderSidebarAttentionSummary(canopySidebarAttentionState.summary);
                    syncCanopyAttentionDocumentTitle();
                    if (window.renderCanopyAttentionBell) {
                        window.renderCanopyAttentionBell(filterCanopyAttentionItems(canopySidebarAttentionState.items));
                    }
                    try {
                        window.dispatchEvent(new CustomEvent('canopy:attention-updated', {
                            detail: {
                                summary: canopySidebarAttentionState.summary,
                                items: canopySidebarAttentionState.items,
                                rev: String(data.summary_rev || ''),
                            }
                        }));
                    } catch (error) {
                        // no-op
                    }
                    return data;
                })
                .catch(() => null)
                .finally(() => {
                    canopySidebarAttentionState.inFlight = false;
                    if (canopySidebarAttentionState.queued) {
                        canopySidebarAttentionState.queued = false;
                        window.setTimeout(() => {
                            requestCanopySidebarAttentionRefresh({ force: false }).catch(() => {});
                        }, 0);
                    }
                });
        }

        function pollCanopyWorkspaceAttentionEvents() {
            if (canopySidebarAttentionState.pollInFlight) return;
            canopySidebarAttentionState.pollInFlight = true;
            const query = new URLSearchParams();
            query.set('after_seq', String(Number(canopySidebarAttentionState.currentEventCursor || 0)));
            query.set('limit', '100');
            SIDEBAR_ATTENTION_EVENT_TYPES.forEach((eventType) => query.append('types', eventType));

            fetch(`/api/v1/events?${query.toString()}`, {
                headers: { 'X-Requested-With': 'XMLHttpRequest' }
            })
                .then((res) => {
                    if (!res.ok) throw new Error(`Workspace attention poll failed (${res.status})`);
                    return res.json();
                })
                .then((data) => {
                    if (!data || typeof data !== 'object') return;
                    const nextSeq = Number(data.next_after_seq || 0);
                    if (nextSeq > Number(canopySidebarAttentionState.currentEventCursor || 0)) {
                        canopySidebarAttentionState.currentEventCursor = nextSeq;
                    }
                    if (nextSeq > Number(canopySidebarDmState.currentEventCursor || 0)) {
                        canopySidebarDmState.currentEventCursor = nextSeq;
                    }
                    const items = Array.isArray(data.items) ? data.items : [];
                    if (!items.length) return;
                    requestCanopySidebarDmRefresh({ force: false }).catch(() => {});
                    requestCanopySidebarAttentionRefresh({ force: false }).catch(() => {});
                })
                .catch(() => {})
                .finally(() => {
                    canopySidebarAttentionState.pollInFlight = false;
                });
        }

        function _scheduleCanopyVisibilityFocusRefresh() {
            if (canopySidebarAttentionState.visibilityFocusDebounceHandle) {
                window.clearTimeout(canopySidebarAttentionState.visibilityFocusDebounceHandle);
            }
            canopySidebarAttentionState.visibilityFocusDebounceHandle = window.setTimeout(function() {
                canopySidebarAttentionState.visibilityFocusDebounceHandle = null;
                pollCanopyWorkspaceAttentionEvents();
                requestCanopySidebarAttentionRefresh({ force: false }).catch(() => {});
                requestCanopySidebarDmRefresh({ force: false }).catch(() => {});
            }, 150);
        }

        function startCanopyWorkspaceAttentionPolling() {
            renderSidebarAttentionSummary(canopySidebarAttentionState.summary);
            canopyRenderSidebarDmContacts(canopySidebarDmState.contacts);
            syncCanopyAttentionDocumentTitle();
            if (window.renderCanopyAttentionBell) {
                window.renderCanopyAttentionBell(filterCanopyAttentionItems(canopySidebarAttentionState.items));
            }
            if (canopySidebarAttentionState.pollHandle) window.clearInterval(canopySidebarAttentionState.pollHandle);
            if (canopySidebarAttentionState.safetyHandle) window.clearInterval(canopySidebarAttentionState.safetyHandle);
            requestCanopySidebarAttentionRefresh({ force: false }).catch(() => {});
            requestCanopySidebarDmRefresh({ force: false }).catch(() => {});
            pollCanopyWorkspaceAttentionEvents();
            canopySidebarAttentionState.pollHandle = window.setInterval(pollCanopyWorkspaceAttentionEvents, 5000);
            canopySidebarAttentionState.safetyHandle = window.setInterval(() => {
                requestCanopySidebarAttentionRefresh({ force: false }).catch(() => {});
                requestCanopySidebarDmRefresh({ force: false }).catch(() => {});
            }, 30000);
            if (!canopySidebarAttentionState.listenersAttached) {
                canopySidebarAttentionState.listenersAttached = true;
                document.addEventListener('visibilitychange', function() {
                    if (document.visibilityState === 'visible') {
                        _scheduleCanopyVisibilityFocusRefresh();
                    }
                });
                window.addEventListener('focus', function() {
                    _scheduleCanopyVisibilityFocusRefresh();
                });
            }
        }

        window.requestCanopySidebarAttentionRefresh = requestCanopySidebarAttentionRefresh;

        document.addEventListener('DOMContentLoaded', function() {
            startCanopyWorkspaceAttentionPolling();
        });

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

        function escapeEmbedHtml(value) {
            return String(value || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;');
        }

        function escapeEmbedAttr(value) {
            return escapeEmbedHtml(value)
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        const CANOPY_DECK_WIDGET_RENDER_MODES = new Set(['iframe', 'card', 'stream_summary', 'module_runtime']);
        const CANOPY_DECK_WIDGET_TYPES = new Set([
            'map',
            'chart',
            'media_embed',
            'story',
            'media_stream',
            'telemetry_panel',
            'module_surface',
        ]);
        const CANOPY_DECK_WIDGET_CALLBACKS = new Set(['open_stream_workspace']);
        const CANOPY_DECK_WIDGET_ACTION_RISKS = new Set(['view', 'low']);
        const CANOPY_DECK_WIDGET_ACTION_SCOPES = new Set(['source', 'station']);
        const CANOPY_DECK_WIDGET_HUMAN_GATES = new Set(['none', 'recommended', 'required']);
        const CANOPY_DECK_WIDGET_STATION_KINDS = new Set([
            'source_bundle',
            'reference_surface',
            'stream_station',
            'telemetry_station',
            'station_surface',
        ]);
        const CANOPY_DECK_WIDGET_DOMAINS = new Set([
            'media',
            'sensor',
            'security',
            'news',
            'radio',
            'tv',
            'education',
            'mapping',
            'market',
            'operations',
            'general',
        ]);
        const CANOPY_MODULE_BUNDLE_FORMATS = new Set(['single_html']);
        const CANOPY_MODULE_CAPABILITIES = new Set([
            'source.read',
            'source.attachments.read',
            'source.snapshot',
            'deck.return',
            'deck.close',
            'deck.media.observe',
            'clipboard.write',
            'module.render.webgl',
            'module.render.wasm',
            'module.storage.local',
            'module.storage.module',
        ]);
        const CANOPY_MODULE_REVIEWED_CAPABILITIES = new Set([
            'module.render.webgl',
            'module.render.wasm',
            'source.attachments.read',
        ]);
        const CANOPY_MODULE_ATTACHMENT_MAX_LIST = 32;
        const CANOPY_MODULE_ATTACHMENT_MAX_TEXT_BYTES = 1024 * 1024;
        const CANOPY_MODULE_ATTACHMENT_MAX_BINARY_BYTES = 4 * 1024 * 1024;
        const CANOPY_MODULE_ATTACHMENT_DENIED_EXTENSIONS = new Set(['.canopy-module.html', '.canopy-module.htm', '.html', '.htm', '.js', '.mjs', '.cjs', '.css', '.svg']);
        const CANOPY_MODULE_ATTACHMENT_DENIED_MIME_TYPES = new Set(['text/html', 'application/xhtml+xml', 'text/javascript', 'application/javascript', 'application/ecmascript', 'text/css', 'image/svg+xml']);
        const CANOPY_MODULE_ATTACHMENT_TEXT_EXTENSIONS = new Set(['.txt', '.text', '.md', '.markdown', '.csv', '.tsv', '.json', '.jsonl', '.ndjson', '.yaml', '.yml', '.xml', '.log', '.ini', '.cfg', '.toml', '.tex', '.cube', '.cub', '.xyz', '.pdb', '.ent', '.cif', '.mmcif', '.sdf', '.mol', '.mol2', '.dx', '.grd', '.vtk', '.obj', '.ply']);
        const CANOPY_MODULE_ATTACHMENT_BINARY_EXTENSIONS = new Set(['.bin', '.dat', '.npy', '.npz', '.h5', '.hdf5', '.stl', '.glb', '.gltf', '.vtu', '.vtp', '.gz', '.gzip']);
        const CANOPY_MODULE_ATTACHMENT_BINARY_MIME_TYPES = new Set(['application/gzip', 'application/x-gzip']);
        const CANOPY_MODULE_STORAGE_PREFIX = 'canopy-module:v2';
        const CANOPY_MODULE_STORAGE_MAX_KEY_BYTES = 128;
        const CANOPY_MODULE_STORAGE_MAX_VALUE_BYTES = 32 * 1024;
        const CANOPY_MODULE_STORAGE_MAX_SCOPE_BYTES = 128 * 1024;
        const CANOPY_MODULE_STORAGE_MAX_KEYS_PER_SCOPE = 80;
        const CANOPY_MODULE_STORAGE_SCOPES = new Set(['source', 'module']);
        const CANOPY_DECK_IFRAME_HOSTS = new Set([
            'www.youtube-nocookie.com',
            'player.vimeo.com',
            'open.spotify.com',
            'w.soundcloud.com',
            'www.google.com',
            'www.openstreetmap.org',
            's.tradingview.com',
            'www.loom.com',
        ]);
        const CANOPY_DECK_EXTERNAL_HOSTS = new Set([
            'www.youtube.com',
            'youtu.be',
            'vimeo.com',
            'www.loom.com',
            'loom.com',
            'open.spotify.com',
            'www.soundcloud.com',
            'soundcloud.com',
            'www.google.com',
            'maps.google.com',
            'www.openstreetmap.org',
            'www.tradingview.com',
            'tradingview.com',
            'x.com',
            'www.x.com',
            'twitter.com',
            'www.twitter.com',
        ]);

        function normalizeDeckWidgetText(value, maxLength = 240) {
            const normalized = String(value == null ? '' : value).trim().replace(/\s+/g, ' ');
            if (!normalized) return '';
            return normalized.slice(0, maxLength);
        }

        function sanitizeDeckWidgetUrl(rawUrl, allowedHosts) {
            const urlObj = safeUrlParse(rawUrl);
            if (!urlObj) return '';
            const protocol = String(urlObj.protocol || '').toLowerCase();
            const host = String(urlObj.hostname || '').toLowerCase();
            if (protocol !== 'https:' && protocol !== 'http:') return '';
            if (protocol === 'http:' && host !== window.location.hostname) return '';
            if (Array.isArray(allowedHosts) || allowedHosts instanceof Set) {
                const list = allowedHosts instanceof Set ? allowedHosts : new Set(allowedHosts);
                if (!list.has(host)) return '';
            }
            return urlObj.toString();
        }

        function normalizeDeckWidgetBadges(badges) {
            if (!Array.isArray(badges)) return [];
            return badges
                .map((value) => normalizeDeckWidgetText(value, 48))
                .filter(Boolean)
                .slice(0, 6);
        }

        function normalizeDeckWidgetDetails(details) {
            if (!Array.isArray(details)) return [];
            return details
                .map((entry) => {
                    if (!entry || typeof entry !== 'object') return null;
                    const label = normalizeDeckWidgetText(entry.label, 32);
                    const value = normalizeDeckWidgetText(entry.value, 80);
                    if (!label || !value) return null;
                    return { label, value };
                })
                .filter(Boolean)
                .slice(0, 8);
        }

        function normalizeDeckModuleCapabilityList(values) {
            if (!Array.isArray(values)) return [];
            return values
                .map((value) => normalizeDeckWidgetText(value, 48).toLowerCase())
                .filter((value, index, list) => value && CANOPY_MODULE_CAPABILITIES.has(value) && list.indexOf(value) === index)
                .slice(0, 8);
        }

        function splitDeckModuleCapabilityText(value) {
            return String(value == null ? '' : value)
                .split(/[\s,;]+/)
                .map((part) => part.trim())
                .filter(Boolean);
        }

        function mergeDeckModuleCapabilityLists(...lists) {
            const combined = [];
            lists.forEach((list) => {
                if (Array.isArray(list)) {
                    combined.push(...list);
                }
            });
            return normalizeDeckModuleCapabilityList(combined);
        }

        function extractDeckModuleCapabilityDeclarations(bundleHtml) {
            const out = { required: [], optional: [] };
            const rawHtml = String(bundleHtml || '');
            if (!rawHtml || typeof DOMParser === 'undefined') return out;
            const addRequired = (values) => {
                out.required = mergeDeckModuleCapabilityLists(out.required, values);
            };
            const addOptional = (values) => {
                out.optional = mergeDeckModuleCapabilityLists(out.optional, values);
            };
            try {
                const doc = new DOMParser().parseFromString(rawHtml, 'text/html');
                doc.querySelectorAll('meta[name="canopy-module-required-capabilities"]').forEach((meta) => {
                    addRequired(splitDeckModuleCapabilityText(meta.getAttribute('content') || ''));
                });
                doc.querySelectorAll('meta[name="canopy-module-capabilities"], meta[name="canopy-module-optional-capabilities"]').forEach((meta) => {
                    addOptional(splitDeckModuleCapabilityText(meta.getAttribute('content') || ''));
                });
                doc.querySelectorAll('script[type="application/json"][data-canopy-module-manifest]').forEach((script) => {
                    let parsed = null;
                    try {
                        parsed = JSON.parse(script.textContent || '{}');
                    } catch (_) {
                        parsed = null;
                    }
                    if (!parsed || typeof parsed !== 'object') return;
                    const caps = parsed.capabilities && typeof parsed.capabilities === 'object'
                        ? parsed.capabilities
                        : {};
                    addRequired(caps.required || parsed.required_capabilities || []);
                    addOptional(caps.optional || parsed.optional_capabilities || []);
                    if (Array.isArray(parsed.capabilities)) {
                        addOptional(parsed.capabilities);
                    }
                });
            } catch (_) {
                return out;
            }
            return out;
        }

        function sanitizeDeckModuleBundleUrl(rawUrl) {
            const urlObj = safeUrlParse(rawUrl);
            if (!urlObj) return '';
            const protocol = String(urlObj.protocol || '').toLowerCase();
            if (protocol !== 'https:' && protocol !== 'http:') return '';
            if (String(urlObj.origin || '') !== String(window.location.origin || '')) return '';
            const path = String(urlObj.pathname || '');
            if (/^\/static\/modules\/[A-Za-z0-9._-]+$/.test(path)) {
                return `${path}${urlObj.search || ''}${urlObj.hash || ''}`;
            }
            const filesMatch = path.match(/^\/files\/([^/?#]+)$/);
            if (!filesMatch) return '';
            const encSeg = filesMatch[1];
            if (encSeg.includes('/') || /%(?:2f|5c)/i.test(encSeg)) return '';
            let decoded;
            try {
                decoded = decodeURIComponent(encSeg);
            } catch (_) {
                return '';
            }
            if (!decoded || decoded.includes('/') || decoded.includes('\\') || decoded === '.' || decoded === '..') return '';
            if (!/^[A-Za-z0-9_.-]+$/.test(decoded)) return '';
            return `/files/${encodeURIComponent(decoded)}${urlObj.search || ''}${urlObj.hash || ''}`;
        }

        function normalizeDeckModuleRuntime(rawRuntime, title) {
            if (!rawRuntime || typeof rawRuntime !== 'object') return null;
            const format = String(rawRuntime.format || 'single_html').trim().toLowerCase();
            if (!CANOPY_MODULE_BUNDLE_FORMATS.has(format)) return null;
            const rawBundleId = rawRuntime.bundle_file_id != null ? rawRuntime.bundle_file_id : rawRuntime.file_id;
            const bundleFileId = String(rawBundleId == null ? '' : rawBundleId).trim().slice(0, 120);
            const fallbackBundleUrl = bundleFileId ? `/files/${encodeURIComponent(bundleFileId)}` : '';
            const primaryBundleUrl = sanitizeDeckModuleBundleUrl(rawRuntime.bundle_url || '');
            const bundleUrl = primaryBundleUrl || sanitizeDeckModuleBundleUrl(fallbackBundleUrl);
            if (!bundleUrl) return null;
            const moduleType = normalizeDeckWidgetText(rawRuntime.module_type || title || 'module surface', 56) || 'module surface';
            const runtimeLabel = normalizeDeckWidgetText(rawRuntime.runtime_label || 'Canopy Module', 48) || 'Canopy Module';
            const capabilities = rawRuntime.capabilities && typeof rawRuntime.capabilities === 'object'
                ? rawRuntime.capabilities
                : {};
            return {
                format,
                bundle_file_id: bundleFileId,
                bundle_url: bundleUrl,
                module_type: moduleType,
                runtime_label: runtimeLabel,
                capabilities: {
                    required: normalizeDeckModuleCapabilityList(capabilities.required),
                    optional: normalizeDeckModuleCapabilityList(capabilities.optional),
                },
            };
        }

        function defaultDeckWidgetStationSurface(widgetType, providerLabel, title) {
            if (widgetType === 'map') {
                return {
                    kind: 'reference_surface',
                    domain: 'mapping',
                    label: 'Map Surface',
                    summary: 'Shared geographic context bound to this source.',
                    recurring: false,
                    scope: 'source',
                };
            }
            if (widgetType === 'chart') {
                return {
                    kind: 'reference_surface',
                    domain: 'market',
                    label: 'Chart Surface',
                    summary: 'Shared chart context bound to this source.',
                    recurring: false,
                    scope: 'source',
                };
            }
            if (widgetType === 'media_stream') {
                return {
                    kind: 'stream_station',
                    domain: 'media',
                    label: providerLabel || 'Media Station',
                    summary: 'Live operational surface for a shared stream.',
                    recurring: true,
                    scope: 'station',
                };
            }
            if (widgetType === 'telemetry_panel') {
                return {
                    kind: 'telemetry_station',
                    domain: 'sensor',
                    label: providerLabel || 'Telemetry Station',
                    summary: 'Live telemetry surface with bounded operator actions.',
                    recurring: true,
                    scope: 'station',
                };
            }
            if (widgetType === 'module_surface') {
                return {
                    kind: 'station_surface',
                    domain: 'education',
                    label: providerLabel || title || 'Interactive Module',
                    summary: 'Safe executable module bound to this source and opened inside the Canopy deck.',
                    recurring: false,
                    scope: 'source',
                };
            }
            if (widgetType === 'story') {
                return {
                    kind: 'station_surface',
                    domain: 'news',
                    label: providerLabel || title || 'Story Surface',
                    summary: 'Story world combining media, references, and interactive context.',
                    recurring: false,
                    scope: 'source',
                };
            }
            return {
                kind: 'source_bundle',
                domain: 'media',
                label: providerLabel || title || 'Source Bundle',
                summary: 'Typed operational context bound to the source.',
                recurring: false,
                scope: 'source',
            };
        }

        function normalizeDeckWidgetStationSurface(rawSurface, widgetType, providerLabel, title) {
            const fallback = defaultDeckWidgetStationSurface(widgetType, providerLabel, title);
            if (!rawSurface || typeof rawSurface !== 'object') return fallback;
            const kind = String(rawSurface.kind || fallback.kind).trim().toLowerCase();
            const domain = String(rawSurface.domain || fallback.domain).trim().toLowerCase();
            const label = normalizeDeckWidgetText(rawSurface.label || fallback.label, 56) || fallback.label;
            const summary = normalizeDeckWidgetText(rawSurface.summary || fallback.summary, 180) || fallback.summary;
            const scope = String(rawSurface.scope || fallback.scope).trim().toLowerCase();
            return {
                kind: CANOPY_DECK_WIDGET_STATION_KINDS.has(kind) ? kind : fallback.kind,
                domain: CANOPY_DECK_WIDGET_DOMAINS.has(domain) ? domain : fallback.domain,
                label,
                summary,
                recurring: rawSurface.recurring == null ? !!fallback.recurring : !!rawSurface.recurring,
                scope: CANOPY_DECK_WIDGET_ACTION_SCOPES.has(scope) ? scope : fallback.scope,
            };
        }

        function defaultDeckWidgetActionPolicy(widgetType, actions) {
            const hasCallback = Array.isArray(actions) && actions.some((action) => action && action.kind === 'callback');
            return {
                bounded: true,
                max_risk: hasCallback || widgetType === 'media_stream' || widgetType === 'telemetry_panel' ? 'low' : 'view',
                human_gate: 'none',
                audit_label: hasCallback ? 'Bounded actions' : 'View-only actions',
            };
        }

        function normalizeDeckWidgetActionPolicy(rawPolicy, widgetType, actions) {
            const fallback = defaultDeckWidgetActionPolicy(widgetType, actions);
            if (!rawPolicy || typeof rawPolicy !== 'object') return fallback;
            const maxRisk = String(rawPolicy.max_risk || fallback.max_risk).trim().toLowerCase();
            const humanGate = String(rawPolicy.human_gate || fallback.human_gate).trim().toLowerCase();
            const auditLabel = normalizeDeckWidgetText(rawPolicy.audit_label || fallback.audit_label, 56) || fallback.audit_label;
            return {
                bounded: rawPolicy.bounded == null ? true : !!rawPolicy.bounded,
                max_risk: CANOPY_DECK_WIDGET_ACTION_RISKS.has(maxRisk) ? maxRisk : fallback.max_risk,
                human_gate: CANOPY_DECK_WIDGET_HUMAN_GATES.has(humanGate) ? humanGate : fallback.human_gate,
                audit_label: auditLabel,
            };
        }

        function normalizeDeckWidgetSourceBinding(rawBinding) {
            if (!rawBinding || typeof rawBinding !== 'object') {
                return {
                    binding_type: 'source',
                    source_scope: 'source',
                    return_label: 'Return to source',
                };
            }
            const bindingType = normalizeDeckWidgetText(rawBinding.binding_type || 'source', 32).toLowerCase() || 'source';
            const sourceScope = String(rawBinding.source_scope || 'source').trim().toLowerCase();
            const returnLabel = normalizeDeckWidgetText(rawBinding.return_label || 'Return to source', 48) || 'Return to source';
            return {
                binding_type: bindingType,
                source_scope: CANOPY_DECK_WIDGET_ACTION_SCOPES.has(sourceScope) ? sourceScope : 'source',
                return_label: returnLabel,
            };
        }

        function normalizeDeckWidgetActions(actions, actionPolicy) {
            if (!Array.isArray(actions)) return [];
            const policy = actionPolicy || defaultDeckWidgetActionPolicy('media_embed', actions);
            return actions
                .map((action) => {
                    if (!action || typeof action !== 'object') return null;
                    const kind = String(action.kind || '').trim().toLowerCase();
                    const label = normalizeDeckWidgetText(action.label, 32);
                    const icon = /^bi-[a-z0-9-]+$/i.test(String(action.icon || '').trim()) ? String(action.icon).trim() : '';
                    const risk = String(action.risk || (kind === 'callback' ? 'low' : 'view')).trim().toLowerCase();
                    const scope = String(action.scope || (kind === 'callback' ? 'station' : 'source')).trim().toLowerCase();
                    const requiresConfirmation = !!action.requires_confirmation;
                    if (!label) return null;
                    if (!CANOPY_DECK_WIDGET_ACTION_RISKS.has(risk)) return null;
                    if (!CANOPY_DECK_WIDGET_ACTION_SCOPES.has(scope)) return null;
                    if (policy.max_risk === 'view' && risk !== 'view') return null;
                    if (kind === 'external_link') {
                        const url = sanitizeDeckWidgetUrl(action.url, CANOPY_DECK_EXTERNAL_HOSTS);
                        if (!url) return null;
                        return { kind, label, icon, url, risk, scope, requires_confirmation: requiresConfirmation };
                    }
                    if (kind === 'clipboard') {
                        const text = normalizeDeckWidgetText(action.text, 160);
                        if (!text) return null;
                        return { kind, label, icon, text, risk, scope, requires_confirmation: requiresConfirmation };
                    }
                    if (kind === 'callback') {
                        const handler = String(action.handler || '').trim();
                        if (!CANOPY_DECK_WIDGET_CALLBACKS.has(handler)) return null;
                        const args = action.args && typeof action.args === 'object' ? action.args : {};
                        if (handler === 'open_stream_workspace') {
                            const streamId = normalizeDeckWidgetText(args.streamId, 120);
                            const mediaKind = normalizeDeckWidgetText(args.mediaKind, 16).toLowerCase();
                            const slotId = normalizeDeckWidgetText(args.slotId, 120);
                            const streamKind = normalizeDeckWidgetText(args.streamKind, 24).toLowerCase();
                            if (!streamId || !slotId) return null;
                            return {
                                kind,
                                label,
                                icon,
                                handler,
                                risk,
                                scope,
                                requires_confirmation: requiresConfirmation,
                                args: { streamId, mediaKind, slotId, streamKind },
                            };
                        }
                    }
                    return null;
                })
                .filter(Boolean)
                .slice(0, 4);
        }

        function sanitizeDeckWidgetManifest(rawManifest) {
            if (!rawManifest || typeof rawManifest !== 'object') return null;
            const widgetType = String(rawManifest.widget_type || '').trim().toLowerCase();
            const renderMode = String(rawManifest.render_mode || '').trim().toLowerCase();
            if (!CANOPY_DECK_WIDGET_TYPES.has(widgetType)) return null;
            if (!CANOPY_DECK_WIDGET_RENDER_MODES.has(renderMode)) return null;
            const title = normalizeDeckWidgetText(rawManifest.title, 96);
            if (!title) return null;
            const subtitle = normalizeDeckWidgetText(rawManifest.subtitle, 160);
            const providerLabel = normalizeDeckWidgetText(rawManifest.provider_label, 32) || 'Widget';
            const bodyText = normalizeDeckWidgetText(rawManifest.body_text, 420);
            const key = normalizeDeckWidgetText(rawManifest.key, 160) || `${widgetType}:${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
            const icon = /^bi-[a-z0-9-]+$/i.test(String(rawManifest.icon || '').trim()) ? String(rawManifest.icon).trim() : 'bi-grid-3x3-gap';
            const embedUrl = renderMode === 'iframe'
                ? sanitizeDeckWidgetUrl(rawManifest.embed_url, CANOPY_DECK_IFRAME_HOSTS)
                : '';
            const externalUrl = rawManifest.external_url
                ? sanitizeDeckWidgetUrl(rawManifest.external_url, CANOPY_DECK_EXTERNAL_HOSTS)
                : '';
            const thumbUrl = typeof _safeImageSrc === 'function'
                ? _safeImageSrc(rawManifest.thumb_url || '')
                : normalizeDeckWidgetText(rawManifest.thumb_url, 240);
            if (renderMode === 'iframe' && !embedUrl) return null;
            const stationSurface = normalizeDeckWidgetStationSurface(rawManifest.station_surface, widgetType, providerLabel, title);
            const sourceBinding = normalizeDeckWidgetSourceBinding(rawManifest.source_binding);
            const actionPolicy = normalizeDeckWidgetActionPolicy(rawManifest.action_policy, widgetType, rawManifest.actions);
            const moduleRuntime = renderMode === 'module_runtime'
                ? normalizeDeckModuleRuntime(rawManifest.module_runtime, title)
                : null;
            if (renderMode === 'module_runtime' && !moduleRuntime) return null;
            return {
                version: 1,
                key,
                widget_type: widgetType,
                render_mode: renderMode,
                title,
                subtitle,
                provider_label: providerLabel,
                icon,
                body_text: bodyText,
                embed_url: embedUrl,
                external_url: externalUrl,
                thumb_url: thumbUrl,
                badges: normalizeDeckWidgetBadges(rawManifest.badges),
                details: normalizeDeckWidgetDetails(rawManifest.details),
                station_surface: stationSurface,
                action_policy: actionPolicy,
                source_binding: sourceBinding,
                actions: normalizeDeckWidgetActions(rawManifest.actions, actionPolicy),
                module_runtime: moduleRuntime,
            };
        }

        function buildDeckWidgetManifestAttrs(rawManifest) {
            const manifest = sanitizeDeckWidgetManifest(rawManifest);
            if (!manifest) return '';
            const json = escapeEmbedAttr(JSON.stringify(manifest));
            return (
                ' data-canopy-widget-manifest="' + json + '"' +
                ' data-canopy-widget-type="' + escapeEmbedAttr(manifest.widget_type) + '"' +
                ' data-canopy-widget-key="' + escapeEmbedAttr(manifest.key) + '"' +
                ' data-canopy-source-ref="widget:' + escapeEmbedAttr(manifest.key) + '"'
            );
        }

        function parseDeckWidgetManifest(node) {
            if (!node || !node.getAttribute) return null;
            const raw = node.getAttribute('data-canopy-widget-manifest');
            if (!raw) return null;
            try {
                return sanitizeDeckWidgetManifest(JSON.parse(raw));
            } catch (_) {
                return null;
            }
        }

        /**
         * Human-readable title from a .canopy-module.html filename (matches channel attachment card builder).
         */
        function humanizeCanopyModuleBundleTitle(rawName) {
            const raw = String(rawName || 'canopy-module').replace(/\.canopy-module\.html?$/i, '');
            const spaced = raw
                .replace(/[-_]+/g, ' ')
                .replace(/\s+/g, ' ')
                .trim();
            if (!spaced) return 'Canopy Module';
            return spaced.replace(/\b\w/g, (ch) => ch.toUpperCase());
        }

        /**
         * Build a module_surface manifest object (before sanitization) from bundle file id and filename.
         * Used when the inline JSON attribute is missing or fails to parse/sanitize.
         */
        function buildCanopyModuleSurfaceManifestFromBundleId(fileId, rawFileName) {
            const fid = String(fileId || '').trim();
            if (!fid) return null;
            const bundleUrl = `/files/${encodeURIComponent(fid)}`;
            const title = humanizeCanopyModuleBundleTitle(rawFileName);
            const safeName = String(rawFileName || 'module bundle').trim().slice(0, 200) || 'module bundle';
            return {
                version: 1,
                key: `module:${fid}`,
                widget_type: 'module_surface',
                render_mode: 'module_runtime',
                title,
                subtitle: 'Safe executable lesson or station logic bound to this source.',
                provider_label: 'Canopy Module',
                icon: 'bi-box-fill',
                body_text: 'Single-file module bundle executed inside the Canopy Module Runtime.',
                badges: ['Module', 'Sandboxed', 'Source-bound'],
                details: [
                    { label: 'File', value: safeName },
                    { label: 'Format', value: 'Single HTML bundle' },
                ],
                station_surface: {
                    kind: 'station_surface',
                    domain: 'education',
                    label: `${title} Surface`,
                    summary: 'Executable lesson or station logic remains bound to this source while capabilities stay brokered.',
                    recurring: false,
                    scope: 'source',
                },
                action_policy: {
                    bounded: true,
                    max_risk: 'view',
                    human_gate: 'none',
                    audit_label: 'Bounded runtime',
                },
                source_binding: {
                    binding_type: 'message_attachment',
                    source_scope: 'source',
                    return_label: 'Return to source',
                },
                module_runtime: {
                    format: 'single_html',
                    bundle_file_id: fid,
                    bundle_url: bundleUrl,
                    module_type: title,
                    runtime_label: 'Canopy Module',
                    capabilities: {
                        required: ['source.read', 'deck.return'],
                        optional: ['clipboard.write', 'module.storage.local'],
                    },
                },
            };
        }

        /**
         * When JSON.parse/sanitize fails on data-canopy-widget-manifest, recover bundle file id from the raw string.
         */
        function extractDeckModuleBundleFileIdFromManifestAttr(raw) {
            if (!raw || typeof raw !== 'string') return '';
            const idMatch = raw.match(/"bundle_file_id"\s*:\s*"([^"]+)"/);
            if (idMatch && idMatch[1]) {
                const id = String(idMatch[1]).trim();
                if (id && /^[A-Za-z0-9_.-]+$/.test(id)) return id;
            }
            const urlMatch = raw.match(/"bundle_url"\s*:\s*"(\/files\/[^"]+)"/);
            if (urlMatch && urlMatch[1]) {
                try {
                    const u = new URL(urlMatch[1], window.location.origin);
                    const m = String(u.pathname || '').match(/^\/files\/([^/]+)$/);
                    if (!m || !m[1]) return '';
                    const seg = decodeURIComponent(m[1]);
                    if (seg && /^[A-Za-z0-9_.-]+$/.test(seg)) return seg;
                } catch (_) {
                    return '';
                }
            }
            return '';
        }

        /**
         * DOM root for module deck open — prefer explicit marker so we never match an unrelated ancestor
         * that has an empty or invalid data-canopy-widget-manifest (e.g. another embed).
         */
        function resolveCanopyModuleDeckManifestHost(node) {
            if (!(node instanceof Element)) return null;
            const marked = node.closest('[data-canopy-module-card]');
            if (marked) return marked;
            const feedAtt = node.closest(
                '.attachment-item[data-canopy-widget-manifest], .attachment-item[data-canopy-module-bundle-id]'
            );
            if (feedAtt) return feedAtt;
            const dmCard = node.closest(
                '.dm-attachment-card[data-canopy-widget-manifest], .dm-attachment-card[data-canopy-module-bundle-id]'
            );
            if (dmCard) return dmCard;
            return node.closest('[data-canopy-widget-manifest],[data-canopy-module-bundle-id]');
        }

        /**
         * Bundle file id: data-canopy-module-bundle-id, scrape manifest attr, or any same-origin /files/<id> link on the card (Download).
         */
        function extractCanopyModuleBundleFileIdFromHost(host) {
            if (!host || !host.getAttribute) return '';
            let fid = String(host.getAttribute('data-canopy-module-bundle-id') || '').trim();
            if (fid) return fid;
            fid = extractDeckModuleBundleFileIdFromManifestAttr(host.getAttribute('data-canopy-widget-manifest') || '');
            if (fid) return fid;
            if (host.querySelectorAll) {
                const links = host.querySelectorAll('a[href*="/files/"]');
                for (let i = 0; i < links.length; i++) {
                    try {
                        const u = new URL(links[i].href, window.location.origin);
                        if (String(u.origin || '') !== String(window.location.origin || '')) continue;
                        const m = String(u.pathname || '').match(/^\/files\/([^/]+)$/);
                        if (!m || !m[1]) continue;
                        const id = decodeURIComponent(m[1]);
                        if (id && /^[A-Za-z0-9_.-]+$/.test(id)) return id;
                    } catch (_) {
                        /* ignore */
                    }
                }
            }
            return '';
        }

        function trimEmbedUrlTrailingPunctuation(rawUrl) {
            let url = String(rawUrl || '');
            let trailing = '';
            while (url.length > 10 && /[).,;:!?\]}>]$/.test(url)) {
                if (url.endsWith(')')) {
                    const opens = (url.match(/\(/g) || []).length;
                    const closes = (url.match(/\)/g) || []).length;
                    if (opens >= closes) break;
                }
                trailing = url.slice(-1) + trailing;
                url = url.slice(0, -1);
            }
            return { url, trailing };
        }

        function buildEmbedCaption(text) {
            if (!text) return '';
            return '<div class="embed-provider-caption">' + escapeEmbedHtml(text) + '</div>';
        }

        function buildIframeEmbedPreview(providerClass, src, title, options = {}) {
            const safeSrc = escapeEmbedAttr(src);
            const safeTitle = escapeEmbedAttr(title || 'Embedded content');
            const allow = escapeEmbedAttr(options.allow || 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture');
            const extraClass = options.extraClass ? ' ' + escapeEmbedAttr(options.extraClass) : '';
            const frameClass = options.frameClass ? ' ' + escapeEmbedAttr(options.frameClass) : '';
            const heightStyle = options.height ? ' style="height:' + String(options.height).replace(/[^0-9.]/g, '') + 'px"' : '';
            const sandbox = options.sandbox ? ' sandbox="' + escapeEmbedAttr(options.sandbox) + '"' : '';
            const referrerPolicy = escapeEmbedAttr(options.referrerPolicy || 'strict-origin-when-cross-origin');
            const caption = buildEmbedCaption(options.caption || '');
            const widgetAttrs = buildDeckWidgetManifestAttrs(options.widgetManifest);
            return (
                '<div class="embed-preview iframe-embed ' + escapeEmbedAttr(providerClass) + extraClass + '"' + widgetAttrs + '>' +
                '<iframe src="' + safeSrc + '" title="' + safeTitle + '" frameborder="0" loading="lazy" allowfullscreen ' +
                'referrerpolicy="' + referrerPolicy + '" allow="' + allow + '"' + sandbox + heightStyle +
                ' class="' + frameClass.trim() + '"></iframe>' +
                caption +
                '</div>'
            );
        }

        function buildNativeMediaEmbed(providerClass, url, title, tagName, mimeType, options = {}) {
            const safeUrl = escapeEmbedAttr(url);
            const safeTitle = escapeEmbedAttr(title || 'Embedded media');
            const safeType = mimeType ? ' type="' + escapeEmbedAttr(mimeType) + '"' : '';
            const caption = buildEmbedCaption(options.caption || '');
            return (
                '<div class="embed-preview native-media-embed ' + escapeEmbedAttr(providerClass) + '">' +
                '<' + tagName + ' controls preload="metadata" playsinline title="' + safeTitle + '">' +
                '<source src="' + safeUrl + '"' + safeType + '>' +
                'Your browser does not support this media.' +
                '</' + tagName + '>' +
                caption +
                '</div>'
            );
        }

        function buildProviderCardEmbed(providerClass, url, title, subtitle, iconClass, options = {}) {
            const safeUrl = escapeEmbedAttr(url);
            const safeTitle = escapeEmbedHtml(title || 'External content');
            const safeSubtitle = escapeEmbedHtml(subtitle || '');
            const safeIcon = escapeEmbedAttr(iconClass || 'bi-box-arrow-up-right');
            const providerLabel = options.providerLabel ? '<span class="embed-provider-pill">' + escapeEmbedHtml(options.providerLabel) + '</span>' : '';
            const note = options.note ? '<div class="embed-provider-note">' + escapeEmbedHtml(options.note) + '</div>' : '';
            const widgetAttrs = buildDeckWidgetManifestAttrs(options.widgetManifest);
            return (
                '<div class="embed-preview provider-card-embed ' + escapeEmbedAttr(providerClass) + '"' + widgetAttrs + '>' +
                '<a class="provider-embed-card" href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' +
                '<div class="provider-embed-head">' +
                '<span class="provider-embed-icon"><i class="bi ' + safeIcon + '"></i></span>' +
                '<div class="provider-embed-copy">' +
                providerLabel +
                '<div class="provider-embed-title">' + safeTitle + '</div>' +
                '<div class="provider-embed-subtitle">' + safeSubtitle + '</div>' +
                note +
                '</div>' +
                '<span class="provider-embed-open"><i class="bi bi-box-arrow-up-right"></i></span>' +
                '</div>' +
                '</a>' +
                '</div>'
            );
        }

        function classifyAudioMime(ext) {
            const normalized = String(ext || '').toLowerCase();
            if (normalized === 'mp3') return 'audio/mpeg';
            if (normalized === 'wav') return 'audio/wav';
            if (normalized === 'ogg') return 'audio/ogg';
            if (normalized === 'm4a') return 'audio/mp4';
            if (normalized === 'aac') return 'audio/aac';
            if (normalized === 'flac') return 'audio/flac';
            return '';
        }

        function classifyVideoMime(ext) {
            const normalized = String(ext || '').toLowerCase();
            if (normalized === 'mp4' || normalized === 'm4v') return 'video/mp4';
            if (normalized === 'webm') return 'video/webm';
            if (normalized === 'ogv') return 'video/ogg';
            if (normalized === 'mov') return 'video/quicktime';
            return '';
        }

        function spotifyEmbedHeight(kind) {
            if (kind === 'track' || kind === 'episode') return 152;
            return 352;
        }

        function isEmbedMatchInsideHtmlTag(html, matchIndex) {
            const source = String(html || '');
            const index = Number(matchIndex);
            if (!Number.isFinite(index) || index < 0) return false;
            const lastTagOpen = source.lastIndexOf('<', index);
            const lastTagClose = source.lastIndexOf('>', index);
            return lastTagOpen > lastTagClose;
        }

        function getCanopyEmbedThemeName() {
            return canopyEmbedTheme() === 'light' ? 'light' : 'dark';
        }

        function safeUrlParse(rawUrl) {
            try {
                return new URL(String(rawUrl || ''), window.location.origin);
            } catch (_) {
                return null;
            }
        }

        function getGoogleMapsEmbedApiKey() {
            if (!window.CANOPY_VARS) return '';
            return String(window.CANOPY_VARS.googleMapsEmbedApiKey || '').trim();
        }

        function extractGoogleMapsQuery(urlObj) {
            if (!urlObj) return '';
            const query = urlObj.searchParams.get('q') || urlObj.searchParams.get('query') || '';
            if (query) return query.trim();
            const parts = urlObj.pathname.split('/').filter(Boolean);
            const placeIdx = parts.indexOf('place');
            if (placeIdx >= 0 && parts[placeIdx + 1]) {
                return decodeURIComponent(parts[placeIdx + 1]).trim();
            }
            const atMatch = urlObj.pathname.match(/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/);
            if (atMatch) {
                return atMatch[1] + ',' + atMatch[2];
            }
            return '';
        }

        function buildGoogleMapsEmbedUrl(rawUrl) {
            const apiKey = getGoogleMapsEmbedApiKey();
            if (!apiKey) return '';
            const urlObj = safeUrlParse(rawUrl);
            const query = extractGoogleMapsQuery(urlObj);
            if (!query) return '';
            return 'https://www.google.com/maps/embed/v1/search?key=' + encodeURIComponent(apiKey) + '&q=' + encodeURIComponent(query);
        }

        function clampNumber(value, min, max) {
            const num = Number(value);
            if (!Number.isFinite(num)) return min;
            return Math.min(max, Math.max(min, num));
        }

        function buildOsmBoundingBox(lat, lon, zoom) {
            const safeLat = clampNumber(lat, -85, 85);
            const safeLon = clampNumber(lon, -180, 180);
            const safeZoom = clampNumber(zoom, 2, 18);
            const lonDelta = 360 / Math.pow(2, safeZoom + 2);
            const latDelta = 180 / Math.pow(2, safeZoom + 2);
            const left = Math.max(-180, safeLon - lonDelta);
            const right = Math.min(180, safeLon + lonDelta);
            const bottom = Math.max(-85, safeLat - latDelta);
            const top = Math.min(85, safeLat + latDelta);
            return [left, bottom, right, top].join(',');
        }

        function buildOpenStreetMapEmbedUrl(rawUrl) {
            const urlObj = safeUrlParse(rawUrl);
            if (!urlObj) return '';
            let lat = '';
            let lon = '';
            let zoom = '';

            const hashMatch = String(urlObj.hash || '').match(/#map=(\d+)\/(-?\d+(?:\.\d+)?)\/(-?\d+(?:\.\d+)?)/);
            if (hashMatch) {
                zoom = hashMatch[1];
                lat = hashMatch[2];
                lon = hashMatch[3];
            }
            if (!lat || !lon) {
                lat = urlObj.searchParams.get('mlat') || '';
                lon = urlObj.searchParams.get('mlon') || '';
            }
            if (!zoom) {
                zoom = urlObj.searchParams.get('zoom') || '12';
            }
            if (!lat || !lon) return '';
            const bbox = buildOsmBoundingBox(lat, lon, zoom);
            return 'https://www.openstreetmap.org/export/embed.html?bbox=' +
                encodeURIComponent(bbox) +
                '&layer=mapnik&marker=' +
                encodeURIComponent(String(lat) + ',' + String(lon));
        }

        function parseTradingViewSymbol(rawUrl) {
            const urlObj = safeUrlParse(rawUrl);
            if (!urlObj) return '';
            const path = String(urlObj.pathname || '');
            const symbolMatch = path.match(/\/symbols\/([A-Za-z0-9._-]+)(?:\/)?/i);
            if (symbolMatch && symbolMatch[1]) {
                const rawSymbol = symbolMatch[1].replace(/\/+$/, '');
                if (rawSymbol.includes('-')) {
                    const idx = rawSymbol.indexOf('-');
                    const exchange = rawSymbol.slice(0, idx).toUpperCase();
                    const symbol = rawSymbol.slice(idx + 1).toUpperCase();
                    if (exchange && symbol) return exchange + ':' + symbol;
                }
                return rawSymbol.toUpperCase();
            }
            const tvSymbol = urlObj.searchParams.get('symbol') || urlObj.searchParams.get('ticker') || '';
            return tvSymbol.trim().toUpperCase();
        }

        function buildTradingViewEmbedUrl(rawUrl) {
            const symbol = parseTradingViewSymbol(rawUrl);
            if (!symbol) return '';
            const params = new URLSearchParams({
                symbol: symbol,
                interval: 'D',
                symboledit: '1',
                saveimage: '0',
                toolbarbg: getCanopyEmbedThemeName() === 'light' ? 'f8fafc' : '0f172a',
                theme: getCanopyEmbedThemeName(),
                style: '1',
                withdateranges: '1',
                hideideas: '1',
                locale: 'en',
            });
            params.set('utm_source', window.location.hostname || 'canopy');
            params.set('utm_medium', 'embed');
            params.set('utm_campaign', 'canopy');
            return 'https://s.tradingview.com/widgetembed/?' + params.toString();
        }

        function buildYouTubeEmbedSrc(videoId, autoplay = true) {
            const safeId = escapeEmbedAttr(videoId);
            return 'https://www.youtube-nocookie.com/embed/' + safeId +
                '?enablejsapi=1&autoplay=' + (autoplay ? '1' : '0') +
                '&playsinline=1&rel=0&origin=' + encodeURIComponent(window.location.origin);
        }

        function createYouTubeFacadeElement(videoId, iframeSrc) {
            const safeId = escapeEmbedAttr(videoId);
            const thumbUrl = 'https://img.youtube.com/vi/' + safeId + '/hqdefault.jpg';
            const facade = document.createElement('div');
            facade.className = 'yt-facade';
            facade.setAttribute('data-iframe-src', iframeSrc);
            facade.setAttribute('title', 'Click to play');
            facade.style.cssText = "position:relative;cursor:pointer;aspect-ratio:16/9;background:#000 url('" +
                escapeEmbedAttr(thumbUrl) +
                "') center/cover no-repeat;border-radius:10px;overflow:hidden;";
            facade.innerHTML =
                '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.25);transition:background 0.15s;">' +
                '<svg width="68" height="48" viewBox="0 0 68 48" style="filter:drop-shadow(0 2px 8px rgba(0,0,0,0.4));"><path d="M66.52 7.74c-.78-2.93-2.49-5.41-5.42-6.19C55.79.13 34 0 34 0S12.21.13 6.9 1.55C3.97 2.33 2.27 4.81 1.48 7.74.06 13.05 0 24 0 24s.06 10.95 1.48 16.26c.78 2.93 2.49 5.41 5.42 6.19C12.21 47.87 34 48 34 48s21.79-.13 27.1-1.55c2.93-.78 4.64-3.26 5.42-6.19C67.94 34.95 68 24 68 24s-.06-10.95-1.48-16.26z" fill="#FF0000"/><path d="M45 24L27 14v20" fill="#fff"/></svg>' +
                '</div>';
            return facade;
        }

        function buildYouTubeFacade(videoId) {
            const safeId = escapeEmbedAttr(videoId);
            const caption = buildEmbedCaption('YouTube');
            return (
                '<div class="embed-preview iframe-embed youtube-embed" data-video-id="' + safeId + '">' +
                createYouTubeFacadeElement(videoId, buildYouTubeEmbedSrc(videoId, true)).outerHTML +
                caption +
                '</div>'
            );
        }

        function materializeYouTubeFacade(facade, options = {}) {
            if (!facade) return null;
            if (facade.tagName && facade.tagName.toLowerCase() === 'iframe') return facade;
            const src = facade.getAttribute('data-iframe-src');
            if (!src) return null;
            let iframeSrc = src;
            try {
                const url = new URL(src, window.location.origin);
                url.searchParams.set('autoplay', options.autoplay === true ? '1' : '0');
                iframeSrc = url.toString();
            } catch (_) {}
            const iframe = document.createElement('iframe');
            iframe.src = iframeSrc;
            iframe.title = 'YouTube video';
            iframe.frameBorder = '0';
            iframe.allowFullscreen = true;
            iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture';
            iframe.referrerPolicy = 'strict-origin-when-cross-origin';
            iframe.style.width = '100%';
            iframe.style.aspectRatio = '16/9';
            iframe.style.borderRadius = '10px';
            iframe.style.background = '#000';
            iframe.style.display = 'block';
            facade.replaceWith(iframe);
            if (typeof window !== 'undefined' && typeof window.canopyRegisterMediaNode === 'function') {
                window.canopyRegisterMediaNode(iframe);
            } else if (typeof registerMediaNode === 'function') {
                registerMediaNode(iframe);
            }
            return iframe;
        }

        document.addEventListener('click', function(e) {
            if (e.defaultPrevented) return;
            var facade = e.target.closest('.yt-facade');
            if (!facade) return;
            materializeYouTubeFacade(facade, { autoplay: true });
        });

        const RICH_EMBED_PROVIDERS = [
            {
                key: 'youtube',
                pattern: /(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/|youtube\.com\/shorts\/|youtube\.com\/live\/)([\w-]{11})(?:[&?]\S*)?/g,
                render(match, videoId) {
                    return buildYouTubeFacade(videoId);
                },
            },
            {
                key: 'vimeo',
                pattern: /https?:\/\/(?:www\.)?vimeo\.com\/(?:video\/)?(\d+)(?:[/?#]\S*)?/g,
                render(match, videoId) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    const embedUrl = 'https://player.vimeo.com/video/' + encodeURIComponent(videoId);
                    return {
                        html: buildIframeEmbedPreview(
                            'vimeo-embed',
                            embedUrl,
                            'Vimeo video ' + videoId,
                            {
                                caption: 'Vimeo',
                                widgetManifest: {
                                    key: `vimeo:${videoId}`,
                                    widget_type: 'media_embed',
                                    render_mode: 'iframe',
                                    title: 'Vimeo video',
                                    subtitle: 'Expanded Vimeo playback inside the deck.',
                                    provider_label: 'Vimeo',
                                    icon: 'bi-vimeo',
                                    embed_url: embedUrl,
                                    external_url: parts.url,
                                    badges: ['Video', 'Embed'],
                                    actions: [
                                        { kind: 'external_link', label: 'Open Vimeo', icon: 'bi-box-arrow-up-right', url: parts.url },
                                    ],
                                },
                            }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'loom',
                pattern: /https?:\/\/(?:www\.)?loom\.com\/(?:share|embed)\/([A-Za-z0-9]+)(?:\?\S*)?/g,
                render(match, shareId) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    const embedUrl = 'https://www.loom.com/embed/' + encodeURIComponent(shareId);
                    return {
                        html: buildIframeEmbedPreview(
                            'loom-embed',
                            embedUrl,
                            'Loom recording ' + shareId,
                            {
                                caption: 'Loom',
                                widgetManifest: {
                                    key: `loom:${shareId}`,
                                    widget_type: 'media_embed',
                                    render_mode: 'iframe',
                                    title: 'Loom recording',
                                    subtitle: 'Deck-ready walkthrough video.',
                                    provider_label: 'Loom',
                                    icon: 'bi-camera-reels',
                                    embed_url: embedUrl,
                                    external_url: parts.url,
                                    badges: ['Video', 'Walkthrough'],
                                    actions: [
                                        { kind: 'external_link', label: 'Open Loom', icon: 'bi-box-arrow-up-right', url: parts.url },
                                    ],
                                },
                            }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'spotify',
                pattern:
                    /https?:\/\/open\.spotify\.com\/(?:intl-[a-z]{2}(?:-[a-z]{2})?\/)?(track|album|playlist|episode|show|artist)\/([A-Za-z0-9]+)(?:\?\S*)?/gi,
                render(match, kind, entityId) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    const embedUrl = 'https://open.spotify.com/embed/' + encodeURIComponent(kind) + '/' + encodeURIComponent(entityId) + '?utm_source=generator';
                    return {
                        html: buildIframeEmbedPreview(
                            'spotify-embed',
                            embedUrl,
                            'Spotify ' + kind,
                            {
                                caption: 'Spotify',
                                height: spotifyEmbedHeight(kind),
                                allow: 'autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture',
                                extraClass: 'fixed-height-embed',
                                widgetManifest: {
                                    key: `spotify:${kind}:${entityId}`,
                                    widget_type: 'media_embed',
                                    render_mode: 'iframe',
                                    title: 'Spotify ' + kind,
                                    subtitle: 'Deck-ready audio player.',
                                    provider_label: 'Spotify',
                                    icon: 'bi-spotify',
                                    embed_url: embedUrl,
                                    external_url: parts.url,
                                    badges: ['Audio', kind],
                                    actions: [
                                        { kind: 'external_link', label: 'Open Spotify', icon: 'bi-box-arrow-up-right', url: parts.url },
                                    ],
                                },
                            }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'soundcloud',
                pattern: /https?:\/\/(?:www\.)?soundcloud\.com\/[^\s<"]+/g,
                render(match) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    const embedUrl = 'https://w.soundcloud.com/player/?url=' + encodeURIComponent(parts.url) + '&color=%2359de89&auto_play=false&hide_related=false&show_comments=true&show_user=true&show_reposts=false&show_teaser=true&visual=false';
                    return {
                        html: buildIframeEmbedPreview(
                            'soundcloud-embed',
                            embedUrl,
                            'SoundCloud audio',
                            {
                                caption: 'SoundCloud',
                                height: 166,
                                allow: 'autoplay',
                                extraClass: 'fixed-height-embed',
                                widgetManifest: {
                                    key: `soundcloud:${parts.url}`,
                                    widget_type: 'media_embed',
                                    render_mode: 'iframe',
                                    title: 'SoundCloud audio',
                                    subtitle: 'Deck-ready shared audio.',
                                    provider_label: 'SoundCloud',
                                    icon: 'bi-soundwave',
                                    embed_url: embedUrl,
                                    external_url: parts.url,
                                    badges: ['Audio', 'Embed'],
                                    actions: [
                                        { kind: 'external_link', label: 'Open SoundCloud', icon: 'bi-box-arrow-up-right', url: parts.url },
                                    ],
                                },
                            }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'x',
                pattern: /https?:\/\/(?:www\.)?(?:x\.com|twitter\.com)\/(?:([\w]+)\/status\/|i\/web\/status\/|i\/status\/)(\d+)(?:\?\S*)?/g,
                render(match, username, statusId) {
                    const url = username
                        ? ('https://x.com/' + username + '/status/' + statusId)
                        : ('https://x.com/i/web/status/' + statusId);
                    const label = username ? ('@' + username) : 'X post';
                    return (
                        '<div class="embed-preview x-embed" data-x-status-id="' + escapeEmbedAttr(statusId) + '" ' +
                        'data-x-username="' + escapeEmbedAttr(username || '') + '" data-x-theme="' + canopyEmbedTheme() + '">' +
                        '<div class="x-embed-card">' +
                        '<a href="' + escapeEmbedAttr(url) + '" target="_blank" rel="noopener noreferrer" class="text-reset text-decoration-none">' +
                        '<div class="d-flex align-items-center gap-2">' +
                        '<i class="bi bi-twitter-x x-icon"></i>' +
                        '<div class="flex-grow-1">' +
                        '<strong>' + escapeEmbedHtml(label) + '</strong>' +
                        '<div class="text-muted small">View post on X</div>' +
                        '</div>' +
                        '<i class="bi bi-box-arrow-up-right x-link-arrow"></i>' +
                        '</div>' +
                        '</a>' +
                        '</div>' +
                        '<div class="x-embed-render"></div>' +
                        '</div>'
                    );
                },
            },
            {
                key: 'google_maps',
                pattern: /https?:\/\/(?:www\.)?(?:google\.[^\/]+\/maps(?:[/?#][^\s<"]*)?|maps\.google\.[^\/]+\/?[^\s<"]*|maps\.app\.goo\.gl\/?[^\s<"]*)/g,
                render(match) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    const embedUrl = buildGoogleMapsEmbedUrl(parts.url);
                    if (embedUrl) {
                        return {
                            html: buildIframeEmbedPreview(
                                'map-embed google-maps-embed',
                                embedUrl,
                                'Google Maps',
                                {
                                    caption: 'Google Maps',
                                    height: 320,
                                    allow: 'geolocation',
                                    referrerPolicy: 'no-referrer-when-downgrade',
                                    extraClass: 'fixed-height-embed map-service-embed',
                                    widgetManifest: {
                                        key: `map:${parts.url}`,
                                        widget_type: 'map',
                                        render_mode: 'iframe',
                                        title: 'Google Maps',
                                        subtitle: 'Explore the shared location in the deck.',
                                        provider_label: 'Google Maps',
                                        icon: 'bi-geo-alt-fill',
                                        embed_url: embedUrl,
                                        external_url: parts.url,
                                        badges: ['Map', 'Interactive'],
                                        actions: [
                                            { kind: 'external_link', label: 'Open map', icon: 'bi-box-arrow-up-right', url: parts.url },
                                        ],
                                    },
                                }
                            ),
                            trailing: parts.trailing,
                        };
                    }
                    return {
                        html: buildProviderCardEmbed(
                            'map-card-embed',
                            parts.url,
                            'Map link',
                            'Open this location in Google Maps.',
                            'bi-geo-alt-fill',
                            {
                                providerLabel: 'Google Maps',
                                note: getGoogleMapsEmbedApiKey()
                                    ? 'Open this location in Google Maps.'
                                    : 'Inline Google Maps requires CANOPY_GOOGLE_MAPS_EMBED_API_KEY; showing a safe card instead.',
                                widgetManifest: {
                                    key: `map-card:${parts.url}`,
                                    widget_type: 'map',
                                    render_mode: 'card',
                                    title: 'Google Maps link',
                                    subtitle: 'Open the shared location externally.',
                                    provider_label: 'Google Maps',
                                    icon: 'bi-geo-alt-fill',
                                    external_url: parts.url,
                                    badges: ['Map', 'External'],
                                    actions: [
                                        { kind: 'external_link', label: 'Open map', icon: 'bi-box-arrow-up-right', url: parts.url },
                                    ],
                                },
                            }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'openstreetmap',
                pattern: /https?:\/\/(?:www\.)?openstreetmap\.org\/[^\s<"]+/g,
                render(match) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    const embedUrl = buildOpenStreetMapEmbedUrl(parts.url);
                    if (embedUrl) {
                        return {
                            html: buildIframeEmbedPreview(
                                'map-embed openstreetmap-embed',
                                embedUrl,
                                'OpenStreetMap',
                                {
                                    caption: 'OpenStreetMap',
                                    height: 320,
                                    extraClass: 'fixed-height-embed map-service-embed',
                                    widgetManifest: {
                                        key: `osm:${parts.url}`,
                                        widget_type: 'map',
                                        render_mode: 'iframe',
                                        title: 'OpenStreetMap',
                                        subtitle: 'Explore the shared map context in the deck.',
                                        provider_label: 'OpenStreetMap',
                                        icon: 'bi-map',
                                        embed_url: embedUrl,
                                        external_url: parts.url,
                                        badges: ['Map', 'Interactive'],
                                        actions: [
                                            { kind: 'external_link', label: 'Open map', icon: 'bi-box-arrow-up-right', url: parts.url },
                                        ],
                                    },
                                }
                            ),
                            trailing: parts.trailing,
                        };
                    }
                    return {
                        html: buildProviderCardEmbed(
                            'map-card-embed',
                            parts.url,
                            'Map link',
                            'Open this location in OpenStreetMap.',
                            'bi-map',
                            {
                                providerLabel: 'OpenStreetMap',
                                note: 'Preview card for shared map context.',
                                widgetManifest: {
                                    key: `osm-card:${parts.url}`,
                                    widget_type: 'map',
                                    render_mode: 'card',
                                    title: 'OpenStreetMap link',
                                    subtitle: 'Open the shared map externally.',
                                    provider_label: 'OpenStreetMap',
                                    icon: 'bi-map',
                                    external_url: parts.url,
                                    badges: ['Map', 'External'],
                                    actions: [
                                        { kind: 'external_link', label: 'Open map', icon: 'bi-box-arrow-up-right', url: parts.url },
                                    ],
                                },
                            }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'tradingview',
                pattern: /https?:\/\/(?:www\.)?tradingview\.com\/[^\s<"]+/g,
                render(match) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    const embedUrl = buildTradingViewEmbedUrl(parts.url);
                    if (embedUrl) {
                        return {
                            html: buildIframeEmbedPreview(
                                'tradingview-embed',
                                embedUrl,
                                'TradingView chart',
                                {
                                    caption: 'TradingView',
                                    height: 360,
                                    extraClass: 'fixed-height-embed chart-service-embed',
                                    widgetManifest: {
                                        key: `chart:${parts.url}`,
                                        widget_type: 'chart',
                                        render_mode: 'iframe',
                                        title: 'TradingView chart',
                                        subtitle: 'Interactive market context in the deck.',
                                        provider_label: 'TradingView',
                                        icon: 'bi-graph-up-arrow',
                                        embed_url: embedUrl,
                                        external_url: parts.url,
                                        badges: ['Chart', 'Interactive'],
                                        actions: [
                                            { kind: 'external_link', label: 'Open chart', icon: 'bi-box-arrow-up-right', url: parts.url },
                                        ],
                                    },
                                }
                            ),
                            trailing: parts.trailing,
                        };
                    }
                    return {
                        html: buildProviderCardEmbed(
                            'tradingview-card-embed',
                            parts.url,
                            'TradingView chart',
                            'Open the live chart or symbol page in TradingView.',
                            'bi-graph-up-arrow',
                            {
                                providerLabel: 'TradingView',
                                note: 'Official TradingView widgets exist; this safe card keeps the channel lightweight.',
                                widgetManifest: {
                                    key: `chart-card:${parts.url}`,
                                    widget_type: 'chart',
                                    render_mode: 'card',
                                    title: 'TradingView chart',
                                    subtitle: 'Open the live chart externally.',
                                    provider_label: 'TradingView',
                                    icon: 'bi-graph-up-arrow',
                                    external_url: parts.url,
                                    badges: ['Chart', 'External'],
                                    actions: [
                                        { kind: 'external_link', label: 'Open chart', icon: 'bi-box-arrow-up-right', url: parts.url },
                                    ],
                                },
                            }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'direct_video',
                pattern: /https?:\/\/[^\s<"]+\.(mp4|webm|ogv|mov|m4v)(?:\?\S*)?/gi,
                render(match, ext) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    return {
                        html: buildNativeMediaEmbed(
                            'native-video-embed',
                            parts.url,
                            'Embedded video',
                            'video',
                            classifyVideoMime(ext),
                            { caption: 'Direct video' }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
            {
                key: 'direct_audio',
                pattern: /https?:\/\/[^\s<"]+\.(mp3|wav|ogg|m4a|aac|flac)(?:\?\S*)?/gi,
                render(match, ext) {
                    const parts = trimEmbedUrlTrailingPunctuation(match);
                    return {
                        html: buildNativeMediaEmbed(
                            'native-audio-embed',
                            parts.url,
                            'Embedded audio',
                            'audio',
                            classifyAudioMime(ext),
                            { caption: 'Direct audio' }
                        ),
                        trailing: parts.trailing,
                    };
                },
            },
        ];

        function collectProviderEmbeds(html) {
            const embeds = [];
            const placeholderPrefix = '\x00EMB_';

            RICH_EMBED_PROVIDERS.forEach(provider => {
                html = html.replace(provider.pattern, function() {
                    const match = arguments[0];
                    const matchIndex = arguments[arguments.length - 2];
                    if (isEmbedMatchInsideHtmlTag(html, matchIndex)) {
                        return match;
                    }
                    const rendered = provider.render.apply(null, arguments);
                    if (!rendered) return arguments[0];
                    const htmlValue = typeof rendered === 'string' ? rendered : rendered.html;
                    if (!htmlValue) return arguments[0];
                    const idx = embeds.length;
                    embeds.push(htmlValue);
                    const trailing = typeof rendered === 'string' ? '' : (rendered.trailing || '');
                    return placeholderPrefix + idx + '\x00' + trailing;
                });
            });

            return { html, embeds, placeholderPrefix };
        }

            function isEscapedMathDelimiter(value, index) {
                let slashCount = 0;
                for (let j = index - 1; j >= 0 && value[j] === '\\'; j--) {
                    slashCount += 1;
                }
                return (slashCount % 2) === 1;
            }

            function hasExplicitMathDelimiters(text) {
                if (!text) return false;
                const value = String(text);
                if (value.indexOf('\\(') !== -1 && value.indexOf('\\)') !== -1) return true;
                if (value.indexOf('\\[') !== -1 && value.indexOf('\\]') !== -1) return true;
                for (let i = 0; i < value.length - 1; i++) {
                    if (value[i] === '$' && value[i + 1] === '$' && !isEscapedMathDelimiter(value, i)) {
                        for (let j = i + 2; j < value.length - 1; j++) {
                            if (value[j] === '$' && value[j + 1] === '$' && !isEscapedMathDelimiter(value, j)) {
                                return true;
                            }
                        }
                    }
                }
                return false;
            }

            function isLikelyMathInlineContent(content) {
                const trimmed = String(content || '').trim();
                if (!trimmed || trimmed.length > 120 || /[\r\n]/.test(trimmed)) return false;

                if (/^\$?[\d,]+(?:\.\d+)?(?:\s*(?:k|m|mm|bn|b|t|%))?(?:\s*(?:usd|cad|eur|gbp))?$/i.test(trimmed)) {
                    return false;
                }
                if (/^[A-Z]{1,8}\s+\$?[\d,]+(?:\.\d+)?(?:\s*%)?$/i.test(trimmed)) {
                    return false;
                }
                if (/^[\d,.\s]+(?:to|vs|at)\s+[\d,.\s]+$/i.test(trimmed)) {
                    return false;
                }

                const hasLatexCommand = /\\[A-Za-z]+/.test(trimmed);
                const hasBinaryOperator = /(?:\d|[A-Za-z)}\]])\s*[-+*=<>/^_]\s*(?:\d|[A-Za-z({[])/.test(trimmed);
                const hasStructuredMath = /[_^{}]/.test(trimmed);
                const hasMathKeywords = /\b(?:sin|cos|tan|log|ln|max|min|sum|prod|int|lim)\b/.test(trimmed);

                return hasLatexCommand || hasBinaryOperator || hasStructuredMath || hasMathKeywords;
            }

            function hasLikelyInlineMath(text) {
                if (!text || String(text).indexOf('$') === -1) return false;
                const value = String(text);
                for (let i = 0; i < value.length; i++) {
                    if (value[i] !== '$' || isEscapedMathDelimiter(value, i)) continue;
                    if (value[i + 1] === '$') {
                        i += 1;
                        continue;
                    }
                    for (let j = i + 1; j < value.length; j++) {
                        if (value[j] !== '$' || isEscapedMathDelimiter(value, j)) continue;
                        if (value[j - 1] === '$' || value[j + 1] === '$') continue;
                        if (isLikelyMathInlineContent(value.slice(i + 1, j))) {
                            return true;
                        }
                        i = j;
                        break;
                    }
                }
                return false;
            }

            function buildMathDelimitersForText(text) {
                const value = String(text || '');
                const delimiters = [];
                if (value.indexOf('$$') !== -1) {
                    delimiters.push({ left: '$$', right: '$$', display: true });
                }
                if (value.indexOf('\\[') !== -1 && value.indexOf('\\]') !== -1) {
                    delimiters.push({ left: '\\[', right: '\\]', display: true });
                }
                if (value.indexOf('\\(') !== -1 && value.indexOf('\\)') !== -1) {
                    delimiters.push({ left: '\\(', right: '\\)', display: false });
                }
                if (hasLikelyInlineMath(value)) {
                    delimiters.push({ left: '$', right: '$', display: false });
                }
                return delimiters;
            }

            function containsMathDelimiters(text) {
                return hasExplicitMathDelimiters(text) || hasLikelyInlineMath(text);
            }

            function renderMathInElementSafe(root) {
                if (!root || typeof window === 'undefined' || typeof window.renderMathInElement !== 'function') {
                    return false;
                }
                const scope = (root instanceof Element || root instanceof Document) ? root : null;
                if (!scope) return false;
                const sourceText = scope.textContent || '';
                const delimiters = buildMathDelimitersForText(sourceText);
                if (!delimiters.length) return false;
                try {
                    window.renderMathInElement(scope, {
                        delimiters: delimiters,
                        throwOnError: false,
                        strict: 'ignore',
                        trust: false,
                        ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code', 'a'],
                        ignoredClasses: ['channel-code', 'no-katex']
                    });
                    return true;
                } catch (e) {
                    return false;
                }
            }
            if (typeof window !== 'undefined') {
                window.renderMathInElementSafe = renderMathInElementSafe;
                window.containsMathDelimiters = containsMathDelimiters;
            }

            function normalizeMentionIdentityKey(value) {
                return String(value || '').trim().toLowerCase();
            }

            function rememberMentionUsers(users) {
                if (typeof window === 'undefined') return {};
                const map = window.mentionIdentityMap && typeof window.mentionIdentityMap === 'object'
                    ? window.mentionIdentityMap
                    : {};
                (Array.isArray(users) ? users : []).forEach(function(user) {
                    if (!user || typeof user !== 'object') return;
                    const uid = String(user.user_id || user.id || '').trim();
                    const username = String(user.username || '').trim();
                    const handle = String(user.handle || '').trim();
                    const display = String(user.display_name || '').trim();
                    const normalized = {
                        ...user,
                        user_id: uid || user.user_id || user.id || '',
                        username: username || uid,
                        display_name: display || username || uid,
                        handle: handle || username || uid,
                    };
                    [uid, username, handle].forEach(function(key) {
                        const normalizedKey = normalizeMentionIdentityKey(key);
                        if (normalizedKey) map[normalizedKey] = normalized;
                    });
                });
                window.mentionIdentityMap = map;
                return map;
            }

            function getMentionIdentityForHandle(handle) {
                const key = normalizeMentionIdentityKey(handle);
                if (!key || typeof window === 'undefined') return null;
                const map = window.mentionIdentityMap || {};
                return map[key] || null;
            }

            if (typeof window !== 'undefined') {
                window.rememberMentionUsers = rememberMentionUsers;
                window.getMentionIdentityForHandle = getMentionIdentityForHandle;
            }

	        function linkifyMentions(text) {
		            if (!text || text.indexOf('@') === -1) return text;
		            const map = (typeof window !== 'undefined' && window.mentionDisplayMap) || {};
		            const mentionRegex = /(^|[^A-Za-z0-9_.\-@])@([A-Za-z0-9](?:[A-Za-z0-9_.\-]{0,47}[A-Za-z0-9]))/g;
		            return text.replace(mentionRegex, function(match, prefix, handle) {
		                var identity = getMentionIdentityForHandle(handle);
		                var display = identity && (identity.display_name || identity.username || identity.user_id);
		                if (display == null || display === '') display = map[handle];
		                if (display == null) display = handle;
		                var escaped = _escapeHtml(display);
		                var safeHandle = _escapeHtml(handle);
		                var safeUserId = identity && identity.user_id ? _escapeHtml(identity.user_id) : '';
		                var accountType = identity && identity.account_type ? _escapeHtml(identity.account_type) : '';
		                return (prefix || '') + '<span class="mention-tag" role="button" tabindex="0" aria-haspopup="dialog" ' +
                            'data-mention="' + safeHandle + '" data-mention-user-id="' + safeUserId + '" ' +
                            'data-mention-account-type="' + accountType + '" title="Open actions for @' + safeHandle + '" ' +
                            'aria-label="Open actions for @' + safeHandle + '">@' + escaped + '</span>';
		            });
		        }

            function _escapeHtml(text) {
                const div = document.createElement('div');
                div.textContent = text == null ? '' : String(text);
                return div.innerHTML;
            }

            function hasCanopyMarkdownSyntax(text) {
                const value = String(text || '');
                return (
                    /(^|\n)\s{0,3}#{1,3}\s+\S/.test(value) ||
                    /(^|\n)\s{0,3}>\s+\S/.test(value) ||
                    /(^|\n)\s{0,3}(?:[-*+]\s+|\d+\.\s+)\S/.test(value) ||
                    /(?:\*\*|__)[^\n]+(?:\*\*|__)/.test(value) ||
                    /(^|[^\*])\*[^\s*][^\n]*[^\s*]\*(?!\*)/.test(value) ||
                    /~~[^\n]+~~/.test(value) ||
                    /`[^`\n]+`/.test(value) ||
                    /\[[^\]\n]+\]\(https?:\/\/[^)\s]+[^\s)]\)/.test(value)
                );
            }

            function renderCanopyInlineMarkdown(html) {
                if (!html) return html;
                const inlineBlocks = [];
                const INLINE_PLACEHOLDER = '\x00INLINE_';
                html = html.replace(/`([^`\n]+)`/g, function(_match, body) {
                    const idx = inlineBlocks.length;
                    inlineBlocks.push('<code class="canopy-inline-code no-katex">' + body + '</code>');
                    return INLINE_PLACEHOLDER + idx + '\x00';
                });
                html = html.replace(/(\*\*|__)([^\n]+?)\1/g, '<strong>$2</strong>');
                html = html.replace(/~~([^\n]+?)~~/g, '<del>$1</del>');
                html = html.replace(/(^|[^\*])\*([^\s*][^\n]*?[^\s*])\*(?!\*)/g, '$1<em>$2</em>');
                for (let i = 0; i < inlineBlocks.length; i++) {
                    html = html.replace(INLINE_PLACEHOLDER + i + '\x00', inlineBlocks[i]);
                }
                return html;
            }

            function renderCanopyBlockMarkdown(html) {
                if (!html) return html;
                const lines = String(html).split('\n');
                const out = [];
                let listType = null;
                let listItems = [];
                let quoteLines = [];

                function flushList() {
                    if (!listType) return;
                    out.push('<' + listType + ' class="canopy-md-list">' + listItems.join('') + '</' + listType + '>');
                    listType = null;
                    listItems = [];
                }

                function flushQuote() {
                    if (!quoteLines.length) return;
                    out.push('<blockquote class="canopy-md-quote">' + quoteLines.join('<br>') + '</blockquote>');
                    quoteLines = [];
                }

                lines.forEach(function(line) {
                    const heading = line.match(/^\s{0,3}(#{1,3})\s+(.+)$/);
                    const quote = line.match(/^\s{0,3}>\s?(.*)$/);
                    const unordered = line.match(/^\s{0,3}[-*+]\s+(.+)$/);
                    const ordered = line.match(/^\s{0,3}\d+\.\s+(.+)$/);

                    if (heading) {
                        flushList();
                        flushQuote();
                        const levelClass = 'h' + Math.min(3, heading[1].length);
                        out.push('<div class="canopy-md-heading ' + levelClass + '">' + heading[2] + '</div>');
                        return;
                    }

                    if (quote) {
                        flushList();
                        quoteLines.push(quote[1] || '&nbsp;');
                        return;
                    }

                    if (unordered || ordered) {
                        flushQuote();
                        const nextType = unordered ? 'ul' : 'ol';
                        if (listType && listType !== nextType) flushList();
                        listType = nextType;
                        listItems.push('<li>' + (unordered ? unordered[1] : ordered[1]) + '</li>');
                        return;
                    }

                    flushList();
                    flushQuote();
                    out.push(line);
                });

                flushList();
                flushQuote();
                return out.join('\n');
            }

            const CANOPY_MARKDOWN_PREVIEW_EXTENSIONS = ['.md', '.markdown'];
            const CANOPY_SPREADSHEET_PREVIEW_EXTENSIONS = ['.csv', '.tsv', '.xlsx', '.xlsm'];
            const CANOPY_SPREADSHEET_PREVIEW_MIME_TYPES = new Set([
                'text/csv',
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                'application/vnd.ms-excel.sheet.macroenabled.12'
            ]);
            const CANOPY_DOCUMENT_PREVIEW_EXTENSIONS = [
                '.docx', '.docm', '.dotx', '.pptx', '.pptm', '.ppsx', '.potx', '.rtf', '.odt', '.odp'
            ];
            const CANOPY_DOCUMENT_PREVIEW_MIME_TYPES = new Set([
                'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'application/vnd.ms-word.document.macroenabled.12',
                'application/vnd.openxmlformats-officedocument.wordprocessingml.template',
                'application/vnd.openxmlformats-officedocument.presentationml.presentation',
                'application/vnd.ms-powerpoint.presentation.macroenabled.12',
                'application/vnd.openxmlformats-officedocument.presentationml.slideshow',
                'application/vnd.openxmlformats-officedocument.presentationml.template',
                'application/rtf',
                'text/rtf',
                'application/vnd.oasis.opendocument.text',
                'application/vnd.oasis.opendocument.presentation'
            ]);
            const CANOPY_TEXT_PREVIEW_EXTENSIONS = [
                '.md', '.markdown', '.txt', '.log', '.json', '.py', '.js', '.ts',
                '.csv', '.tsv', '.yaml', '.yml', '.xml', '.tex', '.html', '.css',
                '.sh', '.bat', '.cfg', '.ini', '.toml'
            ];

            function canopyFileExtension(filename) {
                const raw = String(filename || '').split('?')[0].split('#')[0];
                const dot = raw.lastIndexOf('.');
                return dot >= 0 ? raw.slice(dot).toLowerCase() : '';
            }

            function canopyIsMarkdownPreviewable(filename, contentType) {
                const ext = canopyFileExtension(filename);
                const type = String(contentType || '').toLowerCase();
                return CANOPY_MARKDOWN_PREVIEW_EXTENSIONS.includes(ext) || type === 'text/markdown' || type === 'text/x-markdown';
            }

            function canopyIsModuleBundle(filename, contentType) {
                const name = String(filename || '').toLowerCase();
                const type = String(contentType || '').toLowerCase();
                return type === 'text/html' && (name.endsWith('.canopy-module.html') || name.endsWith('.canopy-module.htm'));
            }

            function canopyIsSpreadsheetPreviewable(filename, contentType) {
                const ext = canopyFileExtension(filename);
                const type = String(contentType || '').toLowerCase();
                return CANOPY_SPREADSHEET_PREVIEW_EXTENSIONS.includes(ext) || CANOPY_SPREADSHEET_PREVIEW_MIME_TYPES.has(type);
            }

            function canopyIsDocumentPreviewable(filename, contentType) {
                const ext = canopyFileExtension(filename);
                const type = String(contentType || '').toLowerCase();
                return CANOPY_DOCUMENT_PREVIEW_EXTENSIONS.includes(ext) || CANOPY_DOCUMENT_PREVIEW_MIME_TYPES.has(type);
            }

            function canopyIsTextPreviewable(filename, contentType) {
                if (canopyIsModuleBundle(filename, contentType)) return false;
                if (canopyIsSpreadsheetPreviewable(filename, contentType)) return false;
                if (canopyIsDocumentPreviewable(filename, contentType)) return false;
                const ext = canopyFileExtension(filename);
                const type = String(contentType || '').toLowerCase();
                if (CANOPY_TEXT_PREVIEW_EXTENSIONS.includes(ext)) return true;
                if (type.startsWith('text/')) return true;
                return ['application/json', 'application/xml', 'application/x-yaml', 'application/javascript', 'application/typescript', 'text/x-tex', 'application/x-latex'].includes(type);
            }

            function canopySpreadsheetColumnLabel(index) {
                let label = '';
                let value = Number(index) + 1;
                while (value > 0) {
                    const rem = (value - 1) % 26;
                    label = String.fromCharCode(65 + rem) + label;
                    value = Math.floor((value - 1) / 26);
                }
                return label;
            }

            function canopyFormatSpreadsheetNumber(value) {
                const num = Number(value);
                if (!Number.isFinite(num)) return '#ERR';
                if (Math.abs(num - Math.round(num)) < 1e-9) return String(Math.round(num));
                return Number(num.toFixed(6)).toString();
            }

            function canopyRenderSpreadsheetTable(sheet) {
                const rows = Array.isArray(sheet && sheet.rows) ? sheet.rows : [];
                const colCount = Math.max(Number(sheet && sheet.preview_col_count) || 0, ...rows.map(row => Array.isArray(row) ? row.length : 0));
                let thead = '';
                if (colCount > 0) {
                    const labels = Array.from({ length: colCount }, function(_, index) {
                        return `<th scope="col">${canopySpreadsheetColumnLabel(index)}</th>`;
                    }).join('');
                    thead = `<thead><tr><th scope="col" class="sheet-row-label"></th>${labels}</tr></thead>`;
                }
                const bodyRows = rows.map(function(row, rowIndex) {
                    const normalized = Array.isArray(row) ? row.slice() : [];
                    while (normalized.length < colCount) {
                        normalized.push({ display: '', kind: 'empty' });
                    }
                    const cells = normalized.map(function(cell) {
                        const display = _escapeHtml(cell && cell.display ? cell.display : '');
                        const title = cell && cell.truncated ? ` title="Truncated from ${Number(cell.full_length || 0)} chars"` : '';
                        const kind = String(cell && cell.kind || 'text');
                        return `<td class="${kind === 'number' ? 'sheet-cell-number' : ''}"${title}>${display || '&nbsp;'}</td>`;
                    }).join('');
                    return `<tr><th scope="row" class="sheet-row-label">${rowIndex + 1}</th>${cells}</tr>`;
                }).join('');
                return `
                    <div class="table-responsive">
                        <table class="table table-sm canopy-sheet-table mb-0">
                            ${thead}
                            <tbody>${bodyRows || '<tr><td class="text-muted small" colspan="2">No preview rows.</td></tr>'}</tbody>
                        </table>
                    </div>
                `;
            }

            function renderSpreadsheetPreviewHtml(previewId, payload) {
                const sheets = Array.isArray(payload && payload.sheets) ? payload.sheets : [];
                const tabs = sheets.map(function(sheet, index) {
                    const active = index === 0 ? ' active' : '';
                    return `<button type="button" class="btn btn-sm btn-outline-secondary${active}" data-sheet-tab="${index}" onclick="switchSpreadsheetPreviewSheet('${previewId}', ${index})">${_escapeHtml(sheet && sheet.name ? sheet.name : ('Sheet ' + (index + 1)))}</button>`;
                }).join('');
                const panels = sheets.map(function(sheet, index) {
                    const hidden = index === 0 ? '' : ' style="display:none;"';
                    const meta = [];
                    const rowCount = Number(sheet && sheet.row_count) || 0;
                    const colCount = Number(sheet && sheet.col_count) || 0;
                    meta.push(`${rowCount} rows`);
                    meta.push(`${colCount} cols`);
                    if (sheet && sheet.truncated_rows) meta.push('row preview clipped');
                    if (sheet && sheet.truncated_cols) meta.push('column preview clipped');
                    return `
                        <div data-sheet-panel="${index}"${hidden}>
                            <div class="small text-muted mb-2">${meta.join(' • ')}</div>
                            ${canopyRenderSpreadsheetTable(sheet)}
                        </div>
                    `;
                }).join('');
                const badges = [];
                if (payload && payload.macro_enabled) {
                    badges.push('<span class="badge text-bg-warning">Macros disabled</span>');
                }
                if (payload && payload.truncated) {
                    badges.push('<span class="badge text-bg-secondary">Preview clipped</span>');
                }
                const warning = payload && payload.warning ? `<div class="small text-warning mt-2"><i class="bi bi-shield-exclamation me-1"></i>${_escapeHtml(payload.warning)}</div>` : '';
                return `
                    <div class="file-preview-container spreadsheet-preview">
                        <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-2">
                            <div class="small fw-semibold"><i class="bi bi-file-earmark-spreadsheet me-1"></i>Spreadsheet preview</div>
                            <div class="d-flex flex-wrap gap-1">${badges.join('')}</div>
                        </div>
                        ${tabs ? `<div class="d-flex flex-wrap gap-1 mb-2">${tabs}</div>` : ''}
                        ${panels || '<div class="small text-muted">No worksheet data available.</div>'}
                        ${warning}
                    </div>
                `;
            }

            function renderFilePreviewPayloadHtml(previewId, payload) {
                if (!payload || payload.previewable === false) {
                    const reason = _escapeHtml((payload && (payload.error || payload.message)) || 'Inline preview is not available for this file.');
                    return `<div class="text-muted p-2"><i class="bi bi-file-earmark me-1"></i>${reason}</div>`;
                }
                if (payload.kind === 'spreadsheet') {
                    return renderSpreadsheetPreviewHtml(previewId, payload);
                }
                if (payload.kind === 'document') {
                    const badges = [];
                    const format = payload.document_format ? String(payload.document_format).toUpperCase() : 'DOC';
                    badges.push(`<span class="badge text-bg-secondary">${_escapeHtml(format)}</span>`);
                    if (payload.macro_enabled) badges.push('<span class="badge text-bg-warning">Macros disabled</span>');
                    if (payload.truncated) badges.push('<span class="badge text-bg-secondary">Preview clipped</span>');
                    const warning = payload.warning ? `<div class="small text-warning mt-2"><i class="bi bi-shield-exclamation me-1"></i>${_escapeHtml(payload.warning)}</div>` : '';
                    const escaped = _escapeHtml(String(payload.text || ''));
                    return `
                        <div class="file-preview-container document-preview">
                            <div class="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-2">
                                <div class="small fw-semibold"><i class="bi bi-file-earmark-text me-1"></i>Document preview</div>
                                <div class="d-flex flex-wrap gap-1">${badges.join('')}</div>
                            </div>
                            <pre><code>${escaped}</code></pre>
                            ${warning}
                        </div>
                    `;
                }
                if (payload.kind === 'markdown' && typeof marked !== 'undefined') {
                    return `<div class="file-preview-container md-preview">${marked.parse(String(payload.text || ''))}</div>`;
                }
                const escaped = _escapeHtml(String(payload.text || ''));
                return `<div class="file-preview-container code-preview"><pre><code>${escaped}</code></pre></div>`;
            }

            function setAttachmentPreviewToggleState(previewId, expanded) {
                const btn = document.querySelector(`[data-preview-toggle="${previewId}"]`);
                if (!btn) return;
                const label = btn.querySelector('.preview-label');
                if (!label) return;
                const collapsedLabel = btn.getAttribute('data-preview-label') || 'Preview';
                const expandedLabel = btn.getAttribute('data-preview-expanded-label') || 'Collapse';
                label.textContent = expanded ? expandedLabel : collapsedLabel;
            }

            function canopyAttachmentPreviewLabels(filename, contentType) {
                if (canopyIsSpreadsheetPreviewable(filename, contentType)) {
                    return { collapsed: 'Open sheet', expanded: 'Hide sheet' };
                }
                if (canopyIsDocumentPreviewable(filename, contentType)) {
                    return { collapsed: 'Preview text', expanded: 'Hide text' };
                }
                return { collapsed: 'Preview', expanded: 'Collapse' };
            }

            function switchSpreadsheetPreviewSheet(previewId, sheetIndex) {
                const wrapper = document.getElementById(previewId);
                if (!wrapper) return;
                wrapper.querySelectorAll('[data-sheet-tab]').forEach(function(tab) {
                    const active = Number(tab.getAttribute('data-sheet-tab')) === Number(sheetIndex);
                    tab.classList.toggle('active', active);
                });
                wrapper.querySelectorAll('[data-sheet-panel]').forEach(function(panel) {
                    panel.style.display = Number(panel.getAttribute('data-sheet-panel')) === Number(sheetIndex) ? '' : 'none';
                });
            }

            function toggleAttachmentPreview(previewId, previewUrl) {
                const wrapper = document.getElementById(previewId);
                if (!wrapper) return;

                if (wrapper.style.display !== 'none') {
                    wrapper.style.display = 'none';
                    setAttachmentPreviewToggleState(previewId, false);
                    return;
                }

                wrapper.style.display = 'block';
                setAttachmentPreviewToggleState(previewId, true);

                if (wrapper.dataset.loaded === 'true') return;
                wrapper.innerHTML = '<div class="text-muted p-2"><i class="bi bi-hourglass-split me-1"></i>Loading preview...</div>';

                apiCall(previewUrl)
                    .then(function(payload) {
                        wrapper.dataset.loaded = 'true';
                        wrapper.innerHTML = renderFilePreviewPayloadHtml(previewId, payload || {});
                    })
                    .catch(function(err) {
                        const msg = _escapeHtml((err && (err.error || err.message)) || 'Could not load preview');
                        wrapper.innerHTML = `<div class="text-danger p-2"><i class="bi bi-exclamation-triangle me-1"></i>${msg}</div>`;
                    });
            }

            if (typeof window !== 'undefined') {
                window.toggleAttachmentPreview = toggleAttachmentPreview;
                window.switchSpreadsheetPreviewSheet = switchSpreadsheetPreviewSheet;
                window.canopyIsSpreadsheetPreviewable = canopyIsSpreadsheetPreviewable;
                window.canopyIsDocumentPreviewable = canopyIsDocumentPreviewable;
                window.canopyIsTextPreviewable = canopyIsTextPreviewable;
                window.canopyIsMarkdownPreviewable = canopyIsMarkdownPreviewable;
                window.canopyAttachmentPreviewLabels = canopyAttachmentPreviewLabels;
                window.canopyIsModuleBundle = canopyIsModuleBundle;
            }

            function renderInlineSheetFallback(body) {
                const escaped = _escapeHtml(body || '');
                return '<div class="channel-code-wrap position-relative mb-2">' +
                    '<button type="button" class="channel-code-copy-btn btn btn-sm position-absolute top-0 end-0 m-1" title="Copy to clipboard" aria-label="Copy">' +
                    '<i class="bi bi-clipboard"></i></button>' +
                    '<pre class="channel-code p-2 pe-4 rounded mb-0" style="background:var(--canopy-bg-tertiary); border:1px solid var(--canopy-border); overflow-x:auto;"><code>' + escaped + '</code></pre></div>';
            }

            function canopyGetSheetEngine() {
                return (typeof window !== 'undefined' && window.CanopySheetEngine) ? window.CanopySheetEngine : null;
            }

            function canopyEncodeSheetSource(source) {
                return encodeURIComponent(String(source || ''));
            }

            function canopyDecodeSheetSource(source) {
                try {
                    return decodeURIComponent(String(source || ''));
                } catch (error) {
                    return String(source || '');
                }
            }

            function canopyCloneInlineSheetSpec(spec) {
                const engine = canopyGetSheetEngine();
                if (!engine || !spec) return { title: '', columns: [], rows: [] };
                const built = engine.buildInlineSheetMatrix(spec);
                const hasColumns = Array.isArray(spec.columns) && spec.columns.length > 0;
                const header = hasColumns ? (built.matrix[0] || []).slice() : Array.from({ length: built.width }, function() { return ''; });
                const rowStart = hasColumns ? 1 : 0;
                const rows = built.matrix.slice(rowStart).map(function(row) { return row.slice(); });
                return {
                    title: String(spec.title || ''),
                    columns: header,
                    rows: rows,
                };
            }

            function canopyInlineSheetLanguage(wrapper) {
                return (wrapper && wrapper.getAttribute('data-sheet-language')) || 'sheet';
            }

            function canopyInlineSheetOccurrence(wrapper) {
                if (!wrapper) return 0;
                const value = Number(wrapper.getAttribute('data-sheet-occurrence'));
                return Number.isFinite(value) ? value : 0;
            }

            function canopyInlineSheetFence(body, language) {
                const lang = String(language || 'sheet').trim() || 'sheet';
                const normalized = String(body || '').trim();
                return '```' + lang + '\n' + normalized + '\n```';
            }

            function canopyReplaceInlineSheetBlock(content, originalBody, updatedBody, language, occurrenceIndex) {
                const text = String(content || '');
                const target = String(originalBody || '').trim();
                const replacement = canopyInlineSheetFence(updatedBody, language);
                const regex = /```(sheet|spreadsheet)\n?([\s\S]*?)```/g;
                let match;
                let matchedOccurrence = 0;
                const targetOccurrence = Number.isFinite(Number(occurrenceIndex)) ? Number(occurrenceIndex) : null;
                while ((match = regex.exec(text))) {
                    if (String(match[2] || '').trim() === target) {
                        if (targetOccurrence === null || matchedOccurrence === targetOccurrence) {
                            return text.slice(0, match.index) + replacement + text.slice(match.index + match[0].length);
                        }
                        matchedOccurrence += 1;
                    }
                }
                return null;
            }

            function canopyRenderInlineSheetTable(evaluated, options) {
                const hasColumns = !!(options && options.hasColumns);
                const headerLabels = Array.isArray(options && options.headerLabels) ? options.headerLabels : null;
                const layout = Array.isArray(options && options.columnLayout) ? options.columnLayout : [];
                const width = Number((evaluated && evaluated.width) || 0);
                const colgroup = Array.from({ length: width + 1 }, function(_, index) {
                    if (index === 0) {
                        return '<col class="canopy-sheet-col-row-label">';
                    }
                    const column = layout[index - 1] || {};
                    const chars = Math.max(6, Math.min(24, Number(column.chars) || 9));
                    const kind = column.kind === 'number' ? 'number' : 'text';
                    const wrap = column.wrap ? ' canopy-sheet-col-wrap' : '';
                    return `<col class="canopy-sheet-col canopy-sheet-col-${kind}${wrap}" style="width:${chars}ch;">`;
                }).join('');
                const headerHtml = Array.from({ length: width }, function(_, index) {
                    const label = headerLabels && typeof headerLabels[index] !== 'undefined' && String(headerLabels[index] || '').trim()
                        ? String(headerLabels[index] || '').trim()
                        : canopySpreadsheetColumnLabel(index);
                    const column = layout[index] || {};
                    const klass = ['canopy-sheet-header-cell'];
                    klass.push(column.kind === 'number' ? 'canopy-sheet-header-number' : 'canopy-sheet-header-text');
                    if (column.wrap) klass.push('canopy-sheet-header-wrap');
                    return `<th scope="col" class="${klass.join(' ')}">${_escapeHtml(label)}</th>`;
                }).join('');
                const rawRows = (evaluated && evaluated.rows ? evaluated.rows : []);
                const visibleRows = hasColumns ? rawRows.slice(1) : rawRows;
                const rowNumberOffset = hasColumns ? 2 : 1;
                const bodyRows = visibleRows.map(function(row, rowIndex) {
                    const cells = row.map(function(resolved, colIndex) {
                        const title = resolved && resolved.formula
                            ? ` title="${_escapeHtml(resolved.formula).replace(/"/g, '&quot;')}"`
                            : '';
                        const column = layout[colIndex];
                        const klass = [];
                        if (resolved && resolved.kind === 'number') klass.push('sheet-cell-number');
                        if (column && column.wrap) klass.push('canopy-sheet-cell-wrap');
                        if (column && column.kind === 'number') klass.push('canopy-sheet-cell-compact');
                        return `<td class="${klass.join(' ')}"${title}>${_escapeHtml(resolved && resolved.display ? resolved.display : '') || '&nbsp;'}</td>`;
                    }).join('');
                    return `<tr><th scope="row" class="sheet-row-label">${rowIndex + rowNumberOffset}</th>${cells}</tr>`;
                }).join('');
                return `
                    <div class="table-responsive">
                        <table class="table table-sm canopy-sheet-table mb-0">
                            <colgroup>${colgroup}</colgroup>
                            <thead><tr><th scope="col" class="sheet-row-label"></th>${headerHtml}</tr></thead>
                            <tbody>${bodyRows || '<tr><td class="text-muted small" colspan="2">No data rows.</td></tr>'}</tbody>
                        </table>
                    </div>
                `;
            }

            function canopyRenderInlineSheetPreviewPanel(spec) {
                const engine = canopyGetSheetEngine();
                const evaluated = engine && typeof engine.evaluateInlineSheetSpec === 'function'
                    ? engine.evaluateInlineSheetSpec(spec)
                    : null;
                if (!evaluated) {
                    return '<div class="small text-muted">Preview unavailable.</div>';
                }
                const columnLayout = engine && typeof engine.buildColumnLayout === 'function'
                    ? engine.buildColumnLayout(spec, evaluated)
                    : [];
                return `
                    <div class="canopy-inline-sheet-preview-label">
                        <span>Live preview</span>
                        <span>${Math.max(0, Number(evaluated.rows ? evaluated.rows.length : 0) - (spec && Array.isArray(spec.columns) && spec.columns.length ? 1 : 0))} rows</span>
                    </div>
                    ${canopyRenderInlineSheetTable(evaluated, {
                        hasColumns: !!(spec && Array.isArray(spec.columns) && spec.columns.length),
                        headerLabels: spec && Array.isArray(spec.columns) ? spec.columns : null,
                        columnLayout: columnLayout
                    })}
                `;
            }

            function canopyRenderInlineSheetView(spec, evaluated, body, language, occurrenceIndex) {
                const safeSource = _escapeHtml(canopyEncodeSheetSource(body));
                const title = _escapeHtml(spec.title || 'Inline sheet');
                const engine = canopyGetSheetEngine();
                const columnLayout = engine && typeof engine.buildColumnLayout === 'function'
                    ? engine.buildColumnLayout(spec, evaluated)
                    : [];
                return `
                    <div class="canopy-inline-sheet"
                         data-inline-sheet="1"
                         data-sheet-source="${safeSource}"
                         data-sheet-language="${_escapeHtml(language || 'sheet')}"
                         data-sheet-occurrence="${Number.isFinite(Number(occurrenceIndex)) ? Number(occurrenceIndex) : 0}">
                        <div class="canopy-inline-sheet-shell">
                            <div class="canopy-inline-sheet-header">
                                <div class="canopy-inline-sheet-heading">
                                    <div class="canopy-inline-sheet-kicker">Inline spreadsheet</div>
                                    <div class="canopy-inline-sheet-title"><i class="bi bi-table me-2"></i>${title}</div>
                                </div>
                                <div class="canopy-inline-sheet-toolbar">
                                    <span class="badge text-bg-secondary">Computed locally</span>
                                    <span class="badge text-bg-dark-subtle text-body-secondary">Safe formulas only</span>
                                    <button type="button" class="btn btn-sm btn-outline-light canopy-inline-sheet-action" onclick="canopyInlineSheetStartEdit(this)">
                                        <i class="bi bi-pencil-square"></i><span>Edit</span>
                                    </button>
                                    <button type="button" class="btn btn-sm btn-outline-light canopy-inline-sheet-action" onclick="canopyInlineSheetCopy(this)">
                                        <i class="bi bi-clipboard"></i><span>Copy block</span>
                                    </button>
                                    <button type="button" class="btn btn-sm btn-success canopy-inline-sheet-action" onclick="canopyInlineSheetApply(this)">
                                        <i class="bi bi-arrow-up-right-square"></i><span>Apply</span>
                                    </button>
                                </div>
                            </div>
                            <div class="canopy-inline-sheet-body">
                                ${canopyRenderInlineSheetTable(evaluated, {
                                    hasColumns: !!(spec && Array.isArray(spec.columns) && spec.columns.length),
                                    headerLabels: spec && Array.isArray(spec.columns) ? spec.columns : null,
                                    columnLayout: columnLayout
                                })}
                            </div>
                        </div>
                    </div>
                `;
            }

            function canopyRenderInlineSheetEditorMarkup(spec, body, language, occurrenceIndex) {
                const rows = Array.isArray(spec.rows) ? spec.rows : [];
                const columns = Array.isArray(spec.columns) ? spec.columns : [];
                const hasColumns = columns.length > 0;
                const engine = canopyGetSheetEngine();
                const evaluated = engine && typeof engine.evaluateInlineSheetSpec === 'function'
                    ? engine.evaluateInlineSheetSpec(spec)
                    : null;
                const columnLayout = engine && typeof engine.buildColumnLayout === 'function' && evaluated
                    ? engine.buildColumnLayout(spec, evaluated, { editable: true })
                    : [];
                const safeSource = _escapeHtml(canopyEncodeSheetSource(body));
                const titleValue = _escapeHtml(spec.title || '');
                const colgroup = [
                    '<col class="canopy-sheet-col-row-label">',
                    ...columns.map(function(_, index) {
                        const column = columnLayout[index] || {};
                        const chars = Math.max(8, Math.min(24, Number(column.chars) || 10));
                        const kind = column.kind === 'number' ? 'number' : 'text';
                        return `<col class="canopy-sheet-col canopy-sheet-col-${kind}" style="width:${chars}ch;">`;
                    })
                ].join('');
                const headerCells = columns.map(function(value, index) {
                    return `
                        <th scope="col">
                            <div class="canopy-sheet-editor-head">
                                <div class="canopy-sheet-editor-col-label">${canopySpreadsheetColumnLabel(index)}</div>
                                <input type="text"
                                       class="form-control form-control-sm"
                                       value="${_escapeHtml(value || '')}"
                                       data-sheet-column-input="${index}"
                                       oninput="canopyInlineSheetRefreshEditor(this)">
                                <button type="button" class="btn btn-sm btn-outline-danger canopy-sheet-delete-axis" onclick="canopyInlineSheetRemoveColumn(this, ${index})" title="Remove column">
                                    <i class="bi bi-x-lg"></i>
                                </button>
                            </div>
                        </th>
                    `;
                }).join('');
                const bodyRows = rows.map(function(row, rowIndex) {
                    const cells = row.map(function(value, colIndex) {
                        const evalRow = rowIndex + (hasColumns ? 1 : 0);
                        const resolved = evaluated && evaluated.rows && evaluated.rows[evalRow] ? evaluated.rows[evalRow][colIndex] : null;
                        const raw = String(value || '').trim();
                        const hint = raw.startsWith('=') && resolved ? '=> ' + String(resolved.display || '') : '';
                        return `
                            <td>
                                <div class="canopy-sheet-editor-cell">
                                    <input type="text"
                                           class="form-control form-control-sm"
                                           value="${_escapeHtml(value || '')}"
                                           data-sheet-cell-input="1"
                                           data-sheet-row-index="${rowIndex}"
                                           data-sheet-col-index="${colIndex}"
                                           oninput="canopyInlineSheetRefreshEditor(this)">
                                    <div class="canopy-sheet-cell-hint" data-sheet-cell-hint${hint ? '' : ' style="display:none;"'}>${_escapeHtml(hint)}</div>
                                </div>
                            </td>
                        `;
                    }).join('');
                    const displayRow = rowIndex + (hasColumns ? 2 : 1);
                    return `
                        <tr data-sheet-row="${rowIndex}">
                            <th scope="row" class="sheet-row-label">
                                <div class="canopy-sheet-editor-rowhead">
                                    <span>${displayRow}</span>
                                    <button type="button" class="btn btn-sm btn-outline-danger canopy-sheet-delete-axis" onclick="canopyInlineSheetRemoveRow(this, ${rowIndex})" title="Remove row">
                                        <i class="bi bi-x-lg"></i>
                                    </button>
                                </div>
                            </th>
                            ${cells}
                        </tr>
                    `;
                }).join('');

                return `
                    <div class="canopy-inline-sheet canopy-inline-sheet-editing"
                         data-inline-sheet="1"
                         data-sheet-source="${safeSource}"
                         data-sheet-language="${_escapeHtml(language || 'sheet')}"
                         data-sheet-occurrence="${Number.isFinite(Number(occurrenceIndex)) ? Number(occurrenceIndex) : 0}">
                        <div class="canopy-inline-sheet-shell">
                            <div class="canopy-inline-sheet-header">
                                <div class="canopy-inline-sheet-heading">
                                    <div class="canopy-inline-sheet-kicker">Inline spreadsheet editor</div>
                                    <input type="text"
                                           class="form-control canopy-inline-sheet-title-input"
                                           value="${titleValue}"
                                           placeholder="Sheet title"
                                           data-sheet-title-input="1"
                                           oninput="canopyInlineSheetRefreshEditor(this)">
                                </div>
                                <div class="canopy-inline-sheet-toolbar">
                                    <button type="button" class="btn btn-sm btn-outline-light canopy-inline-sheet-action" onclick="canopyInlineSheetAddRow(this)">
                                        <i class="bi bi-plus-square"></i><span>Row</span>
                                    </button>
                                    <button type="button" class="btn btn-sm btn-outline-light canopy-inline-sheet-action" onclick="canopyInlineSheetAddColumn(this)">
                                        <i class="bi bi-layout-three-columns"></i><span>Column</span>
                                    </button>
                                    <button type="button" class="btn btn-sm btn-outline-light canopy-inline-sheet-action" onclick="canopyInlineSheetCopy(this)">
                                        <i class="bi bi-clipboard"></i><span>Copy block</span>
                                    </button>
                                    <button type="button" class="btn btn-sm btn-success canopy-inline-sheet-action" onclick="canopyInlineSheetApply(this)">
                                        <i class="bi bi-arrow-up-right-square"></i><span>Apply to editor</span>
                                    </button>
                                    <button type="button" class="btn btn-sm btn-outline-secondary canopy-inline-sheet-action" onclick="canopyInlineSheetCancelEdit(this)">
                                        <i class="bi bi-x-circle"></i><span>Cancel</span>
                                    </button>
                                </div>
                            </div>
                            <div class="canopy-inline-sheet-editor-note">
                                Edit raw cell values or formulas. Preview updates locally. Use <code>Apply to editor</code> to patch the nearest message/post editor and then save normally.
                            </div>
                            <div class="canopy-inline-sheet-editor-grid table-responsive">
                                <table class="table table-sm canopy-sheet-table canopy-sheet-edit-table mb-0">
                                    <colgroup>${colgroup}</colgroup>
                                    <thead>
                                        <tr>
                                            <th scope="col" class="sheet-row-label"></th>
                                            ${headerCells}
                                        </tr>
                                    </thead>
                                    <tbody>${bodyRows}</tbody>
                                </table>
                            </div>
                            <div class="canopy-inline-sheet-live-preview" data-sheet-live-preview>
                                ${canopyRenderInlineSheetPreviewPanel(spec)}
                            </div>
                        </div>
                    </div>
                `;
            }

            function canopyGetInlineSheetWrapper(element) {
                return element ? element.closest('[data-inline-sheet="1"]') : null;
            }

            function canopyCollectInlineSheetSpec(wrapper) {
                const engine = canopyGetSheetEngine();
                if (!wrapper || !engine) return null;
                const titleInput = wrapper.querySelector('[data-sheet-title-input]');
                if (!titleInput) {
                    const source = canopyDecodeSheetSource(wrapper.getAttribute('data-sheet-source'));
                    return engine.parseInlineSheetRows(source);
                }
                const columns = Array.from(wrapper.querySelectorAll('[data-sheet-column-input]')).map(function(input) {
                    return input.value || '';
                });
                const rows = Array.from(wrapper.querySelectorAll('[data-sheet-row]')).map(function(rowEl) {
                    return Array.from(rowEl.querySelectorAll('[data-sheet-cell-input]')).map(function(input) {
                        return input.value || '';
                    });
                });
                return {
                    title: titleInput.value || '',
                    columns: columns,
                    rows: rows,
                };
            }

            function canopyRefreshInlineSheetHints(wrapper, spec, evaluated) {
                const hasColumns = Array.isArray(spec.columns) && spec.columns.length > 0;
                wrapper.querySelectorAll('[data-sheet-cell-input]').forEach(function(input) {
                    const hint = input.parentElement ? input.parentElement.querySelector('[data-sheet-cell-hint]') : null;
                    if (!hint) return;
                    const raw = String(input.value || '').trim();
                    if (!raw.startsWith('=')) {
                        hint.textContent = '';
                        hint.style.display = 'none';
                        return;
                    }
                    const rowIndex = Number(input.getAttribute('data-sheet-row-index'));
                    const colIndex = Number(input.getAttribute('data-sheet-col-index'));
                    const evalRow = rowIndex + (hasColumns ? 1 : 0);
                    const resolved = evaluated && evaluated.rows && evaluated.rows[evalRow] ? evaluated.rows[evalRow][colIndex] : null;
                    hint.textContent = resolved ? '=> ' + String(resolved.display || '') : '';
                    hint.style.display = '';
                });
            }

            function canopyInlineSheetRefreshEditor(element) {
                const wrapper = canopyGetInlineSheetWrapper(element);
                const engine = canopyGetSheetEngine();
                if (!wrapper || !engine) return;
                const spec = canopyCollectInlineSheetSpec(wrapper);
                if (!spec) return;
                const preview = wrapper.querySelector('[data-sheet-live-preview]');
                if (preview) {
                    preview.innerHTML = canopyRenderInlineSheetPreviewPanel(spec);
                }
                const evaluated = engine.evaluateInlineSheetSpec(spec);
                canopyRefreshInlineSheetHints(wrapper, spec, evaluated);
            }

            function canopyRenderInlineSheetEditor(wrapper, spec) {
                if (!wrapper) return;
                const engine = canopyGetSheetEngine();
                const normalized = canopyCloneInlineSheetSpec(spec || {});
                const source = engine && typeof engine.serializeInlineSheetSpec === 'function'
                    ? engine.serializeInlineSheetSpec(normalized)
                    : '';
                wrapper.outerHTML = canopyRenderInlineSheetEditorMarkup(
                    normalized,
                    source,
                    canopyInlineSheetLanguage(wrapper),
                    canopyInlineSheetOccurrence(wrapper)
                );
            }

            function canopyInlineSheetStartEdit(button) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                const engine = canopyGetSheetEngine();
                if (!wrapper || !engine) return;
                const source = canopyDecodeSheetSource(wrapper.getAttribute('data-sheet-source'));
                const spec = engine.parseInlineSheetRows(source);
                if (!spec) return;
                canopyRenderInlineSheetEditor(wrapper, spec);
            }

            function canopyInlineSheetCancelEdit(button) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                const engine = canopyGetSheetEngine();
                if (!wrapper || !engine) return;
                const source = canopyDecodeSheetSource(wrapper.getAttribute('data-sheet-source'));
                const spec = engine.parseInlineSheetRows(source);
                const evaluated = spec ? engine.evaluateInlineSheetSpec(spec) : null;
                if (!spec || !evaluated) return;
                wrapper.outerHTML = canopyRenderInlineSheetView(
                    spec,
                    evaluated,
                    source,
                    canopyInlineSheetLanguage(wrapper),
                    canopyInlineSheetOccurrence(wrapper)
                );
            }

            function canopyInlineSheetAddRow(button) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                const spec = canopyCollectInlineSheetSpec(wrapper);
                if (!wrapper || !spec) return;
                const width = Math.max(1, Array.isArray(spec.columns) ? spec.columns.length : 0, ...(spec.rows || []).map(function(row) { return row.length; }));
                spec.rows.push(Array.from({ length: width }, function() { return ''; }));
                canopyRenderInlineSheetEditor(wrapper, spec);
            }

            function canopyInlineSheetAddColumn(button) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                const spec = canopyCollectInlineSheetSpec(wrapper);
                if (!wrapper || !spec) return;
                spec.columns.push('');
                spec.rows = (spec.rows || []).map(function(row) {
                    const next = Array.isArray(row) ? row.slice() : [];
                    next.push('');
                    return next;
                });
                canopyRenderInlineSheetEditor(wrapper, spec);
            }

            function canopyInlineSheetRemoveRow(button, rowIndex) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                const spec = canopyCollectInlineSheetSpec(wrapper);
                if (!wrapper || !spec) return;
                spec.rows.splice(Number(rowIndex), 1);
                canopyRenderInlineSheetEditor(wrapper, spec);
            }

            function canopyInlineSheetRemoveColumn(button, colIndex) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                const spec = canopyCollectInlineSheetSpec(wrapper);
                if (!wrapper || !spec) return;
                spec.columns.splice(Number(colIndex), 1);
                spec.rows = (spec.rows || []).map(function(row) {
                    const next = Array.isArray(row) ? row.slice() : [];
                    next.splice(Number(colIndex), 1);
                    return next;
                });
                canopyRenderInlineSheetEditor(wrapper, spec);
            }

            function canopyCopyTextToClipboard(text) {
                const value = String(text || '');
                if (navigator.clipboard && navigator.clipboard.writeText) {
                    return navigator.clipboard.writeText(value);
                }
                const ta = document.createElement('textarea');
                ta.value = value;
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                return Promise.resolve();
            }

            function canopyBuildInlineSheetBody(wrapper) {
                const engine = canopyGetSheetEngine();
                if (!engine) return '';
                const spec = canopyCollectInlineSheetSpec(wrapper);
                if (!spec) return '';
                return engine.serializeInlineSheetSpec(spec);
            }

            function canopyInlineSheetCopy(button) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                if (!wrapper) return;
                const body = canopyBuildInlineSheetBody(wrapper) || canopyDecodeSheetSource(wrapper.getAttribute('data-sheet-source'));
                canopyCopyTextToClipboard(canopyInlineSheetFence(body, canopyInlineSheetLanguage(wrapper)))
                    .then(function() {
                        if (typeof showAlert === 'function') showAlert('Sheet block copied to clipboard', 'success');
                    })
                    .catch(function() {
                        if (typeof showAlert === 'function') showAlert('Could not copy sheet block', 'danger');
                    });
            }

            function canopyWaitForTextarea(getter, timeoutMs) {
                const timeout = Number(timeoutMs || 1200);
                return new Promise(function(resolve) {
                    const started = Date.now();
                    (function poll() {
                        const el = getter();
                        if (el) {
                            resolve(el);
                            return;
                        }
                        if (Date.now() - started >= timeout) {
                            resolve(null);
                            return;
                        }
                        window.setTimeout(poll, 40);
                    })();
                });
            }

            function canopyInsertSheetAtSelection(textarea, blockText) {
                const value = String(textarea.value || '');
                const start = typeof textarea.selectionStart === 'number' ? textarea.selectionStart : value.length;
                const end = typeof textarea.selectionEnd === 'number' ? textarea.selectionEnd : value.length;
                const prefix = start > 0 && !/\n$/.test(value.slice(0, start)) ? '\n\n' : '';
                const suffix = end < value.length && !/^\n/.test(value.slice(end)) ? '\n\n' : '';
                textarea.value = value.slice(0, start) + prefix + blockText + suffix + value.slice(end);
                const caret = start + prefix.length + blockText.length;
                if (typeof textarea.setSelectionRange === 'function') {
                    textarea.setSelectionRange(caret, caret);
                }
            }

            async function canopyResolveInlineSheetTargetTextarea(wrapper) {
                const activeCandidates = [
                    wrapper && wrapper.closest('.message-item[data-message-id]') ? wrapper.closest('.message-item[data-message-id]').querySelector('.channel-inline-editor textarea[data-inline-edit-content]') : null,
                    wrapper && wrapper.closest('.post-card[data-post-id]') ? wrapper.closest('.post-card[data-post-id]').querySelector('.feed-inline-editor textarea[data-inline-post-content]') : null,
                    wrapper && wrapper.closest('.message-item[data-message-id]') ? wrapper.closest('.message-item[data-message-id]').querySelector('.dm-inline-editor textarea[data-inline-dm-content]') : null,
                ].filter(Boolean);
                if (activeCandidates.length) return activeCandidates[0];

                const postCard = wrapper ? wrapper.closest('.post-card[data-post-id]') : null;
                if (postCard && typeof editPost === 'function') {
                    const postId = postCard.getAttribute('data-post-id');
                    editPost(postId);
                    return canopyWaitForTextarea(function() {
                        return postCard.querySelector('.feed-inline-editor textarea[data-inline-post-content]');
                    });
                }

                const messageCard = wrapper ? wrapper.closest('.message-item[data-message-id]') : null;
                if (messageCard) {
                    const messageId = messageCard.getAttribute('data-message-id');
                    if (typeof editChannelMessage === 'function' && messageCard.querySelector('.message-actions [onclick*="editChannelMessage"]')) {
                        editChannelMessage(messageId);
                        return canopyWaitForTextarea(function() {
                            return messageCard.querySelector('.channel-inline-editor textarea[data-inline-edit-content]');
                        });
                    }
                    if (typeof editMessage === 'function' && messageCard.querySelector('[data-message-actions] [onclick*="editMessage"]')) {
                        editMessage(messageId);
                        return canopyWaitForTextarea(function() {
                            return messageCard.querySelector('.dm-inline-editor textarea[data-inline-dm-content]');
                        });
                    }
                }

                const composerCandidates = [
                    document.activeElement && document.activeElement.matches && document.activeElement.matches('textarea') ? document.activeElement : null,
                    document.getElementById('message-input'),
                    document.getElementById('postContent'),
                    document.getElementById('messageContent'),
                ].filter(Boolean);
                if (composerCandidates.length) return composerCandidates[0];

                return null;
            }

            async function canopyInlineSheetApply(button) {
                const wrapper = canopyGetInlineSheetWrapper(button);
                if (!wrapper) return;
                const body = canopyBuildInlineSheetBody(wrapper) || canopyDecodeSheetSource(wrapper.getAttribute('data-sheet-source'));
                const originalBody = canopyDecodeSheetSource(wrapper.getAttribute('data-sheet-source'));
                const language = canopyInlineSheetLanguage(wrapper);
                const textarea = await canopyResolveInlineSheetTargetTextarea(wrapper);
                if (!textarea) {
                    return canopyCopyTextToClipboard(canopyInlineSheetFence(body, language))
                        .then(function() {
                            if (typeof showAlert === 'function') {
                                showAlert('No editable message or composer was available. The sheet block was copied instead.', 'info');
                            }
                        })
                        .catch(function() {
                            if (typeof showAlert === 'function') {
                                showAlert('No editable message or composer was available, and copying the sheet block failed.', 'danger');
                            }
                        });
                }

                const replaced = canopyReplaceInlineSheetBlock(
                    textarea.value,
                    originalBody,
                    body,
                    language,
                    canopyInlineSheetOccurrence(wrapper)
                );
                if (replaced == null) {
                    canopyInsertSheetAtSelection(textarea, canopyInlineSheetFence(body, language));
                } else {
                    textarea.value = replaced;
                }
                wrapper.setAttribute('data-sheet-source', canopyEncodeSheetSource(body));
                textarea.dispatchEvent(new Event('input', { bubbles: true }));
                textarea.focus();
                if (typeof showAlert === 'function') {
                    showAlert('Sheet applied to the editor. Save the message or post to persist it.', 'success');
                }
            }

            if (typeof window !== 'undefined') {
                window.canopyInlineSheetAddColumn = canopyInlineSheetAddColumn;
                window.canopyInlineSheetAddRow = canopyInlineSheetAddRow;
                window.canopyInlineSheetApply = canopyInlineSheetApply;
                window.canopyInlineSheetCancelEdit = canopyInlineSheetCancelEdit;
                window.canopyInlineSheetCopy = canopyInlineSheetCopy;
                window.canopyInlineSheetRefreshEditor = canopyInlineSheetRefreshEditor;
                window.canopyInlineSheetRemoveColumn = canopyInlineSheetRemoveColumn;
                window.canopyInlineSheetRemoveRow = canopyInlineSheetRemoveRow;
                window.canopyInlineSheetStartEdit = canopyInlineSheetStartEdit;
            }

            function renderInlineSheetBlock(body, language, occurrenceIndex) {
                const engine = canopyGetSheetEngine();
                const spec = engine && typeof engine.parseInlineSheetRows === 'function'
                    ? engine.parseInlineSheetRows(body)
                    : null;
                if (!spec) {
                    return renderInlineSheetFallback(body);
                }
                const evaluated = engine && typeof engine.evaluateInlineSheetSpec === 'function'
                    ? engine.evaluateInlineSheetSpec(spec)
                    : null;
                if (!evaluated) {
                    return renderInlineSheetFallback(body);
                }
                return canopyRenderInlineSheetView(
                    spec,
                    evaluated,
                    String(body || '').trim(),
                    language || 'sheet',
                    occurrenceIndex
                );
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

	            const rawText = String(text);
            const protectedBlocks = [];
            const BLOCK_PLACEHOLDER = '\x00BLOCK_';
            let sheetBlockOccurrence = 0;
            const protectedText = rawText.replace(/```([A-Za-z0-9_-]+)?\n?([\s\S]*?)```/g, function(match, language, body) {
                const lang = String(language || '').trim().toLowerCase();
                const inner = String(body || '').replace(/^\n/, '').replace(/\n$/, '');
                const idx = protectedBlocks.length;
                if (lang === 'sheet' || lang === 'spreadsheet') {
                    protectedBlocks.push(renderInlineSheetBlock(inner, lang, sheetBlockOccurrence));
                    sheetBlockOccurrence += 1;
                } else {
                    const escaped = _escapeHtml(inner.trim());
                    protectedBlocks.push('<div class="channel-code-wrap position-relative mb-2">' +
                        '<button type="button" class="channel-code-copy-btn btn btn-sm position-absolute top-0 end-0 m-1" title="Copy to clipboard" aria-label="Copy">' +
                        '<i class="bi bi-clipboard"></i></button>' +
                        '<pre class="channel-code p-2 pe-4 rounded mb-0" style="background:var(--canopy-bg-tertiary); border:1px solid var(--canopy-border); overflow-x:auto;"><code>' + escaped + '</code></pre></div>');
                }
                return BLOCK_PLACEHOLDER + idx + '\x00';
            });

	            // Escape HTML first to prevent XSS
            const div = document.createElement('div');
            div.textContent = protectedText;
            let html = div.innerHTML;

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
            html = html.replace(/!\[([^\]]*)\]\(file:([A-Za-z0-9_-]+)\)/g, function(match, alt, fileId) {
                var src = '/files/' + fileId;
                var altEsc = (alt || '').replace(/"/g, '&quot;');
                var encodedUrls = encodeURIComponent(JSON.stringify([src]));
                return '<span class="channel-inline-image-wrap channel-inline-image-block">' +
                    '<img src="' + src + '" alt="' + altEsc + '" class="channel-inline-image channel-inline-image-full" ' +
                    'onclick="openLightbox(0, \'' + encodedUrls + '\')" ' +
                    'onerror="this.onerror=null;this.style.display=\'none\';var s=this.nextElementSibling;if(s)s.classList.remove(\'d-none\');">' +
                    '<span class="d-none small text-muted">Image unavailable</span>' +
                    '</span>';
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
            html = renderCanopyInlineMarkdown(html);
            html = renderCanopyBlockMarkdown(html);
            html = html.replace(/\n/g, '<br>');

            const embedState = collectProviderEmbeds(html);
            html = embedState.html;
            const embeds = embedState.embeds;
            const EMBED_PLACEHOLDER = embedState.placeholderPrefix;

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
                    const hasProtectedBlock = textPart.includes(BLOCK_PLACEHOLDER);
                    if (hasProtectedBlock) {
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

	            if (protectedBlocks.length) {
	                for (let i = 0; i < protectedBlocks.length; i++) {
	                    html = html.replace(BLOCK_PLACEHOLDER + i + '\x00', protectedBlocks[i]);
	                }
	            }

                // Render LaTeX-style equations when KaTeX is available.
                if (containsMathDelimiters(html)) {
                    const probe = document.createElement('div');
                    probe.innerHTML = html;
                    renderMathInElementSafe(probe);
                    html = probe.innerHTML;
                }

	            // Wrap in paragraph or div (div if we have block elements like <pre> so markup stays valid)
            if (!html.includes('embed-preview') && !html.includes('embed-grid')) {
                if (html.includes('<pre') || html.includes('<pre ') || html.includes('<div') || html.includes('<table') || html.includes('<ul') || html.includes('<ol') || html.includes('<blockquote')) {
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
		                var shouldProcess = /https?:\/\//.test(rawText) || /\]\(\/files\//.test(rawText) || /!\[/.test(rawText) || /```/.test(rawText) || hasCanopyMarkdownSyntax(rawText) || containsMathDelimiters(rawText);
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

        window.canopyCopyTextToClipboard = _copyTextToClipboard;

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
                                        <button type="button" class="btn btn-outline-info btn-sm" id="user-identity-resync-avatar" title="Re-download avatar from the network">
                                            <i class="bi bi-arrow-repeat me-1"></i>Resync Avatar
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
                const resyncBtn = modalEl.querySelector('#user-identity-resync-avatar');
                if (resyncBtn) {
                    resyncBtn.addEventListener('click', () => {
                        const uid = resyncBtn.getAttribute('data-user-id') || '';
                        const peer = resyncBtn.getAttribute('data-origin-peer') || '';
                        if (!uid) return;
                        resyncBtn.disabled = true;
                        resyncBtn.innerHTML = '<i class="bi bi-hourglass-split me-1"></i>Syncing...';
                        apiCall('/ajax/resync_user_avatar', {
                            method: 'POST',
                            body: JSON.stringify({ user_id: uid, origin_peer: peer }),
                        })
                        .then(data => {
                            if (data && data.ok) {
                                resyncBtn.innerHTML = '<i class="bi bi-check-circle me-1"></i>Requested';
                                resyncBtn.classList.replace('btn-outline-info', 'btn-outline-success');
                                showAlert('Avatar resync requested — it may take a moment to arrive from the network.', 'success');
                            } else {
                                resyncBtn.innerHTML = '<i class="bi bi-x-circle me-1"></i>Failed';
                                resyncBtn.classList.replace('btn-outline-info', 'btn-outline-warning');
                                showAlert(data.reason || data.error || 'Resync failed', 'warning');
                            }
                        })
                        .catch(() => {
                            resyncBtn.innerHTML = '<i class="bi bi-x-circle me-1"></i>Error';
                            resyncBtn.classList.replace('btn-outline-info', 'btn-outline-danger');
                            showAlert('Avatar resync request failed.', 'danger');
                        })
                        .finally(() => {
                            setTimeout(() => {
                                resyncBtn.disabled = false;
                                resyncBtn.innerHTML = '<i class="bi bi-arrow-repeat me-1"></i>Resync Avatar';
                                resyncBtn.className = 'btn btn-outline-info btn-sm';
                            }, 5000);
                        });
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
            const originPeerRaw = String(source.origin_peer || originPeerFromEl || '').trim();
            const originPeer = (canopyLocalPeerId && originPeerRaw === canopyLocalPeerId) ? '' : originPeerRaw;
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

            const resyncBtn = modalEl.querySelector('#user-identity-resync-avatar');
            if (resyncBtn) {
                resyncBtn.setAttribute('data-user-id', info.user_id || '');
                resyncBtn.setAttribute('data-origin-peer', info.origin_peer || '');
                resyncBtn.disabled = !info.user_id || !info.is_remote;
                resyncBtn.title = info.is_remote
                    ? 'Re-download avatar from the network'
                    : 'Only available for remote users';
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

        // --- Mention identity toolbar ---
        let _mentionIdentityToolbar = null;
        let _mentionIdentityState = null;

        function _mentionCurrentUserId() {
            return encodeURIComponent(String((window.CANOPY_VARS && (window.CANOPY_VARS.userId || window.CANOPY_VARS.localUserId)) || 'anon'));
        }

        function _mentionPendingDmDraftKey(userId) {
            return `canopy.pendingMentionDmDraft.v1.${_mentionCurrentUserId()}.${encodeURIComponent(String(userId || ''))}`;
        }

        function _selectionHasVisibleText() {
            const selection = window.getSelection ? window.getSelection() : null;
            return !!(selection && !selection.isCollapsed && String(selection.toString() || '').trim());
        }

        function _mentionMessagesUrl(userId) {
            const base = (((window.CANOPY_VARS || {}).urls || {}).messages || '/messages');
            return `${base}?with=${encodeURIComponent(String(userId || ''))}`;
        }

        function _mentionDraftForUser(info) {
            const label = (info && (info.display_name || info.username || info.user_id)) || 'this person';
            return `@Canopy Draft a concise direct message to ${label} about:\n\n`;
        }

        function _ensureMentionIdentityToolbar() {
            if (_mentionIdentityToolbar) return _mentionIdentityToolbar;
            const el = document.createElement('div');
            el.id = 'canopy-mention-identity-toolbar';
            el.className = 'mention-identity-toolbar';
            el.setAttribute('role', 'dialog');
            el.setAttribute('aria-label', 'Mention actions');
            el.setAttribute('aria-hidden', 'true');
            el.innerHTML = `
                <div class="mention-identity-card">
                    <div class="mention-identity-head">
                        <div class="mention-identity-avatar" data-mention-avatar></div>
                        <div class="mention-identity-meta">
                            <div class="mention-identity-name" data-mention-name>User</div>
                            <div class="mention-identity-subtitle" data-mention-subtitle>@mention</div>
                        </div>
                        <button type="button" class="mention-identity-close" data-mention-toolbar-action="close" aria-label="Close mention actions">
                            <i class="bi bi-x"></i>
                        </button>
                    </div>
                    <div class="mention-identity-status" data-mention-status></div>
                    <div class="mention-identity-actions">
                        <button type="button" data-mention-toolbar-action="profile"><i class="bi bi-person-badge"></i><span>Profile</span></button>
                        <button type="button" data-mention-toolbar-action="dm"><i class="bi bi-chat-dots"></i><span>DM</span></button>
                        <button type="button" data-mention-toolbar-action="canopy-dm"><i class="bi bi-stars"></i><span>@Canopy DM</span></button>
                        <button type="button" data-mention-toolbar-action="copy"><i class="bi bi-at"></i><span>Copy</span></button>
                    </div>
                </div>`;
            document.body.appendChild(el);
            el.addEventListener('click', handleMentionToolbarAction);
            _mentionIdentityToolbar = el;
            return el;
        }

        function _setMentionToolbarBusy(toolbar, busy) {
            toolbar.querySelectorAll('[data-mention-toolbar-action="profile"], [data-mention-toolbar-action="dm"], [data-mention-toolbar-action="canopy-dm"]').forEach(btn => {
                btn.disabled = !!busy;
            });
        }

        function _mentionLabel(info, handle) {
            return String((info && (info.display_name || info.username || info.user_id)) || handle || 'User').trim();
        }

        function _renderMentionToolbar(info, options = {}) {
            const toolbar = _ensureMentionIdentityToolbar();
            const state = _mentionIdentityState || {};
            const handle = state.handle || '';
            const label = _mentionLabel(info, handle);
            const userId = String((info && info.user_id) || '').trim();
            const avatarUrl = _safeImageSrc(info && info.avatar_url);
            const avatar = toolbar.querySelector('[data-mention-avatar]');
            const name = toolbar.querySelector('[data-mention-name]');
            const subtitle = toolbar.querySelector('[data-mention-subtitle]');
            const status = toolbar.querySelector('[data-mention-status]');
            if (avatar) {
                avatar.innerHTML = avatarUrl
                    ? `<img src="${_escapeHtml(avatarUrl)}" alt="${_escapeHtml(label)}">`
                    : `<span>${_escapeHtml(label.slice(0, 2).toUpperCase())}</span>`;
            }
            if (name) name.textContent = label;
            if (subtitle) {
                const bits = [];
                bits.push(`@${handle || (info && info.username) || userId || 'mention'}`);
                if (info && info.account_type) bits.push(String(info.account_type));
                if (info && info.presence_label) bits.push(String(info.presence_label));
                if (info && info.is_remote) bits.push('remote');
                subtitle.textContent = bits.join(' · ');
            }
            if (status) {
                status.textContent = options.loading
                    ? 'Resolving identity...'
                    : (userId ? 'Choose an action for this identity.' : 'Mention text recognized; identity not resolved yet.');
            }
            _setMentionToolbarBusy(toolbar, options.loading || !userId);
        }

        function _positionMentionToolbar(triggerEl) {
            const toolbar = _ensureMentionIdentityToolbar();
            if (!triggerEl || !toolbar) return;
            toolbar.classList.add('is-open');
            toolbar.setAttribute('aria-hidden', 'false');
            const rect = triggerEl.getBoundingClientRect();
            const width = toolbar.offsetWidth || 320;
            const gutter = 12;
            const left = Math.min(Math.max(rect.left + rect.width / 2 - width / 2, gutter), window.innerWidth - width - gutter);
            const fitsBelow = rect.bottom + 12 + (toolbar.offsetHeight || 190) < window.innerHeight;
            toolbar.style.left = `${Math.round(left)}px`;
            toolbar.style.top = `${Math.round(fitsBelow ? rect.bottom + 8 : Math.max(gutter, rect.top - (toolbar.offsetHeight || 190) - 8))}px`;
        }

        function closeMentionIdentityToolbar() {
            if (!_mentionIdentityToolbar) return;
            _mentionIdentityToolbar.classList.remove('is-open');
            _mentionIdentityToolbar.setAttribute('aria-hidden', 'true');
            _mentionIdentityState = null;
        }

        function _resolveMentionIdentity(handle, userId) {
            const cleanHandle = String(handle || '').trim();
            const cleanUserId = String(userId || '').trim();
            const cached = cleanUserId
                ? (_userIdentityCache[cleanUserId] || getMentionIdentityForHandle(cleanUserId))
                : getMentionIdentityForHandle(cleanHandle);
            if (cached && cached.user_id) {
                return Promise.resolve(_userIdentityInfoFromPayload(cached.user_id, cached.display_name || cached.username || cleanHandle, null, cached));
            }
            if (cleanUserId) {
                return _fetchUserIdentityInfo(cleanUserId).then(payload => (
                    _userIdentityInfoFromPayload(cleanUserId, cleanHandle, null, payload || {})
                ));
            }
            if (!cleanHandle) return Promise.resolve(null);
            return apiCall(`/ajax/mention_suggestions?q=${encodeURIComponent(cleanHandle)}&limit=8`)
                .then(data => {
                    const users = data && Array.isArray(data.users) ? data.users : [];
                    rememberMentionUsers(users);
                    const lower = normalizeMentionIdentityKey(cleanHandle);
                    const match = users.find(user => {
                        return [user.user_id, user.username, user.handle].some(value => normalizeMentionIdentityKey(value) === lower);
                    }) || users[0] || null;
                    if (!match || !match.user_id) return null;
                    return _userIdentityInfoFromPayload(match.user_id, match.display_name || match.username || cleanHandle, null, match);
                });
        }

        function showMentionIdentityToolbar(triggerEl) {
            if (!triggerEl) return;
            const handle = String(triggerEl.getAttribute('data-mention') || '').trim();
            const userId = String(triggerEl.getAttribute('data-mention-user-id') || '').trim();
            if (!handle) return;
            const localInfo = userId
                ? (_userIdentityCache[userId] || getMentionIdentityForHandle(userId) || getMentionIdentityForHandle(handle))
                : getMentionIdentityForHandle(handle);
            _mentionIdentityState = { triggerEl, handle, userId, info: localInfo || null };
            _renderMentionToolbar(localInfo || { user_id: userId, username: handle, display_name: triggerEl.textContent || handle }, { loading: !localInfo && !userId });
            _positionMentionToolbar(triggerEl);
            _resolveMentionIdentity(handle, userId)
                .then(info => {
                    if (!_mentionIdentityState || _mentionIdentityState.triggerEl !== triggerEl) return;
                    if (info && info.user_id) {
                        _mentionIdentityState.userId = info.user_id;
                        _mentionIdentityState.info = info;
                        rememberMentionUsers([info]);
                    }
                    _renderMentionToolbar(_mentionIdentityState.info || info || null, { loading: false });
                    _positionMentionToolbar(triggerEl);
                })
                .catch(() => {
                    if (!_mentionIdentityState || _mentionIdentityState.triggerEl !== triggerEl) return;
                    _renderMentionToolbar(_mentionIdentityState.info || null, { loading: false });
                });
        }

        function _openMentionDm(info, useCanopyDraft) {
            const userId = String(info && info.user_id || '').trim();
            if (!userId) {
                showAlert('Could not resolve that mention to a user.', 'warning');
                return;
            }
            const draft = useCanopyDraft ? _mentionDraftForUser(info) : '';
            const fallbackToMessages = () => {
                if (draft && window.sessionStorage) {
                    try {
                        window.sessionStorage.setItem(_mentionPendingDmDraftKey(userId), draft);
                    } catch (_) {}
                }
                window.location.href = _mentionMessagesUrl(userId);
            };
            closeMentionIdentityToolbar();
            if (typeof window.openDeckInbox === 'function') {
                window.openDeckInbox({ userId }).then((result) => {
                    if (result === null) {
                        fallbackToMessages();
                        return;
                    }
                    if (!draft) return;
                    window.setTimeout(() => {
                        const textarea = document.querySelector('#deck-inbox-surface [data-dm-role="message-content"]');
                        if (!textarea) return;
                        if (!String(textarea.value || '').trim()) {
                            textarea.value = draft;
                            textarea.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                        textarea.focus();
                    }, 120);
                }).catch(fallbackToMessages);
                return;
            }
            fallbackToMessages();
        }

        function _resolveCurrentMentionForAction() {
            const state = _mentionIdentityState;
            if (!state) return Promise.resolve(null);
            if (state.info && state.info.user_id) return Promise.resolve(state.info);
            return _resolveMentionIdentity(state.handle, state.userId).then(info => {
                if (info && _mentionIdentityState === state) {
                    state.info = info;
                    state.userId = info.user_id || state.userId;
                    rememberMentionUsers([info]);
                }
                return info;
            });
        }

        function handleMentionToolbarAction(event) {
            const btn = event.target && event.target.closest ? event.target.closest('[data-mention-toolbar-action]') : null;
            if (!btn || !_mentionIdentityToolbar || !_mentionIdentityToolbar.contains(btn)) return;
            event.preventDefault();
            event.stopPropagation();
            const action = btn.getAttribute('data-mention-toolbar-action') || '';
            if (action === 'close') {
                closeMentionIdentityToolbar();
                return;
            }
            if (action === 'copy') {
                const handle = _mentionIdentityState && _mentionIdentityState.handle;
                _copyTextToClipboard(handle ? `@${handle}` : '', '@mention');
                closeMentionIdentityToolbar();
                return;
            }
            _resolveCurrentMentionForAction().then(info => {
                if (!info || !info.user_id) {
                    showAlert('Could not resolve that mention to a user.', 'warning');
                    return;
                }
                if (action === 'profile') {
                    const triggerEl = _mentionIdentityState && _mentionIdentityState.triggerEl;
                    closeMentionIdentityToolbar();
                    copyUserId(info.user_id, info.display_name || info.username || info.user_id, triggerEl);
                } else if (action === 'dm') {
                    _openMentionDm(info, false);
                } else if (action === 'canopy-dm') {
                    _openMentionDm(info, true);
                }
            });
        }

        function handleMentionIdentityClick(event) {
            const target = event.target instanceof Element ? event.target.closest('.mention-tag[data-mention]') : null;
            if (!target) {
                if (_mentionIdentityToolbar && !_mentionIdentityToolbar.contains(event.target)) {
                    closeMentionIdentityToolbar();
                }
                return;
            }
            if (_selectionHasVisibleText()) return;
            event.preventDefault();
            event.stopPropagation();
            showMentionIdentityToolbar(target);
        }

        function handleMentionIdentityKeydown(event) {
            const target = event.target instanceof Element ? event.target.closest('.mention-tag[data-mention]') : null;
            if (!target) return;
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            showMentionIdentityToolbar(target);
        }

        document.addEventListener('click', handleMentionIdentityClick);
        document.addEventListener('keydown', handleMentionIdentityKeydown);
        window.addEventListener('resize', closeMentionIdentityToolbar, { passive: true });
        window.addEventListener('scroll', closeMentionIdentityToolbar, { passive: true, capture: true });

        window.CanopyMentionActions = {
            close: closeMentionIdentityToolbar,
            pendingDmDraftKey: _mentionPendingDmDraftKey,
            openForElement: showMentionIdentityToolbar,
        };

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

	        (function initWorkspaceSearch(global) {
	            const SEARCH_MIN_LENGTH = 2;
	            const SEARCH_DEBOUNCE_MS = 180;
	            const DEFAULT_SCOPE = 'all';
	            const state = {
	                open: false,
	                query: '',
	                scope: DEFAULT_SCOPE,
	                timer: null,
	                controller: null,
	                activeIndex: -1,
	                lastResults: [],
	                requestSerial: 0,
	                returnFocusEl: null,
	            };

	            function routes() {
	                return (global.CANOPY_VARS && global.CANOPY_VARS.urls) || {};
	            }

	            function endpoint() {
	                return routes().workspaceSearch || '/ajax/workspace_search';
	            }

	            function elements() {
	                return {
	                    trigger: document.getElementById('workspaceSearchTrigger'),
	                    overlay: document.getElementById('workspaceSearchOverlay'),
	                    input: document.getElementById('workspaceSearchInput'),
	                    close: document.getElementById('workspaceSearchClose'),
	                    status: document.getElementById('workspaceSearchStatus'),
	                    results: document.getElementById('workspaceSearchResults'),
	                    filters: Array.from(document.querySelectorAll('[data-workspace-search-scope]')),
	                };
	            }

	            function setStatus(message) {
	                const status = elements().status;
	                if (status) status.textContent = message || '';
	            }

	            function clearSearchTimer() {
	                if (state.timer) {
	                    global.clearTimeout(state.timer);
	                    state.timer = null;
	                }
	            }

	            function abortSearch() {
	                if (state.controller) {
	                    state.controller.abort();
	                    state.controller = null;
	                }
	            }

	            function escapeHtml(value) {
	                return typeof canopyEscapeHtml === 'function' ? canopyEscapeHtml(value) : String(value || '');
	            }

	            function highlightQuery(text, query) {
	                const raw = String(text || '').trim();
	                const needle = String(query || '').trim();
	                if (!raw || !needle) return escapeHtml(raw);
	                const lowerRaw = raw.toLocaleLowerCase();
	                const lowerNeedle = needle.toLocaleLowerCase();
	                const index = lowerRaw.indexOf(lowerNeedle);
	                if (index === -1) return escapeHtml(raw);
	                return [
	                    escapeHtml(raw.slice(0, index)),
	                    '<mark>',
	                    escapeHtml(raw.slice(index, index + needle.length)),
	                    '</mark>',
	                    escapeHtml(raw.slice(index + needle.length)),
	                ].join('');
	            }

	            function resultTimestamp(result) {
	                const value = result && (result.updated_at || result.created_at);
	                return value ? formatTimestamp(value) : '';
	            }

	            function renderEmpty(title, body, icon = 'bi-search') {
	                const results = elements().results;
	                if (!results) return;
	                results.innerHTML = `
	                    <div class="workspace-search-empty">
	                        <div>
	                            <div class="fs-3 mb-2"><i class="bi ${escapeHtml(icon)}"></i></div>
	                            <div class="fw-bold mb-1">${escapeHtml(title)}</div>
	                            <div>${escapeHtml(body)}</div>
	                        </div>
	                    </div>
	                `;
	                state.activeIndex = -1;
	                state.lastResults = [];
	            }

	            function renderLoading() {
	                const results = elements().results;
	                if (!results) return;
	                results.innerHTML = `
	                    <div class="workspace-search-empty">
	                        <div>
	                            <div class="spinner-border spinner-border-sm text-success mb-2" role="status" aria-hidden="true"></div>
	                            <div class="fw-bold mb-1">Searching this meshspace</div>
	                            <div>Checking accessible messages, posts, files, and work cards.</div>
	                        </div>
	                    </div>
	                `;
	            }

	            function flattenGroups(groups) {
	                const flat = [];
	                (groups || []).forEach(group => {
	                    (group.results || []).forEach(result => flat.push(result));
	                });
	                return flat;
	            }

	            function renderGroups(payload) {
	                const resultsEl = elements().results;
	                if (!resultsEl) return;
	                const groups = Array.isArray(payload && payload.groups) ? payload.groups : [];
	                state.lastResults = flattenGroups(groups);
	                state.activeIndex = state.lastResults.length ? 0 : -1;
	                if (!groups.length || !state.lastResults.length) {
	                    renderEmpty('No matches yet', 'Try a different word, file name, channel topic, or person.', 'bi-search-heart');
	                    setStatus('0 results');
	                    return;
	                }
	                let resultIndex = 0;
	                resultsEl.innerHTML = groups.map(group => {
	                    const groupResults = Array.isArray(group.results) ? group.results : [];
	                    const items = groupResults.map(result => {
	                        const index = resultIndex++;
	                        const thumb = result.thumb_url
	                            ? `<img src="${escapeHtml(result.thumb_url)}" alt="">`
	                            : `<i class="bi ${escapeHtml(result.icon || group.icon || 'bi-search')}"></i>`;
	                        const metaParts = [
	                            result.subtitle || '',
	                            resultTimestamp(result),
	                        ].filter(Boolean);
	                        return `
	                            <a class="workspace-search-result${index === 0 ? ' is-active' : ''}"
	                               href="${escapeHtml(result.href || '#')}"
	                               data-workspace-search-result
	                               data-workspace-search-index="${index}"
	                               role="option"
	                               aria-selected="${index === 0 ? 'true' : 'false'}">
	                                <span class="workspace-search-result-icon">${thumb}</span>
	                                <span class="workspace-search-result-main">
	                                    <span class="workspace-search-result-title">
	                                        <span class="workspace-search-result-label">${escapeHtml(result.label || group.label || 'Result')}</span>
	                                        <span>${escapeHtml(result.title || 'Untitled')}</span>
	                                    </span>
	                                    <span class="workspace-search-result-snippet">${highlightQuery(result.snippet || '', payload.query || state.query)}</span>
	                                    <span class="workspace-search-result-meta">${escapeHtml(metaParts.join(' · '))}</span>
	                                </span>
	                                <span class="workspace-search-result-go"><i class="bi bi-arrow-right"></i></span>
	                            </a>
	                        `;
	                    }).join('');
	                    return `
	                        <section class="workspace-search-group" aria-label="${escapeHtml(group.label || 'Results')}">
	                            <div class="workspace-search-group-title">
	                                <i class="bi ${escapeHtml(group.icon || 'bi-collection')}"></i>
	                                <span>${escapeHtml(group.label || 'Results')}</span>
	                                <span>${escapeHtml(group.count || groupResults.length)}</span>
	                            </div>
	                            ${items}
	                        </section>
	                    `;
	                }).join('');
	                setStatus(`${state.lastResults.length} result${state.lastResults.length === 1 ? '' : 's'}`);
	            }

	            function setActiveIndex(nextIndex) {
	                const resultEls = Array.from(document.querySelectorAll('[data-workspace-search-result]'));
	                if (!resultEls.length) {
	                    state.activeIndex = -1;
	                    return;
	                }
	                const bounded = ((nextIndex % resultEls.length) + resultEls.length) % resultEls.length;
	                state.activeIndex = bounded;
	                resultEls.forEach((el, index) => {
	                    const active = index === bounded;
	                    el.classList.toggle('is-active', active);
	                    el.setAttribute('aria-selected', active ? 'true' : 'false');
	                    if (active) {
	                        el.scrollIntoView({ block: 'nearest' });
	                    }
	                });
	            }

	            async function runSearch() {
	                clearSearchTimer();
	                abortSearch();
	                const query = String(elements().input?.value || '').trim();
	                state.query = query;
	                if (query.length < SEARCH_MIN_LENGTH) {
	                    setStatus(`Type at least ${SEARCH_MIN_LENGTH} characters`);
	                    renderEmpty('Search across your meshspace', 'Find accessible channels, DMs, feed posts, Vault files, and work cards.', 'bi-search');
	                    return;
	                }
	                renderLoading();
	                setStatus('Searching...');
	                state.controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
	                const requestSerial = ++state.requestSerial;
	                const requestScope = state.scope || DEFAULT_SCOPE;
	                const params = new URLSearchParams({
	                    q: query,
	                    scope: requestScope,
	                    limit: '40',
	                });
	                try {
	                    const payload = await apiCall(`${endpoint()}?${params.toString()}`, {
	                        method: 'GET',
	                        signal: state.controller ? state.controller.signal : undefined,
	                    });
	                    if (requestSerial !== state.requestSerial || query !== state.query || requestScope !== (state.scope || DEFAULT_SCOPE)) {
	                        return;
	                    }
	                    if (!payload || payload.success === false) {
	                        throw new Error((payload && payload.error) || 'Workspace search failed');
	                    }
	                    renderGroups(payload);
	                } catch (error) {
	                    if (error && error.name === 'AbortError') return;
	                    if (requestSerial !== state.requestSerial) return;
	                    console.warn('Workspace search failed:', error);
	                    setStatus('Search unavailable');
	                    renderEmpty('Search is unavailable', 'Canopy could not complete this search. Try again in a moment.', 'bi-exclamation-triangle');
	                } finally {
	                    if (requestSerial === state.requestSerial) {
	                        state.controller = null;
	                    }
	                }
	            }

	            function scheduleSearch() {
	                clearSearchTimer();
	                state.timer = global.setTimeout(runSearch, SEARCH_DEBOUNCE_MS);
	            }

	            function setScope(scope) {
	                state.scope = scope || DEFAULT_SCOPE;
	                elements().filters.forEach(btn => {
	                    const active = (btn.getAttribute('data-workspace-search-scope') || DEFAULT_SCOPE) === state.scope;
	                    btn.classList.toggle('is-active', active);
	                    btn.setAttribute('aria-selected', active ? 'true' : 'false');
	                });
	                scheduleSearch();
	            }

	            function openSearch(seed = '') {
	                const els = elements();
	                if (!els.overlay || !els.input) return;
	                state.open = true;
	                state.returnFocusEl = document.activeElement && document.activeElement !== document.body
	                    ? document.activeElement
	                    : (els.trigger || null);
	                els.overlay.hidden = false;
	                document.body.classList.add('workspace-search-open');
	                if (seed) {
	                    els.input.value = seed;
	                }
	                global.setTimeout(() => {
	                    els.input.focus();
	                    els.input.select();
	                }, 0);
	                if (String(els.input.value || '').trim().length >= SEARCH_MIN_LENGTH) {
	                    scheduleSearch();
	                }
	            }

	            function closeSearch() {
	                const els = elements();
	                if (!els.overlay) return;
	                state.open = false;
	                clearSearchTimer();
	                abortSearch();
	                state.requestSerial += 1;
	                els.overlay.hidden = true;
	                document.body.classList.remove('workspace-search-open');
	                const returnTarget = state.returnFocusEl;
	                state.returnFocusEl = null;
	                if (returnTarget && document.contains(returnTarget) && typeof returnTarget.focus === 'function') {
	                    returnTarget.focus({ preventScroll: true });
	                }
	            }

	            function openActiveResult() {
	                const active = document.querySelector('[data-workspace-search-result].is-active');
	                if (!active) return false;
	                const href = active.getAttribute('href') || '';
	                if (!href || href === '#') return false;
	                global.location.href = href;
	                return true;
	            }

	            function bindWorkspaceSearch() {
	                const els = elements();
	                if (!els.overlay || els.overlay.dataset.wired === '1') return;
	                els.overlay.dataset.wired = '1';
	                if (els.trigger) {
	                    els.trigger.addEventListener('click', () => openSearch());
	                }
	                if (els.close) {
	                    els.close.addEventListener('click', closeSearch);
	                }
	                els.overlay.addEventListener('click', (event) => {
	                    if (event.target === els.overlay) closeSearch();
	                });
	                if (els.input) {
	                    els.input.addEventListener('input', scheduleSearch);
	                }
	                els.filters.forEach(btn => {
	                    btn.addEventListener('click', () => setScope(btn.getAttribute('data-workspace-search-scope') || DEFAULT_SCOPE));
	                });
	                document.addEventListener('keydown', (event) => {
	                    const key = String(event.key || '').toLowerCase();
	                    const chord = (event.metaKey || event.ctrlKey) && key === 'k';
	                    if (chord) {
	                        event.preventDefault();
	                        openSearch();
	                        return;
	                    }
	                    if (!state.open) return;
	                    if (event.key === 'Escape') {
	                        event.preventDefault();
	                        closeSearch();
	                    } else if (event.key === 'ArrowDown') {
	                        event.preventDefault();
	                        setActiveIndex(state.activeIndex + 1);
	                    } else if (event.key === 'ArrowUp') {
	                        event.preventDefault();
	                        setActiveIndex(state.activeIndex - 1);
	                    } else if (event.key === 'Enter') {
	                        if (openActiveResult()) event.preventDefault();
	                    }
	                });
	                document.addEventListener('mouseover', (event) => {
	                    const result = event.target && event.target.closest ? event.target.closest('[data-workspace-search-result]') : null;
	                    if (!result) return;
	                    const index = Number(result.getAttribute('data-workspace-search-index'));
	                    if (Number.isFinite(index)) setActiveIndex(index);
	                });
	            }

	            global.CanopyWorkspaceSearch = {
	                open: openSearch,
	                close: closeSearch,
	                search: runSearch,
	            };
	            document.addEventListener('DOMContentLoaded', bindWorkspaceSearch);
	        })(window);

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

        // --- Collaboration card helpers (input requests + telemetry) ---
        function _collabCardSelector(cardId) {
            const raw = String(cardId || '');
            if (window.CSS && typeof window.CSS.escape === 'function') {
                return `.collab-card[data-collab-card-id="${window.CSS.escape(raw)}"]`;
            }
            return `.collab-card[data-collab-card-id="${raw.replace(/"/g, '\\"')}"]`;
        }

        function _collabResponseValueText(response) {
            if (!response || response.value === null || response.value === undefined) return '';
            if (typeof response.value === 'string') return response.value;
            try {
                return JSON.stringify(response.value);
            } catch (err) {
                return String(response.value || '');
            }
        }

        function _escapeAttr(text) {
            return _escapeHtml(text).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
        }

        function _collabOptionButtonHtml(cardId, option) {
            const optionText = String(option ?? '');
            const encodedOption = encodeURIComponent(optionText).replace(/'/g, '%27');
            return `<button type="button" class="btn btn-sm btn-outline-info" onclick="submitCollabInputResponse('${_escapeHtml(cardId)}', decodeURIComponent('${encodedOption}'), 'choice')">${_escapeHtml(optionText)}</button>`;
        }

        function _renderCollabSavedResponse(response) {
            const valueText = _collabResponseValueText(response);
            const commentText = response && response.comment ? String(response.comment) : '';
            const updatedAt = response && (response.updated_at || response.created_at) ? String(response.updated_at || response.created_at) : '';
            return `
                <div class="collab-card-answer collab-card-answer--saved">
                    <div class="collab-card-answer-head">
                        <i class="bi bi-check2-circle"></i>
                        <span>Saved response</span>
                    </div>
                    ${valueText ? `<div class="collab-card-answer-value">${_escapeHtml(valueText)}</div>` : ''}
                    ${commentText ? `<div class="collab-card-answer-comment">${_escapeHtml(commentText)}</div>` : ''}
                    ${updatedAt ? `<div class="collab-card-answer-time">Stored ${_escapeHtml(updatedAt)}</div>` : ''}
                </div>`;
        }

        function _renderCollabInputControls(card) {
            const cardId = String(card && card.id || '');
            const config = (card && card.config && typeof card.config === 'object') ? card.config : {};
            const options = Array.isArray(config.options) ? config.options : [];
            const optionButtons = options.length ? `
                <div class="collab-card-options">
                    ${options.map(option => _collabOptionButtonHtml(cardId, option)).join('')}
                </div>` : '';
            return `
                <div class="collab-card-response" data-collab-response-form="${_escapeHtml(cardId)}">
                    ${optionButtons}
                    <div class="input-group input-group-sm mt-2">
                        <input type="text" class="form-control" placeholder="${card && card.my_response ? 'Replace or add rationale' : 'Add a response or rationale'}" data-collab-input="${_escapeHtml(cardId)}">
                        <button class="btn btn-primary" type="button" onclick="submitCollabInputResponse('${_escapeHtml(cardId)}', null, 'text')">${card && card.my_response ? 'Update' : 'Send'}</button>
                    </div>
                </div>`;
        }

        function _renderCollabInputState(card) {
            if (!card) return '';
            if (card.my_response) {
                return `
                    ${_renderCollabSavedResponse(card.my_response)}
                    ${card.can_respond ? `<div class="collab-card-response-note">Need to revise it?</div>${_renderCollabInputControls(card)}` : ''}`;
            }
            if (card.can_respond) {
                return _renderCollabInputControls(card);
            }
            return '<div class="collab-card-lock"><i class="bi bi-lock"></i> Allocated responders only.</div>';
        }

        function _collabTelemetryMetricText(value) {
            if (value === null || value === undefined) return '';
            if (typeof value === 'string') return value;
            try {
                return JSON.stringify(value);
            } catch (err) {
                return String(value || '');
            }
        }

        function _renderCollabTelemetryMetric(metric) {
            const safeMetric = (metric && typeof metric === 'object') ? metric : {};
            const label = String(safeMetric.label || safeMetric.key || 'Metric');
            const value = _collabTelemetryMetricText(safeMetric.value);
            const unit = safeMetric.unit ? ` ${String(safeMetric.unit)}` : '';
            const displayValue = `${value}${unit}`.trim() || 'reported';
            const title = `${label}: ${displayValue}`;
            return `
                <div class="collab-telemetry-metric" title="${_escapeAttr(title)}">
                    <span class="collab-telemetry-metric-label">${_escapeHtml(label)}</span>
                    <strong class="collab-telemetry-metric-value">${_escapeHtml(displayValue)}</strong>
                </div>`;
        }

        function _renderCollabTelemetryMetrics(metrics, cardId) {
            const metricList = Array.isArray(metrics) ? metrics.filter(metric => metric && typeof metric === 'object') : [];
            const visibleMetrics = metricList.slice(0, 4);
            const extraMetrics = metricList.slice(4);
            const visibleHtml = visibleMetrics.map(_renderCollabTelemetryMetric).join('');
            const extraHtml = extraMetrics.length ? `
                <details class="collab-telemetry-more">
                    <summary>${extraMetrics.length} more metric${extraMetrics.length === 1 ? '' : 's'}</summary>
                    <div class="collab-telemetry-metrics collab-telemetry-metrics--extra">
                        ${extraMetrics.map(_renderCollabTelemetryMetric).join('')}
                    </div>
                </details>` : '';
            return `<div class="collab-telemetry-metrics" data-collab-telemetry-metrics="${_escapeAttr(cardId || '')}">${visibleHtml}${extraHtml}</div>`;
        }

        function submitCollabInputResponse(cardId, value, responseType) {
            if (!cardId) return;
            let finalValue = value;
            if (finalValue === null || finalValue === undefined) {
                const input = document.querySelector(`[data-collab-input="${cardId}"]`);
                finalValue = input ? String(input.value || '').trim() : '';
            }
            if (!finalValue) {
                showAlert('Add a response first.', 'warning');
                return;
            }
            apiCall(`/ajax/collab_cards/${encodeURIComponent(cardId)}/respond`, {
                method: 'POST',
                body: JSON.stringify({
                    value: finalValue,
                    response_type: responseType || 'text',
                    advance_source: true,
                    advance_reason: 'input response updated'
                })
            })
                .then(data => {
                    if (!data || !data.success) {
                        showAlert((data && data.error) || 'Failed to save response', 'danger');
                        return;
                    }
                    if (data.card) {
                        applyCollabCardUpdate(data.card);
                    }
                    showAlert('Response saved.', 'success');
                })
                .catch(err => {
                    showAlert((err && (err.error || err.message)) || 'Failed to save response', 'danger');
                });
        }

        function updateCollabInputStatus(cardId, status) {
            if (!cardId || !status) return;
            apiCall(`/ajax/collab_cards/${encodeURIComponent(cardId)}/status`, {
                method: 'POST',
                body: JSON.stringify({
                    status,
                    advance_source: true,
                    advance_reason: `input card ${status}`
                })
            })
                .then(data => {
                    if (!data || !data.success) {
                        showAlert((data && data.error) || 'Failed to update card', 'danger');
                        return;
                    }
                    if (data.card) {
                        applyCollabCardUpdate(data.card);
                    }
                    showAlert('Card updated.', 'success');
                })
                .catch(err => {
                    showAlert((err && (err.error || err.message)) || 'Failed to update card', 'danger');
                });
        }

        function updateCollabTelemetry(cardId) {
            if (!cardId) return;
            const progressInput = document.querySelector(`[data-collab-progress="${cardId}"]`);
            const stageInput = document.querySelector(`[data-collab-stage="${cardId}"]`);
            const payload = {};
            if (progressInput && String(progressInput.value || '').trim()) {
                payload.progress = progressInput.value;
            }
            if (stageInput && String(stageInput.value || '').trim()) {
                payload.stage = stageInput.value.trim();
            }
            if (!Object.keys(payload).length) {
                showAlert('Enter a progress value or stage update first.', 'warning');
                return;
            }
            apiCall(`/ajax/collab_cards/${encodeURIComponent(cardId)}/telemetry`, {
                method: 'POST',
                body: JSON.stringify({
                    ...payload,
                    advance_source: true,
                    advance_reason: payload.stage || 'telemetry updated'
                })
            })
                .then(data => {
                    if (!data || !data.success) {
                        showAlert((data && data.error) || 'Failed to update telemetry', 'danger');
                        return;
                    }
                    if (data.card) {
                        applyCollabCardUpdate(data.card);
                    }
                    if (progressInput) progressInput.value = '';
                    if (stageInput) stageInput.value = '';
                    showAlert('Telemetry updated.', 'success');
                })
                .catch(err => {
                    showAlert((err && (err.error || err.message)) || 'Failed to update telemetry', 'danger');
                });
        }

        function applyCollabCardUpdate(card) {
            if (!card || !card.id) return;
            const cards = document.querySelectorAll(_collabCardSelector(card.id));
            if (!cards.length) return;
            const status = String(card.status || '');
            const telemetry = card.telemetry || {};
            const progress = Math.max(0, Math.min(100, Number(telemetry.progress || 0)));
            cards.forEach(el => {
                if (status) {
                    el.dataset.status = status;
                    const statusEl = el.querySelector('.collab-card-status');
                    if (statusEl) {
                        statusEl.textContent = card.status_label || status.replace(/_/g, ' ');
                        statusEl.className = `collab-card-status ${status}`;
                    }
                }
                const countEl = el.querySelector('.collab-card-meta > span');
                if (countEl && card.response_count !== undefined) {
                    const count = Number(card.response_count || 0);
                    countEl.textContent = `${count} response${count === 1 ? '' : 's'}`;
                }
                if (card.my_response) {
                    const inputState = el.querySelector('[data-collab-input-state]');
                    if (inputState) {
                        inputState.innerHTML = _renderCollabInputState(card);
                    } else {
                        const responseEl = el.querySelector('.collab-card-response');
                        if (responseEl) {
                            responseEl.outerHTML = _renderCollabSavedResponse(card.my_response);
                        }
                    }
                }
                const bar = el.querySelector('.collab-telemetry-bar span');
                if (bar) {
                    bar.style.width = `${progress}%`;
                }
                const progressLabel = el.querySelector('.collab-telemetry-meta strong');
                if (progressLabel) {
                    progressLabel.textContent = `${progress}%`;
                }
                const stageEl = el.querySelector('.collab-telemetry-stage');
                if (stageEl) {
                    const stage = telemetry.stage ? String(telemetry.stage) : 'No stage reported';
                    stageEl.textContent = stage;
                    stageEl.title = stage;
                }
                const metricsEl = el.querySelector('[data-collab-telemetry-metrics]');
                if (metricsEl) {
                    metricsEl.outerHTML = _renderCollabTelemetryMetrics(telemetry.metrics, card.id);
                }
            });
        }

        function advanceCollabCardSource(cardId) {
            if (!cardId) return;
            apiCall(`/ajax/collab_cards/${encodeURIComponent(cardId)}/advance_source`, {
                method: 'POST',
                body: JSON.stringify({ reason: 'manual' })
            })
                .then(data => {
                    if (!data || !data.success) {
                        showAlert((data && data.error) || 'Could not bring this card source forward', 'danger');
                        return;
                    }
                    if (data.card) {
                        applyCollabCardUpdate(data.card);
                    }
                    showAlert('Card source brought forward.', 'success');
                    if (typeof requestChannelThreadRefresh === 'function') {
                        requestChannelThreadRefresh({ forceScroll: false });
                    }
                    if (typeof refreshSidebarChannelState === 'function') {
                        refreshSidebarChannelState();
                    }
                    if (window.location && String(window.location.pathname || '').startsWith('/feed')) {
                        window.setTimeout(() => window.location.reload(), 180);
                    }
                })
                .catch(err => {
                    showAlert((err && (err.error || err.message)) || 'Could not bring card source forward', 'danger');
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

            function isVaultFileDragEvent(event) {
                const types = Array.from((event && event.dataTransfer && event.dataTransfer.types) || []);
                return types.includes(VAULT_FILE_DRAG_TYPE);
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
        const CANOPY_THEME_META_COLORS = Object.freeze({
            dark: '#0f0f1a',
            graphite: '#252526',
            outlook: '#0078d4',
            teams: '#6264a7',
            light: '#ffffff',
            eco: '#052e16',
            'liquid-glass': '#0f0f1a',
            custom: '#0f0f1a'
        });
        const CANOPY_THEME_NAMES = Object.freeze(['dark', 'graphite', 'outlook', 'teams', 'light', 'auto', 'liquid-glass', 'eco', 'custom']);
        const CANOPY_LIGHT_SCHEME_THEMES = Object.freeze(['light', 'outlook', 'teams']);
        const CANOPY_CUSTOM_THEME_STORAGE_KEY = 'canopy-custom-theme';
        const CANOPY_CUSTOM_THEME_VARS = Object.freeze({
            primary: '--canopy-primary',
            primaryHover: '--canopy-primary-hover',
            secondary: '--canopy-secondary',
            secondaryHover: '--canopy-secondary-hover',
            accent: '--canopy-accent',
            accentHover: '--canopy-accent-hover',
            success: '--canopy-success',
            warning: '--canopy-warning',
            danger: '--canopy-danger',
            bgPrimary: '--canopy-bg-primary',
            bgSecondary: '--canopy-bg-secondary',
            bgTertiary: '--canopy-bg-tertiary',
            cardBg: '--canopy-bg-card',
            cardHover: '--canopy-bg-card-hover',
            cardHeader: '--canopy-card-header-bg',
            cardBody: '--canopy-card-body-bg',
            textPrimary: '--canopy-text-primary',
            textSecondary: '--canopy-text-secondary',
            textMuted: '--canopy-text-muted',
            border: '--canopy-border',
            borderLight: '--canopy-border-light',
            mentionBg: '--canopy-mention-bg',
            mentionText: '--canopy-mention-text',
            mentionBorder: '--canopy-mention-border',
            channelBg: '--canopy-channel-tag-bg',
            channelText: '--canopy-channel-tag-text',
            channelBorder: '--canopy-channel-tag-border',
            navbarBg: '--canopy-navbar-bg',
            sidebarBg: '--canopy-sidebar-bg'
        });
        const CANOPY_CUSTOM_THEME_COLOR_RE = /^(#[0-9a-f]{3,8}|rgba?\([^)]+\)|hsla?\([^)]+\)|[a-z]+)$/i;
        let canopyThemeMediaQueryBound = false;

        function normalizeCanopyThemePreference(theme) {
            const preference = String(theme || 'dark').trim().toLowerCase() || 'dark';
            return CANOPY_THEME_NAMES.includes(preference) ? preference : 'dark';
        }

        function resolveCanopyThemePreference(theme) {
            const preference = normalizeCanopyThemePreference(theme);
            if (preference !== 'auto') return preference;
            try {
                return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
            } catch (_) {
                return 'dark';
            }
        }

        function canopyThemeColorScheme(resolvedTheme) {
            const theme = String(resolvedTheme || '').trim().toLowerCase();
            if (theme === 'custom') {
                const custom = readCanopyCustomTheme();
                return String(custom.scheme || '').toLowerCase() === 'light' ? 'light' : 'dark';
            }
            return CANOPY_LIGHT_SCHEME_THEMES.includes(theme) ? 'light' : 'dark';
        }

        function updateCanopyThemeMetaColor(resolvedTheme) {
            const meta = document.querySelector('meta[name="theme-color"]');
            if (meta) {
                const customColor = document.documentElement.style.getPropertyValue('--canopy-theme-color').trim();
                meta.setAttribute('content', resolvedTheme === 'custom' && customColor ? customColor : (CANOPY_THEME_META_COLORS[resolvedTheme] || CANOPY_THEME_META_COLORS.dark));
            }
        }

        function readCanopyCustomTheme() {
            try {
                const stored = localStorage.getItem(CANOPY_CUSTOM_THEME_STORAGE_KEY);
                if (stored) {
                    const parsed = JSON.parse(stored);
                    if (parsed && typeof parsed === 'object') return parsed;
                }
            } catch (_) {
                // Ignore broken local theme JSON.
            }
            const profileCustom = window.CANOPY_VARS && window.CANOPY_VARS.customTheme;
            return profileCustom && typeof profileCustom === 'object' ? profileCustom : {};
        }

        function saveCanopyCustomTheme(theme) {
            try {
                localStorage.setItem(CANOPY_CUSTOM_THEME_STORAGE_KEY, JSON.stringify(theme || {}));
            } catch (_) {
                // Custom theme persistence should not block previewing.
            }
        }

        function clearInlineCustomThemeVars() {
            Object.values(CANOPY_CUSTOM_THEME_VARS).forEach((cssVar) => {
                document.documentElement.style.removeProperty(cssVar);
            });
            [
                '--canopy-gradient-primary',
                '--canopy-gradient-primary-hover',
                '--canopy-gradient-secondary',
                '--canopy-gradient-bg',
                '--canopy-theme-color'
            ].forEach((cssVar) => document.documentElement.style.removeProperty(cssVar));
        }

        function applyCanopyCustomTheme(theme) {
            const customTheme = theme && typeof theme === 'object' ? theme : readCanopyCustomTheme();
            const colors = customTheme.colors && typeof customTheme.colors === 'object' ? customTheme.colors : customTheme;
            Object.keys(CANOPY_CUSTOM_THEME_VARS).forEach((key) => {
                const value = String(colors[key] || '').trim();
                if (value && CANOPY_CUSTOM_THEME_COLOR_RE.test(value)) {
                    document.documentElement.style.setProperty(CANOPY_CUSTOM_THEME_VARS[key], value);
                }
            });
            const primary = String(colors.primary || '').trim();
            const primaryHover = String(colors.primaryHover || primary || '').trim();
            const secondary = String(colors.secondary || primary || '').trim();
            const accent = String(colors.accent || primary || '').trim();
            const bgPrimary = String(colors.bgPrimary || '').trim();
            const bgSecondary = String(colors.bgSecondary || bgPrimary || '').trim();
            if (CANOPY_CUSTOM_THEME_COLOR_RE.test(primary) && CANOPY_CUSTOM_THEME_COLOR_RE.test(primaryHover || primary)) {
                document.documentElement.style.setProperty('--canopy-gradient-primary', `linear-gradient(135deg, ${primary} 0%, ${primaryHover || primary} 100%)`);
                document.documentElement.style.setProperty('--canopy-gradient-primary-hover', `linear-gradient(135deg, ${primaryHover || primary} 0%, ${primary} 100%)`);
            }
            if (CANOPY_CUSTOM_THEME_COLOR_RE.test(accent) && CANOPY_CUSTOM_THEME_COLOR_RE.test(secondary)) {
                document.documentElement.style.setProperty('--canopy-gradient-secondary', `linear-gradient(135deg, ${accent} 0%, ${secondary} 100%)`);
            }
            if (CANOPY_CUSTOM_THEME_COLOR_RE.test(bgPrimary) && CANOPY_CUSTOM_THEME_COLOR_RE.test(bgSecondary)) {
                document.documentElement.style.setProperty('--canopy-gradient-bg', `linear-gradient(180deg, ${bgSecondary} 0%, ${bgPrimary} 100%)`);
                document.documentElement.style.setProperty('--canopy-theme-color', bgPrimary);
            }
            const scheme = String(customTheme.scheme || '').toLowerCase() === 'light' ? 'light' : 'dark';
            document.documentElement.setAttribute('data-custom-theme-scheme', scheme);
            return scheme;
        }

        function readCanopyThemePreference() {
            try {
                return localStorage.getItem('canopy-theme') || '';
            } catch (_) {
                return '';
            }
        }

        function saveCanopyThemePreference(preference) {
            try {
                localStorage.setItem('canopy-theme', preference);
            } catch (_) {
                // Theme persistence should not block the UI when storage is unavailable.
            }
        }

        function bindAutoThemeListener() {
            if (canopyThemeMediaQueryBound || !window.matchMedia) return;
            const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
            const handleChange = () => {
                const savedTheme = readCanopyThemePreference();
                const profileTheme = (window.CANOPY_VARS && window.CANOPY_VARS.profileTheme) || 'dark';
                const activePreference = normalizeCanopyThemePreference(savedTheme || profileTheme || 'dark');
                if (activePreference === 'auto') {
                    applyTheme('auto', { persist: false });
                }
            };
            if (typeof mediaQuery.addEventListener === 'function') {
                mediaQuery.addEventListener('change', handleChange);
            } else if (typeof mediaQuery.addListener === 'function') {
                mediaQuery.addListener(handleChange);
            }
            canopyThemeMediaQueryBound = true;
        }

        function applyTheme(theme, options = {}) {
            const preference = normalizeCanopyThemePreference(theme);
            const resolvedTheme = resolveCanopyThemePreference(preference);
            if (resolvedTheme !== 'custom') {
                clearInlineCustomThemeVars();
            }
            let colorScheme = canopyThemeColorScheme(resolvedTheme);
            document.documentElement.setAttribute('data-theme', resolvedTheme);
            document.documentElement.setAttribute('data-theme-preference', preference);
            if (resolvedTheme === 'custom') {
                colorScheme = applyCanopyCustomTheme(options.customTheme);
            }
            document.documentElement.setAttribute('data-bs-theme', colorScheme);
            document.documentElement.style.colorScheme = colorScheme;
            updateCanopyThemeMetaColor(resolvedTheme);
            if (options.persist !== false) {
                saveCanopyThemePreference(preference);
            }
            if (preference === 'auto') {
                bindAutoThemeListener();
            }
            document.documentElement.offsetHeight;
            return resolvedTheme;
        }

        function loadSavedTheme() {
            // Try to get theme from profile first (server-side), then localStorage
            const profileTheme = (window.CANOPY_VARS && window.CANOPY_VARS.profileTheme) || 'dark';
            const savedTheme = readCanopyThemePreference() || profileTheme || 'dark';
            applyTheme(savedTheme, { persist: false });
        }

        // Load theme on page load
        document.addEventListener('DOMContentLoaded', function() {
            loadSavedTheme();
        });

        // Apply theme immediately (before DOM content loaded)
        (function() {
            const profileTheme = (window.CANOPY_VARS && window.CANOPY_VARS.profileTheme) || 'dark';
            const savedTheme = readCanopyThemePreference() || profileTheme || 'dark';
            const resolvedTheme = resolveCanopyThemePreference(savedTheme);
            let colorScheme = canopyThemeColorScheme(resolvedTheme);
            document.documentElement.setAttribute('data-theme', resolvedTheme);
            document.documentElement.setAttribute('data-theme-preference', normalizeCanopyThemePreference(savedTheme));
            if (resolvedTheme === 'custom') {
                colorScheme = applyCanopyCustomTheme();
            }
            document.documentElement.setAttribute('data-bs-theme', colorScheme);
            document.documentElement.style.colorScheme = colorScheme;
            updateCanopyThemeMetaColor(resolvedTheme);
        })();

        window.CanopyThemes = {
            applyTheme,
            readCustomTheme: readCanopyCustomTheme,
            saveCustomTheme: saveCanopyCustomTheme,
            customThemeVars: CANOPY_CUSTOM_THEME_VARS
        };

        // Format timestamps immediately and then refresh relative labels.
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => formatTimestamps());
        } else {
            formatTimestamps();
        }
        setInterval(() => formatTimestamps(), 30000);

        // --- Attention center + peer rail ---
        function initCanopyAttentionCenter() {
            const bellBtn = document.getElementById('notificationBell');
            const badgeEl = document.getElementById('notificationBadge');
            const listEl = document.getElementById('notificationList');
            const emptyWrap = document.getElementById('notificationEmptyWrap');
            const clearBtn = document.getElementById('notificationClear');
            const filterBar = document.getElementById('notificationFilterBar');
            const filterResetBtn = document.getElementById('notificationFilterReset');

            function cleanPreview(text) {
                const s = String(text || '').replace(/\s+/g, ' ').trim();
                return s
                    .replace(/\*\*(.*?)\*\*/g, '$1')
                    .replace(/__(.*?)__/g, '$1')
                    .replace(/`([^`]*)`/g, '$1')
                    .replace(/~~(.*?)~~/g, '$1');
            }

            function setBadge(count) {
                if (!badgeEl) return;
                const normalized = Math.max(0, Number(count) || 0);
                if (normalized > 0) {
                    badgeEl.style.display = 'inline-flex';
                    badgeEl.textContent = normalized > 99 ? '99+' : String(normalized);
                } else {
                    badgeEl.style.display = 'none';
                    badgeEl.textContent = '0';
                }
            }

            function renderFilterBar() {
                if (!filterBar) return;
                const filters = normalizeCanopyAttentionFilters(canopySidebarAttentionState.filters);
                if (filterResetBtn) {
                    const usingDefaults = isDefaultCanopyAttentionFilters(filters);
                    filterResetBtn.title = usingDefaults
                        ? 'New accounts show inbox items only. Click a filter chip to customise.'
                        : 'Reset to inbox-only default';
                    filterResetBtn.classList.toggle('is-custom', !usingDefaults);
                }
                filterBar.innerHTML = '';
                CANOPY_ATTENTION_FILTER_DEFS.forEach((def) => {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = `notification-filter-chip${filters[def.key] !== false ? ' is-active' : ''}`;
                    btn.setAttribute('data-filter-key', def.key);
                    btn.setAttribute('aria-pressed', filters[def.key] !== false ? 'true' : 'false');
                    btn.innerHTML = `<i class="bi ${def.icon}"></i><span>${def.label}</span>`;
                    btn.addEventListener('click', (event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        const next = normalizeCanopyAttentionFilters(canopySidebarAttentionState.filters);
                        next[def.key] = !(next[def.key] !== false);
                        saveCanopyAttentionFilters(next);
                        renderFilterBar();
                        if (window.renderCanopyAttentionBell) {
                            window.renderCanopyAttentionBell(filterCanopyAttentionItems(canopySidebarAttentionState.items));
                        }
                    });
                    filterBar.appendChild(btn);
                });
            }

            window.renderCanopyAttentionBell = function(items) {
                const normalized = Array.isArray(items) ? items.filter(Boolean).slice(0, 12) : [];
                if (!listEl) {
                    setBadge(countUnseenCanopyAttentionItems(normalized));
                    syncCanopyAttentionDocumentTitle();
                    return;
                }
                listEl.innerHTML = '';
                setBadge(countUnseenCanopyAttentionItems(normalized));
                syncCanopyAttentionDocumentTitle();
                if (!normalized.length) {
                    if (emptyWrap) emptyWrap.style.display = 'block';
                    return;
                }
                if (emptyWrap) emptyWrap.style.display = 'none';

                groupCanopyAttentionItems(normalized).forEach((group) => {
                    const section = document.createElement('div');
                    section.className = 'notification-section';
                    section.setAttribute('data-section-kind', String(group.key || 'other'));

                    const label = document.createElement('div');
                    label.className = 'notification-section-label';
                    const left = document.createElement('span');
                    left.innerHTML = `<i class="bi ${String(group.icon || 'bi-bell')}"></i><span>${String(group.label || 'Other')}</span>`;
                    left.style.display = 'inline-flex';
                    left.style.alignItems = 'center';
                    left.style.gap = '8px';
                    const count = document.createElement('span');
                    count.className = 'notification-section-count';
                    count.textContent = `${group.items.length} ${group.items.length === 1 ? 'item' : 'items'}`;
                    label.appendChild(left);
                    label.appendChild(count);
                    section.appendChild(label);

                    group.items.forEach((item) => {
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'dropdown-item notification-item';

                        const row = document.createElement('div');
                        row.className = 'activity-row';

                        const iconWrap = document.createElement('div');
                        iconWrap.className = 'activity-avatar';
                        const avatarUrl = _safeImageSrc(item.avatar_url || '');
                        if (avatarUrl) {
                            const img = document.createElement('img');
                            img.src = avatarUrl;
                            img.alt = String(item.title || 'Activity');
                            iconWrap.appendChild(img);
                        } else {
                            const fallbackLabel = String(item.title || '').trim();
                            const fallbackInitial = fallbackLabel ? fallbackLabel.slice(0, 1).toUpperCase() : '';
                            if (fallbackInitial && /^[A-Z0-9]$/.test(fallbackInitial)) {
                                iconWrap.textContent = fallbackInitial;
                            } else {
                                const icon = document.createElement('i');
                                icon.className = String(item.icon || (CANOPY_ATTENTION_FILTER_MAP[group.key] || {}).icon || 'bi-bell');
                                iconWrap.appendChild(icon);
                            }
                        }

                        const body = document.createElement('div');
                        body.className = 'activity-body';

                        const top = document.createElement('div');
                        top.className = 'activity-top';

                        const titleEl = document.createElement('span');
                        titleEl.className = 'activity-name';
                        titleEl.textContent = String(item.title || 'Activity');

                        const timeEl = document.createElement('span');
                        timeEl.className = 'activity-time';
                        if (item.created_at) {
                            timeEl.textContent = formatTimestamp(item.created_at);
                            timeEl.setAttribute('data-timestamp', item.created_at);
                        }

                        top.appendChild(titleEl);
                        top.appendChild(timeEl);

                        const sub = document.createElement('div');
                        sub.className = 'activity-sub';
                        const meta = String(item.meta || '').trim();
                        const preview = cleanPreview(item.preview || '');
                        if (meta && preview) {
                            sub.textContent = `${meta} • ${preview}`;
                        } else {
                            sub.textContent = meta || preview || 'Open';
                        }

                        body.appendChild(top);
                        body.appendChild(sub);

                        row.appendChild(iconWrap);
                        row.appendChild(body);
                        btn.appendChild(row);
                        btn.addEventListener('click', () => {
                            const href = String(item.href || '').trim();
                            if (href) window.location.href = href;
                        });
                        section.appendChild(btn);
                    });
                    listEl.appendChild(section);
                });
            };

            if (clearBtn) {
                clearBtn.addEventListener('click', async () => {
                    const routes = (window.CANOPY_VARS && window.CANOPY_VARS.urls) || {};
                    const endpoint = routes.sidebarAttentionClear || '/ajax/sidebar_attention_clear';
                    const csrfToken = document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';
                    try {
                        const res = await fetch(endpoint, {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'X-Requested-With': 'XMLHttpRequest',
                                'X-CSRFToken': csrfToken,
                            },
                            body: JSON.stringify({}),
                        });
                        if (!res.ok) throw new Error(`Sidebar attention clear failed (${res.status})`);
                        const data = await res.json();
                        if (!data || data.success === false) throw new Error('Sidebar attention clear failed');
                        canopySidebarAttentionState.items = [];
                        saveCanopyAttentionDismissCursor(canopySidebarAttentionState.currentEventCursor);
                        saveCanopyAttentionSeenCursor(canopySidebarAttentionState.currentEventCursor);
                        if (window.renderCanopyAttentionBell) {
                            window.renderCanopyAttentionBell([]);
                        }
                        requestCanopySidebarAttentionRefresh({ force: true }).catch(() => {});
                        if (window.requestCanopyMeshspaceSnapshotRefresh) {
                            window.requestCanopyMeshspaceSnapshotRefresh({ force: true });
                        }
                    } catch (error) {
                        console.warn('Canopy sidebar attention clear failed:', error);
                    }
                });
            }

            if (filterResetBtn) {
                filterResetBtn.addEventListener('click', (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    saveCanopyAttentionFilters(null);
                    renderFilterBar();
                    if (window.renderCanopyAttentionBell) {
                        window.renderCanopyAttentionBell(filterCanopyAttentionItems(canopySidebarAttentionState.items));
                    }
                });
            }

            if (bellBtn) {
                bellBtn.addEventListener('click', () => {
                    saveCanopyAttentionSeenCursor(canopySidebarAttentionState.currentEventCursor);
                    if (window.renderCanopyAttentionBell) {
                        window.renderCanopyAttentionBell(filterCanopyAttentionItems(canopySidebarAttentionState.items));
                    }
                });
            }

            renderFilterBar();
            if (window.renderCanopyAttentionBell) {
                window.renderCanopyAttentionBell(filterCanopyAttentionItems(canopySidebarAttentionState.items));
            }
        }

        function startCanopySidebarPeerPolling() {
            const endpoint = ((window.CANOPY_VARS && window.CANOPY_VARS.urls) || {}).peerActivity || '/ajax/peer_activity';
            const baseDelayMs = 2500;
            const maxDelayMs = 15000;

            function scheduleNext(delayMs) {
                if (canopySidebarPeerPollState.timerId) {
                    window.clearTimeout(canopySidebarPeerPollState.timerId);
                }
                canopySidebarPeerPollState.timerId = window.setTimeout(poll, delayMs);
            }

            function handleFailure(detail) {
                canopySidebarPeerPollState.failureCount += 1;
                canopySidebarPeerPollState.nextDelayMs = Math.min(
                    maxDelayMs,
                    baseDelayMs * Math.max(1, Math.pow(2, canopySidebarPeerPollState.failureCount - 1))
                );
                const retrySeconds = Math.max(1, Math.round(canopySidebarPeerPollState.nextDelayMs / 1000));
                console.warn('Canopy sidebar peer poll failed:', detail);
                setSidebarPeerPollingStatus(`Peer refresh degraded, retrying in ${retrySeconds}s`, true);
                scheduleNext(canopySidebarPeerPollState.nextDelayMs);
            }

            function poll() {
                const params = new URLSearchParams();
                if (canopySidebarPeerState.currentRev) {
                    params.set('peer_rev', canopySidebarPeerState.currentRev);
                }
                fetch(`${endpoint}${params.toString() ? `?${params.toString()}` : ''}`)
                    .then(async (r) => {
                        if (!r.ok) {
                            throw new Error(`HTTP ${r.status}`);
                        }
                        return r.json();
                    })
                    .then(data => {
                        if (!data || data.success === false) {
                            throw new Error((data && data.error) || 'peer_activity unsuccessful');
                        }
                        canopySidebarPeerPollState.failureCount = 0;
                        canopySidebarPeerPollState.nextDelayMs = baseDelayMs;
                        setSidebarPeerPollingStatus('Showing up to five peers');
                        if (window.syncCanopySidebarPeers) {
                            window.syncCanopySidebarPeers(data);
                        }
                        scheduleNext(baseDelayMs);
                    })
                    .catch((err) => handleFailure(err));
            }

            poll();
        }

        // --- Sidebar media mini player (audio/video/youtube off-screen helper) ---
        function initSidebarMediaMiniPlayer() {
            const mini = document.getElementById('sidebar-media-mini');
            if (!mini) {
                /* Deck helpers are normally defined below; without the sidebar host, only deep-link fallbacks work. */
                if (typeof window !== 'undefined') {
                    window.openDeckForFeedAntecedentPost = function (sourcePostId) {
                        const pid = String(sourcePostId || '').trim();
                        if (!pid) return false;
                        try {
                            window.location.href = `/feed?focus_post=${encodeURIComponent(pid)}&open_deck=1`;
                        } catch (_) {
                            /* ignore */
                        }
                        return false;
                    };
                    window.openDeckForChannelAntecedentMessage = function (sourceMessageId) {
                        const mid = String(sourceMessageId || '').trim();
                        if (!mid) return false;
                        try {
                            window.location.href = `/channels/locate?message_id=${encodeURIComponent(mid)}&open_deck=1`;
                        } catch (_) {
                            /* ignore */
                        }
                        return false;
                    };
                }
                return;
            }

            const icon = document.getElementById('sidebar-media-mini-icon');
            const titleEl = document.getElementById('sidebar-media-mini-title');
            const subtitleEl = document.getElementById('sidebar-media-mini-subtitle');
            const progressWrap = document.getElementById('sidebar-media-mini-progress');
            const progressBar = document.getElementById('sidebar-media-mini-progress-bar');
            const playBtn = document.getElementById('sidebar-media-mini-play');
            const jumpBtn = document.getElementById('sidebar-media-mini-jump');
            const pipBtn = document.getElementById('sidebar-media-mini-pip');
            const pinBtn = document.getElementById('sidebar-media-mini-pin');
            const expandBtn = document.getElementById('sidebar-media-mini-expand');
            const closeBtn = document.getElementById('sidebar-media-mini-close');
            const timeEl = document.getElementById('sidebar-media-mini-time');
            const mainScroller = document.querySelector('.main-content');
            const miniVideoHost = document.getElementById('sidebar-media-mini-video');
            const topSlot = document.getElementById('sidebar-media-mini-slot-top');
            const bottomSlot = document.getElementById('sidebar-media-mini-slot-bottom');
            const deck = document.getElementById('sidebar-media-deck');
            const deckShell = deck ? deck.querySelector('.sidebar-media-deck-shell') : null;
            const deckBackdrop = document.getElementById('sidebar-media-deck-backdrop');
            const deckStage = document.getElementById('sidebar-media-deck-stage');
            const deckStageShell = deckStage ? deckStage.closest('.sidebar-media-deck-stage-shell') : null;
            const deckVisual = document.getElementById('sidebar-media-deck-visual');
            const deckVisualCover = document.getElementById('sidebar-media-deck-visual-cover');
            const deckVisualIcon = document.getElementById('sidebar-media-deck-visual-icon');
            const deckVisualTitle = document.getElementById('sidebar-media-deck-visual-title');
            const deckVisualSubtitle = document.getElementById('sidebar-media-deck-visual-subtitle');
            const deckChipLabel = document.getElementById('sidebar-media-deck-chip-label');
            const deckCountChip = document.getElementById('sidebar-media-deck-count-chip');
            const deckSource = document.getElementById('sidebar-media-deck-source');
            const deckTitle = document.getElementById('sidebar-media-deck-title');
            const deckSubtitle = document.getElementById('sidebar-media-deck-subtitle');
            const deckProvider = document.getElementById('sidebar-media-deck-provider');
            const deckProviderLabel = document.getElementById('sidebar-media-deck-provider-label');
            const deckCount = document.getElementById('sidebar-media-deck-count');
            const deckStationSummary = document.getElementById('sidebar-media-deck-station-summary');
            const deckStationPolicy = document.getElementById('sidebar-media-deck-station-policy');
            const deckStationTitle = document.getElementById('sidebar-media-deck-station-title');
            const deckStationSubtitle = document.getElementById('sidebar-media-deck-station-subtitle');
            const deckStationBadges = document.getElementById('sidebar-media-deck-station-badges');
            const deckWidgetSummary = document.getElementById('sidebar-media-deck-widget-summary');
            const deckWidgetBadges = document.getElementById('sidebar-media-deck-widget-badges');
            const deckWidgetDetails = document.getElementById('sidebar-media-deck-widget-details');
            const deckWidgetActions = document.getElementById('sidebar-media-deck-widget-actions');
            const deckDetail = document.querySelector('.sidebar-media-deck-detail');
            const deckProgressRow = document.getElementById('sidebar-media-deck-progress-row');
            const deckControls = document.getElementById('sidebar-media-deck-controls');
            const deckSeek = document.getElementById('sidebar-media-deck-seek');
            const deckCurrentTime = document.getElementById('sidebar-media-deck-current-time');
            const deckDuration = document.getElementById('sidebar-media-deck-duration');
            const deckPrevBtn = document.getElementById('sidebar-media-deck-prev');
            const deckPlayBtn = document.getElementById('sidebar-media-deck-play');
            const deckNextBtn = document.getElementById('sidebar-media-deck-next');
            const deckPipBtn = document.getElementById('sidebar-media-deck-pip');
            const deckReturnBtn = document.getElementById('sidebar-media-deck-return');
            const deckMinimizeBtn = document.getElementById('sidebar-media-deck-minimize');
            const deckMiniPlayerBtn = document.getElementById('sidebar-media-deck-mini-player');
            const deckMinimizeFooterBtn = document.getElementById('sidebar-media-deck-minimize-footer');
            const deckMiniFooterBtn = document.getElementById('sidebar-media-deck-mini-player-footer');
            const deckCloseBtn = document.getElementById('sidebar-media-deck-close');
            const deckLargeToggleBtn = document.getElementById('sidebar-media-deck-large-toggle');
            const deckModeTabs = document.getElementById('sidebar-media-deck-mode-tabs');
            const deckSourceTab = document.getElementById('sidebar-media-deck-source-tab');
            const deckInboxTab = document.getElementById('sidebar-media-deck-inbox-tab');
            const deckOpenMessagesBtn = document.getElementById('sidebar-media-deck-open-messages');
            const deckInboxSurface = document.getElementById('deck-inbox-surface');
            const deckInboxSidebar = document.getElementById('deck-inbox-sidebar-sections');
            const deckInboxThreadHeader = document.getElementById('deck-inbox-thread-header');
            const deckInboxThreadBody = document.getElementById('deck-inbox-thread-body');
            const deckInboxThreadScroll = document.getElementById('deck-inbox-thread-scroll');
            const deckInboxComposerSlot = document.getElementById('deck-inbox-composer-slot');
            const deckQueue = document.getElementById('sidebar-media-deck-queue');
            const deckQueueCount = document.getElementById('sidebar-media-deck-queue-count');
            const deckQueueShell = deckQueue ? deckQueue.closest('.sidebar-media-deck-queue-shell') : null;
            const deckQueueToggle = document.getElementById('sidebar-media-deck-queue-toggle');
            const deckDetailToggle = document.getElementById('sidebar-media-deck-detail-toggle');
            const youtubeDeckTitleCache = new Map();

            function normalizeDeckDesktopMode(mode) {
                const raw = String(mode || '').trim().toLowerCase();
                if (raw === 'large' || raw === 'window') return raw;
                return 'default';
            }

            const state = {
                current: null,
                dismissedEl: null,
                observer: null,
                mutationObserver: null,
                tickHandle: null,
                ytApiPromise: null,
                returnUrl: null,
                dockedSubtitle: null,
                deckOpen: false,
                deckItems: [],
                deckSelectedKey: '',
                deckSourceEl: null,
                /** Message/post root captured when opening deck or mini; queue rebuild must not depend on docked media's DOM parent. */
                deckOriginSourceEl: null,
                deckOriginMessageId: '',
                deckOriginPostId: '',
                deckQueueSignature: '',
                deckQueueNeedsRefresh: false,
                deckSeeking: false,
                mediaCounter: 0,
                miniUpdateFrame: 0,
                miniUpdateTimer: null,
                persistMediaRetryHandle: null,
                moduleBundleCache: new Map(),
                moduleSessions: new Map(),
                moduleSessionCounter: 0,
                moduleCapabilityApprovals: new Set(),
                deckQueueCollapsed: false,
                deckDetailCollapsed: false,
                deckLayoutMode: 'default',
                deckLayoutPrimedKey: '',
                deckShellMode: 'source',
                deckInboxActive: false,
                deckInboxSnapshotInFlight: false,
                deckInboxActiveThread: null,
                deckInboxComposerRecipients: [],
                deckInboxConversationWith: '',
                deckInboxConversationGroup: '',
                deckInboxTargetMessageId: '',
                deckInboxSidebarToken: '',
                deckInboxThreadToken: '',
                deckInboxReply: null,
                deckInboxListOpen: false,
                deckInboxSnapshotSeq: 0,
                deckInboxCanopyLLMInFlight: false,
                deckInboxCanopyLLMDraftReady: false,
                deckInboxCanopyLLMIgnoredDraft: '',
                deckInboxReactionInFlight: new Set(),
                deckDesktopMode: normalizeDeckDesktopMode(loadSidebarRailPreference('deckDesktopMode', 'default')),
                /** Last `state.deckItems.length` applied in `syncDeckLayoutMode` (module layout). */
                deckLayoutLastQueueCount: -1,
            };

            function updateMiniPlacementControl() {
                if (!pinBtn) return;
                const atBottom = canopySidebarRailState.miniPosition === 'bottom';
                pinBtn.innerHTML = atBottom
                    ? '<i class="bi bi-arrow-up-square"></i>'
                    : '<i class="bi bi-arrow-down-square"></i>';
                pinBtn.title = atBottom ? 'Move mini player to top' : 'Move mini player lower';
                pinBtn.setAttribute('aria-label', atBottom ? 'Move mini player to top' : 'Move mini player lower');
            }

            function setCanopySidebarMiniPosition(nextPosition) {
                const normalized = String(nextPosition || '').trim().toLowerCase() === 'bottom' ? 'bottom' : 'top';
                const targetSlot = normalized === 'bottom' ? bottomSlot : topSlot;
                if (mini && targetSlot && mini.parentElement !== targetSlot) {
                    targetSlot.appendChild(mini);
                }
                canopySidebarRailState.miniPosition = normalized;
                saveSidebarRailPreference('miniPosition', normalized);
                updateMiniPlacementControl();
            }

            setCanopySidebarMiniPosition(canopySidebarRailState.miniPosition);
            applyDeckDesktopMode(state.deckDesktopMode, { silent: true });

            function scheduleMiniUpdate(delay = 0) {
                const wait = Number(delay || 0);
                if (wait > 0) {
                    if (state.miniUpdateTimer) {
                        clearTimeout(state.miniUpdateTimer);
                    }
                    state.miniUpdateTimer = setTimeout(() => {
                        state.miniUpdateTimer = null;
                        scheduleMiniUpdate(0);
                    }, wait);
                    return;
                }
                if (state.miniUpdateFrame) return;
                state.miniUpdateFrame = window.requestAnimationFrame(() => {
                    state.miniUpdateFrame = 0;
                    updateMini();
                });
            }

            function mediaTypeFor(el) {
                if (!el || !el.tagName) return '';
                const tag = el.tagName.toLowerCase();
                if (tag === 'audio') return 'audio';
                if (tag === 'video') return 'video';
                if ((tag === 'iframe' || tag === 'div') && (el.closest('.youtube-embed') || el.matches('.youtube-embed, .yt-facade'))) return 'youtube';
                return '';
            }

            function resolveYouTubeMediaElement(el, options = {}) {
                if (!el) return null;
                if (el.tagName && el.tagName.toLowerCase() === 'iframe' && el.closest('.youtube-embed')) {
                    return el;
                }
                const wrapper = el.matches && el.matches('.youtube-embed')
                    ? el
                    : (el.closest ? el.closest('.youtube-embed') : null);
                if (!wrapper) return null;
                const existingIframe = wrapper.querySelector('iframe');
                if (existingIframe) return existingIframe;
                if (options.activate !== true) return wrapper;
                const facade = wrapper.querySelector('.yt-facade');
                return materializeYouTubeFacade(facade, { autoplay: options.autoplay === true }) || wrapper;
            }

            /** True when this embed is still the static facade (no iframe) — avoids loading YouTube until Play. */
            function isYouTubeFacadeOnly(el) {
                if (!el) return false;
                const w = resolveYouTubeMediaElement(el, { activate: false });
                if (!w || typeof w.querySelector !== 'function') return false;
                return !!(w.querySelector('.yt-facade')) && !w.querySelector('iframe');
            }

            function mediaIcon(type) {
                if (type === 'audio') return 'bi-music-note-beamed';
                if (type === 'video') return 'bi-camera-video';
                if (type === 'youtube') return 'bi-youtube';
                if (type === 'map') return 'bi-geo-alt';
                if (type === 'chart') return 'bi-graph-up-arrow';
                if (type === 'media_stream') return 'bi-broadcast';
                if (type === 'telemetry_panel') return 'bi-cpu';
                if (type === 'module_surface') return 'bi-box-fill';
                if (type === 'media_embed') return 'bi-grid-1x2';
                if (type === 'story') return 'bi-newspaper';
                return 'bi-play-circle';
            }

            function mediaProviderLabel(type) {
                if (type === 'audio') return 'Audio';
                if (type === 'video') return 'Video';
                if (type === 'youtube') return 'YouTube';
                if (type === 'map') return 'Map';
                if (type === 'chart') return 'Chart';
                if (type === 'media_stream') return 'Live stream';
                if (type === 'telemetry_panel') return 'Telemetry';
                if (type === 'module_surface') return 'Canopy Module';
                if (type === 'media_embed') return 'Embedded media';
                if (type === 'story') return 'Story';
                return 'Media';
            }

            function isDesktopDeckLargeEligible() {
                return window.innerWidth > 900 && window.innerHeight > 620;
            }

            function isDesktopDeckWindowEligible() {
                return window.innerWidth > 1020 && window.innerHeight > 660;
            }

            function activeDeckDesktopMode(mode) {
                const requested = normalizeDeckDesktopMode(mode);
                if (!isDesktopDeckLargeEligible()) return 'default';
                if (requested === 'window' && !isDesktopDeckWindowEligible()) return 'large';
                return requested;
            }

            function syncDeckWindowBounds(activeMode) {
                if (!deck) return;
                if (activeMode !== 'window') return;
                const sourceRect = mainScroller && typeof mainScroller.getBoundingClientRect === 'function'
                    ? mainScroller.getBoundingClientRect()
                    : {
                        top: 56,
                        right: window.innerWidth,
                        bottom: window.innerHeight,
                        left: 0,
                    };
                const gutter = window.innerWidth > 1320 ? 18 : 14;
                const left = Math.max(10, Math.round(sourceRect.left + gutter));
                const top = Math.max(10, Math.round(sourceRect.top + gutter));
                const right = Math.max(10, Math.round(window.innerWidth - sourceRect.right + gutter));
                const bottom = Math.max(10, Math.round(window.innerHeight - sourceRect.bottom + gutter));
                deck.style.setProperty('--canopy-deck-window-left', `${left}px`);
                deck.style.setProperty('--canopy-deck-window-top', `${top}px`);
                deck.style.setProperty('--canopy-deck-window-right', `${right}px`);
                deck.style.setProperty('--canopy-deck-window-bottom', `${bottom}px`);
            }

            function syncDeckDesktopModeButton() {
                if (!deckLargeToggleBtn) return;
                const eligible = isDesktopDeckLargeEligible();
                const activeMode = activeDeckDesktopMode(state.deckDesktopMode);
                deckLargeToggleBtn.hidden = !eligible;
                deckLargeToggleBtn.disabled = !eligible;
                deckLargeToggleBtn.classList.toggle('is-active', activeMode !== 'default');
                if (activeMode === 'window') {
                    deckLargeToggleBtn.innerHTML = '<i class="bi bi-fullscreen-exit"></i><span class="sidebar-media-deck-action-label">Standard view</span>';
                    deckLargeToggleBtn.title = 'Return Canopy deck to standard size';
                } else if (activeMode === 'large' && isDesktopDeckWindowEligible()) {
                    deckLargeToggleBtn.innerHTML = '<i class="bi bi-aspect-ratio"></i><span class="sidebar-media-deck-action-label">Focus view</span>';
                    deckLargeToggleBtn.title = 'Maximize the Canopy deck content area';
                } else if (activeMode === 'large') {
                    deckLargeToggleBtn.innerHTML = '<i class="bi bi-fullscreen-exit"></i><span class="sidebar-media-deck-action-label">Standard view</span>';
                    deckLargeToggleBtn.title = 'Return Canopy deck to standard size';
                } else {
                    deckLargeToggleBtn.innerHTML = '<i class="bi bi-arrows-fullscreen"></i><span class="sidebar-media-deck-action-label">Large view</span>';
                    deckLargeToggleBtn.title = 'Use larger Canopy deck view';
                }
                deckLargeToggleBtn.setAttribute('aria-label', deckLargeToggleBtn.title);
                deckLargeToggleBtn.setAttribute('aria-pressed', activeMode !== 'default' ? 'true' : 'false');
            }

            function applyDeckDesktopMode(mode, options = {}) {
                const requested = normalizeDeckDesktopMode(mode);
                state.deckDesktopMode = requested;
                saveSidebarRailPreference('deckDesktopMode', requested);
                const activeMode = activeDeckDesktopMode(requested);
                syncDeckWindowBounds(activeMode);
                if (deck) {
                    deck.classList.toggle('is-desktop-large', activeMode === 'large');
                    deck.classList.toggle('is-desktop-window', activeMode === 'window');
                    deck.setAttribute('data-deck-desktop-mode', activeMode);
                }
                syncDeckDesktopModeButton();
                if (!options.silent) {
                    scheduleMiniUpdate(0);
                }
            }

            function cycleDeckDesktopMode() {
                const activeMode = activeDeckDesktopMode(state.deckDesktopMode);
                if (activeMode === 'window') {
                    applyDeckDesktopMode('default');
                } else if (activeMode === 'large') {
                    applyDeckDesktopMode(isDesktopDeckWindowEligible() ? 'window' : 'default');
                } else {
                    applyDeckDesktopMode('large');
                }
            }

            function normalizeYouTubeReadableTitle(value, videoId) {
                const text = String(value || '').replace(/\s+/g, ' ').trim();
                if (!text) return '';
                const lower = text.toLowerCase();
                const vid = String(videoId || '').trim().toLowerCase();
                if (lower === 'youtube video' || lower === 'youtube') return '';
                if (vid && lower === vid) return '';
                if (vid && lower === `youtube ${vid}`) return '';
                if (/^youtube[\s:_-]+[a-z0-9_-]{8,}$/i.test(text)) return '';
                if (/^(https?:\/\/|www\.)/i.test(text)) return '';
                return text;
            }

            function getYouTubeReadableTitleFromDom(el) {
                if (!el) return '';
                const wrapper = el.matches && el.matches('.youtube-embed')
                    ? el
                    : (el.closest ? el.closest('.youtube-embed') : null);
                const videoId = getYouTubeVideoId(el);
                const candidates = [
                    el.getAttribute && el.getAttribute('data-canopy-media-title'),
                    wrapper && wrapper.getAttribute && wrapper.getAttribute('data-canopy-media-title'),
                    el.getAttribute && el.getAttribute('data-youtube-title'),
                    wrapper && wrapper.getAttribute && wrapper.getAttribute('data-youtube-title'),
                    el.getAttribute && el.getAttribute('title'),
                    wrapper && wrapper.getAttribute && wrapper.getAttribute('title'),
                ];
                if (wrapper && wrapper.closest) {
                    const manifestHost = wrapper.closest('[data-canopy-widget-manifest]');
                    if (manifestHost) {
                        try {
                            const manifest = parseDeckWidgetManifest(manifestHost);
                            if (manifest && manifest.title) candidates.push(manifest.title);
                        } catch (_) {}
                    }
                    const sourceEl = sourceContainer(wrapper);
                    if (sourceEl && sourceEl.querySelectorAll) {
                        const linkCandidates = sourceEl.querySelectorAll('a[href*="youtu"], a[href*="youtube.com"]');
                        linkCandidates.forEach((link) => {
                            const href = String(link.getAttribute('href') || '').trim();
                            const text = String(link.textContent || '').trim();
                            if (!text || !href) return;
                            if (text === href) return;
                            candidates.push(text);
                        });
                    }
                }
                const cached = videoId ? youtubeDeckTitleCache.get(videoId) : null;
                if (cached && cached.title) candidates.unshift(cached.title);
                for (const candidate of candidates) {
                    const normalized = normalizeYouTubeReadableTitle(candidate, videoId);
                    if (normalized) return normalized;
                }
                return '';
            }

            function applyResolvedYouTubeTitle(videoId, title) {
                const vid = String(videoId || '').trim();
                const normalized = normalizeYouTubeReadableTitle(title, vid);
                if (!vid || !normalized) return;
                document.querySelectorAll(`.youtube-embed[data-video-id="${vid}"], .youtube-embed iframe[data-video-id="${vid}"]`).forEach((node) => {
                    if (!(node instanceof Element)) return;
                    node.setAttribute('data-canopy-media-title', normalized);
                    const wrapper = node.matches('.youtube-embed') ? node : (node.closest ? node.closest('.youtube-embed') : null);
                    if (wrapper) wrapper.setAttribute('data-canopy-media-title', normalized);
                });
                if (Array.isArray(state.deckItems)) {
                    let changed = false;
                    state.deckItems.forEach((item) => {
                        if (!item || item.type !== 'youtube') return;
                        if (getYouTubeVideoId(item.el) !== vid) return;
                        if (item.title !== normalized) {
                            item.title = normalized;
                            changed = true;
                        }
                    });
                    if (changed) {
                        state.deckQueueSignature = '';
                        renderDeckQueue();
                    }
                }
                if (state.current && state.current.type === 'youtube' && getYouTubeVideoId(state.current.el) === vid) {
                    state.current.title = normalized;
                    updateDeckPanel();
                }
            }

            function requestYouTubeReadableTitle(el) {
                const videoId = getYouTubeVideoId(el);
                if (!videoId) return;
                const cached = youtubeDeckTitleCache.get(videoId);
                const now = Date.now();
                if (cached && cached.title) {
                    applyResolvedYouTubeTitle(videoId, cached.title);
                    return;
                }
                if (cached && cached.pending) return;
                if (cached && cached.retryAt && cached.retryAt > now) return;
                youtubeDeckTitleCache.set(videoId, { pending: true, title: '', error: '' });
                fetch(`/api/v1/deck/youtube-title?video_id=${encodeURIComponent(videoId)}`, {
                    method: 'GET',
                    credentials: 'same-origin',
                    headers: { Accept: 'application/json' },
                })
                    .then((resp) => resp.ok ? resp.json() : Promise.reject(new Error(`HTTP ${resp.status}`)))
                    .then((payload) => {
                        const title = normalizeYouTubeReadableTitle(payload && payload.title, videoId);
                        youtubeDeckTitleCache.set(videoId, { pending: false, title, error: '' });
                        if (title) {
                            applyResolvedYouTubeTitle(videoId, title);
                        }
                    })
                    .catch((error) => {
                        youtubeDeckTitleCache.set(videoId, {
                            pending: false,
                            title: '',
                            error: error && error.message ? String(error.message) : 'lookup failed',
                            retryAt: Date.now() + (5 * 60 * 1000),
                        });
                    });
            }

            function preferredMediaTitle(el, type, currentTitle, options = {}) {
                if (!el) return String(currentTitle || '').trim() || 'Now Playing';
                if (type !== 'youtube') return String(currentTitle || '').trim() || titleFromMedia(el, type);
                const domTitle = getYouTubeReadableTitleFromDom(el);
                if (domTitle) return domTitle;
                const playerTitle = titleFromMedia(el, type);
                const normalizedPlayer = normalizeYouTubeReadableTitle(playerTitle, getYouTubeVideoId(el));
                if (normalizedPlayer) return normalizedPlayer;
                if (options && options.allowLookup === true) {
                    requestYouTubeReadableTitle(el);
                }
                const fallback = normalizeYouTubeReadableTitle(currentTitle, getYouTubeVideoId(el));
                return fallback || 'YouTube video';
            }

            function isDeckMediaItem(item) {
                return !!(item && (item.type === 'audio' || item.type === 'video' || item.type === 'youtube'));
            }

            /**
             * Resolve the post/message root for a deck queue item.
             * When media has been moved into the deck or mini host, `sourceContainer(item.el)` is null — do not use it.
             */
            function deckItemSourceEl(item) {
                if (!item) return null;
                if (item.sourceEl && item.sourceEl.isConnected) return item.sourceEl;
                if (item.el && item.el.isConnected) {
                    const el = item.el;
                    if (el.closest && el.closest('#sidebar-media-deck-stage, #sidebar-media-mini-video')) {
                        return null;
                    }
                    return sourceContainer(el);
                }
                return null;
            }

            function firstConnectedDeckAnchor(...candidates) {
                for (let i = 0; i < candidates.length; i++) {
                    const el = candidates[i];
                    if (el && el.isConnected) return el;
                }
                return null;
            }

            /** If the pinned source node was replaced (e.g. channel re-render), re-resolve from stored ids. */
            function refreshDeckOriginSourceElIfStale() {
                if (state.deckOriginSourceEl && state.deckOriginSourceEl.isConnected) return;
                const mid = String(state.deckOriginMessageId || '').trim();
                const pid = String(state.deckOriginPostId || '').trim();
                if (mid && typeof document.querySelector === 'function') {
                    const esc = window.CSS && typeof window.CSS.escape === 'function'
                        ? window.CSS.escape(mid)
                        : mid.replace(/["\\]/g, '\\$&');
                    const row = document.querySelector(`.message-item[data-message-id="${esc}"]`);
                    if (row && row.isConnected) {
                        state.deckOriginSourceEl = row;
                        return;
                    }
                }
                if (pid && typeof document.querySelector === 'function') {
                    const esc = window.CSS && typeof window.CSS.escape === 'function'
                        ? window.CSS.escape(pid)
                        : pid.replace(/["\\]/g, '\\$&');
                    const card = document.querySelector(`.post-card[data-post-id="${esc}"]`);
                    if (card && card.isConnected) {
                        state.deckOriginSourceEl = card;
                    }
                }
            }

            function pinDeckOriginIdsFromSourceEl(sourceEl) {
                if (!sourceEl || !sourceEl.getAttribute) return;
                state.deckOriginMessageId = String(sourceEl.getAttribute('data-message-id') || '').trim();
                state.deckOriginPostId = String(sourceEl.getAttribute('data-post-id') || '').trim();
            }

            function ensureMediaIdentity(el) {
                if (!el) return '';
                const type = mediaTypeFor(el);
                if (type === 'youtube') {
                    const wrapper = (el.matches && el.matches('.youtube-embed'))
                        ? el
                        : (el.closest ? el.closest('.youtube-embed') : null);
                    const holder = wrapper || el;
                    if (!holder.__canopyMiniMediaId) {
                        state.mediaCounter += 1;
                        holder.__canopyMiniMediaId = `canopy-media-${state.mediaCounter}`;
                    }
                    if (el !== holder) {
                        el.__canopyMiniMediaId = holder.__canopyMiniMediaId;
                    }
                    const iframe = holder.querySelector ? holder.querySelector('iframe') : null;
                    if (iframe) {
                        iframe.__canopyMiniMediaId = holder.__canopyMiniMediaId;
                    }
                    return String(holder.__canopyMiniMediaId || '');
                }
                if (!el.__canopyMiniMediaId) {
                    state.mediaCounter += 1;
                    el.__canopyMiniMediaId = `canopy-media-${state.mediaCounter}`;
                }
                return String(el.__canopyMiniMediaId || '');
            }

            function safeMediaThumbSrc(value) {
                if (typeof _safeImageSrc === 'function') {
                    return _safeImageSrc(value || '');
                }
                return value || '';
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
                                    maybeRestoreYouTubeDockState(el);
                                },
                                onStateChange: (event) => {
                                    el.__canopyMiniYTState = event && Number.isFinite(event.data) ? event.data : -1;
                                    if (isYouTubePlayingState(el.__canopyMiniYTState)) {
                                        state.dismissedEl = null;
                                        setCurrent(el, 'youtube');
                                        return;
                                    }
                                    scheduleMiniUpdate(50);
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
                if (!el || !el.closest) return null;
                const postOrMessage = el.closest('.post-card[data-post-id], .message-item[data-message-id]');
                return postOrMessage || el.closest('.card');
            }

            function parseSourceLayoutConfig(rootEl) {
                if (!(rootEl instanceof Element)) return null;
                const raw = String(rootEl.getAttribute('data-canopy-source-layout') || '').trim();
                if (!raw) return null;
                try {
                    const parsed = JSON.parse(raw);
                    if (!parsed || typeof parsed !== 'object') return null;
                    return parsed;
                } catch (_) {
                    return null;
                }
            }

            function getSourceLayoutSignature(layout) {
                try {
                    return JSON.stringify(layout || {});
                } catch (_) {
                    return '';
                }
            }

            function getSourceLayoutRoot(sourceEl) {
                if (!(sourceEl instanceof Element)) return null;
                return sourceEl.querySelector('[data-canopy-source-layout]');
            }

            function getSourceLayoutDefaultRef(sourceEl) {
                const layoutRoot = getSourceLayoutRoot(sourceEl);
                if (!(layoutRoot instanceof Element)) return '';
                const layout = parseSourceLayoutConfig(layoutRoot);
                const ref = layout && layout.deck ? String(layout.deck.default_ref || '').trim() : '';
                return ref || '';
            }

            function findSourceRefNode(rootEl, ref) {
                if (!(rootEl instanceof Element)) return null;
                const cleanRef = String(ref || '').trim();
                if (!cleanRef) return null;
                const nodes = rootEl.querySelectorAll('[data-canopy-source-ref]');
                for (const node of nodes) {
                    if (String(node.getAttribute('data-canopy-source-ref') || '').trim() === cleanRef) {
                        return node;
                    }
                }
                return null;
            }

            function hasMeaningfulSourceChildren(node) {
                if (!(node instanceof Element)) return false;
                if (node.querySelector('[data-canopy-source-ref], .attachment-item, .dm-attachment, .mg-cell, .embed-preview, .provider-card-embed')) {
                    return true;
                }
                return !!String(node.textContent || '').trim();
            }

            function pruneEmptySourceLayoutWrappers(rootEl) {
                if (!(rootEl instanceof Element)) return;
                rootEl.querySelectorAll('.media-grid, .post-attachments, .attachments, .dm-attachment-list').forEach((node) => {
                    if (!hasMeaningfulSourceChildren(node)) {
                        node.remove();
                    }
                });
            }

            function createSourceLayoutActions(actions) {
                if (!Array.isArray(actions) || !actions.length) return null;
                const row = document.createElement('div');
                row.className = 'canopy-source-layout-actions';
                actions.forEach((action) => {
                    if (!action || String(action.kind || '').trim() !== 'link') return;
                    const label = String(action.label || '').trim();
                    const url = String(action.url || '').trim();
                    if (!label || !url) return;
                    const isHttp = url.startsWith('http://') || url.startsWith('https://');
                    const isPath = url.startsWith('/') && !url.startsWith('//');
                    if (!isHttp && !isPath) return;
                    const link = document.createElement('a');
                    link.className = 'btn btn-sm btn-outline-secondary';
                    link.href = url;
                    if (url.startsWith('http://') || url.startsWith('https://')) {
                        link.target = '_blank';
                        link.rel = 'noopener noreferrer';
                    }
                    link.textContent = label;
                    row.appendChild(link);
                });
                return row.childElementCount ? row : null;
            }

            function moveSourceNode(node, slot, claimed) {
                if (!(node instanceof Element) || !(slot instanceof Element)) return;
                if (claimed.has(node)) return;
                claimed.add(node);
                slot.appendChild(node);
            }

            /**
             * Channel images use a single `.mg-cell` inside `.media-grid` with max-width caps.
             * Moving only the cell leaves an empty grid and keeps thumbnail sizing — promote the
             * whole grid when this attachment is the only cell in that grid.
             */
            function promoteAttachmentHostNode(node) {
                if (!(node instanceof Element) || !node.classList || !node.classList.contains('mg-cell')) {
                    return node;
                }
                const grid = node.closest('.media-grid');
                if (!(grid instanceof Element)) return node;
                try {
                    const cells = grid.querySelectorAll('.mg-cell');
                    if (cells.length === 1) return grid;
                } catch (_) {
                    return node;
                }
                return node;
            }

            function applySourceLayout(rootEl) {
                if (!(rootEl instanceof Element)) return;
                const layout = parseSourceLayoutConfig(rootEl);
                if (!layout) return;
                const signature = getSourceLayoutSignature(layout);
                const existingShell = Array.from(rootEl.children || []).find((child) => child.classList && child.classList.contains('canopy-source-layout-shell')) || null;
                if (existingShell && existingShell.getAttribute('data-layout-signature') === signature) {
                    if (layout.deck && layout.deck.default_ref) {
                        rootEl.setAttribute('data-canopy-default-deck-ref', String(layout.deck.default_ref || '').trim());
                    }
                    return;
                }
                /* Layout JSON changed (e.g. live edit): rebuild. Removing the shell returns moved
                   nodes to rootEl so findSourceRefNode / moveSourceNode can run again. */
                if (existingShell) {
                    while (existingShell.firstChild) {
                        rootEl.insertBefore(existingShell.firstChild, existingShell);
                    }
                    existingShell.remove();
                }

                const shell = document.createElement('div');
                shell.className = 'canopy-source-layout-shell';
                shell.setAttribute('data-layout-signature', signature);

                const top = document.createElement('div');
                top.className = 'canopy-source-layout-top';
                const main = document.createElement('div');
                main.className = 'canopy-source-layout-main';
                const side = document.createElement('aside');
                side.className = 'canopy-source-layout-side';
                top.appendChild(main);
                top.appendChild(side);

                const hero = document.createElement('div');
                hero.className = 'canopy-source-layout-hero';
                const lede = document.createElement('div');
                lede.className = 'canopy-source-layout-lede';
                const actions = document.createElement('div');
                actions.className = 'canopy-source-layout-actions-wrap';
                const strip = document.createElement('div');
                strip.className = 'canopy-source-layout-strip';
                const below = document.createElement('div');
                below.className = 'canopy-source-layout-below';

                main.appendChild(hero);
                main.appendChild(lede);
                main.appendChild(actions);
                shell.appendChild(top);
                shell.appendChild(strip);
                shell.appendChild(below);

                const claimed = new Set();
                const heroRef = layout.hero && layout.hero.ref ? String(layout.hero.ref).trim() : '';
                const ledeRef = layout.lede && layout.lede.ref ? String(layout.lede.ref).trim() : 'content:lede';
                if (heroRef) {
                    let heroNode = findSourceRefNode(rootEl, heroRef);
                    heroNode = promoteAttachmentHostNode(heroNode);
                    moveSourceNode(heroNode, hero, claimed);
                }
                if (ledeRef) {
                    const ledeNode = findSourceRefNode(rootEl, ledeRef);
                    if (ledeNode && !claimed.has(ledeNode)) {
                        moveSourceNode(ledeNode, lede, claimed);
                    }
                }
                if (Array.isArray(layout.supporting)) {
                    layout.supporting.forEach((entry) => {
                        let node = findSourceRefNode(rootEl, entry && entry.ref);
                        node = promoteAttachmentHostNode(node);
                        if (!(node instanceof Element) || claimed.has(node)) return;
                        const placement = String(entry.placement || '').trim();
                        const targetSlot = placement === 'right' ? side : placement === 'strip' ? strip : below;
                        const labelText = entry && entry.label ? String(entry.label).trim() : '';
                        if (labelText) {
                            const wrap = document.createElement('div');
                            wrap.className = 'canopy-source-layout-supporting-block';
                            const lab = document.createElement('div');
                            lab.className = 'canopy-source-layout-slot-label';
                            lab.textContent = labelText;
                            wrap.appendChild(lab);
                            moveSourceNode(node, wrap, claimed);
                            moveSourceNode(wrap, targetSlot, claimed);
                        } else {
                            moveSourceNode(node, targetSlot, claimed);
                        }
                    });
                }

                pruneEmptySourceLayoutWrappers(rootEl);
                Array.from(rootEl.childNodes).forEach((child) => {
                    if (child === shell) return;
                    if (child instanceof Element && claimed.has(child)) return;
                    if (child instanceof Text && !String(child.textContent || '').trim()) {
                        child.remove();
                        return;
                    }
                    below.appendChild(child);
                });

                const deckRef = layout.deck && layout.deck.default_ref ? String(layout.deck.default_ref).trim() : '';
                let toolbar = createSourceLayoutActions(layout.actions);
                if (!toolbar && deckRef) {
                    toolbar = document.createElement('div');
                    toolbar.className = 'canopy-source-layout-actions canopy-source-layout-actions--deck-only';
                }
                if (toolbar && deckRef) {
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'btn btn-sm btn-outline-primary canopy-source-layout-deck-launch';
                    btn.setAttribute('aria-label', 'Open Canopy media deck for this source');
                    btn.innerHTML = '<i class="bi bi-grid-1x2-fill me-1" aria-hidden="true"></i>Open deck';
                    btn.addEventListener('click', () => {
                        try {
                            const src = sourceContainer(rootEl);
                            if (src) openMediaDeckForSource(src, {});
                        } catch (_) {}
                    });
                    toolbar.appendChild(btn);
                }
                if (toolbar) {
                    actions.appendChild(toolbar);
                }
                if (layout.hero && layout.hero.label && String(layout.hero.label).trim() && hero.childNodes.length) {
                    const hl = document.createElement('div');
                    hl.className = 'canopy-source-layout-hero-label';
                    hl.textContent = String(layout.hero.label).trim();
                    hero.insertBefore(hl, hero.firstChild);
                }
                [hero, lede, actions, side, strip, below].forEach((slot) => {
                    if (!slot.childNodes.length) slot.remove();
                });
                if (!top.childNodes.length) top.remove();
                rootEl.appendChild(shell);
                if (deckRef) {
                    rootEl.setAttribute('data-canopy-default-deck-ref', deckRef);
                }
            }

            function applySourceLayoutsInScope(scope) {
                const seen = new Set();
                const maybeApply = (node) => {
                    if (!(node instanceof Element)) return;
                    const root = node.matches('[data-canopy-source-layout]')
                        ? node
                        : (node.closest ? node.closest('[data-canopy-source-layout]') : null);
                    if (!root || seen.has(root)) return;
                    seen.add(root);
                    applySourceLayout(root);
                };
                maybeApply(scope instanceof Element ? scope : null);
                if (scope && scope.querySelectorAll) {
                    scope.querySelectorAll('[data-canopy-source-layout]').forEach(maybeApply);
                } else {
                    document.querySelectorAll('[data-canopy-source-layout]').forEach(maybeApply);
                }
            }

            if (typeof window !== 'undefined') {
                window.canopyApplySourceLayoutsInScope = applySourceLayoutsInScope;
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
                    const domTitle = getYouTubeReadableTitleFromDom(el);
                    if (domTitle) {
                        return domTitle;
                    }
                    const player = el.__canopyMiniYTPlayer;
                    if (player && typeof player.getVideoData === 'function') {
                        try {
                            const data = player.getVideoData() || {};
                            const playerTitle = normalizeYouTubeReadableTitle(data.title, getYouTubeVideoId(el));
                            if (playerTitle) {
                                const wrapper = el.closest && el.closest('.youtube-embed');
                                el.setAttribute('data-canopy-media-title', playerTitle);
                                if (wrapper) wrapper.setAttribute('data-canopy-media-title', playerTitle);
                                return playerTitle;
                            }
                        } catch (_) {}
                    }
                    return 'YouTube video';
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

            function subtitleFromMedia(el, type) {
                const base = sourceSubtitle(el);
                if (type === 'audio') return `${base} • audio`;
                if (type === 'video') return `${base} • video`;
                if (type === 'youtube') return `${base} • youtube`;
                return base;
            }

            function getYouTubeVideoId(el) {
                if (!el) return '';
                const wrapper = el.closest ? el.closest('.youtube-embed') : null;
                const direct = String(
                    el.getAttribute('data-video-id') ||
                    (wrapper && wrapper.getAttribute('data-video-id')) ||
                    ''
                ).trim();
                if (direct) return direct;
                const src = String(
                    el.getAttribute('src') ||
                    el.getAttribute('data-iframe-src') ||
                    (wrapper && wrapper.getAttribute('data-iframe-src')) ||
                    (wrapper && wrapper.querySelector && wrapper.querySelector('.yt-facade')
                        && wrapper.querySelector('.yt-facade').getAttribute('data-iframe-src')) ||
                    ''
                ).trim();
                if (!src) return '';
                try {
                    const url = new URL(src, window.location.origin);
                    const embedMatch = url.pathname.match(/\/embed\/([^/?#&]+)/);
                    if (embedMatch && embedMatch[1]) return String(embedMatch[1]).trim();
                    if ((url.hostname === 'youtu.be' || url.hostname.endsWith('.youtu.be')) && url.pathname) {
                        return String(url.pathname.split('/').filter(Boolean)[0] || '').trim();
                    }
                    const watchId = String(url.searchParams.get('v') || '').trim();
                    if (watchId) return watchId;
                } catch (_) {}
                return '';
            }

            function resolveMediaThumbnail(el, type) {
                if (!el) return '';
                if (type === 'youtube') {
                    const videoId = getYouTubeVideoId(el);
                    if (videoId) {
                        return `https://i.ytimg.com/vi/${encodeURIComponent(videoId)}/hqdefault.jpg`;
                    }
                }

                if (type === 'video') {
                    const poster = safeMediaThumbSrc(el.getAttribute('poster') || '');
                    if (poster) return poster;
                }

                const attachment = el.closest('.attachment-item, .provider-card-embed, .embed-preview');
                if (attachment) {
                    const candidate = attachment.querySelector('img');
                    if (candidate) {
                        const src = safeMediaThumbSrc(candidate.getAttribute('src') || candidate.src || '');
                        if (src) return src;
                    }
                }

                const container = sourceContainer(el);
                if (container) {
                    const candidates = Array.from(container.querySelectorAll('img')).filter((img) => {
                        if (!(img instanceof HTMLImageElement)) return false;
                        const src = safeMediaThumbSrc(img.getAttribute('src') || img.src || '');
                        if (!src) return false;
                        if (img.closest('.sidebar-dm-avatar, .sidebar-peer-avatar, .profile-avatar, .message-avatar, .comment-avatar')) return false;
                        const width = Number(img.getAttribute('width') || img.naturalWidth || img.width || 0);
                        const height = Number(img.getAttribute('height') || img.naturalHeight || img.height || 0);
                        return width >= 80 || height >= 80;
                    });
                    if (candidates.length) {
                        const img = candidates[0];
                        return safeMediaThumbSrc(img.getAttribute('src') || img.src || '');
                    }
                }
                return '';
            }

            function getMediaDockWrapper(el, type) {
                if (!el) return null;
                if (type === 'youtube') return (el.matches && el.matches('.youtube-embed') ? el : el.closest('.youtube-embed')) || el;
                if (type === 'video') return el;
                return null;
            }

            function capturePlaceholderSize(el, wrapper) {
                const rect = wrapper && typeof wrapper.getBoundingClientRect === 'function'
                    ? wrapper.getBoundingClientRect()
                    : (el && typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : null);
                return {
                    width: Math.max(160, Math.round((rect && rect.width) || (wrapper && wrapper.offsetWidth) || (el && el.offsetWidth) || 320)),
                    height: Math.max(90, Math.round((rect && rect.height) || (wrapper && wrapper.offsetHeight) || (el && el.offsetHeight) || 180)),
                };
            }

            function populateMediaPlaceholderPreview(placeholder, el, type) {
                if (!(placeholder instanceof Element) || !el) return;
                const thumb = resolveMediaThumbnail(el, type);
                const label = type === 'youtube' ? 'Playing in deck' : 'Open in deck';
                placeholder.setAttribute('aria-label', label);
                placeholder.setAttribute('title', label);
                if (thumb) {
                    placeholder.style.backgroundImage = `linear-gradient(rgba(15, 23, 42, 0.18), rgba(15, 23, 42, 0.5)), url("${String(thumb).replace(/"/g, '%22')}")`;
                    placeholder.style.backgroundSize = 'cover';
                    placeholder.style.backgroundPosition = 'center';
                }
                const badge = document.createElement('div');
                badge.className = 'canopy-media-placeholder-badge';
                badge.innerHTML = `<i class="bi ${mediaIcon(type)}"></i><span>${label}</span>`;
                placeholder.appendChild(badge);
            }

            function ensureMediaPlaceholder(el, type) {
                if (!el) return null;
                if (type === 'youtube') {
                    if (el.__canopyAutoDockPlaceholder) return el.__canopyAutoDockPlaceholder;
                    const existingWrapper = getMediaDockWrapper(el, type);
                    if (existingWrapper && existingWrapper !== el && existingWrapper.__canopyAutoDockPlaceholder) {
                        return existingWrapper.__canopyAutoDockPlaceholder;
                    }
                }
                if (type === 'video' && el.__canopyMiniVideoPlaceholder) return el.__canopyMiniVideoPlaceholder;
                const wrapper = getMediaDockWrapper(el, type);
                if (!wrapper || !wrapper.parentNode) return null;
                if (wrapper.parentNode === miniVideoHost || wrapper.parentNode === deckStage) return null;
                const size = capturePlaceholderSize(el, wrapper);
                const placeholder = document.createElement('div');
                placeholder.className = type === 'youtube' ? 'canopy-yt-mini-placeholder' : 'canopy-video-mini-placeholder';
                placeholder.style.cssText = `width:${size.width}px;height:${size.height}px;`;
                populateMediaPlaceholderPreview(placeholder, el, type);
                wrapper.parentNode.insertBefore(placeholder, wrapper);
                if (type === 'youtube') {
                    const storeTarget = wrapper !== el ? wrapper : el;
                    storeTarget.__canopyAutoDockPlaceholder = placeholder;
                    if (el !== storeTarget) el.__canopyAutoDockPlaceholder = placeholder;
                } else if (type === 'video') {
                    el.__canopyMiniVideoPlaceholder = placeholder;
                }
                if (state.observer) state.observer.observe(placeholder);
                return placeholder;
            }

            function restoreDockedMedia(el, options = {}) {
                if (!el) return;
                const type = mediaTypeFor(el);
                const preferMini = options && options.preferMini === true;
                if (type === 'youtube') {
                    const forceDockMini = options && options.forceDockMini === true;
                    if (preferMini && miniVideoHost && (forceDockMini || isOffscreen(el))) {
                        const wrapper = getMediaDockWrapper(el, type);
                        if (wrapper && wrapper.parentNode !== miniVideoHost) {
                            prepareYouTubeEmbedForHostMove(el, {
                                skipResumeUrlRewrite: isSidebarDeckOrMiniHost(wrapper.parentNode),
                            });
                            miniVideoHost.innerHTML = '';
                            miniVideoHost.appendChild(wrapper);
                            miniVideoHost.style.display = 'block';
                            const ytIframe = resolveYouTubeMediaElement(el, { activate: false });
                            if (ytIframe && ytIframe.tagName.toLowerCase() === 'iframe') {
                                maybeRestoreYouTubeDockState(ytIframe);
                            }
                        }
                        return;
                    }
                    const wrapper = getMediaDockWrapper(el, type);
                    const ph = el.__canopyAutoDockPlaceholder
                        || (wrapper && wrapper !== el ? wrapper.__canopyAutoDockPlaceholder : null);
                    if (ph && ph.isConnected && ph.parentNode && wrapper) {
                        prepareYouTubeEmbedForHostMove(el, {
                            skipResumeUrlRewrite: true,
                        });
                        ph.parentNode.insertBefore(wrapper, ph);
                        ph.remove();
                        const videoId = getYouTubeVideoId(wrapper || el);
                        if (videoId) {
                            const existingCaption = wrapper.querySelector('.embed-provider-caption');
                            const iframe = resolveYouTubeMediaElement(wrapper, { activate: false });
                            if (iframe && iframe.tagName && iframe.tagName.toLowerCase() === 'iframe') {
                                resetYouTubePlayerBridge(iframe);
                            }
                            wrapper.innerHTML = '';
                            wrapper.appendChild(createYouTubeFacadeElement(videoId, buildYouTubeEmbedSrc(videoId, true)));
                            if (existingCaption) {
                                wrapper.appendChild(existingCaption);
                            } else {
                                wrapper.insertAdjacentHTML('beforeend', buildEmbedCaption('YouTube'));
                            }
                        }
                    }
                    delete el.__canopyAutoDockPlaceholder;
                    if (wrapper && wrapper !== el) delete wrapper.__canopyAutoDockPlaceholder;
                } else if (type === 'video') {
                    const ph = el.__canopyMiniVideoPlaceholder;
                    if (ph && ph.isConnected && ph.parentNode) {
                        ph.parentNode.insertBefore(el, ph);
                        ph.remove();
                    }
                    delete el.__canopyMiniVideoPlaceholder;
                }
            }

            function restoreDeckStageChildToSource(node) {
                if (!(node instanceof Element)) return false;
                let mediaEl = null;
                if (node.matches && node.matches('video, .youtube-embed')) {
                    mediaEl = node;
                } else if (node.querySelector) {
                    mediaEl = node.querySelector('video, .youtube-embed');
                }
                if (!mediaEl) return false;
                const type = mediaTypeFor(mediaEl);
                if (type !== 'youtube' && type !== 'video') return false;
                restoreDockedMedia(mediaEl, { preferMini: false });
                return true;
            }

            function clearDeckStageDockedNodes() {
                if (!deckStage) return;
                Array.from(deckStage.children).forEach((child) => {
                    if (child === deckVisual) return;
                    teardownDeckModuleSessionsInNode(child);
                    restoreDeckStageChildToSource(child);
                    if (child.parentNode === deckStage) {
                        child.remove();
                    }
                });
            }

            function moveDockedMediaToHost(el, host) {
                if (!el || !host) return false;
                const type = mediaTypeFor(el);
                if (type !== 'youtube' && type !== 'video') return false;
                ensureMediaPlaceholder(el, type);
                const wrapper = getMediaDockWrapper(el, type);
                if (!wrapper) return false;
                if (wrapper.parentNode === host) {
                    host.style.display = 'block';
                    return true;
                }
                if (type === 'youtube') {
                    const currentHost = wrapper.parentNode;
                    const preserveIframeDocument = !!(
                        currentHost &&
                        currentHost.isConnected &&
                        host &&
                        host.isConnected
                    );
                    prepareYouTubeEmbedForHostMove(el, {
                        skipResumeUrlRewrite: preserveIframeDocument,
                    });
                }
                if (host === deckStage) {
                    clearDeckStageDockedNodes();
                    if (deckVisual && deckVisual.parentNode === deckStage) {
                        deckStage.insertBefore(wrapper, deckVisual);
                    } else {
                        host.appendChild(wrapper);
                    }
                } else {
                    host.innerHTML = '';
                    host.appendChild(wrapper);
                }
                host.style.display = 'block';
                if (type === 'youtube') {
                    const ytIframe = resolveYouTubeMediaElement(el, { activate: false });
                    if (ytIframe && ytIframe.tagName.toLowerCase() === 'iframe') {
                        maybeRestoreYouTubeDockState(ytIframe);
                    }
                }
                return true;
            }

            function clearOrphanedDockedMedia(el, type, sourceEl) {
                if (!el) return;
                const wrapper = getMediaDockWrapper(el, type);
                if (!wrapper) return;
                const parent = wrapper.parentNode;
                if (parent !== miniVideoHost && parent !== deckStage) return;
                if (sourceEl && sourceEl.isConnected) return;
                wrapper.remove();
                if (parent === miniVideoHost && miniVideoHost) {
                    miniVideoHost.innerHTML = '';
                    miniVideoHost.style.display = 'none';
                }
                if (parent === deckStage && deckStage) {
                    clearDeckStageDockedNodes();
                    deckStage.classList.add('is-empty');
                }
            }

            function pauseMediaElement(el, type) {
                if (!el) return;
                try {
                    if (type === 'audio' || type === 'video') {
                        el.pause();
                    } else if (type === 'youtube') {
                        const target = resolveYouTubeMediaElement(el, { activate: false });
                        const player = target && target.__canopyMiniYTPlayer;
                        if (player && typeof player.pauseVideo === 'function') {
                            player.pauseVideo();
                            target.__canopyMiniYTState = 2;
                        }
                    }
                } catch (_) {}
            }

            function deactivateMediaEntry(entry, options = {}) {
                if (!entry || !entry.el) return;
                const el = entry.el;
                const type = entry.type || mediaTypeFor(el);
                if (!type) return;
                pauseMediaElement(el, type);
                if (type === 'youtube') {
                    clearYouTubeDockResumeState(el);
                }
                restoreDockedMedia(el, { preferMini: false });
                // Re-assert pause after restoration so a switched-away item cannot
                // keep playing from its original source behind the active deck item.
                pauseMediaElement(el, type);
                clearOrphanedDockedMedia(el, type, entry.sourceEl || sourceContainer(el));
                if (options.resetReturnState !== false) {
                    state.dockedSubtitle = null;
                    state.returnUrl = null;
                }
            }

            function playMediaElement(el, type) {
                if (!el) return;
                try {
                    if (type === 'audio' || type === 'video') {
                        const playResult = el.play();
                        if (playResult && typeof playResult.catch === 'function') {
                            playResult.catch(() => {});
                        }
                    } else if (type === 'youtube') {
                        const ytEl = resolveYouTubeMediaElement(el, { activate: true, autoplay: true });
                        if (!ytEl || ytEl.tagName.toLowerCase() !== 'iframe') return;
                        initYouTubePlayer(ytEl);
                        const player = ytEl.__canopyMiniYTPlayer;
                        if (player && typeof player.playVideo === 'function') {
                            player.playVideo();
                            ytEl.__canopyMiniYTState = 1;
                        }
                    }
                } catch (_) {}
            }

            function buildRelatedMediaList(sourceEl, activeEl) {
                const items = [];
                const seen = new Set();

                function pushCandidate(node) {
                    if (!node || !(node instanceof Element)) return;
                    const type = mediaTypeFor(node);
                    if (!type) return;
                    const target = type === 'youtube' ? (resolveYouTubeMediaElement(node, { activate: false }) || node) : node;
                    const key = ensureMediaIdentity(target);
                    if (!key || seen.has(key)) return;
                    seen.add(key);
                    items.push({
                        key,
                        el: target,
                        type,
                        title: titleFromMedia(target, type),
                        subtitle: subtitleFromMedia(target, type),
                        thumb: resolveMediaThumbnail(target, type),
                    });
                }

                if (activeEl) pushCandidate(activeEl);
                if (sourceEl && sourceEl.querySelectorAll) {
                    sourceEl.querySelectorAll('audio, video, .youtube-embed').forEach(pushCandidate);
                }
                return items;
            }

            function buildDeckWidgetItem(node, manifest, sourceEl) {
                if (!(node instanceof Element) || !manifest || !manifest.key) return null;
                return {
                    key: manifest.key,
                    el: node,
                    sourceEl: sourceEl || sourceContainer(node),
                    type: manifest.widget_type,
                    title: manifest.title,
                    subtitle: manifest.subtitle || sourceSubtitle(node),
                    thumb: manifest.thumb_url || '',
                    providerLabel: manifest.provider_label || mediaProviderLabel(manifest.widget_type),
                    icon: manifest.icon || mediaIcon(manifest.widget_type),
                    manifest,
                };
            }

            function mergeExplicitDeckItem(items, explicitItem) {
                const merged = Array.isArray(items) ? items.slice() : [];
                if (!explicitItem || !explicitItem.key) return merged;
                const existingIndex = merged.findIndex((item) => String(item && item.key || '') === String(explicitItem.key || ''));
                if (existingIndex >= 0) {
                    merged[existingIndex] = {
                        ...merged[existingIndex],
                        ...explicitItem,
                        sourceEl: explicitItem.sourceEl || merged[existingIndex].sourceEl || null,
                        manifest: explicitItem.manifest || merged[existingIndex].manifest || null,
                        el: explicitItem.el || merged[existingIndex].el,
                    };
                    return merged;
                }
                merged.push(explicitItem);
                return merged;
            }

            /**
             * Resolve a widget manifest for deck queue discovery: inline JSON, or module card bundle-id rebuild
             * (matches openMediaDeckForManifestNode so queue rebuilds do not drop modules without data-canopy-widget-manifest).
             */
            function widgetManifestFromDeckNode(node) {
                if (!(node instanceof Element)) return null;
                let manifest = parseDeckWidgetManifest(node);
                if (manifest) return manifest;
                if (String(node.getAttribute('data-canopy-module-card') || '').trim() !== '1') return null;
                const fid = extractCanopyModuleBundleFileIdFromHost(node);
                if (!fid) return null;
                let rawName = String(node.getAttribute('data-canopy-module-bundle-name') || '').trim();
                if (!rawName) {
                    const titleEl = node.querySelector('.stream-card-title, .fw-semibold');
                    if (titleEl && titleEl.textContent) rawName = titleEl.textContent.trim();
                }
                const rawBuilt = buildCanopyModuleSurfaceManifestFromBundleId(fid, rawName);
                return rawBuilt ? sanitizeDeckWidgetManifest(rawBuilt) : null;
            }

            function buildSourceWidgetList(sourceEl) {
                const items = [];
                const seen = new Set();
                if (!sourceEl || !sourceEl.querySelectorAll) return items;
                const candidates = new Set();
                sourceEl.querySelectorAll('[data-canopy-widget-manifest]').forEach((n) => candidates.add(n));
                sourceEl.querySelectorAll('[data-canopy-module-card="1"]').forEach((n) => candidates.add(n));
                candidates.forEach((node) => {
                    const manifest = widgetManifestFromDeckNode(node);
                    const item = buildDeckWidgetItem(node, manifest, sourceEl);
                    if (!item || seen.has(item.key)) return;
                    seen.add(item.key);
                    items.push(item);
                });
                return items;
            }

            /** True if el is still under this deck session's message/post (survives stale sourceEl refs). */
            function widgetDeckOriginContainsEl(origin, el) {
                if (!(el instanceof Element) || !el.isConnected) return false;
                if (origin && origin.isConnected && origin.contains(el)) return true;
                const mid = String(state.deckOriginMessageId || '').trim();
                if (mid && typeof document.querySelector === 'function') {
                    const esc = window.CSS && typeof window.CSS.escape === 'function'
                        ? window.CSS.escape(mid)
                        : mid.replace(/["\\]/g, '\\$&');
                    const row = document.querySelector(`.message-item[data-message-id="${esc}"]`);
                    if (row && row.isConnected && row.contains(el)) return true;
                }
                const pid = String(state.deckOriginPostId || '').trim();
                if (pid && typeof document.querySelector === 'function') {
                    const esc = window.CSS && typeof window.CSS.escape === 'function'
                        ? window.CSS.escape(pid)
                        : pid.replace(/["\\]/g, '\\$&');
                    const card = document.querySelector(`.post-card[data-post-id="${esc}"]`);
                    if (card && card.isConnected && card.contains(el)) return true;
                }
                return false;
            }

            /** On deck open: ensure every widget node under sourceEl appears in the list (Deck launcher + Open module parity). */
            function mergeDeckWidgetUnionIntoDeckItems(sourceEl, items) {
                if (!sourceEl || !sourceEl.isConnected || !sourceEl.querySelectorAll) return items;
                const out = Array.isArray(items) ? items.slice() : [];
                const keys = new Set();
                out.forEach((i) => {
                    if (i && i.key !== undefined && i.key !== null && i.key !== '') keys.add(i.key);
                });
                try {
                    buildSourceWidgetList(sourceEl).forEach((w) => {
                        if (!w || w.key === undefined || w.key === null || w.key === '' || keys.has(w.key)) return;
                        out.push(w);
                        keys.add(w.key);
                    });
                } catch (_) {
                    /* Do not block deck open if widget discovery throws on malformed DOM. */
                }
                return out;
            }

            /**
             * After a queue rebuild, never drop widget rows that still belong to this session's post/message
             * if the fresh DOM scan missed them (docked media, anchor churn, manifest attr edge cases).
             */
            function deckItemKeyUsable(key) {
                return key !== undefined && key !== null && key !== '';
            }

            function deckItemBelongsToOrigin(item, origin) {
                if (!item) return false;
                const itemSource = item.sourceEl instanceof Element ? item.sourceEl : null;
                if (origin && origin.isConnected && itemSource && itemSource === origin) {
                    return true;
                }
                const originMessageId = String((origin && origin.getAttribute && origin.getAttribute('data-message-id')) || state.deckOriginMessageId || '').trim();
                const originPostId = String((origin && origin.getAttribute && origin.getAttribute('data-post-id')) || state.deckOriginPostId || '').trim();
                const itemMessageId = String((itemSource && itemSource.getAttribute && itemSource.getAttribute('data-message-id')) || '').trim();
                const itemPostId = String((itemSource && itemSource.getAttribute && itemSource.getAttribute('data-post-id')) || '').trim();
                if (originMessageId && itemMessageId && originMessageId === itemMessageId) return true;
                if (originPostId && itemPostId && originPostId === itemPostId) return true;
                return false;
            }

            function canPreserveDeckItemFromPrevious(item, origin) {
                if (!item || !deckItemKeyUsable(item.key)) return false;
                if (isDeckMediaItem(item)) {
                    const wrapper = getMediaDockWrapper(item.el, item.type);
                    const stillConnected = !!((wrapper && wrapper.isConnected) || (item.el instanceof Element && item.el.isConnected));
                    return stillConnected && deckItemBelongsToOrigin(item, origin);
                }
                if (!item.manifest) return false;
                if (!(item.el instanceof Element) || !item.el.isConnected) return false;
                return widgetDeckOriginContainsEl(origin, item.el) || deckItemBelongsToOrigin(item, origin);
            }

            function reconcileDeckQueueItemsBuilt(built, previousItems, origin) {
                const merged = [];
                const keys = new Set();
                const builtArr = Array.isArray(built) ? built : [];
                const prevArr = Array.isArray(previousItems) ? previousItems : [];
                const builtByKey = new Map();
                builtArr.forEach((item) => {
                    if (!item || !deckItemKeyUsable(item.key)) return;
                    builtByKey.set(item.key, item);
                });
                builtArr.forEach((item) => {
                    if (!item || !deckItemKeyUsable(item.key) || keys.has(item.key)) return;
                    merged.push(item);
                    keys.add(item.key);
                });
                prevArr.forEach((item) => {
                    if (!item || !deckItemKeyUsable(item.key) || keys.has(item.key)) return;
                    const replacement = builtByKey.get(item.key);
                    if (replacement) {
                        return;
                    }
                    if (!canPreserveDeckItemFromPrevious(item, origin)) return;
                    merged.push(item);
                    keys.add(item.key);
                });
                if (origin && origin.isConnected && origin.querySelectorAll) {
                    try {
                        buildSourceWidgetList(origin).forEach((w) => {
                            if (!w || !deckItemKeyUsable(w.key) || keys.has(w.key)) return;
                            merged.push(w);
                            keys.add(w.key);
                        });
                    } catch (_) {
                        /* Keep merged list from built + preserved widgets. */
                    }
                }
                /* Empty rebuild would clear deckItems → getDeckSelectedItem null → deck hides when current is null. */
                if (!merged.length && prevArr.length) {
                    return prevArr.filter((item) => item && deckItemKeyUsable(item.key));
                }
                return merged;
            }

            function buildSourceDeckItems(sourceEl, activeEl) {
                const mediaItems = buildRelatedMediaList(sourceEl, activeEl).map((item) => ({
                    ...item,
                    sourceEl: sourceEl || sourceContainer(item.el),
                }));
                let widgetItems = [];
                try {
                    widgetItems = buildSourceWidgetList(sourceEl);
                } catch (_) {
                    widgetItems = [];
                }
                return mediaItems.concat(widgetItems);
            }

            function getActiveMediaForSource(sourceEl) {
                if (!sourceEl || !sourceEl.isConnected || !state.current || !state.current.el) return null;
                return state.current.sourceEl === sourceEl ? state.current.el : null;
            }

            function getSourceMediaDeckItems(sourceEl) {
                if (!sourceEl || !sourceEl.isConnected) return [];
                return buildRelatedMediaList(sourceEl, getActiveMediaForSource(sourceEl));
            }

            function getSourceDeckItems(sourceEl) {
                if (!sourceEl || !sourceEl.isConnected) return [];
                return buildSourceDeckItems(sourceEl, getActiveMediaForSource(sourceEl));
            }

            function getDeckSelectedItem() {
                if (!Array.isArray(state.deckItems) || !state.deckItems.length) return null;
                if (state.deckSelectedKey) {
                    const selected = state.deckItems.find((item) => item.key === state.deckSelectedKey);
                    if (selected) return selected;
                }
                if (state.current && state.current.el) {
                    const currentMatch = state.deckItems.find((item) => isSameDeckMediaItem(
                        state.current.el,
                        state.current.type,
                        item
                    ));
                    if (currentMatch) return currentMatch;
                }
                return state.deckItems[0] || null;
            }

            function ensureMediaSourceLinked() {
                if (!state.current || !state.current.el || !state.current.el.isConnected) return;
                if (!state.current.sourceEl || !state.current.sourceEl.isConnected) {
                    const next = sourceContainer(state.current.el);
                    if (next && next.isConnected) {
                        state.current.sourceEl = next;
                    } else if (state.deckOpen) {
                        const pinned = firstConnectedDeckAnchor(
                            state.deckOriginSourceEl,
                            state.deckSourceEl
                        );
                        if (pinned) {
                            state.current.sourceEl = pinned;
                        }
                    }
                }
            }

            /**
             * If the current media node was removed (DOM churn) but the post/message is still in the document,
             * rebind `state.current` to the best playable element in that source.
             * @returns {boolean} true if `state.current` points at a connected node afterward
             */
            function repairMediaCurrentReference() {
                if (!state.current) return false;
                const cur = state.current;
                if (cur.el && cur.el.isConnected) {
                    ensureMediaSourceLinked();
                    return true;
                }
                if (!cur.sourceEl || !cur.sourceEl.isConnected) {
                    return false;
                }
                const items = getSourceDeckItems(cur.sourceEl);
                if (!items.length) {
                    return false;
                }
                const pref = getPreferredDeckItemForSource(cur.sourceEl, items);
                if (!pref) {
                    return false;
                }
                state.deckItems = items;
                state.deckQueueSignature = '';
                if (isDeckMediaItem(pref)) {
                    state.current = {
                        el: pref.el,
                        type: pref.type,
                        sourceEl: cur.sourceEl,
                        activatedAt: Date.now(),
                    };
                    state.deckSelectedKey = pref.key || '';
                } else {
                    state.current = null;
                    state.deckSelectedKey = pref.key || '';
                }
                state.dismissedEl = null;
                return true;
            }

            /** If the deck is open but the video/YT wrapper is not on the stage, move it back (fixes empty stage after races). */
            function reconcileDeckStageMediaPlacement() {
                if (!state.deckOpen || !state.current || !deckStage) return;
                const t = state.current.type;
                if (t !== 'youtube' && t !== 'video') return;
                const w = getMediaDockWrapper(state.current.el, t);
                if (!w || !w.isConnected) return;
                if (deckStage.contains(w)) return;
                if (miniVideoHost && miniVideoHost.contains(w)) return;
                moveDockedMediaToHost(state.current.el, deckStage);
            }

            function scrollDeckSelectionIntoView() {
                if (!deckQueue || !state.deckSelectedKey) return;
                const selectorKey = window.CSS && typeof window.CSS.escape === 'function'
                    ? window.CSS.escape(state.deckSelectedKey)
                    : state.deckSelectedKey.replace(/["\\]/g, '\\$&');
                const activeBtn = deckQueue.querySelector(`.sidebar-media-deck-item[data-media-key="${selectorKey}"]`);
                if (!activeBtn || typeof activeBtn.scrollIntoView !== 'function') return;
                window.requestAnimationFrame(() => {
                    activeBtn.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
                });
            }

            function selectDeckItem(item, options = {}) {
                if (!item) return;
                const shouldPlay = options.play === true;
                state.dismissedEl = null;
                state.deckSourceEl = firstConnectedDeckAnchor(
                    deckItemSourceEl(item),
                    state.deckOriginSourceEl,
                    state.deckSourceEl
                );
                if (isDeckMediaItem(item)) {
                    const deferYt = !shouldPlay && item.type === 'youtube';
                    setCurrent(item.el, item.type, deferYt ? { deferYouTubeMaterialize: true } : undefined);
                    state.deckSelectedKey = item.key || '';
                    if (state.current && state.current.el) {
                        if (shouldPlay) {
                            playMediaElement(state.current.el, state.current.type);
                        } else {
                            pauseMediaElement(state.current.el, state.current.type);
                        }
                    }
                } else {
                    if (state.current && state.current.el) {
                        deactivateMediaEntry(state.current);
                    }
                    state.current = null;
                    state.deckSelectedKey = item.key || '';
                }
                updateDeckPanel();
                updateSourceDeckLauncherActiveStates();
                scrollDeckSelectionIntoView();
            }

            function resolveSourceMediaDeckLauncherHost(sourceEl) {
                if (!sourceEl || !sourceEl.isConnected) {
                    return { host: null, owned: false };
                }
                const actionsHost = sourceEl.querySelector('[data-post-actions] .d-flex.gap-2.flex-wrap, .message-actions .d-flex.gap-2.flex-wrap');
                if (actionsHost) {
                    return { host: actionsHost, owned: false };
                }
                let slot = sourceEl.__canopyMediaDeckSlot;
                if (slot && slot.isConnected) {
                    return { host: slot, owned: true };
                }
                slot = sourceEl.querySelector('.canopy-media-deck-source-slot[data-media-deck-slot="1"]');
                if (!slot) {
                    slot = document.createElement('div');
                    slot.className = 'canopy-media-deck-source-slot';
                    slot.setAttribute('data-media-deck-slot', '1');
                    const dmFooter = sourceEl.querySelector('.dm-bubble-footer');
                    const dmBubble = sourceEl.querySelector('.dm-bubble');
                    if (dmFooter && typeof dmFooter.insertAdjacentElement === 'function') {
                        dmFooter.insertAdjacentElement('afterend', slot);
                    } else if (dmBubble) {
                        dmBubble.appendChild(slot);
                    } else {
                        sourceEl.appendChild(slot);
                    }
                }
                sourceEl.__canopyMediaDeckSlot = slot;
                return { host: slot, owned: true };
            }

            /** True when deck item matches current media (YouTube may be iframe in state vs wrapper in item list). */
            function isSameDeckMediaItem(currentEl, currentType, item) {
                if (!currentEl || !item) return false;
                if (currentType !== item.type) return false;
                if (item.type === 'youtube') {
                    const a = getMediaDockWrapper(currentEl, 'youtube');
                    const b = getMediaDockWrapper(item.el, 'youtube');
                    return !!(a && b && a === b);
                }
                return item.el === currentEl;
            }

            function getPreferredDeckItemForSource(sourceEl, items) {
                const deckItems = Array.isArray(items) ? items : getSourceDeckItems(sourceEl);
                if (!deckItems.length) return null;
                if (state.current && state.current.el && state.current.sourceEl === sourceEl) {
                    const currentMatch = deckItems.find((item) => isDeckMediaItem(item) && isSameDeckMediaItem(
                        state.current.el,
                        state.current.type,
                        item
                    ));
                    if (currentMatch) return currentMatch;
                }
                const defaultRef = getSourceLayoutDefaultRef(sourceEl);
                if (defaultRef) {
                    const defaultMatch = deckItems.find((item) => {
                        const manifestKey = item && item.manifest && item.manifest.key
                            ? `widget:${String(item.manifest.key || '').trim()}`
                            : '';
                        if (manifestKey && manifestKey === defaultRef) return true;
                        const host = item && item.el && item.el.closest ? item.el.closest('[data-canopy-source-ref]') : null;
                        return !!(host && String(host.getAttribute('data-canopy-source-ref') || '').trim() === defaultRef);
                    });
                    if (defaultMatch) return defaultMatch;
                }
                const playingMatch = deckItems.find((item) => isDeckMediaItem(item) && isElementPlaying(item.el, item.type));
                if (playingMatch) return playingMatch;
                return deckItems[0] || null;
            }

            function updateSourceDeckLauncherActiveStates() {
                const activeSourceEl = state.deckOpen
                    ? firstConnectedDeckAnchor(
                        state.deckSourceEl,
                        state.deckOriginSourceEl,
                        state.current && state.current.sourceEl
                    )
                    : ((state.current && state.current.sourceEl && state.dismissedEl !== state.current.el) ? state.current.sourceEl : null);
                const activeSource = activeSourceEl ? ensureMediaIdentity(activeSourceEl) : '';
                document.querySelectorAll('[data-open-media-deck], [data-open-mini-player]').forEach((btn) => {
                    const isDeck = btn.hasAttribute('data-open-media-deck');
                    const sourceId = String(btn.getAttribute('data-source-media-id') || '');
                    const isActive = !!sourceId && !!activeSource && sourceId === activeSource && (isDeck ? state.deckOpen : !state.deckOpen);
                    btn.classList.toggle('is-active', isActive);
                    btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
                    if (isDeck) btn.setAttribute('aria-expanded', isActive ? 'true' : 'false');
                });
            }

            function openMediaDeckForSource(sourceEl, options = {}) {
                if (!sourceEl || !sourceEl.isConnected) return;
                const explicitItem = options.explicitItem || null;
                let items = mergeExplicitDeckItem(getSourceDeckItems(sourceEl), explicitItem);
                const previousDeckItems = Array.isArray(state.deckItems) ? state.deckItems : [];
                items = reconcileDeckQueueItemsBuilt(items, previousDeckItems, sourceEl);
                const preferredKey = String(options.preferredKey || '').trim();
                let preferred = preferredKey
                    ? items.find((item) => String(item.key || '').trim() === preferredKey)
                    : null;
                if (!preferred && explicitItem && explicitItem.key) {
                    preferred = items.find((item) => String(item.key || '').trim() === String(explicitItem.key || '').trim()) || explicitItem;
                }
                if (!preferred) {
                    preferred = getPreferredDeckItemForSource(sourceEl, items);
                }
                if (!preferred) return;
                try {
                    items = mergeDeckWidgetUnionIntoDeckItems(sourceEl, items);
                } catch (_) {
                    /* Keep pre-union list so deck still opens. */
                }
                state.deckItems = items;
                state.deckQueueSignature = '';
                state.deckQueueNeedsRefresh = false;
                state.deckSourceEl = sourceEl;
                state.deckOriginSourceEl = sourceEl;
                pinDeckOriginIdsFromSourceEl(sourceEl);
                state.deckSelectedKey = preferred.key;
                state.dismissedEl = null;
                state.returnUrl = null;
                state.dockedSubtitle = null;
                state.deckOpen = true;
                state.deckShellMode = 'source';
                selectDeckItem(preferred, { play: options.play === true });
                updateSourceDeckLauncherActiveStates();
                scheduleMiniUpdate(20);
            }

            function openMediaDeckForManifestNode(node) {
                if (!(node instanceof Element)) return false;
                const manifestHost = resolveCanopyModuleDeckManifestHost(node);
                if (!manifestHost || !manifestHost.isConnected) return false;
                let manifest = parseDeckWidgetManifest(manifestHost);
                if (!manifest) {
                    const fid = extractCanopyModuleBundleFileIdFromHost(manifestHost);
                    if (fid) {
                        let rawName = manifestHost.getAttribute('data-canopy-module-bundle-name') || '';
                        if (!rawName) {
                            const titleEl = manifestHost.querySelector('.stream-card-title, .fw-semibold');
                            if (titleEl && titleEl.textContent) rawName = titleEl.textContent.trim();
                        }
                        const rawBuilt = buildCanopyModuleSurfaceManifestFromBundleId(fid, rawName);
                        manifest = rawBuilt ? sanitizeDeckWidgetManifest(rawBuilt) : null;
                    }
                }
                if (!manifest) {
                    if (typeof showAlert === 'function') {
                        showAlert('Could not open module — attachment metadata is incomplete or invalid.', 'warning');
                    }
                    return false;
                }
                const sourceEl = deckItemSourceEl({ el: manifestHost }) || sourceContainer(manifestHost);
                if (!sourceEl || !sourceEl.isConnected) {
                    if (typeof showAlert === 'function') {
                        showAlert('Could not open module — source message/post container was not found.', 'warning');
                    }
                    return false;
                }
                const explicitItem = buildDeckWidgetItem(manifestHost, manifest, sourceEl);
                if (!explicitItem) {
                    if (typeof showAlert === 'function') {
                        showAlert('Could not open module — deck item could not be built.', 'warning');
                    }
                    return false;
                }
                openMediaDeckForSource(sourceEl, {
                    preferredKey: manifest.key || String(manifestHost.getAttribute('data-canopy-widget-key') || '').trim(),
                    explicitItem,
                });
                return true;
            }

            /** Open the sidebar mini player for this post/message (no deck); keeps YouTube as facade until Play. */
            function openMiniPlayerForSource(sourceEl) {
                if (!sourceEl || !sourceEl.isConnected || !miniVideoHost) return;
                const previousDeckItems = Array.isArray(state.deckItems) ? state.deckItems : [];
                const fullDeckItems = reconcileDeckQueueItemsBuilt(
                    mergeDeckWidgetUnionIntoDeckItems(sourceEl, getSourceDeckItems(sourceEl)),
                    previousDeckItems,
                    sourceEl
                );
                const items = getSourceMediaDeckItems(sourceEl);
                const preferred = getPreferredDeckItemForSource(sourceEl, items);
                if (!preferred) return;
                state.deckOpen = false;
                state.deckSourceEl = sourceEl;
                state.deckOriginSourceEl = sourceEl;
                pinDeckOriginIdsFromSourceEl(sourceEl);
                if (expandBtn) {
                    expandBtn.innerHTML = '<i class="bi bi-arrows-angle-expand"></i>';
                    expandBtn.title = 'Open Canopy deck';
                }
                state.deckItems = fullDeckItems;
                state.deckQueueSignature = '';
                state.deckQueueNeedsRefresh = false;
                state.deckSelectedKey = preferred.key;
                state.dismissedEl = null;
                state.returnUrl = null;
                state.dockedSubtitle = null;
                selectDeckItem(preferred, { play: false });
                if (state.current && (preferred.type === 'youtube' || preferred.type === 'video')) {
                    moveDockedMediaToHost(state.current.el, miniVideoHost);
                }
                if (deckStage && !deckStage.querySelector('.youtube-embed, video')) {
                    deckStage.classList.add('is-empty');
                    if (deckVisual) deckVisual.hidden = false;
                }
                updateDeckVisibility();
                updateSourceDeckLauncherActiveStates();
                scheduleMiniUpdate(20);
            }

            function switchDeckToMiniPlayer() {
                const selectedItem = getDeckSelectedItem();
                if (!selectedItem || !isDeckMediaItem(selectedItem) || !state.current || !state.current.el || !miniVideoHost) {
                    closeMediaDeck({ forceClose: true });
                    return;
                }
                state.deckOpen = false;
                if (expandBtn) {
                    expandBtn.innerHTML = '<i class="bi bi-arrows-angle-expand"></i>';
                    expandBtn.title = 'Open Canopy deck';
                }
                const { el, type } = state.current;
                if (type === 'youtube' || type === 'video') {
                    moveDockedMediaToHost(el, miniVideoHost);
                }
                if (deckStage && !deckStage.querySelector('.youtube-embed, video')) {
                    deckStage.classList.add('is-empty');
                    if (deckVisual) deckVisual.hidden = false;
                }
                updateDeckVisibility();
                updateSourceDeckLauncherActiveStates();
                scheduleMiniUpdate(30);
            }

            function attachMediaLauncherButton(btn, mqCoarseOrNarrow, openFn) {
                let lastOpenAt = 0;
                const openFromLauncher = (event) => {
                    if (event) {
                        event.preventDefault();
                        event.stopPropagation();
                    }
                    openFn();
                    lastOpenAt = Date.now();
                };
                btn.addEventListener('pointerdown', (event) => {
                    if (!mqCoarseOrNarrow.matches || event.pointerType === 'mouse' || event.button !== 0) return;
                    openFromLauncher(event);
                }, true);
                btn.addEventListener('click', (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    if (mqCoarseOrNarrow.matches && (Date.now() - lastOpenAt) < 650) return;
                    openFromLauncher(event);
                });
            }

            function syncSourceMediaDeckLauncher(sourceEl) {
                if (!sourceEl || !sourceEl.isConnected) return;
                const items = getSourceDeckItems(sourceEl);
                const mediaItems = items.filter(isDeckMediaItem);

                function removeLegacyStandaloneLaunchers() {
                    sourceEl.querySelectorAll('[data-open-media-deck], [data-open-mini-player]').forEach((btn) => {
                        if (!btn.closest('[data-canopy-playback-launcher]')) {
                            btn.remove();
                        }
                    });
                }

                if (!items.length) {
                    const wrap = sourceEl.querySelector('[data-canopy-playback-launcher]');
                    if (wrap) wrap.remove();
                    removeLegacyStandaloneLaunchers();
                    if (sourceEl.__canopyMediaDeckSlot && sourceEl.__canopyMediaDeckSlot.isConnected && !sourceEl.__canopyMediaDeckSlot.childElementCount) {
                        sourceEl.__canopyMediaDeckSlot.remove();
                    }
                    delete sourceEl.__canopyMediaDeckSlot;
                    return;
                }

                removeLegacyStandaloneLaunchers();

                const hostInfo = resolveSourceMediaDeckLauncherHost(sourceEl);
                const host = hostInfo.host;
                if (!host) return;
                const currentSourceId = ensureMediaIdentity(sourceEl);
                const mqCoarseOrNarrow = window.matchMedia('(max-width: 640px), (pointer: coarse)');

                let wrap = sourceEl.querySelector('[data-canopy-playback-launcher]');
                let btnDeck;
                let btnMini;
                let divider;

                if (!wrap) {
                    wrap = document.createElement('div');
                    wrap.className = 'canopy-media-playback-launcher';
                    wrap.setAttribute('data-canopy-playback-launcher', '1');
                    wrap.setAttribute('role', 'group');
                    wrap.setAttribute('aria-label', 'Open Canopy deck or mini player');

                    btnDeck = document.createElement('button');
                    btnDeck.type = 'button';
                    btnDeck.className = 'canopy-media-playback-seg canopy-media-playback-seg--deck';
                    btnDeck.setAttribute('data-open-media-deck', '1');
                    attachMediaLauncherButton(btnDeck, mqCoarseOrNarrow, () => openMediaDeckForSource(sourceEl));

                    divider = document.createElement('span');
                    divider.className = 'canopy-media-playback-seg-divider';
                    divider.setAttribute('aria-hidden', 'true');

                    btnMini = document.createElement('button');
                    btnMini.type = 'button';
                    btnMini.className = 'canopy-media-playback-seg canopy-media-playback-seg--mini';
                    btnMini.setAttribute('data-open-mini-player', '1');
                    attachMediaLauncherButton(btnMini, mqCoarseOrNarrow, () => openMiniPlayerForSource(sourceEl));

                    wrap.appendChild(btnDeck);
                    wrap.appendChild(divider);
                    wrap.appendChild(btnMini);
                } else {
                    btnDeck = wrap.querySelector('[data-open-media-deck]');
                    btnMini = wrap.querySelector('[data-open-mini-player]');
                    divider = wrap.querySelector('.canopy-media-playback-seg-divider');
                }

                if (wrap.parentNode !== host) {
                    host.appendChild(wrap);
                }

                const countLabel = items.length === 1 ? '1' : String(items.length);
                const miniCountLabel = mediaItems.length === 1 ? '1' : String(mediaItems.length);
                const deckOnly = mediaItems.length === 0;
                const renderSig = `${currentSourceId}|${countLabel}|${miniCountLabel}|${deckOnly ? '1' : '0'}`;

                wrap.classList.toggle('canopy-media-playback-launcher--deck-only', deckOnly);
                wrap.classList.toggle('is-in-source-slot', !!hostInfo.owned);
                if (divider) {
                    divider.hidden = deckOnly;
                    divider.style.display = deckOnly ? 'none' : '';
                }
                if (btnMini) {
                    btnMini.hidden = deckOnly;
                    btnMini.style.display = deckOnly ? 'none' : '';
                }

                btnDeck.setAttribute('data-source-media-id', currentSourceId);
                btnMini.setAttribute('data-source-media-id', currentSourceId);
                btnDeck.setAttribute('data-launcher-signature', `${currentSourceId}|${countLabel}`);
                btnMini.setAttribute('data-launcher-signature', `${currentSourceId}|${miniCountLabel}`);
                btnDeck.setAttribute('aria-label', items.length > 1 ? `Open deck with ${items.length} items` : 'Open deck');
                btnDeck.title = items.length > 1 ? `Open deck (${items.length} items)` : 'Open deck';
                btnMini.setAttribute(
                    'aria-label',
                    mediaItems.length > 1 ? `Open mini player (${mediaItems.length} playable)` : 'Open mini player'
                );
                btnMini.title = mediaItems.length > 1 ? `Mini player · ${mediaItems.length} playable` : 'Mini player';

                if (wrap.getAttribute('data-rendered-signature') !== renderSig) {
                    btnDeck.innerHTML =
                        `<i class="bi bi-grid-1x2" aria-hidden="true"></i>` +
                        `<span class="canopy-media-deck-launcher-label">Deck</span>` +
                        `<span class="canopy-media-deck-launcher-count">${countLabel}</span>`;
                    if (!deckOnly) {
                        btnMini.innerHTML =
                            `<i class="bi bi-pip" aria-hidden="true"></i>` +
                            `<span class="canopy-media-deck-launcher-label">Mini</span>` +
                            `<span class="canopy-media-deck-launcher-count">${miniCountLabel}</span>`;
                    }
                    wrap.setAttribute('data-rendered-signature', renderSig);
                }
            }

            function syncSourceMediaDeckLaunchersInScope(scope) {
                const seen = new Set();
                const addSource = (node) => {
                    if (!(node instanceof Element)) return;
                    const source = node.matches && node.matches('.post-card[data-post-id], .message-item[data-message-id]')
                        ? node
                        : node.closest ? node.closest('.post-card[data-post-id], .message-item[data-message-id]') : null;
                    if (!source || seen.has(source)) return;
                    seen.add(source);
                    syncSourceMediaDeckLauncher(source);
                };

                addSource(scope instanceof Element ? scope : null);
                if (scope && scope.querySelectorAll) {
                    scope.querySelectorAll('.post-card[data-post-id], .message-item[data-message-id]').forEach(addSource);
                } else {
                    document.querySelectorAll('.post-card[data-post-id], .message-item[data-message-id]').forEach(addSource);
                }
                updateSourceDeckLauncherActiveStates();
            }

            function setDeckVisualState(item) {
                if (!deckVisual || !deckVisualIcon || !deckVisualTitle || !deckVisualSubtitle || !deckVisualCover) return;
                const type = item ? item.type : '';
                const cover = item ? item.thumb : '';
                const iconClass = mediaIcon(type);
                deckVisual.hidden = false;
                deckVisualIcon.innerHTML = `<i class="bi ${iconClass}"></i>`;
                deckVisualTitle.textContent = item ? item.title : 'Now Playing';
                deckVisualSubtitle.textContent = item ? item.subtitle : 'Expanded playback with related media from the same post or message.';
                deckVisualCover.style.backgroundImage = cover
                    ? `url("${String(cover).replace(/"/g, '%22')}")`
                    : 'none';
            }

            function setDeckWidgetSummaryHidden(hidden) {
                if (!deckWidgetSummary) return;
                deckWidgetSummary.hidden = !!hidden;
            }

            function clearDeckWidgetSummary() {
                setDeckWidgetSummaryHidden(true);
                if (deckWidgetBadges) deckWidgetBadges.innerHTML = '';
                if (deckWidgetDetails) deckWidgetDetails.innerHTML = '';
                if (deckWidgetActions) deckWidgetActions.innerHTML = '';
            }

            function setDeckStationSummaryHidden(hidden) {
                if (!deckStationSummary) return;
                deckStationSummary.hidden = !!hidden;
            }

            function clearDeckStationSummary() {
                setDeckStationSummaryHidden(true);
                if (deckStationPolicy) deckStationPolicy.textContent = 'Bounded actions';
                if (deckStationTitle) deckStationTitle.textContent = 'Source-bound surface';
                if (deckStationSubtitle) deckStationSubtitle.textContent = 'Typed operational context stays attached to the source while actions remain bounded.';
                if (deckStationBadges) deckStationBadges.innerHTML = '';
            }

            function isDeckModuleItem(item) {
                return !!(item && item.manifest && item.manifest.render_mode === 'module_runtime');
            }

            function setDeckQueueCollapsed(collapsed) {
                const next = !!collapsed;
                state.deckQueueCollapsed = next;
                if (deckQueueShell) {
                    deckQueueShell.classList.toggle('is-collapsed', next);
                }
                if (deckQueueToggle) {
                    deckQueueToggle.setAttribute('aria-expanded', next ? 'false' : 'true');
                    deckQueueToggle.innerHTML = next
                        ? '<i class="bi bi-chevron-down"></i><span>Show list</span>'
                        : '<i class="bi bi-chevron-up"></i><span>Collapse list</span>';
                }
            }

            function setDeckDetailCollapsed(collapsed) {
                const next = !!collapsed;
                state.deckDetailCollapsed = next;
                if (deckDetail) {
                    deckDetail.classList.toggle('is-collapsed', next);
                }
                if (deckDetailToggle) {
                    deckDetailToggle.setAttribute('aria-expanded', next ? 'false' : 'true');
                    deckDetailToggle.innerHTML = next
                        ? '<i class="bi bi-layout-text-window"></i><span>Show details</span>'
                        : '<i class="bi bi-layout-text-window-reverse"></i><span>Hide details</span>';
                }
            }

            function syncDeckLayoutMode(selectedItem) {
                const moduleActive = isDeckModuleItem(selectedItem);
                if (deck) {
                    deck.classList.toggle('is-module-active', moduleActive);
                }
                if (moduleActive) {
                    const itemCount = Array.isArray(state.deckItems) ? state.deckItems.length : 0;
                    const keyStr = String(selectedItem.key || '');
                    const layoutBump = state.deckLayoutMode !== 'module'
                        || state.deckLayoutPrimedKey !== keyStr
                        || state.deckLayoutLastQueueCount !== itemCount;
                    if (layoutBump) {
                        setDeckQueueCollapsed(true);
                        setDeckDetailCollapsed(true);
                        state.deckLayoutPrimedKey = keyStr;
                        state.deckLayoutLastQueueCount = itemCount;
                    }
                    state.deckLayoutMode = 'module';
                    return;
                }
                if (state.deckLayoutMode === 'module') {
                    setDeckQueueCollapsed(false);
                    setDeckDetailCollapsed(false);
                }
                state.deckLayoutMode = 'default';
                state.deckLayoutPrimedKey = '';
                state.deckLayoutLastQueueCount = -1;
            }

            function resetDeckLayoutMode() {
                if (deck) {
                    deck.classList.remove('is-module-active');
                }
                setDeckQueueCollapsed(false);
                setDeckDetailCollapsed(false);
                state.deckLayoutMode = 'default';
                state.deckLayoutPrimedKey = '';
                state.deckLayoutLastQueueCount = -1;
            }

            function getRequestedDeckModuleCapabilities(manifest) {
                const runtime = manifest && manifest.module_runtime && typeof manifest.module_runtime === 'object'
                    ? manifest.module_runtime
                    : {};
                const required = Array.isArray(runtime.capabilities && runtime.capabilities.required)
                    ? runtime.capabilities.required
                    : [];
                const optional = Array.isArray(runtime.capabilities && runtime.capabilities.optional)
                    ? runtime.capabilities.optional
                    : [];
                return normalizeDeckModuleCapabilityList(required.concat(optional));
            }

            function deckModuleCapabilityApprovalKey(manifest, capability) {
                const runtime = manifest && manifest.module_runtime && typeof manifest.module_runtime === 'object'
                    ? manifest.module_runtime
                    : {};
                const moduleId = String(runtime.bundle_file_id || runtime.bundle_url || (manifest && manifest.key) || 'module').trim();
                const cap = String(capability || '').trim().toLowerCase();
                return moduleId && cap ? `${moduleId}:${cap}` : '';
            }

            function deckModuleCapabilityApproved(manifest, capability) {
                const cap = String(capability || '').trim().toLowerCase();
                if (!CANOPY_MODULE_REVIEWED_CAPABILITIES.has(cap)) return true;
                const key = deckModuleCapabilityApprovalKey(manifest, cap);
                return !!(key && state.moduleCapabilityApprovals.has(key));
            }

            function approveDeckModuleCapability(manifest, capability) {
                const key = deckModuleCapabilityApprovalKey(manifest, capability);
                if (key) state.moduleCapabilityApprovals.add(key);
            }

            function getGrantedDeckModuleCapabilities(manifest) {
                return getRequestedDeckModuleCapabilities(manifest).filter((capability) => (
                    deckModuleCapabilityApproved(manifest, capability)
                ));
            }

            function getPendingDeckModuleCapabilities(manifest) {
                return getRequestedDeckModuleCapabilities(manifest).filter((capability) => (
                    CANOPY_MODULE_REVIEWED_CAPABILITIES.has(capability)
                    && !deckModuleCapabilityApproved(manifest, capability)
                ));
            }

            function deckModuleHasCapability(manifest, capability) {
                return getGrantedDeckModuleCapabilities(manifest).includes(String(capability || '').trim().toLowerCase());
            }

            function humanizeDeckModuleCapability(capability) {
                const cap = String(capability || '').trim().toLowerCase();
                if (cap === 'module.render.webgl') return 'WebGL rendering';
                if (cap === 'module.render.wasm') return 'WebAssembly execution';
                if (cap === 'source.attachments.read') return 'Source attachment data';
                if (cap === 'module.storage.local') return 'Local source storage';
                if (cap === 'module.storage.module') return 'Local module storage';
                if (cap === 'source.read') return 'Source read';
                if (cap === 'source.snapshot') return 'Source snapshot';
                if (cap === 'deck.return') return 'Deck return';
                if (cap === 'deck.close') return 'Deck close';
                if (cap === 'deck.media.observe') return 'Media observe';
                if (cap === 'clipboard.write') return 'Clipboard write';
                return cap;
            }

            function applyDeckModuleBundleDeclarations(item, bundleHtml) {
                if (!item || !item.manifest || !item.manifest.module_runtime) return item;
                const declarations = extractDeckModuleCapabilityDeclarations(bundleHtml);
                if (!declarations.required.length && !declarations.optional.length) return item;
                const manifest = item.manifest;
                const runtime = Object.assign({}, manifest.module_runtime);
                const caps = runtime.capabilities && typeof runtime.capabilities === 'object'
                    ? runtime.capabilities
                    : {};
                runtime.capabilities = {
                    required: mergeDeckModuleCapabilityLists(caps.required, declarations.required),
                    optional: mergeDeckModuleCapabilityLists(caps.optional, declarations.optional),
                };
                item.manifest = Object.assign({}, manifest, { module_runtime: runtime });
                return item;
            }

            function serializeDeckModuleInlineJson(value) {
                return JSON.stringify(value || {}).replace(/</g, '\\u003c');
            }

            function deckModuleStorageByteLength(value) {
                const text = String(value == null ? '' : value);
                try {
                    return new TextEncoder().encode(text).length;
                } catch (_) {
                    try {
                        return unescape(encodeURIComponent(text)).length;
                    } catch (__) {
                        return text.length;
                    }
                }
            }

            function normalizeDeckModuleStorageComponent(value, fallback, maxLength = 140) {
                const raw = String(value == null ? '' : value).trim() || fallback || 'unknown';
                return encodeURIComponent(raw)
                    .replace(/%/g, '~')
                    .replace(/[!'()*]/g, '')
                    .slice(0, maxLength) || String(fallback || 'unknown');
            }

            function normalizeDeckModuleStorageKey(payloadKey) {
                const key = String(payloadKey == null ? '' : payloadKey).trim();
                if (!key) return '';
                if (deckModuleStorageByteLength(key) > CANOPY_MODULE_STORAGE_MAX_KEY_BYTES) return '';
                if (!/^[A-Za-z0-9._:-]+$/.test(key)) return '';
                return key;
            }

            function normalizeDeckModuleStorageValue(rawValue) {
                let value;
                if (typeof rawValue === 'string') {
                    value = rawValue;
                } else if (rawValue == null) {
                    value = '';
                } else {
                    try {
                        value = JSON.stringify(rawValue);
                    } catch (_) {
                        value = String(rawValue);
                    }
                }
                if (deckModuleStorageByteLength(value) > CANOPY_MODULE_STORAGE_MAX_VALUE_BYTES) {
                    throw new Error(`Value exceeds local module storage limit (${CANOPY_MODULE_STORAGE_MAX_VALUE_BYTES} bytes)`);
                }
                return value;
            }

            function getCurrentMeshspaceIdForModuleStorage() {
                const meshNode = document.querySelector('[data-meshspace-current-id]');
                return String(
                    (meshNode && meshNode.getAttribute('data-meshspace-current-id'))
                    || (window.CANOPY_VARS && window.CANOPY_VARS.currentMeshspaceId)
                    || 'default-mesh'
                ).trim() || 'default-mesh';
            }

            function getCurrentUserIdForModuleStorage() {
                return String(
                    (window.CANOPY_VARS && (window.CANOPY_VARS.localUserId || window.CANOPY_VARS.userId))
                    || 'local-user'
                ).trim() || 'local-user';
            }

            function deckModuleStorageSourceId(session) {
                const sourceEl = deckItemSourceEl(session.item);
                return sourceEl
                    ? String(sourceEl.getAttribute('data-post-id') || sourceEl.getAttribute('data-message-id') || 'source')
                    : 'source';
            }

            function deckModuleStorageScope(rawScope) {
                const scope = String(rawScope || 'source').trim().toLowerCase();
                return CANOPY_MODULE_STORAGE_SCOPES.has(scope) ? scope : 'source';
            }

            function deckModuleStorageCapabilityForScope(rawScope) {
                return deckModuleStorageScope(rawScope) === 'module'
                    ? 'module.storage.module'
                    : 'module.storage.local';
            }

            function deckModuleSessionStoragePrefix(session, rawScope) {
                if (!session || !session.item || !session.item.manifest) return '';
                const manifest = session.item.manifest || {};
                const runtime = manifest.module_runtime || {};
                const scope = deckModuleStorageScope(rawScope);
                const meshId = normalizeDeckModuleStorageComponent(getCurrentMeshspaceIdForModuleStorage(), 'mesh');
                const userId = normalizeDeckModuleStorageComponent(getCurrentUserIdForModuleStorage(), 'user');
                const moduleId = normalizeDeckModuleStorageComponent(
                    runtime.bundle_file_id || runtime.bundle_url || manifest.key || 'module',
                    'module',
                    170
                );
                const manifestKey = normalizeDeckModuleStorageComponent(manifest.key || moduleId, 'manifest', 170);
                const sourceId = scope === 'source'
                    ? normalizeDeckModuleStorageComponent(deckModuleStorageSourceId(session), 'source', 170)
                    : 'all-sources';
                return `${CANOPY_MODULE_STORAGE_PREFIX}:${meshId}:${userId}:${moduleId}:${manifestKey}:${scope}:${sourceId}:`;
            }

            function deckModuleSessionStorageKey(session, payloadKey, rawScope) {
                const key = normalizeDeckModuleStorageKey(payloadKey);
                if (!key) return '';
                const prefix = deckModuleSessionStoragePrefix(session, rawScope);
                return prefix ? `${prefix}${key}` : '';
            }

            function deckModuleLegacyStorageKey(session, payloadKey) {
                if (!session || !session.item || !session.item.manifest) return '';
                const key = normalizeDeckModuleStorageKey(payloadKey);
                if (!key) return '';
                const sourceId = deckModuleStorageSourceId(session);
                return `canopy-module:${session.item.manifest.key}:${sourceId}:${key}`;
            }

            function deckModuleStorageScopeEntries(session, rawScope) {
                const prefix = deckModuleSessionStoragePrefix(session, rawScope);
                if (!prefix || !window.localStorage) return [];
                const entries = [];
                for (let i = 0; i < window.localStorage.length; i += 1) {
                    const storageKey = window.localStorage.key(i);
                    if (!storageKey || !storageKey.startsWith(prefix)) continue;
                    const key = storageKey.slice(prefix.length);
                    entries.push({
                        key,
                        storageKey,
                        value: window.localStorage.getItem(storageKey),
                    });
                }
                entries.sort((a, b) => a.key.localeCompare(b.key));
                return entries;
            }

            function deckModuleStorageUsageBytes(entries, replacingStorageKey, nextValue) {
                let total = 0;
                (entries || []).forEach((entry) => {
                    if (!entry || entry.storageKey === replacingStorageKey) return;
                    total += deckModuleStorageByteLength(entry.storageKey);
                    total += deckModuleStorageByteLength(entry.value || '');
                });
                if (replacingStorageKey) {
                    total += deckModuleStorageByteLength(replacingStorageKey);
                    total += deckModuleStorageByteLength(nextValue || '');
                }
                return total;
            }

            function deckModuleStorageInfo(session, rawScope) {
                const scope = deckModuleStorageScope(rawScope);
                const entries = deckModuleStorageScopeEntries(session, scope);
                return {
                    scope,
                    key_count: entries.length,
                    max_keys: CANOPY_MODULE_STORAGE_MAX_KEYS_PER_SCOPE,
                    bytes_used: deckModuleStorageUsageBytes(entries, '', ''),
                    max_bytes: CANOPY_MODULE_STORAGE_MAX_SCOPE_BYTES,
                    max_value_bytes: CANOPY_MODULE_STORAGE_MAX_VALUE_BYTES,
                };
            }

            function deckModuleStorageGet(session, payloadKey, rawScope) {
                const scope = deckModuleStorageScope(rawScope);
                const storageKey = deckModuleSessionStorageKey(session, payloadKey, scope);
                if (!storageKey) throw new Error('Invalid storage key');
                let value = window.localStorage.getItem(storageKey);
                let migrated = false;
                if (value === null && scope === 'source') {
                    const legacyKey = deckModuleLegacyStorageKey(session, payloadKey);
                    if (legacyKey) {
                        const legacyValue = window.localStorage.getItem(legacyKey);
                        if (legacyValue !== null) {
                            value = legacyValue;
                            try {
                                window.localStorage.setItem(storageKey, legacyValue);
                                migrated = true;
                            } catch (_) {
                                migrated = false;
                            }
                        }
                    }
                }
                return {
                    key: normalizeDeckModuleStorageKey(payloadKey),
                    scope,
                    value,
                    found: value !== null,
                    migrated,
                };
            }

            function deckModuleStorageSet(session, payloadKey, rawValue, rawScope) {
                const scope = deckModuleStorageScope(rawScope);
                const storageKey = deckModuleSessionStorageKey(session, payloadKey, scope);
                if (!storageKey) throw new Error('Invalid storage key');
                const value = normalizeDeckModuleStorageValue(rawValue);
                const entries = deckModuleStorageScopeEntries(session, scope);
                const exists = entries.some((entry) => entry.storageKey === storageKey);
                if (!exists && entries.length >= CANOPY_MODULE_STORAGE_MAX_KEYS_PER_SCOPE) {
                    throw new Error(`Local module storage key limit reached (${CANOPY_MODULE_STORAGE_MAX_KEYS_PER_SCOPE})`);
                }
                const nextBytes = deckModuleStorageUsageBytes(entries, storageKey, value);
                if (nextBytes > CANOPY_MODULE_STORAGE_MAX_SCOPE_BYTES) {
                    throw new Error(`Local module storage quota exceeded (${CANOPY_MODULE_STORAGE_MAX_SCOPE_BYTES} bytes per scope)`);
                }
                window.localStorage.setItem(storageKey, value);
                return {
                    key: normalizeDeckModuleStorageKey(payloadKey),
                    scope,
                    stored: true,
                    bytes_used: nextBytes,
                    max_bytes: CANOPY_MODULE_STORAGE_MAX_SCOPE_BYTES,
                };
            }

            function deckModuleStorageRemove(session, payloadKey, rawScope) {
                const scope = deckModuleStorageScope(rawScope);
                const storageKey = deckModuleSessionStorageKey(session, payloadKey, scope);
                if (!storageKey) throw new Error('Invalid storage key');
                const existed = window.localStorage.getItem(storageKey) !== null;
                window.localStorage.removeItem(storageKey);
                return {
                    key: normalizeDeckModuleStorageKey(payloadKey),
                    scope,
                    removed: existed,
                };
            }

            function deckModuleStorageKeys(session, rawScope) {
                const scope = deckModuleStorageScope(rawScope);
                const entries = deckModuleStorageScopeEntries(session, scope);
                return {
                    scope,
                    keys: entries.map((entry) => entry.key).slice(0, CANOPY_MODULE_STORAGE_MAX_KEYS_PER_SCOPE),
                };
            }

            function deckModuleStorageClear(session, rawScope) {
                const scope = deckModuleStorageScope(rawScope);
                const entries = deckModuleStorageScopeEntries(session, scope);
                entries.forEach((entry) => window.localStorage.removeItem(entry.storageKey));
                return {
                    scope,
                    cleared: entries.length,
                };
            }

            function normalizeDeckModuleAttachmentId(value) {
                const raw = String(value == null ? '' : value).trim();
                if (!raw || raw.length > 180) return '';
                if (raw.includes('/') || raw.includes('\\') || /[\x00-\x1f\x7f]/.test(raw)) return '';
                return raw;
            }

            function deckModuleAttachmentIdFromSourceRef(rawRef) {
                const ref = String(rawRef || '').trim();
                if (!ref.toLowerCase().startsWith('attachment:')) return '';
                return normalizeDeckModuleAttachmentId(ref.slice('attachment:'.length));
            }

            function deckModuleAttachmentIdFromFileUrl(rawUrl) {
                try {
                    const url = new URL(String(rawUrl || ''), window.location.origin);
                    if (String(url.origin || '') !== String(window.location.origin || '')) return '';
                    const match = String(url.pathname || '').match(/^\/files\/([^/?#]+)(?:\/thumb)?$/);
                    if (!match) return '';
                    return normalizeDeckModuleAttachmentId(decodeURIComponent(match[1]));
                } catch (_) {
                    return '';
                }
            }

            function sanitizeDeckModuleAttachmentFetchUrl(rawUrl, expectedId) {
                const expected = normalizeDeckModuleAttachmentId(expectedId);
                if (!expected) return '';
                try {
                    const url = new URL(String(rawUrl || ''), window.location.origin);
                    if (String(url.origin || '') !== String(window.location.origin || '')) return '';
                    const match = String(url.pathname || '').match(/^\/files\/([^/?#]+)(?:\/thumb)?$/);
                    if (!match) return '';
                    const found = normalizeDeckModuleAttachmentId(decodeURIComponent(match[1]));
                    if (found !== expected) return '';
                    return `/files/${encodeURIComponent(expected)}`;
                } catch (_) {
                    return '';
                }
            }

            function deckModuleAttachmentExtension(nameOrId) {
                const lower = String(nameOrId || '').trim().toLowerCase();
                const canopyMatch = lower.match(/\.canopy-module\.html?$/);
                if (canopyMatch) return canopyMatch[0];
                const dot = lower.lastIndexOf('.');
                if (dot < 0 || dot === lower.length - 1) return '';
                return lower.slice(dot);
            }

            function deckModuleAttachmentNameFromNode(node, fallbackId) {
                if (!node) return fallbackId || 'attachment';
                const explicit =
                    node.getAttribute('data-canopy-module-bundle-name')
                    || node.getAttribute('download')
                    || '';
                if (explicit) return normalizeDeckWidgetText(explicit, 180) || fallbackId || 'attachment';
                const link = node.matches && node.matches('a[download], a[href*="/files/"]')
                    ? node
                    : node.querySelector && node.querySelector('a[download], a[href*="/files/"]');
                if (link) {
                    const dl = link.getAttribute('download') || '';
                    if (dl) return normalizeDeckWidgetText(dl, 180) || fallbackId || 'attachment';
                }
                const labelNode = node.querySelector && node.querySelector('strong, .fw-semibold, .dm-attachment-meta span');
                const label = normalizeDeckWidgetText((labelNode && labelNode.textContent) || '', 180);
                return label || fallbackId || 'attachment';
            }

            function deckModuleAttachmentTypeFromNode(node) {
                if (!node) return '';
                const media = node.matches && node.matches('img, audio, video, source')
                    ? node
                    : node.querySelector && node.querySelector('source[type], audio source[type], video source[type], img');
                if (media) {
                    const typed = normalizeDeckWidgetText(media.getAttribute('type') || '', 96).toLowerCase();
                    if (typed) return typed;
                    const tag = String(media.tagName || '').toLowerCase();
                    if (tag === 'img') return 'image/*';
                    if (tag === 'audio') return 'audio/*';
                    if (tag === 'video') return 'video/*';
                }
                return '';
            }

            function deckModuleSourceIdFromElement(sourceEl, attrName) {
                if (!sourceEl || !sourceEl.getAttribute) return '';
                return String(sourceEl.getAttribute(attrName) || '').trim();
            }

            function deckModuleSourceMatchesSession(session, sourceEl) {
                if (!session || !sourceEl || !sourceEl.getAttribute) return false;
                const messageId = String(session.source_message_id || '').trim();
                const postId = String(session.source_post_id || '').trim();
                if (!messageId && !postId) return false;
                const sourceMessageId = deckModuleSourceIdFromElement(sourceEl, 'data-message-id');
                const sourcePostId = deckModuleSourceIdFromElement(sourceEl, 'data-post-id');
                if (messageId && sourceMessageId && messageId === sourceMessageId) return true;
                if (postId && sourcePostId && postId === sourcePostId) return true;
                return false;
            }

            function deckModuleSessionSourceEl(session) {
                const itemSource = session && session.item ? deckItemSourceEl(session.item) : null;
                if (itemSource && itemSource.isConnected && deckModuleSourceMatchesSession(session, itemSource)) {
                    return itemSource;
                }
                const messageId = String(session && session.source_message_id || '').trim();
                const postId = String(session && session.source_post_id || '').trim();
                const selectorEscape = (value) => (
                    window.CSS && typeof window.CSS.escape === 'function'
                        ? window.CSS.escape(value)
                        : String(value || '').replace(/["\\]/g, '\\$&')
                );
                if (messageId && typeof document.querySelector === 'function') {
                    const row = document.querySelector(`.message-item[data-message-id="${selectorEscape(messageId)}"]`);
                    if (row && row.isConnected) return row;
                }
                if (postId && typeof document.querySelector === 'function') {
                    const card = document.querySelector(`.post-card[data-post-id="${selectorEscape(postId)}"]`);
                    if (card && card.isConnected) return card;
                }
                return firstConnectedDeckAnchor(
                    itemSource,
                    state.deckOriginSourceEl,
                    state.deckSourceEl
                );
            }

            function deckModuleExplicitAttachmentUrlFromNode(node, attachmentId) {
                if (!node) return '';
                const candidates = [];
                if (node.matches && node.matches('a[href], img[src], source[src]')) {
                    candidates.push(node);
                }
                if (node.querySelectorAll) {
                    node.querySelectorAll('a[href], img[src], source[src]').forEach((candidate) => {
                        candidates.push(candidate);
                    });
                }
                for (const candidate of candidates) {
                    const raw = candidate.getAttribute('href') || candidate.getAttribute('src') || '';
                    const sanitized = sanitizeDeckModuleAttachmentFetchUrl(raw, attachmentId);
                    if (sanitized) return sanitized;
                }
                const onclickNodes = [];
                if (node.matches && node.matches('[onclick]')) onclickNodes.push(node);
                if (node.querySelectorAll) {
                    node.querySelectorAll('[onclick]').forEach((candidate) => onclickNodes.push(candidate));
                }
                for (const candidate of onclickNodes) {
                    const onclick = String(candidate.getAttribute('onclick') || '');
                    const match = onclick.match(/(^|[^A-Za-z0-9_])\/files\/([^'"`)#?\s]+)/);
                    if (!match) continue;
                    const sanitized = sanitizeDeckModuleAttachmentFetchUrl(`/files/${match[2]}`, attachmentId);
                    if (sanitized) return sanitized;
                }
                return '';
            }

            function deckModuleSourceAttachmentEntries(sourceEl) {
                if (!sourceEl || !sourceEl.querySelectorAll) return [];
                const nodes = [];
                if (sourceEl.matches && sourceEl.matches('[data-canopy-source-ref^="attachment:"]')) {
                    nodes.push(sourceEl);
                }
                sourceEl.querySelectorAll('[data-canopy-source-ref^="attachment:"]').forEach((node) => {
                    nodes.push(node);
                });
                const byId = new Map();
                nodes.forEach((node) => {
                    const attachmentId = deckModuleAttachmentIdFromSourceRef(node.getAttribute('data-canopy-source-ref') || '');
                    if (!attachmentId || byId.has(attachmentId)) return;
                    const name = deckModuleAttachmentNameFromNode(node, attachmentId);
                    const explicitUrl = deckModuleExplicitAttachmentUrlFromNode(node, attachmentId);
                    byId.set(attachmentId, {
                        attachment_id: attachmentId,
                        id: attachmentId,
                        name,
                        type: deckModuleAttachmentTypeFromNode(node),
                        extension: deckModuleAttachmentExtension(name || attachmentId),
                        available: !!explicitUrl,
                        fetch_url: explicitUrl,
                    });
                });
                return Array.from(byId.values()).slice(0, CANOPY_MODULE_ATTACHMENT_MAX_LIST);
            }

            function deckModuleSourceAttachmentListForModule(sourceEl) {
                return deckModuleSourceAttachmentEntries(sourceEl).map((entry) => ({
                    attachment_id: entry.attachment_id,
                    id: entry.id,
                    name: entry.name,
                    type: entry.type,
                    extension: entry.extension,
                    available: !!entry.available,
                    max_text_bytes: CANOPY_MODULE_ATTACHMENT_MAX_TEXT_BYTES,
                    max_binary_bytes: CANOPY_MODULE_ATTACHMENT_MAX_BINARY_BYTES,
                }));
            }

            function deckModuleAttachmentPolicy(entry, mode, manifest) {
                const readMode = String(mode || 'text').trim().toLowerCase();
                const name = String((entry && entry.name) || '').trim();
                const attachmentId = String((entry && entry.attachment_id) || '').trim();
                const mime = String((entry && entry.type) || '').trim().toLowerCase();
                const extension = deckModuleAttachmentExtension(name || attachmentId);
                if (CANOPY_MODULE_ATTACHMENT_DENIED_EXTENSIONS.has(extension)) {
                    return { ok: false, reason: 'Attachment type is not readable by modules' };
                }
                if (CANOPY_MODULE_ATTACHMENT_DENIED_MIME_TYPES.has(mime)) {
                    return { ok: false, reason: 'Attachment MIME type is not readable by modules' };
                }
                const wasmLike = extension === '.wasm' || mime === 'application/wasm' || mime === 'application/wasm-module';
                if (wasmLike) {
                    if (!deckModuleHasCapability(manifest, 'module.render.wasm')) {
                        return { ok: false, reason: 'module.render.wasm not granted for WebAssembly data' };
                    }
                    if (readMode !== 'base64' && readMode !== 'data_url') {
                        return { ok: false, reason: 'WebAssembly data must be read as base64 or data_url' };
                    }
                    return { ok: true, mode: readMode, text_like: false, wasm_like: true };
                }
                const textLike =
                    mime.startsWith('text/')
                    || mime === 'application/json'
                    || mime === 'application/geo+json'
                    || mime === 'application/x-ndjson'
                    || mime === 'application/xml'
                    || mime === 'application/x-yaml'
                    || mime === 'application/yaml'
                    || CANOPY_MODULE_ATTACHMENT_TEXT_EXTENSIONS.has(extension);
                const binaryLike =
                    textLike
                    || mime === 'application/octet-stream'
                    || CANOPY_MODULE_ATTACHMENT_BINARY_MIME_TYPES.has(mime)
                    || mime.startsWith('model/')
                    || CANOPY_MODULE_ATTACHMENT_BINARY_EXTENSIONS.has(extension);
                if ((readMode === 'text' || readMode === 'json') && !textLike) {
                    return { ok: false, reason: 'Attachment is not an allowed text or structured data type' };
                }
                if ((readMode === 'base64' || readMode === 'data_url') && !binaryLike) {
                    return { ok: false, reason: 'Attachment is not an allowed module data type' };
                }
                return { ok: true, mode: readMode, text_like: textLike };
            }

            function deckModuleArrayBufferToBase64(buffer) {
                const bytes = new Uint8Array(buffer || new ArrayBuffer(0));
                let binary = '';
                const chunkSize = 0x8000;
                for (let offset = 0; offset < bytes.length; offset += chunkSize) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + chunkSize));
                }
                return btoa(binary);
            }

            async function readDeckModuleSourceAttachment(session, params) {
                if (!session || !session.item) throw new Error('Module session unavailable');
                const sourceEl = deckModuleSessionSourceEl(session);
                const attachmentId = normalizeDeckModuleAttachmentId(
                    params.attachment_id || params.attachmentId || params.id || params.file_id || params.fileId
                );
                if (!attachmentId) throw new Error('Attachment id is required');
                const entry = deckModuleSourceAttachmentEntries(sourceEl).find((candidate) => candidate.attachment_id === attachmentId);
                if (!entry) throw new Error('Attachment is not bound to this source');
                if (!entry.fetch_url) throw new Error('Attachment is not available on this device yet');
                const mode = String(params.mode || 'text').trim().toLowerCase() || 'text';
                if (!['text', 'json', 'base64', 'data_url'].includes(mode)) {
                    throw new Error('Unsupported attachment read mode');
                }
                const manifest = session && session.item ? session.item.manifest : null;
                const policy = deckModuleAttachmentPolicy(entry, mode, manifest);
                if (!policy.ok) throw new Error(policy.reason || 'Attachment cannot be read by this module');
                const response = await fetch(entry.fetch_url, {
                    credentials: 'same-origin',
                    redirect: 'error',
                    headers: { 'Accept': mode === 'json' ? 'application/json, text/plain;q=0.9' : '*/*' },
                });
                if (!response.ok) {
                    throw new Error(response.status === 404 ? 'Attachment is not available on this device yet' : `Attachment read failed (${response.status})`);
                }
                const contentLength = Number(response.headers.get('Content-Length') || '0') || 0;
                const contentType = String(response.headers.get('Content-Type') || entry.type || 'application/octet-stream').split(';')[0].trim().toLowerCase() || 'application/octet-stream';
                if (CANOPY_MODULE_ATTACHMENT_DENIED_MIME_TYPES.has(contentType)) {
                    throw new Error('Attachment MIME type is not readable by modules');
                }
                const responseWasmLike = contentType === 'application/wasm' || contentType === 'application/wasm-module';
                if (responseWasmLike) {
                    if (!deckModuleHasCapability(manifest, 'module.render.wasm')) {
                        throw new Error('module.render.wasm not granted for WebAssembly data');
                    }
                    if (mode !== 'base64' && mode !== 'data_url') {
                        throw new Error('WebAssembly data must be read as base64 or data_url');
                    }
                }
                if (mode === 'text' || mode === 'json') {
                    if (contentLength > CANOPY_MODULE_ATTACHMENT_MAX_TEXT_BYTES) {
                        throw new Error(`Attachment exceeds text read limit (${CANOPY_MODULE_ATTACHMENT_MAX_TEXT_BYTES} bytes)`);
                    }
                    const text = await response.text();
                    if (deckModuleStorageByteLength(text) > CANOPY_MODULE_ATTACHMENT_MAX_TEXT_BYTES) {
                        throw new Error(`Attachment exceeds text read limit (${CANOPY_MODULE_ATTACHMENT_MAX_TEXT_BYTES} bytes)`);
                    }
                    if (mode === 'json') {
                        let jsonValue;
                        try {
                            jsonValue = JSON.parse(text);
                        } catch (_) {
                            throw new Error('Attachment is not valid JSON');
                        }
                        return {
                            attachment_id: attachmentId,
                            name: entry.name,
                            type: contentType,
                            mode: 'json',
                            bytes: deckModuleStorageByteLength(text),
                            json: jsonValue,
                        };
                    }
                    return {
                        attachment_id: attachmentId,
                        name: entry.name,
                        type: contentType,
                        mode: 'text',
                        bytes: deckModuleStorageByteLength(text),
                        text,
                    };
                }
                if (contentLength > CANOPY_MODULE_ATTACHMENT_MAX_BINARY_BYTES) {
                    throw new Error(`Attachment exceeds binary read limit (${CANOPY_MODULE_ATTACHMENT_MAX_BINARY_BYTES} bytes)`);
                }
                const buffer = await response.arrayBuffer();
                if (buffer.byteLength > CANOPY_MODULE_ATTACHMENT_MAX_BINARY_BYTES) {
                    throw new Error(`Attachment exceeds binary read limit (${CANOPY_MODULE_ATTACHMENT_MAX_BINARY_BYTES} bytes)`);
                }
                const base64 = deckModuleArrayBufferToBase64(buffer);
                const result = {
                    attachment_id: attachmentId,
                    name: entry.name,
                    type: contentType,
                    mode,
                    bytes: buffer.byteLength,
                    base64,
                };
                if (mode === 'data_url') {
                    result.data_url = `data:${contentType || 'application/octet-stream'};base64,${base64}`;
                }
                return result;
            }

            function buildDeckModuleSourceSnapshot(sourceEl, options = {}) {
                const includeAttachments = options.includeAttachments === true;
                if (!sourceEl) {
                    return {
                        kind: 'source',
                        source_id: '',
                        subtitle: 'Canopy source',
                        text: '',
                        deck_items: [],
                        attachments: [],
                    };
                }
                const contentNode =
                    sourceEl.querySelector('.message-content, .card-text, .feed-post-content, [data-message-body], .dm-bubble-body')
                    || sourceEl;
                const text = normalizeDeckWidgetText((contentNode && contentNode.textContent) || '', 1800);
                const kind = sourceEl.matches('.post-card[data-post-id]') ? 'post' : 'message';
                const sourceId = String(sourceEl.getAttribute('data-post-id') || sourceEl.getAttribute('data-message-id') || '').trim();
                const deckItems = buildSourceDeckItems(sourceEl, null).map((entry) => ({
                    key: entry.key,
                    type: entry.type,
                    title: entry.title,
                    provider_label: entry.providerLabel || mediaProviderLabel(entry.type),
                })).slice(0, 12);
                return {
                    kind,
                    source_id: sourceId,
                    subtitle: sourceSubtitle(sourceEl),
                    text,
                    deck_items: deckItems,
                    attachments: includeAttachments ? deckModuleSourceAttachmentListForModule(sourceEl) : [],
                };
            }

            function buildDeckModuleMediaSnapshot() {
                if (!(state.current && state.current.el && isDeckMediaItem(state.current))) return null;
                return {
                    type: state.current.type,
                    title: state.current.title || titleFromMedia(state.current.el, state.current.type),
                    subtitle: state.current.subtitle || subtitleFromMedia(state.current.el, state.current.type),
                    is_playing: isElementPlaying(state.current.el, state.current.type),
                };
            }

            function buildDeckModuleContext(item) {
                const manifest = item && item.manifest ? item.manifest : null;
                const sourceEl = firstConnectedDeckAnchor(
                    deckItemSourceEl(item),
                    state.deckOriginSourceEl,
                    state.deckSourceEl
                );
                return {
                    version: 1,
                    title: manifest ? manifest.title : '',
                    subtitle: manifest ? (manifest.subtitle || '') : '',
                    provider_label: manifest ? (manifest.provider_label || '') : '',
                    station_surface: manifest ? (manifest.station_surface || null) : null,
                    source_binding: manifest ? (manifest.source_binding || null) : null,
                    capabilities: getGrantedDeckModuleCapabilities(manifest),
                    source: buildDeckModuleSourceSnapshot(sourceEl, {
                        includeAttachments: !!(
                            manifest
                            && (deckModuleHasCapability(manifest, 'source.read') || deckModuleHasCapability(manifest, 'source.attachments.read'))
                        ),
                    }),
                    media: buildDeckModuleMediaSnapshot(),
                };
            }

            function buildDeckModuleContextForSession(session) {
                const item = session && session.item ? session.item : null;
                const manifest = item && item.manifest ? item.manifest : null;
                const sourceEl = deckModuleSessionSourceEl(session);
                return {
                    session_id: session ? session.id : '',
                    title: item ? (item.title || '') : '',
                    subtitle: manifest ? (manifest.subtitle || '') : '',
                    provider_label: manifest ? (manifest.provider_label || '') : '',
                    station_surface: manifest ? (manifest.station_surface || null) : null,
                    source_binding: manifest ? (manifest.source_binding || null) : null,
                    capabilities: getGrantedDeckModuleCapabilities(manifest),
                    source: buildDeckModuleSourceSnapshot(sourceEl, {
                        includeAttachments: !!(
                            manifest
                            && (deckModuleHasCapability(manifest, 'source.read') || deckModuleHasCapability(manifest, 'source.attachments.read'))
                        ),
                    }),
                    media: buildDeckModuleMediaSnapshot(),
                };
            }

            function moduleRuntimeCacheKey(runtime) {
                if (!runtime) return '';
                return `${runtime.bundle_url || ''}:${runtime.bundle_file_id || ''}:${runtime.format || ''}`;
            }

            function fetchDeckModuleBundle(runtime) {
                const cacheKey = moduleRuntimeCacheKey(runtime);
                if (!cacheKey) return Promise.reject(new Error('Module bundle not configured'));
                if (state.moduleBundleCache.has(cacheKey)) {
                    return state.moduleBundleCache.get(cacheKey);
                }
                const pending = fetch(runtime.bundle_url, {
                    credentials: 'same-origin',
                    headers: { 'Accept': 'text/html, text/plain;q=0.9' },
                }).then(async (response) => {
                    if (!response.ok) {
                        throw new Error(`Bundle request failed (${response.status})`);
                    }
                    const text = await response.text();
                    if (!text || !String(text).trim()) {
                        throw new Error('Module bundle is empty');
                    }
                    if (text.length > 300000) {
                        throw new Error('Module bundle exceeds the v1 size budget');
                    }
                    return text;
                }).catch((error) => {
                    state.moduleBundleCache.delete(cacheKey);
                    throw error;
                });
                state.moduleBundleCache.set(cacheKey, pending);
                return pending;
            }

            function buildDeckModuleBootstrapScript(sessionId, item) {
                const manifest = item && item.manifest ? item.manifest : {};
                const capabilities = getGrantedDeckModuleCapabilities(manifest);
                return `
(function () {
  const sessionId = ${serializeDeckModuleInlineJson(sessionId)};
  const grantedCapabilities = ${serializeDeckModuleInlineJson(capabilities)};
  const webglGranted = grantedCapabilities.indexOf('module.render.webgl') !== -1;
  const wasmGranted = grantedCapabilities.indexOf('module.render.wasm') !== -1;
  const pending = new Map();
  const listeners = new Set();
  let counter = 0;

  function isWebGLContextType(type) {
    const t = String(type || '').trim().toLowerCase();
    return t === 'webgl' || t === 'webgl2' || t === 'experimental-webgl';
  }

  function installWebGLCapabilityGuard() {
    if (webglGranted) return;
    const block = function getContextGuard(original) {
      return function guardedGetContext(type) {
        if (isWebGLContextType(type)) {
          try {
            window.dispatchEvent(new CustomEvent('canopy-module-webgl-denied', { detail: { capability: 'module.render.webgl' } }));
          } catch (_) {}
          return null;
        }
        return original.apply(this, arguments);
      };
    };
    try {
      if (window.HTMLCanvasElement && HTMLCanvasElement.prototype && HTMLCanvasElement.prototype.getContext) {
        const originalCanvasGetContext = HTMLCanvasElement.prototype.getContext;
        Object.defineProperty(HTMLCanvasElement.prototype, 'getContext', {
          configurable: false,
          writable: false,
          value: block(originalCanvasGetContext)
        });
      }
    } catch (_) {}
    try {
      if (window.OffscreenCanvas && OffscreenCanvas.prototype && OffscreenCanvas.prototype.getContext) {
        const originalOffscreenGetContext = OffscreenCanvas.prototype.getContext;
        Object.defineProperty(OffscreenCanvas.prototype, 'getContext', {
          configurable: false,
          writable: false,
          value: block(originalOffscreenGetContext)
        });
      }
    } catch (_) {}
  }

  function installWasmCapabilityGuard() {
    if (wasmGranted) return;
    try {
      if (!window.WebAssembly) return;
      const denied = function wasmDeniedError() {
        try {
          return new WebAssembly.CompileError('module.render.wasm not granted');
        } catch (_) {
          return new Error('module.render.wasm not granted');
        }
      };
      const reject = function deniedWasmPromise() {
        try {
          window.dispatchEvent(new CustomEvent('canopy-module-wasm-denied', { detail: { capability: 'module.render.wasm' } }));
        } catch (_) {}
        return Promise.reject(denied());
      };
      ['compile', 'instantiate', 'compileStreaming', 'instantiateStreaming'].forEach((name) => {
        try {
          if (typeof WebAssembly[name] === 'function') {
            Object.defineProperty(WebAssembly, name, {
              configurable: false,
              writable: false,
              value: reject
            });
          }
        } catch (_) {}
      });
      try {
        if (typeof WebAssembly.Module === 'function') {
          Object.defineProperty(WebAssembly, 'Module', {
            configurable: false,
            writable: false,
            value: function DeniedWebAssemblyModule() {
              throw denied();
            }
          });
        }
      } catch (_) {}
    } catch (_) {}
  }

  installWebGLCapabilityGuard();
  installWasmCapabilityGuard();

  function emit(type, payload) {
    parent.postMessage({ canopyModule: true, sessionId, type, payload: payload || {} }, '*');
  }

  function request(method, payload) {
    return new Promise((resolve, reject) => {
      const id = 'req-' + (++counter);
      pending.set(id, { resolve, reject });
      emit('request', { id, method, payload: payload || {} });
      window.setTimeout(() => {
        if (!pending.has(id)) return;
        pending.delete(id);
        reject(new Error('Module request timed out'));
      }, 8000);
    });
  }

  window.addEventListener('message', (event) => {
    const msg = event && event.data ? event.data : {};
    if (!msg || msg.canopyModule !== true || msg.sessionId !== sessionId) return;
    if (msg.type === 'response' && msg.payload && msg.payload.id) {
      const entry = pending.get(msg.payload.id);
      if (!entry) return;
      pending.delete(msg.payload.id);
      if (msg.payload.ok === false) {
        entry.reject(new Error((msg.payload.error && msg.payload.error.message) || 'Module request failed'));
      } else {
        entry.resolve(msg.payload.result);
      }
      return;
    }
    if (msg.type === 'context') {
      listeners.forEach((listener) => {
        try { listener(msg.payload || null); } catch (_) {}
      });
    }
  });

  function storagePayload(key, options) {
    const payload = Object.assign({}, options || {});
    payload.key = key;
    return payload;
  }

  const storageApi = Object.freeze({
    get(key, options) {
      return request('module.storage.get', storagePayload(key, options)).then((result) => result ? result.value : null);
    },
    set(key, value, options) {
      const payload = storagePayload(key, options);
      payload.value = value;
      return request('module.storage.set', payload);
    },
    remove(key, options) {
      return request('module.storage.remove', storagePayload(key, options));
    },
    keys(options) {
      return request('module.storage.keys', options || {}).then((result) => result && Array.isArray(result.keys) ? result.keys : []);
    },
    info(options) {
      return request('module.storage.info', options || {});
    },
    clear(options) {
      const payload = Object.assign({}, options || {});
      payload.confirm = true;
      return request('module.storage.clear', payload);
    },
    getJson(key, fallbackValue, options) {
      return this.get(key, options).then((value) => {
        if (value == null || value === '') return fallbackValue;
        try { return JSON.parse(value); } catch (_) { return fallbackValue; }
      });
    },
    setJson(key, value, options) {
      return this.set(key, JSON.stringify(value), options);
    }
  });

  const attachmentsApi = Object.freeze({
    list() {
      return request('source.attachments.list', {});
    },
    read(attachmentId, options) {
      const payload = Object.assign({}, options || {});
      payload.attachment_id = attachmentId;
      return request('source.attachments.read', payload);
    },
    readText(attachmentId, options) {
      return this.read(attachmentId, Object.assign({}, options || {}, { mode: 'text' })).then((result) => result ? result.text : '');
    },
    readJson(attachmentId, options) {
      return this.read(attachmentId, Object.assign({}, options || {}, { mode: 'json' })).then((result) => result ? result.json : null);
    },
    readBase64(attachmentId, options) {
      return this.read(attachmentId, Object.assign({}, options || {}, { mode: 'base64' }));
    },
    readDataUrl(attachmentId, options) {
      return this.read(attachmentId, Object.assign({}, options || {}, { mode: 'data_url' })).then((result) => result ? result.data_url : '');
    }
  });

  window.CanopyModule = Object.freeze({
    version: 1,
    sessionId,
    capabilities: grantedCapabilities.slice(),
    source: Object.freeze({
      attachments: attachmentsApi
    }),
    render: Object.freeze({
      webgl: Object.freeze({ enabled: webglGranted, capability: 'module.render.webgl' }),
      wasm: Object.freeze({ enabled: wasmGranted, capability: 'module.render.wasm' })
    }),
    storage: storageApi,
    request(method, payload) {
      return request(method, payload);
    },
    perform(method, payload) {
      return request(method, payload);
    },
    getContext() {
      return request('context.get', {});
    },
    onContext(listener) {
      if (typeof listener !== 'function') return () => {};
      listeners.add(listener);
      return () => listeners.delete(listener);
    }
  });

  emit('runtime.ready', {
    href: String(window.location.href || ''),
    title: String(document.title || '')
  });
})();`;
            }

            function injectDeckModuleRuntime(bundleHtml, bootstrapJs, manifest) {
                const shellTitle = escapeEmbedHtml((manifest && manifest.title) || 'Canopy Module');
                const scriptSrc = deckModuleHasCapability(manifest, 'module.render.wasm')
                    ? "'unsafe-inline' 'wasm-unsafe-eval'"
                    : "'unsafe-inline'";
                const csp = `default-src 'none'; img-src data: blob:; media-src data: blob:; style-src 'unsafe-inline'; script-src ${scriptSrc}; connect-src 'none'; font-src data:; frame-src 'none'; worker-src 'none'; child-src 'none'; form-action 'none'; base-uri 'none';`;
                const bootstrapTag = `<script>${bootstrapJs.replace(/<\/script/gi, '<\\\\/script')}<\/script>`;
                /* Let module UIs use height:100% / flex-fill inside the deck iframe (avoids a short document box). */
                const moduleShellBaseStyle =
                    '<style data-canopy-module-shell="1">' +
                    'html,body{min-height:100%;height:100%;margin:0;box-sizing:border-box}' +
                    '</style>';
                const headInjection =
                    `<meta charset="utf-8">` +
                    `<meta name="viewport" content="width=device-width, initial-scale=1">` +
                    `<meta http-equiv="Content-Security-Policy" content="${escapeEmbedAttr(csp)}">` +
                    `<title>${shellTitle}</title>` +
                    moduleShellBaseStyle +
                    bootstrapTag;
                const rawHtml = String(bundleHtml || '');
                if (/<head[\s>]/i.test(rawHtml)) {
                    return rawHtml.replace(/<head([^>]*)>/i, `<head$1>${headInjection}`);
                }
                if (/<html[\s>]/i.test(rawHtml)) {
                    return rawHtml.replace(/<html([^>]*)>/i, `<html$1><head>${headInjection}</head>`);
                }
                return `<!doctype html><html><head>${headInjection}</head><body>${rawHtml}</body></html>`;
            }

            function teardownDeckModuleSessionsInNode(node) {
                if (!node || !node.querySelectorAll) return;
                node.querySelectorAll('[data-canopy-module-session-id]').forEach((frame) => {
                    const sessionId = String(frame.getAttribute('data-canopy-module-session-id') || '').trim();
                    if (sessionId) {
                        state.moduleSessions.delete(sessionId);
                    }
                });
            }

            function postDeckModuleSessionMessage(session, type, payload) {
                if (!(session && session.frame && session.frame.contentWindow)) return;
                session.frame.contentWindow.postMessage({
                    canopyModule: true,
                    sessionId: session.id,
                    type,
                    payload: payload || {},
                }, '*');
            }

            function postDeckModuleContext(session) {
                postDeckModuleSessionMessage(session, 'context', buildDeckModuleContextForSession(session));
            }

            async function respondDeckModuleRequest(session, payload) {
                const id = payload && payload.id ? String(payload.id) : '';
                const method = String(payload && payload.method || '').trim().toLowerCase();
                const params = payload && payload.payload && typeof payload.payload === 'object' ? payload.payload : {};
                const manifest = session && session.item ? session.item.manifest : null;
                const respond = (ok, result, errorMessage) => {
                    postDeckModuleSessionMessage(session, 'response', {
                        id,
                        ok,
                        result: ok ? (result == null ? null : result) : null,
                        error: ok ? null : { message: errorMessage || 'Module request failed' },
                    });
                };
                if (!id || !method || !manifest) {
                    respond(false, null, 'Invalid module request');
                    return;
                }
                try {
                    if (method === 'context.get') {
                        respond(true, buildDeckModuleContextForSession(session), '');
                        return;
                    }
                    if (method === 'source.snapshot') {
                        if (!deckModuleHasCapability(manifest, 'source.read') && !deckModuleHasCapability(manifest, 'source.snapshot')) {
                            respond(false, null, 'source.snapshot not granted');
                            return;
                        }
                        respond(true, buildDeckModuleContextForSession(session).source, '');
                        return;
                    }
                    if (method === 'source.attachments.list') {
                        if (!deckModuleHasCapability(manifest, 'source.read') && !deckModuleHasCapability(manifest, 'source.attachments.read')) {
                            respond(false, null, 'source attachment access not granted');
                            return;
                        }
                        const sourceEl = deckModuleSessionSourceEl(session);
                        respond(true, {
                            attachments: deckModuleSourceAttachmentListForModule(sourceEl),
                        }, '');
                        return;
                    }
                    if (method === 'source.attachments.read' || method === 'source.attachment.read') {
                        if (!deckModuleHasCapability(manifest, 'source.attachments.read')) {
                            respond(false, null, 'source.attachments.read not granted');
                            return;
                        }
                        respond(true, await readDeckModuleSourceAttachment(session, params), '');
                        return;
                    }
                    if (method === 'deck.media.get_state') {
                        if (!deckModuleHasCapability(manifest, 'deck.media.observe')) {
                            respond(false, null, 'deck.media.observe not granted');
                            return;
                        }
                        respond(true, buildDeckModuleMediaSnapshot(), '');
                        return;
                    }
                    if (method === 'deck.return') {
                        if (!deckModuleHasCapability(manifest, 'deck.return')) {
                            respond(false, null, 'deck.return not granted');
                            return;
                        }
                        jumpToDeckItemSource(session.item, { forceClose: true });
                        respond(true, { returned: true }, '');
                        return;
                    }
                    if (method === 'deck.close') {
                        if (!deckModuleHasCapability(manifest, 'deck.close')) {
                            respond(false, null, 'deck.close not granted');
                            return;
                        }
                        closeMediaDeck({ forceClose: true });
                        respond(true, { closed: true }, '');
                        return;
                    }
                    if (method === 'clipboard.write') {
                        if (!deckModuleHasCapability(manifest, 'clipboard.write')) {
                            respond(false, null, 'clipboard.write not granted');
                            return;
                        }
                        const text = normalizeDeckWidgetText(params.text, 2000);
                        if (!text) {
                            respond(false, null, 'No clipboard text provided');
                            return;
                        }
                        await navigator.clipboard.writeText(text);
                        respond(true, { written: true }, '');
                        return;
                    }
                    if (method === 'module.storage.get') {
                        const capability = deckModuleStorageCapabilityForScope(params.scope);
                        if (!deckModuleHasCapability(manifest, capability)) {
                            respond(false, null, `${capability} not granted`);
                            return;
                        }
                        respond(true, deckModuleStorageGet(session, params.key, params.scope), '');
                        return;
                    }
                    if (method === 'module.storage.set') {
                        const capability = deckModuleStorageCapabilityForScope(params.scope);
                        if (!deckModuleHasCapability(manifest, capability)) {
                            respond(false, null, `${capability} not granted`);
                            return;
                        }
                        respond(true, deckModuleStorageSet(session, params.key, params.value, params.scope), '');
                        return;
                    }
                    if (method === 'module.storage.remove') {
                        const capability = deckModuleStorageCapabilityForScope(params.scope);
                        if (!deckModuleHasCapability(manifest, capability)) {
                            respond(false, null, `${capability} not granted`);
                            return;
                        }
                        respond(true, deckModuleStorageRemove(session, params.key, params.scope), '');
                        return;
                    }
                    if (method === 'module.storage.keys') {
                        const capability = deckModuleStorageCapabilityForScope(params.scope);
                        if (!deckModuleHasCapability(manifest, capability)) {
                            respond(false, null, `${capability} not granted`);
                            return;
                        }
                        respond(true, deckModuleStorageKeys(session, params.scope), '');
                        return;
                    }
                    if (method === 'module.storage.info') {
                        const capability = deckModuleStorageCapabilityForScope(params.scope);
                        if (!deckModuleHasCapability(manifest, capability)) {
                            respond(false, null, `${capability} not granted`);
                            return;
                        }
                        respond(true, deckModuleStorageInfo(session, params.scope), '');
                        return;
                    }
                    if (method === 'module.storage.clear') {
                        const capability = deckModuleStorageCapabilityForScope(params.scope);
                        if (!deckModuleHasCapability(manifest, capability)) {
                            respond(false, null, `${capability} not granted`);
                            return;
                        }
                        if (params.confirm !== true) {
                            respond(false, null, 'module.storage.clear requires confirm: true');
                            return;
                        }
                        respond(true, deckModuleStorageClear(session, params.scope), '');
                        return;
                    }
                    respond(false, null, `Unsupported module method: ${method}`);
                } catch (error) {
                    respond(false, null, error && error.message ? error.message : 'Module request failed');
                }
            }

            window.addEventListener('message', (event) => {
                const msg = event && event.data ? event.data : {};
                if (!msg || msg.canopyModule !== true) return;
                const sessionId = String(msg.sessionId || '').trim();
                if (!sessionId || !state.moduleSessions.has(sessionId)) return;
                const session = state.moduleSessions.get(sessionId);
                if (!session || !session.frame || event.source !== session.frame.contentWindow) return;
                if (msg.type === 'runtime.ready') {
                    postDeckModuleContext(session);
                    return;
                }
                if (msg.type === 'request') {
                    respondDeckModuleRequest(session, msg.payload || {});
                }
            });

            function deckItemContextSubtitle(item) {
                const sourceEl = firstConnectedDeckAnchor(
                    deckItemSourceEl(item),
                    state.deckOriginSourceEl,
                    state.deckSourceEl
                );
                return sourceEl ? sourceSubtitle(sourceEl) : 'Canopy source';
            }

            function widgetIframeAllow(manifest) {
                const widgetType = String((manifest && manifest.widget_type) || '').toLowerCase();
                if (widgetType === 'map') return 'geolocation';
                if (widgetType === 'chart') return 'clipboard-write';
                return 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture';
            }

            /** Spotify/SoundCloud deck iframes match in-feed previews (no sandbox) so embeds can initialize. */
            function deckWidgetIframeSandboxValue(embedUrl) {
                try {
                    const u = new URL(String(embedUrl || ''), window.location.origin);
                    const host = u.hostname.toLowerCase();
                    if (host === 'open.spotify.com' || host === 'w.soundcloud.com') {
                        return '';
                    }
                } catch (_) {
                    /* fall through */
                }
                return 'allow-scripts allow-same-origin allow-popups allow-popups-to-escape-sandbox allow-presentation';
            }

            function deckWidgetStageSignature(item) {
                if (!item || !item.manifest) return '';
                const m = item.manifest;
                if (m.render_mode === 'module_runtime' && m.module_runtime) {
                    const requested = getRequestedDeckModuleCapabilities(m).join(',');
                    const granted = getGrantedDeckModuleCapabilities(m).join(',');
                    return `module:${String(m.module_runtime.bundle_url || '')}:${String(m.key || '')}:${requested}:${granted}`;
                }
                if (m.render_mode === 'iframe' && m.embed_url) {
                    return `iframe:${String(m.embed_url)}`;
                }
                const body = String(m.body_text || m.subtitle || '');
                return `panel:${String(item.key || '')}:${String(m.title || '')}:${body.slice(0, 200)}`;
            }

            function jumpToDeckItemSource(item, options = {}) {
                const forceClose = options.forceClose === true;
                const sourceEl = firstConnectedDeckAnchor(
                    deckItemSourceEl(item),
                    state.deckOriginSourceEl,
                    state.deckSourceEl
                );
                if (forceClose && state.deckOpen) {
                    closeMediaDeck({ forceClose: true });
                }
                if (sourceEl && sourceEl.isConnected) {
                    sourceEl.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
                    applyFocusFlash(sourceEl);
                }
            }

            function canRunDeckWidgetAction(action, manifest) {
                if (!action || !manifest) return false;
                const policy = manifest.action_policy || {};
                const maxRisk = String(policy.max_risk || 'view').toLowerCase();
                if (maxRisk === 'view' && action.risk !== 'view') return false;
                return true;
            }

            async function runDeckWidgetAction(action, item) {
                if (!action || !item) return;
                const manifest = item.manifest || {};
                if (!canRunDeckWidgetAction(action, manifest)) {
                    if (typeof showAlert === 'function') showAlert('This action is outside the bounded policy for this deck item.', 'warning');
                    return;
                }
                if (action.requires_confirmation === true) {
                    const confirmed = window.confirm(`Run "${action.label}" from this deck item?`);
                    if (!confirmed) return;
                }
                if (action.kind === 'external_link' && action.url) {
                    window.open(action.url, '_blank', 'noopener');
                    return;
                }
                if (action.kind === 'clipboard' && action.text) {
                    try {
                        await navigator.clipboard.writeText(action.text);
                        if (typeof showAlert === 'function') showAlert('Copied to clipboard', 'success');
                    } catch (_) {
                        if (typeof showAlert === 'function') showAlert('Clipboard write failed', 'warning');
                    }
                    return;
                }
                if (action.kind === 'callback' && action.handler === 'open_stream_workspace') {
                    const args = action.args || {};
                    if (typeof window.openStreamAttachmentPlayer !== 'function') {
                        if (typeof showAlert === 'function') showAlert('Stream workspace is not available on this surface.', 'info');
                        return;
                    }
                    try {
                        await window.openStreamAttachmentPlayer(args.streamId, args.mediaKind, args.slotId, args.streamKind || 'media');
                        jumpToDeckItemSource(item, { forceClose: true });
                    } catch (_) {
                        if (typeof showAlert === 'function') showAlert('Could not open the stream workspace.', 'warning');
                    }
                }
            }

            function renderDeckStationSummary(manifest) {
                clearDeckStationSummary();
                if (!manifest) return;
                const station = manifest.station_surface || null;
                if (!station) return;
                const policy = manifest.action_policy || {};
                const maxRisk = String(policy.max_risk || 'view').toLowerCase();
                const humanGate = String(policy.human_gate || 'none').toLowerCase();
                const isSimpleReferenceSurface =
                    station.kind === 'reference_surface'
                    && station.recurring !== true
                    && station.scope !== 'station'
                    && maxRisk === 'view'
                    && humanGate === 'none';
                if (isSimpleReferenceSurface) return;
                setDeckStationSummaryHidden(false);
                if (deckStationPolicy) {
                    deckStationPolicy.textContent = (policy.audit_label || 'Bounded actions');
                }
                if (deckStationTitle) {
                    deckStationTitle.textContent = station.label || manifest.provider_label || manifest.title || 'Station surface';
                }
                if (deckStationSubtitle) {
                    deckStationSubtitle.textContent = station.summary || deckItemContextSubtitle({ manifest });
                }
                if (deckStationBadges) {
                    deckStationBadges.innerHTML = '';
                    [
                        station.domain || '',
                        station.recurring ? 'Recurring station' : 'Source-bound',
                        station.scope === 'station' ? 'Station-scoped' : 'Source-scoped',
                        maxRisk === 'low' ? 'Low-risk actions' : 'View-only',
                        humanGate !== 'none' ? `Human gate: ${humanGate}` : '',
                    ].filter(Boolean).forEach((label) => {
                        const badge = document.createElement('span');
                        badge.className = 'sidebar-media-deck-station-badge';
                        badge.textContent = label;
                        deckStationBadges.appendChild(badge);
                    });
                }
            }

            function renderDeckWidgetSummary(item) {
                clearDeckStationSummary();
                clearDeckWidgetSummary();
                if (!item || isDeckMediaItem(item) || !item.manifest) return;
                const manifest = item.manifest;
                renderDeckStationSummary(manifest);
                setDeckWidgetSummaryHidden(false);

                if (deckWidgetBadges) {
                    const badges = manifest.badges || [];
                    deckWidgetBadges.innerHTML = '';
                    badges.forEach((badgeText) => {
                        const badge = document.createElement('span');
                        badge.className = 'sidebar-media-deck-widget-badge';
                        badge.textContent = badgeText;
                        deckWidgetBadges.appendChild(badge);
                    });
                    if (!deckWidgetBadges.childElementCount) {
                        const badge = document.createElement('span');
                        badge.className = 'sidebar-media-deck-widget-badge';
                        badge.textContent = manifest.provider_label || 'Widget';
                        deckWidgetBadges.appendChild(badge);
                    }
                }

                if (deckWidgetDetails) {
                    const detailEntries = Array.isArray(manifest.details) ? [...manifest.details] : [];
                    if (manifest.render_mode === 'module_runtime' && manifest.module_runtime) {
                        const requestedCaps = getRequestedDeckModuleCapabilities(manifest);
                        const caps = getGrantedDeckModuleCapabilities(manifest);
                        const pendingCaps = getPendingDeckModuleCapabilities(manifest);
                        if (pendingCaps.length) {
                            detailEntries.unshift({
                                label: 'Review needed',
                                value: pendingCaps.map(humanizeDeckModuleCapability).join(', '),
                            });
                        }
                        if (caps.length) {
                            detailEntries.unshift({
                                label: 'Capabilities',
                                value: caps.join(', '),
                            });
                        } else if (requestedCaps.length) {
                            detailEntries.unshift({
                                label: 'Capabilities',
                                value: 'None granted yet',
                            });
                        }
                        detailEntries.push({
                            label: 'Runtime',
                            value: manifest.module_runtime.runtime_label || 'Canopy Module',
                        });
                        detailEntries.push({
                            label: 'Bundle',
                            value: manifest.module_runtime.format === 'single_html' ? 'Single HTML bundle' : manifest.module_runtime.format,
                        });
                    }
                    deckWidgetDetails.innerHTML = '';
                    detailEntries.slice(0, 8).forEach((entry) => {
                        const block = document.createElement('div');
                        block.className = 'sidebar-media-deck-widget-kv';
                        block.innerHTML =
                            `<div class="sidebar-media-deck-widget-kv-label">${escapeEmbedHtml(entry.label)}</div>` +
                            `<div class="sidebar-media-deck-widget-kv-value">${escapeEmbedHtml(entry.value)}</div>`;
                        deckWidgetDetails.appendChild(block);
                    });
                }

                if (deckWidgetActions) {
                    deckWidgetActions.innerHTML = '';
                    const actions = Array.isArray(manifest.actions) ? manifest.actions : [];
                    actions.forEach((action) => {
                        const btn = document.createElement('button');
                        btn.type = 'button';
                        btn.className = 'sidebar-media-deck-btn';
                        btn.innerHTML = `${action.icon ? `<i class="bi ${escapeEmbedAttr(action.icon)}"></i>` : ''}<span>${escapeEmbedHtml(action.label)}</span>`;
                        btn.title = `${action.scope === 'station' ? 'Station' : 'Source'} action · ${action.risk === 'low' ? 'low risk' : 'view only'}`;
                        btn.addEventListener('click', () => { runDeckWidgetAction(action, item); });
                        deckWidgetActions.appendChild(btn);
                    });
                }
            }

            function renderDeckModuleCapabilityApprovalPanel(host, item, pendingCapabilities) {
                const manifest = item && item.manifest ? item.manifest : {};
                const pending = Array.isArray(pendingCapabilities) ? pendingCapabilities : [];
                const readableCaps = pending.map(humanizeDeckModuleCapability).join(', ');
                const hasWebGL = pending.includes('module.render.webgl');
                const hasWasm = pending.includes('module.render.wasm');
                const hasAttachmentRead = pending.includes('source.attachments.read');
                const visualOnly = (hasWebGL || hasWasm) && !hasAttachmentRead;
                const title = hasAttachmentRead && !hasWebGL && !hasWasm
                    ? 'Source data review required'
                    : (visualOnly ? 'Rendering capability review required' : 'Module capability review required');
                const copyParts = [];
                if (hasAttachmentRead) {
                    copyParts.push('Source data access is read-only and limited to attachments already bound to this source. The host validates the attachment id and returns capped text, JSON, or base64 data through the broker.');
                }
                if (hasWebGL) {
                    copyParts.push('WebGL enables GPU-backed canvas rendering inside the sandbox.');
                }
                if (hasWasm) {
                    copyParts.push('WebAssembly enables browser WASM compilation and instantiation through the narrow CSP token wasm-unsafe-eval; it does not grant JavaScript eval().');
                }
                copyParts.push('No raw network, API, storage, credentials, or same-origin access is granted.');
                const copy = copyParts.join(' ');
                const icon = hasAttachmentRead ? 'bi-file-earmark-arrow-down' : (hasWasm ? 'bi-cpu' : 'bi-gpu-card');
                const label = hasAttachmentRead && !hasWebGL && !hasWasm
                    ? 'Allow source data this session'
                    : (hasWebGL && !hasAttachmentRead && !hasWasm ? 'Enable WebGL this session' : (hasWasm && !hasAttachmentRead && !hasWebGL ? 'Enable WASM this session' : 'Allow capabilities this session'));
                host.innerHTML = `
                    <div class="sidebar-media-deck-widget-panel sidebar-media-deck-module-panel">
                        <div class="sidebar-media-deck-widget-panel-title">${escapeEmbedHtml(title)}</div>
                        <div class="sidebar-media-deck-widget-panel-copy">
                            This module requests ${escapeEmbedHtml(readableCaps || 'an elevated capability')}. ${escapeEmbedHtml(copy)}
                        </div>
                        <div class="sidebar-media-deck-widget-actions mt-3">
                            <button type="button" class="sidebar-media-deck-btn" data-canopy-approve-capability="1">
                                <i class="bi ${escapeEmbedAttr(icon)}"></i><span>${escapeEmbedHtml(label)}</span>
                            </button>
                        </div>
                    </div>
                `;
                const approveBtn = host.querySelector('[data-canopy-approve-capability="1"]');
                if (approveBtn) {
                    approveBtn.addEventListener('click', () => {
                        pending.forEach((capability) => {
                            approveDeckModuleCapability(manifest, capability);
                        });
                        renderDeckWidgetSummary(item);
                        renderDeckWidgetStage(item);
                    });
                }
            }

            function renderDeckWidgetStage(item) {
                if (!deckStage || !item || !item.manifest) return;
                const manifest = item.manifest;
                const nextSig = deckWidgetStageSignature(item);
                const existingHost = deckStage.querySelector(':scope > .sidebar-media-deck-widget-stage');
                if (existingHost && nextSig && existingHost.dataset.canopyDeckWidgetSig === nextSig) {
                    deckStage.classList.remove('is-empty');
                    if (deckVisual) deckVisual.hidden = true;
                    return;
                }

                clearDeckStageDockedNodes();
                deckStage.classList.remove('is-empty');
                if (deckVisual) deckVisual.hidden = true;

                const host = document.createElement('div');
                host.className = 'sidebar-media-deck-widget-stage';
                host.dataset.canopyDeckWidgetSig = nextSig;

                if (manifest.render_mode === 'module_runtime' && manifest.module_runtime) {
                    host.innerHTML = `
                        <div class="sidebar-media-deck-widget-panel sidebar-media-deck-module-panel">
                            <div class="sidebar-media-deck-widget-panel-title">Loading module</div>
                            <div class="sidebar-media-deck-widget-panel-copy">Preparing the sandboxed runtime for this source-bound module.</div>
                        </div>
                    `;
                    deckStage.appendChild(host);
                    fetchDeckModuleBundle(manifest.module_runtime).then((bundleHtml) => {
                        if (!host.isConnected || host.dataset.canopyDeckWidgetSig !== nextSig) return;
                        const moduleItem = applyDeckModuleBundleDeclarations(item, bundleHtml);
                        const moduleManifest = moduleItem && moduleItem.manifest ? moduleItem.manifest : manifest;
                        const pendingCapabilities = getPendingDeckModuleCapabilities(moduleManifest);
                        if (pendingCapabilities.length) {
                            renderDeckWidgetSummary(moduleItem);
                            renderDeckModuleCapabilityApprovalPanel(host, moduleItem, pendingCapabilities);
                            return;
                        }
                        state.moduleSessionCounter += 1;
                        const sessionId = `canopy-module-session-${state.moduleSessionCounter}`;
                        const iframe = document.createElement('iframe');
                        iframe.className = 'sidebar-media-deck-widget-frame sidebar-media-deck-module-frame';
                        iframe.title = moduleManifest.title || 'Canopy Module';
                        iframe.loading = 'lazy';
                        iframe.setAttribute('sandbox', 'allow-scripts');
                        iframe.setAttribute('data-canopy-module-session-id', sessionId);
                        iframe.srcdoc = injectDeckModuleRuntime(
                            bundleHtml,
                            buildDeckModuleBootstrapScript(sessionId, moduleItem),
                            moduleManifest
                        );
                        state.moduleSessions.set(sessionId, {
                            id: sessionId,
                            item: moduleItem,
                            frame: iframe,
                            source_message_id: deckModuleSourceIdFromElement(moduleItem && moduleItem.sourceEl ? moduleItem.sourceEl : null, 'data-message-id'),
                            source_post_id: deckModuleSourceIdFromElement(moduleItem && moduleItem.sourceEl ? moduleItem.sourceEl : null, 'data-post-id'),
                        });
                        renderDeckWidgetSummary(moduleItem);
                        host.innerHTML = '';
                        host.appendChild(iframe);
                    }).catch((error) => {
                        if (!host.isConnected || host.dataset.canopyDeckWidgetSig !== nextSig) return;
                        host.innerHTML = `
                            <div class="sidebar-media-deck-widget-panel sidebar-media-deck-module-panel">
                                <div class="sidebar-media-deck-widget-panel-title">Module unavailable</div>
                                <div class="sidebar-media-deck-widget-panel-copy">${escapeEmbedHtml((error && error.message) || 'Could not load the module bundle.')}</div>
                            </div>
                        `;
                    });
                } else if (manifest.render_mode === 'iframe' && manifest.embed_url) {
                    const iframe = document.createElement('iframe');
                    iframe.className = 'sidebar-media-deck-widget-frame';
                    iframe.src = manifest.embed_url;
                    iframe.title = manifest.title || 'Deck widget';
                    iframe.loading = 'lazy';
                    iframe.referrerPolicy = 'strict-origin-when-cross-origin';
                    iframe.allow = widgetIframeAllow(manifest);
                    const sandboxVal = deckWidgetIframeSandboxValue(manifest.embed_url);
                    if (sandboxVal) {
                        iframe.setAttribute('sandbox', sandboxVal);
                    }
                    host.appendChild(iframe);
                } else {
                    const panel = document.createElement('div');
                    panel.className = 'sidebar-media-deck-widget-panel';
                    panel.innerHTML =
                        `<div class="sidebar-media-deck-widget-panel-title">${escapeEmbedHtml(manifest.title)}</div>` +
                        `<div class="sidebar-media-deck-widget-panel-copy">${escapeEmbedHtml(manifest.subtitle || manifest.body_text || deckItemContextSubtitle(item))}</div>`;
                    if (manifest.body_text && manifest.body_text !== manifest.subtitle) {
                        const copy = document.createElement('div');
                        copy.className = 'sidebar-media-deck-widget-panel-copy';
                        copy.textContent = manifest.body_text;
                        panel.appendChild(copy);
                    }
                    host.appendChild(panel);
                }
                deckStage.appendChild(host);
            }

            function renderDeckQueue() {
                if (!deckQueue || !deckQueueCount || !deckCount || !deckCountChip || !deckSource) return;
                refreshDeckOriginSourceElIfStale();
                const previousDeckItems = Array.isArray(state.deckItems) ? state.deckItems : [];
                if (!previousDeckItems.length || state.deckQueueNeedsRefresh) {
                    const current = state.current;
                    const selectedNow = getDeckSelectedItem();
                    const sourceEl = firstConnectedDeckAnchor(
                        state.deckOriginSourceEl,
                        selectedNow && deckItemSourceEl(selectedNow),
                        state.deckSourceEl,
                        current && current.sourceEl
                    );
                    state.deckSourceEl = sourceEl || (state.deckSourceEl && state.deckSourceEl.isConnected ? state.deckSourceEl : null);
                    const anchorForExplicit = sourceEl || state.deckOriginSourceEl || state.deckSourceEl;
                    const explicitSelectedWidget = (
                        selectedNow
                        && !isDeckMediaItem(selectedNow)
                        && selectedNow.manifest
                        && selectedNow.el
                        && selectedNow.el.isConnected
                        && widgetDeckOriginContainsEl(anchorForExplicit, selectedNow.el)
                    ) ? buildDeckWidgetItem(selectedNow.el, selectedNow.manifest, anchorForExplicit) : null;
                    const built = mergeExplicitDeckItem(
                        buildSourceDeckItems(sourceEl, current ? current.el : null),
                        explicitSelectedWidget
                    );
                    const originForReconcile = firstConnectedDeckAnchor(
                        state.deckOriginSourceEl,
                        sourceEl,
                        state.deckSourceEl
                    );
                    state.deckItems = reconcileDeckQueueItemsBuilt(built, previousDeckItems, originForReconcile);
                    state.deckQueueNeedsRefresh = false;
                }
                const items = state.deckItems;
                const selectedItem = getDeckSelectedItem();
                if (selectedItem && state.deckSelectedKey && selectedItem.key !== state.deckSelectedKey) {
                    state.deckSelectedKey = selectedItem.key;
                }
                const total = items.length;
                const label = total === 1 ? '1 item' : `${total} items`;
                const activeKey = selectedItem ? selectedItem.key : '';
                const nextSignature = `${activeKey}::${items.map((item) => `${item.key}:${item.type}`).join('|')}`;
                deckQueueCount.textContent = label;
                deckCountChip.textContent = label;
                if (deckChipLabel) {
                    deckChipLabel.textContent = `Canopy Deck · ${label}`;
                }
                if (deckCountChip) {
                    deckCountChip.hidden = true;
                }
                if (deckQueueCount) {
                    deckQueueCount.hidden = true;
                }
                if (deckQueueToggle) {
                    deckQueueToggle.hidden = total <= 1;
                }
                if (total <= 1) {
                    deckCount.textContent = '';
                    deckCount.hidden = true;
                } else {
                    deckCount.hidden = false;
                    const idx = items.findIndex((row) => selectedItem && row.key === selectedItem.key);
                    deckCount.textContent = idx >= 0 ? `Item ${idx + 1} of ${total}` : `${total} in queue`;
                }
                deckSource.textContent = selectedItem ? deckItemContextSubtitle(selectedItem) : 'Now playing from Canopy';

                const renderedQueueItems = deckQueue.querySelectorAll('.sidebar-media-deck-item').length;
                if (state.deckQueueSignature === nextSignature && deckQueue.childElementCount && renderedQueueItems === items.length) {
                    return;
                }
                state.deckQueueSignature = nextSignature;

                deckQueue.innerHTML = '';
                if (!items.length) {
                    const empty = document.createElement('div');
                    empty.className = 'sidebar-media-deck-empty';
                    empty.textContent = 'Multiple videos or clips in the same post/message will appear here.';
                    deckQueue.appendChild(empty);
                    return;
                }

                items.forEach((item, index) => {
                    if (item && item.el && isDeckMediaItem(item)) {
                        item.title = preferredMediaTitle(item.el, item.type, item.title, { allowLookup: false });
                    }
                    const btn = document.createElement('button');
                    btn.type = 'button';
                    btn.className = 'sidebar-media-deck-item' + (selectedItem && selectedItem.key === item.key ? ' is-active' : '');
                    btn.dataset.mediaKey = item.key;
                    btn.dataset.mediaIndex = String(index);

                    const thumb = document.createElement('div');
                    thumb.className = 'sidebar-media-deck-item-thumb';
                    if (item.thumb) {
                        const img = document.createElement('img');
                        img.src = item.thumb;
                        img.alt = '';
                        img.loading = 'lazy';
                        thumb.appendChild(img);
                    } else {
                        thumb.innerHTML = `<i class="bi ${mediaIcon(item.type)}"></i>`;
                    }

                    const labelWrap = document.createElement('div');
                    labelWrap.className = 'sidebar-media-deck-item-label';

                    const title = document.createElement('div');
                    title.className = 'sidebar-media-deck-item-title';
                    title.textContent = item.title;

                    const meta = document.createElement('div');
                    meta.className = 'sidebar-media-deck-item-meta';
                    meta.textContent = item.providerLabel || mediaProviderLabel(item.type);

                    labelWrap.appendChild(title);
                    labelWrap.appendChild(meta);
                    btn.appendChild(thumb);
                    btn.appendChild(labelWrap);
                    deckQueue.appendChild(btn);
                });
                scrollDeckSelectionIntoView();
            }

            function sourceDeckHasRenderableState() {
                return !!(state.deckOpen && (state.current || getDeckSelectedItem()));
            }

            function deckInboxMeshspaceLabel() {
                const mesh = (window.CANOPY_VARS && window.CANOPY_VARS.currentMeshspace) || {};
                return String(mesh.name || '').trim() || 'Current meshspace';
            }

            function updateDeckModeChrome() {
                if (!deck) return;
                const mode = state.deckShellMode === 'dm' ? 'dm' : 'source';
                const sourceAvailable = sourceDeckHasRenderableState();
                const showTabs = !!(state.deckInboxActive && sourceAvailable);
                deck.setAttribute('data-deck-mode', mode);
                deck.classList.toggle('is-dm-mode', mode === 'dm');
                deck.classList.toggle('is-source-mode', mode !== 'dm');
                if (deckShell) {
                    deckShell.setAttribute('aria-label', mode === 'dm' ? 'Deck Inbox direct messages' : 'Expanded media player');
                }
                if (deckInboxSurface) {
                    deckInboxSurface.hidden = !(mode === 'dm' && state.deckInboxActive);
                    deckInboxSurface.classList.toggle('is-list-open', !!state.deckInboxListOpen);
                    deckInboxSurface.querySelectorAll('[data-dm-action="toggle-list"]').forEach((btn) => {
                        btn.setAttribute('aria-expanded', state.deckInboxListOpen ? 'true' : 'false');
                    });
                }
                if (deckModeTabs) {
                    deckModeTabs.hidden = !showTabs;
                }
                if (deckSourceTab) {
                    deckSourceTab.classList.toggle('is-active', mode !== 'dm');
                    deckSourceTab.disabled = !sourceAvailable;
                }
                if (deckInboxTab) {
                    deckInboxTab.classList.toggle('is-active', mode === 'dm');
                    deckInboxTab.disabled = !state.deckInboxActive;
                }
                if (deckOpenMessagesBtn) {
                    deckOpenMessagesBtn.hidden = mode !== 'dm';
                    const href = (state.deckInboxActiveThread && state.deckInboxActiveThread.href)
                        || (((window.CANOPY_VARS || {}).urls || {}).messages || '/messages');
                    deckOpenMessagesBtn.setAttribute('href', href);
                }
                if (mode === 'dm') {
                    if (deckChipLabel) deckChipLabel.textContent = 'Deck Inbox';
                    if (deckCountChip) deckCountChip.hidden = true;
                    if (deckQueueCount) deckQueueCount.hidden = true;
                    if (deckSource) {
                        deckSource.textContent = `${deckInboxMeshspaceLabel()} · DMs without leaving this workspace`;
                    }
                }
            }

            function setDeckShellMode(mode) {
                const requested = mode === 'dm' ? 'dm' : 'source';
                if (requested === 'source' && !sourceDeckHasRenderableState() && state.deckInboxActive) {
                    state.deckShellMode = 'dm';
                } else {
                    state.deckShellMode = requested;
                }
                updateDeckModeChrome();
                updateDeckVisibility();
            }

            function deckInboxSetLoading(message) {
                if (deckInboxThreadBody) {
                    deckInboxThreadBody.innerHTML = `<div class="deck-inbox-loading">${escapeEmbedHtml(message || 'Loading Deck Inbox...')}</div>`;
                }
            }

            function deckInboxTargetFromHref(href) {
                const target = {};
                if (!href) return target;
                try {
                    const url = new URL(href, window.location.origin);
                    const withId = (url.searchParams.get('with') || '').trim();
                    const groupId = (url.searchParams.get('group') || '').trim();
                    if (withId) target.userId = withId;
                    if (groupId) target.groupId = groupId;
                    if (url.hash && url.hash.startsWith('#message-')) {
                        target.targetMessageId = decodeURIComponent(url.hash.slice('#message-'.length));
                    }
                } catch (_) {
                    /* ignore malformed local href */
                }
                return target;
            }

            function buildDeckInboxSnapshotUrl(options = {}) {
                const merged = {
                    ...deckInboxTargetFromHref(options.targetUrl || options.href || ''),
                    ...options,
                };
                const params = new URLSearchParams();
                params.set('surface', 'deck');
                const userId = String(merged.userId || merged.with || '').trim();
                const groupId = String(merged.groupId || merged.group || '').trim();
                if (groupId) {
                    params.set('group', groupId);
                } else if (userId) {
                    params.set('with', userId);
                } else if (state.deckInboxConversationGroup) {
                    params.set('group', state.deckInboxConversationGroup);
                } else if (state.deckInboxConversationWith) {
                    params.set('with', state.deckInboxConversationWith);
                }
                state.deckInboxTargetMessageId = String(merged.targetMessageId || '').trim();
                return `/ajax/messages/thread_snapshot?${params.toString()}`;
            }

            function applyDeckInboxSnapshot(data, options = {}) {
                if (!data || data.success === false) {
                    throw new Error((data && data.error) || 'Failed to load Deck Inbox');
                }
                if (deckInboxSidebar && typeof data.sidebar_html === 'string') {
                    deckInboxSidebar.innerHTML = data.sidebar_html;
                }
                if (deckInboxThreadHeader && typeof data.thread_header_html === 'string') {
                    deckInboxThreadHeader.innerHTML = data.thread_header_html;
                }
                if (deckInboxThreadBody && typeof data.thread_body_html === 'string') {
                    deckInboxThreadBody.innerHTML = data.thread_body_html;
                }
                if (deckInboxComposerSlot && typeof data.composer_html === 'string') {
                    deckInboxComposerSlot.innerHTML = data.composer_html;
                    state.deckInboxCanopyLLMDraftReady = false;
                    state.deckInboxCanopyLLMIgnoredDraft = '';
                }
                state.deckInboxActiveThread = data.active_thread || null;
                state.deckInboxComposerRecipients = Array.isArray(data.composer_recipients) ? data.composer_recipients : [];
                state.deckInboxConversationWith = String(data.conversation_with || '').trim();
                state.deckInboxConversationGroup = String(data.conversation_group || '').trim();
                state.deckInboxSidebarToken = String(data.sidebar_state_token || '');
                state.deckInboxThreadToken = String(data.thread_state_token || '');
                state.deckInboxReply = null;
                if (!state.deckInboxActiveThread) {
                    state.deckInboxListOpen = true;
                }
                updateDeckModeChrome();
                updateDeckInboxCanopyLLMComposePanel();
                if (typeof formatTimestamps === 'function' && deckInboxSurface) {
                    formatTimestamps(deckInboxSurface);
                }
                if (typeof window.canopyApplySourceLayoutsInScope === 'function' && deckInboxSurface) {
                    try {
                        window.canopyApplySourceLayoutsInScope(deckInboxSurface);
                    } catch (_) {
                        /* layout errors should not block DM usage */
                    }
                }
                if (typeof window.requestCanopySidebarDmRefresh === 'function') {
                    window.requestCanopySidebarDmRefresh({ force: true }).catch(() => {});
                }
                window.setTimeout(() => {
                    if (!deckInboxThreadScroll) return;
                    const targetId = String(options.targetMessageId || state.deckInboxTargetMessageId || '').trim();
                    if (targetId) {
                        const esc = window.CSS && typeof window.CSS.escape === 'function'
                            ? window.CSS.escape(targetId)
                            : targetId.replace(/["\\]/g, '\\$&');
                        const targetEl = deckInboxThreadBody && deckInboxThreadBody.querySelector(`#message-${esc}`);
                        if (targetEl && typeof targetEl.scrollIntoView === 'function') {
                            targetEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            targetEl.classList.add('flash-focus');
                            return;
                        }
                    }
                    deckInboxThreadScroll.scrollTop = deckInboxThreadScroll.scrollHeight;
                }, 40);
            }

            function loadDeckInboxSnapshot(options = {}) {
                if (!deckInboxSurface) return Promise.resolve(null);
                const requestSeq = ++state.deckInboxSnapshotSeq;
                state.deckInboxSnapshotInFlight = true;
                deckInboxSetLoading('Loading Deck Inbox...');
                const snapshotUrl = buildDeckInboxSnapshotUrl(options);
                return fetch(snapshotUrl, {
                    headers: { 'X-Requested-With': 'XMLHttpRequest' },
                })
                    .then((res) => {
                        if (!res.ok) throw new Error(`Deck Inbox snapshot failed (${res.status})`);
                        return res.json();
                    })
                    .then((data) => {
                        if (requestSeq !== state.deckInboxSnapshotSeq) return data;
                        applyDeckInboxSnapshot(data, options);
                        return data;
                    })
                    .catch((err) => {
                        if (requestSeq !== state.deckInboxSnapshotSeq) return null;
                        if (deckInboxThreadBody) {
                            deckInboxThreadBody.innerHTML = '<div class="deck-inbox-empty"><div class="deck-inbox-empty-card">Deck Inbox could not load. Open Messages for the full workspace.</div></div>';
                        }
                        showAlert((err && err.message) || 'Deck Inbox could not load', 'warning');
                        return null;
                    })
                    .finally(() => {
                        if (requestSeq === state.deckInboxSnapshotSeq) {
                            state.deckInboxSnapshotInFlight = false;
                        }
                    });
            }

            function openDeckInbox(options = {}) {
                if (!deck || !deckInboxSurface) {
                    window.location.href = (((window.CANOPY_VARS || {}).urls || {}).messages || '/messages');
                    return Promise.resolve(null);
                }
                state.deckInboxActive = true;
                state.dismissedEl = null;
                setDeckShellMode('dm');
                updateDeckVisibility();
                return loadDeckInboxSnapshot(options);
            }

            function closeDeckInbox(options = {}) {
                state.deckInboxActive = false;
                state.deckInboxReply = null;
                state.deckInboxTargetMessageId = '';
                state.deckInboxListOpen = false;
                state.deckInboxCanopyLLMInFlight = false;
                state.deckInboxCanopyLLMDraftReady = false;
                state.deckInboxCanopyLLMIgnoredDraft = '';
                state.deckInboxSnapshotSeq += 1;
                state.deckInboxSnapshotInFlight = false;
                if (options.purgeContent) {
                    if (deckInboxSidebar) deckInboxSidebar.innerHTML = '';
                    if (deckInboxThreadHeader) deckInboxThreadHeader.innerHTML = '';
                    if (deckInboxThreadBody) deckInboxThreadBody.innerHTML = '';
                    if (deckInboxComposerSlot) deckInboxComposerSlot.innerHTML = '';
                    state.deckInboxActiveThread = null;
                    state.deckInboxComposerRecipients = [];
                    state.deckInboxConversationWith = '';
                    state.deckInboxConversationGroup = '';
                    state.deckInboxSidebarToken = '';
                    state.deckInboxThreadToken = '';
                }
                if (deckInboxSurface) deckInboxSurface.hidden = true;
                const restoreSource = sourceDeckHasRenderableState() && options.restoreSource !== false;
                state.deckShellMode = 'source';
                if (restoreSource) {
                    updateDeckPanel();
                    return;
                }
                updateDeckVisibility();
                updateDeckModeChrome();
            }

            function closeDeckShell(options = {}) {
                if (state.deckShellMode === 'dm' && state.deckInboxActive) {
                    closeDeckInbox({ restoreSource: true });
                    return;
                }
                closeMediaDeck(options);
            }

            function deckInboxComposerRoot() {
                return deckInboxSurface ? deckInboxSurface.querySelector('[data-dm-role="composer"]') : null;
            }

            function deckInboxTextArea() {
                const root = deckInboxComposerRoot();
                return root ? root.querySelector('[data-dm-role="message-content"]') : null;
            }

            function deckInboxHasCanopyLLMTrigger(value) {
                return !!(window.CanopyLLMCompose && window.CanopyLLMCompose.hasTrigger(value));
            }

            function deckInboxShouldGenerateCanopyLLMDraft(value) {
                const content = String(value || '');
                return deckInboxHasCanopyLLMTrigger(content)
                    && (!state.deckInboxCanopyLLMIgnoredDraft || content !== state.deckInboxCanopyLLMIgnoredDraft);
            }

            function setDeckInboxCanopyLLMStatus(message, visible = true, kind = 'info', options = {}) {
                const root = deckInboxComposerRoot();
                const panel = root ? root.querySelector('[data-dm-role="canopy-llm-status"]') : null;
                const text = root ? root.querySelector('[data-dm-role="canopy-llm-status-text"]') : null;
                const generateBtn = root ? root.querySelector('[data-dm-role="canopy-llm-generate"]') : null;
                const dismissBtn = root ? root.querySelector('[data-dm-role="canopy-llm-dismiss"]') : null;
                if (!panel || !text) return;
                panel.hidden = !visible;
                panel.classList.toggle('alert-success', kind === 'success');
                panel.classList.toggle('alert-danger', kind === 'error');
                panel.classList.toggle('alert-primary', kind !== 'success' && kind !== 'error');
                if (!visible) {
                    text.textContent = '';
                    return;
                }
                text.textContent = message || 'Preparing draft...';
                const textarea = deckInboxTextArea();
                const busy = !!options.busy || state.deckInboxCanopyLLMInFlight;
                if (generateBtn) {
                    const label = options.actionLabel || (state.deckInboxCanopyLLMDraftReady ? 'Refine draft' : 'Generate draft');
                    generateBtn.disabled = busy || !deckInboxShouldGenerateCanopyLLMDraft(textarea && textarea.value);
                    generateBtn.innerHTML = busy
                        ? '<span class="spinner-border spinner-border-sm me-1" role="status" aria-hidden="true"></span>Generating...'
                        : `<i class="bi bi-stars me-1"></i>${canopyEscapeHtml(label)}`;
                }
                if (dismissBtn) {
                    dismissBtn.disabled = busy || !deckInboxHasCanopyLLMTrigger(textarea && textarea.value);
                    dismissBtn.title = 'Ignore @Canopy for this exact draft and enable normal send';
                }
            }

            function updateDeckInboxCanopyLLMComposePanel() {
                if (state.deckInboxCanopyLLMInFlight) return;
                const textarea = deckInboxTextArea();
                const content = String((textarea && textarea.value) || '');
                if (state.deckInboxCanopyLLMIgnoredDraft && content !== state.deckInboxCanopyLLMIgnoredDraft) {
                    state.deckInboxCanopyLLMIgnoredDraft = '';
                }
                if (deckInboxHasCanopyLLMTrigger(content) && !deckInboxShouldGenerateCanopyLLMDraft(content)) {
                    setDeckInboxCanopyLLMStatus('', false);
                    return;
                }
                if (deckInboxHasCanopyLLMTrigger(content)) {
                    setDeckInboxCanopyLLMStatus(
                        state.deckInboxCanopyLLMDraftReady
                            ? 'Ready to refine this DM draft. The result will return here for review before sending.'
                            : 'Ready to generate a DM draft. Send or Generate will fill the composer first, not post automatically.',
                        true,
                        'info',
                        { actionLabel: state.deckInboxCanopyLLMDraftReady ? 'Refine draft' : 'Generate draft' }
                    );
                    return;
                }
                if (state.deckInboxCanopyLLMDraftReady && content.trim()) {
                    setDeckInboxCanopyLLMStatus('Draft inserted. Review and send when ready, or add @Canopy to refine again.', true, 'success', {
                        actionLabel: 'Refine draft'
                    });
                    return;
                }
                state.deckInboxCanopyLLMDraftReady = false;
                setDeckInboxCanopyLLMStatus('', false);
            }

            function dismissDeckInboxCanopyLLMComposePanel() {
                if (state.deckInboxCanopyLLMInFlight) return;
                const textarea = deckInboxTextArea();
                const content = String((textarea && textarea.value) || '');
                state.deckInboxCanopyLLMIgnoredDraft = deckInboxHasCanopyLLMTrigger(content) ? content : '';
                state.deckInboxCanopyLLMDraftReady = false;
                setDeckInboxCanopyLLMStatus('', false);
                if (textarea) textarea.focus();
            }

            function setDeckInboxReply(messageId, senderLabel, preview) {
                state.deckInboxReply = {
                    messageId: String(messageId || '').trim(),
                    senderLabel: String(senderLabel || 'Reply'),
                    preview: String(preview || 'Replying in thread'),
                };
                const root = deckInboxComposerRoot();
                if (!root) return;
                const box = root.querySelector('[data-dm-role="reply-context"]');
                const author = root.querySelector('[data-dm-role="reply-author"]');
                const content = root.querySelector('[data-dm-role="reply-preview"]');
                if (author) author.textContent = state.deckInboxReply.senderLabel;
                if (content) content.textContent = state.deckInboxReply.preview;
                if (box) box.classList.add('active');
                const textarea = deckInboxTextArea();
                if (textarea) textarea.focus();
            }

            function clearDeckInboxReply() {
                state.deckInboxReply = null;
                const root = deckInboxComposerRoot();
                const box = root ? root.querySelector('[data-dm-role="reply-context"]') : null;
                if (box) box.classList.remove('active');
            }

            async function generateDeckInboxCanopyLLMDraft(recipients = null) {
                if (state.deckInboxCanopyLLMInFlight) return false;
                const textarea = deckInboxTextArea();
                const rawContent = String((textarea && textarea.value) || '');
                if (!deckInboxHasCanopyLLMTrigger(rawContent)) {
                    setDeckInboxCanopyLLMStatus('Add @Canopy plus instructions to the message, then generate again.', true, 'info');
                    if (textarea) textarea.focus();
                    return false;
                }
                if (!deckInboxShouldGenerateCanopyLLMDraft(rawContent)) {
                    state.deckInboxCanopyLLMIgnoredDraft = '';
                    updateDeckInboxCanopyLLMComposePanel();
                    return false;
                }
                const targetRecipients = Array.isArray(recipients)
                    ? recipients
                    : (Array.isArray(state.deckInboxComposerRecipients) ? state.deckInboxComposerRecipients : [])
                        .map((item) => String((item && item.user_id) || '').trim())
                        .filter(Boolean);
                state.deckInboxCanopyLLMInFlight = true;
                state.deckInboxCanopyLLMIgnoredDraft = '';
                const root = deckInboxComposerRoot();
                const sendBtn = root ? root.querySelector('[data-dm-role="send-button"]') : null;
                if (sendBtn) {
                    sendBtn.disabled = true;
                    sendBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Drafting...';
                }
                setDeckInboxCanopyLLMStatus('Sending prompt to your configured LLM...', true, 'info', { busy: true });
                let fullText = '';
                const payload = {
                    content: rawContent,
                    surface: 'dm',
                    recipient_ids: targetRecipients,
                    conversation_with: state.deckInboxConversationWith || '',
                    conversation_group: state.deckInboxConversationGroup || '',
                };
                try {
                    let data = null;
                    try {
                        data = await window.CanopyLLMCompose.streamDraft(payload, {
                            onStatus(message) {
                                if (message) setDeckInboxCanopyLLMStatus(message, true, 'info', { busy: true });
                            },
                            onDelta(delta) {
                                fullText += delta;
                                if (textarea) textarea.value = fullText;
                            },
                        });
                    } catch (streamError) {
                        if (streamError && streamError.fallback) {
                            data = await window.CanopyLLMCompose.expandDraft(payload);
                        } else {
                            throw streamError;
                        }
                    }
                    const expanded = String((data && data.content) || '').trim();
                    if (!expanded) throw { error: 'Canopy AI Compose returned an empty draft.' };
                    if (textarea && !fullText) {
                        textarea.value = expanded;
                        textarea.setSelectionRange(textarea.value.length, textarea.value.length);
                    }
                    state.deckInboxCanopyLLMDraftReady = true;
                    setDeckInboxCanopyLLMStatus(`Draft inserted from ${(data && data.model) || 'LLM'}. Review, edit, send, or add @Canopy to refine again.`, true, 'success', {
                        actionLabel: 'Refine draft'
                    });
                    if (textarea) textarea.focus();
                    return true;
                } catch (error) {
                    console.error('Canopy AI Compose failed:', error);
                    const message = (error && (error.error || error.message)) || 'Canopy AI Compose failed.';
                    setDeckInboxCanopyLLMStatus(message, true, 'error');
                    showAlert(message, 'warning');
                    return false;
                } finally {
                    state.deckInboxCanopyLLMInFlight = false;
                    if (sendBtn) {
                        sendBtn.disabled = false;
                        sendBtn.innerHTML = '<i class="bi bi-send"></i> Send';
                    }
                    updateDeckInboxCanopyLLMComposePanel();
                }
            }

            async function sendDeckInboxMessage() {
                const textarea = deckInboxTextArea();
                const content = String((textarea && textarea.value) || '').trim();
                if (!content) {
                    showAlert('Message content required', 'warning');
                    return;
                }
                const recipients = (Array.isArray(state.deckInboxComposerRecipients) ? state.deckInboxComposerRecipients : [])
                    .map((item) => String((item && item.user_id) || '').trim())
                    .filter(Boolean);
                if (!recipients.length) {
                    showAlert('Open a conversation before sending from Deck Inbox.', 'warning');
                    return;
                }
                if (deckInboxShouldGenerateCanopyLLMDraft(content)) {
                    await generateDeckInboxCanopyLLMDraft(recipients);
                    return;
                }
                const payload = { content };
                if (recipients.length > 1) payload.recipient_ids = recipients;
                else payload.recipient_id = recipients[0];
                if (state.deckInboxReply && state.deckInboxReply.messageId) {
                    payload.reply_to = state.deckInboxReply.messageId;
                }
                const root = deckInboxComposerRoot();
                const sendBtn = root ? root.querySelector('[data-dm-role="send-button"]') : null;
                if (sendBtn) {
                    sendBtn.disabled = true;
                    sendBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Sending...';
                }
                apiCall('/ajax/send_message', {
                    method: 'POST',
                    body: JSON.stringify(payload),
                })
                    .then((data) => {
                        if (!data || data.success === false || data.error) {
                            throw new Error((data && data.error) || 'Failed to send message');
                        }
                        if (textarea) textarea.value = '';
                        state.deckInboxCanopyLLMDraftReady = false;
                        state.deckInboxCanopyLLMIgnoredDraft = '';
                        updateDeckInboxCanopyLLMComposePanel();
                        clearDeckInboxReply();
                        const nextOptions = {};
                        if (data && data.group_id) {
                            nextOptions.groupId = data.group_id;
                        } else if (state.deckInboxConversationGroup) {
                            nextOptions.groupId = state.deckInboxConversationGroup;
                        } else if (state.deckInboxConversationWith) {
                            nextOptions.userId = state.deckInboxConversationWith;
                        }
                        return loadDeckInboxSnapshot(nextOptions);
                    })
                    .catch((err) => {
                        showAlert((err && (err.error || err.message)) || 'Failed to send message', 'danger');
                    })
                    .finally(() => {
                        if (sendBtn) {
                            sendBtn.disabled = false;
                            sendBtn.innerHTML = '<i class="bi bi-send"></i> Send';
                        }
                    });
            }

            function deleteDeckInboxMessage(messageId) {
                const cleanId = String(messageId || '').trim();
                if (!cleanId) return;
                if (!window.confirm('Delete this message?')) return;
                apiCall('/ajax/delete_message', {
                    method: 'POST',
                    body: JSON.stringify({ message_id: cleanId }),
                })
                    .then(() => loadDeckInboxSnapshot({}))
                    .then(() => showAlert('Message deleted', 'success'))
                    .catch((err) => showAlert((err && (err.error || err.message)) || 'Delete failed', 'danger'));
            }

            function getDeckInboxMessageCopyText(messageId) {
                if (!deckInboxThreadBody) return '';
                const escapedId = window.CSS && typeof window.CSS.escape === 'function'
                    ? window.CSS.escape(String(messageId || ''))
                    : String(messageId || '').replace(/["\\]/g, '\\$&');
                const row = deckInboxThreadBody.querySelector(`#message-${escapedId}`);
                const contentEl = row ? (row.querySelector('[data-message-copy-text="1"]') || row.querySelector('.dm-message-text')) : null;
                if (!contentEl) return '';
                return String(contentEl.innerText || contentEl.textContent || '').trim();
            }

            function copyDeckInboxMessageText(messageId) {
                _copyTextToClipboard(getDeckInboxMessageCopyText(messageId), 'message text');
            }

            function normalizeDeckDmReactionKey(value) {
                const raw = String(value || '').trim().toLowerCase();
                if (!raw) return 'like';
                if (raw.startsWith(':') && raw.endsWith(':') && raw.length > 2) {
                    const slug = raw.slice(1, -1)
                        .replace(/[^a-z0-9_-]+/g, '-')
                        .replace(/^-+|-+$/g, '');
                    return `custom:${slug || 'emoji'}`;
                }
                if (raw.startsWith('custom:')) {
                    const slug = raw.slice('custom:'.length)
                        .replace(/[^a-z0-9_-]+/g, '-')
                        .replace(/^-+|-+$/g, '');
                    return `custom:${slug || 'emoji'}`;
                }
                return raw;
            }

            function normalizeDeckDmOptionalReactionKey(value) {
                const raw = String(value || '').trim();
                return raw ? normalizeDeckDmReactionKey(raw) : '';
            }

            function normalizeDeckDmReactionCounts(value) {
                if (!value || typeof value !== 'object') return {};
                const counts = {};
                Object.entries(value).forEach(([type, count]) => {
                    const key = normalizeDeckDmReactionKey(type);
                    const parsed = Math.max(0, parseInt(count, 10) || 0);
                    if (key && parsed > 0) counts[key] = parsed;
                });
                return counts;
            }

            function getDeckDmReactionStrip(messageId) {
                const root = deckInboxSurface || document;
                const escapedId = window.CSS && typeof window.CSS.escape === 'function'
                    ? window.CSS.escape(String(messageId))
                    : String(messageId).replace(/["\\]/g, '\\$&');
                return root.querySelector(`.dm-reaction-strip[data-message-id="${escapedId}"]`);
            }

            function getDeckDmReactionState(messageId) {
                const strip = getDeckDmReactionStrip(messageId);
                if (!strip) return { counts: {}, userReaction: '' };
                let counts = {};
                try {
                    counts = normalizeDeckDmReactionCounts(JSON.parse(strip.dataset.reactions || '{}'));
                } catch (_) {
                    counts = {};
                }
                return {
                    counts,
                    userReaction: normalizeDeckDmOptionalReactionKey(strip.dataset.userReaction || ''),
                };
            }

            function getDeckDmReactionOptions(strip, counts = {}) {
                const options = [];
                const seen = new Set();
                const root = strip || deckInboxSurface || document;
                root.querySelectorAll('.dm-reaction-choice[data-reaction-type]').forEach((choice) => {
                    const type = normalizeDeckDmReactionKey(choice.getAttribute('data-reaction-type') || '');
                    if (!type || seen.has(type)) return;
                    const img = choice.querySelector('img');
                    const emoji = (choice.querySelector('span')?.textContent || '').trim();
                    const small = (choice.querySelector('small')?.textContent || '').trim();
                    seen.add(type);
                    options.push({
                        type,
                        label: small || choice.getAttribute('title') || (type.startsWith('custom:') ? type.slice(7) : 'Reaction'),
                        emoji: emoji || (type.startsWith('custom:') ? `:${type.slice(7)}:` : '👍'),
                        image_url: img ? (img.getAttribute('src') || img.src || '') : '',
                    });
                });
                Object.keys(normalizeDeckDmReactionCounts(counts)).sort().forEach((type) => {
                    if (seen.has(type)) return;
                    const label = type.startsWith('custom:')
                        ? type.slice(7).replace(/[-_]+/g, ' ').replace(/\b\w/g, (m) => m.toUpperCase())
                        : 'Reaction';
                    options.push({
                        type,
                        label,
                        emoji: type.startsWith('custom:') ? `:${type.slice(7)}:` : '👍',
                        image_url: '',
                    });
                });
                return options;
            }

            function buildDeckDmReactionState(current, selectedType) {
                const selected = normalizeDeckDmReactionKey(selectedType || 'like');
                const counts = normalizeDeckDmReactionCounts((current && current.counts) || {});
                const previous = normalizeDeckDmOptionalReactionKey((current && current.userReaction) || '');
                let userReaction = selected;
                if (previous === selected) {
                    counts[selected] = Math.max(0, (counts[selected] || 0) - 1);
                    if (counts[selected] <= 0) delete counts[selected];
                    userReaction = '';
                } else {
                    if (previous) {
                        counts[previous] = Math.max(0, (counts[previous] || 0) - 1);
                        if (counts[previous] <= 0) delete counts[previous];
                    }
                    counts[selected] = Math.max(0, (counts[selected] || 0)) + 1;
                }
                return { counts, userReaction };
            }

            function renderDeckDmReactionVisual(option) {
                if (option && option.image_url) {
                    return `<img class="dm-reaction-custom-img" src="${escapeEmbedAttr(option.image_url)}" alt="" loading="lazy">`;
                }
                return `<span class="dm-reaction-emoji">${escapeEmbedHtml((option && option.emoji) || '👍')}</span>`;
            }

            function renderDeckDmReactionPills(messageId, strip, counts, userReaction) {
                const normalizedCounts = normalizeDeckDmReactionCounts(counts);
                return getDeckDmReactionOptions(strip, normalizedCounts)
                    .map((option) => {
                        const count = normalizedCounts[option.type] || 0;
                        if (!count) return '';
                        const selected = userReaction === option.type ? ' is-selected' : '';
                        return `
                            <button type="button"
                                    class="dm-reaction-pill${selected}"
                                    data-reaction-type="${escapeEmbedAttr(option.type)}"
                                    onclick="toggleDmReaction(${JSON.stringify(String(messageId))}, ${JSON.stringify(option.type)})"
                                    title="${escapeEmbedAttr(option.label)}"
                                    aria-label="${escapeEmbedAttr(`${option.label} reaction, ${count}`)}"
                                    aria-pressed="${selected ? 'true' : 'false'}">
                                ${renderDeckDmReactionVisual(option)}
                                <span class="dm-reaction-count">${count}</span>
                            </button>
                        `;
                    })
                    .join('');
            }

            function updateDeckDmReactionStrip(messageId, counts, userReaction) {
                const strip = getDeckDmReactionStrip(messageId);
                if (!strip) return;
                const normalizedCounts = normalizeDeckDmReactionCounts(counts);
                const normalizedReaction = normalizeDeckDmOptionalReactionKey(userReaction);
                strip.dataset.reactions = JSON.stringify(normalizedCounts);
                strip.dataset.userReaction = normalizedReaction;
                strip.classList.toggle('has-reactions', Object.keys(normalizedCounts).length > 0);
                const pills = strip.querySelector('[data-dm-reaction-pills]');
                if (pills) {
                    pills.innerHTML = renderDeckDmReactionPills(messageId, strip, normalizedCounts, normalizedReaction);
                }
                strip.querySelectorAll('.dm-reaction-choice[data-reaction-type]').forEach((choice) => {
                    const selected = normalizeDeckDmReactionKey(choice.getAttribute('data-reaction-type') || '') === normalizedReaction;
                    choice.classList.toggle('is-selected', selected);
                    choice.setAttribute('aria-pressed', selected ? 'true' : 'false');
                });
            }

            function setDeckDmReactionBusy(messageId, busy) {
                const strip = getDeckDmReactionStrip(messageId);
                if (!strip) return;
                strip.classList.toggle('is-busy', Boolean(busy));
                strip.setAttribute('aria-busy', busy ? 'true' : 'false');
                strip.querySelectorAll('button').forEach((button) => {
                    button.disabled = Boolean(busy);
                });
            }

            function openDeckDmReactionPicker(messageId) {
                const strip = getDeckDmReactionStrip(messageId);
                const toggle = strip ? strip.querySelector('.dm-reaction-add') : null;
                if (!toggle || toggle.disabled) return;
                if (window.bootstrap && window.bootstrap.Dropdown) {
                    window.bootstrap.Dropdown.getOrCreateInstance(toggle, { autoClose: 'outside' }).show();
                } else {
                    const menu = strip.querySelector('.dm-reaction-palette');
                    if (menu) menu.classList.add('show');
                    toggle.setAttribute('aria-expanded', 'true');
                }
                if (typeof toggle.focus === 'function') {
                    try {
                        toggle.focus({ preventScroll: true });
                    } catch (_) {
                        toggle.focus();
                    }
                }
            }

            function closeDeckDmReactionPicker(messageId) {
                const strip = getDeckDmReactionStrip(messageId);
                const toggle = strip ? strip.querySelector('.dm-reaction-add') : null;
                if (!toggle) return;
                if (window.bootstrap && window.bootstrap.Dropdown) {
                    const instance = window.bootstrap.Dropdown.getInstance(toggle);
                    if (instance) instance.hide();
                } else {
                    const menu = strip.querySelector('.dm-reaction-palette');
                    if (menu) menu.classList.remove('show');
                }
                toggle.setAttribute('aria-expanded', 'false');
            }

            function toggleDeckDmReaction(messageId, reactionType) {
                const normalizedId = String(messageId || '').trim();
                if (!normalizedId || state.deckInboxReactionInFlight.has(normalizedId)) return;
                const selectedReaction = normalizeDeckDmReactionKey(reactionType || 'like');
                const before = getDeckDmReactionState(normalizedId);
                const optimistic = buildDeckDmReactionState(before, selectedReaction);
                updateDeckDmReactionStrip(normalizedId, optimistic.counts, optimistic.userReaction);
                state.deckInboxReactionInFlight.add(normalizedId);
                setDeckDmReactionBusy(normalizedId, true);

                apiCall('/ajax/toggle_like', {
                    method: 'POST',
                    body: JSON.stringify({
                        message_id: normalizedId,
                        item_type: 'dm_message',
                        reaction_type: selectedReaction,
                    }),
                })
                    .then((data) => {
                        if (!data || !data.success) {
                            throw new Error((data && data.error) || 'Reaction update failed');
                        }
                        const interactions = data.interactions || {};
                        updateDeckDmReactionStrip(
                            normalizedId,
                            interactions.like_counts || optimistic.counts,
                            data.user_reaction || ''
                        );
                        closeDeckDmReactionPicker(normalizedId);
                    })
                    .catch((err) => {
                        console.error('Deck Inbox reaction update failed:', err);
                        updateDeckDmReactionStrip(normalizedId, before.counts, before.userReaction);
                        showAlert((err && (err.error || err.message)) || 'Failed to update reaction', 'danger');
                    })
                    .finally(() => {
                        state.deckInboxReactionInFlight.delete(normalizedId);
                        setDeckDmReactionBusy(normalizedId, false);
                    });
            }

            if (typeof window.toggleDmReaction !== 'function') {
                window.toggleDmReaction = toggleDeckDmReaction;
            }
            if (typeof window.openDmReactionPicker !== 'function') {
                window.openDmReactionPicker = openDeckDmReactionPicker;
            }

            function setDeckInboxListOpen(nextOpen) {
                state.deckInboxListOpen = !!nextOpen;
                updateDeckModeChrome();
            }

            function handleDeckInboxAction(event) {
                const actionEl = event.target && event.target.closest ? event.target.closest('[data-dm-action]') : null;
                if (!actionEl || !deckInboxSurface || !deckInboxSurface.contains(actionEl)) return;
                const action = String(actionEl.getAttribute('data-dm-action') || '').trim();
                if (!action) return;
                event.preventDefault();
                if (action === 'send-message') {
                    sendDeckInboxMessage();
                } else if (action === 'clear-composer') {
                    const textarea = deckInboxTextArea();
                    if (textarea) textarea.value = '';
                    state.deckInboxCanopyLLMDraftReady = false;
                    state.deckInboxCanopyLLMIgnoredDraft = '';
                    updateDeckInboxCanopyLLMComposePanel();
                    clearDeckInboxReply();
                } else if (action === 'generate-canopy-draft') {
                    generateDeckInboxCanopyLLMDraft();
                } else if (action === 'send-as-written') {
                    dismissDeckInboxCanopyLLMComposePanel();
                } else if (action === 'toggle-list') {
                    setDeckInboxListOpen(!state.deckInboxListOpen);
                } else if (action === 'toggle-composer-expanded') {
                    const root = deckInboxComposerRoot();
                    if (root) {
                        const next = !root.classList.contains('composer-expanded');
                        root.classList.toggle('composer-expanded', next);
                        actionEl.setAttribute('aria-pressed', next ? 'true' : 'false');
                    }
                } else if (action === 'clear-reply') {
                    clearDeckInboxReply();
                } else if (action === 'reply') {
                    setDeckInboxReply(
                        actionEl.getAttribute('data-message-id'),
                        actionEl.getAttribute('data-sender-label'),
                        actionEl.getAttribute('data-preview')
                    );
                } else if (action === 'copy-text') {
                    copyDeckInboxMessageText(actionEl.getAttribute('data-message-id'));
                } else if (action === 'open-reactions') {
                    openDeckDmReactionPicker(actionEl.getAttribute('data-message-id'));
                } else if (action === 'jump-message') {
                    const messageId = String(actionEl.getAttribute('data-message-id') || '').trim();
                    const esc = window.CSS && typeof window.CSS.escape === 'function'
                        ? window.CSS.escape(messageId)
                        : messageId.replace(/["\\]/g, '\\$&');
                    const row = deckInboxThreadBody && deckInboxThreadBody.querySelector(`#message-${esc}`);
                    if (row && typeof row.scrollIntoView === 'function') {
                        row.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        row.classList.add('flash-focus');
                    }
                } else if (action === 'delete') {
                    deleteDeckInboxMessage(actionEl.getAttribute('data-message-id'));
                }
            }

            function handleDeckInboxThreadClick(event) {
                const link = event.target && event.target.closest ? event.target.closest('[data-dm-thread-link]') : null;
                if (!link || !deckInboxSurface || !deckInboxSurface.contains(link)) return;
                if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                event.preventDefault();
                const target = deckInboxTargetFromHref(link.getAttribute('href') || '');
                setDeckInboxListOpen(false);
                openDeckInbox(target);
            }

            if (deckInboxSurface) {
                deckInboxSurface.addEventListener('click', handleDeckInboxAction);
                deckInboxSurface.addEventListener('click', handleDeckInboxThreadClick);
                deckInboxSurface.addEventListener('keydown', (event) => {
                    const textarea = event.target && event.target.closest ? event.target.closest('[data-dm-role="message-content"]') : null;
                    if (!textarea) return;
                    if (event.key === 'Enter' && !event.shiftKey) {
                        event.preventDefault();
                        sendDeckInboxMessage();
                    }
                });
                deckInboxSurface.addEventListener('input', (event) => {
                    const textarea = event.target && event.target.closest ? event.target.closest('[data-dm-role="message-content"]') : null;
                    if (textarea) updateDeckInboxCanopyLLMComposePanel();
                });
            }

            window.openDeckInbox = openDeckInbox;
            window.closeDeckInbox = closeDeckInbox;
            window.addEventListener('canopy:meshspace-switch-start', () => {
                closeDeckInbox({ restoreSource: false, purgeContent: true });
            });
            document.addEventListener('click', (event) => {
                const link = event.target && event.target.closest
                    ? event.target.closest('[data-meshspace-switcher-item], [data-meshspace-quick-item]')
                    : null;
                if (!link || event.defaultPrevented) return;
                const nextMeshId = String(link.getAttribute('data-meshspace-id') || '').trim();
                const rail = document.querySelector('[data-meshspace-header-rail]');
                const currentMeshId = String(
                    (rail && rail.getAttribute('data-meshspace-current-id'))
                    || (((window.CANOPY_VARS || {}).currentMeshspace || {}).id)
                    || ''
                ).trim();
                if (nextMeshId && currentMeshId && nextMeshId !== currentMeshId) {
                    closeDeckInbox({ restoreSource: false, purgeContent: true });
                }
            }, true);

            function updateDeckVisibility() {
                if (!deck || !deckBackdrop) return;
                applyDeckDesktopMode(state.deckDesktopMode, { silent: true });
                if (state.deckShellMode === 'source' && !sourceDeckHasRenderableState() && state.deckInboxActive) {
                    state.deckShellMode = 'dm';
                }
                const sourceVisible = sourceDeckHasRenderableState();
                const inboxVisible = !!state.deckInboxActive;
                const visible = sourceVisible || inboxVisible;
                const mobileDeckMode = isMobileDeckModalMode();
                deck.hidden = !visible;
                deck.setAttribute('aria-hidden', visible ? 'false' : 'true');
                deck.inert = !visible;
                deck.classList.toggle('is-visible', visible);
                deckBackdrop.hidden = !visible;
                deckBackdrop.inert = !visible;
                deckBackdrop.classList.toggle('is-visible', visible);
                document.body.classList.toggle('canopy-media-deck-open', visible);
                document.body.classList.toggle('canopy-media-deck-modal', visible && mobileDeckMode);
                if (deckShell) {
                    deckShell.setAttribute('aria-modal', visible && mobileDeckMode ? 'true' : 'false');
                }
                updateDeckModeChrome();
            }

            function isMobileDeckModalMode() {
                return window.matchMedia('(max-width: 640px), (max-height: 540px) and (orientation: landscape)').matches;
            }

            function scrollDeckStageIntoView(behavior = 'smooth') {
                if (!state.deckOpen || !deckStageShell || !isMobileDeckModalMode()) return;
                if (typeof deckStageShell.scrollIntoView === 'function') {
                    deckStageShell.scrollIntoView({ behavior, block: 'start', inline: 'nearest' });
                }
            }

            function syncDeckStage() {
                if (!deckStage) return;
                const selectedItem = getDeckSelectedItem();
                if (!selectedItem || !state.deckOpen) {
                    clearDeckStageDockedNodes();
                    deckStage.classList.add('is-empty');
                    setDeckVisualState(null);
                    return;
                }

                if (!isDeckMediaItem(selectedItem)) {
                    renderDeckWidgetStage(selectedItem);
                    return;
                }

                const type = selectedItem.type;
                const el = selectedItem.el;
                if (type === 'youtube' || type === 'video') {
                    const moved = moveDockedMediaToHost(el, deckStage);
                    if (moved) {
                        deckStage.classList.remove('is-empty');
                        if (deckVisual) deckVisual.hidden = true;
                        return;
                    }
                }

                clearDeckStageDockedNodes();
                deckStage.classList.add('is-empty');
                setDeckVisualState(selectedItem || {
                    el,
                    type,
                    title: titleFromMedia(el, type),
                    subtitle: subtitleFromMedia(el, type),
                    thumb: resolveMediaThumbnail(el, type),
                });
            }

            function updateDeckControls(selectedItem) {
                if (!deckPlayBtn || !deckSeek || !deckCurrentTime || !deckDuration || !deckPipBtn) return;
                const isMedia = isDeckMediaItem(selectedItem);
                const el = selectedItem ? selectedItem.el : null;
                const type = selectedItem ? selectedItem.type : '';

                if (deckProgressRow) deckProgressRow.hidden = !isMedia;
                if (deckPlayBtn) deckPlayBtn.hidden = !isMedia;
                if (deckMiniPlayerBtn) deckMiniPlayerBtn.hidden = !isMedia;
                if (deckMiniFooterBtn) deckMiniFooterBtn.hidden = !isMedia;

                if (!isMedia) {
                    deckCurrentTime.textContent = '--:--';
                    deckDuration.textContent = '--:--';
                    deckSeek.value = '0';
                    deckSeek.disabled = true;
                    if (deckPipBtn) deckPipBtn.style.display = 'none';
                    return;
                }

                let currentTime = 0;
                let duration = 0;
                let paused = false;

                if (type === 'audio' || type === 'video') {
                    paused = !!el.paused;
                    currentTime = Number(el.currentTime || 0);
                    duration = Number(el.duration || 0);
                } else if (type === 'youtube') {
                    const ytIframe = resolveYouTubeMediaElement(el, { activate: false });
                    const ytState = Number(ytIframe && ytIframe.__canopyMiniYTState);
                    paused = !isYouTubePlayingState(ytState);
                    currentTime = getYouTubeCurrentTimeSafe(ytIframe || el);
                    try {
                        const player = ytIframe && ytIframe.__canopyMiniYTPlayer;
                        if (player && typeof player.getDuration === 'function') {
                            duration = Number(player.getDuration() || 0);
                        }
                    } catch (_) {}
                } else {
                    paused = true;
                }

                deckPlayBtn.innerHTML = `<i class="bi bi-${paused ? 'play-fill' : 'pause-fill'}"></i><span>${paused ? 'Play' : 'Pause'}</span>`;
                deckCurrentTime.textContent = formatTime(currentTime);
                deckDuration.textContent = duration > 0 ? formatTime(duration) : '--:--';

                if (!state.deckSeeking) {
                    const seekValue = duration > 0 ? Math.round((currentTime / duration) * 1000) : 0;
                    deckSeek.value = String(Math.max(0, Math.min(1000, seekValue)));
                }
                deckSeek.disabled = !(duration > 0.1);

                if (deckPipBtn) {
                    if (type === 'video' && supportsPictureInPicture(el)) {
                        const inPiP = isPictureInPictureActiveFor(el);
                        deckPipBtn.style.display = '';
                        deckPipBtn.innerHTML = `<i class="bi bi-pip"></i><span>${inPiP ? 'Exit PiP' : 'PiP'}</span>`;
                    } else {
                        deckPipBtn.style.display = 'none';
                    }
                }
            }

            function updateDeckPanel() {
                updateDeckVisibility();
                const selectedItem = getDeckSelectedItem();
                if (!state.deckOpen || !selectedItem) return;
                syncDeckLayoutMode(selectedItem);
                refreshDeckOriginSourceElIfStale();
                const anchorUpdate = firstConnectedDeckAnchor(
                    state.deckOriginSourceEl,
                    deckItemSourceEl(selectedItem),
                    state.deckSourceEl,
                    state.current && state.current.sourceEl
                );
                state.deckSourceEl = anchorUpdate || (state.deckSourceEl && state.deckSourceEl.isConnected ? state.deckSourceEl : null);

                if (isDeckMediaItem(selectedItem)) {
                    if (state.current && state.current.el && !state.current.el.isConnected) {
                        repairMediaCurrentReference();
                    } else {
                        ensureMediaSourceLinked();
                    }
                    if (!state.current || !state.current.el || !state.current.el.isConnected) {
                        closeMediaDeck({ forceClose: true });
                        scheduleMiniUpdate(30);
                        return;
                    }
                    reconcileDeckStageMediaPlacement();
                }
                renderDeckQueue();
                syncDeckLayoutMode(getDeckSelectedItem() || selectedItem);

                const type = selectedItem.type;
                const mediaTitle = selectedItem && selectedItem.el
                    ? preferredMediaTitle(selectedItem.el, type, selectedItem.title, { allowLookup: true })
                    : (selectedItem.title || titleFromMedia(selectedItem.el, type));
                selectedItem.title = mediaTitle;
                const subtitle = deckItemContextSubtitle(selectedItem);

                if (deckTitle) deckTitle.textContent = mediaTitle;
                if (deckSubtitle) {
                    deckSubtitle.textContent = subtitle;
                }
                if (deckProvider) {
                    const iconNode = deckProvider.querySelector('i');
                    if (iconNode) {
                        iconNode.className = `bi ${selectedItem.icon || mediaIcon(type)}`;
                    }
                }
                if (deckProviderLabel) {
                    deckProviderLabel.textContent = selectedItem.providerLabel || mediaProviderLabel(type);
                }
                if (deckReturnBtn) {
                    const returnLabel = selectedItem && selectedItem.manifest && selectedItem.manifest.source_binding
                        ? selectedItem.manifest.source_binding.return_label
                        : 'Return to source';
                    deckReturnBtn.innerHTML = `<i class="bi bi-arrow-up-right-square"></i><span>${escapeEmbedHtml(returnLabel)}</span>`;
                }

                syncDeckStage();
                renderDeckWidgetSummary(selectedItem);
                updateDeckControls(selectedItem);
                updateDeckModeChrome();
            }

            function openMediaDeck() {
                if (!state.deckOriginSourceEl || !state.deckOriginSourceEl.isConnected) {
                    state.deckOriginSourceEl = firstConnectedDeckAnchor(
                        state.deckSourceEl,
                        state.current && state.current.sourceEl
                    );
                }
                if (state.deckOriginSourceEl && state.deckOriginSourceEl.isConnected) {
                    if (!String(state.deckOriginMessageId || '').trim()) {
                        state.deckOriginMessageId = String(state.deckOriginSourceEl.getAttribute('data-message-id') || '').trim();
                    }
                    if (!String(state.deckOriginPostId || '').trim()) {
                        state.deckOriginPostId = String(state.deckOriginSourceEl.getAttribute('data-post-id') || '').trim();
                    }
                }
                const mobileDeckMode = isMobileDeckModalMode();
                setDeckQueueCollapsed(mobileDeckMode);
                setDeckDetailCollapsed(mobileDeckMode);
                state.deckQueueNeedsRefresh = true;
                state.deckOpen = true;
                state.deckShellMode = 'source';
                if (state.current || getDeckSelectedItem()) updateDeckPanel();
                else updateDeckVisibility();
                updateSourceDeckLauncherActiveStates();
                if (mobileDeckMode) {
                    scrollDeckStageIntoView('auto');
                }
                if (expandBtn) {
                    expandBtn.innerHTML = '<i class="bi bi-arrows-angle-contract"></i>';
                    expandBtn.title = 'Collapse Canopy deck';
                }
            }

            function closeMediaDeck(options = {}) {
                const preserveMini = !(options && options.forceClose === true);
                moveFocusOutOfDeck();
                if (state.current && state.current.type === 'youtube') {
                    releaseFocusedYouTubeFrame(state.current.el);
                }
                state.deckOpen = false;
                state.deckSelectedKey = '';
                state.deckSourceEl = null;
                state.deckOriginSourceEl = null;
                state.deckOriginMessageId = '';
                state.deckOriginPostId = '';
                state.deckQueueNeedsRefresh = false;
                if (expandBtn) {
                    expandBtn.innerHTML = '<i class="bi bi-arrows-angle-expand"></i>';
                    expandBtn.title = 'Open Canopy deck';
                }
                if (state.current && state.current.el && isDeckMediaItem(state.current)) {
                    restoreDockedMedia(state.current.el, {
                        preferMini: preserveMini,
                        forceDockMini: preserveMini,
                    });
                }
                if (deckStage) {
                    clearDeckStageDockedNodes();
                    deckStage.classList.add('is-empty');
                }
                resetDeckLayoutMode();
                clearDeckStationSummary();
                clearDeckWidgetSummary();
                updateDeckVisibility();
                updateSourceDeckLauncherActiveStates();
                scheduleMiniUpdate(40);
            }

            function stopActiveMediaPlayback(options = {}) {
                const clearDismissed = options.clearDismissed === true;
                const activeEntry = state.current && state.current.el ? state.current : null;
                if (activeEntry) {
                    const el = activeEntry.el;
                    const type = activeEntry.type;
                    if (type === 'youtube') {
                        clearYouTubeDockResumeState(el);
                    }
                    pauseMediaElement(el, type);
                    deactivateMediaEntry(activeEntry);
                    if (type === 'video' || type === 'youtube') {
                        clearOrphanedDockedMedia(el, type, activeEntry.sourceEl || sourceContainer(el));
                    }
                    state.dismissedEl = clearDismissed ? null : el;
                } else if (clearDismissed) {
                    state.dismissedEl = null;
                }

                state.current = null;
                state.returnUrl = null;
                state.dockedSubtitle = null;
                state.deckSelectedKey = '';
                state.deckItems = [];
                state.deckQueueSignature = '';
                state.deckSourceEl = null;
                state.deckOriginSourceEl = null;
                state.deckOriginMessageId = '';
                state.deckOriginPostId = '';
                state.deckQueueNeedsRefresh = false;
                state.deckOpen = false;

                if (miniVideoHost) {
                    miniVideoHost.style.display = 'none';
                    miniVideoHost.innerHTML = '';
                }
                if (deckStage) {
                    clearDeckStageDockedNodes();
                    deckStage.classList.add('is-empty');
                }
                resetDeckLayoutMode();
                clearDeckStationSummary();
                clearDeckWidgetSummary();
                updateDeckVisibility();
                updateSourceDeckLauncherActiveStates();
                hideMini();
            }

            function playDeckRelative(delta) {
                if (!state.deckItems.length) return;
                const selectedItem = getDeckSelectedItem();
                const currentIndex = Math.max(0, state.deckItems.findIndex((item) => selectedItem && item.key === selectedItem.key));
                const nextIndex = (currentIndex + delta + state.deckItems.length) % state.deckItems.length;
                const nextItem = state.deckItems[nextIndex];
                if (!nextItem) return;
                selectDeckItem(nextItem, { play: true });
                scrollDeckStageIntoView();
                scheduleMiniUpdate(20);
            }

            function seekCurrentMediaTo(ratio) {
                if (!state.current || !state.current.el) return;
                const el = state.current.el;
                const type = state.current.type;
                const clampedRatio = Math.max(0, Math.min(1, Number(ratio) || 0));
                if (type === 'audio' || type === 'video') {
                    const duration = Number(el.duration || 0);
                    if (duration > 0) {
                        el.currentTime = duration * clampedRatio;
                    }
                } else if (type === 'youtube') {
                    try {
                        const ytIframe = resolveYouTubeMediaElement(el, { activate: false });
                        const player = ytIframe && ytIframe.__canopyMiniYTPlayer;
                        const duration = Number(player && typeof player.getDuration === 'function' ? player.getDuration() : 0);
                        if (player && typeof player.seekTo === 'function' && duration > 0) {
                            player.seekTo(duration * clampedRatio, true);
                        }
                    } catch (_) {}
                }
                scheduleMiniUpdate(40);
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
                    const ytIframe = resolveYouTubeMediaElement(el, { activate: false });
                    return isYouTubePlayingState(Number(ytIframe && ytIframe.__canopyMiniYTState));
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

            function isDockedInMiniHost(el) {
                return miniVideoHost && el && miniVideoHost.contains(el);
            }

            function hideMini() {
                mini.classList.remove('is-visible');
                if (pipBtn) pipBtn.style.display = 'none';
                if (miniVideoHost) miniVideoHost.style.display = 'none';
                if (expandBtn) expandBtn.disabled = true;
            }

            function showMini() {
                mini.classList.add('is-visible');
                if (expandBtn) expandBtn.disabled = false;
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

            function getYouTubeCurrentTimeSafe(el) {
                try {
                    const player = el && el.__canopyMiniYTPlayer;
                    if (player && typeof player.getCurrentTime === 'function') {
                        const t = Number(player.getCurrentTime());
                        if (Number.isFinite(t) && t > 0) return t;
                    }
                } catch (_) {}
                const remembered = Number((el && el.__canopyMiniYTLastTime) || 0);
                return Number.isFinite(remembered) ? remembered : 0;
            }

            function getYouTubePlayerStateSafe(el) {
                try {
                    const player = el && el.__canopyMiniYTPlayer;
                    if (player && typeof player.getPlayerState === 'function') {
                        const stateNow = Number(player.getPlayerState());
                        if (Number.isFinite(stateNow)) return stateNow;
                    }
                } catch (_) {}
                const remembered = Number((el && el.__canopyMiniYTState) || -1);
                return Number.isFinite(remembered) ? remembered : -1;
            }

            function clearYouTubeDockResumeState(el) {
                if (!el) return;
                if (el.__canopyMiniYTDockRestoreTimer) {
                    clearInterval(el.__canopyMiniYTDockRestoreTimer);
                    delete el.__canopyMiniYTDockRestoreTimer;
                }
                delete el.__canopyMiniYTDockResumeAt;
                delete el.__canopyMiniYTDockShouldResume;
            }

            function shouldPersistActiveYouTube(el) {
                if (!el) return false;
                if (state.dismissedEl && state.dismissedEl === el) return false;
                return isYouTubePlayingState(getYouTubePlayerStateSafe(el));
            }

            /** @returns {boolean} true if iframe src was updated (navigation; YT.Player must be re-created). */
            function setYouTubeDockResumeParams(el, resumeAt, shouldResume) {
                if (!el) return false;
                try {
                    const src = el.getAttribute('src') || '';
                    if (!src) return false;
                    const url = new URL(src, window.location.origin);
                    if (resumeAt > 1) {
                        url.searchParams.set('start', String(Math.max(0, Math.floor(resumeAt))));
                    } else {
                        url.searchParams.delete('start');
                    }
                    if (shouldResume) {
                        url.searchParams.set('autoplay', '1');
                    } else {
                        url.searchParams.delete('autoplay');
                    }
                    const next = url.toString();
                    if (next !== src) {
                        el.src = next;
                        return true;
                    }
                } catch (_) {}
                return false;
            }

            /** After iframe src change or full reload, tear down stale YT API bridge so initYouTubePlayer can run again. */
            function resetYouTubePlayerBridge(iframe) {
                if (!iframe) return;
                try {
                    const p = iframe.__canopyMiniYTPlayer;
                    if (p && typeof p.destroy === 'function') {
                        p.destroy();
                    }
                } catch (_) {}
                if (iframe.__canopyMiniYTDockRestoreTimer) {
                    try {
                        clearInterval(iframe.__canopyMiniYTDockRestoreTimer);
                    } catch (_) {}
                    delete iframe.__canopyMiniYTDockRestoreTimer;
                }
                delete iframe.__canopyMiniYTPlayer;
                delete iframe.__canopyMiniYTReady;
                delete iframe.__canopyMiniYTReadyInit;
                delete iframe.__canopyMiniYTFailed;
                iframe.__canopyMiniYTState = -1;
            }

            /** True when the embed wrapper currently lives in sidebar mini or deck stage (DOM stays in-page; avoid forced iframe reload). */
            function isSidebarDeckOrMiniHost(node) {
                return !!(node && (node === deckStage || node === miniVideoHost));
            }

            /**
             * Snapshot time/play state; optionally sync embed URL before reparenting.
             * In-page moves usually preserve the iframe document, so rewriting src + resetting the
             * YT bridge is more harmful than helpful there. Only reload when a caller explicitly
             * asks for it by leaving skipResumeUrlRewrite false.
             */
            function prepareYouTubeEmbedForHostMove(el, opts) {
                const options = opts && typeof opts === 'object' ? opts : {};
                const iframe = resolveYouTubeMediaElement(el, { activate: false });
                if (!iframe || iframe.tagName.toLowerCase() !== 'iframe') return;
                releaseFocusedYouTubeFrame(iframe);
                const t = getYouTubeCurrentTimeSafe(iframe);
                let playing = isYouTubePlayingState(Number(iframe.__canopyMiniYTState));
                try {
                    const p = iframe.__canopyMiniYTPlayer;
                    if (p && typeof p.getPlayerState === 'function') {
                        playing = isYouTubePlayingState(Number(p.getPlayerState()));
                    }
                } catch (_) {}
                iframe.__canopyMiniYTDockResumeAt = t;
                iframe.__canopyMiniYTDockShouldResume = playing;
                if (t > 0.5) {
                    iframe.__canopyMiniYTLastTime = t;
                }
                if (options.skipResumeUrlRewrite === true) {
                    return;
                }
                const srcChanged = setYouTubeDockResumeParams(
                    iframe,
                    Number(iframe.__canopyMiniYTDockResumeAt || 0),
                    iframe.__canopyMiniYTDockShouldResume === true
                );
                if (srcChanged) {
                    resetYouTubePlayerBridge(iframe);
                    initYouTubePlayer(iframe);
                }
            }

            function maybeRestoreYouTubeDockState(el) {
                if (!el) return;
                const hasDockState =
                    Object.prototype.hasOwnProperty.call(el, '__canopyMiniYTDockResumeAt') ||
                    Object.prototype.hasOwnProperty.call(el, '__canopyMiniYTDockShouldResume');
                if (!hasDockState) return;
                const resumeAt = Number(el.__canopyMiniYTDockResumeAt || 0);
                const shouldResume = el.__canopyMiniYTDockShouldResume === true;
                if (!resumeAt && !shouldResume) return;

                let attempts = 0;
                if (el.__canopyMiniYTDockRestoreTimer) {
                    clearInterval(el.__canopyMiniYTDockRestoreTimer);
                }
                el.__canopyMiniYTDockRestoreTimer = setInterval(() => {
                    attempts += 1;
                    try {
                        const player = el.__canopyMiniYTPlayer;
                        if (!player || typeof player.getPlayerState !== 'function') {
                            if (attempts > 8) clearInterval(el.__canopyMiniYTDockRestoreTimer);
                            return;
                        }
                        if (resumeAt > 1 && typeof player.seekTo === 'function') {
                            const current = getYouTubeCurrentTimeSafe(el);
                            if (!Number.isFinite(current) || Math.abs(current - resumeAt) > 1.5) {
                                player.seekTo(resumeAt, true);
                            }
                        }
                        if (shouldResume && typeof player.playVideo === 'function') {
                            player.playVideo();
                        }
                        const stateNow = Number(player.getPlayerState());
                        const currentNow = getYouTubeCurrentTimeSafe(el);
                        const timeOk = resumeAt <= 1 || Math.abs(currentNow - resumeAt) <= 1.5;
                        const stateOk = !shouldResume || stateNow === 1 || stateNow === 3;
                        if ((timeOk && stateOk) || attempts > 8) {
                            clearYouTubeDockResumeState(el);
                        }
                    } catch (_) {
                        if (attempts > 8) {
                            clearYouTubeDockResumeState(el);
                        }
                    }
                }, 350);
            }

            function releaseFocusedYouTubeFrame(el) {
                const iframe = resolveYouTubeMediaElement(el, { activate: false });
                if (!iframe || iframe.tagName.toLowerCase() !== 'iframe') return;
                try {
                    if (document.activeElement === iframe && typeof iframe.blur === 'function') {
                        iframe.blur();
                    }
                } catch (_) {}
            }

            function moveFocusOutOfDeck() {
                if (!deck || !document || !document.activeElement) return;
                const active = document.activeElement;
                if (!deck.contains(active)) return;
                const sourceEl = firstConnectedDeckAnchor(state.deckSourceEl, state.deckOriginSourceEl);
                const sourceLauncher = sourceEl && sourceEl.querySelector
                    ? sourceEl.querySelector('[data-open-media-deck], [data-open-mini-player]')
                    : null;
                const focusTarget = sourceLauncher || expandBtn || document.body;
                try {
                    if (typeof active.blur === 'function') active.blur();
                } catch (_) {}
                try {
                    if (focusTarget && typeof focusTarget.focus === 'function') {
                        focusTarget.focus({ preventScroll: true });
                    }
                } catch (_) {}
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

            function setCurrent(el, forcedType, opts) {
                if (!el) return;
                const options = opts && typeof opts === 'object' ? opts : {};
                const type = forcedType || mediaTypeFor(el);
                if (!type) return;
                if (type === 'youtube') {
                    const defer = options.deferYouTubeMaterialize === true;
                    el = resolveYouTubeMediaElement(el, { activate: !defer, autoplay: false }) || el;
                }

                if (state.current && state.current.el === el && state.current.type === type) {
                    scheduleMiniUpdate();
                    return;
                }

                if (type === 'youtube' && state.current && state.current.type === 'youtube') {
                    const curWrapper = getMediaDockWrapper(state.current.el, 'youtube');
                    const newWrapper = getMediaDockWrapper(el, 'youtube');
                    if (curWrapper && newWrapper && curWrapper === newWrapper) {
                        state.current.el = el;
                        scheduleMiniUpdate();
                        return;
                    }
                }

                if (state.current && state.current.el && state.current.el !== el) {
                    deactivateMediaEntry(state.current);
                }

                let nextSourceEl = sourceContainer(el);
                if (!nextSourceEl && state.deckOpen) {
                    nextSourceEl = firstConnectedDeckAnchor(
                        state.deckOriginSourceEl,
                        state.deckSourceEl,
                        state.current && state.current.sourceEl
                    );
                }
                state.current = {
                    el: el,
                    type: type,
                    sourceEl: nextSourceEl,
                    activatedAt: Date.now()
                };
                state.dismissedEl = null;
                if (type === 'youtube' && miniVideoHost && !state.deckOpen && !isDockedInMiniHost(el) && isOffscreen(el)) {
                    autoDockYouTube(el);
                }
                updateSourceDeckLauncherActiveStates();
                scheduleMiniUpdate();
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

            function undockYouTube(el) {
                if (!el) return;
                restoreDockedMedia(el, { preferMini: false });
                if (!state.returnUrl) state.dockedSubtitle = null;
                if (miniVideoHost) {
                    miniVideoHost.style.display = 'none';
                    miniVideoHost.innerHTML = '';
                }
            }

            function autoDockYouTube(el) {
                if (!miniVideoHost) return;
                if (isDockedInMiniHost(el)) return;
                el.__canopyMiniYTDockResumeAt = getYouTubeCurrentTimeSafe(el);
                el.__canopyMiniYTDockShouldResume = isYouTubePlayingState(Number(el.__canopyMiniYTState));
                setYouTubeDockResumeParams(
                    el,
                    Number(el.__canopyMiniYTDockResumeAt || 0),
                    el.__canopyMiniYTDockShouldResume === true
                );
                if (!state.dockedSubtitle) state.dockedSubtitle = sourceSubtitle(el);
                moveDockedMediaToHost(el, miniVideoHost);
                maybeRestoreYouTubeDockState(el);
            }

            function updateMini() {
                if (!state.current) {
                    if (state.deckOpen && getDeckSelectedItem()) {
                        updateDeckPanel();
                        hideMini();
                        return;
                    }
                    const fallback = findPlayingElement();
                    if (fallback) {
                        setCurrent(fallback);
                        return;
                    }
                    if (state.deckOpen) closeMediaDeck({ forceClose: true });
                    hideMini();
                    return;
                }
                if (!state.current.el || !state.current.el.isConnected) {
                    if (!repairMediaCurrentReference()) {
                        const staleCurrent = state.current;
                        state.current = null;
                        deactivateMediaEntry(staleCurrent);
                        const fallback = findPlayingElement();
                        if (fallback) {
                            setCurrent(fallback);
                            return;
                        }
                        if (state.deckOpen) closeMediaDeck({ forceClose: true });
                        hideMini();
                        return;
                    }
                } else {
                    ensureMediaSourceLinked();
                }

                const current = state.current;
                const type = current.type;
                const el = current.el;
                const isDocked = isDockedInMiniHost(el);
                const isResumablePause = (type === 'audio' || type === 'video') && !!el.paused && !el.ended;

                if (state.dismissedEl && state.dismissedEl === el) {
                    if (state.deckOpen) closeMediaDeck({ forceClose: true });
                    hideMini();
                    return;
                }

                if (state.deckOpen) {
                    updateDeckPanel();
                    hideMini();
                    return;
                }

                if (isDocked && type === 'youtube') {
                    const ytIframe = resolveYouTubeMediaElement(el, { activate: false });
                    const ytState = Number(ytIframe && ytIframe.__canopyMiniYTState);
                    const ytWrap = getMediaDockWrapper(el, 'youtube');
                    const hasIframe = !!(ytWrap && ytWrap.querySelector('iframe'));
                    if (hasIframe && ytState === 0) {
                        if (state.deckOpen) closeMediaDeck({ forceClose: true });
                        hideMini();
                        return;
                    }
                } else if (!isElementPlaying(el, type) && !isResumablePause) {
                    if (isDocked && type === 'youtube' && isYouTubeFacadeOnly(el)) {
                        // Static preview in mini — keep chrome visible until user taps Play.
                    } else {
                        const fallback = findPlayingElement();
                        if (fallback && fallback !== el) {
                            setCurrent(fallback);
                            return;
                        }
                        if (state.deckOpen) closeMediaDeck({ forceClose: true });
                        hideMini();
                        return;
                    }
                }

                if (!isDocked && !isOffscreen(el)) {
                    if (state.dismissedEl === el) state.dismissedEl = null;
                    if (state.deckOpen) {
                        updateDeckPanel();
                    }
                    hideMini();
                    return;
                }

                if (miniVideoHost) {
                    miniVideoHost.style.display = isDockedInMiniHost(el) ? 'block' : 'none';
                }

                const mediaTitle = titleFromMedia(el, type);
                const subtitle = state.dockedSubtitle || sourceSubtitle(el);
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
                } else if (type === 'youtube') {
                    playBtn.style.display = '';
                    if (isYouTubeFacadeOnly(el)) {
                        playBtn.innerHTML = '<i class="bi bi-play-fill me-1"></i><span>Play</span>';
                        progressWrap.classList.remove('show');
                        progressBar.style.width = '0%';
                        timeEl.textContent = 'YouTube';
                    } else {
                        const ytIframe = resolveYouTubeMediaElement(el, { activate: false });
                        const ytPlayer = ytIframe && ytIframe.__canopyMiniYTPlayer;
                        const ytState = Number(ytIframe && ytIframe.__canopyMiniYTState);
                        const isPaused = ytState === 2;
                        playBtn.innerHTML = `<i class="bi bi-${isPaused ? 'play-fill' : 'pause-fill'} me-1"></i><span>${isPaused ? 'Play' : 'Pause'}</span>`;
                        progressWrap.classList.remove('show');
                        progressBar.style.width = '0%';
                        if (ytPlayer && typeof ytPlayer.getCurrentTime === 'function') {
                            try {
                                const cur = ytPlayer.getCurrentTime();
                                const dur = ytPlayer.getDuration();
                                if (dur > 0) {
                                    const pct = Math.max(0, Math.min(100, (cur / dur) * 100));
                                    progressWrap.classList.add('show');
                                    progressBar.style.width = `${pct}%`;
                                    timeEl.textContent = `${formatTime(cur)} / ${formatTime(dur)}`;
                                    if (ytIframe) ytIframe.__canopyMiniYTLastTime = cur;
                                } else {
                                    timeEl.textContent = 'YouTube';
                                }
                            } catch (_) {
                                timeEl.textContent = 'YouTube';
                            }
                        } else {
                            timeEl.textContent = 'YouTube';
                        }
                    }
                    updatePiPButton(null, '');
                } else {
                    playBtn.style.display = 'none';
                    progressWrap.classList.remove('show');
                    progressBar.style.width = '0%';
                    timeEl.textContent = 'YouTube';
                    updatePiPButton(null, '');
                }

                if (expandBtn) {
                    expandBtn.disabled = false;
                    expandBtn.innerHTML = state.deckOpen
                        ? '<i class="bi bi-arrows-angle-contract"></i>'
                        : '<i class="bi bi-arrows-angle-expand"></i>';
                    expandBtn.title = state.deckOpen ? 'Collapse Canopy deck' : 'Open Canopy deck';
                }

                showMini();
                updateDeckPanel();
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
                    el.addEventListener('pause', () => scheduleMiniUpdate(60));
                    el.addEventListener('ended', () => scheduleMiniUpdate(60));
                    el.addEventListener('timeupdate', scheduleMiniUpdate);
                    el.addEventListener('seeking', scheduleMiniUpdate);
                    if (type === 'video') {
                        el.addEventListener('enterpictureinpicture', () => scheduleMiniUpdate(20));
                        el.addEventListener('leavepictureinpicture', () => scheduleMiniUpdate(20));
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

            if (typeof window !== 'undefined') {
                window.canopyRegisterMediaNode = registerMediaNode;
            }

            function scanForMedia(scope) {
                const root = scope || document;
                root.querySelectorAll('audio, video, .youtube-embed iframe').forEach(registerMediaNode);
                syncSourceMediaDeckLaunchersInScope(root);
            }

            function jumpToCurrentSource() {
                const selectedItem = getDeckSelectedItem();
                const deckJumpAnchor = firstConnectedDeckAnchor(state.deckSourceEl, state.deckOriginSourceEl);
                if (!state.current && !selectedItem && !deckJumpAnchor) return;
                if (!state.current || !state.current.el || !isDeckMediaItem(selectedItem)) {
                    jumpToDeckItemSource(selectedItem || { sourceEl: deckJumpAnchor }, { forceClose: true });
                    hideMini();
                    return;
                }
                const el = state.current.el;

                if (state.deckOpen) {
                    closeMediaDeck({ forceClose: true });
                }

                if (el.__canopyAutoDockPlaceholder || el.__canopyMiniVideoPlaceholder) {
                    restoreDockedMedia(el, { preferMini: false });
                    state.dismissedEl = null;
                    state.dockedSubtitle = null;
                    const container = sourceContainer(el);
                    const scrollTarget = container && container.isConnected ? container : el;
                    if (scrollTarget.isConnected) {
                        scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
                        applyFocusFlash(scrollTarget);
                    }
                    hideMini();
                    return;
                }

                if (state.returnUrl) {
                    window.location.href = state.returnUrl;
                    return;
                }

                const target = state.current.sourceEl && state.current.sourceEl.isConnected
                    ? state.current.sourceEl
                    : null;
                if (target) {
                    target.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'nearest' });
                    applyFocusFlash(target);
                }
                hideMini();
            }

            /**
             * Open the Canopy media deck for a feed post by id (e.g. antecedent of a repost/variant).
             * Open first (same as inline posts). If empty queue, run layout compositor and retry once + rAF.
             * No full-page navigation when the card is already in the DOM.
             */
            function openDeckForFeedAntecedentPost(sourcePostId) {
                const pid = String(sourcePostId || '').trim();
                if (!pid) return false;
                const esc = window.CSS && typeof window.CSS.escape === 'function'
                    ? window.CSS.escape(pid)
                    : pid.replace(/["\\]/g, '\\$&');
                const card = document.querySelector(`.post-card[data-post-id="${esc}"]`);
                if (!card || !card.isConnected) {
                    try {
                        window.location.href = `/feed?focus_post=${encodeURIComponent(pid)}&open_deck=1`;
                    } catch (_) {
                        /* ignore */
                    }
                    return false;
                }

                function tryOpenFromCard() {
                    try {
                        openMediaDeckForSource(card, {});
                    } catch (_) {
                        /* ignore */
                    }
                    if (state.deckOpen) return true;
                    const modHost = card.querySelector('[data-canopy-module-card="1"], [data-canopy-widget-manifest]');
                    if (modHost) {
                        try {
                            openMediaDeckForManifestNode(modHost);
                        } catch (_) {
                            /* ignore */
                        }
                    }
                    if (state.deckOpen) return true;
                    const bundleHost = card.querySelector('[data-canopy-module-bundle-id]');
                    if (bundleHost) {
                        try {
                            openMediaDeckForManifestNode(bundleHost);
                        } catch (_) {
                            /* ignore */
                        }
                    }
                    return !!state.deckOpen;
                }

                if (tryOpenFromCard()) return true;

                if (typeof window.canopyApplySourceLayoutsInScope === 'function') {
                    try {
                        window.canopyApplySourceLayoutsInScope(card);
                    } catch (_) {
                        /* ignore */
                    }
                }
                if (tryOpenFromCard()) return true;

                requestAnimationFrame(() => {
                    if (typeof window.canopyApplySourceLayoutsInScope === 'function') {
                        try {
                            window.canopyApplySourceLayoutsInScope(card);
                        } catch (_) {
                            /* ignore */
                        }
                    }
                    if (tryOpenFromCard()) return;
                    requestAnimationFrame(() => {
                        tryOpenFromCard();
                    });
                });
                return true;
            }

            /**
             * Open the Canopy deck for a channel message by id (e.g. antecedent of repost/variant).
             * Same strategy as openDeckForFeedAntecedentPost (no forced navigate when row is in DOM).
             *
             * Channel messages often carry `data-canopy-source-layout` on `.message-content`; the compositor
             * moves attachments into a shell. Opening the deck *before* `canopyApplySourceLayoutsInScope` runs
             * yields an empty item list — so we always sync layout + media launchers first, then try the same
             * controls users click (layout "Open deck" + playback launcher).
             */
            function openDeckForChannelAntecedentMessage(sourceMessageId) {
                const mid = String(sourceMessageId || '').trim();
                if (!mid) return false;
                const esc = window.CSS && typeof window.CSS.escape === 'function'
                    ? window.CSS.escape(mid)
                    : mid.replace(/["\\]/g, '\\$&');
                const row = document.querySelector(`.message-item[data-message-id="${esc}"]`);
                if (!row || !row.isConnected) {
                    try {
                        window.location.href = `/channels/locate?message_id=${encodeURIComponent(mid)}&open_deck=1`;
                    } catch (_) {
                        /* ignore */
                    }
                    return false;
                }

                function syncRowForDeck() {
                    if (typeof window.canopyApplySourceLayoutsInScope === 'function') {
                        try {
                            window.canopyApplySourceLayoutsInScope(row);
                        } catch (_) {
                            /* ignore */
                        }
                    }
                    /* Facades have no iframe until click — deck scan/queue needs real iframes to dock reliably. */
                    try {
                        row.querySelectorAll('.youtube-embed .yt-facade').forEach((facade) => {
                            try {
                                materializeYouTubeFacade(facade, { autoplay: false });
                            } catch (_) {
                                /* ignore */
                            }
                        });
                    } catch (_) {
                        /* ignore */
                    }
                    try {
                        scanForMedia(row);
                    } catch (_) {
                        /* ignore */
                    }
                    try {
                        syncSourceMediaDeckLaunchersInScope(row);
                    } catch (_) {
                        /* ignore */
                    }
                }

                /** Prefer the same code paths as inline UI (layout toolbar + injected launcher). */
                function tryOpenViaChannelDeckControls() {
                    try {
                        const layoutBtn = row.querySelector('.canopy-source-layout-deck-launch');
                        if (layoutBtn && typeof layoutBtn.click === 'function') {
                            layoutBtn.click();
                            if (state.deckOpen) return true;
                        }
                    } catch (_) {
                        /* ignore */
                    }
                    try {
                        const deckBtn = row.querySelector('[data-canopy-playback-launcher] [data-open-media-deck]');
                        if (deckBtn && typeof deckBtn.click === 'function') {
                            deckBtn.click();
                            if (state.deckOpen) return true;
                        }
                    } catch (_) {
                        /* ignore */
                    }
                    return false;
                }

                function tryOpenFromRow() {
                    syncRowForDeck();
                    if (tryOpenViaChannelDeckControls()) return true;
                    const roots = [
                        row,
                        row.querySelector('.message-content'),
                        row.querySelector('[data-canopy-source-ref="content:lede"]'),
                    ].filter((n) => n && n.nodeType === 1);
                    const uniqRoots = [];
                    roots.forEach((el) => {
                        if (uniqRoots.indexOf(el) < 0) uniqRoots.push(el);
                    });
                    for (let i = 0; i < uniqRoots.length; i += 1) {
                        try {
                            openMediaDeckForSource(uniqRoots[i], { play: true });
                        } catch (_) {
                            /* ignore */
                        }
                        if (state.deckOpen) return true;
                    }
                    const modHost = row.querySelector('[data-canopy-module-card="1"], [data-canopy-widget-manifest]');
                    if (modHost) {
                        try {
                            openMediaDeckForManifestNode(modHost);
                        } catch (_) {
                            /* ignore */
                        }
                    }
                    if (state.deckOpen) return true;
                    const bundleHost = row.querySelector('[data-canopy-module-bundle-id]');
                    if (bundleHost) {
                        try {
                            openMediaDeckForManifestNode(bundleHost);
                        } catch (_) {
                            /* ignore */
                        }
                    }
                    return !!state.deckOpen;
                }

                if (tryOpenFromRow()) return true;

                if (typeof window.canopyApplySourceLayoutsInScope === 'function') {
                    try {
                        window.canopyApplySourceLayoutsInScope(row);
                    } catch (_) {
                        /* ignore */
                    }
                }
                if (tryOpenFromRow()) return true;

                requestAnimationFrame(() => {
                    if (tryOpenFromRow()) return;
                    requestAnimationFrame(() => {
                        tryOpenFromRow();
                    });
                });
                return true;
            }

            if (typeof window !== 'undefined') {
                window.openMediaDeckForManifestNode = openMediaDeckForManifestNode;
                window.openDeckForFeedAntecedentPost = openDeckForFeedAntecedentPost;
                window.openDeckForChannelAntecedentMessage = openDeckForChannelAntecedentMessage;
            }

            if (playBtn) {
                playBtn.addEventListener('click', () => {
                    if (!state.current || !state.current.el) return;
                    const el = state.current.el;
                    const type = state.current.type;
                    if (type === 'audio' || type === 'video') {
                        try {
                            if (el.paused) el.play(); else el.pause();
                        } catch (_) {}
                    } else if (type === 'youtube') {
                        try {
                            const ytEl = resolveYouTubeMediaElement(el, { activate: false });
                            const player = ytEl && ytEl.__canopyMiniYTPlayer;
                            if (player && typeof player.getPlayerState === 'function') {
                                const s = player.getPlayerState();
                                if (s === 1 || s === 3) {
                                    pauseMediaElement(el, type);
                                } else {
                                    playMediaElement(el, type);
                                }
                            } else {
                                playMediaElement(el, type);
                            }
                        } catch (_) {}
                    }
                    scheduleMiniUpdate(50);
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
                    scheduleMiniUpdate(50);
                });
            }

            if (closeBtn) {
                closeBtn.addEventListener('click', () => {
                    stopActiveMediaPlayback();
                });
            }

            if (pinBtn) {
                pinBtn.addEventListener('click', () => {
                    setCanopySidebarMiniPosition(canopySidebarRailState.miniPosition === 'bottom' ? 'top' : 'bottom');
                });
            }

            if (expandBtn) {
                expandBtn.addEventListener('click', () => {
                    if (!state.current) return;
                    if (state.deckOpen) closeMediaDeck();
                    else openMediaDeck();
                });
            }

            if (deckMinimizeBtn) {
                deckMinimizeBtn.addEventListener('click', () => closeDeckShell());
            }

            if (deckMiniPlayerBtn) {
                deckMiniPlayerBtn.addEventListener('click', () => switchDeckToMiniPlayer());
            }

            if (deckLargeToggleBtn) {
                deckLargeToggleBtn.addEventListener('click', () => cycleDeckDesktopMode());
            }

            if (deckMinimizeFooterBtn) {
                deckMinimizeFooterBtn.addEventListener('click', () => closeDeckShell());
            }

            if (deckMiniFooterBtn) {
                deckMiniFooterBtn.addEventListener('click', () => switchDeckToMiniPlayer());
            }

            if (deckCloseBtn) {
                deckCloseBtn.addEventListener('click', () => closeDeckShell({ forceClose: true }));
            }

            if (deckSourceTab) {
                deckSourceTab.addEventListener('click', () => {
                    if (sourceDeckHasRenderableState()) {
                        setDeckShellMode('source');
                        updateDeckPanel();
                    }
                });
            }

            if (deckInboxTab) {
                deckInboxTab.addEventListener('click', () => {
                    if (state.deckInboxActive) {
                        setDeckShellMode('dm');
                    }
                });
            }

            if (deckQueueToggle) {
                deckQueueToggle.addEventListener('click', () => {
                    setDeckQueueCollapsed(!state.deckQueueCollapsed);
                });
            }

            if (deckDetailToggle) {
                deckDetailToggle.addEventListener('click', () => {
                    setDeckDetailCollapsed(!state.deckDetailCollapsed);
                });
            }

            if (deckBackdrop) {
                deckBackdrop.addEventListener('click', () => closeDeckShell());
            }

            if (deckPrevBtn) {
                deckPrevBtn.addEventListener('click', () => playDeckRelative(-1));
            }

            if (deckNextBtn) {
                deckNextBtn.addEventListener('click', () => playDeckRelative(1));
            }

            if (deckPlayBtn) {
                deckPlayBtn.addEventListener('click', () => {
                    if (!state.current || !state.current.el) return;
                    const el = state.current.el;
                    const type = state.current.type;
                    if (type === 'audio' || type === 'video') {
                        try {
                            if (el.paused) playMediaElement(el, type); else pauseMediaElement(el, type);
                        } catch (_) {}
                    } else if (type === 'youtube') {
                        try {
                            const player = el.__canopyMiniYTPlayer;
                            if (player) {
                                const s = player.getPlayerState();
                                if (s === 1 || s === 3) pauseMediaElement(el, type);
                                else playMediaElement(el, type);
                            } else {
                                playMediaElement(el, type);
                            }
                        } catch (_) {}
                    }
                    scrollDeckStageIntoView();
                    scheduleMiniUpdate(50);
                });
            }

            if (deckReturnBtn) {
                deckReturnBtn.addEventListener('click', () => jumpToCurrentSource());
            }

            document.querySelectorAll('[data-canopy-stop-active-media-nav]').forEach((link) => {
                if (link.dataset.boundCanopyStopMediaNav === '1') return;
                link.dataset.boundCanopyStopMediaNav = '1';
                link.addEventListener('click', () => {
                    stopActiveMediaPlayback({ clearDismissed: true });
                }, true);
            });

            if (deckPipBtn) {
                deckPipBtn.addEventListener('click', async () => {
                    if (!state.current || !state.current.el || state.current.type !== 'video') {
                        jumpToCurrentSource();
                        return;
                    }
                    const el = state.current.el;
                    if (!supportsPictureInPicture(el)) {
                        if (typeof showAlert === 'function') {
                            showAlert('Picture-in-Picture is not available for this video here.', 'info');
                        }
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
                    scheduleMiniUpdate(50);
                });
            }

            if (deckSeek) {
                deckSeek.addEventListener('input', () => {
                    state.deckSeeking = true;
                    if (!state.current || !state.current.el) return;
                    const ratio = Math.max(0, Math.min(1, Number(deckSeek.value || 0) / 1000));
                    let duration = 0;
                    if (state.current.type === 'audio' || state.current.type === 'video') {
                        duration = Number(state.current.el.duration || 0);
                    } else if (state.current.type === 'youtube') {
                        try {
                            const player = state.current.el.__canopyMiniYTPlayer;
                            duration = Number(player && typeof player.getDuration === 'function' ? player.getDuration() : 0);
                        } catch (_) {}
                    }
                    const previewTime = duration > 0 ? duration * ratio : 0;
                    if (deckCurrentTime) deckCurrentTime.textContent = formatTime(previewTime);
                });
                deckSeek.addEventListener('change', () => {
                    const ratio = Math.max(0, Math.min(1, Number(deckSeek.value || 0) / 1000));
                    seekCurrentMediaTo(ratio);
                    state.deckSeeking = false;
                });
                deckSeek.addEventListener('pointerup', () => {
                    state.deckSeeking = false;
                });
            }

            if (deckQueue) {
                deckQueue.addEventListener('click', (event) => {
                    const btn = event.target && event.target.closest ? event.target.closest('.sidebar-media-deck-item[data-media-index]') : null;
                    if (!btn) return;
                    const key = String(btn.getAttribute('data-media-key') || '').trim();
                    const nextItem = key
                        ? state.deckItems.find((item) => String(item && item.key || '').trim() === key)
                        : null;
                    if (!nextItem) return;
                    selectDeckItem(nextItem, { play: true });
                    scrollDeckStageIntoView();
                    scheduleMiniUpdate(20);
                });
            }

            const observerRoot = mainScroller || null;
            state.observer = new IntersectionObserver((entries) => {
                entries.forEach((entry) => {
                    const visible = entry.isIntersecting && entry.intersectionRatio > 0.2;
                    entry.target.__canopyMiniVisible = visible;
                    if (
                        state.current &&
                        state.current.type === 'youtube' &&
                        state.current.el === entry.target &&
                        !visible &&
                        miniVideoHost &&
                        !state.deckOpen &&
                        !isDockedInMiniHost(entry.target) &&
                        isYouTubePlayingState(Number(entry.target.__canopyMiniYTState))
                    ) {
                        autoDockYouTube(entry.target);
                    }
                });
                scheduleMiniUpdate();
            }, {
                root: observerRoot,
                threshold: [0, 0.2, 0.4, 0.7, 1]
            });

            scanForMedia(document);
            applySourceLayoutsInScope(document);

            const mutationRoot = mainScroller || document.body;
            state.mutationObserver = new MutationObserver((mutations) => {
                const dirtySources = new Set();
                function addDirtySource(node) {
                    if (!(node instanceof Element)) return;
                    const source = node.matches && node.matches('.post-card[data-post-id], .message-item[data-message-id]')
                        ? node
                        : node.closest ? node.closest('.post-card[data-post-id], .message-item[data-message-id]') : null;
                    if (source) dirtySources.add(source);
                }
                mutations.forEach((mutation) => {
                    addDirtySource(mutation.target);
                    mutation.addedNodes.forEach((node) => {
                        if (!(node instanceof Element)) return;
                        addDirtySource(node);
                        if (node.matches && (node.matches('audio') || node.matches('video') || node.matches('.youtube-embed iframe'))) {
                            registerMediaNode(node);
                        }
                        if (node.querySelectorAll) {
                            scanForMedia(node);
                        }
                    });
                    mutation.removedNodes.forEach((node) => {
                        if (!(node instanceof Element)) return;
                        addDirtySource(mutation.target);
                    });
                });
                dirtySources.forEach((source) => {
                    if (state.deckOpen && state.current && state.current.sourceEl === source) return;
                    applySourceLayoutsInScope(source);
                    syncSourceMediaDeckLauncher(source);
                });
                updateSourceDeckLauncherActiveStates();
                scheduleMiniUpdate();
            });
            state.mutationObserver.observe(mutationRoot, { childList: true, subtree: true });

            window.addEventListener('resize', () => {
                applyDeckDesktopMode(state.deckDesktopMode, { silent: true });
                scheduleMiniUpdate();
            });
            window.addEventListener('canopy:sidebar-state-changed', () => {
                applyDeckDesktopMode(state.deckDesktopMode, { silent: true });
                window.setTimeout(() => applyDeckDesktopMode(state.deckDesktopMode, { silent: true }), 340);
            });
            document.addEventListener('visibilitychange', scheduleMiniUpdate);
            document.addEventListener('keydown', (event) => {
                if (!state.deckOpen && !state.deckInboxActive) return;
                if (event.key === 'Escape') {
                    closeDeckShell();
                }
            });
            state.tickHandle = setInterval(scheduleMiniUpdate, 700);
            scheduleMiniUpdate();

            window.canopyPersistActiveMedia = function() {
                if (!state.current || !state.current.el) return;
                const el = state.current.el;
                const type = state.current.type;
                const sourceEl = firstConnectedDeckAnchor(
                    state.current.sourceEl,
                    state.deckOriginSourceEl,
                    state.deckSourceEl
                );
                if (sourceEl && sourceEl.isConnected) {
                    state.current.sourceEl = sourceEl;
                    if (!state.deckOriginSourceEl || !state.deckOriginSourceEl.isConnected) {
                        state.deckOriginSourceEl = sourceEl;
                    }
                    if (!state.deckSourceEl || !state.deckSourceEl.isConnected) {
                        state.deckSourceEl = sourceEl;
                    }
                    pinDeckOriginIdsFromSourceEl(sourceEl);
                }
                if (state.deckOpen) {
                    if (state.persistMediaRetryHandle) {
                        clearInterval(state.persistMediaRetryHandle);
                        state.persistMediaRetryHandle = null;
                    }
                    if ((type === 'youtube' || type === 'video') && deckStage && state.current && state.current.el) {
                        moveDockedMediaToHost(state.current.el, deckStage);
                    }
                    return;
                }
                if (type !== 'youtube' || !miniVideoHost) return;
                if (!shouldPersistActiveYouTube(el)) {
                    clearYouTubeDockResumeState(el);
                    state.returnUrl = null;
                    state.dockedSubtitle = null;
                    if (isDockedInMiniHost(el)) {
                        miniVideoHost.style.display = 'none';
                        miniVideoHost.innerHTML = '';
                    }
                    hideMini();
                    return;
                }

                if (!state.dockedSubtitle) state.dockedSubtitle = sourceSubtitle(el);
                const messageId = sourceEl && sourceEl.getAttribute('data-message-id');
                state.returnUrl = messageId
                    ? '/channels/locate?message_id=' + encodeURIComponent(messageId)
                    : window.location.pathname + window.location.search;

                if (el.__canopyAutoDockPlaceholder) {
                    var ph = el.__canopyAutoDockPlaceholder;
                    if (ph.isConnected) ph.remove();
                    delete el.__canopyAutoDockPlaceholder;
                }
                var ytWrapperForPlaceholder = el.closest ? el.closest('.youtube-embed') : null;
                if (ytWrapperForPlaceholder && ytWrapperForPlaceholder !== el && ytWrapperForPlaceholder.__canopyAutoDockPlaceholder) {
                    var wrapperPlaceholder = ytWrapperForPlaceholder.__canopyAutoDockPlaceholder;
                    if (wrapperPlaceholder.isConnected) wrapperPlaceholder.remove();
                    delete ytWrapperForPlaceholder.__canopyAutoDockPlaceholder;
                }

                if (!isDockedInMiniHost(el)) {
                    var wrapper = el.closest('.youtube-embed');
                    if (wrapper) {
                        miniVideoHost.innerHTML = '';
                        miniVideoHost.appendChild(wrapper);
                        miniVideoHost.style.display = 'block';
                    }
                }

                try {
                    var player = el.__canopyMiniYTPlayer;
                    if (player && typeof player.playVideo === 'function') {
                        player.playVideo();
                    }
                } catch (_) {}

                if (state.persistMediaRetryHandle) {
                    clearInterval(state.persistMediaRetryHandle);
                    state.persistMediaRetryHandle = null;
                }
                function clearPersistRetry() {
                    clearInterval(state.persistMediaRetryHandle);
                    state.persistMediaRetryHandle = null;
                }
                var retries = 0;
                state.persistMediaRetryHandle = setInterval(function() {
                    retries++;
                    if (retries > 6) { clearPersistRetry(); return; }
                    try {
                        var p = el.__canopyMiniYTPlayer;
                        if (p && typeof p.getPlayerState === 'function') {
                            var s = p.getPlayerState();
                            if (s === 1 || s === 3) { clearPersistRetry(); return; }
                            p.playVideo();
                        }
                    } catch (_) { clearPersistRetry(); }
                }, 800);
            };
        }
        
        // Three-state Sidebar Toggle Functionality with Mobile Support
        function initSidebarToggle() {
            const toggleBtn = document.getElementById('sidebar-toggle');
            const sidebarContainer = document.getElementById('sidebar-container');
            const sidebar = document.getElementById('main-sidebar');
            const contentContainer = document.getElementById('content-container');
            const mobileBackdrop = document.getElementById('mobile-backdrop');
            const mobileSidebarQuery = window.matchMedia ? window.matchMedia('(max-width: 576px)') : null;
            
            if (!toggleBtn || !sidebarContainer || !sidebar || !contentContainer) {
                return;
            }
            
            // Sidebar states: 'expanded', 'collapsed', 'hidden'
            const desktopStorageKey = 'sidebar-state';
            const mobileStorageKey = 'sidebar-state-mobile';
            const validSidebarStates = new Set(['expanded', 'collapsed', 'hidden']);

            function isMobileSidebarMode() {
                return mobileSidebarQuery ? mobileSidebarQuery.matches : window.innerWidth <= 576;
            }

            function normalizeSidebarStateForViewport(state) {
                const normalized = validSidebarStates.has(state) ? state : 'expanded';
                if (isMobileSidebarMode()) {
                    return normalized === 'expanded' ? 'expanded' : 'hidden';
                }
                return normalized;
            }

            function readSidebarState() {
                const storageKey = isMobileSidebarMode() ? mobileStorageKey : desktopStorageKey;
                const fallback = isMobileSidebarMode() ? 'hidden' : 'expanded';
                return normalizeSidebarStateForViewport(localStorage.getItem(storageKey) || fallback);
            }

            function saveSidebarState(state) {
                const storageKey = isMobileSidebarMode() ? mobileStorageKey : desktopStorageKey;
                localStorage.setItem(storageKey, normalizeSidebarStateForViewport(state));
            }

            let currentState = readSidebarState();

            function syncCompactNavTitles(state) {
                const compact = state === 'collapsed';
                sidebar.querySelectorAll('.nav-link[data-title]').forEach((link) => {
                    const compactTitle = String(link.getAttribute('data-title') || '').trim();
                    if (compact && compactTitle) {
                        if (!link.hasAttribute('data-expanded-title')) {
                            link.setAttribute('data-expanded-title', link.getAttribute('title') || '');
                        }
                        link.setAttribute('title', compactTitle);
                        link.setAttribute('aria-label', compactTitle);
                        return;
                    }

                    const previousTitle = link.getAttribute('data-expanded-title');
                    if (previousTitle !== null) {
                        if (previousTitle) {
                            link.setAttribute('title', previousTitle);
                        } else {
                            link.removeAttribute('title');
                        }
                        link.removeAttribute('data-expanded-title');
                    }
                    link.removeAttribute('aria-label');
                });
            }
            
            applySidebarState(currentState);
            
            // Toggle button click handler - cycles through states
            toggleBtn.addEventListener('click', function() {
                let newState;
                const isMobile = isMobileSidebarMode();
                
                if (isMobile) {
                    // Mobile is a drawer: visible or hidden. Avoid an invisible intermediate state.
                    newState = currentState === 'expanded' ? 'hidden' : 'expanded';
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
                saveSidebarState(newState);
            });
            
            // Mobile backdrop click handler
            if (mobileBackdrop) {
                mobileBackdrop.addEventListener('click', function() {
                    if (currentState === 'expanded') {
                        currentState = 'hidden';
                        applySidebarState('hidden');
                        saveSidebarState('hidden');
                        toggleBtn.focus();
                    }
                });
            }
            
            function applySidebarState(state) {
                const toggleIcon = toggleBtn.querySelector('i');
                const isMobile = isMobileSidebarMode();
                state = normalizeSidebarStateForViewport(state);
                
                // Clear all state classes
                sidebarContainer.classList.remove('expanded', 'collapsed', 'hidden');
                sidebar.classList.remove('expanded', 'collapsed', 'hidden');
                
                // Handle mobile backdrop
                if (mobileBackdrop) {
                    mobileBackdrop.classList.remove('show');
                    mobileBackdrop.setAttribute('aria-hidden', 'true');
                }
                
                switch(state) {
                    case 'expanded':
                        // Full sidebar with text
                        sidebarContainer.classList.add('expanded');
                        sidebar.classList.add('expanded');
                        toggleIcon.className = 'bi bi-list';
                        toggleBtn.setAttribute('title', isMobile ? 'Close navigation' : 'Collapse to Icons');
                        toggleBtn.setAttribute('aria-label', isMobile ? 'Close navigation' : 'Collapse to Icons');
                        toggleBtn.setAttribute('aria-expanded', 'true');

                        // Show backdrop on mobile
                        if (isMobile && mobileBackdrop) {
                            mobileBackdrop.classList.add('show');
                            mobileBackdrop.setAttribute('aria-hidden', 'false');
                        }
                        break;
                        
                    case 'collapsed':
                        // Icon-only sidebar
                        sidebarContainer.classList.add('collapsed');
                        sidebar.classList.add('collapsed');
                        toggleIcon.className = 'bi bi-chevron-left';
                        toggleBtn.setAttribute('title', 'Hide Sidebar');
                        toggleBtn.setAttribute('aria-label', 'Hide Sidebar');
                        toggleBtn.setAttribute('aria-expanded', 'false');
                        break;

                    case 'hidden':
                        // No sidebar
                        sidebarContainer.classList.add('hidden');
                        sidebar.classList.add('hidden');
                        toggleIcon.className = 'bi bi-chevron-right';
                        toggleBtn.setAttribute('title', isMobile ? 'Open navigation' : 'Show Sidebar');
                        toggleBtn.setAttribute('aria-label', isMobile ? 'Open navigation' : 'Show Sidebar');
                        toggleBtn.setAttribute('aria-expanded', 'false');
                        break;
                }

                document.body.classList.toggle('canopy-mobile-sidebar-open', isMobile && state === 'expanded');
                syncCompactNavTitles(state);

                // Update current state
                currentState = state;
                window.dispatchEvent(new CustomEvent('canopy:sidebar-state-changed', {
                    detail: { state }
                }));
            }
            
            // Handle window resize for responsive behavior
            window.addEventListener('resize', function() {
                const nextState = normalizeSidebarStateForViewport(currentState);
                if (nextState !== currentState) {
                    currentState = nextState;
                    saveSidebarState(nextState);
                }
                applySidebarState(currentState);
            });
            
            // Touch and swipe gestures for mobile
            if ('ontouchstart' in window) {
                let touchStartX = 0;
                let touchStartY = 0;
                let touchEndX = 0;
                let touchEndY = 0;
                let touchStartedInInteractive = false;
                let touchStartedInSidebar = false;
                let touchStartedOnBackdrop = false;
                let touchWasMultiTouch = false;
                
                document.addEventListener('touchstart', function(e) {
                    if (e.touches && e.touches.length > 1) {
                        touchWasMultiTouch = true;
                        return;
                    }
                    touchWasMultiTouch = false;
                    const touch = e.changedTouches && e.changedTouches[0];
                    if (!touch) return;
                    touchStartX = touch.clientX;
                    touchStartY = touch.clientY;
                    const target = e.target;
                    touchStartedInSidebar = Boolean(target && target.closest && target.closest('#sidebar-container'));
                    touchStartedOnBackdrop = target === mobileBackdrop;
                    touchStartedInInteractive = Boolean(target && target.closest && target.closest(
                        'input, textarea, select, button, a, [role="button"], .dropdown-menu, .modal, .canopy-media-deck-portal'
                    ));
                });
                
                document.addEventListener('touchend', function(e) {
                    const touch = e.changedTouches && e.changedTouches[0];
                    if (!touch) return;
                    touchEndX = touch.clientX;
                    touchEndY = touch.clientY;
                    handleSwipe();
                });
                
                function handleSwipe() {
                    if (!isMobileSidebarMode()) return;
                    if (touchWasMultiTouch) return;
                    if (touchStartedInInteractive) return;
                    const swipeThreshold = 50;
                    const swipeDistance = touchEndX - touchStartX;
                    const verticalDistance = Math.abs(touchEndY - touchStartY);
                    if (verticalDistance > Math.abs(swipeDistance) * 0.75) return;
                    
                    if (Math.abs(swipeDistance) > swipeThreshold) {
                        if (swipeDistance > 0 && touchStartX < 28) {
                            // Swipe right from left edge - show sidebar
                            if (currentState !== 'expanded') {
                                currentState = 'expanded';
                                applySidebarState('expanded');
                                saveSidebarState('expanded');
                            }
                        } else if (swipeDistance < 0 && currentState === 'expanded' && (touchStartedInSidebar || touchStartedOnBackdrop)) {
                            // Swipe left - hide sidebar
                            currentState = 'hidden';
                            applySidebarState('hidden');
                            saveSidebarState('hidden');
                        }
                    }
                }
            }
            
            // Add keyboard shortcuts
            document.addEventListener('keydown', function(e) {
                const key = String(e.key || '').toLowerCase();

                if (e.key === 'Escape' && isMobileSidebarMode() && currentState === 'expanded') {
                    currentState = 'hidden';
                    applySidebarState('hidden');
                    saveSidebarState('hidden');
                    toggleBtn.focus();
                    return;
                }

                // Ctrl+B or Cmd+B - cycle through states
                if ((e.ctrlKey || e.metaKey) && !e.shiftKey && key === 'b') {
                    e.preventDefault();
                    toggleBtn.click();
                }
                
                // Ctrl+Shift+B or Cmd+Shift+B - toggle between expanded and hidden
                if ((e.ctrlKey || e.metaKey) && e.shiftKey && key === 'b') {
                    e.preventDefault();
                    const newState = currentState === 'expanded' ? 'hidden' : 'expanded';
                    currentState = newState;
                    applySidebarState(newState);
                    saveSidebarState(newState);
                }
            });
            
            // Return the state management functions for external use
            return {
                getCurrentState: () => currentState,
                setState: (state) => {
                    if (validSidebarStates.has(state)) {
                        const nextState = normalizeSidebarStateForViewport(state);
                        currentState = nextState;
                        applySidebarState(nextState);
                        saveSidebarState(nextState);
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

        function requestRemoteAttachmentDownload(attachment, triggerEl) {
            const payload = (attachment && typeof attachment === 'object') ? attachment : null;
            if (!payload || !payload.origin_file_id || !payload.source_peer_id) {
                if (typeof showAlert === 'function') showAlert('Remote attachment metadata is incomplete.', 'warning');
                return Promise.resolve(false);
            }

            const button = triggerEl instanceof HTMLElement ? triggerEl : null;
            const originalHtml = button ? button.innerHTML : '';
            if (button) {
                button.disabled = true;
                button.innerHTML = '<i class="bi bi-hourglass-split"></i>';
            }

            return fetch('/ajax/files/request-remote-download', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '',
                },
                body: JSON.stringify({ attachment: payload }),
            })
                .then(async (response) => {
                    const data = await response.json().catch(() => ({}));
                    if (!response.ok || !data.success) {
                        throw new Error(data.error || 'Could not request download');
                    }
                    if (typeof showAlert === 'function') {
                        showAlert(
                            String(data.message || '').trim() || 'Large attachment download requested. It will appear when the transfer completes.',
                            data.queued ? 'info' : 'success'
                        );
                    }
                    return true;
                })
                .catch((err) => {
                    if (typeof showAlert === 'function') {
                        showAlert(err.message || 'Could not request download', 'danger');
                    }
                    return false;
                })
                .finally(() => {
                    if (button) {
                        button.disabled = false;
                        button.innerHTML = originalHtml;
                    }
                });
        }

        if (typeof window !== 'undefined') {
            window.openFileAccessInspector = openFileAccessInspector;
            window.requestRemoteAttachmentDownload = requestRemoteAttachmentDownload;
        }

        function applyWorkspaceOnboardingVisibility(root = document) {
            if (!root || !root.querySelectorAll) {
                return;
            }
            root.querySelectorAll('[data-workspace-onboarding="1"]').forEach((card) => {
                const dismissKey = String(card.getAttribute('data-dismiss-key') || '').trim();
                if (!dismissKey) {
                    return;
                }
                try {
                    if (window.localStorage && window.localStorage.getItem(dismissKey) === '1') {
                        card.style.display = 'none';
                    }
                } catch (err) {
                    console.debug('Workspace onboarding visibility check skipped', err);
                }
            });
        }

        function dismissWorkspaceOnboarding(buttonOrCard) {
            const source = buttonOrCard && buttonOrCard.nodeType ? buttonOrCard : null;
            const card = source ? source.closest('[data-workspace-onboarding="1"]') : null;
            if (!card) {
                return;
            }
            const dismissKey = String(card.getAttribute('data-dismiss-key') || '').trim();
            if (dismissKey) {
                try {
                    if (window.localStorage) {
                        window.localStorage.setItem(dismissKey, '1');
                    }
                } catch (err) {
                    console.debug('Workspace onboarding dismiss skipped', err);
                }
            }
            card.style.display = 'none';
        }

        if (typeof window !== 'undefined') {
            window.dismissWorkspaceOnboarding = dismissWorkspaceOnboarding;
            window.applyWorkspaceOnboardingVisibility = applyWorkspaceOnboardingVisibility;
        }

        // Initialize sidebar toggle when DOM is ready
        document.addEventListener('DOMContentLoaded', function() {
            initSidebarMediaMiniPlayer();
            initContentContextModal();
            initSidebarToggle();
            initMobileOptimizations();
            initCanopyAttentionCenter();
            startCanopySidebarPeerPolling();
            applyWorkspaceOnboardingVisibility(document);
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
                setTimeout(function() {
                    setVH();
                    window.dispatchEvent(new Event('resize'));
                }, 150);
            });
            
            // Prevent elastic scrolling on iOS outside designated scroll areas.
            document.addEventListener('touchmove', function(e) {
                const target = e.target;
                if (target && target.closest && target.closest(
                    '.main-content, .sidebar, .modal, .offcanvas, .dropdown-menu, ' +
                    '.workspace-search-overlay, .canopy-media-deck-portal, .sidebar-media-deck'
                )) {
                    return;
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
