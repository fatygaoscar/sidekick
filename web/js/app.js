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
            isStartingRecording: false,
            isStoppingRecording: false,
            isUploadingFile: false,
            sessionId: null,
            lastSessionId: null,
            elapsedSeconds: 0,
            recordingStartTime: null,
            livePreviewEnabled: false,
            previewConnectionState: 'idle',
        };

        this.timerInterval = null;
        this.audioCapture = null;
        this.visualizer = null;
        this.ws = null;
        this.audioUploadPromise = null;
        this.previewAttachPromise = null;
        this._pageGuardArmed = false;
        this._historyGuardArmed = false;
        this._suppressHistoryGuardPop = false;

        this.clientId = crypto.randomUUID();
        this.chunkUploads = new Map();
        this.chunkResults = new Map();
        this.pendingChunks = new Map();
        this.expectedChunkCount = 0;
        this.captureStopMeta = null;
        this.fallbackBlob = null;
        this.fallbackMimeType = null;

        this.elements = {
            recordBtn: document.getElementById('record-btn'),
            uploadBtn: document.getElementById('upload-btn'),
            uploadInput: document.getElementById('upload-input'),
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
        this._syncPrimaryControls();
    }

    _initWebSocket() {
        this.ws = new SidekickWebSocket({
            onOpen: () => this._onConnected(),
            onClose: () => this._onDisconnected(),
            onError: () => this._onWebSocketError(),
            onState: (state) => this._onState(state),
            onTranscription: (message) => this._onLiveTranscription(message),
        });
    }

    _initAudioCapture() {
        this.audioCapture = new AudioCapture({
            sampleRate: 16000,
            captureSampleRate: 48000,
            onAudioData: (buffer) => {
                if (
                    this.state.isRecording
                    && this.state.livePreviewEnabled
                    && this.state.previewConnectionState === 'connected'
                ) {
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
            },
            onLevelUpdate: (level, frequencyData, sampleRate) => {
                this.visualizer.draw(level, frequencyData, sampleRate);
            },
        });
    }

    _bindEvents() {
        this.elements.recordBtn.addEventListener('click', () => this._toggleRecording());
        this.elements.uploadBtn?.addEventListener('click', () => this._handleUploadClick());
        this.elements.uploadInput?.addEventListener('change', (event) => this._handleUploadSelection(event));

        const armPageGuard = () => this._armMainPageHistoryGuard();
        window.addEventListener('pointerdown', armPageGuard, { passive: true, once: true });
        window.addEventListener('touchstart', armPageGuard, { passive: true, once: true });
        window.addEventListener('keydown', armPageGuard, { once: true });

        window.addEventListener('beforeunload', (event) => {
            if (
                !this.state.isRecording
                && !this.state.isStartingRecording
                && !this.state.isStoppingRecording
                && !this.state.isUploadingFile
            ) {
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
            if (
                !link
                || (
                    !this.state.isRecording
                    && !this.state.isStartingRecording
                    && !this.state.isStoppingRecording
                    && !this.state.isUploadingFile
                )
            ) {
                return;
            }
            if (link.target === '_blank' || link.hasAttribute('download')) {
                return;
            }
            event.preventDefault();
            this.elements.statusText.textContent = this.state.isUploadingFile
                ? 'Wait for the upload to finish before leaving this page'
                : 'Stop recording before leaving this page';
        });

        window.addEventListener('popstate', () => {
            if (this._suppressHistoryGuardPop) {
                this._suppressHistoryGuardPop = false;
                return;
            }
            if ((this.state.isRecording || this.state.isStartingRecording || this.state.isStoppingRecording) && this._historyGuardArmed) {
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
            if (
                document.visibilityState === 'visible'
                && this.state.isRecording
                && !this.ws.isConnected
            ) {
                void this._connectPreviewSocket(this.state.sessionId || this.state.lastSessionId);
            }
        });
    }

    _onConnected() {
        if (
            this.state.isRecording
            && (this.state.sessionId || this.state.lastSessionId)
            && this.state.previewConnectionState !== 'connected'
            && !this.previewAttachPromise
        ) {
            void this._connectPreviewSocket(this.state.sessionId || this.state.lastSessionId);
            return;
        }
        this._syncConnectionStatus();
    }

    _onDisconnected() {
        this.state.previewConnectionState = this.state.isRecording ? 'degraded' : 'idle';
        this._syncConnectionStatus();
    }

    _onWebSocketError() {
        if (!this.state.isRecording) {
            return;
        }
        this.state.previewConnectionState = 'degraded';
        this._syncConnectionStatus();
    }

    _onState(state) {
        this.state.livePreviewEnabled = !!state.live_preview_enabled;
        this._syncLivePreviewVisibility();
        this._syncConnectionStatus();
    }

    _syncConnectionStatus() {
        const dot = this.elements.connectionDot;
        const text = this.elements.connectionText;
        if (!dot || !text) {
            return;
        }

        const recordingActive = this.state.isRecording || this.state.isStartingRecording || this.state.isStoppingRecording;
        if (!recordingActive) {
            dot.classList.remove('connected');
            text.textContent = 'Ready';
            return;
        }

        if (this.state.previewConnectionState === 'connected' && this.state.livePreviewEnabled) {
            dot.classList.add('connected');
            text.textContent = 'Connected';
            return;
        }

        dot.classList.remove('connected');
        text.textContent = 'Preview unavailable';
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
        if (this.state.isUploadingFile) {
            this.elements.statusText.textContent = 'Wait for the upload to finish';
            return;
        }
        if (this.state.isStartingRecording || this.state.isStoppingRecording) {
            return;
        }
        if (this.state.isRecording) {
            await this._stopRecording();
        } else {
            await this._startRecording();
        }
    }

    _syncPrimaryControls() {
        if (this.elements.recordBtn) {
            this.elements.recordBtn.disabled = (
                this.state.isUploadingFile
                || this.state.isStartingRecording
                || this.state.isStoppingRecording
            );
        }
        if (this.elements.uploadBtn) {
            this.elements.uploadBtn.disabled = (
                this.state.isRecording
                || this.state.isStartingRecording
                || this.state.isStoppingRecording
                || this.state.isUploadingFile
            );
            this.elements.uploadBtn.textContent = this.state.isUploadingFile ? 'Uploading...' : 'Upload File';
        }
    }

    _handleUploadClick() {
        if (this.state.isRecording || this.state.isStartingRecording || this.state.isStoppingRecording) {
            this.elements.statusText.textContent = 'Stop recording before uploading a file';
            return;
        }
        if (this.state.isUploadingFile) {
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

    async _startRecording() {
        try {
            this.state.isStartingRecording = true;
            this._syncPrimaryControls();
            this.elements.statusText.textContent = 'Starting...';
            this._resetRecordingBootstrapState();

            await this.audioCapture.start();
            const sessionId = await this._createSession({
                timeoutMs: 15000,
                retries: 1,
                networkErrorMessage: 'Network request failed while creating recording session',
                httpErrorMessage: 'Failed to create recording session',
                logLabel: 'recording:create_session',
            });

            this.state.sessionId = sessionId;
            this.state.lastSessionId = sessionId;
            this._beginRecordingUi(sessionId);
            void this._connectPreviewSocket(sessionId);

            this.elements.statusText.textContent = 'Recording';
        } catch (error) {
            console.error('Failed to start recording:', error);
            try {
                this.audioCapture.stop();
            } catch (_stopError) {
                // no-op: best effort cleanup after failed startup
            }
            try {
                await this._disconnectPreviewSocket(this.state.sessionId || this.state.lastSessionId, {
                    flushBuffer: false,
                });
            } catch (_disconnectError) {
                // no-op: preview is best effort during cleanup
            }
            if (this.state.sessionId) {
                try {
                    await this._endSession(this.state.sessionId, {
                        networkErrorMessage: 'Network request failed while cleaning up failed recording session',
                        logLabel: 'recording:cleanup_session',
                    });
                } catch (cleanupError) {
                    console.warn('Failed to clean up recording session after start failure:', cleanupError?.message || cleanupError);
                }
            }
            this._resetRecordingBootstrapState();
            this.elements.statusText.textContent = 'Could not start session';
        } finally {
            this.state.isStartingRecording = false;
            this._syncPrimaryControls();
            this._syncConnectionStatus();
        }
    }

    async _stopRecording() {
        const sessionId = this.state.sessionId || this.state.lastSessionId;
        if (sessionId) {
            this.state.lastSessionId = sessionId;
        }

        if (!sessionId) {
            this._resetRecordingBootstrapState();
            this.elements.statusText.textContent = 'Could not stop recording';
            return;
        }

        this.state.isStoppingRecording = true;
        this._syncPrimaryControls();
        const captureStopPromise = this.audioCapture.stopAndWait();
        this.visualizer.clear();
        const previewDisconnectPromise = this._disconnectPreviewSocket(sessionId, {
            flushBuffer: true,
        });
        this._endRecordingUi();
        this.elements.statusText.textContent = 'Finalizing recording...';

        try {
            this.captureStopMeta = await captureStopPromise;
            this.expectedChunkCount = this.captureStopMeta?.chunkCount || 0;
            await this._waitForChunkUploadsToSettle();
            let completion = await this._waitForRecordingCompletion(sessionId, {
                mimeType: this.captureStopMeta?.mimeType || this.fallbackMimeType || 'audio/webm',
                expectedChunks: this.expectedChunkCount,
            });
            if (!completion?.workspace_ready && this.fallbackBlob) {
                this.elements.statusText.textContent = 'Uploading backup audio...';
                await this._uploadSessionAudio(
                    sessionId,
                    this.fallbackBlob,
                    this.fallbackMimeType || this.captureStopMeta?.mimeType || 'audio/webm'
                );
                this.elements.statusText.textContent = 'Finalizing recording...';
                completion = await this._waitForRecordingCompletion(sessionId, {
                    mimeType: this.captureStopMeta?.mimeType || this.fallbackMimeType || 'audio/webm',
                    expectedChunks: this.expectedChunkCount,
                    allowFallbackBlob: true,
                    retryWindowMs: 15000,
                });
            }
            if (!completion?.workspace_ready) {
                const reason = completion?.recoverable_from_chunks
                    ? 'Recording audio is still recoverable, but finalization did not complete. Reopen it from History and try again.'
                    : completion?.reason === 'missing_chunks'
                        ? 'Recording audio is incomplete'
                        : 'Recording audio is not ready yet';
                throw new Error(reason);
            }
            await previewDisconnectPromise.catch((error) => {
                console.warn('Failed to disconnect preview socket cleanly:', error?.message || error);
            });
            this.elements.statusText.textContent = 'Opening workspace...';
            console.info('[recording_stop:open_workspace:start]', { sessionId });
            await this.workspace.open(sessionId, {
                autoStartTranscription: true,
                initialTab: 'speakers',
                workspaceLoadTimeoutMs: 15000,
                workspaceLoadRetries: 3,
            });
            console.info('[recording_stop:open_workspace:ok]', { sessionId });
            this.elements.statusText.textContent = 'Review recording';
        } catch (error) {
            console.error('Failed to prepare recording workspace:', error);
            console.warn('[recording_stop:fail]', {
                sessionId,
                message: error?.message || 'Recording finalization failed',
            });
            this._resetAfterWorkspace({
                preserveLastSessionId: true,
                statusText: error?.message || 'Recording finalization failed',
            });
            alert(error?.message || 'Failed to finalize recording');
        } finally {
            this.state.isStoppingRecording = false;
            this._syncPrimaryControls();
            this._syncConnectionStatus();
        }
    }

    async _startFileUpload(file) {
        if (this.state.isRecording || this.state.isStartingRecording || this.state.isStoppingRecording) {
            this.elements.statusText.textContent = 'Stop recording before uploading a file';
            return;
        }
        if (this.state.isUploadingFile) {
            return;
        }

        const extension = this._inferUploadExtension(file);
        if (!this._isSupportedUpload(file, extension)) {
            this.elements.statusText.textContent = 'Unsupported file type';
            alert('Unsupported file type. Choose MP3, WAV, M4A, or MP4.');
            return;
        }

        let sessionId = null;
        this.state.isUploadingFile = true;
        this._syncPrimaryControls();
        this.elements.statusText.textContent = 'Preparing upload...';

        try {
            sessionId = await this._createSession({
                timeoutMs: 15000,
                retries: 1,
                networkErrorMessage: 'Network request failed while creating upload session',
                httpErrorMessage: 'Failed to create upload session',
                logLabel: 'upload:create_session',
            });
            this.state.sessionId = sessionId;
            this.state.lastSessionId = sessionId;

            this.elements.statusText.textContent = 'Uploading file...';
            await this._uploadImportedAudio(sessionId, file, extension);
            await this._seedUploadedRecordingTitle(sessionId, file.name);
            await this._finalizeUploadSession(sessionId);

            this.elements.statusText.textContent = 'Opening workspace...';
            await this.workspace.open(sessionId, {
                autoStartTranscription: false,
                transcriptionStartMode: 'manual',
                workspaceLoadTimeoutMs: 15000,
                workspaceLoadRetries: 3,
            });
            this.elements.statusText.textContent = 'Review upload';
        } catch (error) {
            console.error('Failed to upload file:', error);
            if (sessionId) {
                await this._cleanupFailedUploadSession(sessionId);
            }
            this.elements.statusText.textContent = 'Upload failed';
            alert(error?.message || 'Failed to upload file');
        } finally {
            this.state.isUploadingFile = false;
            this._syncPrimaryControls();
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

    async _uploadImportedAudio(sessionId, file, extension) {
        const mimeType = this._uploadMimeType(file, extension);
        const response = await window.SidekickNetwork.request(`/api/recordings/${sessionId}/audio`, {
            method: 'PUT',
            headers: {
                'Content-Type': mimeType,
                'X-Upload-Extension': extension,
            },
            body: file,
        }, {
            timeoutMs: 600000,
            retries: 0,
            networkErrorMessage: 'Network request failed while uploading file',
            logLabel: 'upload:file',
        });

        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.detail || 'Failed to upload file');
        }
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
            console.warn('Failed to seed uploaded recording title:', error?.message || error);
        }
    }

    async _finalizeUploadSession(sessionId) {
        try {
            const completion = await this._completeRecording(sessionId, {
                mimeType: 'audio/webm',
                expectedChunks: 0,
                allowFallbackBlob: true,
            });
            if (!completion?.workspace_ready) {
                throw new Error('Uploaded audio is not ready yet');
            }
            this.state.sessionId = null;
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
            console.warn('Failed to finalize upload session cleanly:', {
                sessionId,
                message: error?.message || 'Failed to finalize upload session',
            });
            throw error;
        }
    }

    async _cleanupFailedUploadSession(sessionId) {
        try {
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

    _beginRecordingUi(sessionId) {
        this.state.isRecording = true;
        this.state.sessionId = sessionId;
        this.state.lastSessionId = sessionId;
        this.state.previewConnectionState = 'connecting';
        this._armRecordingHistoryGuard();
        this._syncPrimaryControls();
        this.elements.recordBtn.classList.add('recording');
        this.elements.recordBtn.textContent = 'Stop';
        this.elements.recordBtn.setAttribute('aria-pressed', 'true');
        this._resetLivePreview();
        this._syncLivePreviewVisibility();
        this._startTimer();
        this._flushPendingChunks();
        this._syncConnectionStatus();
    }

    _endRecordingUi() {
        this.state.isRecording = false;
        this.state.livePreviewEnabled = false;
        this.state.previewConnectionState = 'idle';
        this._disarmRecordingHistoryGuard();
        this._syncPrimaryControls();
        this.elements.recordBtn.classList.remove('recording');
        this.elements.recordBtn.textContent = 'Record';
        this.elements.recordBtn.setAttribute('aria-pressed', 'false');
        this._syncLivePreviewVisibility();
        this._stopTimer();
        this._syncConnectionStatus();
    }

    _resetRecordingBootstrapState() {
        this.state.isRecording = false;
        this.state.livePreviewEnabled = false;
        this.state.previewConnectionState = 'idle';
        this.state.sessionId = null;
        this.state.lastSessionId = null;
        this.audioUploadPromise = null;
        this.chunkUploads = new Map();
        this.chunkResults = new Map();
        this.pendingChunks = new Map();
        this.expectedChunkCount = 0;
        this.captureStopMeta = null;
        this.fallbackBlob = null;
        this.fallbackMimeType = null;
        this.previewAttachPromise = null;
        this._disarmRecordingHistoryGuard();
        this.elements.recordBtn.classList.remove('recording');
        this.elements.recordBtn.textContent = 'Record';
        this.elements.recordBtn.setAttribute('aria-pressed', 'false');
        this._stopTimer();
        this._syncLivePreviewVisibility();
        this._syncConnectionStatus();
    }

    async _connectPreviewSocket(sessionId) {
        if (!sessionId) {
            return null;
        }
        if (this.previewAttachPromise) {
            return this.previewAttachPromise;
        }

        this.state.previewConnectionState = 'connecting';
        this._syncConnectionStatus();

        this.previewAttachPromise = (async () => {
            try {
                await this.ws.waitForOpen(5000);
                const attached = await this.ws.attachSession(sessionId, 5000);
                this.state.livePreviewEnabled = !!attached?.live_preview_enabled;
                this.state.previewConnectionState = (
                    attached?.live_preview_enabled ? 'connected' : 'degraded'
                );
                this._syncLivePreviewVisibility();
                this._syncConnectionStatus();
                return attached;
            } catch (error) {
                this.state.livePreviewEnabled = false;
                this.state.previewConnectionState = 'degraded';
                this._syncLivePreviewVisibility();
                this._syncConnectionStatus();
                console.warn('Preview connection unavailable; recording will still save:', error?.message || error);
                try {
                    this.ws.disconnect();
                } catch (_disconnectError) {
                    // no-op: preview is best effort
                }
                return null;
            } finally {
                this.previewAttachPromise = null;
            }
        })();

        return await this.previewAttachPromise;
    }

    async _disconnectPreviewSocket(sessionId, options = {}) {
        this.state.livePreviewEnabled = false;
        this.state.previewConnectionState = 'idle';
        this.previewAttachPromise = null;
        this._syncLivePreviewVisibility();
        this._syncConnectionStatus();

        try {
            if (sessionId) {
                await this.ws.detachSession(sessionId, options, 3000);
            }
        } finally {
            this.ws.disconnect();
        }
    }

    _resetAfterWorkspace(options = {}) {
        const {
            preserveLastSessionId = false,
            statusText = '',
        } = options;
        this.state.isUploadingFile = false;
        this.state.isStartingRecording = false;
        this.state.isStoppingRecording = false;
        this.state.livePreviewEnabled = false;
        this.state.previewConnectionState = 'idle';
        this.state.elapsedSeconds = 0;
        this.state.recordingStartTime = null;
        this._updateTimerDisplay();
        this.state.sessionId = null;
        if (!preserveLastSessionId) {
            this.state.lastSessionId = null;
        }
        this.audioUploadPromise = null;
        this.chunkUploads = new Map();
        this.chunkResults = new Map();
        this.pendingChunks = new Map();
        this.expectedChunkCount = 0;
        this.captureStopMeta = null;
        this.fallbackBlob = null;
        this.fallbackMimeType = null;
        this.previewAttachPromise = null;
        this.elements.statusText.textContent = statusText;
        this._syncLivePreviewVisibility();
        this._syncConnectionStatus();
        this._syncPrimaryControls();
    }

    _resetLivePreview() {
        this.elements.livePreviewText.textContent = 'Listening...';
    }

    async _uploadChunkBestEffort(blob, mimeType, chunkIndex) {
        const sessionId = this.state.sessionId || this.state.lastSessionId;
        if (!sessionId) {
            this.pendingChunks.set(chunkIndex, { blob, mimeType, chunkIndex });
            console.info('[recording_stop:chunk:queued]', { chunkIndex });
            return;
        }

        this._scheduleChunkUpload(sessionId, blob, mimeType, chunkIndex);
    }

    _scheduleChunkUpload(sessionId, blob, mimeType, chunkIndex) {
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

    _flushPendingChunks() {
        const sessionId = this.state.sessionId || this.state.lastSessionId;
        if (!sessionId || this.pendingChunks.size === 0) {
            return;
        }

        const queuedChunks = Array.from(this.pendingChunks.values())
            .sort((left, right) => left.chunkIndex - right.chunkIndex);
        this.pendingChunks.clear();

        console.info('[recording_stop:chunk:flush]', {
            sessionId,
            queuedChunkCount: queuedChunks.length,
        });

        queuedChunks.forEach(({ blob, mimeType, chunkIndex }) => {
            if (this.chunkResults.get(chunkIndex)?.success) {
                return;
            }
            this._scheduleChunkUpload(sessionId, blob, mimeType, chunkIndex);
        });
    }

    async _doChunkUpload(sessionId, blob, mimeType, chunkIndex) {
        const maxAttempts = 3;
        for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
            try {
                const response = await window.SidekickNetwork.request(`/api/recordings/${sessionId}/audio/chunks/${chunkIndex}`, {
                    method: 'PUT',
                    headers: {
                        'Content-Type': mimeType || 'audio/webm',
                        'X-Client-ID': this.clientId,
                    },
                    body: blob,
                }, {
                    timeoutMs: 15000,
                    retries: 0,
                    networkErrorMessage: 'Network request failed while uploading recording audio chunk',
                    logLabel: 'recording_stop:upload_chunk',
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

    async _waitForRecordingCompletion(sessionId, options = {}) {
        const retryWindowMs = Number.isFinite(options.retryWindowMs) ? options.retryWindowMs : 30000;
        const retryIntervalMs = Number.isFinite(options.retryIntervalMs) ? options.retryIntervalMs : 500;
        const startedAt = Date.now();
        let completion = await this._completeRecording(sessionId, options);

        while (
            completion
            && !completion.workspace_ready
            && completion.recoverable_from_chunks
            && Date.now() - startedAt < retryWindowMs
        ) {
            this.elements.statusText.textContent = 'Waiting for last audio chunks...';
            await new Promise((resolve) => setTimeout(resolve, retryIntervalMs));
            completion = await this._completeRecording(sessionId, options);
        }

        return completion;
    }

    async _completeRecording(sessionId, options = {}) {
        return await window.SidekickNetwork.json(`/api/recordings/${sessionId}/complete`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                client_id: this.clientId,
                mime_type: options.mimeType || this.captureStopMeta?.mimeType || this.fallbackMimeType || 'audio/webm',
                expected_chunks: Number.isFinite(options.expectedChunks) ? options.expectedChunks : this.expectedChunkCount,
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

    async _uploadSessionAudio(sessionId, blob, mimeType) {
        const response = await window.SidekickNetwork.request(`/api/recordings/${sessionId}/audio`, {
            method: 'PUT',
            headers: {
                'Content-Type': mimeType || 'audio/webm',
            },
            body: blob,
        }, {
            timeoutMs: 900000,
            retries: 1,
            networkErrorMessage: 'Network request failed while uploading recording audio',
            logLabel: 'recording_stop:upload_audio',
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
