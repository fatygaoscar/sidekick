(function () {
    class RecordingWorkspace {
        constructor(options = {}) {
            this.options = options;
            this.templates = {};
            this._settingsSaveTimer = null;
            this._settingsSavePromise = null;
            this._bannerTimer = null;
            this._bodyScrollLocked = false;
            this._speakerPlayback = null;
            this.state = {
                sessionId: null,
                workspace: null,
                activeTab: 'summary',
                jobStatus: null,
                speakerAssignments: {},
                speakerDirty: false,
                speakerEditMode: false,
                editMode: false,
                editBuffer: '',
                showRefineInput: false,
                summaryHistory: [],
                selectedSavedSummaryId: null,
                banner: null,
            };

            this._ensureDom();
            this._bindEvents();
        }

        async open(sessionId, options = {}) {
            this.options = { ...this.options, ...options };
            this.state.sessionId = sessionId;
            this.state.jobStatus = null;
            this.state.speakerAssignments = {};
            this.state.speakerDirty = false;
            this.state.speakerEditMode = false;
            this.state.editMode = false;
            this.state.editBuffer = '';
            this.state.showRefineInput = false;
            this.state.summaryHistory = [];
            this.state.selectedSavedSummaryId = null;
            this.state.banner = null;
            this.elements.modal.classList.remove('hidden');
            this._lockBodyScroll();

            await this._loadTemplates();
            await this._loadWorkspace();

            if (this.options.initialTab) {
                this.state.activeTab = this.options.initialTab;
                this._renderTabs();
            }

            if (this.options.autoStartTranscription && !this.state.workspace?.recording?.has_transcription) {
                await this._startTranscriptionJob();
            }
        }

        async close() {
            const saved = await this._flushSettingsSave();
            if (saved === false) {
                return;
            }

            this.elements.modal.classList.add('hidden');
            this._unlockBodyScroll();
            this.state.jobStatus = null;
            this.state.workspace = null;
            this.state.sessionId = null;
            this.state.speakerAssignments = {};
            this.state.speakerDirty = false;
            this.state.speakerEditMode = false;
            this.state.editMode = false;
            this.state.editBuffer = '';
            this.state.showRefineInput = false;
            this.state.summaryHistory = [];
            this.state.selectedSavedSummaryId = null;
            this._settingsSavePromise = null;
            this._stopSpeakerPlayback();
            if (typeof this.options.onClose === 'function') {
                this.options.onClose();
            }
        }

        async _loadTemplates() {
            if (Object.keys(this.templates).length > 0) {
                return;
            }

            const response = await fetch('/api/templates');
            if (!response.ok) {
                throw new Error('Failed to load templates');
            }

            const payload = await response.json();
            this.templates = payload.templates || {};
        }

        async _loadWorkspace({ keepTab = true } = {}) {
            if (!this.state.sessionId) {
                return;
            }

            const response = await fetch(`/api/recordings/${this.state.sessionId}/workspace`);
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to load recording workspace');
            }

            const payload = await response.json();
            const previousTab = this.state.activeTab;
            this.state.workspace = payload;
            this.state.selectedSavedSummaryId =
                this.state.selectedSavedSummaryId || payload.saved_summaries?.[0]?.id || null;

            if (!this.state.speakerDirty) {
                this.state.speakerAssignments = {};
                (payload.speaker_review?.speakers || []).forEach((speaker) => {
                    this.state.speakerAssignments[speaker.speaker_cluster] = speaker.display_name || '';
                });
            }

            if (!keepTab) {
                this.state.activeTab = this._defaultTab();
            } else if (!payload.state?.can_generate_summary && previousTab === 'summary') {
                this.state.activeTab = payload.state?.requires_speaker_review ? 'speakers' : previousTab;
            } else if (previousTab) {
                this.state.activeTab = previousTab;
            } else {
                this.state.activeTab = this._defaultTab();
            }

            if (!payload.draft_summary && this.state.editMode) {
                this.state.editMode = false;
                this.state.editBuffer = '';
            }

            this._render();
        }

        _defaultTab() {
            const workspace = this.state.workspace;
            if (!workspace) {
                return 'summary';
            }
            if (!workspace.recording?.has_transcription) {
                return 'speakers';
            }
            if (workspace.state?.requires_speaker_review) {
                return 'speakers';
            }
            if (workspace.draft_summary || (workspace.saved_summaries || []).length > 0) {
                return 'summary';
            }
            return 'transcript';
        }

        _ensureDom() {
            let modal = document.getElementById('recording-workspace-modal');
            if (!modal) {
                modal = document.createElement('div');
                modal.id = 'recording-workspace-modal';
                modal.className = 'modal hidden workspace-modal';
                modal.setAttribute('role', 'dialog');
                modal.setAttribute('aria-modal', 'true');
                modal.setAttribute('aria-labelledby', 'workspace-title-input');
                modal.innerHTML = `
                    <div class="modal-content workspace-modal-content">
                        <div class="modal-header workspace-header">
                            <div class="workspace-header-copy">
                                <input id="workspace-title-input" class="workspace-title-input" placeholder="Untitled Recording" autocapitalize="words">
                                <div id="workspace-meta" class="workspace-meta"></div>
                            </div>
                            <button type="button" class="modal-close" id="workspace-close" aria-label="Close workspace">&times;</button>
                        </div>
                        <div id="workspace-banner" class="workspace-banner hidden" aria-live="polite"></div>
                        <div id="workspace-progress" class="workspace-progress hidden" aria-live="polite">
                            <div class="processing-panel-header">
                                <div class="processing-title">Workspace Progress</div>
                                <div id="workspace-progress-overall" class="processing-overall">0%</div>
                            </div>
                            <div id="workspace-progress-stage" class="processing-stage">Queued</div>
                            <div class="processing-grid">
                                <div class="processing-row">
                                    <div class="processing-label-wrap">
                                        <span class="processing-label">Transcription</span>
                                        <span id="workspace-progress-transcription-text" class="processing-percent">0%</span>
                                    </div>
                                    <div class="processing-bar">
                                        <div id="workspace-progress-transcription-fill" class="processing-bar-fill"></div>
                                    </div>
                                </div>
                                <div class="processing-row">
                                    <div class="processing-label-wrap">
                                        <span class="processing-label">Summary</span>
                                        <span id="workspace-progress-summary-text" class="processing-percent">0%</span>
                                    </div>
                                    <div class="processing-bar">
                                        <div id="workspace-progress-summary-fill" class="processing-bar-fill"></div>
                                    </div>
                                </div>
                            </div>
                            <div id="workspace-progress-message" class="processing-text">Preparing...</div>
                        </div>
                        <div class="workspace-tab-row" role="tablist" aria-label="Workspace sections">
                            <button type="button" class="workspace-tab active" id="workspace-tab-speakers" data-tab="speakers" role="tab" aria-controls="workspace-panel-speakers" aria-selected="true">Speakers</button>
                            <button type="button" class="workspace-tab" id="workspace-tab-summary" data-tab="summary" role="tab" aria-controls="workspace-panel-summary" aria-selected="false">Summary</button>
                            <button type="button" class="workspace-tab" id="workspace-tab-settings" data-tab="settings" role="tab" aria-controls="workspace-panel-settings" aria-selected="false">Settings</button>
                            <button type="button" class="workspace-tab" id="workspace-tab-transcript" data-tab="transcript" role="tab" aria-controls="workspace-panel-transcript" aria-selected="false">Transcript</button>
                        </div>
                        <div class="modal-body workspace-body">
                            <section class="workspace-panel" id="workspace-panel-speakers" data-panel="speakers" role="tabpanel" aria-labelledby="workspace-tab-speakers">
                                <div class="workspace-panel-copy">
                                    <h3>Speaker Review</h3>
                                    <p id="workspace-speakers-copy" class="workspace-copy"></p>
                                </div>
                                <div id="workspace-speakers-list" class="speaker-card-list"></div>
                                <div id="workspace-speaker-actions" class="workspace-summary-actions hidden">
                                    <button type="button" class="btn" id="workspace-speaker-edit-btn">Edit</button>
                                </div>
                            </section>
                            <section class="workspace-panel hidden" id="workspace-panel-summary" data-panel="summary" role="tabpanel" aria-labelledby="workspace-tab-summary" aria-hidden="true">
                                <div class="workspace-panel-copy">
                                    <h3>Summary Draft</h3>
                                    <div id="workspace-summary-meta" class="summary-meta"></div>
                                </div>
                                <div id="workspace-summary-version-row" class="summary-version-row hidden">
                                    <select id="workspace-summary-version-select" class="version-select"></select>
                                </div>
                                <div id="workspace-summary-display" class="summary-body"></div>
                                <textarea id="workspace-summary-edit" class="summary-edit-textarea hidden" spellcheck="true"></textarea>
                                <div class="workspace-summary-actions">
                                    <button class="btn" id="workspace-edit-btn">Edit</button>
                                    <button class="btn" id="workspace-revise-btn">Ask AI to Revise</button>
                                    <button class="btn hidden" id="workspace-undo-btn">Undo</button>
                                </div>
                                <div id="workspace-refine-section" class="hidden">
                                    <div class="refine-input-row">
                                        <input type="text" id="workspace-refine-input" class="refine-input" placeholder="e.g. tighten the takeaways, make it more technical">
                                        <button type="button" class="btn" id="workspace-refine-cancel">Cancel</button>
                                        <button type="button" class="btn btn-primary" id="workspace-refine-submit">Revise</button>
                                    </div>
                                </div>
                            </section>
                            <section class="workspace-panel hidden" id="workspace-panel-transcript" data-panel="transcript" role="tabpanel" aria-labelledby="workspace-tab-transcript" aria-hidden="true">
                                <div class="workspace-panel-copy">
                                    <h3>Transcript</h3>
                                    <p class="workspace-copy">The transcript stays available while you review speakers and summary changes.</p>
                                </div>
                                <div id="workspace-transcript" class="transcript-view"></div>
                            </section>
                            <section class="workspace-panel hidden" id="workspace-panel-settings" data-panel="settings" role="tabpanel" aria-labelledby="workspace-tab-settings" aria-hidden="true">
                                <div class="workspace-panel-copy">
                                    <h3>Summary Settings</h3>
                                    <p class="workspace-copy">Template and prompt persist with this recording.</p>
                                </div>
                                <div class="form-group">
                                    <label class="form-label">Template</label>
                                    <div id="workspace-template-grid" class="template-grid"></div>
                                </div>
                                <div class="form-group">
                                    <label class="form-label">Custom Prompt <span class="form-label-optional">(optional)</span></label>
                                    <textarea id="workspace-custom-prompt" class="form-input form-textarea workspace-prompt-textarea" placeholder="Leave blank to use the selected template prompt."></textarea>
                                </div>
                            </section>
                        </div>
                        <div class="modal-footer workspace-footer">
                            <div id="workspace-footer-status" class="workspace-footer-status"></div>
                            <div class="workspace-footer-actions">
                                <button type="button" class="btn hidden" id="workspace-open-obsidian-btn">Open in Obsidian</button>
                                <button type="button" class="btn" id="workspace-secondary-btn">Close</button>
                                <button type="button" class="btn btn-primary" id="workspace-primary-btn">Continue</button>
                            </div>
                        </div>
                    </div>
                `;
                document.body.appendChild(modal);
            }

            this.elements = {
                modal,
                close: modal.querySelector('#workspace-close'),
                banner: modal.querySelector('#workspace-banner'),
                titleInput: modal.querySelector('#workspace-title-input'),
                meta: modal.querySelector('#workspace-meta'),
                progress: modal.querySelector('#workspace-progress'),
                progressStage: modal.querySelector('#workspace-progress-stage'),
                progressMessage: modal.querySelector('#workspace-progress-message'),
                progressOverall: modal.querySelector('#workspace-progress-overall'),
                progressTranscriptionText: modal.querySelector('#workspace-progress-transcription-text'),
                progressSummaryText: modal.querySelector('#workspace-progress-summary-text'),
                progressTranscriptionFill: modal.querySelector('#workspace-progress-transcription-fill'),
                progressSummaryFill: modal.querySelector('#workspace-progress-summary-fill'),
                tabButtons: Array.from(modal.querySelectorAll('.workspace-tab')),
                panels: Array.from(modal.querySelectorAll('.workspace-panel')),
                speakersCopy: modal.querySelector('#workspace-speakers-copy'),
                speakersList: modal.querySelector('#workspace-speakers-list'),
                speakerActions: modal.querySelector('#workspace-speaker-actions'),
                speakerEditBtn: modal.querySelector('#workspace-speaker-edit-btn'),
                summaryMeta: modal.querySelector('#workspace-summary-meta'),
                summaryVersionRow: modal.querySelector('#workspace-summary-version-row'),
                summaryVersionSelect: modal.querySelector('#workspace-summary-version-select'),
                summaryDisplay: modal.querySelector('#workspace-summary-display'),
                summaryEdit: modal.querySelector('#workspace-summary-edit'),
                editBtn: modal.querySelector('#workspace-edit-btn'),
                reviseBtn: modal.querySelector('#workspace-revise-btn'),
                undoBtn: modal.querySelector('#workspace-undo-btn'),
                refineSection: modal.querySelector('#workspace-refine-section'),
                refineInput: modal.querySelector('#workspace-refine-input'),
                refineCancel: modal.querySelector('#workspace-refine-cancel'),
                refineSubmit: modal.querySelector('#workspace-refine-submit'),
                transcript: modal.querySelector('#workspace-transcript'),
                templateGrid: modal.querySelector('#workspace-template-grid'),
                customPrompt: modal.querySelector('#workspace-custom-prompt'),
                footerStatus: modal.querySelector('#workspace-footer-status'),
                openObsidianBtn: modal.querySelector('#workspace-open-obsidian-btn'),
                secondaryBtn: modal.querySelector('#workspace-secondary-btn'),
                primaryBtn: modal.querySelector('#workspace-primary-btn'),
            };
        }

        _bindEvents() {
            this.elements.close.addEventListener('click', () => this.close());
            this.elements.modal.addEventListener('click', (event) => {
                if (event.target === this.elements.modal) {
                    this.close();
                }
            });

            this.elements.tabButtons.forEach((button) => {
                button.addEventListener('click', () => {
                    this.state.activeTab = button.dataset.tab;
                    this._renderTabs();
                    this._renderFooter();
                });
            });

            this.elements.titleInput.addEventListener('input', () => this._queueSettingsSave());
            this.elements.customPrompt.addEventListener('input', () => {
                this._autoResizeTextarea(this.elements.customPrompt);
                this._queueSettingsSave();
            });

            this.elements.templateGrid.addEventListener('click', (event) => {
                const target = event.target.closest('.template-btn');
                if (!target) {
                    return;
                }
                this._selectTemplate(target.dataset.template);
            });

            this.elements.speakersList.addEventListener('input', (event) => {
                const input = event.target.closest('.speaker-name-input');
                if (!input) {
                    return;
                }
                this.state.speakerAssignments[input.dataset.cluster] = input.value;
                this.state.speakerDirty = true;
                this._renderFooter();
            });
            this.elements.speakerEditBtn.addEventListener('click', () => this._toggleSpeakerEditMode());

            this.elements.summaryVersionSelect.addEventListener('change', (event) => {
                this.state.selectedSavedSummaryId = event.target.value;
                this._renderSummary();
            });

            this.elements.editBtn.addEventListener('click', () => this._toggleEditMode());
            this.elements.reviseBtn.addEventListener('click', () => this._showRefineInput());
            this.elements.refineCancel.addEventListener('click', () => this._hideRefineInput());
            this.elements.refineSubmit.addEventListener('click', () => this._submitRefine());
            this.elements.refineInput.addEventListener('keypress', (event) => {
                if (event.key === 'Enter') {
                    this._submitRefine();
                }
            });
            this.elements.undoBtn.addEventListener('click', () => this._undoSummaryChange());
            this.elements.summaryEdit.addEventListener('input', () => {
                this.state.editBuffer = this.elements.summaryEdit.value;
                this._autoResizeTextarea(this.elements.summaryEdit);
            });

            this.elements.openObsidianBtn.addEventListener('click', () => this._openInObsidian());
            this.elements.secondaryBtn.addEventListener('click', () => this.close());
            this.elements.primaryBtn.addEventListener('click', () => this._handlePrimaryAction());
        }

        _lockBodyScroll() {
            if (this._bodyScrollLocked) {
                return;
            }

            const body = document.body;
            const lockCount = Number(body.dataset.modalLockCount || 0);

            if (lockCount === 0) {
                const scrollY = window.scrollY || window.pageYOffset || 0;
                body.dataset.modalScrollY = String(scrollY);
                body.classList.add('modal-open');
                body.style.top = `-${scrollY}px`;
            }

            body.dataset.modalLockCount = String(lockCount + 1);
            this._bodyScrollLocked = true;
        }

        _unlockBodyScroll() {
            if (!this._bodyScrollLocked) {
                return;
            }

            const body = document.body;
            const lockCount = Number(body.dataset.modalLockCount || 0);
            const nextCount = Math.max(0, lockCount - 1);

            if (nextCount === 0) {
                const scrollY = Number(body.dataset.modalScrollY || 0);
                body.classList.remove('modal-open');
                body.style.top = '';
                delete body.dataset.modalLockCount;
                delete body.dataset.modalScrollY;
                window.scrollTo(0, scrollY);
            } else {
                body.dataset.modalLockCount = String(nextCount);
            }

            this._bodyScrollLocked = false;
        }

        _render() {
            this._renderHeader();
            this._renderProgress();
            this._renderBanner();
            this._renderTabs();
            this._renderSpeakers();
            this._renderSummary();
            this._renderTranscript();
            this._renderSettings();
            this._renderFooter();
        }

        _renderHeader() {
            const workspace = this.state.workspace;
            if (!workspace) {
                return;
            }

            if (document.activeElement !== this.elements.titleInput) {
                this.elements.titleInput.value = workspace.settings?.title || '';
            }

            const parts = [
                workspace.recording?.recorded_date_label,
                workspace.recording?.recorded_time_label,
                workspace.recording?.recorded_timezone_label,
                workspace.recording?.duration_seconds != null
                    ? this._formatDuration(workspace.recording.duration_seconds)
                    : null,
            ].filter(Boolean);

            const badges = [];
            if (workspace.state?.requires_speaker_review) {
                badges.push('<span class="workspace-badge workspace-badge-warning">Needs speaker review</span>');
            }
            if (workspace.draft_summary) {
                badges.push('<span class="workspace-badge workspace-badge-accent">Draft</span>');
            } else if ((workspace.saved_summaries || []).length > 0) {
                badges.push('<span class="workspace-badge">Saved</span>');
            }
            if (workspace.state?.summary_out_of_date) {
                badges.push('<span class="workspace-badge workspace-badge-warning">Out of date</span>');
            }

            this.elements.meta.innerHTML = `
                <span>${parts.join(' · ') || 'Recording workspace'}</span>
                ${badges.join('')}
            `;
        }

        _renderProgress() {
            const job = this.state.jobStatus;
            this.elements.progress.classList.toggle('hidden', !job);
            if (!job) {
                return;
            }

            const transcriptionProgress = Math.round((job.transcription_progress || 0) * 100);
            const summaryProgress = Math.round((job.summarization_progress || 0) * 100);
            const overall = Math.round((job.overall_progress || 0) * 100);

            this.elements.progressStage.textContent = this._formatStage(job.stage);
            this.elements.progressMessage.textContent = job.message || 'Working...';
            this.elements.progressOverall.textContent = `${overall}%`;
            this.elements.progressTranscriptionText.textContent = `${transcriptionProgress}%`;
            this.elements.progressSummaryText.textContent = `${summaryProgress}%`;
            this.elements.progressTranscriptionFill.style.width = `${transcriptionProgress}%`;
            this.elements.progressSummaryFill.style.width = `${summaryProgress}%`;
        }

        _renderBanner() {
            const banner = this.state.banner;
            this.elements.banner.classList.toggle('hidden', !banner);
            this.elements.banner.classList.toggle('workspace-banner-error', banner?.tone === 'error');
            this.elements.banner.classList.toggle('workspace-banner-success', banner?.tone === 'success');
            this.elements.banner.textContent = banner?.message || '';
        }

        _renderTabs() {
            this.elements.tabButtons.forEach((button) => {
                const active = button.dataset.tab === this.state.activeTab;
                button.classList.toggle('active', active);
                button.setAttribute('aria-selected', active ? 'true' : 'false');
                button.tabIndex = active ? 0 : -1;
            });
            this.elements.panels.forEach((panel) => {
                const active = panel.dataset.panel === this.state.activeTab;
                panel.classList.toggle('hidden', !active);
                panel.setAttribute('aria-hidden', active ? 'false' : 'true');
            });
        }

        _renderSpeakers() {
            const workspace = this.state.workspace;
            const speakers = workspace?.speaker_review?.speakers || [];
            const inputsLocked = this._speakerInputsLocked();
            this._stopSpeakerPlayback();

            if (!workspace?.recording?.has_transcription) {
                this.elements.speakersCopy.textContent = 'Transcription will start automatically once the recording is ready.';
                this.elements.speakersList.innerHTML = '<div class="workspace-empty">No transcript yet.</div>';
                this.elements.speakerActions.classList.add('hidden');
                return;
            }

            if (workspace.speaker_review?.required) {
                this.elements.speakersCopy.textContent = 'Assign names you know now. Any unresolved speakers will appear as Attendee labels in the transcript and summary until you map them.';
            } else {
                this.elements.speakersCopy.textContent = 'Single-speaker or already-reviewed recordings can continue immediately.';
            }

            if (speakers.length === 0) {
                this.elements.speakersList.innerHTML = '<div class="workspace-empty">No speaker clusters were detected.</div>';
                this.elements.speakerActions.classList.add('hidden');
                return;
            }

            this.elements.speakersList.innerHTML = speakers
                .map((speaker, index) => {
                    const value = this.state.speakerAssignments[speaker.speaker_cluster] ?? speaker.display_name ?? '';
                    const disabledAttr = inputsLocked ? 'disabled' : '';
                    return `
                        <div class="speaker-card">
                            <div class="speaker-card-header">
                                <div>
                                    <div class="speaker-card-label">${this._escapeHtml(speaker.speaker_cluster)}</div>
                                    <div class="speaker-card-preview">${this._escapeHtml(speaker.preview_text || '')}</div>
                                </div>
                            </div>
                            <div class="speaker-card-controls">
                                <audio id="workspace-speaker-audio-${index}" preload="none" src="${speaker.audio_url}"></audio>
                                <button
                                    type="button"
                                    class="btn btn-small speaker-audio-btn"
                                    data-audio-id="workspace-speaker-audio-${index}"
                                    data-start="${speaker.clip_start}"
                                    data-end="${speaker.clip_end}"
                                >
                                    Play Clip
                                </button>
                                <input
                                    type="text"
                                    class="form-input speaker-name-input"
                                    data-cluster="${this._escapeHtml(speaker.speaker_cluster)}"
                                    value="${this._escapeHtml(value)}"
                                    placeholder="Enter speaker name"
                                    ${disabledAttr}
                                >
                            </div>
                        </div>
                    `;
                })
                .join('');

            const showSpeakerActions = this._canLockSpeakerInputs() || this.state.speakerEditMode;
            this.elements.speakerActions.classList.toggle('hidden', !showSpeakerActions);
            this.elements.speakerEditBtn.textContent = this.state.speakerEditMode ? 'Done Editing' : 'Edit';

            this.elements.speakersList.querySelectorAll('.speaker-audio-btn').forEach((button) => {
                button.addEventListener('click', () => {
                    const audio = document.getElementById(button.dataset.audioId);
                    if (!audio) {
                        return;
                    }
                    const start = Number(button.dataset.start || 0);
                    const end = Number(button.dataset.end || 0);
                    const isCurrent = this._speakerPlayback?.audio === audio && !audio.paused;
                    if (isCurrent) {
                        this._stopSpeakerPlayback();
                        return;
                    }
                    this._playSpeakerClip({ button, audio, start, end });
                });
            });
        }

        _speakerAssignmentsComplete() {
            const speakers = this.state.workspace?.speaker_review?.speakers || [];
            return speakers.length > 0 && speakers.every((speaker) => {
                const value = this.state.speakerAssignments[speaker.speaker_cluster] ?? speaker.display_name ?? '';
                return Boolean(String(value).trim());
            });
        }

        _canLockSpeakerInputs() {
            return Boolean(this.state.workspace?.speaker_review?.completed) || this._speakerAssignmentsComplete();
        }

        _speakerInputsLocked() {
            return this._canLockSpeakerInputs() && !this.state.speakerEditMode;
        }

        async _toggleSpeakerEditMode() {
            if (!this.state.speakerEditMode) {
                this.state.speakerEditMode = true;
                this._renderSpeakers();
                const firstInput = this.elements.speakersList.querySelector('.speaker-name-input:not(:disabled)');
                firstInput?.focus();
                return;
            }

            this.state.speakerEditMode = false;
            if (this.state.speakerDirty) {
                try {
                    await this._saveSpeakerAssignments();
                } catch (error) {
                    this.state.speakerEditMode = true;
                    this._renderSpeakers();
                    this._showBanner(error.message || 'Failed to save speaker assignments.', 'error');
                    return;
                }
            } else {
                this._renderSpeakers();
            }
            this._renderFooter();
        }

        _playSpeakerClip({ button, audio, start, end }) {
            this._stopSpeakerPlayback();

            audio.currentTime = start;
            button.textContent = 'Stop Clip';

            const stopAtEnd = () => {
                if (audio.currentTime >= end) {
                    this._stopSpeakerPlayback();
                }
            };
            const handlePause = () => {
                if (this._speakerPlayback?.audio === audio) {
                    this._stopSpeakerPlayback();
                }
            };

            audio.addEventListener('timeupdate', stopAtEnd);
            audio.addEventListener('pause', handlePause);

            this._speakerPlayback = {
                audio,
                button,
                stopAtEnd,
                handlePause,
            };

            audio.play().catch(() => {
                this._stopSpeakerPlayback();
            });
        }

        _stopSpeakerPlayback() {
            if (!this._speakerPlayback) {
                return;
            }

            const { audio, button, stopAtEnd, handlePause } = this._speakerPlayback;
            audio.removeEventListener('timeupdate', stopAtEnd);
            audio.removeEventListener('pause', handlePause);
            if (!audio.paused) {
                audio.pause();
            }
            if (button?.isConnected) {
                button.textContent = 'Play Clip';
            }
            this._speakerPlayback = null;
        }

        _renderSummary() {
            const workspace = this.state.workspace;
            const summary = this._currentSummary();

            this.elements.summaryVersionRow.classList.toggle(
                'hidden',
                !workspace || (workspace.saved_summaries || []).length <= 1
            );

            if ((workspace?.saved_summaries || []).length > 1) {
                this.elements.summaryVersionSelect.innerHTML = workspace.saved_summaries
                    .map((saved, index) => {
                        const selected = saved.id === this.state.selectedSavedSummaryId ? 'selected' : '';
                        const label = saved.saved_to_obsidian_at
                            ? `Saved ${index + 1}`
                            : `Version ${index + 1}`;
                        return `<option value="${saved.id}" ${selected}>${label}</option>`;
                    })
                    .join('');
            } else {
                this.elements.summaryVersionSelect.innerHTML = '';
            }

            const metaParts = [];
            if (summary?.status === 'draft') {
                metaParts.push('Current draft');
            } else if (summary?.status === 'saved') {
                metaParts.push('Saved version');
            }
            if (summary?.template) {
                metaParts.push(summary.template);
            }
            if (summary?.source_type) {
                metaParts.push(summary.source_type.replace(/_/g, ' '));
            }
            if (workspace?.state?.summary_out_of_date) {
                metaParts.push('Needs re-summarization');
            }
            this.elements.summaryMeta.textContent = metaParts.join(' · ');

            if (!summary) {
                const emptyMessage = workspace?.state?.requires_speaker_review
                    ? 'Generate a summary when you are ready. Unresolved speakers will be shown as Attendee labels.'
                    : 'Generate a summary once transcription is ready.';
                this.elements.summaryDisplay.innerHTML = `<div class="workspace-empty">${this._escapeHtml(emptyMessage)}</div>`;
                this.elements.summaryEdit.classList.add('hidden');
                this.elements.summaryDisplay.classList.remove('hidden');
                this.elements.editBtn.disabled = true;
                this.elements.reviseBtn.disabled = true;
                this.elements.undoBtn.classList.toggle('hidden', this.state.summaryHistory.length === 0);
                this.elements.refineSection.classList.add('hidden');
                return;
            }

            this.elements.editBtn.disabled = false;
            this.elements.reviseBtn.disabled = false;
            this.elements.undoBtn.classList.toggle('hidden', this.state.summaryHistory.length === 0);
            this.elements.editBtn.textContent = this.state.editMode ? 'Done Editing' : 'Edit';

            if (this.state.editMode) {
                this.elements.summaryEdit.classList.remove('hidden');
                this.elements.summaryDisplay.classList.add('hidden');
                if (!this.state.editBuffer) {
                    this.state.editBuffer = summary.content || '';
                }
                this.elements.summaryEdit.value = this.state.editBuffer;
                this._autoResizeTextarea(this.elements.summaryEdit);
            } else {
                this.elements.summaryEdit.classList.add('hidden');
                this.elements.summaryDisplay.classList.remove('hidden');
                this._renderMarkdown(this.elements.summaryDisplay, summary.content || '');
            }

            this.elements.refineSection.classList.toggle('hidden', !this.state.showRefineInput);
        }

        _renderTranscript() {
            const transcript = this.state.workspace?.transcript || [];
            if (!transcript.length) {
                this.elements.transcript.innerHTML = '<div class="workspace-empty">Transcript not available yet.</div>';
                return;
            }

            this.elements.transcript.innerHTML = transcript
                .map((segment) => {
                    const importantClass = segment.is_important ? ' important' : '';
                    const speaker = segment.speaker ? `<span class="speaker">${this._escapeHtml(segment.speaker)}</span> ` : '';
                    return `
                        <div class="transcript-segment${importantClass}">
                            <span class="timestamp">${this._escapeHtml(segment.timestamp)}</span>
                            <div class="text">${speaker}${this._escapeHtml(segment.text)}</div>
                        </div>
                    `;
                })
                .join('');
        }

        _renderSettings() {
            const workspace = this.state.workspace;
            const settings = workspace?.settings || {};
            const selectedTemplate = settings.template_key || 'meeting';
            const order = ['meeting', 'strategic_review', 'working_session', 'standup', 'one_on_one', 'brainstorm', 'custom'];

            this.elements.templateGrid.innerHTML = order
                .filter((key) => this.templates[key])
                .map((key) => {
                    const template = this.templates[key];
                    const selectedClass = key === selectedTemplate ? ' selected' : '';
                    return `<button type="button" class="template-btn${selectedClass}" data-template="${key}">${this._escapeHtml(template.name)}</button>`;
                })
                .join('');

            if (document.activeElement !== this.elements.customPrompt) {
                this.elements.customPrompt.value = settings.custom_prompt || '';
                this._autoResizeTextarea(this.elements.customPrompt);
            }
        }

        _renderFooter() {
            const workspace = this.state.workspace;
            const primaryAction = this._getPrimaryAction();

            this.elements.footerStatus.textContent = this._footerStatusText();
            this.elements.primaryBtn.textContent = primaryAction.label;
            this.elements.primaryBtn.disabled = !!primaryAction.disabled;

            const showOpenButton = Boolean(workspace?.obsidian?.open_uri) && primaryAction.action !== 'open_obsidian';
            this.elements.openObsidianBtn.classList.toggle('hidden', !showOpenButton);
        }

        _footerStatusText() {
            const workspace = this.state.workspace;
            if (!workspace) {
                return 'Loading workspace...';
            }
            if (this.state.jobStatus) {
                return this.state.jobStatus.message || 'Working...';
            }
            if (workspace.state?.requires_speaker_review) {
                return 'Review speakers now, or continue with Attendee labels for anyone still unresolved.';
            }
            if (workspace.draft_summary) {
                return 'Current draft has not been saved to Obsidian.';
            }
            if (workspace.state?.summary_out_of_date) {
                return 'Current summary is stale against the latest speakers or settings.';
            }
            if (workspace.obsidian?.open_uri) {
                return 'Saved version is available in Obsidian.';
            }
            if (!workspace.recording?.has_transcription) {
                return 'Transcription is required before summary generation.';
            }
            return 'Recording workspace ready.';
        }

        _getPrimaryAction() {
            const workspace = this.state.workspace;
            if (!workspace) {
                return { label: 'Loading...', disabled: true, action: 'none' };
            }
            if (this.state.jobStatus) {
                return {
                    label: this.state.jobStatus.stage === 'transcribing' ? 'Transcribing...' : 'Working...',
                    disabled: true,
                    action: 'none',
                };
            }
            if (!workspace.recording?.has_transcription) {
                return { label: 'Transcribe Recording', action: 'transcribe' };
            }
            if (workspace.state?.requires_speaker_review) {
                if (
                    this.state.activeTab === 'summary'
                    && !this._currentSummary()
                    && workspace.state?.can_generate_summary
                ) {
                    return { label: 'Generate Summary', action: 'summarize' };
                }
                if (this.state.activeTab !== 'speakers') {
                    return { label: 'Review Speakers', action: 'go_speakers' };
                }
                return { label: 'Continue to Summary', action: 'complete_speakers' };
            }
            if (workspace.state?.summary_out_of_date) {
                return { label: 'Re-summarize', action: 'summarize' };
            }
            if (workspace.draft_summary) {
                return { label: 'Save to Obsidian', action: 'save_draft' };
            }
            if (!this._currentSummary() && workspace.state?.can_generate_summary) {
                return { label: 'Generate Summary', action: 'summarize' };
            }
            if (workspace.obsidian?.open_uri) {
                return { label: 'Open in Obsidian', action: 'open_obsidian' };
            }
            if (this.state.activeTab !== 'summary') {
                return { label: 'View Summary', action: 'go_summary' };
            }
            return { label: 'Close', action: 'close' };
        }

        async _handlePrimaryAction() {
            const action = this._getPrimaryAction().action;
            switch (action) {
                case 'transcribe':
                    await this._startTranscriptionJob();
                    break;
                case 'go_speakers':
                    this.state.activeTab = 'speakers';
                    this._renderTabs();
                    this._renderFooter();
                    break;
                case 'complete_speakers':
                    await this._completeSpeakerReview();
                    break;
                case 'summarize':
                    await this._startSummaryJob();
                    break;
                case 'save_draft':
                    await this._saveDraft();
                    break;
                case 'open_obsidian':
                    this._openInObsidian();
                    break;
                case 'go_summary':
                    this.state.activeTab = 'summary';
                    this._renderTabs();
                    this._renderFooter();
                    break;
                case 'close':
                    await this.close();
                    break;
                default:
                    break;
            }
        }

        async _completeSpeakerReview() {
            try {
                this.state.activeTab = 'summary';
                this._renderTabs();
                this._renderFooter();
                await this._startSummaryJob();
            } catch (error) {
                this._showBanner(error.message || 'Failed to save speaker assignments.', 'error');
            }
        }

        async _startTranscriptionJob() {
            if (!this.state.sessionId) {
                return;
            }
            const saved = await this._flushSettingsSave();
            if (saved === false) {
                return;
            }

            const response = await fetch(`/api/recordings/${this.state.sessionId}/transcription-job`, {
                method: 'POST',
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                this._showBanner(error.detail || 'Failed to start transcription.', 'error');
                return;
            }

            const payload = await response.json();
            await this._pollJob('transcription', payload.job_id, `/api/transcription-jobs/${payload.job_id}`);
        }

        async _startSummaryJob() {
            if (!this.state.sessionId) {
                return;
            }

            try {
                const saved = await this._flushSettingsSave();
                if (saved === false) {
                    return;
                }
                if (this.state.speakerDirty) {
                    await this._saveSpeakerAssignments();
                }
            } catch (error) {
                this._showBanner(error.message || 'Failed to save workspace changes.', 'error');
                return;
            }

            const response = await fetch(`/api/recordings/${this.state.sessionId}/summary-job`, {
                method: 'POST',
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                this._showBanner(error.detail || 'Failed to start summary generation.', 'error');
                return;
            }

            const payload = await response.json();
            await this._pollJob('summary', payload.job_id, `/api/summary-jobs/${payload.job_id}`);
        }

        async _pollJob(kind, jobId, url) {
            while (this.state.sessionId) {
                const response = await fetch(url);
                if (!response.ok) {
                    const error = await response.json().catch(() => ({}));
                    this.state.jobStatus = null;
                    this._render();
                    this._showBanner(error.detail || 'Failed to read job status.', 'error');
                    return;
                }

                const job = await response.json();
                this.state.jobStatus = {
                    kind,
                    ...job,
                };
                this._renderProgress();
                this._renderFooter();

                if (job.status === 'completed') {
                    this.state.jobStatus = null;
                    await this._loadWorkspace({ keepTab: false });
                    this.state.activeTab = kind === 'transcription'
                        ? (this.state.workspace?.state?.requires_speaker_review ? 'speakers' : 'summary')
                        : 'summary';
                    this._render();
                    this._showBanner(
                        kind === 'transcription' ? 'Transcript ready.' : 'Summary draft ready.',
                        'success'
                    );
                    return;
                }

                if (job.status === 'failed') {
                    this.state.jobStatus = null;
                    this._render();
                    this._showBanner(job.error || 'Job failed.', 'error');
                    return;
                }

                await this._sleep(900);
            }
        }

        async _saveSpeakerAssignments() {
            if (!this.state.speakerDirty && this.state.workspace?.speaker_review?.completed) {
                return;
            }

            const assignments = {};
            Object.entries(this.state.speakerAssignments).forEach(([cluster, value]) => {
                if (value && value.trim()) {
                    assignments[cluster] = value.trim();
                }
            });

            const response = await fetch(`/api/recordings/${this.state.sessionId}/speakers`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ assignments }),
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to save speaker assignments');
            }

            this.state.speakerDirty = false;
            await this._loadWorkspace();
        }

        _showRefineInput() {
            this.state.showRefineInput = true;
            this._renderSummary();
            this.elements.refineInput.value = '';
            this.elements.refineInput.focus();
        }

        _hideRefineInput() {
            this.state.showRefineInput = false;
            this._renderSummary();
        }

        async _submitRefine() {
            const instruction = this.elements.refineInput.value.trim();
            if (!instruction) {
                this.elements.refineInput.focus();
                return;
            }

            const draftId = await this._ensureDraft('ai_revised');
            const currentSummary = this._currentSummary();
            if (currentSummary?.content) {
                this.state.summaryHistory.push(currentSummary.content);
            }

            this.elements.refineSubmit.disabled = true;
            this.elements.refineSubmit.textContent = 'Revising...';
            try {
                const response = await fetch(`/api/summary-drafts/${draftId}/revise`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ instruction }),
                });
                if (!response.ok) {
                    const error = await response.json().catch(() => ({}));
                    throw new Error(error.detail || 'Revision failed');
                }
                this.state.showRefineInput = false;
                await this._loadWorkspace();
                this.state.activeTab = 'summary';
                this._render();
            } catch (error) {
                this._showBanner(error.message, 'error');
            } finally {
                this.elements.refineSubmit.disabled = false;
                this.elements.refineSubmit.textContent = 'Revise';
            }
        }

        async _toggleEditMode() {
            if (!this.state.editMode) {
                try {
                    await this._ensureDraft('manual_edit');
                } catch (error) {
                    this._showBanner(error.message || 'Failed to create summary draft.', 'error');
                    return;
                }
                this.state.editMode = true;
                this.state.editBuffer = this._currentSummary()?.content || '';
                this._renderSummary();
                this.elements.summaryEdit.focus();
                return;
            }

            const draft = this.state.workspace?.draft_summary;
            if (!draft) {
                this.state.editMode = false;
                this.state.editBuffer = '';
                this._renderSummary();
                return;
            }

            const currentContent = draft.content || '';
            const nextContent = this.elements.summaryEdit.value;
            if (nextContent !== currentContent) {
                this.state.summaryHistory.push(currentContent);
                const response = await fetch(`/api/summary-drafts/${draft.id}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content: nextContent }),
                });
                if (!response.ok) {
                    const error = await response.json().catch(() => ({}));
                    this._showBanner(error.detail || 'Failed to save manual edits.', 'error');
                    return;
                }
                await this._loadWorkspace();
            }

            this.state.editMode = false;
            this.state.editBuffer = '';
            this._render();
        }

        async _undoSummaryChange() {
            if (this.state.summaryHistory.length === 0) {
                return;
            }
            const previous = this.state.summaryHistory.pop();
            let draftId;
            try {
                draftId = await this._ensureDraft('manual_edit');
            } catch (error) {
                this._showBanner(error.message || 'Failed to create summary draft.', 'error');
                return;
            }
            const response = await fetch(`/api/summary-drafts/${draftId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: previous }),
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                this._showBanner(error.detail || 'Failed to undo summary change.', 'error');
                return;
            }
            await this._loadWorkspace();
            this._render();
        }

        async _ensureDraft(sourceType) {
            if (this.state.workspace?.draft_summary?.id) {
                return this.state.workspace.draft_summary.id;
            }

            const sourceSummary = this._currentSummary();
            const response = await fetch(`/api/recordings/${this.state.sessionId}/summary-draft`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    source_type: sourceType,
                    source_summary_id: sourceSummary?.status === 'saved' ? sourceSummary.id : null,
                }),
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                throw new Error(error.detail || 'Failed to create summary draft');
            }
            const payload = await response.json();
            await this._loadWorkspace();
            return payload.draft_summary_id;
        }

        async _saveDraft() {
            const draft = this.state.workspace?.draft_summary;
            if (!draft) {
                return;
            }

            await this._flushSettingsSave();
            this.elements.primaryBtn.disabled = true;
            this.elements.primaryBtn.textContent = 'Saving...';
            try {
                const response = await fetch(`/api/summary-drafts/${draft.id}/save`, {
                    method: 'POST',
                });
                if (!response.ok) {
                    const error = await response.json().catch(() => ({}));
                    throw new Error(error.detail || 'Failed to save draft');
                }
                await this._loadWorkspace();
                this.state.activeTab = 'summary';
                this._render();
                this._showBanner('Saved to Obsidian.', 'success');
                if (typeof this.options.onSaved === 'function') {
                    this.options.onSaved();
                }
            } catch (error) {
                this._showBanner(error.message, 'error');
            } finally {
                this.elements.primaryBtn.disabled = false;
            }
        }

        _openInObsidian() {
            const uri = this.state.workspace?.obsidian?.open_uri;
            if (!uri) {
                return;
            }
            window.open(uri, '_blank');
        }

        _currentSummary() {
            const workspace = this.state.workspace;
            if (!workspace) {
                return null;
            }
            if (workspace.draft_summary) {
                return workspace.draft_summary;
            }
            if (!workspace.saved_summaries || workspace.saved_summaries.length === 0) {
                return null;
            }
            return (
                workspace.saved_summaries.find((summary) => summary.id === this.state.selectedSavedSummaryId) ||
                workspace.saved_summaries[0]
            );
        }

        _selectTemplate(templateKey) {
            const current = this.state.workspace?.settings || {};
            this.state.workspace.settings = {
                ...current,
                template_key: templateKey,
            };
            this._renderSettings();
            this._queueSettingsSave();
        }

        _queueSettingsSave() {
            if (!this.state.workspace) {
                return;
            }
            this.state.workspace.settings = {
                ...this.state.workspace.settings,
                title: this.elements.titleInput.value.trim() || null,
                custom_prompt: this.elements.customPrompt.value.trim() || null,
            };
            this.elements.footerStatus.textContent = 'Saving settings...';
            if (this._settingsSaveTimer) {
                clearTimeout(this._settingsSaveTimer);
            }
            this._settingsSaveTimer = window.setTimeout(() => {
                this._settingsSaveTimer = null;
                this._settingsSavePromise = this._saveSettings();
            }, 300);
        }

        async _saveSettings() {
            if (!this.state.sessionId) {
                return true;
            }

            const payload = {
                title: this.elements.titleInput.value.trim() || null,
                template_key: this.state.workspace?.settings?.template_key || 'meeting',
                custom_prompt: this.elements.customPrompt.value.trim() || null,
            };

            const response = await fetch(`/api/recordings/${this.state.sessionId}/settings`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                this._showBanner(error.detail || 'Failed to save settings.', 'error');
                return false;
            }

            await this._loadWorkspace();
            return true;
        }

        async _flushSettingsSave() {
            if (this._settingsSaveTimer) {
                clearTimeout(this._settingsSaveTimer);
                this._settingsSaveTimer = null;
                this._settingsSavePromise = this._saveSettings();
            }
            if (this._settingsSavePromise) {
                const savePromise = this._settingsSavePromise;
                const saved = await savePromise;
                if (this._settingsSavePromise === savePromise) {
                    this._settingsSavePromise = null;
                }
                return saved !== false;
            }
            return true;
        }

        _showBanner(message, tone = 'success') {
            this.state.banner = { message, tone };
            this._renderBanner();
            if (this._bannerTimer) {
                clearTimeout(this._bannerTimer);
            }
            this._bannerTimer = window.setTimeout(() => {
                this.state.banner = null;
                this._renderBanner();
            }, 3200);
        }

        _renderMarkdown(element, markdown) {
            if (typeof marked !== 'undefined') {
                element.innerHTML = marked.parse(markdown || '');
                return;
            }
            element.textContent = markdown || '';
        }

        _formatStage(stage) {
            const map = {
                queued: 'Queued',
                transcribing: 'Transcribing Audio',
                summarizing: 'Generating Summary',
                writing: 'Writing Output',
                completed: 'Completed',
                failed: 'Failed',
                ready: 'Ready',
            };
            return map[stage] || 'Working';
        }

        _formatDuration(seconds) {
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            const remainingSeconds = seconds % 60;
            return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${remainingSeconds.toString().padStart(2, '0')}`;
        }

        _autoResizeTextarea(textarea) {
            textarea.style.height = 'auto';
            textarea.style.height = `${Math.max(textarea.scrollHeight, 120)}px`;
        }

        _escapeHtml(value) {
            const div = document.createElement('div');
            div.textContent = value == null ? '' : String(value);
            return div.innerHTML;
        }

        _sleep(ms) {
            return new Promise((resolve) => window.setTimeout(resolve, ms));
        }
    }

    window.RecordingWorkspace = RecordingWorkspace;
})();
