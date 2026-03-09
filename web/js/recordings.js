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
        this.workspace = new window.RecordingWorkspace({
            onClose: () => this._loadRecordings(),
        });

        this.elements = {
            recordingsList: document.getElementById('recordings-list'),
            loadingState: document.getElementById('loading-state'),
        };

        this._init();
    }

    async _init() {
        await this._loadRecordings();
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
