/**
 * Sidekick - Simplified Recording App
 */

class SidekickApp {
    constructor() {
        this.state = {
            isRecording: false,
            sessionId: null,
            lastSessionId: null,
            elapsedSeconds: 0,
            recordingStartTime: null,
            selectedTemplate: 'meeting',
            livePreviewEnabled: false,
            promptVisible: false,
            promptEdited: false,
        };

        this.templates = {};

        this.timerInterval = null;
        this.audioCapture = null;
        this.visualizer = null;
        this.ws = null;
        this.obsidianUri = null;
        this.audioUploadPromise = null;
        this.pendingChunks = [];
        this.isUploadingChunks = false;
        this.uploadedChunkCount = 0;
        this.finalizedChunkAudio = false;
        this.captureStoppedPromise = null;
        this.captureStopMeta = null;
        this.resolveCaptureStopped = null;
        this.fallbackBlob = null;
        this.fallbackMimeType = null;

        this.elements = {
            recordBtn: document.getElementById('record-btn'),
            timer: document.getElementById('timer'),
            audioCanvas: document.getElementById('audio-canvas'),
            statusText: document.getElementById('status-text'),
            connectionDot: document.getElementById('connection-dot'),
            connectionText: document.getElementById('connection-text'),
            livePreview: document.getElementById('live-preview'),
            livePreviewText: document.getElementById('live-preview-text'),

            // Naming modal
            namingModal: document.getElementById('naming-modal'),
            namingClose: document.getElementById('naming-close'),
            namingCancel: document.getElementById('naming-cancel'),
            namingSubmit: document.getElementById('naming-submit'),
            recordingTitle: document.getElementById('recording-title'),
            templateGrid: document.getElementById('template-grid'),
            togglePrompt: document.getElementById('toggle-prompt'),
            promptContainer: document.getElementById('prompt-container'),
            customPrompt: document.getElementById('custom-prompt'),

            // Confirmation modal
            confirmationModal: document.getElementById('confirmation-modal'),
            confirmationClose: document.getElementById('confirmation-close'),
            confirmationFilename: document.getElementById('confirmation-filename'),
            confirmationPreview: document.getElementById('confirmation-preview'),
            newRecordingBtn: document.getElementById('new-recording-btn'),
            openObsidianBtn: document.getElementById('open-obsidian-btn'),

            // Processing overlay
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

    _init() {
        this.visualizer = new AudioVisualizer(this.elements.audioCanvas);
        this.visualizer.clear();

        this._initWebSocket();
        this._initAudioCapture();
        this._bindEvents();
        this._loadTemplates();
    }

    _initWebSocket() {
        this.ws = new SidekickWebSocket({
            onOpen: () => this._onConnected(),
            onClose: () => this._onDisconnected(),
            onState: (state) => this._onState(state),
            onTranscription: (message) => this._onLiveTranscription(message),
        });
        this.ws.connect();
    }

    _initAudioCapture() {
        this.audioCapture = new AudioCapture({
            sampleRate: 16000,
            onAudioData: (buffer) => {
                if (this.state.isRecording) {
                    this.ws.sendAudio(buffer);
                }
            },
            onEncodedAudio: (blob, mimeType) => {
                // Keep full blob as fallback if chunk persistence fails.
                this.fallbackBlob = blob;
                this.fallbackMimeType = mimeType;
            },
            onEncodedChunk: (blob, mimeType, chunkIndex) => {
                this.pendingChunks.push({ blob, mimeType, chunkIndex });
                this._processChunkQueue();
            },
            onCaptureStopped: (meta) => {
                this.captureStopMeta = meta;
                if (this.resolveCaptureStopped) {
                    this.resolveCaptureStopped(meta);
                    this.resolveCaptureStopped = null;
                }
            },
            onLevelUpdate: (level, frequencyData) => {
                this.visualizer.draw(level, frequencyData);
            },
        });
    }

    _bindEvents() {
        // Record button
        this.elements.recordBtn.addEventListener('click', () => this._toggleRecording());

        // Naming modal
        this.elements.namingClose.addEventListener('click', () => this._closeNamingModal());
        this.elements.namingCancel.addEventListener('click', () => this._closeNamingModal());
        this.elements.namingSubmit.addEventListener('click', () => this._processRecording());
        this.elements.namingModal.addEventListener('click', (e) => {
            if (e.target === this.elements.namingModal) this._closeNamingModal();
        });

        // Template buttons are bound dynamically in _renderTemplates

        // Toggle prompt visibility
        this.elements.togglePrompt.addEventListener('click', () => this._togglePromptVisibility());

        // Track if prompt was edited
        this.elements.customPrompt.addEventListener('input', () => {
            this.state.promptEdited = true;
        });

        // Confirmation modal
        this.elements.confirmationClose.addEventListener('click', () => this._closeConfirmationModal());
        this.elements.newRecordingBtn.addEventListener('click', () => this._closeConfirmationModal());
        this.elements.openObsidianBtn.addEventListener('click', () => this._openObsidian());
        this.elements.confirmationModal.addEventListener('click', (e) => {
            if (e.target === this.elements.confirmationModal) this._closeConfirmationModal();
        });

        // Enter key in title input
        this.elements.recordingTitle.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this._processRecording();
        });

        // Reconnect WebSocket when tab becomes visible (browser may have killed connection)
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible' && !this.ws.isConnected) {
                console.log('Tab visible, reconnecting WebSocket...');
                this.ws.connect();
            }
        });
    }

    // Connection handlers
    _onConnected() {
        this.elements.connectionDot.classList.add('connected');
        this.elements.connectionText.textContent = 'Connected';
    }

    _onDisconnected() {
        this.elements.connectionDot.classList.remove('connected');
        this.elements.connectionText.textContent = 'Disconnected';
    }

    _onState(state) {
        this.state.livePreviewEnabled = !!state.live_preview_enabled;
        if (state.session) {
            this.state.sessionId = state.session.id;
            this.state.lastSessionId = state.session.id;
        } else {
            this.state.sessionId = null;
        }
        this._syncLivePreviewVisibility();
    }

    _onLiveTranscription(message) {
        if (!this.state.livePreviewEnabled || !this.state.isRecording) return;
        const text = (message.text || '').trim();
        if (!text) return;

        const current = this.elements.livePreviewText.textContent.trim();
        const appended = current && current !== 'Listening...' ? `${current}\n${text}` : text;
        // Keep preview concise; export path is authoritative transcript.
        const maxChars = 1000;
        this.elements.livePreviewText.textContent = appended.slice(-maxChars);
    }

    _syncLivePreviewVisibility() {
        const visible = this.state.isRecording && this.state.livePreviewEnabled;
        this.elements.livePreview.classList.toggle('hidden', !visible);
        if (visible && !this.elements.livePreviewText.textContent.trim()) {
            this.elements.livePreviewText.textContent = 'Listening...';
        }
    }

    // Recording flow
    async _toggleRecording() {
        if (this.state.isRecording) {
            await this._stopRecording();
        } else {
            await this._startRecording();
        }
    }

    async _startRecording() {
        try {
            this.elements.statusText.textContent = 'Starting...';
            this.state.sessionId = null;
            this.state.lastSessionId = null;

            await this.audioCapture.start();
            this.ws.startSession();
            const startedSessionId = await this._waitForSessionId(3000);
            if (!startedSessionId) {
                throw new Error('Failed to start recording session');
            }
            this.state.lastSessionId = startedSessionId;
            this.audioUploadPromise = null;
            this.pendingChunks = [];
            this.isUploadingChunks = false;
            this.uploadedChunkCount = 0;
            this.finalizedChunkAudio = false;
            this.captureStopMeta = null;
            this.captureStoppedPromise = null;
            this.resolveCaptureStopped = null;
            this.fallbackBlob = null;
            this.fallbackMimeType = null;

            this.state.isRecording = true;
            this.elements.recordBtn.classList.add('recording');
            this.elements.recordBtn.textContent = 'Stop';
            this.elements.statusText.textContent = 'Recording';
            this._resetLivePreview();
            this._syncLivePreviewVisibility();

            this._startTimer();
        } catch (error) {
            console.error('Failed to start recording:', error);
            this.audioCapture.stop();
            this.elements.statusText.textContent = 'Could not start session';
            this._syncLivePreviewVisibility();
        }
    }

    async _stopRecording() {
        const sessionId = this.state.sessionId;
        this.state.lastSessionId = sessionId;
        this.captureStoppedPromise = new Promise((resolve) => {
            this.resolveCaptureStopped = resolve;
        });
        this.audioCapture.stop();
        this.visualizer.clear();
        this.ws.endSession();

        this.state.isRecording = false;
        this.elements.recordBtn.classList.remove('recording');
        this.elements.recordBtn.textContent = 'Record';
        this.elements.statusText.textContent = 'Ready';
        this._syncLivePreviewVisibility();

        this._stopTimer();
        this._showNamingModal();

        if (!sessionId) {
            console.warn('No session id available for audio upload');
        }
    }

    // Timer - uses wall clock to avoid drift when tab is inactive
    _startTimer() {
        this.state.recordingStartTime = Date.now();
        this.state.elapsedSeconds = 0;
        this._updateTimerDisplay();

        this.timerInterval = setInterval(() => {
            if (this.state.recordingStartTime) {
                this.state.elapsedSeconds = Math.floor((Date.now() - this.state.recordingStartTime) / 1000);
                this._updateTimerDisplay();
            }
        }, 250); // Update more frequently for smoother display after tab switch
    }

    _stopTimer() {
        if (this.timerInterval) {
            clearInterval(this.timerInterval);
            this.timerInterval = null;
        }
        // Capture final elapsed time from wall clock
        if (this.state.recordingStartTime) {
            this.state.elapsedSeconds = Math.floor((Date.now() - this.state.recordingStartTime) / 1000);
            this._updateTimerDisplay();
        }
    }

    _updateTimerDisplay() {
        const h = Math.floor(this.state.elapsedSeconds / 3600);
        const m = Math.floor((this.state.elapsedSeconds % 3600) / 60);
        const s = this.state.elapsedSeconds % 60;
        this.elements.timer.textContent =
            `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }

    // Templates
    async _loadTemplates() {
        try {
            const response = await fetch('/api/templates');
            if (!response.ok) throw new Error('Failed to load templates');
            const data = await response.json();
            this.templates = data.templates;
            this._renderTemplates();
        } catch (error) {
            console.error('Failed to load templates:', error);
            // Fallback to basic template
            this.templates = {
                meeting: { name: 'General Meeting', description: 'General meeting notes', prompt: '' }
            };
            this._renderTemplates();
        }
    }

    _renderTemplates() {
        const grid = this.elements.templateGrid;
        grid.innerHTML = '';

        // Show only primary templates in the requested UX order.
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

            if (key === this.state.selectedTemplate) {
                btn.classList.add('selected');
            }

            btn.addEventListener('click', () => this._selectTemplate(key));
            grid.appendChild(btn);
        });
    }

    // Naming modal
    _showNamingModal() {
        this.elements.recordingTitle.value = '';
        this.state.promptEdited = false;
        this.state.promptVisible = false;
        this.elements.promptContainer.classList.add('hidden');
        this.elements.togglePrompt.textContent = 'Show';
        this.elements.togglePrompt.classList.remove('active');
        this._selectTemplate('meeting');
        this.elements.namingModal.classList.remove('hidden');
        this.elements.recordingTitle.focus();
    }

    _closeNamingModal() {
        this.elements.namingModal.classList.add('hidden');
        this._resetTimer();
    }

    _selectTemplate(templateKey) {
        this.state.selectedTemplate = templateKey;
        this.state.promptEdited = false;

        // Update button states
        const buttons = this.elements.templateGrid.querySelectorAll('.template-btn');
        buttons.forEach(btn => {
            btn.classList.toggle('selected', btn.dataset.template === templateKey);
        });

        // Load the template prompt
        const template = this.templates[templateKey];
        if (template) {
            this.elements.customPrompt.value = template.prompt || '';
        }
    }

    _togglePromptVisibility() {
        this.state.promptVisible = !this.state.promptVisible;
        if (this.state.promptVisible) {
            this.elements.promptContainer.classList.remove('hidden');
            this.elements.togglePrompt.textContent = 'Hide';
            this.elements.togglePrompt.classList.add('active');
        } else {
            this.elements.promptContainer.classList.add('hidden');
            this.elements.togglePrompt.textContent = 'Show';
            this.elements.togglePrompt.classList.remove('active');
        }
    }

    async _processRecording() {
        const title = this.elements.recordingTitle.value.trim();
        if (!title) {
            this.elements.recordingTitle.focus();
            return;
        }

        const exportSessionId = this.state.sessionId || this.state.lastSessionId;
        if (!exportSessionId) {
            alert('No recording session found');
            return;
        }

        const template = this.state.selectedTemplate;
        // Send custom_prompt if: it's a custom template OR the user edited the prompt
        const promptValue = this.elements.customPrompt.value.trim();
        const customPrompt = (template === 'custom' || this.state.promptEdited) ? promptValue : null;

        this.elements.namingModal.classList.add('hidden');
        this._setProcessingState({
            stage: 'queued',
            message: 'Preparing export...',
            transcriptionProgress: 0,
            summarizationProgress: 0,
            overallProgress: 0,
        });
        this.elements.processingOverlay.classList.remove('hidden');

        try {
            await this._ensureRecordingAudioPersisted(exportSessionId);

            const createResponse = await fetch(`/api/recordings/${exportSessionId}/export-obsidian-job`, {
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
            this._resetTimer();
        }
    }

    // Confirmation modal
    _showConfirmation(result) {
        this.elements.processingOverlay.classList.add('hidden');
        this.obsidianUri = result.obsidian_uri;

        this.elements.confirmationFilename.textContent = result.filename;
        this.elements.confirmationPreview.textContent = result.summary_preview;
        this.elements.confirmationModal.classList.remove('hidden');
    }

    _closeConfirmationModal() {
        this.elements.confirmationModal.classList.add('hidden');
        this._resetTimer();
    }

    _openObsidian() {
        if (this.obsidianUri) {
            window.open(this.obsidianUri, '_blank');
        }
        this._closeConfirmationModal();
    }

    _resetTimer() {
        this.state.elapsedSeconds = 0;
        this.state.recordingStartTime = null;
        this._updateTimerDisplay();
        this.state.sessionId = null;
        this.audioUploadPromise = null;
        this.pendingChunks = [];
        this.isUploadingChunks = false;
        this.uploadedChunkCount = 0;
        this.finalizedChunkAudio = false;
        this.captureStoppedPromise = null;
        this.captureStopMeta = null;
        this.resolveCaptureStopped = null;
        this.fallbackBlob = null;
        this.fallbackMimeType = null;
    }

    _resetLivePreview() {
        this.elements.livePreviewText.textContent = 'Listening...';
    }

    async _uploadSessionAudio(sessionId, blob, mimeType) {
        const response = await fetch(`/api/recordings/${sessionId}/audio`, {
            method: 'PUT',
            headers: {
                'Content-Type': mimeType || 'audio/webm',
            },
            body: blob,
        });

        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            const error = new Error(payload.detail || 'Audio upload failed');
            console.error('Failed to upload session audio:', error);
            throw error;
        }
    }

    async _waitForSessionId(timeoutMs = 3000) {
        const startedAt = Date.now();
        while (Date.now() - startedAt < timeoutMs) {
            if (this.state.sessionId) return this.state.sessionId;
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        return null;
    }

    async _processChunkQueue() {
        if (this.isUploadingChunks) return;
        this.isUploadingChunks = true;

        try {
            while (this.pendingChunks.length > 0) {
                const sessionId = this.state.sessionId || this.state.lastSessionId;
                if (!sessionId) break;

                const nextChunk = this.pendingChunks[0];
                await this._uploadSessionAudioChunk(sessionId, nextChunk);
                this.pendingChunks.shift();
                this.uploadedChunkCount += 1;
            }
        } finally {
            this.isUploadingChunks = false;
        }
    }

    async _uploadSessionAudioChunk(sessionId, chunk) {
        const maxAttempts = 4;
        for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
            try {
                const response = await fetch(
                    `/api/recordings/${sessionId}/audio/chunks/${chunk.chunkIndex}`,
                    {
                        method: 'PUT',
                        headers: {
                            'Content-Type': chunk.mimeType || 'audio/webm',
                        },
                        body: chunk.blob,
                    }
                );

                if (response.ok) {
                    return;
                }

                const payload = await response.json().catch(() => ({}));
                if (response.status === 409) {
                    const expected = Number(
                        payload.expected_chunk_index
                        || response.headers.get('X-Expected-Chunk-Index')
                    );
                    if (!Number.isNaN(expected) && chunk.chunkIndex < expected) {
                        return;
                    }
                    throw new Error(payload.detail || 'Chunk index out of order');
                }
                throw new Error(payload.detail || 'Audio chunk upload failed');
            } catch (error) {
                if (attempt === maxAttempts) {
                    throw error;
                }
                await new Promise(resolve => setTimeout(resolve, 250 * attempt));
            }
        }
    }

    async _ensureRecordingAudioPersisted(sessionId) {
        if (!sessionId) {
            throw new Error('No recording session found');
        }

        if (this.captureStoppedPromise) {
            await this.captureStoppedPromise;
        }

        await this._processChunkQueue();
        await this._waitForChunkQueueDrain();

        if (this.uploadedChunkCount > 0 && !this.finalizedChunkAudio) {
            await this._finalizeChunkedAudio(sessionId);
            this.finalizedChunkAudio = true;
            return;
        }

        if (!this.uploadedChunkCount && this.fallbackBlob) {
            this.audioUploadPromise = this._uploadSessionAudio(sessionId, this.fallbackBlob, this.fallbackMimeType);
            await this.audioUploadPromise;
        }
    }

    async _waitForChunkQueueDrain(timeoutMs = 20000) {
        const startedAt = Date.now();
        while (Date.now() - startedAt < timeoutMs) {
            if (!this.isUploadingChunks && this.pendingChunks.length === 0) {
                return;
            }
            await new Promise(resolve => setTimeout(resolve, 60));
            if (!this.isUploadingChunks && this.pendingChunks.length > 0) {
                await this._processChunkQueue();
            }
        }
        throw new Error('Timed out while uploading recording audio');
    }

    async _finalizeChunkedAudio(sessionId) {
        const response = await fetch(`/api/recordings/${sessionId}/audio/finalize`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                mime_type: this.captureStopMeta?.mimeType || this.fallbackMimeType || 'audio/webm',
                uploaded_chunks: this.uploadedChunkCount,
            }),
        });

        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            const error = new Error(payload.detail || 'Failed to finalize recording audio');
            console.error('Failed to finalize chunked audio:', error);
            throw error;
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

    _setProcessingState({
        stage,
        message,
        transcriptionProgress,
        summarizationProgress,
        overallProgress,
    }) {
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
        if (this.elements.processingSummarizationText) {
            this.elements.processingSummarizationText.textContent = `${sumPct}%`;
        }
        if (this.elements.processingTranscriptionFill) {
            this.elements.processingTranscriptionFill.style.width = `${txPct}%`;
            this.elements.processingTranscriptionFill.classList.remove('indeterminate');
        }
        if (this.elements.processingSummarizationFill) {
            // Use indeterminate animation during summarization (LLM timing is unpredictable)
            const isSummarizing = stage === 'summarizing';
            this.elements.processingSummarizationFill.classList.toggle('indeterminate', isSummarizing);
            if (isSummarizing) {
                this.elements.processingSummarizationFill.style.width = '';
                if (this.elements.processingSummarizationText) {
                    this.elements.processingSummarizationText.textContent = '...';
                }
            } else {
                this.elements.processingSummarizationFill.style.width = `${sumPct}%`;
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
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new SidekickApp();
});
