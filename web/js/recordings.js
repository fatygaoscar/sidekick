/**
 * Recordings list page.
 *
 * The history list stays lightweight. All detailed work happens inside the
 * shared RecordingWorkspace modal.
 */

class RecordingsPage {
    constructor() {
        this.recordings = [];
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

    async _loadRecordings() {
        try {
            const response = await fetch('/api/recordings');
            if (!response.ok) {
                throw new Error('Failed to load recordings');
            }

            this.recordings = await response.json();
            this._renderRecordings();
        } catch (error) {
            console.error('Failed to load recordings:', error);
            this._renderError();
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
        });
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

        return `
            <div class="recording-card" data-id="${recording.id}">
                <div class="recording-date">${dateStr} ${timeStr}</div>
                <div class="recording-title-row">
                    <span class="recording-title">${this._escapeHtml(title)}</span>
                </div>
                <div class="recording-meta">
                    <span>Duration: ${this._formatDuration(recording.duration_seconds)}</span>
                </div>
                <div class="recording-badges">${badges.join('')}</div>
                <div class="recording-actions">
                    <button class="btn view-btn" data-id="${recording.id}">Open</button>
                    <button class="btn delete-btn" data-id="${recording.id}">Delete</button>
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
        const confirmed = window.confirm('Delete this recording and all associated transcript and summary data?');
        if (!confirmed) {
            return;
        }

        const response = await fetch(`/api/recordings/${id}`, { method: 'DELETE' });
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            alert(error.detail || 'Failed to delete recording');
            return;
        }

        await this._loadRecordings();
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
