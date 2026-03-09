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
        this.searchState = {
            query: '',
            answer: null,
            confidence: 'low',
            results: [],
            loading: false,
            error: null,
            filtersOpen: false,
        };
        this.workspace = new window.RecordingWorkspace({
            onClose: () => this._loadRecordings(),
        });

        this.elements = {
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
            this.searchState.results = Array.isArray(payload.results) ? payload.results : [];
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
            this.searchState.results = [];
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

        const answerBlock = this.searchState.answer
            ? `
                <div class="recordings-search-answer">
                    <div class="recordings-search-answer-meta">
                        <span class="recordings-search-answer-label">Answer</span>
                        <span class="recordings-search-answer-confidence">${this._escapeHtml(this.searchState.confidence)}</span>
                    </div>
                    <p class="recordings-search-answer-copy">${this._escapeHtml(this.searchState.answer)}</p>
                </div>
            `
            : `
                <div class="recordings-search-answer recordings-search-answer-muted">
                    <div class="recordings-search-answer-meta">
                        <span class="recordings-search-answer-label">Evidence Only</span>
                    </div>
                    <p class="recordings-search-answer-copy">Showing transcript evidence because the AI answer was unavailable.</p>
                </div>
            `;

        const results = this.searchState.results.length > 0
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

        this.elements.recordingsList.querySelectorAll('.view-btn').forEach((button) => {
            button.addEventListener('click', async () => {
                try {
                    await this.workspace.open(button.dataset.id);
                } catch (error) {
                    alert(error.message || 'Failed to open recording workspace');
                }
            });
        });

        this.elements.recordingsList.querySelectorAll('.delete-btn').forEach((button) => {
            button.addEventListener('click', () => this._deleteRecording(button.dataset.id));
        });
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
        const deleteDisabledAttr = isDeleting ? ' disabled aria-disabled="true"' : '';
        const deleteLabel = isDeleting ? 'Deleting...' : 'Delete';

        return `
            <div class="recording-card" data-id="${recording.id}">
                <div class="recording-card-shell">
                    <div class="recording-primary">
                        <div class="recording-title-row">
                            <span class="recording-title">${this._escapeHtml(title)}</span>
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
                <div class="recording-card-footer">
                    <button class="btn delete-btn" data-id="${recording.id}"${deleteDisabledAttr}>${deleteLabel}</button>
                    <div class="recording-actions">
                        <button class="btn btn-primary view-btn" data-id="${recording.id}">Open</button>
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
