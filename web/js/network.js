/**
 * Shared network helpers for workspace and recording flows.
 */

(function () {
    const DEFAULT_RETRY_BACKOFF_MS = [300, 900];

    function sleep(ms) {
        return new Promise((resolve) => setTimeout(resolve, ms));
    }

    function resolveUrl(url) {
        const apiBase = typeof window !== 'undefined' && typeof window.__SIDEKICK_API_BASE === 'string'
            ? window.__SIDEKICK_API_BASE.trim()
            : '';
        if (!apiBase || typeof url !== 'string' || !url.startsWith('/api/')) {
            return url;
        }
        return `${apiBase}${url}`;
    }

    function normalizeErrorMessage(error, fallbackMessage = 'Network request failed') {
        if (error?.name === 'AbortError') {
            return 'Network request timed out';
        }

        const message = typeof error?.message === 'string' ? error.message.trim() : '';
        if (!message || message === 'Failed to fetch' || message === 'Load failed') {
            return fallbackMessage;
        }

        return message;
    }

    async function request(url, options = {}, config = {}) {
        const {
            timeoutMs = 10000,
            retries = 0,
            retryBackoffMs = DEFAULT_RETRY_BACKOFF_MS,
            retryOnNetworkError = true,
            networkErrorMessage = 'Network request failed',
            logLabel = 'network',
        } = config;

        const resolvedUrl = resolveUrl(url);
        const method = options.method || 'GET';
        let lastError = null;

        for (let attempt = 0; attempt <= retries; attempt += 1) {
            const controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
            const timeoutId = controller
                ? window.setTimeout(() => controller.abort(), timeoutMs)
                : null;

            try {
                const response = await fetch(resolvedUrl, {
                    credentials: 'same-origin',
                    cache: 'no-store',
                    ...options,
                    signal: controller ? controller.signal : options.signal,
                });
                if (timeoutId) {
                    clearTimeout(timeoutId);
                }
                return response;
            } catch (error) {
                if (timeoutId) {
                    clearTimeout(timeoutId);
                }

                lastError = new Error(normalizeErrorMessage(error, networkErrorMessage));
                console.warn(`[${logLabel}] network failure`, {
                    url,
                    resolvedUrl,
                    method,
                    attempt: attempt + 1,
                    retries,
                    message: lastError.message,
                });

                if (!retryOnNetworkError || attempt === retries) {
                    throw lastError;
                }

                const backoffIndex = Math.min(attempt, retryBackoffMs.length - 1);
                const delayMs = retryBackoffMs[backoffIndex] || retryBackoffMs[retryBackoffMs.length - 1] || 300;
                await sleep(delayMs);
            }
        }

        throw lastError || new Error(networkErrorMessage);
    }

    async function json(url, options = {}, config = {}) {
        const response = await request(url, options, config);
        const contentType = response.headers.get('content-type') || '';
        let payload = null;

        if (contentType.includes('application/json')) {
            payload = await response.json().catch(() => null);
        } else if (response.status !== 204) {
            payload = await response.text().catch(() => null);
        }

        if (!response.ok) {
            const detail = payload && typeof payload === 'object' ? payload.detail : null;
            throw new Error(detail || config.httpErrorMessage || `HTTP ${response.status}`);
        }

        return payload;
    }

    window.SidekickNetwork = {
        request,
        json,
        sleep,
        normalizeErrorMessage,
        resolveUrl,
    };
})();
