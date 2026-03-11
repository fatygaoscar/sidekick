(function () {
    class SettingsPage {
        constructor() {
            this.state = {
                loading: true,
                settings: {},
                summarization: null,
                diagnostics: null,
                savingKeys: new Set(),
                switchingProvider: false,
                diagnosticsLoading: false,
                banner: null,
            };
            this._bannerTimer = null;
            this.features = [
                {
                    key: 'workspace_chat_enabled',
                    label: 'Meeting Assistant',
                    badge: 'Experimental',
                    description: 'Transcript-grounded chat and apply-to-draft workflow inside the recording workspace.',
                },
            ];

            this.elements = {
                banner: document.getElementById('settings-banner'),
                loading: document.getElementById('settings-loading'),
                list: document.getElementById('settings-list'),
                providerLoading: document.getElementById('settings-provider-loading'),
                providerList: document.getElementById('settings-provider-list'),
                diagnostics: document.getElementById('settings-diagnostics'),
                runDiagnostics: document.getElementById('settings-run-diagnostics'),
            };

            this._bindEvents();
            void this._load();
        }

        _bindEvents() {
            this.elements.list?.addEventListener('change', (event) => {
                const input = event.target.closest('[data-setting-key]');
                if (!input) {
                    return;
                }
                void this._toggleSetting(input.dataset.settingKey, input.checked);
            });
            this.elements.providerList?.addEventListener('change', (event) => {
                const input = event.target.closest('[name="summarization-backend"]');
                if (!input) {
                    return;
                }
                void this._switchProvider(input.value);
            });
            this.elements.runDiagnostics?.addEventListener('click', () => {
                void this._runDiagnostics();
            });
        }

        async _load() {
            this.state.loading = true;
            this._render();
            try {
                const payload = await window.SidekickNetwork.json('/api/settings', {}, {
                    timeoutMs: 8000,
                    retries: 1,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to load settings',
                    logLabel: 'settings:load',
                });
                this._applyPayload(payload);
                await this._runDiagnostics({ silentFailure: true });
            } catch (error) {
                this._showBanner(error?.message || 'Failed to load settings.', 'error');
            } finally {
                this.state.loading = false;
                this._render();
            }
        }

        _applyPayload(payload) {
            this.state.settings = payload?.settings || {};
            this.state.summarization = payload?.summarization || null;
            if (payload?.summarization?.diagnostics) {
                this.state.diagnostics = payload.summarization.diagnostics;
            }
        }

        async _toggleSetting(key, value) {
            if (!key || this.state.savingKeys.has(key)) {
                return;
            }
            const previousValue = Boolean(this.state.settings[key]);
            this.state.settings = {
                ...this.state.settings,
                [key]: value,
            };
            this.state.savingKeys.add(key);
            this._render();

            try {
                const payload = await window.SidekickNetwork.json('/api/settings', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ [key]: value }),
                }, {
                    timeoutMs: 8000,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to update settings',
                    logLabel: 'settings:update-toggle',
                });
                this._applyPayload(payload);
                this._showBanner('Settings saved.', 'success');
            } catch (error) {
                this.state.settings = {
                    ...this.state.settings,
                    [key]: previousValue,
                };
                this._showBanner(error?.message || 'Failed to update settings.', 'error');
            } finally {
                this.state.savingKeys.delete(key);
                this._render();
            }
        }

        async _switchProvider(provider) {
            const normalized = String(provider || '').trim().toLowerCase();
            const previous = String(this.state.settings?.summarization_backend || '').toLowerCase();
            if (!normalized || normalized === previous || this.state.switchingProvider) {
                this._render();
                return;
            }

            this.state.settings = {
                ...this.state.settings,
                summarization_backend: normalized,
            };
            this.state.switchingProvider = true;
            this._render();

            try {
                const payload = await window.SidekickNetwork.json('/api/settings', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ summarization_backend: normalized }),
                }, {
                    timeoutMs: 20000,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to switch summarization provider',
                    logLabel: 'settings:update-provider',
                });
                this._applyPayload(payload);
                this._showBanner('Summarization provider updated.', 'success');
                await this._runDiagnostics({ silentFailure: true });
            } catch (error) {
                this.state.settings = {
                    ...this.state.settings,
                    summarization_backend: previous,
                };
                this._showBanner(error?.message || 'Failed to switch provider.', 'error');
            } finally {
                this.state.switchingProvider = false;
                this._render();
            }
        }

        async _runDiagnostics({ silentFailure = false } = {}) {
            if (this.state.diagnosticsLoading) {
                return;
            }
            this.state.diagnosticsLoading = true;
            this._render();
            try {
                const payload = await window.SidekickNetwork.json('/api/settings/summarization/diagnostics', {
                    method: 'POST',
                }, {
                    timeoutMs: 20000,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to run diagnostics',
                    logLabel: 'settings:diagnostics',
                });
                this._applyPayload(payload);
                if (!silentFailure) {
                    this._showBanner('Diagnostics complete.', 'success');
                }
            } catch (error) {
                if (!silentFailure) {
                    this._showBanner(error?.message || 'Failed to run diagnostics.', 'error');
                }
            } finally {
                this.state.diagnosticsLoading = false;
                this._render();
            }
        }

        _showBanner(message, tone = 'success') {
            if (this._bannerTimer) {
                window.clearTimeout(this._bannerTimer);
                this._bannerTimer = null;
            }
            this.state.banner = { message, tone };
            this._renderBanner();
            this._bannerTimer = window.setTimeout(() => {
                this.state.banner = null;
                this._renderBanner();
                this._bannerTimer = null;
            }, 3000);
        }

        _renderBanner() {
            if (!this.elements.banner) {
                return;
            }
            const banner = this.state.banner;
            this.elements.banner.classList.toggle('hidden', !banner);
            this.elements.banner.classList.toggle('workspace-banner-error', banner?.tone === 'error');
            this.elements.banner.classList.toggle('workspace-banner-success', banner?.tone !== 'error');
            this.elements.banner.textContent = banner?.message || '';
        }

        _render() {
            this._renderBanner();
            if (this.elements.loading) {
                this.elements.loading.classList.toggle('hidden', !this.state.loading);
            }
            if (this.elements.list) {
                this.elements.list.classList.toggle('hidden', this.state.loading);
                this.elements.list.innerHTML = this.features.map((feature) => this._renderFeature(feature)).join('');
            }
            if (this.elements.providerLoading) {
                this.elements.providerLoading.classList.toggle('hidden', !this.state.switchingProvider);
            }
            if (this.elements.providerList) {
                this.elements.providerList.classList.toggle('hidden', this.state.loading);
                this.elements.providerList.innerHTML = this._renderProviders();
            }
            if (this.elements.runDiagnostics) {
                this.elements.runDiagnostics.disabled = this.state.loading || this.state.diagnosticsLoading || this.state.switchingProvider;
                this.elements.runDiagnostics.textContent = this.state.diagnosticsLoading ? 'Running Diagnostics...' : 'Run Diagnostics';
            }
            if (this.elements.diagnostics) {
                const shouldShow = Boolean(this.state.diagnostics);
                this.elements.diagnostics.classList.toggle('hidden', !shouldShow);
                this.elements.diagnostics.innerHTML = shouldShow ? this._renderDiagnostics() : '';
            }
        }

        _renderProviders() {
            const summarization = this.state.summarization || {};
            const providers = summarization.providers || {};
            const selected = String(this.state.settings?.summarization_backend || summarization.selected_backend || '').toLowerCase();
            return Object.entries(providers).map(([key, provider]) => {
                const checked = key === selected;
                const disabled = this.state.loading || this.state.switchingProvider;
                const configured = provider?.configured ? 'Configured' : 'Not configured';
                const subtitle = provider?.host
                    ? `${provider.model || ''} · ${provider.host}`
                    : (provider?.model || 'No model configured');
                return `
                    <label class="settings-item settings-provider-option">
                        <div class="settings-item-copy">
                            <div class="settings-item-head">
                                <h5>${this._escapeHtml(provider?.label || key)}</h5>
                            </div>
                            <p class="settings-item-description">${this._escapeHtml(subtitle)}</p>
                            <div class="settings-item-meta">${this._escapeHtml(configured)} · Applies to new requests only.</div>
                        </div>
                        <span class="settings-choice">
                            <input
                                type="radio"
                                name="summarization-backend"
                                value="${this._escapeHtml(key)}"
                                ${checked ? 'checked' : ''}
                                ${disabled ? 'disabled' : ''}
                            >
                        </span>
                    </label>
                `;
            }).join('');
        }

        _renderDiagnostics() {
            const diagnostics = this.state.diagnostics || {};
            return Object.entries(diagnostics).map(([key, diagnostic]) => {
                const ready = Boolean(diagnostic?.ready);
                const status = ready ? 'Ready' : 'Issue detected';
                const detailParts = [];
                if (diagnostic?.latency_ms != null) {
                    detailParts.push(`${Math.round(diagnostic.latency_ms)} ms`);
                }
                if (diagnostic?.request_id) {
                    detailParts.push(`request ${diagnostic.request_id}`);
                }
                return `
                    <article class="settings-item">
                        <div class="settings-item-copy">
                            <div class="settings-item-head">
                                <h5>${this._escapeHtml(this._providerLabel(key))}</h5>
                                <span class="workspace-badge ${ready ? 'workspace-badge-success' : 'workspace-badge-warning'}">${this._escapeHtml(status)}</span>
                            </div>
                            <p class="settings-item-description">${this._escapeHtml(diagnostic?.message || 'No diagnostic message returned.')}</p>
                            <div class="settings-item-meta">${this._escapeHtml(detailParts.join(' · ') || 'No extra diagnostic details.')}</div>
                        </div>
                    </article>
                `;
            }).join('');
        }

        _renderFeature(feature) {
            const enabled = Boolean(this.state.settings?.[feature.key]);
            const saving = this.state.savingKeys.has(feature.key);
            const status = saving ? 'Saving...' : (enabled ? 'Enabled' : 'Disabled');

            return `
                <article class="settings-item">
                    <div class="settings-item-copy">
                        <div class="settings-item-head">
                            <h5>${this._escapeHtml(feature.label)}</h5>
                            <span class="workspace-badge workspace-badge-warning">${this._escapeHtml(feature.badge)}</span>
                        </div>
                        <p class="settings-item-description">${this._escapeHtml(feature.description)}</p>
                        <div class="settings-item-meta">${this._escapeHtml(status)} · Applies to future workspace loads immediately.</div>
                    </div>
                    <label class="settings-toggle">
                        <input
                            type="checkbox"
                            class="settings-toggle-input"
                            data-setting-key="${this._escapeHtml(feature.key)}"
                            ${enabled ? 'checked' : ''}
                            ${saving || this.state.loading ? 'disabled' : ''}
                        >
                        <span class="settings-toggle-ui" aria-hidden="true"></span>
                        <span class="sr-only">${enabled ? 'Disable' : 'Enable'} ${this._escapeHtml(feature.label)}</span>
                    </label>
                </article>
            `;
        }

        _providerLabel(key) {
            return key === 'ollama' ? 'Local qwen3:8b' : 'OpenAI';
        }

        _escapeHtml(value) {
            return String(value ?? '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }
    }

    window.addEventListener('DOMContentLoaded', () => {
        new SettingsPage();
    });
})();
