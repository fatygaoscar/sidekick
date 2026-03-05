/**
 * Simplified WebSocket client for audio streaming
 */

class SidekickWebSocket {
    constructor(options = {}) {
        this.url = options.url || this._getDefaultUrl();
        this.reconnectInterval = options.reconnectInterval || 3000;
        this.maxReconnectAttempts = options.maxReconnectAttempts || Infinity;
        this.pingInterval = options.pingInterval || 25000; // 25 seconds

        this.ws = null;
        this.reconnectAttempts = 0;
        this.isConnected = false;
        this.shouldReconnect = true;
        this.pingTimer = null;

        // Event handlers
        this.onOpen = options.onOpen || (() => {});
        this.onClose = options.onClose || (() => {});
        this.onError = options.onError || (() => {});
        this.onState = options.onState || (() => {});
        this.onTranscription = options.onTranscription || (() => {});
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
                this._startPing();
                this.onOpen();
            };

            this.ws.onclose = (event) => {
                console.log('WebSocket closed', event.code, event.reason);
                this.isConnected = false;
                this._stopPing();
                this.onClose(event);

                if (this.shouldReconnect && this.reconnectAttempts < this.maxReconnectAttempts) {
                    this.reconnectAttempts++;
                    const delay = Math.min(this.reconnectInterval * Math.pow(1.5, this.reconnectAttempts - 1), 30000);
                    console.log(`Reconnecting... attempt ${this.reconnectAttempts} in ${Math.round(delay)}ms`);
                    setTimeout(() => this.connect(), delay);
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
        this._stopPing();
        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    _startPing() {
        this._stopPing();
        this.pingTimer = setInterval(() => {
            if (this.isConnected) {
                this.ping();
            }
        }, this.pingInterval);
    }

    _stopPing() {
        if (this.pingTimer) {
            clearInterval(this.pingTimer);
            this.pingTimer = null;
        }
    }

    _handleMessage(event) {
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
                case 'pong':
                    break;
                default:
                    console.log('Message:', message.type);
            }

        } catch (error) {
            console.error('Failed to parse message:', error);
        }
    }

    sendAudio(audioBuffer) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(audioBuffer);
        }
    }

    sendCommand(command, data = {}) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({ command, ...data }));
        }
    }

    startSession() {
        let timezoneName = null;
        try {
            timezoneName = Intl.DateTimeFormat().resolvedOptions().timeZone || null;
        } catch (_error) {
            timezoneName = null;
        }

        this.sendCommand('start_session', {
            mode: 'work',
            timezone_name: timezoneName,
            timezone_offset_minutes: new Date().getTimezoneOffset(),
        });
    }

    endSession() {
        this.sendCommand('end_session');
    }

    ping() {
        this.sendCommand('ping');
    }
}

window.SidekickWebSocket = SidekickWebSocket;
