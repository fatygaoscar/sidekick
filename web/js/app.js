/**
 * Main application logic for Sidekick
 */

class SidekickApp {
    constructor() {
        // State
        this.session = null;
        this.meeting = null;
        this.segments = [];
        this.elapsedSeconds = 0;
        this.timerInterval = null;

        // Components
        this.audioCapture = null;
        this.visualizer = null;
        this.ws = null;

        // DOM Elements
        this.elements = {
            // Connection
            connectionStatus: document.getElementById('connection-status'),

            // Mode
            modeSelect: document.getElementById('mode-select'),
            submodeSelect: document.getElementById('submode-select'),

            // Recording
            recordingStatus: document.getElementById('recording-status'),
            elapsedTime: document.getElementById('elapsed-time'),
            sessionBtn: document.getElementById('session-btn'),

            // Meeting
            meetingControls: document.getElementById('meeting-controls'),
            meetingStatus: document.getElementById('meeting-status'),
            keyStartBtn: document.getElementById('key-start-btn'),
            keyStopBtn: document.getElementById('key-stop-btn'),
            importantBtn: document.getElementById('important-btn'),

            // Audio
            audioCanvas: document.getElementById('audio-canvas'),

            // Transcript
            transcriptContent: document.getElementById('transcript-content'),
            clearBtn: document.getElementById('clear-btn'),
            exportBtn: document.getElementById('export-btn'),
            segmentCount: document.getElementById('segment-count'),

            // Summary
            summarizeBtn: document.getElementById('summarize-btn'),
            summaryType: document.getElementById('summary-type'),
            summaryModal: document.getElementById('summary-modal'),
            summaryContent: document.getElementById('summary-content'),
            closeSummary: document.getElementById('close-summary'),
            copySummary: document.getElementById('copy-summary'),
            downloadSummary: document.getElementById('download-summary'),
        };

        this._init();
    }

    _init() {
        // Initialize visualizer
        this.visualizer = new AudioVisualizer(this.elements.audioCanvas);
        this.visualizer.clear();

        // Initialize WebSocket
        this._initWebSocket();

        // Initialize audio capture
        this._initAudioCapture();

        // Bind event handlers
        this._bindEvents();

        // Load modes
        this._loadModes();
    }

    _initWebSocket() {
        this.ws = new SidekickWebSocket({
            onOpen: () => this._onConnected(),
            onClose: () => this._onDisconnected(),
            onError: (err) => this._onError(err),
            onState: (state) => this._onState(state),
            onTranscription: (data) => this._onTranscription(data),
            onImportantMarked: (data) => this._onImportantMarked(data),
            onErrorMessage: (data) => this._onErrorMessage(data),
        });

        this.ws.connect();
    }

    _initAudioCapture() {
        this.audioCapture = new AudioCapture({
            sampleRate: 16000,
            onAudioData: (buffer) => {
                if (this.session) {
                    this.ws.sendAudio(buffer);
                }
            },
            onLevelUpdate: (level, frequencyData) => {
                this.visualizer.draw(level, frequencyData);
            },
        });
    }

    _bindEvents() {
        // Session control
        this.elements.sessionBtn.addEventListener('click', () => this._toggleSession());

        // Meeting controls
        this.elements.keyStartBtn.addEventListener('click', () => this._startMeeting());
        this.elements.keyStopBtn.addEventListener('click', () => this._endMeeting());
        this.elements.importantBtn.addEventListener('click', () => this._markImportant());

        // Mode selection
        this.elements.modeSelect.addEventListener('change', (e) => {
            this._changeMode(e.target.value);
        });

        // Transcript controls
        this.elements.clearBtn.addEventListener('click', () => this._clearTranscript());
        this.elements.exportBtn.addEventListener('click', () => this._exportTranscript());

        // Summary
        this.elements.summarizeBtn.addEventListener('click', () => this._generateSummary());
        this.elements.closeSummary.addEventListener('click', () => this._closeSummaryModal());
        this.elements.copySummary.addEventListener('click', () => this._copySummary());
        this.elements.downloadSummary.addEventListener('click', () => this._downloadSummary());

        // Close modal on outside click
        this.elements.summaryModal.addEventListener('click', (e) => {
            if (e.target === this.elements.summaryModal) {
                this._closeSummaryModal();
            }
        });
    }

    async _loadModes() {
        try {
            const response = await fetch('/api/modes');
            const data = await response.json();

            // Populate mode select
            this.elements.modeSelect.innerHTML = '';
            for (const [key, mode] of Object.entries(data.modes)) {
                const option = document.createElement('option');
                option.value = key;
                option.textContent = mode.name;
                this.elements.modeSelect.appendChild(option);
            }

            // Set default
            this.elements.modeSelect.value = data.default_mode;
        } catch (error) {
            console.error('Failed to load modes:', error);
        }
    }

    // Connection handlers
    _onConnected() {
        this.elements.connectionStatus.textContent = 'Connected';
        this.elements.connectionStatus.classList.remove('disconnected');
        this.elements.connectionStatus.classList.add('connected');
    }

    _onDisconnected() {
        this.elements.connectionStatus.textContent = 'Disconnected';
        this.elements.connectionStatus.classList.remove('connected');
        this.elements.connectionStatus.classList.add('disconnected');
    }

    _onError(error) {
        console.error('WebSocket error:', error);
    }

    _onState(state) {
        // Update session state
        this.session = state.session;
        this.meeting = state.meeting;

        // Update UI
        this._updateUI();
    }

    _onTranscription(data) {
        this._addSegment({
            text: data.text,
            startTime: data.start_time,
            endTime: data.end_time,
            isImportant: data.is_important,
        });
    }

    _onImportantMarked(data) {
        // Flash the important button
        this.elements.importantBtn.classList.add('flash');
        setTimeout(() => {
            this.elements.importantBtn.classList.remove('flash');
        }, 500);
    }

    _onErrorMessage(data) {
        console.error('Server error:', data.message);
        // Could show a toast notification here
    }

    // Session management
    async _toggleSession() {
        if (this.session) {
            await this._endSession();
        } else {
            await this._startSession();
        }
    }

    async _startSession() {
        try {
            // Start audio capture
            await this.audioCapture.start();

            // Start session via WebSocket
            const mode = this.elements.modeSelect.value;
            this.ws.startSession(mode);

            // Start timer
            this._startTimer();

        } catch (error) {
            console.error('Failed to start session:', error);
            alert('Failed to access microphone. Please grant permission and try again.');
        }
    }

    async _endSession() {
        // Stop audio capture
        this.audioCapture.stop();
        this.visualizer.clear();

        // End session via WebSocket
        this.ws.endSession();

        // Stop timer
        this._stopTimer();
    }

    // Meeting management
    _startMeeting() {
        this.ws.startMeeting();
    }

    _endMeeting() {
        this.ws.endMeeting();
    }

    _markImportant() {
        this.ws.markImportant();
    }

    _changeMode(mode) {
        if (this.session) {
            this.ws.changeMode(mode);
        }
    }

    // Timer
    _startTimer() {
        this.elapsedSeconds = 0;
        this._updateTimerDisplay();

        this.timerInterval = setInterval(() => {
            this.elapsedSeconds++;
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
        const hours = Math.floor(this.elapsedSeconds / 3600);
        const minutes = Math.floor((this.elapsedSeconds % 3600) / 60);
        const seconds = this.elapsedSeconds % 60;

        this.elements.elapsedTime.textContent =
            `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
    }

    // Transcript management
    _addSegment(segment) {
        this.segments.push(segment);
        this._renderSegment(segment);
        this._updateSegmentCount();
        this._scrollToBottom();
    }

    _renderSegment(segment) {
        // Remove placeholder if present
        const placeholder = this.elements.transcriptContent.querySelector('.placeholder');
        if (placeholder) {
            placeholder.remove();
        }

        const div = document.createElement('div');
        div.className = 'transcript-segment' + (segment.isImportant ? ' important' : '');

        const timestamp = document.createElement('div');
        timestamp.className = 'timestamp';
        timestamp.textContent = this._formatTime(segment.startTime);

        const text = document.createElement('div');
        text.className = 'text';
        text.textContent = segment.text;

        div.appendChild(timestamp);
        div.appendChild(text);
        this.elements.transcriptContent.appendChild(div);
    }

    _formatTime(seconds) {
        const mins = Math.floor(seconds / 60);
        const secs = Math.floor(seconds % 60);
        return `${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    }

    _scrollToBottom() {
        this.elements.transcriptContent.scrollTop = this.elements.transcriptContent.scrollHeight;
    }

    _updateSegmentCount() {
        this.elements.segmentCount.textContent = `${this.segments.length} segments`;
    }

    _clearTranscript() {
        this.segments = [];
        this.elements.transcriptContent.innerHTML = '<p class="placeholder">Start a session to begin transcription...</p>';
        this._updateSegmentCount();
    }

    _exportTranscript() {
        const text = this.segments.map(s => {
            const time = this._formatTime(s.startTime);
            const marker = s.isImportant ? ' [IMPORTANT]' : '';
            return `[${time}]${marker} ${s.text}`;
        }).join('\n\n');

        const blob = new Blob([text], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `transcript_${new Date().toISOString().slice(0, 10)}.txt`;
        a.click();
        URL.revokeObjectURL(url);
    }

    // Summary
    async _generateSummary() {
        if (!this.meeting) {
            alert('Please start a meeting first to generate a summary.');
            return;
        }

        const summaryType = this.elements.summaryType.value;

        try {
            this.elements.summarizeBtn.disabled = true;
            this.elements.summarizeBtn.textContent = 'Generating...';

            const response = await fetch(`/api/meetings/${this.meeting.id}/summarize`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt_type: summaryType }),
            });

            if (!response.ok) {
                throw new Error('Failed to generate summary');
            }

            const data = await response.json();
            this._showSummary(data.content);

        } catch (error) {
            console.error('Failed to generate summary:', error);
            alert('Failed to generate summary. Please try again.');
        } finally {
            this.elements.summarizeBtn.disabled = false;
            this.elements.summarizeBtn.textContent = 'Generate Summary';
        }
    }

    _showSummary(content) {
        this.elements.summaryContent.innerHTML = `<pre>${content}</pre>`;
        this.elements.summaryModal.classList.remove('hidden');
    }

    _closeSummaryModal() {
        this.elements.summaryModal.classList.add('hidden');
    }

    _copySummary() {
        const content = this.elements.summaryContent.textContent;
        navigator.clipboard.writeText(content).then(() => {
            this.elements.copySummary.textContent = 'Copied!';
            setTimeout(() => {
                this.elements.copySummary.textContent = 'Copy to Clipboard';
            }, 2000);
        });
    }

    _downloadSummary() {
        const content = this.elements.summaryContent.textContent;
        const blob = new Blob([content], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `summary_${new Date().toISOString().slice(0, 10)}.txt`;
        a.click();
        URL.revokeObjectURL(url);
    }

    // UI updates
    _updateUI() {
        // Session state
        if (this.session) {
            this.elements.recordingStatus.classList.add('active');
            this.elements.recordingStatus.querySelector('.status-text').textContent = 'Recording';
            this.elements.sessionBtn.textContent = 'End Session';
            this.elements.sessionBtn.classList.remove('btn-primary');
            this.elements.sessionBtn.classList.add('btn-danger');

            // Enable meeting controls
            this.elements.keyStartBtn.disabled = false;
            this.elements.importantBtn.disabled = !this.meeting;
            this.elements.summarizeBtn.disabled = !this.meeting;
        } else {
            this.elements.recordingStatus.classList.remove('active');
            this.elements.recordingStatus.querySelector('.status-text').textContent = 'Not Recording';
            this.elements.sessionBtn.textContent = 'Start Session';
            this.elements.sessionBtn.classList.add('btn-primary');
            this.elements.sessionBtn.classList.remove('btn-danger');

            // Disable meeting controls
            this.elements.keyStartBtn.disabled = true;
            this.elements.keyStopBtn.disabled = true;
            this.elements.importantBtn.disabled = true;
            this.elements.summarizeBtn.disabled = true;
        }

        // Meeting state
        if (this.meeting) {
            this.elements.meetingStatus.classList.add('active');
            this.elements.meetingStatus.querySelector('span:last-child').textContent =
                this.meeting.title || 'Meeting in progress';
            this.elements.keyStartBtn.disabled = true;
            this.elements.keyStopBtn.disabled = false;
            this.elements.importantBtn.disabled = false;
        } else if (this.session) {
            this.elements.meetingStatus.classList.remove('active');
            this.elements.meetingStatus.querySelector('span:last-child').textContent = 'No active meeting';
            this.elements.keyStartBtn.disabled = false;
            this.elements.keyStopBtn.disabled = true;
        }

        // Mode
        if (this.session) {
            this.elements.modeSelect.value = this.session.mode;
        }
    }
}

// Initialize app when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
    window.app = new SidekickApp();
});
