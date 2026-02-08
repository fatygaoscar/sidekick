/**
 * Recordings page logic
 */

class RecordingsPage {
    constructor() {
        this.recordings = [];
        this.currentRecording = null;
        this.selectedTemplate = 'meeting';
        this.obsidianUri = null;

        this.elements = {
            recordingsList: document.getElementById('recordings-list'),
            loadingState: document.getElementById('loading-state'),

            // View modal
            viewModal: document.getElementById('view-modal'),
            viewClose: document.getElementById('view-close'),
            viewCloseBtn: document.getElementById('view-close-btn'),
            viewTitle: document.getElementById('view-title'),
            viewMeta: document.getElementById('view-meta'),
            viewAudioGroup: document.getElementById('view-audio-group'),
            viewAudioPlayer: document.getElementById('view-audio-player'),
            viewAudioDownload: document.getElementById('view-audio-download'),
            viewTranscript: document.getElementById('view-transcript'),
            resummarizeBtn: document.getElementById('resummarize-btn'),

            // Re-summarize modal
            resummarizeModal: document.getElementById('resummarize-modal'),
            resummarizeClose: document.getElementById('resummarize-close'),
            resummarizeCancel: document.getElementById('resummarize-cancel'),
            resummarizeSubmit: document.getElementById('resummarize-submit'),
            resummarizeTitle: document.getElementById('resummarize-title'),
            templateBtns: document.querySelectorAll('.template-btn'),
            resummarizeCustomGroup: document.getElementById('resummarize-custom-group'),
            resummarizeCustomPrompt: document.getElementById('resummarize-custom-prompt'),

            // Confirmation modal
            confirmationModal: document.getElementById('confirmation-modal'),
            confirmationClose: document.getElementById('confirmation-close'),
            confirmationFilename: document.getElementById('confirmation-filename'),
            confirmationPreview: document.getElementById('confirmation-preview'),
            backToListBtn: document.getElementById('back-to-list-btn'),
            openObsidianBtn: document.getElementById('open-obsidian-btn'),

            // Processing
            processingOverlay: document.getElementById('processing-overlay'),
        };

        this._init();
    }

    async _init() {
        this._bindEvents();
        await this._loadRecordings();
    }

    _bindEvents() {
        // View modal
        this.elements.viewClose.addEventListener('click', () => this._closeViewModal());
        this.elements.viewCloseBtn.addEventListener('click', () => this._closeViewModal());
        this.elements.viewModal.addEventListener('click', (e) => {
            if (e.target === this.elements.viewModal) this._closeViewModal();
        });
        this.elements.resummarizeBtn.addEventListener('click', () => this._showResummarizeModal());

        // Re-summarize modal
        this.elements.resummarizeClose.addEventListener('click', () => this._closeResummarizeModal());
        this.elements.resummarizeCancel.addEventListener('click', () => this._closeResummarizeModal());
        this.elements.resummarizeSubmit.addEventListener('click', () => this._processResummarize());
        this.elements.resummarizeModal.addEventListener('click', (e) => {
            if (e.target === this.elements.resummarizeModal) this._closeResummarizeModal();
        });

        // Template buttons
        this.elements.templateBtns.forEach(btn => {
            btn.addEventListener('click', () => this._selectTemplate(btn.dataset.template));
        });

        // Confirmation modal
        this.elements.confirmationClose.addEventListener('click', () => this._closeConfirmationModal());
        this.elements.backToListBtn.addEventListener('click', () => this._closeConfirmationModal());
        this.elements.openObsidianBtn.addEventListener('click', () => this._openObsidian());
        this.elements.confirmationModal.addEventListener('click', (e) => {
            if (e.target === this.elements.confirmationModal) this._closeConfirmationModal();
        });
    }

    async _loadRecordings() {
        try {
            const response = await fetch('/api/recordings');
            if (!response.ok) throw new Error('Failed to load recordings');

            this.recordings = await response.json();
            this._renderRecordings();

        } catch (error) {
            console.error('Failed to load recordings:', error);
            this._renderError();
        }
    }

    _renderRecordings() {
        this.elements.loadingState.remove();

        if (this.recordings.length === 0) {
            this.elements.recordingsList.innerHTML = `
                <div class="empty-state">
                    <div class="empty-state-icon">&#9673;</div>
                    <p class="empty-state-text">No recordings yet</p>
                </div>
            `;
            return;
        }

        this.elements.recordingsList.innerHTML = this.recordings.map(rec => this._renderCard(rec)).join('');

        // Bind card action buttons
        this.elements.recordingsList.querySelectorAll('.view-btn').forEach(btn => {
            btn.addEventListener('click', () => this._viewRecording(btn.dataset.id));
        });

        this.elements.recordingsList.querySelectorAll('.export-btn').forEach(btn => {
            btn.addEventListener('click', () => this._quickExport(btn.dataset.id));
        });

        this.elements.recordingsList.querySelectorAll('.delete-btn').forEach(btn => {
            btn.addEventListener('click', () => this._confirmDelete(btn.dataset.id));
        });
    }

    _renderCard(rec) {
        const date = new Date(rec.started_at);
        const dateStr = date.toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
        });
        const timeStr = date.toLocaleTimeString('en-US', {
            hour: '2-digit',
            minute: '2-digit',
        });

        const duration = this._formatDuration(rec.duration_seconds);
        const title = rec.formatted_title || rec.title || 'Untitled Recording';
        const audioButtons = rec.has_audio
            ? `
                <button class="btn view-btn" data-id="${rec.id}">View</button>
                <a class="btn" href="/api/recordings/${rec.id}/audio?download=true">Download Audio</a>
            `
            : `<button class="btn view-btn" data-id="${rec.id}">View</button>`;

        return `
            <div class="recording-card">
                <div class="recording-date">${dateStr} ${timeStr}</div>
                <div class="recording-title">${this._escapeHtml(title)}</div>
                <div class="recording-meta">
                    <span>Duration: ${duration}</span>
                    <span>Segments: ${rec.segment_count}</span>
                    ${rec.has_summary ? '<span>Has Summary</span>' : ''}
                </div>
                <div class="recording-actions">
                    ${audioButtons}
                    <button class="btn export-btn" data-id="${rec.id}">Export</button>
                    <button class="btn delete-btn" data-id="${rec.id}">Delete</button>
                </div>
            </div>
        `;
    }

    _renderError() {
        this.elements.loadingState.innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">!</div>
                <p class="empty-state-text">Failed to load recordings</p>
            </div>
        `;
    }

    _formatDuration(seconds) {
        const h = Math.floor(seconds / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        const s = seconds % 60;
        return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }

    _escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    async _viewRecording(id) {
        try {
            const response = await fetch(`/api/recordings/${id}`);
            if (!response.ok) throw new Error('Failed to load recording');

            this.currentRecording = await response.json();
            this._showViewModal();

        } catch (error) {
            console.error('Failed to load recording:', error);
            alert('Failed to load recording');
        }
    }

    _showViewModal() {
        const rec = this.currentRecording;
        const date = new Date(rec.started_at);

        this.elements.viewTitle.textContent = rec.formatted_title || rec.title || 'Recording';
        this.elements.viewMeta.innerHTML = `
            <span>Date: ${date.toLocaleDateString()}</span>
            <span>Duration: ${this._formatDuration(rec.duration_seconds)}</span>
            <span>Segments: ${rec.segment_count}</span>
        `;

        if (rec.has_audio && rec.audio_url) {
            this.elements.viewAudioGroup.classList.remove('hidden');
            this.elements.viewAudioPlayer.src = rec.audio_url;
            this.elements.viewAudioDownload.href = rec.audio_download_url || `${rec.audio_url}?download=true`;
        } else {
            this.elements.viewAudioGroup.classList.add('hidden');
            this.elements.viewAudioPlayer.removeAttribute('src');
            this.elements.viewAudioPlayer.load();
            this.elements.viewAudioDownload.removeAttribute('href');
        }

        // Render transcript
        if (rec.transcript && rec.transcript.length > 0) {
            this.elements.viewTranscript.innerHTML = rec.transcript.map(seg => `
                <div class="transcript-segment${seg.is_important ? ' important' : ''}">
                    <span class="timestamp">${seg.timestamp}</span>
                    <span class="text">${this._escapeHtml(seg.text)}</span>
                </div>
            `).join('');
        } else {
            this.elements.viewTranscript.innerHTML = '<p class="text-muted">No transcript available</p>';
        }

        this.elements.viewModal.classList.remove('hidden');
    }

    _closeViewModal() {
        this.elements.viewAudioPlayer.pause();
        this.elements.viewModal.classList.add('hidden');
    }

    _showResummarizeModal() {
        this._closeViewModal();

        const title = this.currentRecording?.title || this.currentRecording?.meetings[0]?.title || '';
        this.elements.resummarizeTitle.value = title;
        this.elements.resummarizeCustomPrompt.value = '';
        this._selectTemplate('meeting');

        this.elements.resummarizeModal.classList.remove('hidden');
        this.elements.resummarizeTitle.focus();
    }

    _closeResummarizeModal() {
        this.elements.resummarizeModal.classList.add('hidden');
    }

    _selectTemplate(template) {
        this.selectedTemplate = template;

        this.elements.templateBtns.forEach(btn => {
            btn.classList.toggle('selected', btn.dataset.template === template);
        });

        if (template === 'custom') {
            this.elements.resummarizeCustomGroup.classList.remove('hidden');
            this.elements.resummarizeCustomPrompt.focus();
        } else {
            this.elements.resummarizeCustomGroup.classList.add('hidden');
        }
    }

    async _processResummarize() {
        const title = this.elements.resummarizeTitle.value.trim();
        if (!title) {
            this.elements.resummarizeTitle.focus();
            return;
        }

        const template = this.selectedTemplate;
        const customPrompt = template === 'custom' ? this.elements.resummarizeCustomPrompt.value.trim() : null;

        await this._exportRecording(this.currentRecording.id, title, template, customPrompt);
    }

    async _quickExport(id) {
        // Find the recording to get its title
        const rec = this.recordings.find(r => r.id === id);
        const title = rec?.title || `Recording ${new Date(rec.started_at).toLocaleDateString()}`;

        await this._exportRecording(id, title, 'meeting', null);
    }

    async _exportRecording(id, title, template, customPrompt) {
        this.elements.resummarizeModal.classList.add('hidden');
        this.elements.processingOverlay.classList.remove('hidden');

        try {
            const response = await fetch(`/api/recordings/${id}/export-obsidian`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    title,
                    template,
                    custom_prompt: customPrompt,
                }),
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Export failed');
            }

            const result = await response.json();
            this._showConfirmation(result);

        } catch (error) {
            console.error('Export failed:', error);
            this.elements.processingOverlay.classList.add('hidden');
            alert(`Export failed: ${error.message}`);
        }
    }

    _showConfirmation(result) {
        this.elements.processingOverlay.classList.add('hidden');
        this.obsidianUri = result.obsidian_uri;

        this.elements.confirmationFilename.textContent = result.filename;
        this.elements.confirmationPreview.textContent = result.summary_preview;
        this.elements.confirmationModal.classList.remove('hidden');
    }

    _closeConfirmationModal() {
        this.elements.confirmationModal.classList.add('hidden');
    }

    _openObsidian() {
        if (this.obsidianUri) {
            window.open(this.obsidianUri, '_blank');
        }
        this._closeConfirmationModal();
    }

    _confirmDelete(id) {
        const rec = this.recordings.find(r => r.id === id);
        const title = rec?.title || 'this recording';

        if (confirm(`Delete "${title}"?\n\nThis will permanently remove the recording and transcript.`)) {
            this._deleteRecording(id);
        }
    }

    async _deleteRecording(id) {
        try {
            const response = await fetch(`/api/recordings/${id}`, {
                method: 'DELETE',
            });

            if (!response.ok) {
                const error = await response.json();
                throw new Error(error.detail || 'Delete failed');
            }

            // Remove from local list and re-render
            this.recordings = this.recordings.filter(r => r.id !== id);
            this._renderRecordings();

        } catch (error) {
            console.error('Delete failed:', error);
            alert(`Delete failed: ${error.message}`);
        }
    }
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.recordingsPage = new RecordingsPage();
});
