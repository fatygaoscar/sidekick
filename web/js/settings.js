(function () {
    class SettingsPage {
        constructor() {
            this.state = {
                loading: true,
                settings: {},
                summarization: null,
                diarization: null,
                speakerProfiles: [],
                speakerProfileExamplesByProfileId: {},
                loadingSpeakerProfileExampleIds: new Set(),
                expandedSpeakerProfileIds: new Set(),
                diagnostics: null,
                savingKeys: new Set(),
                deletingSpeakerProfileIds: new Set(),
                deletingSpeakerExampleIds: new Set(),
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
                {
                    key: 'speaker_repair_enabled',
                    label: 'Speaker Repair',
                    badge: 'Experimental',
                    description: 'Advanced speaker repair tools for retranscribing with speaker counts and merging duplicate diarization clusters.',
                },
            ];

            this.elements = {
                banner: document.getElementById('settings-banner'),
                loading: document.getElementById('settings-loading'),
                list: document.getElementById('settings-list'),
                providerLoading: document.getElementById('settings-provider-loading'),
                providerList: document.getElementById('settings-provider-list'),
                diarization: document.getElementById('settings-diarization'),
                speakerProfiles: document.getElementById('settings-speaker-profiles'),
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
            this.elements.speakerProfiles?.addEventListener('click', (event) => {
                const deleteProfileButton = event.target.closest('[data-delete-speaker-profile-id]');
                if (deleteProfileButton) {
                    void this._deleteSpeakerProfile(deleteProfileButton.dataset.deleteSpeakerProfileId || '');
                    return;
                }
                const toggleExamplesButton = event.target.closest('[data-toggle-speaker-profile-examples]');
                if (toggleExamplesButton) {
                    void this._toggleSpeakerProfileExamples(toggleExamplesButton.dataset.toggleSpeakerProfileExamples || '');
                    return;
                }
                const deleteExampleButton = event.target.closest('[data-delete-speaker-example-id]');
                if (deleteExampleButton) {
                    void this._deleteSpeakerProfileExample(
                        deleteExampleButton.dataset.deleteSpeakerExampleId || '',
                        deleteExampleButton.dataset.profileId || '',
                        deleteExampleButton.dataset.profileName || ''
                    );
                }
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
                await this._loadSpeakerProfiles({ silentFailure: true });
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
            this.state.diarization = payload?.diarization || null;
            if (payload?.summarization?.diagnostics) {
                this.state.diagnostics = payload.summarization.diagnostics;
            }
        }

        async _loadSpeakerProfiles({ silentFailure = false } = {}) {
            try {
                const payload = await window.SidekickNetwork.json('/api/speaker-profiles', {}, {
                    timeoutMs: 8000,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to load speaker profiles',
                    logLabel: 'settings:speaker-profiles',
                });
                this.state.speakerProfiles = Array.isArray(payload?.profiles) ? payload.profiles : [];
            } catch (error) {
                this.state.speakerProfiles = [];
                if (!silentFailure) {
                    this._showBanner(error?.message || 'Failed to load speaker profiles.', 'error');
                }
            } finally {
                this._render();
            }
        }

        async _deleteSpeakerProfile(profileId) {
            const normalizedId = String(profileId || '').trim();
            if (!normalizedId || this.state.deletingSpeakerProfileIds.has(normalizedId)) {
                return;
            }
            this.state.deletingSpeakerProfileIds.add(normalizedId);
            this._render();
            try {
                await window.SidekickNetwork.json(`/api/speaker-profiles/${normalizedId}`, {
                    method: 'DELETE',
                }, {
                    timeoutMs: 8000,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to remove speaker profile',
                    logLabel: 'settings:delete-speaker-profile',
                });
                this.state.speakerProfiles = (this.state.speakerProfiles || []).filter((profile) => profile.id !== normalizedId);
                this._showBanner('Empty speaker profile removed.', 'success');
            } catch (error) {
                this._showBanner(error?.message || 'Failed to remove speaker profile.', 'error');
            } finally {
                this.state.deletingSpeakerProfileIds.delete(normalizedId);
                this._render();
            }
        }

        async _toggleSpeakerProfileExamples(profileId) {
            const normalizedId = String(profileId || '').trim();
            if (!normalizedId) {
                return;
            }
            if (this.state.expandedSpeakerProfileIds.has(normalizedId)) {
                this.state.expandedSpeakerProfileIds.delete(normalizedId);
                this._render();
                return;
            }
            this.state.expandedSpeakerProfileIds.add(normalizedId);
            this._render();
            if (!this.state.speakerProfileExamplesByProfileId[normalizedId]) {
                await this._loadSpeakerProfileExamples(normalizedId);
            }
        }

        async _loadSpeakerProfileExamples(profileId, { silentFailure = false } = {}) {
            const normalizedId = String(profileId || '').trim();
            if (!normalizedId || this.state.loadingSpeakerProfileExampleIds.has(normalizedId)) {
                return;
            }
            this.state.loadingSpeakerProfileExampleIds.add(normalizedId);
            this._render();
            try {
                const payload = await window.SidekickNetwork.json(`/api/speaker-profiles/${normalizedId}/examples`, {}, {
                    timeoutMs: 8000,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to load speaker profile examples',
                    logLabel: 'settings:speaker-profile-examples',
                });
                this.state.speakerProfileExamplesByProfileId = {
                    ...this.state.speakerProfileExamplesByProfileId,
                    [normalizedId]: Array.isArray(payload?.examples) ? payload.examples : [],
                };
                if (payload?.profile) {
                    this.state.speakerProfiles = (this.state.speakerProfiles || []).map((profile) => (
                        profile.id === normalizedId ? payload.profile : profile
                    ));
                }
            } catch (error) {
                if (!silentFailure) {
                    this._showBanner(error?.message || 'Failed to load saved voice examples.', 'error');
                }
            } finally {
                this.state.loadingSpeakerProfileExampleIds.delete(normalizedId);
                this._render();
            }
        }

        async _deleteSpeakerProfileExample(exampleId, profileId, profileName) {
            const normalizedExampleId = String(exampleId || '').trim();
            const normalizedProfileId = String(profileId || '').trim();
            if (!normalizedExampleId || !normalizedProfileId || this.state.deletingSpeakerExampleIds.has(normalizedExampleId)) {
                return;
            }
            const confirmed = window.confirm(
                `Remove this voice example from ${profileName || 'this speaker'}'s profile?`
            );
            if (!confirmed) {
                return;
            }

            this.state.deletingSpeakerExampleIds.add(normalizedExampleId);
            this._render();
            try {
                await window.SidekickNetwork.json(`/api/speaker-profile-examples/${normalizedExampleId}`, {
                    method: 'DELETE',
                }, {
                    timeoutMs: 8000,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: 'Settings network request failed',
                    httpErrorMessage: 'Failed to remove voice example',
                    logLabel: 'settings:delete-speaker-profile-example',
                });
                const existingExamples = Array.isArray(this.state.speakerProfileExamplesByProfileId[normalizedProfileId])
                    ? this.state.speakerProfileExamplesByProfileId[normalizedProfileId]
                    : [];
                const updatedExamples = existingExamples.filter((example) => example.id !== normalizedExampleId);
                this.state.speakerProfileExamplesByProfileId = {
                    ...this.state.speakerProfileExamplesByProfileId,
                    [normalizedProfileId]: updatedExamples,
                };
                this.state.speakerProfiles = (this.state.speakerProfiles || []).map((profile) => (
                    profile.id === normalizedProfileId
                        ? { ...profile, example_count: Math.max(0, Number(profile.example_count || 0) - 1) }
                        : profile
                ));
                this._showBanner('Voice example removed.', 'success');
            } catch (error) {
                this._showBanner(error?.message || 'Failed to remove voice example.', 'error');
            } finally {
                this.state.deletingSpeakerExampleIds.delete(normalizedExampleId);
                this._render();
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
            if (this.elements.diarization) {
                const shouldShow = Boolean(this.state.diarization);
                this.elements.diarization.classList.toggle('hidden', !shouldShow);
                this.elements.diarization.innerHTML = shouldShow ? this._renderDiarization() : '';
            }
            if (this.elements.speakerProfiles) {
                this.elements.speakerProfiles.classList.toggle('hidden', false);
                this.elements.speakerProfiles.innerHTML = this._renderSpeakerProfiles();
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

        _renderDiarization() {
            const diarization = this.state.diarization || {};
            const ready = Boolean(diarization?.ready);
            const enabled = Boolean(diarization?.enabled);
            const status = !enabled
                ? 'Disabled'
                : (ready ? 'Ready' : 'Issue detected');
            const detailParts = [];
            if (diarization?.model) {
                detailParts.push(String(diarization.model));
            }
            if (diarization?.device) {
                detailParts.push(String(diarization.device));
            }
            return `
                <article class="settings-item">
                    <div class="settings-item-copy">
                        <div class="settings-item-head">
                            <h5>Local speaker detection</h5>
                            <span class="workspace-badge ${ready ? 'workspace-badge-success' : 'workspace-badge-warning'}">${this._escapeHtml(status)}</span>
                        </div>
                        <p class="settings-item-description">${this._escapeHtml(diarization?.error || (enabled ? 'community-1 is ready for initial transcription and repair.' : 'Speaker diarization is currently disabled.'))}</p>
                        <div class="settings-item-meta">${this._escapeHtml(detailParts.join(' · ') || 'No additional diarization details.')}</div>
                    </div>
                </article>
            `;
        }

        _renderSpeakerProfiles() {
            const profiles = this.state.speakerProfiles || [];
            if (!profiles.length) {
                return `
                    <article class="settings-item">
                        <div class="settings-item-copy">
                            <div class="settings-item-head">
                                <h5>No local speaker profiles yet</h5>
                            </div>
                            <p class="settings-item-description">Add a confirmed speaker from the Speakers tab to start recognizing them across recordings.</p>
                            <div class="settings-item-meta">Profiles stay local to this Sidekick instance.</div>
                        </div>
                    </article>
                `;
            }
            return profiles.map((profile) => {
                const exampleCount = Number(profile.example_count || 0);
                const expanded = this.state.expandedSpeakerProfileIds.has(profile.id);
                const loadingExamples = this.state.loadingSpeakerProfileExampleIds.has(profile.id);
                const examples = Array.isArray(this.state.speakerProfileExamplesByProfileId[profile.id])
                    ? this.state.speakerProfileExamplesByProfileId[profile.id]
                    : [];
                return `
                <article class="settings-item">
                    <div class="settings-item-copy">
                        <div class="settings-item-head">
                            <h5>${this._escapeHtml(profile.display_name || 'Speaker')}</h5>
                        </div>
                        <p class="settings-item-description">${this._escapeHtml(`${exampleCount} saved voice example${exampleCount === 1 ? '' : 's'}.`)}</p>
                        <div class="settings-item-meta">Local profile</div>
                        ${exampleCount > 0 ? `
                            <div class="settings-example-review">
                                <button
                                    type="button"
                                    class="btn btn-small"
                                    data-toggle-speaker-profile-examples="${this._escapeHtml(profile.id)}"
                                >
                                    ${expanded ? 'Hide Examples' : 'Review Examples'}
                                </button>
                            </div>
                        ` : ''}
                        ${expanded ? `
                            <div class="settings-example-list">
                                ${loadingExamples ? `
                                    <div class="settings-example-empty">Loading saved voice examples...</div>
                                ` : (examples.length ? examples.map((example) => `
                                    <div class="settings-example-item">
                                        <div class="settings-example-meta">
                                            <div><strong>${this._escapeHtml(example.recording_title || 'Recording')}</strong></div>
                                            <div>${this._escapeHtml(this._formatExampleMeta(example))}</div>
                                        </div>
                                        <audio controls preload="none" src="${this._escapeHtml(window.SidekickNetwork.resolveUrl(example.clip_url || ''))}"></audio>
                                        <div class="settings-example-actions">
                                            <button
                                                type="button"
                                                class="btn btn-small"
                                                data-delete-speaker-example-id="${this._escapeHtml(example.id)}"
                                                data-profile-id="${this._escapeHtml(profile.id)}"
                                                data-profile-name="${this._escapeHtml(profile.display_name || 'Speaker')}"
                                                ${this.state.deletingSpeakerExampleIds.has(example.id) ? 'disabled' : ''}
                                            >
                                                ${this.state.deletingSpeakerExampleIds.has(example.id) ? 'Removing...' : 'Remove Example'}
                                            </button>
                                        </div>
                                    </div>
                                `).join('') : `
                                    <div class="settings-example-empty">No saved examples are available for review.</div>
                                `)}
                            </div>
                        ` : ''}
                    </div>
                    ${exampleCount === 0 ? `
                        <button
                            type="button"
                            class="btn btn-small"
                            data-delete-speaker-profile-id="${this._escapeHtml(profile.id)}"
                            ${this.state.deletingSpeakerProfileIds.has(profile.id) ? 'disabled' : ''}
                        >
                            ${this.state.deletingSpeakerProfileIds.has(profile.id) ? 'Removing...' : 'Remove'}
                        </button>
                    ` : ''}
                </article>
            `;
            }).join('');
        }

        _formatExampleMeta(example) {
            const parts = [];
            if (example?.source_type) {
                parts.push(this._humanizeExampleSourceType(example.source_type));
            }
            if (example?.duration_seconds != null) {
                parts.push(`${Number(example.duration_seconds || 0).toFixed(1)}s`);
            }
            if (example?.created_at) {
                const date = new Date(example.created_at);
                if (!Number.isNaN(date.getTime())) {
                    parts.push(date.toLocaleString());
                }
            }
            return parts.join(' · ') || 'Saved voice example';
        }

        _humanizeExampleSourceType(value) {
            const normalized = String(value || '').trim().toLowerCase();
            if (normalized === 'manual_match_correction') {
                return 'Match correction';
            }
            if (normalized === 'manual_promoted_example') {
                return 'Manual add';
            }
            if (normalized === 'manual_assignment') {
                return 'Manual assignment';
            }
            if (normalized === 'merged_cluster') {
                return 'Merged cluster';
            }
            return normalized
                .split('_')
                .filter(Boolean)
                .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
                .join(' ') || 'Saved example';
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
