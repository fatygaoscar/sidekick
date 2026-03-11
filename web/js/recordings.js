/**
 * Recordings list page.
 *
 * The history list stays lightweight. All detailed work happens inside the
 * shared RecordingWorkspace modal.
 */

class RecordingsPage {
    constructor() {
        this.recordings = [];
        this.deletingIds = new Set();
        this.openMenuRecordingId = null;
        this.clientId = crypto.randomUUID();
        this.searchState = {
            query: '',
            answer: null,
            confidence: 'low',
            answerType: 'partial',
            reasoningNote: null,
            followUpQueries: [],
            results: [],
            groups: [],
            loading: false,
            error: null,
            filtersOpen: false,
        };
        this.uploadState = {
            isUploading: false,
            phase: 'idle',
            percent: 0,
            fileName: '',
            message: '',
            indeterminate: false,
        };
        this.workspace = new window.RecordingWorkspace({
            onClose: () => this._loadRecordings(),
        });

        this.elements = {
            uploadBtn: document.getElementById('upload-btn'),
            uploadInput: document.getElementById('upload-input'),
            uploadProgress: document.getElementById('upload-progress'),
            uploadProgressPercent: document.getElementById('upload-progress-percent'),
            uploadProgressFile: document.getElementById('upload-progress-file'),
            uploadProgressFill: document.getElementById('upload-progress-fill'),
            uploadProgressMessage: document.getElementById('upload-progress-message'),
            recordingsList: document.getElementById('recordings-list'),
            loadingState: document.getElementById('loading-state'),
            searchForm: document.getElementById('recordings-search-form'),
            searchInput: document.getElementById('recordings-search-input'),
            searchButton: document.getElementById('recordings-search-button'),
            searchResults: document.getElementById('recordings-search-results'),
            searchFilters: document.getElementById('recordings-search-filters'),
            searchFiltersToggle: document.getElementById('search-filters-toggle'),
            searchDateFrom: document.getElementById('recordings-search-date-from'),
            searchDateTo: document.getElementById('recordings-search-date-to'),
        };

        this._init();
    }

    async _init() {
        this._bindSearchEvents();
        this._bindUploadEvents();
        this._bindGlobalEvents();
        this._renderUploadProgress();
        this._renderSearch();
        await this._loadRecordings();
    }

    _bindSearchEvents() {
        this.elements.searchForm?.addEventListener('submit', (event) => {
            event.preventDefault();
            void this._runSearch();
        });
        this.elements.searchInput?.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                void this._runSearch();
            }
        });
        this.elements.searchInput?.addEventListener('input', () => {
            this.searchState.query = this.elements.searchInput.value;
            this._syncSearchControls();
        });
        this.elements.searchFiltersToggle?.addEventListener('click', (event) => {
            event.preventDefault();
            this.searchState.filtersOpen = !this.searchState.filtersOpen;
            this._renderSearch();
        });
    }

    _bindUploadEvents() {
        this.elements.uploadBtn?.addEventListener('click', () => this._handleUploadClick());
        this.elements.uploadInput?.addEventListener('change', (event) => this._handleUploadSelection(event));
    }

    _bindGlobalEvents() {
        document.addEventListener('click', (event) => {
            if (!this.openMenuRecordingId) {
                return;
            }
            if (event.target.closest('.recording-menu-shell')) {
                return;
            }
            this.openMenuRecordingId = null;
            this._renderRecordings();
        });

        document.addEventListener('keydown', (event) => {
            if (event.key !== 'Escape' || !this.openMenuRecordingId) {
                return;
            }
            this.openMenuRecordingId = null;
            this._renderRecordings();
        });
    }

    _handleUploadClick() {
        if (this.uploadState.isUploading) {
            return;
        }
        this.elements.uploadInput?.click();
    }

    async _handleUploadSelection(event) {
        const input = event.target;
        const file = input?.files?.[0];
        if (!file) {
            return;
        }

        try {
            await this._startFileUpload(file);
        } finally {
            if (input) {
                input.value = '';
            }
        }
    }

    _setUploadProgressState(patch = {}) {
        this.uploadState = {
            ...this.uploadState,
            ...patch,
        };
        this._renderUploadProgress();
    }

    _resetUploadProgress() {
        this.uploadState = {
            isUploading: false,
            phase: 'idle',
            percent: 0,
            fileName: '',
            message: '',
            indeterminate: false,
        };
        this._renderUploadProgress();
    }

    _renderUploadProgress() {
        const progress = this.elements.uploadProgress;
        const percent = this.elements.uploadProgressPercent;
        const file = this.elements.uploadProgressFile;
        const fill = this.elements.uploadProgressFill;
        const message = this.elements.uploadProgressMessage;
        const uploadBtn = this.elements.uploadBtn;
        if (!progress || !percent || !file || !fill || !message || !uploadBtn) {
            return;
        }

        progress.classList.toggle('hidden', this.uploadState.phase === 'idle');
        uploadBtn.disabled = this.uploadState.isUploading;
        uploadBtn.textContent = this.uploadState.isUploading ? '...' : '+';

        file.textContent = this.uploadState.fileName || 'Preparing upload...';
        message.textContent = this.uploadState.message || 'Preparing upload...';
        fill.classList.toggle('indeterminate', !!this.uploadState.indeterminate);
        fill.style.width = this.uploadState.indeterminate ? '100%' : `${Math.max(0, Math.min(100, this.uploadState.percent || 0))}%`;
        percent.textContent = this.uploadState.indeterminate
            ? 'Processing'
            : `${Math.max(0, Math.min(100, Math.round(this.uploadState.percent || 0)))}%`;
    }

    async _startFileUpload(file) {
        if (this.uploadState.isUploading) {
            return;
        }

        const extension = this._inferUploadExtension(file);
        if (!this._isSupportedUpload(file, extension)) {
            alert('Unsupported file type. Choose MP3, WAV, M4A, or MP4.');
            return;
        }

        const mimeType = this._uploadMimeType(file, extension);
        let sessionId = null;
        let canDeleteFailedSession = false;
        this._setUploadProgressState({
            isUploading: true,
            phase: 'uploading',
            percent: 0,
            fileName: file.name || 'Selected file',
            message: 'Preparing upload...',
            indeterminate: false,
        });

        try {
            console.info('[upload:create_session:start]', { fileName: file.name, mimeType });
            sessionId = await this._createSession({
                timeoutMs: 15000,
                retries: 1,
                networkErrorMessage: 'Network request failed while creating upload session',
                httpErrorMessage: 'Failed to create upload session',
                logLabel: 'upload:create_session',
            });
            canDeleteFailedSession = true;
            console.info('[upload:create_session:ok]', { sessionId });

            this._setUploadProgressState({
                phase: 'uploading',
                percent: 0,
                message: 'Uploading file...',
                indeterminate: false,
            });
            console.info('[upload:audio:start]', {
                sessionId,
                fileName: file.name,
                sizeBytes: Number.isFinite(file.size) ? file.size : null,
            });
            await this._uploadImportedAudioWithProgress(sessionId, file, extension, mimeType);
            canDeleteFailedSession = false;
            console.info('[upload:audio:ok]', { sessionId });

            this._setUploadProgressState({
                phase: 'finalizing',
                percent: 100,
                message: 'Finalizing upload...',
                indeterminate: true,
            });
            console.info('[upload:title:start]', { sessionId });
            await this._seedUploadedRecordingTitle(sessionId, file.name);
            console.info('[upload:title:done]', { sessionId });
            console.info('[upload:finalize:start]', { sessionId });
            await this._finalizeUploadSession(sessionId, mimeType);
            console.info('[upload:finalize:ok]', { sessionId });

            this._setUploadProgressState({
                phase: 'opening',
                message: 'Opening workspace...',
                indeterminate: true,
            });
            console.info('[upload:workspace:start]', { sessionId });
            await this.workspace.open(sessionId, {
                autoStartTranscription: false,
                transcriptionStartMode: 'manual',
                workspaceLoadTimeoutMs: 15000,
                workspaceLoadRetries: 3,
            });
            console.info('[upload:workspace:ok]', { sessionId });
            this._resetUploadProgress();
        } catch (error) {
            console.error('Failed to upload file:', error);
            console.warn('[upload:workflow:fail]', {
                sessionId,
                canDeleteFailedSession,
                message: error?.message || 'Failed to upload file',
            });
            if (sessionId && canDeleteFailedSession) {
                console.warn('[upload:cleanup:execute]', {
                    sessionId,
                    message: error?.message || 'Upload failed before audio was fully stored',
                });
                await this._cleanupFailedUploadSession(sessionId);
            } else if (sessionId) {
                console.warn('[upload:cleanup:skip]', {
                    sessionId,
                    message: error?.message || 'Post-upload failure',
                });
            }
            if (sessionId && !canDeleteFailedSession) {
                this._setUploadProgressState({
                    phase: 'opening',
                    percent: 100,
                    fileName: file.name || 'Selected file',
                    message: 'Upload finished. Reopen it from History if the workspace does not open.',
                    indeterminate: false,
                });
                await this._loadRecordings({ renderErrorOnFailure: false });
                alert(error?.message || 'Upload finished, but opening the workspace failed. Reopen it from History.');
                this._resetUploadProgress();
                return;
            }
            this._resetUploadProgress();
            alert(error?.message || 'Failed to upload file');
        }
    }

    async _loadRecordings(options = {}) {
        const {
            renderErrorOnFailure = true,
        } = options;

        console.info('[recordings:load:start]');
        try {
            this.recordings = await window.SidekickNetwork.json('/api/recordings', {}, {
                timeoutMs: 10000,
                retries: 1,
                networkErrorMessage: 'Recordings network request failed',
                httpErrorMessage: 'Failed to load recordings',
                logLabel: 'recordings:load',
            });
            console.info('[recordings:load:ok]', { count: this.recordings.length });
            this._renderRecordings();
        } catch (error) {
            console.error('Failed to load recordings:', error);
            console.warn('[recordings:load:fail]', {
                message: error?.message || 'Failed to load recordings',
            });
            if (renderErrorOnFailure) {
                this._renderError();
            }
        }
    }

    async _runSearch() {
        const query = (this.elements.searchInput?.value || '').trim();
        if (!query || this.searchState.loading) {
            return;
        }

        this.searchState.loading = true;
        this.searchState.error = null;
        this.searchState.query = query;
        this._renderSearch();

        try {
            console.info('[recordings:search:start]', { query });
            const payload = await window.SidekickNetwork.json('/api/search/recordings', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    query,
                    date_from: this.elements.searchDateFrom?.value || null,
                    date_to: this.elements.searchDateTo?.value || null,
                    limit: 8,
                }),
            }, {
                timeoutMs: 10000,
                retries: 1,
                networkErrorMessage: 'Recordings search network request failed',
                httpErrorMessage: 'Search failed',
                logLabel: 'recordings:search',
            });

            this.searchState.answer = payload.answer || null;
            this.searchState.confidence = payload.confidence || 'low';
            this.searchState.answerType = payload.answer_type || 'partial';
            this.searchState.reasoningNote = payload.reasoning_note || null;
            this.searchState.followUpQueries = Array.isArray(payload.follow_up_queries)
                ? payload.follow_up_queries.filter(Boolean).slice(0, 3)
                : [];
            this.searchState.results = Array.isArray(payload.results) ? payload.results : [];
            this.searchState.groups = Array.isArray(payload.groups) ? payload.groups : [];
            console.info('[recordings:search:ok]', {
                query,
                resultCount: this.searchState.results.length,
            });
        } catch (error) {
            console.error('Failed to search recordings:', error);
            console.warn('[recordings:search:fail]', {
                query,
                message: error?.message || 'Search failed',
            });
            this.searchState.error = error.message || 'Search failed';
            this.searchState.answer = null;
            this.searchState.answerType = 'partial';
            this.searchState.reasoningNote = null;
            this.searchState.followUpQueries = [];
            this.searchState.results = [];
            this.searchState.groups = [];
        } finally {
            this.searchState.loading = false;
            this._renderSearch();
        }
    }

    _renderSearch() {
        if (!this.elements.searchResults || !this.elements.searchFiltersToggle || !this.elements.searchFilters) {
            return;
        }

        const hasContent = this.searchState.loading
            || this.searchState.error
            || this.searchState.answer
            || this.searchState.reasoningNote
            || this.searchState.followUpQueries.length > 0
            || this.searchState.groups.length > 0
            || this.searchState.results.length > 0;

        this.elements.searchFilters.classList.toggle('hidden', !this.searchState.filtersOpen);
        this.elements.searchFiltersToggle.classList.toggle('btn-primary', this.searchState.filtersOpen);
        this.elements.searchFiltersToggle.textContent = this.searchState.filtersOpen ? 'Hide Filters' : 'Filters';
        this.elements.searchResults.classList.toggle('hidden', !hasContent);

        this._syncSearchControls();

        if (!hasContent) {
            this.elements.searchResults.innerHTML = '';
            return;
        }

        if (this.searchState.loading) {
            this.elements.searchResults.innerHTML = `
                <div class="recordings-search-status">
                    <div class="processing-spinner"></div>
                    <div>
                        <div class="recordings-search-status-title">Searching recordings</div>
                        <div class="recordings-search-status-copy">Retrieving transcript evidence and building an answer.</div>
                    </div>
                </div>
            `;
            return;
        }

        if (this.searchState.error) {
            this.elements.searchResults.innerHTML = `
                <div class="recordings-search-error">${this._escapeHtml(this.searchState.error)}</div>
            `;
            return;
        }

        const answerLabel = this.searchState.answer
            ? 'Search Summary'
            : 'Evidence Only';
        const answerBlock = `
            <div class="recordings-search-answer${this.searchState.answer ? '' : ' recordings-search-answer-muted'}">
                <div class="recordings-search-answer-meta">
                    <span class="recordings-search-answer-label">${this._escapeHtml(answerLabel)}</span>
                    <div class="recordings-search-answer-tags">
                        <span class="recordings-search-answer-type">${this._escapeHtml(this._searchAnswerTypeLabel(this.searchState.answerType))}</span>
                        <span class="recordings-search-answer-confidence">${this._escapeHtml(this.searchState.confidence)}</span>
                    </div>
                </div>
                <p class="recordings-search-answer-copy">${this._escapeHtml(this.searchState.answer || 'Showing transcript evidence because the AI answer was unavailable.')}</p>
                ${this.searchState.reasoningNote ? `<p class="recordings-search-answer-note">${this._escapeHtml(this.searchState.reasoningNote)}</p>` : ''}
                ${this._renderFollowUpQueries()}
            </div>
        `;

        const results = this.searchState.groups.length > 0
            ? this.searchState.groups.map((group, index) => this._renderSearchGroup(group, index)).join('')
            : this.searchState.results.length > 0
                ? this.searchState.results.map((result) => this._renderSearchResult(result)).join('')
                : `
                    <div class="recordings-search-empty">
                        No grounded matches found across your recordings. Try fewer specifics or a shorter question.
                    </div>
                `;

        this.elements.searchResults.innerHTML = `
            ${answerBlock}
            <div class="recordings-search-list">${results}</div>
        `;

        this.elements.searchResults.querySelectorAll('.search-open-btn').forEach((button) => {
            button.addEventListener('click', () => {
                const result = this.searchState.results.find((item) => item.citation_id === button.dataset.citationId);
                if (!result) {
                    return;
                }
                void this._openWorkspaceFromCitation(result);
            });
        });

        this.elements.searchResults.querySelectorAll('.search-follow-up-btn').forEach((button) => {
            button.addEventListener('click', () => {
                const query = (button.dataset.query || '').trim();
                if (!query || !this.elements.searchInput) {
                    return;
                }
                this.elements.searchInput.value = query;
                this.searchState.query = query;
                this._syncSearchControls();
                void this._runSearch();
            });
        });

        this.elements.searchResults.querySelectorAll('.search-group-open-transcript-btn').forEach((button) => {
            button.addEventListener('click', () => {
                const group = this.searchState.groups[Number(button.dataset.groupIndex)];
                if (!group) {
                    return;
                }
                void this._openWorkspaceTranscriptFromGroup(group);
            });
        });

        this.elements.searchResults.querySelectorAll('.search-group-open-summary-btn').forEach((button) => {
            button.addEventListener('click', () => {
                const group = this.searchState.groups[Number(button.dataset.groupIndex)];
                if (!group) {
                    return;
                }
                void this._openWorkspaceSummaryFromGroup(group);
            });
        });
    }

    _renderFollowUpQueries() {
        if (!this.searchState.followUpQueries.length) {
            return '';
        }
        return `
            <div class="recordings-search-follow-ups">
                <div class="recordings-search-follow-ups-label">Try next</div>
                <div class="recordings-search-follow-ups-list">
                    ${this.searchState.followUpQueries.map((query) => `
                        <button type="button" class="btn btn-small search-follow-up-btn" data-query="${this._escapeHtml(query)}">${this._escapeHtml(query)}</button>
                    `).join('')}
                </div>
            </div>
        `;
    }

    _searchAnswerTypeLabel(answerType) {
        switch (String(answerType || '').toLowerCase()) {
            case 'direct_answer':
                return 'Direct Answer';
            case 'multi_recording':
                return 'Multiple Meetings';
            case 'insufficient_evidence':
                return 'No Grounded Match';
            default:
                return 'Partial';
        }
    }

    _renderSearchGroup(group, index) {
        const citedBadge = group.has_cited_evidence
            ? '<span class="workspace-badge workspace-badge-accent">Cited</span>'
            : '<span class="workspace-badge">Evidence</span>';
        const snippets = Array.isArray(group.snippets)
            ? group.snippets.map((snippet) => `
                <div class="recordings-search-group-snippet${snippet.is_cited ? ' recordings-search-group-snippet-cited' : ''}">
                    <div class="recordings-search-group-snippet-meta">
                        ${snippet.speaker ? `<span>${this._escapeHtml(snippet.speaker)}</span><span aria-hidden="true">&middot;</span>` : ''}
                        <span>${this._escapeHtml(snippet.timestamp)}</span>
                        ${snippet.is_cited ? '<span aria-hidden="true">&middot;</span><span>Cited</span>' : ''}
                    </div>
                    <p class="recordings-search-card-snippet">${this._escapeHtml(snippet.snippet)}</p>
                </div>
            `).join('')
            : `
                <div class="recordings-search-empty">
                    No grounded matches found across your recordings. Try fewer specifics or a shorter question.
                </div>
            `;

        return `
            <article class="recordings-search-card recordings-search-group-card">
                <div class="recordings-search-card-head">
                    <div class="recordings-search-card-meta">
                        <span>${this._escapeHtml(group.recorded_date_label)}</span>
                        <span aria-hidden="true">&middot;</span>
                        <span>${this._escapeHtml(group.recorded_time_label)}</span>
                    </div>
                    <div class="recordings-search-card-badges">${citedBadge}</div>
                </div>
                <div class="recordings-search-card-title">${this._escapeHtml(group.recording_title || 'Untitled Recording')}</div>
                <p class="recordings-search-group-reason">${this._escapeHtml(group.match_reason || 'Relevant transcript evidence found in this recording.')}</p>
                <div class="recordings-search-group-snippets">${snippets}</div>
                <div class="recordings-search-card-footer">
                    <button class="btn btn-small search-group-open-summary-btn" data-group-index="${index}">Open Summary</button>
                    <button class="btn btn-small search-group-open-transcript-btn" data-group-index="${index}">Open Transcript</button>
                </div>
            </article>
        `;
    }

    _syncSearchControls() {
        if (!this.elements.searchButton) {
            return;
        }
        const hasQuery = Boolean((this.elements.searchInput?.value || '').trim());
        this.elements.searchButton.disabled = !hasQuery || this.searchState.loading;
    }

    _renderSearchResult(result) {
        const speaker = result.speaker ? `<span>${this._escapeHtml(result.speaker)}</span>` : '';
        const speakerDivider = result.speaker ? '<span aria-hidden="true">&middot;</span>' : '';
        const citedBadge = result.is_cited
            ? '<span class="workspace-badge workspace-badge-accent">Cited</span>'
            : '<span class="workspace-badge">Evidence</span>';

        return `
            <article class="recordings-search-card">
                <div class="recordings-search-card-head">
                    <div class="recordings-search-card-meta">
                        <span>${this._escapeHtml(result.recorded_date_label)}</span>
                        <span aria-hidden="true">&middot;</span>
                        <span>${this._escapeHtml(result.recorded_time_label)}</span>
                        ${speakerDivider}
                        ${speaker}
                        <span aria-hidden="true">&middot;</span>
                        <span>${this._escapeHtml(result.timestamp)}</span>
                    </div>
                    <div class="recordings-search-card-badges">${citedBadge}</div>
                </div>
                <div class="recordings-search-card-title">${this._escapeHtml(result.recording_title || 'Untitled Recording')}</div>
                <p class="recordings-search-card-snippet">${this._escapeHtml(result.snippet)}</p>
                <div class="recordings-search-card-footer">
                    <button class="btn btn-small search-open-btn" data-citation-id="${this._escapeHtml(result.citation_id)}">Open Transcript</button>
                </div>
            </article>
        `;
    }

    async _openWorkspaceFromCitation(result) {
        try {
            await this.workspace.open(result.session_id, {
                initialTab: 'transcript',
                workspaceVersionId: result.transcript_version_id || null,
                highlightSegmentIds: result.transcript_segment_ids || [],
                focusStartTime: result.start_time,
            });
        } catch (error) {
            alert(error.message || 'Failed to open recording workspace');
        }
    }

    async _openWorkspaceTranscriptFromGroup(group) {
        const preferredSnippet = Array.isArray(group.snippets)
            ? group.snippets.find((snippet) => snippet.is_cited) || group.snippets[0]
            : null;
        try {
            await this.workspace.open(group.session_id, {
                initialTab: 'transcript',
                workspaceVersionId: group.transcript_version_id || null,
                highlightSegmentIds: preferredSnippet?.transcript_segment_ids || [],
                focusStartTime: preferredSnippet?.start_time ?? 0,
            });
        } catch (error) {
            alert(error.message || 'Failed to open recording workspace');
        }
    }

    async _openWorkspaceSummaryFromGroup(group) {
        try {
            await this.workspace.open(group.session_id, {
                initialTab: 'summary',
                workspaceVersionId: group.transcript_version_id || null,
            });
        } catch (error) {
            alert(error.message || 'Failed to open recording workspace');
        }
    }

    _renderRecordings() {
        if (this.elements.loadingState) {
            this.elements.loadingState.remove();
        }

        if (this.recordings.length === 0) {
            this.elements.recordingsList.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">&#9673;</div>
                    <p class="empty-state-text">No recordings yet</p>
                </div>
            `;
            return;
        }

        this.elements.recordingsList.innerHTML = this.recordings
            .map((recording) => this._renderCard(recording))
            .join('');

        this.elements.recordingsList.querySelectorAll('.recording-card').forEach((card) => {
            card.addEventListener('click', (event) => {
                if (event.target.closest('button, a, input, select, textarea')) {
                    return;
                }
                void this._openRecordingCard(card.dataset.id);
            });

            card.addEventListener('keydown', (event) => {
                if (event.target.closest('button, a, input, select, textarea')) {
                    return;
                }
                if (event.key !== 'Enter' && event.key !== ' ') {
                    return;
                }
                event.preventDefault();
                void this._openRecordingCard(card.dataset.id);
            });
        });

        this.elements.recordingsList.querySelectorAll('.recording-menu-btn').forEach((button) => {
            button.addEventListener('click', (event) => {
                event.stopPropagation();
                const nextOpenId = this.openMenuRecordingId === button.dataset.id ? null : button.dataset.id;
                this.openMenuRecordingId = nextOpenId;
                this._renderRecordings();
            });
        });

        this.elements.recordingsList.querySelectorAll('.recording-menu-delete-btn').forEach((button) => {
            button.addEventListener('click', (event) => {
                event.stopPropagation();
                this.openMenuRecordingId = null;
                void this._deleteRecording(button.dataset.id);
            });
        });
    }

    async _openRecordingCard(recordingId) {
        try {
            await this.workspace.open(recordingId);
        } catch (error) {
            alert(error.message || 'Failed to open recording workspace');
        }
    }

    _renderCard(recording) {
        const startedAt = new Date(recording.started_at);
        const dateStr = startedAt.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
        });
        const timeStr = startedAt.toLocaleTimeString('en-US', {
            hour: '2-digit',
            minute: '2-digit',
        }).replace(' AM', 'AM').replace(' PM', 'PM');
        const dateLabel = recording.recorded_date_label || dateStr;
        const timeLabel = recording.recorded_time_label || timeStr;
        const title = (recording.title || 'Untitled Recording').trim() || 'Untitled Recording';
        const badges = [];
        if (recording.needs_speaker_review) {
            badges.push('<span class="workspace-badge workspace-badge-warning">Speaker review</span>');
        }
        if (recording.has_draft) {
            badges.push('<span class="workspace-badge workspace-badge-accent">Draft</span>');
        } else if (recording.has_summary) {
            badges.push('<span class="workspace-badge">Saved</span>');
        }

        const isDeleting = this.deletingIds.has(recording.id);
        const menuExpandedAttr = this.openMenuRecordingId === recording.id ? 'true' : 'false';
        const menuMarkup = this.openMenuRecordingId === recording.id ? `
            <div class="recording-card-menu" role="menu">
                <button
                    type="button"
                    class="recording-menu-delete-btn"
                    data-id="${recording.id}"
                    role="menuitem"
                    ${isDeleting ? ' disabled aria-disabled="true"' : ''}
                >${isDeleting ? 'Deleting...' : 'Delete recording…'}</button>
            </div>
        ` : '';

        return `
            <div
                class="recording-card"
                data-id="${recording.id}"
                role="button"
                tabindex="0"
                aria-label="Open ${this._escapeHtml(title)}"
            >
                <div class="recording-card-shell">
                    <div class="recording-primary">
                        <div class="recording-title-row">
                            <span class="recording-title">${this._escapeHtml(title)}</span>
                            <div class="recording-menu-shell">
                                <button
                                    type="button"
                                    class="recording-menu-btn"
                                    data-id="${recording.id}"
                                    aria-label="Recording actions"
                                    aria-haspopup="menu"
                                    aria-expanded="${menuExpandedAttr}"
                                >⋯</button>
                                ${menuMarkup}
                            </div>
                        </div>
                        <div class="recording-subline">
                            <div class="recording-meta-line">
                                <span>${this._escapeHtml(dateLabel)}</span>
                                <span aria-hidden="true">&middot;</span>
                                <span>${this._escapeHtml(timeLabel)}</span>
                                <span aria-hidden="true">&middot;</span>
                                <span>${this._formatDuration(recording.duration_seconds)}</span>
                            </div>
                        </div>
                        <div class="recording-badges">${badges.join('')}</div>
                    </div>
                </div>
            </div>
        `;
    }

    _renderError() {
        this.elements.recordingsList.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">!</div>
                <p class="empty-state-text">Failed to load recordings</p>
            </div>
        `;
    }

    async _deleteRecording(id) {
        if (!id || this.deletingIds.has(id)) {
            return;
        }

        const confirmed = window.confirm('Delete this recording and all associated transcript and summary data?');
        if (!confirmed) {
            return;
        }

        this.deletingIds.add(id);
        this._renderRecordings();
        console.info('[recordings:delete:start]', { recordingId: id });

        try {
            await window.SidekickNetwork.request(`/api/recordings/${id}`, {
                method: 'DELETE',
            }, {
                timeoutMs: 10000,
                retries: 1,
                networkErrorMessage: 'Recordings delete network request failed',
                logLabel: 'recordings:delete',
            });

            this.recordings = this.recordings.filter((recording) => recording.id !== id);
            console.info('[recordings:delete:ok]', { recordingId: id });
            this._renderRecordings();
            void this._loadRecordings({ renderErrorOnFailure: false });
        } catch (error) {
            console.error('Failed to delete recording:', error);
            console.warn('[recordings:delete:fail]', {
                recordingId: id,
                message: error?.message || 'Failed to delete recording',
            });
            alert(error?.message || 'Failed to delete recording');
        } finally {
            this.deletingIds.delete(id);
            this._renderRecordings();
        }
    }

    async _createSession(config = {}) {
        let timezoneName = null;
        try {
            timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || null;
        } catch (_error) {
            timezoneName = null;
        }

        const payload = await window.SidekickNetwork.json('/api/sessions', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                mode: 'work',
                submode: null,
                timezone_name: timezoneName,
                timezone_offset_minutes: new Date().getTimezoneOffset(),
            }),
        }, config);

        if (!payload?.id) {
            throw new Error(config.httpErrorMessage || 'Failed to create session');
        }

        return payload.id;
    }

    async _endSession(sessionId, config = {}) {
        if (!sessionId) {
            return null;
        }

        return await window.SidekickNetwork.json(`/api/sessions/${sessionId}`, {
            method: 'DELETE',
        }, {
            timeoutMs: 10000,
            retries: 0,
            httpErrorMessage: 'Failed to end session',
            ...config,
        });
    }

    async _uploadImportedAudioWithProgress(sessionId, file, extension, mimeType) {
        const resolvedUrl = window.SidekickNetwork.resolveUrl(`/api/recordings/${sessionId}/audio`);

        await new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            xhr.open('PUT', resolvedUrl, true);
            xhr.withCredentials = false;
            xhr.timeout = 600000;
            xhr.setRequestHeader('Content-Type', mimeType);
            xhr.setRequestHeader('X-Upload-Extension', extension);

            xhr.upload.onprogress = (event) => {
                if (!event.lengthComputable) {
                    return;
                }
                const percent = Math.round((event.loaded / event.total) * 100);
                this._setUploadProgressState({
                    phase: 'uploading',
                    percent,
                    message: 'Uploading file...',
                    indeterminate: false,
                });
            };

            xhr.onerror = () => {
                console.warn('[upload:audio:fail]', { sessionId, status: xhr.status || null, kind: 'network' });
                reject(new Error('Network request failed while uploading file'));
            };
            xhr.ontimeout = () => {
                console.warn('[upload:audio:fail]', { sessionId, status: xhr.status || null, kind: 'timeout' });
                reject(new Error('Network request timed out while uploading file'));
            };
            xhr.onabort = () => {
                console.warn('[upload:audio:fail]', { sessionId, status: xhr.status || null, kind: 'abort' });
                reject(new Error('Upload was cancelled'));
            };

            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    this._setUploadProgressState({
                        phase: 'uploading',
                        percent: 100,
                        message: 'Uploading file...',
                        indeterminate: false,
                    });
                    resolve();
                    return;
                }

                let detail = 'Failed to upload file';
                try {
                    const payload = JSON.parse(xhr.responseText || '{}');
                    if (payload?.detail) {
                        detail = payload.detail;
                    }
                } catch (_error) {
                    if (xhr.status) {
                        detail = `Failed to upload file (HTTP ${xhr.status})`;
                    }
                }
                reject(new Error(detail));
            };

            xhr.send(file);
        });
    }

    async _seedUploadedRecordingTitle(sessionId, filename) {
        const title = this._deriveTitleFromFilename(filename);
        if (!title) {
            return;
        }

        try {
            await window.SidekickNetwork.json(`/api/recordings/${sessionId}/settings`, {
                method: 'PATCH',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    title,
                }),
            }, {
                timeoutMs: 10000,
                retries: 0,
                networkErrorMessage: 'Network request failed while naming uploaded file',
                httpErrorMessage: 'Failed to update uploaded recording title',
                logLabel: 'upload:title',
            });
        } catch (error) {
            console.warn('[upload:title:fail]', {
                sessionId,
                message: error?.message || 'Failed to update uploaded recording title',
            });
            console.warn('Failed to seed uploaded recording title:', error?.message || error);
        }
    }

    async _finalizeUploadSession(sessionId, mimeType) {
        try {
            const completion = await this._completeRecording(sessionId, {
                mimeType,
                expectedChunks: 0,
                allowFallbackBlob: true,
            });
            if (!completion?.workspace_ready) {
                throw new Error('Uploaded audio is not ready yet');
            }
        } catch (error) {
            try {
                await this._endSession(sessionId, {
                    networkErrorMessage: 'Network request failed while finalizing upload session',
                    logLabel: 'upload:end_session',
                });
            } catch (endError) {
                console.warn('Failed to end upload session cleanly after complete fallback:', {
                    sessionId,
                    message: endError?.message || 'Failed to end upload session',
                });
            }
            console.warn('[upload:finalize:fail]', {
                sessionId,
                message: error?.message || 'Failed to finalize upload session',
            });
            console.warn('Failed to finalize upload session cleanly:', {
                sessionId,
                message: error?.message || 'Failed to finalize upload session',
            });
            throw error;
        }
    }

    async _completeRecording(sessionId, options = {}) {
        return await window.SidekickNetwork.json(`/api/recordings/${sessionId}/complete`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                client_id: this.clientId,
                mime_type: options.mimeType || 'application/octet-stream',
                expected_chunks: Number.isFinite(options.expectedChunks) ? options.expectedChunks : 0,
                allow_fallback_blob: !!options.allowFallbackBlob,
            }),
        }, {
            timeoutMs: 30000,
            retries: 1,
            networkErrorMessage: 'Network request failed while finalizing recording',
            httpErrorMessage: 'Failed to finalize recording',
            logLabel: 'recording_stop:complete',
        });
    }

    async _cleanupFailedUploadSession(sessionId) {
        try {
            console.info('[upload:cleanup:start]', { sessionId });
            const response = await window.SidekickNetwork.request(`/api/recordings/${sessionId}`, {
                method: 'DELETE',
            }, {
                timeoutMs: 10000,
                retries: 0,
                networkErrorMessage: 'Network request failed while cleaning up failed upload',
                logLabel: 'upload:cleanup',
            });
            if (!response.ok && response.status !== 404) {
                throw new Error(`HTTP ${response.status}`);
            }
            console.info('[upload:cleanup:ok]', { sessionId, status: response.status });
        } catch (cleanupError) {
            console.warn('Failed to clean up failed upload session:', {
                sessionId,
                message: cleanupError?.message || 'Cleanup failed',
            });
        }
    }

    _inferUploadExtension(file) {
        const filename = String(file?.name || '').toLowerCase();
        if (filename.endsWith('.wav')) {
            return 'wav';
        }
        if (filename.endsWith('.mp3')) {
            return 'mp3';
        }
        if (filename.endsWith('.m4a')) {
            return 'm4a';
        }
        if (filename.endsWith('.mp4')) {
            return 'mp4';
        }

        const fileType = String(file?.type || '').toLowerCase();
        if (fileType.includes('audio/wav') || fileType.includes('audio/x-wav')) {
            return 'wav';
        }
        if (fileType.includes('audio/mpeg') || fileType.includes('audio/mp3')) {
            return 'mp3';
        }
        if (fileType.includes('video/mp4')) {
            return 'mp4';
        }
        if (fileType.includes('audio/mp4') || fileType.includes('audio/x-m4a') || fileType.includes('audio/m4a')) {
            return 'm4a';
        }
        return '';
    }

    _isSupportedUpload(file, extension) {
        const supportedExtensions = new Set(['wav', 'mp3', 'm4a', 'mp4']);
        if (supportedExtensions.has(extension)) {
            return true;
        }

        const fileType = String(file?.type || '').toLowerCase();
        return (
            fileType.includes('audio/wav')
            || fileType.includes('audio/x-wav')
            || fileType.includes('audio/mpeg')
            || fileType.includes('audio/mp3')
            || fileType.includes('audio/mp4')
            || fileType.includes('audio/x-m4a')
            || fileType.includes('video/mp4')
        );
    }

    _uploadMimeType(file, extension) {
        const fileType = String(file?.type || '').trim();
        if (fileType) {
            return fileType;
        }
        if (extension === 'wav') {
            return 'audio/wav';
        }
        if (extension === 'mp3') {
            return 'audio/mpeg';
        }
        if (extension === 'mp4') {
            return 'video/mp4';
        }
        if (extension === 'm4a') {
            return 'audio/mp4';
        }
        return 'application/octet-stream';
    }

    _deriveTitleFromFilename(filename) {
        const normalized = String(filename || '').trim();
        if (!normalized) {
            return '';
        }
        return normalized.replace(/\.[^.]+$/, '').trim();
    }

    _formatDuration(seconds) {
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const remainingSeconds = seconds % 60;
        return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${remainingSeconds.toString().padStart(2, '0')}`;
    }

    _escapeHtml(value) {
        const div = document.createElement('div');
        div.textContent = value == null ? '' : String(value);
        return div.innerHTML;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.recordingsPage = new RecordingsPage();
});
