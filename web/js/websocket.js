/**
 * WebSocket client for audio streaming and transcript updates
 */

class SidekickWebSocket {
    constructor(options = {}) {
        this.url = options.url || this._getDefaultUrl();
        this.reconnectInterval = options.reconnectInterval || 3000;
        this.maxReconnectAttempts = options.maxReconnectAttempts || 10;

        this.ws = null;
        this.reconnectAttempts = 0;
        this.isConnected = false;
        this.shouldReconnect = true;

        // Event handlers
        this.onOpen = options.onOpen || (() => {});
        this.onClose = options.onClose || (() => {});
        this.onError = options.onError || (() => {});
        this.onState = options.onState || (() => {});
        this.onTranscription = options.onTranscription || (() => {});
        this.onImportantMarked = options.onImportantMarked || (() => {});
        this.onErrorMessage = options.onErrorMessage || (() => {});
    }

    _getDefaultUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${protocol}//${window.location.host}/ws/audio`;
    }

    connect() {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            return;
        }

        this.shouldReconnect = true;

        try {
            this.ws = new WebSocket(this.url);
            this.ws.binaryType = 'arraybuffer';

            this.ws.onopen = () => {
                console.log('WebSocket connected');
                this.isConnected = true;
                this.reconnectAttempts = 0;
                this.onOpen();
            };

            this.ws.onclose = (event) => {
                console.log('WebSocket closed', event.code, event.reason);
                this.isConnected = false;
                this.onClose(event);

                if (this.shouldReconnect && this.reconnectAttempts < this.maxReconnectAttempts) {
                    this.reconnectAttempts++;
                    console.log(`Reconnecting... attempt ${this.reconnectAttempts}`);
                    setTimeout(() => this.connect(), this.reconnectInterval);
                }
            };

            this.ws.onerror = (error) => {
                console.error('WebSocket error:', error);
                this.onError(error);
            };

            this.ws.onmessage = (event) => {
                this._handleMessage(event);
            };

        } catch (error) {
            console.error('Failed to create WebSocket:', error);
            this.onError(error);
        }
    }

    disconnect() {
        this.shouldReconnect = false;
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    _handleMessage(event) {
        // Binary data is unexpected in this direction
        if (event.data instanceof ArrayBuffer) {
            return;
        }

        try {
            const message = JSON.parse(event.data);

            switch (message.type) {
                case 'state':
                    this.onState(message);
                    break;

                case 'transcription':
                    this.onTranscription(message);
                    break;

                case 'important_marked':
                    this.onImportantMarked(message);
                    break;

                case 'error':
                    console.error('Server error:', message.message);
                    this.onErrorMessage(message);
                    break;

                case 'pong':
                    // Heartbeat response
                    break;

                default:
                    console.log('Unknown message type:', message.type);
            }

        } catch (error) {
            console.error('Failed to parse message:', error);
        }
    }

    // Send audio data
    sendAudio(audioBuffer) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(audioBuffer);
        }
    }

    // Send commands
    sendCommand(command, data = {}) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ command, ...data }));
        }
    }

    // Session commands
    startSession(mode = 'work', submode = null) {
        this.sendCommand('start_session', { mode, submode });
    }

    endSession() {
        this.sendCommand('end_session');
    }

    // Meeting commands
    startMeeting(title = null) {
        this.sendCommand('start_meeting', { title });
    }

    endMeeting() {
        this.sendCommand('end_meeting');
    }

    // Mark important
    markImportant(note = null) {
        this.sendCommand('mark_important', { note });
    }

    // Change mode
    changeMode(mode, submode = null) {
        this.sendCommand('change_mode', { mode, submode });
    }

    // Heartbeat
    ping() {
        this.sendCommand('ping');
    }
}

// Export
window.SidekickWebSocket = SidekickWebSocket;
