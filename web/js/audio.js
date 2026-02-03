/**
 * Browser audio capture using Web Audio API
 */

class AudioCapture {
    constructor(options = {}) {
        this.sampleRate = options.sampleRate || 16000;
        this.onAudioData = options.onAudioData || (() => {});
        this.onLevelUpdate = options.onLevelUpdate || (() => {});

        this.audioContext = null;
        this.mediaStream = null;
        this.workletNode = null;
        this.analyser = null;
        this.isCapturing = false;
    }

    async start() {
        if (this.isCapturing) return;

        try {
            // Get microphone access
            this.mediaStream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    channelCount: 1,
                    sampleRate: this.sampleRate,
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                },
            });

            // Create audio context
            this.audioContext = new AudioContext({
                sampleRate: this.sampleRate,
            });

            // Create source from microphone
            const source = this.audioContext.createMediaStreamSource(this.mediaStream);

            // Create analyser for visualization
            this.analyser = this.audioContext.createAnalyser();
            this.analyser.fftSize = 256;
            source.connect(this.analyser);

            // Try to use AudioWorklet, fall back to ScriptProcessor
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
        // Convert Float32 to Int16 PCM
        const pcm = new Int16Array(samples.length);
        for (let i = 0; i < samples.length; i++) {
            const s = Math.max(-1, Math.min(1, samples[i]));
            pcm[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
        }

        this.onAudioData(pcm.buffer);
    }

    _startLevelMonitoring() {
        if (!this.analyser) return;

        const dataArray = new Uint8Array(this.analyser.frequencyBinCount);

        const updateLevel = () => {
            if (!this.isCapturing) return;

            this.analyser.getByteFrequencyData(dataArray);

            // Calculate RMS level
            let sum = 0;
            for (let i = 0; i < dataArray.length; i++) {
                sum += dataArray[i] * dataArray[i];
            }
            const rms = Math.sqrt(sum / dataArray.length);
            const level = Math.min(1, rms / 128);

            this.onLevelUpdate(level, dataArray);
            requestAnimationFrame(updateLevel);
        };

        requestAnimationFrame(updateLevel);
    }

    stop() {
        this.isCapturing = false;

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
    }
}

// Audio visualizer
class AudioVisualizer {
    constructor(canvas) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.width = canvas.width;
        this.height = canvas.height;
    }

    draw(level, frequencyData) {
        const ctx = this.ctx;
        const width = this.width;
        const height = this.height;

        // Clear
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, width, height);

        if (!frequencyData) {
            // Draw idle state
            ctx.fillStyle = '#334155';
            ctx.fillRect(0, height / 2 - 2, width, 4);
            return;
        }

        // Draw frequency bars
        const barCount = Math.min(frequencyData.length, 32);
        const barWidth = width / barCount - 2;
        const barSpacing = 2;

        for (let i = 0; i < barCount; i++) {
            const value = frequencyData[i] / 255;
            const barHeight = value * height * 0.9;

            const x = i * (barWidth + barSpacing);
            const y = height - barHeight;

            // Gradient color based on level
            const hue = 200 + (value * 60); // Blue to cyan
            ctx.fillStyle = `hsl(${hue}, 70%, 50%)`;
            ctx.fillRect(x, y, barWidth, barHeight);
        }

        // Draw peak indicator
        const peakWidth = width * level;
        ctx.fillStyle = 'rgba(33, 150, 243, 0.3)';
        ctx.fillRect(0, 0, peakWidth, 4);
    }

    clear() {
        this.ctx.fillStyle = '#1a1a2e';
        this.ctx.fillRect(0, 0, this.width, this.height);
        this.ctx.fillStyle = '#334155';
        this.ctx.fillRect(0, this.height / 2 - 2, this.width, 4);
    }
}

// Export for use in app.js
window.AudioCapture = AudioCapture;
window.AudioVisualizer = AudioVisualizer;
