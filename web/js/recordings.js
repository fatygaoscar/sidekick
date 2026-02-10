/**
 * Recordings page logic
 */

class RecordingsPage {
    constructor() {
        this.TRANSCRIPT_PAGE_SIZE = 100;
        this.recordings = [];
        this.currentRecording = null;
        this.transcriptVisibleCount = 0;
        this.selectedTemplate = 'meeting';
        this.promptEdited = false;
        this.promptVisible = false;
        this.obsidianUri = null;
        this.templates = {};

        this.elements = {
            recordingsList: document.getElementById('recordings-list'),
            loadingState: document.getElementById('loading-state'),

            // View modal
            viewModal: document.getElementById('view-modal'),
            viewClose: document.getElementById('view-close'),
            viewTitle: document.getElementById('view-title'),
            viewMeta: document.getElementById('view-meta'),
            viewAudioGroup: document.getElementById('view-audio-group'),
            viewAudioPlayer: document.getElementById('view-audio-player'),
            viewAudioDownload: document.getElementById('view-audio-download'),
            viewTranscriptDownload: document.getElementById('view-transcript-download'),
            viewTranscriptGroup: document.getElementById('view-transcript-group'),
            viewTranscript: document.getElementById('view-transcript'),
            viewTranscriptMoreBtn: document.getElementById('view-transcript-more-btn'),
            resummarizeBtn: document.getElementById('resummarize-btn'),
            viewOpenObsidianBtn: document.getElementById('view-open-obsidian-btn'),

            // Re-summarize modal
            resummarizeModal: document.getElementById('resummarize-modal'),
            resummarizeClose: document.getElementById('resummarize-close'),
            resummarizeCancel: document.getElementById('resummarize-cancel'),
            resummarizeSubmit: document.getElementById('resummarize-submit'),
            resummarizeTitle: document.getElementById('resummarize-title'),
            templateGrid: document.getElementById('resummarize-template-grid'),
            togglePrompt: document.getElementById('resummarize-toggle-prompt'),
            promptContainer: document.getElementById('resummarize-prompt-container'),
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
            processingText: document.getElementById('processing-text'),
            processingStage: document.getElementById('processing-stage'),
            processingOverall: document.getElementById('processing-overall'),
            processingTranscriptionText: document.getElementById('processing-transcription-text'),
            processingSummarizationText: document.getElementById('processing-summarization-text'),
            processingTranscriptionFill: document.getElementById('processing-transcription-fill'),
            processingSummarizationFill: document.getElementById('processing-summarization-fill'),
        };

        this._init();
    }

    async _init() {
        this._bindEvents();
        await this._loadTemplates();
        await this._loadRecordings();
    }

    _bindEvents() {
        // View modal
        this.elements.viewClose.addEventListener('click', () => this._closeViewModal());
        this.elements.viewModal.addEventListener('click', (e) => {
            if (e.target === this.elements.viewModal) this._closeViewModal();
        });
        this.elements.resummarizeBtn.addEventListener('click', () => this._showResummarizeModal());
        this.elements.viewOpenObsidianBtn.addEventListener('click', () => this._openRecordingInObsidian());
        this.elements.viewTranscriptDownload.addEventListener('click', () => this._downloadTranscript());
        this.elements.viewTranscriptMoreBtn.addEventListener('click', () => this._showMoreTranscript());

        // Re-summarize modal
        this.elements.resummarizeClose.addEventListener('click', () => this._closeResummarizeModal());
        this.elements.resummarizeCancel.addEventListener('click', () => this._closeResummarizeModal());
        this.elements.resummarizeSubmit.addEventListener('click', () => this._processResummarize());
        this.elements.resummarizeModal.addEventListener('click', (e) => {
            if (e.target === this.elements.resummarizeModal) this._closeResummarizeModal();
        });

        // Toggle prompt visibility
        this.elements.togglePrompt.addEventListener('click', () => this._togglePromptVisibility());

        // Track if prompt was edited
        this.elements.resummarizeCustomPrompt.addEventListener('input', () => {
            this.promptEdited = true;
            this._autoResizePrompt(this.elements.resummarizeCustomPrompt);
        });

        // Confirmation modal
        this.elements.confirmationClose.addEventListener('click', () => this._closeConfirmationModal());
        this.elements.backToListBtn.addEventListener('click', () => this._closeConfirmationModal());
        this.elements.openObsidianBtn.addEventListener('click', () => this._openObsidian());
        this.elements.confirmationModal.addEventListener('click', (e) => {
            if (e.target === this.elements.confirmationModal) this._closeConfirmationModal();
        });
    }

    async _loadTemplates() {
        try {
            const response = await fetch('/api/templates');
            if (!response.ok) throw new Error('Failed to load templates');
            const data = await response.json();
            this.templates = data.templates;
            this._renderTemplates();
        } catch (error) {
            console.error('Failed to load templates:', error);
            this.templates = { meeting: { name: 'General Meeting', description: 'General meeting notes', prompt: '' } };
            this._renderTemplates();
        }
    }

    _renderTemplates() {
        const grid = this.elements.templateGrid;
        grid.innerHTML = '';

        const order = ['meeting', 'strategic_review', 'working_session', 'standup', 'one_on_one', 'brainstorm', 'custom'];
        const sortedKeys = order.filter(k => k in this.templates);

        sortedKeys.forEach(key => {
            const template = this.templates[key];
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'template-btn';
            btn.dataset.template = key;
            btn.textContent = template.name;
            btn.title = template.description;

            if (key === this.selectedTemplate) {
                btn.classList.add('selected');
            }

            btn.addEventListener('click', () => this._selectTemplate(key));
            grid.appendChild(btn);
        });
    }

    _togglePromptVisibility() {
        this.promptVisible = !this.promptVisible;
        if (this.promptVisible) {
            this.elements.promptContainer.classList.remove('hidden');
            this.elements.togglePrompt.textContent = 'Hide';
            this.elements.togglePrompt.classList.add('active');
            this._autoResizePrompt(this.elements.resummarizeCustomPrompt);
        } else {
            this.elements.promptContainer.classList.add('hidden');
            this.elements.togglePrompt.textContent = 'Show';
            this.elements.togglePrompt.classList.remove('active');
        }
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
        const title = this._buildRecordingDeviceFormattedTitle(rec);
        return `
            <div class="recording-card">
                <div class="recording-date">${dateStr} ${timeStr}</div>
                <div class="recording-title">${this._escapeHtml(title)}</div>
                <div class="recording-meta">
                    <span>Duration: ${duration}</span>
                    ${rec.has_summary ? '<span>Has Summary</span>' : ''}
                </div>
                <div class="recording-actions">
                    <button class="btn view-btn" data-id="${rec.id}">View</button>
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
        const dateLabel = date.toLocaleDateString();
        const timeLabel = date.toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit',
        });

        this.elements.viewTitle.textContent = this._buildRecordingDeviceFormattedTitle(rec);
        this.elements.viewMeta.innerHTML = `
            <span>Date: ${dateLabel} ${timeLabel}</span>
            <span>Duration: ${this._formatDuration(rec.duration_seconds)}</span>
        `;

        if (rec.has_audio && rec.audio_url) {
            this.elements.viewAudioGroup.classList.remove('hidden');
            this.elements.viewAudioPlayer.src = rec.audio_url;
            this.elements.viewAudioDownload.href = rec.audio_download_url || `${rec.audio_url}?download=true`;
            this.elements.viewAudioDownload.classList.remove('hidden');
            this.elements.viewAudioDownload.setAttribute('aria-disabled', 'false');
        } else {
            this.elements.viewAudioGroup.classList.add('hidden');
            this.elements.viewAudioPlayer.removeAttribute('src');
            this.elements.viewAudioPlayer.load();
            this.elements.viewAudioDownload.removeAttribute('href');
            this.elements.viewAudioDownload.classList.add('hidden');
            this.elements.viewAudioDownload.setAttribute('aria-disabled', 'true');
        }

        // Render transcript only after authoritative transcription has been run.
        if (rec.has_transcription && rec.transcript && rec.transcript.length > 0) {
            this.elements.viewTranscriptGroup.classList.remove('hidden');
            this.transcriptVisibleCount = Math.min(this.TRANSCRIPT_PAGE_SIZE, rec.transcript.length);
            this._renderTranscriptPreview();
        } else {
            this.elements.viewTranscriptGroup.classList.add('hidden');
            this.elements.viewTranscript.innerHTML = '';
            this.transcriptVisibleCount = 0;
            this.elements.viewTranscriptMoreBtn.classList.add('hidden');
        }

        this._syncTranscriptActionButton(rec);
        this._syncViewActionButtons(rec);

        this.elements.viewModal.classList.remove('hidden');
    }

    _closeViewModal() {
        this.elements.viewAudioPlayer.pause();
        this.elements.viewModal.classList.add('hidden');
    }

    _showResummarizeModal() {
        if (!this.currentRecording?.has_summary) {
            return;
        }
        this._closeViewModal();

        const title = this.currentRecording?.title || this.currentRecording?.meetings?.[0]?.title || '';
        this.elements.resummarizeTitle.value = title;
        this.promptEdited = false;
        this.promptVisible = false;
        this.elements.promptContainer.classList.add('hidden');
        this.elements.togglePrompt.textContent = 'Show';
        this.elements.togglePrompt.classList.remove('active');
        this._selectTemplate('meeting');

        this.elements.resummarizeModal.classList.remove('hidden');
        this.elements.resummarizeTitle.focus();
    }

    _closeResummarizeModal() {
        this.elements.resummarizeModal.classList.add('hidden');
    }

    _selectTemplate(templateKey) {
        this.selectedTemplate = templateKey;
        this.promptEdited = false;

        const buttons = this.elements.templateGrid.querySelectorAll('.template-btn');
        buttons.forEach(btn => {
            btn.classList.toggle('selected', btn.dataset.template === templateKey);
        });

        const template = this.templates[templateKey];
        if (template) {
            this.elements.resummarizeCustomPrompt.value = template.prompt || '';
            this._autoResizePrompt(this.elements.resummarizeCustomPrompt);
        }
    }

    _autoResizePrompt(textarea) {
        if (!textarea) return;
        textarea.style.height = 'auto';
        textarea.style.height = `${Math.max(textarea.scrollHeight, 240)}px`;
    }

    async _processResummarize() {
        const title = this.elements.resummarizeTitle.value.trim();
        if (!title) {
            this.elements.resummarizeTitle.focus();
            return;
        }

        const template = this.selectedTemplate;
        const promptValue = this.elements.resummarizeCustomPrompt.value.trim();
        const customPrompt = (template === 'custom' || this.promptEdited) ? promptValue : null;

        await this._exportRecording(this.currentRecording.id, title, template, customPrompt);
    }

    _downloadTranscript() {
        const rec = this.currentRecording;
        if (!rec) {
            return;
        }

        if (!rec.has_transcription || !rec.transcript || rec.transcript.length === 0) {
            this._syncTranscriptActionButton(rec, true);
            this._runTranscriptionForCurrentRecording();
            return;
        }

        const lines = rec.transcript.map((seg) => `${seg.timestamp} ${seg.text}`);
        const transcriptText = `${lines.join('\n')}\n`;
        const blob = new Blob([transcriptText], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);

        const baseTitle = this._buildRecordingDeviceFormattedTitle(rec);
        const title = baseTitle.replace(/[<>:"/\\|?*]+/g, '').trim() || 'recording';
        const link = document.createElement('a');
        link.href = url;
        link.download = `${title} - transcript.txt`;
        document.body.appendChild(link);
        link.click();
        link.remove();

        URL.revokeObjectURL(url);
    }

    _syncTranscriptActionButton(rec, isProcessing = false) {
        if (!this.elements.viewTranscriptDownload) return;

        if (isProcessing) {
            this.elements.viewTranscriptDownload.textContent = 'Transcribing...';
            this.elements.viewTranscriptDownload.disabled = true;
            return;
        }

        const ready = !!(rec && rec.has_transcription && rec.transcript && rec.transcript.length > 0);
        this.elements.viewTranscriptDownload.textContent = ready ? 'Download Transcript' : 'Transcribe Audio';
        this.elements.viewTranscriptDownload.disabled = false;
    }

    _renderTranscriptPreview() {
        const rec = this.currentRecording;
        if (!rec || !rec.transcript || rec.transcript.length === 0) {
            this.elements.viewTranscript.innerHTML = '';
            this.elements.viewTranscriptMoreBtn.classList.add('hidden');
            return;
        }

        const visible = rec.transcript.slice(0, this.transcriptVisibleCount);
        this.elements.viewTranscript.innerHTML = visible.map(seg => `
            <div class="transcript-segment${seg.is_important ? ' important' : ''}">
                <span class="timestamp">${seg.timestamp}</span>
                <span class="text">${this._escapeHtml(seg.text)}</span>
            </div>
        `).join('');

        if (this.transcriptVisibleCount < rec.transcript.length) {
            this.elements.viewTranscriptMoreBtn.classList.remove('hidden');
            this.elements.viewTranscriptMoreBtn.textContent = `Show More (${this.transcriptVisibleCount}/${rec.transcript.length})`;
        } else if (rec.transcript.length > this.TRANSCRIPT_PAGE_SIZE) {
            this.elements.viewTranscriptMoreBtn.classList.remove('hidden');
            this.elements.viewTranscriptMoreBtn.textContent = 'Show Less';
        } else {
            this.elements.viewTranscriptMoreBtn.classList.add('hidden');
        }
    }

    _showMoreTranscript() {
        const rec = this.currentRecording;
        if (!rec || !rec.transcript || rec.transcript.length === 0) return;

        if (this.transcriptVisibleCount < rec.transcript.length) {
            this.transcriptVisibleCount = Math.min(
                rec.transcript.length,
                this.transcriptVisibleCount + this.TRANSCRIPT_PAGE_SIZE
            );
        } else {
            this.transcriptVisibleCount = Math.min(this.TRANSCRIPT_PAGE_SIZE, rec.transcript.length);
            this.elements.viewTranscriptGroup.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
        this._renderTranscriptPreview();
    }

    _syncViewActionButtons(rec) {
        const hasSummary = !!rec?.has_summary;
        this.elements.resummarizeBtn.disabled = !hasSummary;
        this.elements.viewOpenObsidianBtn.disabled = !hasSummary || !rec?.open_in_obsidian_uri;
    }

    _openRecordingInObsidian() {
        const rec = this.currentRecording;
        if (!rec || !rec.has_summary || !rec.open_in_obsidian_uri) {
            return;
        }
        window.open(rec.open_in_obsidian_uri, '_blank');
    }

    async _runTranscriptionForCurrentRecording() {
        const rec = this.currentRecording;
        if (!rec) return;

        this._closeViewModal();
        this._setProcessingState({
            stage: 'queued',
            message: 'Preparing transcription...',
            transcriptionProgress: 0,
            summarizationProgress: 0,
            overallProgress: 0,
        });
        this.elements.processingOverlay.classList.remove('hidden');

        try {
            const createResponse = await fetch(`/api/recordings/${rec.id}/transcription-job`, {
                method: 'POST',
            });
            if (!createResponse.ok) {
                const error = await createResponse.json().catch(() => ({}));
                if (createResponse.status === 404 && error.detail === 'Not Found') {
                    throw new Error('Transcription endpoint is unavailable. Restart Sidekick and try again.');
                }
                throw new Error(error.detail || 'Failed to start transcription');
            }

            const job = await createResponse.json();
            await this._waitForTranscriptionJob(job.job_id);
            this.elements.processingOverlay.classList.add('hidden');
            await this._viewRecording(rec.id);
        } catch (error) {
            console.error('Transcription failed:', error);
            this.elements.processingOverlay.classList.add('hidden');
            this._syncTranscriptActionButton(rec, false);
            alert(`Transcription failed: ${error.message}`);
        }
    }

    async _waitForTranscriptionJob(jobId) {
        const maxPollMs = 20 * 60 * 1000;
        const startedAt = Date.now();

        while (Date.now() - startedAt < maxPollMs) {
            const response = await fetch(`/api/transcription-jobs/${jobId}`);
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to read transcription status');
            }

            const job = await response.json();
            this._setProcessingState({
                stage: job.stage,
                message: job.message,
                transcriptionProgress: Number(job.transcription_progress || 0),
                summarizationProgress: 0,
                overallProgress: Number(job.overall_progress || 0),
            });

            if (job.status === 'completed') return;
            if (job.status === 'failed') {
                throw new Error(job.error || 'Transcription job failed');
            }

            await new Promise(resolve => setTimeout(resolve, 900));
        }

        throw new Error('Transcription timed out');
    }

    _buildLocalFormattedTitle(date, title) {
        const yyyy = date.getFullYear();
        const mm = String(date.getMonth() + 1).padStart(2, '0');
        const dd = String(date.getDate()).padStart(2, '0');
        const hh = String(date.getHours()).padStart(2, '0');
        const min = String(date.getMinutes()).padStart(2, '0');
        const baseTitle = (title || 'Untitled Recording').trim() || 'Untitled Recording';
        return `${yyyy}-${mm}-${dd}-${hh}${min} - ${baseTitle}`;
    }

    _buildRecordingDeviceFormattedTitle(rec) {
        const startedAt = new Date(rec.started_at);
        const baseTitle = (rec?.title || 'Untitled Recording').trim() || 'Untitled Recording';

        // Prefer recording device IANA timezone when available.
        if (rec?.timezone_name) {
            try {
                const parts = new Intl.DateTimeFormat('en-CA', {
                    timeZone: rec.timezone_name,
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    hour12: false,
                }).formatToParts(startedAt);
                const byType = Object.fromEntries(parts.map(p => [p.type, p.value]));
                const yyyy = byType.year;
                const mm = byType.month;
                const dd = byType.day;
                const hh = byType.hour;
                const min = byType.minute;
                return `${yyyy}-${mm}-${dd}-${hh}${min} - ${baseTitle}`;
            } catch (_error) {
                // Fall through to numeric-offset fallback.
            }
        }

        // Fallback: use recording device UTC offset if captured.
        if (typeof rec?.timezone_offset_minutes === 'number' && !Number.isNaN(rec.timezone_offset_minutes)) {
            // JS offset semantics: UTC - local (minutes). local = UTC - offset.
            const localMillis = startedAt.getTime() - (rec.timezone_offset_minutes * 60000);
            const shifted = new Date(localMillis);
            const yyyy = shifted.getUTCFullYear();
            const mm = String(shifted.getUTCMonth() + 1).padStart(2, '0');
            const dd = String(shifted.getUTCDate()).padStart(2, '0');
            const hh = String(shifted.getUTCHours()).padStart(2, '0');
            const min = String(shifted.getUTCMinutes()).padStart(2, '0');
            return `${yyyy}-${mm}-${dd}-${hh}${min} - ${baseTitle}`;
        }

        // Legacy recordings without timezone metadata: viewer-local fallback.
        return this._buildLocalFormattedTitle(startedAt, baseTitle);
    }

    async _exportRecording(id, title, template, customPrompt) {
        this.elements.resummarizeModal.classList.add('hidden');
        this._setProcessingState({
            stage: 'queued',
            message: 'Preparing export...',
            transcriptionProgress: 0,
            summarizationProgress: 0,
            overallProgress: 0,
        });
        this.elements.processingOverlay.classList.remove('hidden');

        try {
            const createResponse = await fetch(`/api/recordings/${id}/export-obsidian-job`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    title,
                    template,
                    custom_prompt: customPrompt,
                }),
            });

            if (!createResponse.ok) {
                const error = await createResponse.json();
                throw new Error(error.detail || 'Export failed');
            }

            const job = await createResponse.json();
            const result = await this._waitForExportJob(job.job_id);
            this._showConfirmation(result);

        } catch (error) {
            console.error('Export failed:', error);
            this.elements.processingOverlay.classList.add('hidden');
            alert(`Export failed: ${error.message}`);
        }
    }

    async _waitForExportJob(jobId) {
        const maxPollMs = 20 * 60 * 1000;
        const startedAt = Date.now();

        while (Date.now() - startedAt < maxPollMs) {
            const response = await fetch(`/api/export-jobs/${jobId}`);
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to read export status');
            }

            const job = await response.json();
            this._setProcessingState({
                stage: job.stage,
                message: job.message,
                transcriptionProgress: Number(job.transcription_progress || 0),
                summarizationProgress: Number(job.summarization_progress || 0),
                overallProgress: Number(job.overall_progress || 0),
            });

            if (job.status === 'completed') {
                if (job.result) return job.result;
                throw new Error('Export completed without a result payload');
            }

            if (job.status === 'failed') {
                throw new Error(job.error || 'Export job failed');
            }

            await new Promise(resolve => setTimeout(resolve, 900));
        }

        throw new Error('Export timed out');
    }

    _setProcessingState({ stage, message, transcriptionProgress, summarizationProgress, overallProgress }) {
        if (!this.elements.processingOverlay) return;

        const txPct = Math.max(0, Math.min(100, Math.round(transcriptionProgress * 100)));
        const sumPct = Math.max(0, Math.min(100, Math.round(summarizationProgress * 100)));
        const overallPct = Math.max(0, Math.min(100, Math.round(overallProgress * 100)));

        if (this.elements.processingStage) {
            this.elements.processingStage.textContent = this._formatStage(stage);
        }
        if (this.elements.processingText) {
            this.elements.processingText.textContent = message || 'Processing...';
        }
        if (this.elements.processingOverall) {
            this.elements.processingOverall.textContent = `${overallPct}%`;
        }
        if (this.elements.processingTranscriptionText) {
            this.elements.processingTranscriptionText.textContent = `${txPct}%`;
        }
        if (this.elements.processingTranscriptionFill) {
            this.elements.processingTranscriptionFill.style.width = `${txPct}%`;
            this.elements.processingTranscriptionFill.classList.remove('indeterminate');
        }
        if (this.elements.processingSummarizationFill) {
            const isSummarizing = stage === 'summarizing';
            this.elements.processingSummarizationFill.classList.toggle('indeterminate', isSummarizing);
            if (isSummarizing) {
                this.elements.processingSummarizationFill.style.width = '';
                if (this.elements.processingSummarizationText) {
                    this.elements.processingSummarizationText.textContent = '...';
                }
            } else {
                this.elements.processingSummarizationFill.style.width = `${sumPct}%`;
                if (this.elements.processingSummarizationText) {
                    this.elements.processingSummarizationText.textContent = `${sumPct}%`;
                }
            }
        }
    }

    _formatStage(stage) {
        const map = {
            queued: 'Queued',
            transcribing: 'Transcribing Audio',
            summarizing: 'Generating Summary',
            writing: 'Writing Note',
            completed: 'Completed',
            failed: 'Failed',
        };
        return map[stage] || 'Processing';
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
