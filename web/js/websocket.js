/**
 * Simplified WebSocket client for audio streaming
 */

class SidekickWebSocket {
    constructor(options = {}) {
        this.url = options.url || this._getDefaultUrl();
        this.reconnectInterval = options.reconnectInterval || 3000;
        this.maxReconnectAttempts = options.maxReconnectAttempts || Infinity;
        this.pingInterval = options.pingInterval || 25000; // 25 seconds
        this.connectionAttemptStartedAt = 0;

        this.ws = null;
        this.reconnectAttempts = 0;
        this.isConnected = false;
        this.shouldReconnect = true;
        this.pingTimer = null;
        this.pendingMessages = [];
        this.openWaiters = new Set();
        this.messageWaiters = new Set();

        // Event handlers
        this.onOpen = options.onOpen || (() => {});
        this.onClose = options.onClose || (() => {});
        this.onError = options.onError || (() => {});
        this.onState = options.onState || (() => {});
        this.onTranscription = options.onTranscription || (() => {});
    }

    _getDefaultUrl() {
        if (typeof window !== 'undefined' && typeof window.__SIDEKICK_WS_URL === 'string' && window.__SIDEKICK_WS_URL) {
            return window.__SIDEKICK_WS_URL;
        }
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${protocol}//${window.location.host}/ws/audio`;
    }

    connect() {
        if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        this.shouldReconnect = true;
        this.connectionAttemptStartedAt = Date.now();

        try {
            this.ws = new WebSocket(this.url);
            this.ws.binaryType = 'arraybuffer';

            this.ws.onopen = () => {
                console.log('WebSocket connected');
                this.isConnected = true;
                this.reconnectAttempts = 0;
                this._startPing();
                this._flushPendingMessages();
                this._resolveOpenWaiters();
                this.onOpen();
            };

            this.ws.onclose = (event) => {
                console.log('WebSocket closed', event.code, event.reason);
                this.isConnected = false;
                this._stopPing();
                this._rejectOpenWaiters(new Error(`WebSocket closed (${event.code || 'unknown'})`));
                this._rejectMessageWaiters(new Error(`WebSocket closed (${event.code || 'unknown'})`));
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
                this._rejectOpenWaiters(new Error('WebSocket error'));
                this._rejectMessageWaiters(new Error('WebSocket error'));
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
        this._rejectOpenWaiters(new Error('WebSocket disconnected'));
        this._rejectMessageWaiters(new Error('WebSocket disconnected'));
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
            if (message.type === 'error') {
                const error = new Error(message.message || 'WebSocket command failed');
                this._rejectMessageWaiters(error);
                console.error('WebSocket command error:', message.message || 'Unknown error');
                return;
            }

            this._resolveMessageWaiters(message);

            switch (message.type) {
                case 'state':
                    this.onState(message);
                    break;
                case 'transcription':
                    this.onTranscription(message);
                    break;
                case 'session_attached':
                case 'session_detached':
                case 'pong':
                    break;
                default:
                    console.log('Message:', message.type);
            }

        } catch (error) {
            console.error('Failed to parse message:', error);
        }
    }

    _flushPendingMessages() {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN || this.pendingMessages.length === 0) {
            return;
        }

        for (const message of this.pendingMessages) {
            this.ws.send(message);
        }
        this.pendingMessages = [];
    }

    _resolveOpenWaiters() {
        for (const waiter of this.openWaiters) {
            clearTimeout(waiter.timeoutId);
            waiter.resolve();
        }
        this.openWaiters.clear();
    }

    _rejectOpenWaiters(error) {
        for (const waiter of this.openWaiters) {
            clearTimeout(waiter.timeoutId);
            waiter.reject(error);
        }
        this.openWaiters.clear();
    }

    _resolveMessageWaiters(message) {
        let matched = false;
        for (const waiter of Array.from(this.messageWaiters)) {
            if (!waiter.match(message)) {
                continue;
            }
            matched = true;
            clearTimeout(waiter.timeoutId);
            this.messageWaiters.delete(waiter);
            waiter.resolve(message);
        }
        return matched;
    }

    _rejectMessageWaiters(error) {
        for (const waiter of this.messageWaiters) {
            clearTimeout(waiter.timeoutId);
            waiter.reject(error);
        }
        this.messageWaiters.clear();
    }

    waitForOpen(timeoutMs = 10000) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            return Promise.resolve();
        }

        if (
            this.ws
            && this.ws.readyState === WebSocket.CONNECTING
            && this.connectionAttemptStartedAt
            && (Date.now() - this.connectionAttemptStartedAt) >= timeoutMs
        ) {
            try {
                this.ws.close();
            } catch (_error) {
                // ignore socket close failures while recovering a stale connect
            }
            this.ws = null;
        }

        this.connect();

        return new Promise((resolve, reject) => {
            const waiter = {
                resolve: () => resolve(),
                reject: (error) => reject(error),
                timeoutId: setTimeout(() => {
                    this.openWaiters.delete(waiter);
                    reject(new Error('WebSocket connection timeout'));
                }, timeoutMs),
            };

            this.openWaiters.add(waiter);
        });
    }

    sendAudio(audioBuffer) {
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(audioBuffer);
        }
    }

    sendCommand(command, data = {}) {
        const payload = JSON.stringify({ command, ...data });
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(payload);
            return;
        }

        if (!this.ws || this.ws.readyState === WebSocket.CLOSED || this.ws.readyState === WebSocket.CLOSING) {
            this.connect();
        }

        this.pendingMessages.push(payload);
    }

    _waitForMessage(match, timeoutMs = 5000) {
        return new Promise((resolve, reject) => {
            const waiter = {
                match,
                resolve,
                reject,
                timeoutId: setTimeout(() => {
                    this.messageWaiters.delete(waiter);
                    reject(new Error('WebSocket message timeout'));
                }, timeoutMs),
            };

            this.messageWaiters.add(waiter);
        });
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

    async attachSession(sessionId, timeoutMs = 5000) {
        await this.waitForOpen(timeoutMs);
        const waiter = this._waitForMessage(
            (message) => message.type === 'session_attached' && message.session_id === sessionId,
            timeoutMs,
        );
        this.sendCommand('attach_session', { session_id: sessionId });
        return await waiter;
    }

    async detachSession(sessionId, options = {}, timeoutMs = 5000) {
        if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
            return null;
        }

        const waiter = this._waitForMessage(
            (message) => message.type === 'session_detached' && (!sessionId || message.session_id === sessionId),
            timeoutMs,
        );
        this.sendCommand('detach_session', {
            session_id: sessionId,
            flush_buffer: !!options.flushBuffer,
        });
        return await waiter;
    }

    ping() {
        this.sendCommand('ping');
    }
}

window.SidekickWebSocket = SidekickWebSocket;
