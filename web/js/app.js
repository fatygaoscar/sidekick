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
            selectedTemplate: 'meeting',
            livePreviewEnabled: false,
        };

        this.timerInterval = null;
        this.audioCapture = null;
        this.visualizer = null;
        this.ws = null;
        this.obsidianUri = null;
        this.audioUploadPromise = null;

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
            templateBtns: document.querySelectorAll('.template-btn'),
            customPromptGroup: document.getElementById('custom-prompt-group'),
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
        };

        this._init();
    }

    _init() {
        this.visualizer = new AudioVisualizer(this.elements.audioCanvas);
        this.visualizer.clear();

        this._initWebSocket();
        this._initAudioCapture();
        this._bindEvents();
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
                const targetSessionId = this.state.sessionId || this.state.lastSessionId;
                if (!targetSessionId) return;
                this.audioUploadPromise = this._uploadSessionAudio(targetSessionId, blob, mimeType);
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

        // Template buttons
        this.elements.templateBtns.forEach(btn => {
            btn.addEventListener('click', () => this._selectTemplate(btn.dataset.template));
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

    // Timer
    _startTimer() {
        this.state.elapsedSeconds = 0;
        this._updateTimerDisplay();

        this.timerInterval = setInterval(() => {
            this.state.elapsedSeconds++;
            this._updateTimerDisplay();
        }, 1000);
    }

    _stopTimer() {
        if (this.timerInterval) {
            clearInterval(this.timerInterval);
            this.timerInterval = null;
        }
    }

    _updateTimerDisplay() {
        const h = Math.floor(this.state.elapsedSeconds / 3600);
        const m = Math.floor((this.state.elapsedSeconds % 3600) / 60);
        const s = this.state.elapsedSeconds % 60;
        this.elements.timer.textContent =
            `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }

    // Naming modal
    _showNamingModal() {
        this.elements.recordingTitle.value = '';
        this.elements.customPrompt.value = '';
        this._selectTemplate('meeting');
        this.elements.namingModal.classList.remove('hidden');
        this.elements.recordingTitle.focus();
    }

    _closeNamingModal() {
        this.elements.namingModal.classList.add('hidden');
        this._resetTimer();
    }

    _selectTemplate(template) {
        this.state.selectedTemplate = template;

        this.elements.templateBtns.forEach(btn => {
            btn.classList.toggle('selected', btn.dataset.template === template);
        });

        if (template === 'custom') {
            this.elements.customPromptGroup.classList.remove('hidden');
            this.elements.customPrompt.focus();
        } else {
            this.elements.customPromptGroup.classList.add('hidden');
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
        const customPrompt = template === 'custom' ? this.elements.customPrompt.value.trim() : null;

        this.elements.namingModal.classList.add('hidden');
        this.elements.processingOverlay.classList.remove('hidden');

        try {
            if (this.audioUploadPromise) {
                await this.audioUploadPromise;
            }

            const response = await fetch(`/api/recordings/${exportSessionId}/export-obsidian`, {
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
        this._updateTimerDisplay();
        this.state.sessionId = null;
        this.audioUploadPromise = null;
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
}

// Initialize when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new SidekickApp();
});
