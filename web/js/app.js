/**
 * Sidekick recording page.
 *
 * Recording and upload logic stays here. Post-recording review moves into the
 * shared RecordingWorkspace controller.
 */

class SidekickApp {
    constructor() {
        this.state = {
            isRecording: false,
            sessionId: null,
            lastSessionId: null,
            elapsedSeconds: 0,
            recordingStartTime: null,
            livePreviewEnabled: false,
        };

        this.timerInterval = null;
        this.audioCapture = null;
        this.visualizer = null;
        this.ws = null;
        this.audioUploadPromise = null;
        this._pageGuardArmed = false;
        this._historyGuardArmed = false;
        this._suppressHistoryGuardPop = false;

        this.clientId = crypto.randomUUID();
        this.chunkUploads = new Map();
        this.chunkResults = new Map();
        this.expectedChunkCount = 0;
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
        };

        this.workspace = new window.RecordingWorkspace({
            onClose: () => this._resetAfterWorkspace(),
        });

        this._init();
    }

    _init() {
        this._seedMainPageHistoryState();
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
            captureSampleRate: 48000,
            onAudioData: (buffer) => {
                if (this.state.isRecording) {
                    this.ws.sendAudio(buffer);
                }
            },
            onEncodedAudio: (blob, mimeType) => {
                this.fallbackBlob = blob;
                this.fallbackMimeType = mimeType;
            },
            onEncodedChunk: (blob, mimeType, chunkIndex) => {
                this._uploadChunkBestEffort(blob, mimeType, chunkIndex);
            },
            onCaptureStopped: (meta) => {
                this.captureStopMeta = meta;
                this.expectedChunkCount = meta.chunkCount || 0;
                if (this.resolveCaptureStopped) {
                    this.resolveCaptureStopped(meta);
                    this.resolveCaptureStopped = null;
                }
            },
            onLevelUpdate: (level, frequencyData, sampleRate) => {
                this.visualizer.draw(level, frequencyData, sampleRate);
            },
        });
    }

    _bindEvents() {
        this.elements.recordBtn.addEventListener('click', () => this._toggleRecording());

        const armPageGuard = () => this._armMainPageHistoryGuard();
        window.addEventListener('pointerdown', armPageGuard, { passive: true, once: true });
        window.addEventListener('touchstart', armPageGuard, { passive: true, once: true });
        window.addEventListener('keydown', armPageGuard, { once: true });

        window.addEventListener('beforeunload', (event) => {
            if (!this.state.isRecording) {
                return;
            }
            event.preventDefault();
            event.returnValue = '';
        });

        document.addEventListener('touchmove', (event) => {
            if (!document.body.classList.contains('main-page')) {
                return;
            }
            if (document.querySelector('.modal:not(.hidden)')) {
                return;
            }
            event.preventDefault();
        }, { passive: false });

        document.addEventListener('click', (event) => {
            const link = event.target.closest('a[href]');
            if (!link || !this.state.isRecording) {
                return;
            }
            if (link.target === '_blank' || link.hasAttribute('download')) {
                return;
            }
            event.preventDefault();
            this.elements.statusText.textContent = 'Stop recording before leaving this page';
        });

        window.addEventListener('popstate', () => {
            if (this._suppressHistoryGuardPop) {
                this._suppressHistoryGuardPop = false;
                return;
            }
            if (this.state.isRecording && this._historyGuardArmed) {
                window.history.pushState({ __sidekickRecordingGuard: true, __sidekickMainPage: true }, '', window.location.href);
                this.elements.statusText.textContent = 'Stop recording before leaving this page';
                return;
            }
            if (this._pageGuardArmed) {
                window.history.pushState({ __sidekickMainPage: true }, '', window.location.href);
            }
        });

        window.addEventListener('pagehide', () => {
            if (!this.audioCapture?.isCapturing) {
                return;
            }
            try {
                this.ws.endSession();
            } catch (_error) {
                // no-op: page is being hidden/unloaded
            }
            try {
                this.audioCapture.stop();
            } catch (_error) {
                // no-op: page is being hidden/unloaded
            }
            try {
                this.ws.disconnect();
            } catch (_error) {
                // no-op: page is being hidden/unloaded
            }
        });

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'visible' && !this.ws.isConnected) {
                this.ws.connect();
            }
        });
    }

    _onConnected() {
        this.elements.connectionDot.classList.add('connected');
        this.elements.connectionText.textContent = 'Connected';
    }

    _onDisconnected() {
        this.elements.connectionDot.classList.remove('connected');
        this.elements.connectionText.textContent = 'Ready';
    }

    _onState(state) {
        this.state.livePreviewEnabled = !!state.live_preview_enabled;
        if (state.session) {
            this.state.sessionId = state.session.id;
            this.state.lastSessionId = state.session.id;
        } else if (!this.state.isRecording) {
            this.state.sessionId = null;
        }
        this._syncLivePreviewVisibility();
    }

    _onLiveTranscription(message) {
        if (!this.state.livePreviewEnabled || !this.state.isRecording) {
            return;
        }

        const text = (message.text || '').trim();
        if (!text) {
            return;
        }

        const current = this.elements.livePreviewText.textContent.trim();
        const appended = current && current !== 'Listening...' ? `${current}\n${text}` : text;
        this.elements.livePreviewText.textContent = appended.slice(-1000);
    }

    _syncLivePreviewVisibility() {
        const visible = this.state.isRecording && this.state.livePreviewEnabled;
        this.elements.livePreview.classList.toggle('hidden', !visible);
        if (visible && !this.elements.livePreviewText.textContent.trim()) {
            this.elements.livePreviewText.textContent = 'Listening...';
        }
    }

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
            this.state.isRecording = true;
            this._armRecordingHistoryGuard();
            this.elements.recordBtn.classList.add('recording');
            this.elements.recordBtn.textContent = 'Stop';
            this.elements.recordBtn.setAttribute('aria-pressed', 'true');
            this.elements.statusText.textContent = 'Starting recording...';
            this._resetLivePreview();
            this._syncLivePreviewVisibility();
            this._startTimer();
            this.ws.startSession();

            const startedSessionId = await this._waitForSessionId(3000);
            if (!startedSessionId) {
                throw new Error('Failed to start recording session');
            }

            this.state.lastSessionId = startedSessionId;
            this.audioUploadPromise = null;
            this.chunkUploads = new Map();
            this.chunkResults = new Map();
            this.expectedChunkCount = 0;
            this.finalizedChunkAudio = false;
            this.captureStopMeta = null;
            this.captureStoppedPromise = null;
            this.resolveCaptureStopped = null;
            this.fallbackBlob = null;
            this.fallbackMimeType = null;

            this.elements.statusText.textContent = 'Recording';
        } catch (error) {
            console.error('Failed to start recording:', error);
            this.state.isRecording = false;
            this._disarmRecordingHistoryGuard();
            this.audioCapture.stop();
            this.elements.recordBtn.classList.remove('recording');
            this.elements.recordBtn.textContent = 'Record';
            this.elements.recordBtn.setAttribute('aria-pressed', 'false');
            this._stopTimer();
            this.elements.statusText.textContent = 'Could not start session';
            this._syncLivePreviewVisibility();
        }
    }

    async _stopRecording() {
        const sessionId = this.state.sessionId || this.state.lastSessionId;
        if (sessionId) {
            this.state.lastSessionId = sessionId;
        }

        this.captureStoppedPromise = new Promise((resolve) => {
            this.resolveCaptureStopped = resolve;
        });

        this.audioCapture.stop();
        this.visualizer.clear();
        this.ws.endSession();

        this.state.isRecording = false;
        this._disarmRecordingHistoryGuard();
        this.elements.recordBtn.classList.remove('recording');
        this.elements.recordBtn.textContent = 'Record';
        this.elements.recordBtn.setAttribute('aria-pressed', 'false');
        this.elements.statusText.textContent = 'Finalizing recording...';
        this._syncLivePreviewVisibility();
        this._stopTimer();

        if (!sessionId) {
            this.elements.statusText.textContent = 'Missing session';
            return;
        }

        try {
            await this._ensureRecordingAudioPersisted(sessionId);
            this.elements.statusText.textContent = 'Opening workspace...';
            await this.workspace.open(sessionId, {
                autoStartTranscription: true,
                initialTab: 'speakers',
            });
            this.elements.statusText.textContent = 'Review recording';
        } catch (error) {
            console.error('Failed to prepare recording workspace:', error);
            this.elements.statusText.textContent = 'Could not open workspace';
            alert(`Could not open workspace: ${error.message}`);
            this._resetAfterWorkspace();
        }
    }

    _startTimer() {
        this.state.recordingStartTime = Date.now();
        this.state.elapsedSeconds = 0;
        this._updateTimerDisplay();

        this.timerInterval = setInterval(() => {
            if (this.state.recordingStartTime) {
                this.state.elapsedSeconds = Math.floor((Date.now() - this.state.recordingStartTime) / 1000);
                this._updateTimerDisplay();
            }
        }, 250);
    }

    _stopTimer() {
        if (this.timerInterval) {
            clearInterval(this.timerInterval);
            this.timerInterval = null;
        }
        if (this.state.recordingStartTime) {
            this.state.elapsedSeconds = Math.floor((Date.now() - this.state.recordingStartTime) / 1000);
            this._updateTimerDisplay();
        }
    }

    _updateTimerDisplay() {
        const hours = Math.floor(this.state.elapsedSeconds / 3600);
        const minutes = Math.floor((this.state.elapsedSeconds % 3600) / 60);
        const seconds = this.state.elapsedSeconds % 60;
        this.elements.timer.textContent = `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
    }

    _seedMainPageHistoryState() {
        const currentState = window.history.state || {};
        if (currentState.__sidekickMainPage) {
            return;
        }
        window.history.replaceState(
            { ...currentState, __sidekickMainPage: true },
            '',
            window.location.href
        );
    }

    _armMainPageHistoryGuard() {
        if (this._pageGuardArmed) {
            return;
        }
        window.history.pushState({ __sidekickMainPage: true }, '', window.location.href);
        this._pageGuardArmed = true;
    }

    _armRecordingHistoryGuard() {
        if (this._historyGuardArmed) {
            return;
        }
        window.history.pushState({ __sidekickRecordingGuard: true, __sidekickMainPage: true }, '', window.location.href);
        this._historyGuardArmed = true;
    }

    _disarmRecordingHistoryGuard() {
        if (!this._historyGuardArmed) {
            return;
        }
        this._historyGuardArmed = false;
        if (window.history.state && window.history.state.__sidekickRecordingGuard) {
            this._suppressHistoryGuardPop = true;
            window.history.back();
        }
    }

    _resetAfterWorkspace() {
        this.state.elapsedSeconds = 0;
        this.state.recordingStartTime = null;
        this._updateTimerDisplay();
        this.state.sessionId = null;
        this.state.lastSessionId = null;
        this.audioUploadPromise = null;
        this.chunkUploads = new Map();
        this.chunkResults = new Map();
        this.expectedChunkCount = 0;
        this.finalizedChunkAudio = false;
        this.captureStoppedPromise = null;
        this.captureStopMeta = null;
        this.resolveCaptureStopped = null;
        this.fallbackBlob = null;
        this.fallbackMimeType = null;
        this.elements.statusText.textContent = '';
    }

    _resetLivePreview() {
        this.elements.livePreviewText.textContent = 'Listening...';
    }

    async _waitForSessionId(timeoutMs = 3000) {
        const startedAt = Date.now();
        while (Date.now() - startedAt < timeoutMs) {
            if (this.state.sessionId) {
                return this.state.sessionId;
            }
            await new Promise((resolve) => setTimeout(resolve, 50));
        }
        return null;
    }

    async _uploadChunkBestEffort(blob, mimeType, chunkIndex) {
        const sessionId = this.state.sessionId || this.state.lastSessionId;
        if (!sessionId) {
            this.chunkResults.set(chunkIndex, { success: false, error: 'No session ID' });
            return;
        }

        const uploadPromise = this._doChunkUpload(sessionId, blob, mimeType, chunkIndex);
        this.chunkUploads.set(chunkIndex, uploadPromise);
        uploadPromise.then(
            () => {
                this.chunkResults.set(chunkIndex, { success: true });
            },
            (error) => {
                this.chunkResults.set(chunkIndex, { success: false, error: error.message });
            }
        );
    }

    async _doChunkUpload(sessionId, blob, mimeType, chunkIndex) {
        const maxAttempts = 3;
        for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
            try {
                const response = await fetch(`/api/recordings/${sessionId}/audio/chunks/${chunkIndex}`, {
                    method: 'PUT',
                    headers: {
                        'Content-Type': mimeType || 'audio/webm',
                        'X-Client-ID': this.clientId,
                    },
                    body: blob,
                });

                if (response.ok) {
                    return;
                }

                const payload = await response.json().catch(() => ({}));
                throw new Error(payload.detail || `HTTP ${response.status}`);
            } catch (error) {
                if (attempt === maxAttempts) {
                    throw error;
                }
                await new Promise((resolve) => setTimeout(resolve, 100 * attempt));
            }
        }
    }

    async _waitForChunkUploadsToSettle(timeoutMs = 30000) {
        const promises = Array.from(this.chunkUploads.values());
        await Promise.race([
            Promise.allSettled(promises),
            new Promise((_, reject) => setTimeout(() => reject(new Error('Chunk upload timeout')), timeoutMs)),
        ]).catch(() => {});
    }

    _allChunksSucceeded() {
        if (this.expectedChunkCount === 0) {
            return false;
        }
        for (let index = 0; index < this.expectedChunkCount; index += 1) {
            const result = this.chunkResults.get(index);
            if (!result || !result.success) {
                return false;
            }
        }
        return true;
    }

    async _ensureRecordingAudioPersisted(sessionId) {
        if (!sessionId) {
            throw new Error('No recording session found');
        }

        if (this.captureStoppedPromise) {
            await this.captureStoppedPromise;
        }

        await this._waitForChunkUploadsToSettle();
        const allChunksOk = this._allChunksSucceeded();

        if (allChunksOk && this.expectedChunkCount > 0 && !this.finalizedChunkAudio) {
            try {
                await this._finalizeChunkedAudio(sessionId);
                this.finalizedChunkAudio = true;
                return;
            } catch (error) {
                console.warn('Chunk finalization failed, falling back to full blob:', error.message);
            }
        }

        if (this.fallbackBlob) {
            this.audioUploadPromise = this._uploadSessionAudio(
                sessionId,
                this.fallbackBlob,
                this.fallbackMimeType
            );
            await this.audioUploadPromise;
        } else if (!this.finalizedChunkAudio) {
            throw new Error('No audio data available');
        }
    }

    async _finalizeChunkedAudio(sessionId) {
        const response = await fetch(`/api/recordings/${sessionId}/audio/finalize`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-Client-ID': this.clientId,
            },
            body: JSON.stringify({
                mime_type: this.captureStopMeta?.mimeType || this.fallbackMimeType || 'audio/webm',
                expected_chunks: this.expectedChunkCount,
            }),
        });

        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.detail || 'Failed to finalize recording audio');
        }
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
            throw new Error(payload.detail || 'Audio upload failed');
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.app = new SidekickApp();
});
