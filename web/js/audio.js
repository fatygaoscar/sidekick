/**
 * Browser audio capture using Web Audio API
 */

class AudioCapture {
    constructor(options = {}) {
        this.sampleRate = options.sampleRate || 16000; // Target rate for streaming
        this.captureSampleRate = options.captureSampleRate || 48000; // Rate for recording/playback
        this.onAudioData = options.onAudioData || (() => {});
        this.onLevelUpdate = options.onLevelUpdate || (() => {});
        this.onEncodedAudio = options.onEncodedAudio || (() => {});
        this.onEncodedChunk = options.onEncodedChunk || (() => {});
        this.onCaptureStopped = options.onCaptureStopped || (() => {});

        this.audioContext = null;
        this.mediaStream = null;
        this.workletNode = null;
        this.analyser = null;
        this.mediaRecorder = null;
        this.recordedChunks = [];
        this.recordedMimeType = null;
        this.chunkIndex = 0;
        this.isCapturing = false;
        this._pendingStopCleanup = null;

        // Resampling state
        this.resampleBuffer = [];
    }

    async start() {
        if (this.isCapturing) return;

        try {
            // Get microphone access - request high quality
            this.mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    sampleRate: this.captureSampleRate,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                },
            });

            // Create audio context at hardware rate or requested capture rate
            this.audioContext = new AudioContext({
                sampleRate: this.captureSampleRate,
            });
            this.actualCaptureRate = this.audioContext.sampleRate;
            
            console.log(`[AudioCapture] Capturing at ${this.actualCaptureRate}Hz, target streaming at ${this.sampleRate}Hz`);

            // Create source from microphone
            const source = this.audioContext.createMediaStreamSource(this.mediaStream);

            this._startMediaRecorder();

            // Create analyser for visualization
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 4096;
            this.analyser.minDecibels = -96;
            this.analyser.maxDecibels = -18;
            this.analyser.smoothingTimeConstant = 0.6;
            source.connect(this.analyser);

            // Setup audio processing for streaming
            try {
                await this._setupWorklet(source);
            } catch (e) {
                console.warn('AudioWorklet not supported, using ScriptProcessor', e);
                this._setupScriptProcessor(source);
            }

            this.isCapturing = true;
            this._startLevelMonitoring();

        } catch (error) {
            console.error('Failed to start audio capture:', error);
            throw error;
        }
    }

    async _setupWorklet(source) {
        // Register worklet processor
        const workletCode = `
            class AudioProcessor extends AudioWorkletProcessor {
                constructor() {
                    super();
                    this.buffer = [];
                    this.bufferSize = 4096;
                }

                process(inputs, outputs, parameters) {
                    const input = inputs[0];
                    if (input.length > 0) {
                        const samples = input[0];
                        this.buffer.push(...samples);

                        while (this.buffer.length >= this.bufferSize) {
                            const chunk = this.buffer.splice(0, this.bufferSize);
                            this.port.postMessage({
                                type: 'audio',
                                samples: new Float32Array(chunk),
                            });
                        }
                    }
                    return true;
                }
            }
            registerProcessor('audio-processor', AudioProcessor);
        `;

        const blob = new Blob([workletCode], { type: 'application/javascript' });
        const url = URL.createObjectURL(blob);

        await this.audioContext.audioWorklet.addModule(url);
        URL.revokeObjectURL(url);

        this.workletNode = new AudioWorkletNode(this.audioContext, 'audio-processor');
        this.workletNode.port.onmessage = (event) => {
            if (event.data.type === 'audio') {
                this._processAudio(event.data.samples);
            }
        };

        source.connect(this.workletNode);
    }

    _setupScriptProcessor(source) {
        // Fallback for browsers without AudioWorklet support
        const bufferSize = 4096;
        const scriptNode = this.audioContext.createScriptProcessor(bufferSize, 1, 1);

        scriptNode.onaudioprocess = (event) => {
            const samples = event.inputBuffer.getChannelData(0);
            this._processAudio(new Float32Array(samples));
        };

        source.connect(scriptNode);
        scriptNode.connect(this.audioContext.destination);
        this.workletNode = scriptNode;
    }

    _processAudio(samples) {
        // Downsample from actualCaptureRate to this.sampleRate (16000)
        const ratio = this.actualCaptureRate / this.sampleRate;
        
        // Simple linear interpolation / decimation for efficiency
        const targetLength = Math.round(samples.length / ratio);
        const downsampled = new Float32Array(targetLength);
        
        for (let i = 0; i < targetLength; i++) {
            const pos = i * ratio;
            const index = Math.floor(pos);
            const fraction = pos - index;
            
            if (index + 1 < samples.length) {
                // Linear interpolation
                downsampled[i] = samples[index] * (1 - fraction) + samples[index + 1] * fraction;
            } else {
                downsampled[i] = samples[index];
            }
        }

        // Convert Float32 to Int16 PCM
        const pcm = new Int16Array(downsampled.length);
        for (let i = 0; i < downsampled.length; i++) {
            const s = Math.max(-1, Math.min(1, downsampled[i]));
            pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }

        this.onAudioData(pcm.buffer);
    }

    _startLevelMonitoring() {
        if (!this.analyser) return;

        const frequencyData = new Float32Array(this.analyser.frequencyBinCount);
        const timeDomainData = new Uint8Array(this.analyser.fftSize);

        const updateLevel = () => {
            if (!this.isCapturing) return;

            this.analyser.getFloatFrequencyData(frequencyData);
            this.analyser.getByteTimeDomainData(timeDomainData);

            // Calculate RMS level
            let sum = 0;
            for (let i = 0; i < timeDomainData.length; i++) {
                const centered = (timeDomainData[i] - 128) / 128;
                sum += centered * centered;
            }
            const rms = Math.sqrt(sum / timeDomainData.length);
            const level = Math.min(1, rms * 1.6);

            this.onLevelUpdate(level, frequencyData, this.audioContext?.sampleRate || this.captureSampleRate);
            requestAnimationFrame(updateLevel);
        };

        requestAnimationFrame(updateLevel);
    }

    _startMediaRecorder() {
        if (typeof MediaRecorder === 'undefined' || !this.mediaStream) {
            return;
        }

        const mimeCandidates = [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/mp4',
        ];
        let mimeType = '';
        for (const candidate of mimeCandidates) {
            if (MediaRecorder.isTypeSupported(candidate)) {
                mimeType = candidate;
                break;
            }
        }

        this.recordedChunks = [];
        this.recordedMimeType = mimeType || 'audio/webm';
        this.chunkIndex = 0;

        this.mediaRecorder = mimeType
            ? new MediaRecorder(this.mediaStream, { mimeType })
            : new MediaRecorder(this.mediaStream);

        this.mediaRecorder.ondataavailable = (event) => {
            if (event.data && event.data.size > 0) {
                this.recordedChunks.push(event.data);
                this.onEncodedChunk(event.data, this.recordedMimeType, this.chunkIndex);
                this.chunkIndex += 1;
            }
        };

        this.mediaRecorder.onstop = () => {
            if (this.recordedChunks.length) {
                const blob = new Blob(this.recordedChunks, { type: this.recordedMimeType });
                this.onEncodedAudio(blob, this.recordedMimeType);
            }
            this.onCaptureStopped({
                chunkCount: this.chunkIndex,
                mimeType: this.recordedMimeType,
            });
            this.recordedChunks = [];
            this.chunkIndex = 0;
            this._runPendingStopCleanup();
        };

        this.mediaRecorder.start(1000);
    }

    stop() {
        this.isCapturing = false;
        let awaitingMediaRecorderStop = false;

        const cleanup = () => {
            if (this.workletNode) {
                this.workletNode.disconnect();
                this.workletNode = null;
            }

            if (this.analyser) {
                this.analyser.disconnect();
                this.analyser = null;
            }

            if (this.audioContext) {
                this.audioContext.close();
                this.audioContext = null;
            }

            if (this.mediaStream) {
                this.mediaStream.getTracks().forEach(track => track.stop());
                this.mediaStream = null;
            }
        };

        if (this.mediaRecorder) {
            if (this.mediaRecorder.state !== 'inactive') {
                awaitingMediaRecorderStop = true;
                this._pendingStopCleanup = cleanup;
                try {
                    this.mediaRecorder.requestData();
                } catch (_error) {
                    // Safari may throw if no data is ready yet.
                }
                this.mediaRecorder.stop();
                window.setTimeout(() => {
                    this._runPendingStopCleanup();
                }, 1500);
            }
            this.mediaRecorder = null;
        }

        if (!awaitingMediaRecorderStop) {
            cleanup();
            this.onCaptureStopped({
                chunkCount: this.chunkIndex,
                mimeType: this.recordedMimeType || 'audio/webm',
            });
            this.chunkIndex = 0;
        }
    }

    _runPendingStopCleanup() {
        if (!this._pendingStopCleanup) {
            return;
        }
        const cleanup = this._pendingStopCleanup;
        this._pendingStopCleanup = null;
        cleanup();
    }
}

// Audio visualizer - Berkeley Mono aesthetic
class AudioVisualizer {
    constructor(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.sampleRate = 48000;
        this.dpr = Math.max(1, window.devicePixelRatio || 1);
        this.width = 0;
        this.height = 0;
        this.bandLevels = new Float32Array(40);

        this._resizeCanvas = this._resizeCanvas.bind(this);
        this._resizeCanvas();
        window.addEventListener('resize', this._resizeCanvas, { passive: true });
    }

    _resizeCanvas() {
        const rect = this.canvas.getBoundingClientRect();
        const width = Math.max(1, Math.round(rect.width || this.canvas.width || 400));
        const height = Math.max(1, Math.round(rect.height || this.canvas.height || 60));
        const dpr = Math.max(1, window.devicePixelRatio || 1);

        if (
            width === this.width &&
            height === this.height &&
            dpr === this.dpr
        ) {
            return;
        }

        this.width = width;
        this.height = height;
        this.dpr = dpr;
        this.canvas.width = Math.round(width * dpr);
        this.canvas.height = Math.round(height * dpr);
        this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        this.ctx.imageSmoothingEnabled = false;
    }

    draw(level, frequencyData, sampleRate = this.sampleRate) {
        this._resizeCanvas();

        const ctx = this.ctx;
        const width = this.width;
        const height = this.height;
        this.sampleRate = sampleRate || this.sampleRate;

        // Clear with dark background
        ctx.fillStyle = '#0a0a0a';
        ctx.fillRect(0, 0, width, height);

        if (!frequencyData) {
            this._drawIdle();
            return;
        }

        // Draw frequency bars - full-range, log-spaced analyzer
        const barCount = 40;
        const barWidth = 4;
        const gap = Math.max(1, Math.floor((width - (barCount * barWidth)) / (barCount - 1)));
        const totalWidth = (barCount * barWidth) + ((barCount - 1) * gap);
        const startX = Math.floor((width - totalWidth) / 2);
        const nyquist = this.sampleRate / 2;
        const minFrequency = 20;
        const maxFrequency = Math.min(20000, nyquist);
        const minLog = Math.log10(minFrequency);
        const maxLog = Math.log10(maxFrequency);
        const minDb = -96;
        const maxDb = -18;

        for (let i = 0; i < barCount; i++) {
            const startRatio = i / barCount;
            const endRatio = (i + 1) / barCount;
            const startFrequency = 10 ** (minLog + ((maxLog - minLog) * startRatio));
            const endFrequency = 10 ** (minLog + ((maxLog - minLog) * endRatio));
            const startIndex = Math.max(0, Math.floor((startFrequency / nyquist) * frequencyData.length));
            const endIndex = Math.min(
                frequencyData.length - 1,
                Math.max(startIndex, Math.ceil((endFrequency / nyquist) * frequencyData.length))
            );

            let powerSum = 0;
            let sampleCount = 0;
            for (let dataIndex = startIndex; dataIndex <= endIndex; dataIndex++) {
                const db = frequencyData[dataIndex];
                if (!Number.isFinite(db)) {
                    continue;
                }
                powerSum += 10 ** (db / 10);
                sampleCount += 1;
            }

            let averageDb = minDb;
            if (sampleCount > 0 && powerSum > 0) {
                averageDb = 10 * Math.log10(powerSum / sampleCount);
            }

            const normalized = Math.max(0, Math.min(1, (averageDb - minDb) / (maxDb - minDb)));
            const previous = this.bandLevels[i] || 0;
            const smoothed = normalized >= previous
                ? (previous * 0.45) + (normalized * 0.55)
                : (previous * 0.82) + (normalized * 0.18);

            this.bandLevels[i] = smoothed;

            const barHeight = Math.max(2, Math.round(smoothed * height * 0.85));

            const x = startX + (i * (barWidth + gap));
            const y = Math.floor((height - barHeight) / 2);

            // Flat light-gray bars for a simpler display
            ctx.fillStyle = '#cfcfcf';
            ctx.fillRect(x, y, barWidth, barHeight);
        }
    }

    _drawIdle() {
        const ctx = this.ctx;
        const width = this.width;
        const height = this.height;

        // Draw subtle center line
        ctx.fillStyle = '#2a2a2a';
        ctx.fillRect(0, Math.floor(height / 2) - 1, width, 2);
    }

    clear() {
        this.bandLevels.fill(0);
        this.ctx.fillStyle = '#0a0a0a';
        this.ctx.fillRect(0, 0, this.width, this.height);
        this._drawIdle();
    }
}

// Export for use in app.js
window.AudioCapture = AudioCapture;
window.AudioVisualizer = AudioVisualizer;
