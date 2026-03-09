(function () {
    const SUMMARY_REVISE_TIMEOUT_MS = 45000;
    const SUMMARY_REVISE_TIMEOUT_MESSAGE = 'AI revision timed out. Please try again.';
    const SUMMARY_REVISE_NETWORK_MESSAGE = 'AI revision failed to reach the server. Please try again.';

    class RecordingWorkspace {
        constructor(options = {}) {
            this.baseOptions = { ...options };
            this.options = { ...options };
            this.templates = {};
            this._settingsSaveTimer = null;
            this._settingsSavePromise = null;
            this._bannerTimer = null;
            this._metaTooltipTimer = null;
            this._bodyScrollLocked = false;
            this._speakerAudio = null;
            this._speakerPlayback = null;
            this._useNativeScrollTimeline = false;
            this._useSimpleMobileTabMotion = this._detectSimpleMobileTabMotion();
            this._tabStageCurrent = 0;
            this._tabStageTarget = 0;
            this._tabStageFrame = null;
            this._tabStageHeight = 0;
            this._tabShellTop = 0;
            this._tabStageLastAppliedOffset = null;
            this._tabShellTopApplied = null;
            this._tabStageActiveUntil = 0;
            this._tabStagePaddingApplied = null;
            this._tabStageTransitionMode = null;
            this._tabStageResizeObserver = null;
            this.state = {
                sessionId: null,
                workspace: null,
                transcriptVersions: [],
                selectedTranscriptVersionId: null,
                activeTab: 'summary',
                jobStatus: null,
                speakerAssignments: {},
                speakerDirty: false,
                speakerEditMode: false,
                promptEditMode: false,
                promptEditValue: '',
                promptEditInitialValue: '',
                settingsViewMode: 'selected',
                editMode: false,
                editBuffer: '',
                showRefineInput: false,
                isRefiningSummary: false,
                summaryHistory: [],
                selectedSavedSummaryId: null,
                banner: null,
                pendingResetSummaryId: null,
                lastCustomPrompt: '',
            };

            this._ensureDom();
            this._bindEvents();
        }

        _detectSimpleMobileTabMotion() {
            if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
                return false;
            }
            return window.matchMedia('(max-width: 700px), (hover: none) and (pointer: coarse)').matches;
        }

        _ensureSpeakerAudio() {
            if (!this._speakerAudio) {
                this._speakerAudio = new Audio();
                this._speakerAudio.preload = 'auto';
            }
            return this._speakerAudio;
        }

        async open(sessionId, options = {}) {
            this.options = { ...this.baseOptions, ...options };
            this.state.sessionId = sessionId;
            this.state.jobStatus = null;
            this.state.transcriptVersions = [];
            this.state.selectedTranscriptVersionId = options.workspaceVersionId || null;
            this.state.speakerAssignments = {};
            this.state.speakerDirty = false;
            this.state.speakerEditMode = false;
            this.state.promptEditMode = false;
            this.state.promptEditValue = '';
            this.state.promptEditInitialValue = '';
            this.state.settingsViewMode = 'selected';
            this.state.editMode = false;
            this.state.editBuffer = '';
            this.state.showRefineInput = false;
            this.state.isRefiningSummary = false;
            this.state.summaryHistory = [];
            this.state.selectedSavedSummaryId = null;
            this.state.banner = null;
            this.state.pendingResetSummaryId = null;
            this.state.lastCustomPrompt = '';
            this.elements.modal.classList.toggle('workspace-native-scroll-timeline', this._useNativeScrollTimeline);
            this.elements.modal.classList.remove('hidden');
            this._lockBodyScroll();

            console.info('[workspace_open:start]', { sessionId });

            try {
                await this._loadTemplates();
                await this._loadWorkspace();
            } catch (error) {
                console.warn('[workspace_open:fail]', {
                    sessionId,
                    message: error?.message || 'Workspace unavailable',
                });
                this._revertOpenState();
                throw error;
            }

            if (this.options.initialTab) {
                this.state.activeTab = this.options.initialTab;
                this._renderTabs();
            }

            this._applySearchFocus();

            if (this.options.autoStartTranscription && !this.state.workspace?.recording?.has_transcription) {
                this._startTranscriptionJob().catch((error) => {
                    this.state.jobStatus = null;
                    this._render();
                    this._showBanner(error?.message || 'Failed to start transcription.', 'error');
                });
            }

            console.info('[workspace_open:ok]', { sessionId });
        }

        async close() {
            if (this.state.isRefiningSummary) {
                return;
            }

            const saved = await this._flushSettingsSave();
            if (saved === false) {
                return;
            }

            this.elements.modal.classList.add('hidden');
            this._unlockBodyScroll();
            this.state.jobStatus = null;
            this.state.workspace = null;
            this.state.sessionId = null;
            this.state.transcriptVersions = [];
            this.state.selectedTranscriptVersionId = null;
            this.state.speakerAssignments = {};
            this.state.speakerDirty = false;
            this.state.speakerEditMode = false;
            this.state.promptEditMode = false;
            this.state.promptEditValue = '';
            this.state.promptEditInitialValue = '';
            this.state.settingsViewMode = 'selected';
            this.state.editMode = false;
            this.state.editBuffer = '';
            this.state.showRefineInput = false;
            this.state.isRefiningSummary = false;
            this.state.summaryHistory = [];
            this.state.selectedSavedSummaryId = null;
            this.state.pendingResetSummaryId = null;
            this.state.lastCustomPrompt = '';
            this._settingsSavePromise = null;
            this._resetTabScrollStage();
            this._stopSpeakerPlayback();
            this.options = { ...this.baseOptions };
            if (typeof this.options.onClose === 'function') {
                this.options.onClose();
            }
        }

        async _loadTemplates() {
            if (Object.keys(this.templates).length > 0) {
                return;
            }

            console.info('[workspace_open:templates:start]');
            const payload = await this._jsonRequest('/api/templates', {}, {
                timeoutMs: 8000,
                retries: 2,
                networkErrorMessage: 'Workspace network request failed',
                httpErrorMessage: 'Failed to load templates',
                logLabel: 'workspace_open:templates',
            });
            this.templates = payload.templates || {};
            console.info('[workspace_open:templates:ok]');
        }

        async _loadWorkspace({ keepTab = true } = {}) {
            if (!this.state.sessionId) {
                return;
            }

            console.info('[workspace_open:data:start]', { sessionId: this.state.sessionId });
            const workspaceUrl = new URL(`/api/recordings/${this.state.sessionId}/workspace`, window.location.origin);
            if (this.state.selectedTranscriptVersionId) {
                workspaceUrl.searchParams.set('transcript_version_id', this.state.selectedTranscriptVersionId);
            }
            const payload = await this._jsonRequest(`${workspaceUrl.pathname}${workspaceUrl.search}`, {}, {
                timeoutMs: 8000,
                retries: 2,
                networkErrorMessage: 'Workspace network request failed',
                httpErrorMessage: 'Failed to load recording workspace',
                logLabel: 'workspace_open:data',
            });
            const previousTab = this.state.activeTab;
            this.state.workspace = payload;
            this.state.transcriptVersions = Array.isArray(payload.transcript_versions)
                ? payload.transcript_versions
                : [];
            this.state.selectedTranscriptVersionId = payload.active_transcript_version?.id || null;
            const draftId = payload.draft_summary?.id || null;
            const savedIds = (payload.saved_summaries || []).map((summary) => summary.id);
            const availableSummaryIds = [draftId, ...savedIds].filter(Boolean);
            if (!availableSummaryIds.includes(this.state.selectedSavedSummaryId)) {
                this.state.selectedSavedSummaryId = draftId || savedIds[0] || null;
            }

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

            if (!this.state.promptEditMode) {
                this.state.promptEditValue = '';
                this.state.promptEditInitialValue = '';
            }

            this._syncLastCustomPrompt();

            this._render();
            console.info('[workspace_open:data:ok]', { sessionId: this.state.sessionId });
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
                        <div class="workspace-top-stack">
                            <div class="modal-header workspace-header">
                                <div class="workspace-header-copy">
                                    <input id="workspace-title-input" class="workspace-title-input" placeholder="Untitled Recording" autocapitalize="words">
                                    <div id="workspace-meta" class="workspace-meta"></div>
                                    <div id="workspace-meta-tooltip" class="workspace-meta-tooltip hidden" aria-live="polite"></div>
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
                        </div>
                        <div class="workspace-tab-shell" id="workspace-tab-shell">
                            <div class="workspace-tab-row" role="tablist" aria-label="Workspace sections">
                                <button type="button" class="workspace-tab active" id="workspace-tab-speakers" data-tab="speakers" role="tab" aria-controls="workspace-panel-speakers" aria-selected="true">Speakers</button>
                                <button type="button" class="workspace-tab" id="workspace-tab-summary" data-tab="summary" role="tab" aria-controls="workspace-panel-summary" aria-selected="false">Summary</button>
                                <button type="button" class="workspace-tab" id="workspace-tab-settings" data-tab="settings" role="tab" aria-controls="workspace-panel-settings" aria-selected="false">Settings</button>
                                <button type="button" class="workspace-tab" id="workspace-tab-transcript" data-tab="transcript" role="tab" aria-controls="workspace-panel-transcript" aria-selected="false">Transcript</button>
                            </div>
                        </div>
                        <div class="modal-body workspace-body">
                            <div id="workspace-scroll-stage" class="workspace-scroll-stage">
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
                                        <h3>Summary Preview</h3>
                                        <div id="workspace-summary-meta" class="summary-meta"></div>
                                    </div>
                                    <div id="workspace-summary-version-row" class="summary-version-row hidden">
                                        <select id="workspace-summary-version-select" class="version-select"></select>
                                    </div>
                                    <div id="workspace-summary-display" class="summary-body"></div>
                                    <div id="workspace-summary-edit-notice" class="workspace-edit-notice hidden">Editing summary. Click Done Editing when you are finished.</div>
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
                                    <div id="workspace-transcript-version-row" class="summary-version-row hidden">
                                        <select id="workspace-transcript-version-select" class="version-select"></select>
                                        <button type="button" class="btn btn-small" id="workspace-retranscribe-btn">Re-transcribe</button>
                                    </div>
                                    <div id="workspace-transcript" class="transcript-view"></div>
                                </section>
                                <section class="workspace-panel hidden" id="workspace-panel-settings" data-panel="settings" role="tabpanel" aria-labelledby="workspace-tab-settings" aria-hidden="true">
                                    <div class="workspace-panel-copy">
                                        <h3>Summary Settings</h3>
                                        <p class="workspace-copy">Template and prompt persist with this recording. Editing a built-in prompt switches this recording to Custom.</p>
                                    </div>
                                    <div class="form-group">
                                        <label class="form-label">Template</label>
                                        <div id="workspace-template-grid" class="template-grid"></div>
                                    </div>
                                    <div class="form-group">
                                        <div class="workspace-summary-actions workspace-settings-reset-actions">
                                            <button type="button" class="btn" id="workspace-prompt-reset-btn">Reset</button>
                                        </div>
                                        <div class="workspace-field-header workspace-prompt-header">
                                            <label class="form-label">Prompt</label>
                                            <button type="button" class="btn btn-small workspace-prompt-edit-btn" id="workspace-prompt-edit-btn">Edit Prompt</button>
                                        </div>
                                        <div id="workspace-prompt-display" class="summary-body workspace-prompt-display"></div>
                                        <div id="workspace-prompt-edit-notice" class="workspace-edit-notice hidden">Editing prompt. Click Done Editing when you are finished.</div>
                                        <textarea id="workspace-custom-prompt" class="form-input form-textarea workspace-prompt-textarea hidden" placeholder="Write custom prompt instructions."></textarea>
                                    </div>
                                </section>
                            </div>
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
                topStack: modal.querySelector('.workspace-top-stack'),
                close: modal.querySelector('#workspace-close'),
                banner: modal.querySelector('#workspace-banner'),
                titleInput: modal.querySelector('#workspace-title-input'),
                meta: modal.querySelector('#workspace-meta'),
                metaTooltip: modal.querySelector('#workspace-meta-tooltip'),
                progress: modal.querySelector('#workspace-progress'),
                progressStage: modal.querySelector('#workspace-progress-stage'),
                progressMessage: modal.querySelector('#workspace-progress-message'),
                progressOverall: modal.querySelector('#workspace-progress-overall'),
                progressTranscriptionText: modal.querySelector('#workspace-progress-transcription-text'),
                progressSummaryText: modal.querySelector('#workspace-progress-summary-text'),
                progressTranscriptionFill: modal.querySelector('#workspace-progress-transcription-fill'),
                progressSummaryFill: modal.querySelector('#workspace-progress-summary-fill'),
                tabShell: modal.querySelector('#workspace-tab-shell'),
                tabRow: modal.querySelector('.workspace-tab-row'),
                tabButtons: Array.from(modal.querySelectorAll('.workspace-tab')),
                body: modal.querySelector('.workspace-body'),
                scrollStage: modal.querySelector('#workspace-scroll-stage'),
                panels: Array.from(modal.querySelectorAll('.workspace-panel')),
                speakersCopy: modal.querySelector('#workspace-speakers-copy'),
                speakersList: modal.querySelector('#workspace-speakers-list'),
                speakerActions: modal.querySelector('#workspace-speaker-actions'),
                speakerEditBtn: modal.querySelector('#workspace-speaker-edit-btn'),
                summaryMeta: modal.querySelector('#workspace-summary-meta'),
                summaryVersionRow: modal.querySelector('#workspace-summary-version-row'),
                summaryVersionSelect: modal.querySelector('#workspace-summary-version-select'),
                transcriptVersionRow: modal.querySelector('#workspace-transcript-version-row'),
                transcriptVersionSelect: modal.querySelector('#workspace-transcript-version-select'),
                retranscribeBtn: modal.querySelector('#workspace-retranscribe-btn'),
                summaryDisplay: modal.querySelector('#workspace-summary-display'),
                summaryEditNotice: modal.querySelector('#workspace-summary-edit-notice'),
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
                promptDisplay: modal.querySelector('#workspace-prompt-display'),
                promptEditNotice: modal.querySelector('#workspace-prompt-edit-notice'),
                customPrompt: modal.querySelector('#workspace-custom-prompt'),
                promptResetBtn: modal.querySelector('#workspace-prompt-reset-btn'),
                promptEditBtn: modal.querySelector('#workspace-prompt-edit-btn'),
                footerStatus: modal.querySelector('#workspace-footer-status'),
                openObsidianBtn: modal.querySelector('#workspace-open-obsidian-btn'),
                secondaryBtn: modal.querySelector('#workspace-secondary-btn'),
                primaryBtn: modal.querySelector('#workspace-primary-btn'),
            };
            this.elements.modal.classList.toggle('workspace-native-scroll-timeline', this._useNativeScrollTimeline);
        }

        _bindEvents() {
            this.elements.close.addEventListener('click', () => this.close());
            this.elements.modal.addEventListener('click', (event) => {
                if (!event.target.closest('.workspace-badge-button')) {
                    this._hideMetaTooltip();
                }
                if (event.target === this.elements.modal) {
                    this.close();
                }
            });

            this.elements.meta.addEventListener('click', (event) => {
                const tooltipButton = event.target.closest('.workspace-badge-button[data-tooltip]');
                if (!tooltipButton) {
                    return;
                }
                event.preventDefault();
                event.stopPropagation();
                const message = tooltipButton.dataset.tooltip || tooltipButton.getAttribute('aria-label') || '';
                if (!message) {
                    return;
                }
                this._toggleMetaTooltip(message);
            });

            this.elements.tabButtons.forEach((button) => {
                button.addEventListener('click', () => {
                    this.state.activeTab = button.dataset.tab;
                    this._renderTabs();
                    this._renderFooter();
                });
            });

            this.elements.body.addEventListener('scroll', () => {
                this._syncTabScrollStage();
            }, { passive: true });

            window.addEventListener('resize', () => {
                this._useSimpleMobileTabMotion = this._detectSimpleMobileTabMotion();
                this._syncTabShellLayout();
                this._syncTabScrollStage({ immediate: true });
            }, { passive: true });

            if (typeof window.ResizeObserver === 'function') {
                this._tabStageResizeObserver = new window.ResizeObserver(() => {
                    this._syncTabShellLayout();
                    this._syncTabScrollStage({ immediate: true });
                });
                if (this.elements.topStack) {
                    this._tabStageResizeObserver.observe(this.elements.topStack);
                }
                if (this.elements.tabRow) {
                    this._tabStageResizeObserver.observe(this.elements.tabRow);
                }
            }

            this.elements.titleInput.addEventListener('input', () => this._queueSettingsSave());
            this.elements.customPrompt.addEventListener('input', () => {
                this._handlePromptInput();
            });
            this.elements.promptResetBtn.addEventListener('click', () => this._resetPromptSettings());
            this.elements.promptEditBtn.addEventListener('click', () => this._togglePromptEditMode());

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
                this._resetSettingsEditSession();
                this._renderHeader();
                this._renderSummary();
                this._renderSettings();
                this._renderFooter();
            });
            this.elements.transcriptVersionSelect.addEventListener('change', (event) => {
                void this._handleTranscriptVersionChange(event.target.value);
            });
            this.elements.retranscribeBtn.addEventListener('click', () => {
                void this._startRetranscription();
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
            const currentSummary = this._currentSummary();

            if (document.activeElement !== this.elements.titleInput) {
                this.elements.titleInput.value = workspace.settings?.title || '';
            }

            const parts = [
                workspace.recording?.recorded_datetime_label,
            ].filter(Boolean);

            const badges = [];
            if (workspace.state?.requires_speaker_review) {
                badges.push('<span class="workspace-badge workspace-badge-warning">Needs speaker review</span>');
            }
            if (currentSummary?.status === 'draft') {
                badges.push('<span class="workspace-badge workspace-badge-accent">Draft</span>');
            } else if (currentSummary?.status === 'saved' || (workspace.saved_summaries || []).length > 0) {
                badges.push('<span class="workspace-badge">Saved</span>');
            }
            if (this._currentSummaryIsOutOfDate()) {
                const staleReason = this._escapeHtml(
                    this._currentSummaryOutOfDateReason() || 'Summary settings changed.'
                );
                badges.push(
                    `<button type="button" class="workspace-badge workspace-badge-warning workspace-badge-button" title="${staleReason}" aria-label="${staleReason}" data-tooltip="${staleReason}">Out of date</button>`
                );
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

        _toggleMetaTooltip(message) {
            if (
                !this.elements.metaTooltip.classList.contains('hidden')
                && this.elements.metaTooltip.textContent === message
            ) {
                this._hideMetaTooltip();
                return;
            }
            this._showMetaTooltip(message);
        }

        _showMetaTooltip(message) {
            if (this._metaTooltipTimer) {
                clearTimeout(this._metaTooltipTimer);
            }
            this.elements.metaTooltip.textContent = message;
            this.elements.metaTooltip.classList.remove('hidden');
            this._metaTooltipTimer = window.setTimeout(() => {
                this._hideMetaTooltip();
            }, 2800);
        }

        _hideMetaTooltip() {
            if (this._metaTooltipTimer) {
                clearTimeout(this._metaTooltipTimer);
                this._metaTooltipTimer = null;
            }
            this.elements.metaTooltip.textContent = '';
            this.elements.metaTooltip.classList.add('hidden');
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
            this._syncTabScrollStage({ immediate: true });
        }

        _syncTabScrollStage({ immediate = false } = {}) {
            const body = this.elements.body;
            const topStack = this.elements.topStack;
            const tabShell = this.elements.tabShell;
            const tabRow = this.elements.tabRow;
            const scrollStage = this.elements.scrollStage;
            if (!body || !topStack || !tabShell || !tabRow || !scrollStage) {
                return;
            }

            if (!this._tabStageHeight) {
                this._syncTabShellLayout(topStack, tabShell, scrollStage);
            }

            if (this._useNativeScrollTimeline) {
                tabShell.style.transform = '';
                tabShell.style.transition = '';
                return;
            }

            if (immediate) {
                this._tabStageCurrent = this._computeTabStageOffset();
                this._applyTabScrollStage(this._tabStageCurrent);
                return;
            }

            this._tabStageActiveUntil = performance.now() + 140;
            if (this._tabStageFrame) {
                return;
            }

            this._tabStageFrame = window.requestAnimationFrame(() => this._animateTabScrollStage());
        }

        _syncTabShellLayout(topStack = this.elements.topStack, tabShell = this.elements.tabShell, scrollStage = this.elements.scrollStage, tabHeight = null) {
            if (!topStack || !tabShell || !scrollStage) {
                return;
            }

            const resolvedTabHeight = tabHeight ?? Math.ceil(this.elements.tabRow?.offsetHeight || this.elements.tabRow?.getBoundingClientRect().height || 0);
            this._tabStageHeight = resolvedTabHeight;
            this._tabShellTop = Math.ceil(topStack.getBoundingClientRect().height);
            if (this._tabShellTopApplied !== this._tabShellTop) {
                tabShell.style.top = `${this._tabShellTop}px`;
                this._tabShellTopApplied = this._tabShellTop;
            }
            if (tabShell.style.height !== `${resolvedTabHeight}px`) {
                tabShell.style.height = `${resolvedTabHeight}px`;
            }
            tabShell.style.setProperty('--workspace-tab-height', `${resolvedTabHeight}px`);
            if (this._tabStagePaddingApplied !== resolvedTabHeight) {
                scrollStage.style.paddingTop = `${resolvedTabHeight}px`;
                this._tabStagePaddingApplied = resolvedTabHeight;
            }
        }

        _computeTabStageOffset() {
            const scrollTop = this.elements.body?.scrollTop || 0;
            return Math.min(scrollTop, this._tabStageHeight || 0);
        }

        _animateTabScrollStage() {
            this._tabStageFrame = null;
            this._tabStageTarget = this._computeTabStageOffset();
            this._tabStageCurrent = this._tabStageTarget;
            this._applyTabScrollStage(this._tabStageCurrent);

            if (performance.now() < this._tabStageActiveUntil) {
                this._tabStageFrame = window.requestAnimationFrame(() => this._animateTabScrollStage());
            }
        }

        _applyTabScrollStage(offset) {
            const tabShell = this.elements.tabShell;
            const tabRow = this.elements.tabRow;
            if (!tabRow || !tabShell) {
                return;
            }

            if (this._useNativeScrollTimeline) {
                tabShell.style.transform = '';
                tabShell.style.transition = '';
                tabRow.style.transform = '';
                return;
            }

            let visualOffset = offset;
            const softenZone = Math.min(28, this._tabStageHeight || 28);
            visualOffset = Math.round(visualOffset * 100) / 100;
            if (this._tabShellTopApplied !== this._tabShellTop) {
                tabShell.style.top = `${this._tabShellTop}px`;
                this._tabShellTopApplied = this._tabShellTop;
            }
            const transitionMode = this._useSimpleMobileTabMotion
                ? 'none'
                : (visualOffset > 0 && visualOffset < softenZone ? 'soft' : 'track');
            if (this._tabStageTransitionMode !== transitionMode) {
                if (transitionMode === 'soft') {
                    tabShell.style.transition = 'transform 260ms cubic-bezier(0.22, 1, 0.36, 1)';
                } else if (transitionMode === 'track') {
                    tabShell.style.transition = 'transform 110ms cubic-bezier(0.22, 0.78, 0.22, 1)';
                } else {
                    tabShell.style.transition = 'none';
                }
                this._tabStageTransitionMode = transitionMode;
            }
            if (this._tabStageLastAppliedOffset !== visualOffset) {
                tabShell.style.transform = visualOffset > 0 ? `translate3d(0, ${-visualOffset}px, 0)` : '';
                this._tabStageLastAppliedOffset = visualOffset;
            }
            tabRow.style.transform = '';
        }

        _resetTabScrollStage() {
            if (this._tabStageFrame) {
                window.cancelAnimationFrame(this._tabStageFrame);
                this._tabStageFrame = null;
            }
            this._tabStageCurrent = 0;
            this._tabStageTarget = 0;
            this._tabStageHeight = 0;
            this._tabShellTop = 0;
            this._tabStageLastAppliedOffset = null;
            this._tabShellTopApplied = null;
            this._tabStageActiveUntil = 0;
            this._tabStagePaddingApplied = null;
            this._tabStageTransitionMode = null;
            this._applyTabScrollStage(0);
        }

        _renderSpeakers() {
            const workspace = this.state.workspace;
            const speakers = workspace?.speaker_review?.speakers || [];
            const inputsLocked = this._speakerInputsLocked();
            this._stopSpeakerPlayback();

            if (!workspace?.recording?.has_transcription) {
                this.elements.speakersCopy.textContent = this.options.transcriptionStartMode === 'manual'
                    ? 'Recording uploaded. Click Transcribe Recording when you are ready.'
                    : 'Transcription will start automatically once the recording is ready.';
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
                                <button
                                    type="button"
                                    class="btn btn-small speaker-audio-btn"
                                    data-speaker-cluster="${this._escapeHtml(speaker.speaker_cluster)}"
                                    data-clip-url="${this._escapeHtml(speaker.clip_url || '')}"
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
                    const clipUrl = this._resolveApiMediaUrl(button.dataset.clipUrl || '');
                    const isCurrent = this._speakerPlayback?.button === button
                        && this._speakerPlayback?.clipUrl === clipUrl;
                    if (isCurrent) {
                        this._stopSpeakerPlayback();
                        return;
                    }
                    this._playSpeakerClip({
                        button,
                        clipUrl,
                        speakerCluster: button.dataset.speakerCluster || '',
                    });
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

        async _playSpeakerClip({ button, clipUrl, speakerCluster }) {
            if (!clipUrl) {
                this._showBanner('Unable to play speaker clip.', 'error');
                return;
            }

            this._stopSpeakerPlayback();
            const audio = this._ensureSpeakerAudio();
            const handleEnded = () => {
                this._stopSpeakerPlayback();
            };
            const handleError = () => {
                console.warn('[speaker_clip:play:fail]', {
                    sessionId: this.state.sessionId,
                    speakerCluster,
                    url: clipUrl,
                    mediaError: audio.error?.message || audio.error?.code || 'unknown',
                });
                this._stopSpeakerPlayback();
                this._showBanner('Unable to play speaker clip.', 'error');
            };

            this._speakerPlayback = {
                audio,
                button,
                clipUrl,
                speakerCluster,
                handleEnded,
                handleError,
            };
            button.disabled = true;
            button.textContent = 'Loading...';
            audio.addEventListener('ended', handleEnded);
            audio.addEventListener('error', handleError);

            try {
                console.info('[speaker_clip:play:start]', {
                    sessionId: this.state.sessionId,
                    speakerCluster,
                    url: clipUrl,
                });
                audio.pause();
                if (audio.src !== clipUrl) {
                    audio.src = clipUrl;
                }
                audio.currentTime = 0;
                audio.load();
                await audio.play();
                if (this._speakerPlayback?.button === button) {
                    button.disabled = false;
                    button.textContent = 'Stop Clip';
                }
                console.info('[speaker_clip:play:ok]', {
                    sessionId: this.state.sessionId,
                    speakerCluster,
                    url: clipUrl,
                });
            } catch (error) {
                const aborted = error?.name === 'AbortError';
                this._stopSpeakerPlayback();
                if (!aborted) {
                    console.warn('[speaker_clip:play:fail]', {
                        sessionId: this.state.sessionId,
                        speakerCluster,
                        url: clipUrl,
                        message: error?.message || 'Unable to play clip',
                    });
                    this._showBanner('Unable to play speaker clip.', 'error');
                }
            }
        }

        _stopSpeakerPlayback() {
            if (!this._speakerPlayback) {
                return;
            }

            const { audio, button, handleEnded, handleError } = this._speakerPlayback;
            audio.removeEventListener('ended', handleEnded);
            audio.removeEventListener('error', handleError);
            if (!audio.paused) {
                audio.pause();
            }
            if (button?.isConnected) {
                button.disabled = false;
                button.textContent = 'Play Clip';
            }
            this._speakerPlayback = null;
        }

        _renderSummary() {
            const workspace = this.state.workspace;
            const summary = this._currentSummary();
            const refineLocked = this.state.isRefiningSummary;

            this.elements.summaryVersionRow.classList.toggle(
                'hidden',
                !workspace || (!workspace.draft_summary && (workspace.saved_summaries || []).length <= 1)
            );
            this.elements.summaryVersionSelect.disabled = refineLocked;

            if (workspace?.draft_summary || (workspace?.saved_summaries || []).length > 1) {
                const totalSavedVersions = workspace.saved_summaries.length;
                const options = [];
                if (workspace.draft_summary) {
                    const selected = workspace.draft_summary.id === this.state.selectedSavedSummaryId ? 'selected' : '';
                    options.push(
                        `<option value="${workspace.draft_summary.id}" ${selected}>v${totalSavedVersions + 1} (Draft)</option>`
                    );
                }
                options.push(...workspace.saved_summaries.map((saved, index) => {
                        const selected = saved.id === this.state.selectedSavedSummaryId ? 'selected' : '';
                        const label = saved.saved_to_obsidian_at
                            ? (index === 0 ? `v${totalSavedVersions} (Latest)` : `v${totalSavedVersions - index}`)
                            : `Version ${index + 1}`;
                        return `<option value="${saved.id}" ${selected}>${label}</option>`;
                    }));
                this.elements.summaryVersionSelect.innerHTML = options.join('');
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
            if (this._currentSummaryIsOutOfDate()) {
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
                this.elements.undoBtn.disabled = true;
                this.elements.refineSection.classList.add('hidden');
                return;
            }

            this.elements.editBtn.disabled = refineLocked;
            this.elements.reviseBtn.disabled = refineLocked;
            this.elements.undoBtn.classList.toggle('hidden', this.state.summaryHistory.length === 0);
            this.elements.undoBtn.disabled = refineLocked || this.state.summaryHistory.length === 0;
            this.elements.editBtn.textContent = this.state.editMode ? 'Done Editing' : 'Edit';
            this.elements.editBtn.classList.toggle('workspace-done-btn', this.state.editMode);
            this.elements.summaryEditNotice.classList.toggle('hidden', !this.state.editMode);
            this.elements.summaryEdit.classList.toggle('workspace-editor-active', this.state.editMode);
            this.elements.summaryDisplay.classList.toggle('workspace-editor-active', this.state.editMode);

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
            this.elements.refineCancel.disabled = refineLocked;
            this.elements.refineSubmit.disabled = refineLocked;
            this.elements.refineSubmit.textContent = refineLocked ? 'Revising...' : 'Revise';
        }

        _renderTranscript() {
            const workspace = this.state.workspace;
            const transcript = this.state.workspace?.transcript || [];
            const transcriptVersions = Array.isArray(this.state.transcriptVersions)
                ? this.state.transcriptVersions
                : [];
            const showTranscriptControls = Boolean(
                workspace?.debug_retranscribe_enabled
                && transcriptVersions.length > 0
            );

            this.elements.transcriptVersionRow.classList.toggle('hidden', !showTranscriptControls);
            if (showTranscriptControls) {
                const options = transcriptVersions.map((version) => {
                    const selected = version.id === this.state.selectedTranscriptVersionId ? 'selected' : '';
                    let label = version.label || `v${version.version_number}`;
                    if (version.status === 'processing') {
                        label += ' (Processing)';
                    } else if (version.is_latest) {
                        label += ' (Latest)';
                    }
                    return `<option value="${this._escapeHtml(version.id)}" ${selected}>${this._escapeHtml(label)}</option>`;
                });
                this.elements.transcriptVersionSelect.innerHTML = options.join('');
                this.elements.transcriptVersionSelect.disabled = Boolean(this.state.jobStatus);
                this.elements.retranscribeBtn.disabled = Boolean(this.state.jobStatus);
                this.elements.retranscribeBtn.classList.toggle(
                    'hidden',
                    !workspace?.recording?.has_audio || !workspace?.recording?.has_transcription
                );
            } else {
                this.elements.transcriptVersionSelect.innerHTML = '';
                this.elements.retranscribeBtn.classList.add('hidden');
            }

            if (!transcript.length) {
                this.elements.transcript.innerHTML = '<div class="workspace-empty">Transcript not available yet.</div>';
                this.elements.transcript.style.removeProperty('--transcript-time-width');
                this.elements.transcript.style.removeProperty('--transcript-speaker-width');
                return;
            }

            this.elements.transcript.style.setProperty(
                '--transcript-time-width',
                `${this._measureTranscriptTimestampWidth(transcript)}px`
            );
            this.elements.transcript.style.setProperty(
                '--transcript-speaker-width',
                `${this._measureTranscriptSpeakerWidth(transcript)}px`
            );

            this.elements.transcript.innerHTML = transcript
                .map((segment) => {
                    const importantClass = segment.is_important ? ' important' : '';
                    const speaker = segment.speaker || '?';
                    const speakerStyle = speaker === '?'
                        ? ''
                        : ` style="--speaker-color: ${this._speakerColorForLabel(speaker)}"`;
                    return `
                        <div class="transcript-segment${importantClass}" data-segment-id="${this._escapeHtml(segment.id)}">
                            <span class="timestamp">${this._escapeHtml(segment.timestamp)}</span>
                            <span class="speaker-inline${speaker === '?' ? ' speaker-inline-unknown' : ''}"${speakerStyle}>[${this._escapeHtml(speaker)}]:</span>
                            <span class="transcript-line-text">${this._escapeHtml(segment.text)}</span>
                        </div>
                    `;
                })
                .join('');
        }

        async _handleTranscriptVersionChange(versionId) {
            if (!versionId || versionId === this.state.selectedTranscriptVersionId || this.state.jobStatus) {
                return;
            }
            this.state.selectedTranscriptVersionId = versionId;
            await this._loadWorkspace();
            this._render();
        }

        async _startRetranscription() {
            if (!this.state.sessionId || !this.state.workspace?.debug_retranscribe_enabled || this.state.jobStatus) {
                return;
            }
            const confirmed = window.confirm(
                'Create a new transcript version from this recording audio? Older transcript and summary versions will be preserved.'
            );
            if (!confirmed) {
                return;
            }
            await this._startTranscriptionJob({
                mode: 'retranscribe',
                sourceTranscriptVersionId: this.state.selectedTranscriptVersionId,
            });
        }

        _applySearchFocus() {
            const highlightSegmentIds = Array.isArray(this.options.highlightSegmentIds)
                ? this.options.highlightSegmentIds.filter(Boolean)
                : [];
            if (!highlightSegmentIds.length || this.state.activeTab !== 'transcript') {
                return;
            }

            window.requestAnimationFrame(() => {
                const nodes = [];
                highlightSegmentIds.forEach((segmentId) => {
                    const node = this.elements.transcript.querySelector(
                        `.transcript-segment[data-segment-id="${CSS.escape(String(segmentId))}"]`
                    );
                    if (!node) {
                        return;
                    }
                    node.classList.add('transcript-segment-highlight');
                    nodes.push(node);
                });

                if (nodes.length > 0) {
                    nodes[0].scrollIntoView({ behavior: 'smooth', block: 'center' });
                }

                window.setTimeout(() => {
                    nodes.forEach((node) => node.classList.remove('transcript-segment-highlight'));
                }, 3200);
            });
        }

        _renderSettings() {
            const workspace = this.state.workspace;
            const settings = this._settingsFormValues();
            const selectedTemplate = this._normalizeTemplateKey(settings.template_key);
            const order = ['meeting', 'strategic_review', 'working_session', 'custom'];

            this._syncTemplateGrid(order, selectedTemplate);

            const promptText = this.state.promptEditMode
                ? this.state.promptEditValue
                : this._currentPromptDisplayText();

            this.elements.promptDisplay.classList.toggle('hidden', this.state.promptEditMode);
            this.elements.customPrompt.classList.toggle('hidden', !this.state.promptEditMode);
            this.elements.promptResetBtn.disabled = !this._canResetSummarySettings();
            this.elements.promptEditBtn.textContent = this.state.promptEditMode ? 'Done Editing' : 'Edit Prompt';
            this.elements.promptEditBtn.classList.toggle('workspace-done-btn', this.state.promptEditMode);
            this.elements.promptEditNotice.classList.toggle('hidden', !this.state.promptEditMode);
            this.elements.customPrompt.classList.toggle('workspace-editor-active', this.state.promptEditMode);
            this.elements.promptDisplay.classList.toggle('workspace-editor-active', this.state.promptEditMode);

            if (this.state.promptEditMode) {
                if (document.activeElement !== this.elements.customPrompt) {
                    this.elements.customPrompt.value = this.state.promptEditValue;
                }
                this._autoResizeTextarea(this.elements.customPrompt);
                return;
            }

            this._renderMarkdown(this.elements.promptDisplay, promptText);
        }

        _syncTemplateGrid(order, selectedTemplate) {
            const templateKeys = order.filter((key) => this.templates[key]);
            const existingButtons = Array.from(this.elements.templateGrid.querySelectorAll('.template-btn'));
            const existingKeys = existingButtons.map((button) => button.dataset.template);
            const needsRebuild = existingKeys.length !== templateKeys.length
                || existingKeys.some((key, index) => key !== templateKeys[index]);

            if (needsRebuild) {
                this.elements.templateGrid.innerHTML = templateKeys
                    .map((key) => {
                        const template = this.templates[key];
                        const selectedClass = key === selectedTemplate ? ' selected' : '';
                        const pressed = key === selectedTemplate ? 'true' : 'false';
                        return `<button type="button" class="template-btn${selectedClass}" data-template="${key}" aria-pressed="${pressed}">${this._escapeHtml(template.name)}</button>`;
                    })
                    .join('');
            }

            Array.from(this.elements.templateGrid.querySelectorAll('.template-btn')).forEach((button) => {
                const selected = button.dataset.template === selectedTemplate;
                button.classList.toggle('selected', selected);
                button.setAttribute('aria-pressed', selected ? 'true' : 'false');
            });
        }

        _renderFooter() {
            const workspace = this.state.workspace;
            const currentSummary = this._currentSummary();
            const primaryAction = this._getPrimaryAction();
            const refining = this.state.isRefiningSummary;

            this.elements.footerStatus.textContent = this._footerStatusText();
            this.elements.primaryBtn.textContent = refining && primaryAction.action === 'save_draft'
                ? 'Revising...'
                : primaryAction.label;
            this.elements.primaryBtn.disabled = refining || !!primaryAction.disabled;
            this.elements.secondaryBtn.disabled = refining;
            this.elements.close.disabled = refining;

            const showOpenButton = Boolean(workspace?.obsidian?.open_uri)
                && currentSummary?.status === 'saved'
                && !this._currentSummaryIsOutOfDate()
                && primaryAction.action !== 'open_obsidian';
            this.elements.openObsidianBtn.classList.toggle('hidden', !showOpenButton);
        }

        _footerStatusText() {
            const workspace = this.state.workspace;
            const currentSummary = this._currentSummary();
            if (!workspace) {
                return 'Loading workspace...';
            }
            if (this.state.isRefiningSummary) {
                return 'Revising summary...';
            }
            if (this.state.jobStatus) {
                return this.state.jobStatus.message || 'Working...';
            }
            if (workspace.state?.requires_speaker_review) {
                return 'Review speakers now, or continue with Attendee labels for anyone still unresolved.';
            }
            if (currentSummary?.status === 'draft') {
                return 'Current draft has not been saved to Obsidian.';
            }
            if (this._currentSummaryIsOutOfDate()) {
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
            const currentSummary = this._currentSummary();
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
            if (this._currentSummaryIsOutOfDate()) {
                return { label: 'Re-summarize', action: 'summarize' };
            }
            if (currentSummary?.status === 'draft') {
                return { label: 'Save to Obsidian', action: 'save_draft' };
            }
            if (!currentSummary && workspace.state?.can_generate_summary) {
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

        async _startTranscriptionJob(options = {}) {
            if (!this.state.sessionId || this.state.jobStatus?.kind === 'transcription') {
                return;
            }
            const mode = options.mode || 'initial';
            const sourceTranscriptVersionId = options.sourceTranscriptVersionId || null;
            this.state.jobStatus = {
                kind: 'transcription',
                status: 'queued',
                stage: 'queued',
                message: 'Starting transcription',
                transcription_progress: 0,
                summarization_progress: 0,
                overall_progress: 0,
            };
            this._renderProgress();
            this._renderFooter();

            try {
                const saved = await this._flushSettingsSave();
                if (saved === false) {
                    this.state.jobStatus = null;
                    this._render();
                    return;
                }

                console.info('[workspace_open:auto_transcription:start]', { sessionId: this.state.sessionId });
                const response = await window.SidekickNetwork.request(`/api/recordings/${this.state.sessionId}/transcription-job`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        mode,
                        source_transcript_version_id: sourceTranscriptVersionId,
                    }),
                }, {
                    timeoutMs: 10000,
                    retries: 1,
                    networkErrorMessage: 'Workspace network request failed',
                    logLabel: 'workspace_open:auto_transcription',
                });
                if (!response.ok) {
                    const error = await response.json().catch(() => ({}));
                    this.state.jobStatus = null;
                    this._render();
                    this._showBanner(error.detail || 'Failed to start transcription.', 'error');
                    return;
                }

                const payload = await response.json();
                this.state.jobStatus = {
                    ...this.state.jobStatus,
                    job_id: payload.job_id,
                    status: payload.status || 'queued',
                };
                this._renderProgress();
                this._renderFooter();
                await this._pollJob('transcription', payload.job_id, `/api/transcription-jobs/${payload.job_id}`);
            } catch (error) {
                this.state.jobStatus = null;
                this._render();
                this._showBanner(error?.message || 'Failed to start transcription.', 'error');
            }
        }

        async _startSummaryJob() {
            if (!this.state.sessionId || this.state.jobStatus?.kind === 'summary') {
                return;
            }

            try {
                this.state.jobStatus = {
                    kind: 'summary',
                    status: 'queued',
                    stage: 'queued',
                    message: 'Starting summary generation',
                    transcription_progress: 1,
                    summarization_progress: 0,
                    overall_progress: 0,
                };
                this._renderProgress();
                this._renderFooter();

                const saved = await this._flushSettingsSave();
                if (saved === false) {
                    this.state.jobStatus = null;
                    this._render();
                    return;
                }
                if (this.state.speakerDirty) {
                    await this._saveSpeakerAssignments();
                }
            } catch (error) {
                this.state.jobStatus = null;
                this._render();
                this._showBanner(error.message || 'Failed to save workspace changes.', 'error');
                return;
            }

            const response = await window.SidekickNetwork.request(`/api/recordings/${this.state.sessionId}/summary-job`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    transcript_version_id: this.state.selectedTranscriptVersionId,
                }),
            }, {
                timeoutMs: 10000,
                retries: 1,
                networkErrorMessage: 'Workspace network request failed',
                logLabel: 'workspace_summary:start',
            });
            if (!response.ok) {
                const error = await response.json().catch(() => ({}));
                this.state.jobStatus = null;
                this._render();
                this._showBanner(error.detail || 'Failed to start summary generation.', 'error');
                return;
            }

            const payload = await response.json();
            this.state.jobStatus = {
                ...this.state.jobStatus,
                job_id: payload.job_id,
                status: payload.status || 'queued',
            };
            this._renderProgress();
            this._renderFooter();
            await this._pollJob('summary', payload.job_id, `/api/summary-jobs/${payload.job_id}`);
        }

        async _pollJob(kind, jobId, url) {
            while (this.state.sessionId) {
                try {
                    const job = await this._jsonRequest(url, {}, {
                        timeoutMs: 10000,
                        retries: 1,
                        networkErrorMessage: 'Lost connection while checking job status',
                        httpErrorMessage: 'Failed to read job status.',
                        logLabel: `job_poll:${kind}`,
                    });
                    this.state.jobStatus = {
                        kind,
                        ...job,
                    };
                    this._renderProgress();
                    this._renderFooter();

                    if (job.status === 'completed') {
                        this.state.jobStatus = null;
                        if (kind === 'transcription' && job.transcript_version_id) {
                            this.state.selectedTranscriptVersionId = job.transcript_version_id;
                        }
                        await this._loadWorkspace({ keepTab: false });
                        if (kind === 'summary' && this.state.workspace?.draft_summary?.id) {
                            this.state.selectedSavedSummaryId = this.state.workspace.draft_summary.id;
                            this._resetSettingsEditSession();
                        }
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
                } catch (error) {
                    this.state.jobStatus = null;
                    this._render();
                    console.warn(`[job_poll:${kind}:fail]`, {
                        sessionId: this.state.sessionId,
                        jobId,
                        message: error?.message || 'Failed to read job status.',
                    });
                    this._showBanner(error?.message || 'Failed to read job status.', 'error');
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

            await this._jsonRequest(`/api/recordings/${this.state.sessionId}/speakers`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    assignments,
                    transcript_version_id: this.state.selectedTranscriptVersionId,
                }),
            }, {
                timeoutMs: 10000,
                retries: 1,
                networkErrorMessage: 'Workspace network request failed',
                httpErrorMessage: 'Failed to save speaker assignments',
                logLabel: 'workspace_speakers:save',
            });

            this.state.speakerDirty = false;
            await this._loadWorkspace();
        }

        _showRefineInput() {
            if (this.state.isRefiningSummary) {
                return;
            }
            if (this.state.workspace?.draft_summary?.id) {
                this.state.selectedSavedSummaryId = this.state.workspace.draft_summary.id;
            }
            this.state.showRefineInput = true;
            this._renderSummary();
            this.elements.refineInput.value = '';
            this.elements.refineInput.focus();
        }

        _hideRefineInput() {
            if (this.state.isRefiningSummary) {
                return;
            }
            this.state.showRefineInput = false;
            this._renderSummary();
        }

        async _submitRefine() {
            if (this.state.isRefiningSummary) {
                return;
            }

            const instruction = this.elements.refineInput.value.trim();
            if (!instruction) {
                this.elements.refineInput.focus();
                return;
            }

            const draftId = await this._ensureDraft('ai_revised');
            this.state.selectedSavedSummaryId = draftId;
            const currentSummary = this._currentSummary();
            if (currentSummary?.content) {
                this.state.summaryHistory.push(currentSummary.content);
            }

            this.state.isRefiningSummary = true;
            this._renderSummary();
            this._renderFooter();
            try {
                await this._jsonRequest(`/api/summary-drafts/${draftId}/revise`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ instruction }),
                }, {
                    timeoutMs: SUMMARY_REVISE_TIMEOUT_MS,
                    retries: 0,
                    retryOnNetworkError: false,
                    networkErrorMessage: SUMMARY_REVISE_NETWORK_MESSAGE,
                    httpErrorMessage: 'Revision failed',
                    logLabel: 'workspace_summary:revise',
                });
                this.state.showRefineInput = false;
                await this._loadWorkspace();
                if (this.state.workspace?.draft_summary?.id) {
                    this.state.selectedSavedSummaryId = this.state.workspace.draft_summary.id;
                }
                this.state.activeTab = 'summary';
                this._render();
            } catch (error) {
                let message = error?.message || 'Revision failed';
                if (message === 'Network request timed out') {
                    message = SUMMARY_REVISE_TIMEOUT_MESSAGE;
                } else if (message === 'Network request failed' || message === 'Workspace network request failed') {
                    message = SUMMARY_REVISE_NETWORK_MESSAGE;
                }
                this._showBanner(message, 'error');
            } finally {
                this.state.isRefiningSummary = false;
                this._renderSummary();
                this._renderFooter();
            }
        }

        async _toggleEditMode() {
            if (this.state.isRefiningSummary) {
                return;
            }
            if (!this.state.editMode) {
                let draftId;
                try {
                    draftId = await this._ensureDraft('manual_edit');
                } catch (error) {
                    this._showBanner(error.message || 'Failed to create summary draft.', 'error');
                    return;
                }
                this.state.selectedSavedSummaryId = draftId;
                this.state.editMode = true;
                this.state.editBuffer = this._currentSummary()?.content || '';
                this._renderSummary();
                if (this._detectSimpleMobileTabMotion()) {
                    this._bringEditorIntoView(this.elements.summaryEdit);
                } else {
                    this.elements.summaryEdit.focus();
                }
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
                try {
                    await this._jsonRequest(`/api/summary-drafts/${draft.id}`, {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ content: nextContent }),
                    }, {
                        timeoutMs: 10000,
                        retries: 1,
                        networkErrorMessage: 'Workspace network request failed',
                        httpErrorMessage: 'Failed to save manual edits.',
                        logLabel: 'workspace_summary:manual_edit',
                    });
                } catch (error) {
                    this._showBanner(error.message || 'Failed to save manual edits.', 'error');
                    return;
                }
                await this._loadWorkspace();
                if (this.state.workspace?.draft_summary?.id) {
                    this.state.selectedSavedSummaryId = this.state.workspace.draft_summary.id;
                }
            }

            this.state.editMode = false;
            this.state.editBuffer = '';
            this._render();
        }

        async _undoSummaryChange() {
            if (this.state.isRefiningSummary) {
                return;
            }
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
            try {
                await this._jsonRequest(`/api/summary-drafts/${draftId}`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ content: previous }),
                }, {
                    timeoutMs: 10000,
                    retries: 1,
                    networkErrorMessage: 'Workspace network request failed',
                    httpErrorMessage: 'Failed to undo summary change.',
                    logLabel: 'workspace_summary:undo',
                });
            } catch (error) {
                this._showBanner(error.message || 'Failed to undo summary change.', 'error');
                return;
            }
            await this._loadWorkspace();
            if (this.state.workspace?.draft_summary?.id) {
                this.state.selectedSavedSummaryId = this.state.workspace.draft_summary.id;
            }
            this._render();
        }

        async _ensureDraft(sourceType) {
            if (this.state.workspace?.draft_summary?.id) {
                return this.state.workspace.draft_summary.id;
            }

            const payload = await this._jsonRequest(`/api/recordings/${this.state.sessionId}/summary-draft`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    source_type: sourceType,
                    source_summary_id: null,
                    transcript_version_id: this.state.selectedTranscriptVersionId,
                }),
            }, {
                timeoutMs: 10000,
                retries: 1,
                networkErrorMessage: 'Workspace network request failed',
                httpErrorMessage: 'Failed to create summary draft',
                logLabel: 'workspace_summary:create_draft',
            });
            await this._loadWorkspace();
            this.state.selectedSavedSummaryId = payload.draft_summary_id;
            return payload.draft_summary_id;
        }

        async _saveDraft() {
            if (this.state.isRefiningSummary) {
                return;
            }
            const draft = this.state.workspace?.draft_summary;
            if (!draft) {
                return;
            }

            await this._flushSettingsSave();
            this.elements.primaryBtn.disabled = true;
            this.elements.primaryBtn.textContent = 'Saving...';
            try {
                await this._jsonRequest(`/api/summary-drafts/${draft.id}/save`, {
                    method: 'POST',
                }, {
                    timeoutMs: 10000,
                    retries: 1,
                    networkErrorMessage: 'Workspace network request failed',
                    httpErrorMessage: 'Failed to save draft',
                    logLabel: 'workspace_summary:save_draft',
                });
                await this._loadWorkspace();
                this.state.selectedSavedSummaryId = this.state.workspace?.saved_summaries?.[0]?.id || null;
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
            if (workspace.draft_summary?.id === this.state.selectedSavedSummaryId) {
                return workspace.draft_summary;
            }
            const savedSummary = (workspace.saved_summaries || []).find(
                (summary) => summary.id === this.state.selectedSavedSummaryId
            );
            if (savedSummary) {
                return savedSummary;
            }
            return workspace.draft_summary || workspace.saved_summaries?.[0] || null;
        }

        _currentSummaryIsOutOfDate() {
            return Boolean(this._currentSummaryOutOfDateReason());
        }

        _currentSummaryOutOfDateReason() {
            const localReason = this._localSummaryOutOfDateReason();
            if (localReason) {
                return localReason;
            }
            return this._selectedSummaryServerOutOfDateReason();
        }

        _localSummaryOutOfDateReason() {
            if (this.state.settingsViewMode !== 'workspace') {
                return null;
            }
            const currentSummary = this._currentSummary();
            const currentSettings = this.state.workspace?.settings;
            if (!currentSummary || !currentSettings) {
                return null;
            }

            const currentTemplateKey = this._normalizeTemplateKey(currentSettings.template_key);
            const summaryTemplateKey = this._normalizeTemplateKey(currentSummary.template_key);
            if (currentTemplateKey !== summaryTemplateKey) {
                return 'Summary settings changed to a different template.';
            }

            const currentPrompt = this._normalizeOptionalText(currentSettings.custom_prompt);
            const summaryPrompt = this._normalizeOptionalText(currentSummary.custom_prompt);
            if (currentPrompt !== summaryPrompt) {
                return 'Summary prompt settings changed after this summary was generated.';
            }

            return null;
        }

        _selectedSummaryServerOutOfDateReason() {
            const currentSummary = this._currentSummary();
            const staleReason = currentSummary?.summary_out_of_date_reason
                || this.state.workspace?.state?.summary_out_of_date_reason
                || null;
            if (!staleReason) {
                return null;
            }
            if (staleReason.startsWith('Speaker assignments changed')) {
                return staleReason;
            }
            return null;
        }

        _selectTemplate(templateKey) {
            this._beginSettingsEditSession();
            const current = this.state.workspace?.settings || {};
            const nextSettings = {
                ...current,
                template_key: templateKey,
            };

            this.state.promptEditMode = false;
            this.state.promptEditValue = '';
            this.state.promptEditInitialValue = '';

            if (templateKey === 'custom') {
                nextSettings.custom_prompt = this._preferredCustomPrompt();
            } else if (current.template_key !== templateKey) {
                this._syncLastCustomPrompt();
                nextSettings.custom_prompt = null;
            }

            this.state.workspace.settings = {
                ...nextSettings,
            };
            this._renderSettings();
            this._renderHeader();
            this._renderSummary();
            this._renderFooter();
            this._queueSettingsSave();
        }

        _togglePromptEditMode() {
            if (!this.state.workspace) {
                return;
            }

            if (!this.state.promptEditMode) {
                this._beginSettingsEditSession();
                const promptText = this._currentPromptDisplayText();
                this.state.promptEditMode = true;
                this.state.promptEditInitialValue = promptText;
                this.state.promptEditValue = promptText;
                this._renderSettings();
                if (this._detectSimpleMobileTabMotion()) {
                    this._bringEditorIntoView(this.elements.customPrompt);
                } else {
                    this.elements.customPrompt.focus();
                    this.elements.customPrompt.setSelectionRange(
                        this.elements.customPrompt.value.length,
                        this.elements.customPrompt.value.length
                    );
                }
                return;
            }

            this.state.promptEditMode = false;
            this.state.promptEditValue = '';
            this.state.promptEditInitialValue = '';
            this._renderSettings();
            this._renderFooter();
        }

        _resetPromptSettings() {
            const summary = this._currentSummary();
            if (!summary || !this.state.workspace) {
                return;
            }

            const hadSettingsDifference = this._hasSummarySettingsToRestore();
            if (!hadSettingsDifference) {
                const staleReason = this._currentSummaryOutOfDateReason();
                if (staleReason) {
                    this._showBanner(`Reset does not clear this: ${staleReason}`, 'error');
                } else {
                    this._showBanner('Settings already match this summary.', 'success');
                }
                return;
            }

            this.state.settingsViewMode = 'workspace';
            this.state.promptEditMode = false;
            this.state.promptEditValue = '';
            this.state.promptEditInitialValue = '';
            this.state.pendingResetSummaryId = summary.id;
            this.state.workspace.settings = {
                ...this.state.workspace.settings,
                template_key: this._normalizeTemplateKey(summary.template_key),
                custom_prompt: summary.custom_prompt || null,
            };
            this._syncLastCustomPrompt();
            this._renderSettings();
            this._renderHeader();
            this._renderSummary();
            this._renderFooter();
            this._queueSettingsSave();
        }

        _handlePromptInput() {
            this._beginSettingsEditSession();
            this._autoResizeTextarea(this.elements.customPrompt);
            const value = this.elements.customPrompt.value;

            if (!this.state.promptEditMode) {
                this.state.promptEditMode = true;
            }

            if (
                this.state.workspace?.settings?.template_key !== 'custom'
                && value !== this.state.promptEditInitialValue
            ) {
                this.state.workspace.settings = {
                    ...this.state.workspace.settings,
                    template_key: 'custom',
                    custom_prompt: value.trim() || null,
                };
            } else if (this.state.workspace?.settings?.template_key === 'custom') {
                this.state.workspace.settings = {
                    ...this.state.workspace.settings,
                    custom_prompt: value.trim() || null,
                };
            }

            this.state.promptEditValue = value;
            if (value.trim()) {
                this.state.lastCustomPrompt = value.trim();
            }
            this._renderSettings();
            this._renderHeader();
            this._renderSummary();
            this._renderFooter();
            this._queueSettingsSave();
        }

        _queueSettingsSave() {
            if (!this.state.workspace) {
                return;
            }
            this.state.workspace.settings = {
                ...this.state.workspace.settings,
                title: this.elements.titleInput.value.trim() || null,
                custom_prompt: this._settingsCustomPromptValue(),
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
                template_key: this._normalizeTemplateKey(this.state.workspace?.settings?.template_key),
                custom_prompt: this._settingsCustomPromptValue(),
                transcript_version_id: this.state.selectedTranscriptVersionId,
            };

            try {
                await this._jsonRequest(`/api/recordings/${this.state.sessionId}/settings`, {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                }, {
                    timeoutMs: 10000,
                    retries: 1,
                    networkErrorMessage: 'Workspace network request failed',
                    httpErrorMessage: 'Failed to save settings.',
                    logLabel: 'workspace_settings:save',
                });
            } catch (error) {
                this.state.pendingResetSummaryId = null;
                this._showBanner(error.message || 'Failed to save settings.', 'error');
                return false;
            }

            await this._loadWorkspace();
            this._handlePostSettingsSave();
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

        async _jsonRequest(url, options = {}, config = {}) {
            return window.SidekickNetwork.json(url, options, config);
        }

        _resolveApiMediaUrl(url) {
            if (!url) {
                return '';
            }
            return window.SidekickNetwork.resolveUrl(url);
        }

        _revertOpenState() {
            this.elements.modal.classList.add('hidden');
            this._unlockBodyScroll();
            this.state.sessionId = null;
            this.state.jobStatus = null;
            this.state.workspace = null;
            this.state.banner = null;
            this.state.selectedSavedSummaryId = null;
            this._resetTabScrollStage();
            this._stopSpeakerPlayback();
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

        _currentPromptDisplayText() {
            const settings = this._settingsFormValues();
            const templateKey = this._normalizeTemplateKey(settings.template_key);
            const selectedTemplate = this.templates[templateKey];
            const basePrompt = selectedTemplate?.prompt || '';

            if (templateKey === 'custom') {
                return settings.custom_prompt || basePrompt || '';
            }

            const extraInstructions = (settings.custom_prompt || '').trim();
            if (!extraInstructions) {
                return basePrompt;
            }

            return `${basePrompt}\n\n## Additional Instructions\n\n${extraInstructions}`;
        }

        _promptSourceLabel() {
            const settings = this._settingsFormValues();
            const templateKey = this._normalizeTemplateKey(settings.template_key);
            const templateName = this.templates[templateKey]?.name || 'Prompt';

            if (templateKey === 'custom') {
                return 'Custom prompt';
            }

            if ((settings.custom_prompt || '').trim()) {
                return `${templateName} prompt with additional instructions`;
            }

            return `${templateName} prompt`;
        }

        _settingsCustomPromptValue() {
            const settings = this.state.workspace?.settings || {};
            if (this._normalizeTemplateKey(settings.template_key) !== 'custom') {
                return settings.custom_prompt || null;
            }

            if (this.state.promptEditMode) {
                return this.state.promptEditValue.trim() || null;
            }

            return settings.custom_prompt || null;
        }

        _preferredCustomPrompt() {
            const workspacePrompt = (this.state.workspace?.settings?.custom_prompt || '').trim();
            const summaryPrompt = (this._currentSummary()?.custom_prompt || '').trim();
            const lastPrompt = (this.state.lastCustomPrompt || '').trim();
            const defaultPrompt = (this.templates.custom?.prompt || '').trim();
            return workspacePrompt || summaryPrompt || lastPrompt || defaultPrompt || '';
        }

        _syncLastCustomPrompt() {
            const workspacePrompt = (this.state.workspace?.settings?.custom_prompt || '').trim();
            const summaryPrompt = (this._currentSummary()?.custom_prompt || '').trim();
            const nextPrompt = workspacePrompt || summaryPrompt || this.state.lastCustomPrompt || '';
            this.state.lastCustomPrompt = nextPrompt;
        }

        _speakerColorForLabel(label) {
            const palette = [
                '#7dd3fc',
                '#86efac',
                '#fca5a5',
                '#fcd34d',
                '#c4b5fd',
                '#fdba74',
                '#67e8f9',
                '#f9a8d4',
                '#bef264',
                '#93c5fd',
            ];
            let hash = 0;
            const value = String(label || '');
            for (let index = 0; index < value.length; index += 1) {
                hash = ((hash << 5) - hash) + value.charCodeAt(index);
                hash |= 0;
            }
            return palette[Math.abs(hash) % palette.length];
        }

        _measureTranscriptTimestampWidth(transcript) {
            const labels = transcript.map((segment) => String(segment.timestamp || '[00:00]'));
            const longestLabel = labels.reduce((longest, label) => (
                label.length > longest.length ? label : longest
            ), '[00:00]');
            const transcriptStyles = window.getComputedStyle(this.elements.transcript);
            const fontSize = '11px';
            const fontFamily = transcriptStyles.fontFamily || 'inherit';
            const fontWeight = '400';
            const canvas = document.createElement('canvas');
            const context = canvas.getContext('2d');
            if (!context) {
                return Math.max((longestLabel.length * 7) + 4, 42);
            }

            context.font = `${fontWeight} ${fontSize} ${fontFamily}`;
            const width = Math.ceil(context.measureText(longestLabel).width);
            return Math.max(width + 4, 42);
        }

        _measureTranscriptSpeakerWidth(transcript) {
            const labels = transcript.map((segment) => `[${String(segment.speaker || '?')}]:`);
            const longestLabel = labels.reduce((longest, label) => (
                label.length > longest.length ? label : longest
            ), '[?]:');
            const transcriptStyles = window.getComputedStyle(this.elements.transcript);
            const fontSize = transcriptStyles.fontSize || '16px';
            const fontFamily = transcriptStyles.fontFamily || 'inherit';
            const fontWeight = '500';
            const canvas = document.createElement('canvas');
            const context = canvas.getContext('2d');
            if (!context) {
                return Math.max((longestLabel.length * 8) + 4, 44);
            }

            context.font = `${fontWeight} ${fontSize} ${fontFamily}`;
            const width = Math.ceil(context.measureText(longestLabel).width);
            return Math.max(width + 4, 44);
        }

        _hasSummarySettingsToRestore() {
            const summary = this._currentSummary();
            if (!summary) {
                return false;
            }

            const currentSettings = this.state.workspace?.settings || {};
            const currentTemplateKey = this._normalizeTemplateKey(currentSettings.template_key);
            const currentCustomPrompt = currentSettings.custom_prompt || null;
            const summaryTemplateKey = this._normalizeTemplateKey(summary.template_key);
            const summaryCustomPrompt = summary.custom_prompt || null;

            return currentTemplateKey !== summaryTemplateKey || currentCustomPrompt !== summaryCustomPrompt;
        }

        _canResetSummarySettings() {
            return Boolean(this._currentSummary());
        }

        _handlePostSettingsSave() {
            const resetSummaryId = this.state.pendingResetSummaryId;
            this.state.pendingResetSummaryId = null;
            if (!resetSummaryId) {
                return;
            }

            const currentSummary = this._currentSummary();
            if (!currentSummary || currentSummary.id !== resetSummaryId) {
                return;
            }

            if (this._hasSummarySettingsToRestore()) {
                this._showBanner('Failed to restore the settings used for this summary.', 'error');
                return;
            }

            const staleReason = this._currentSummaryOutOfDateReason();
            if (staleReason) {
                this._showBanner(`Settings restored. Still out of date: ${staleReason}`, 'success');
                return;
            }

            this._showBanner('Restored the settings used for this summary.', 'success');
        }

        _settingsFormValues() {
            const workspaceSettings = this.state.workspace?.settings || {};
            if (this.state.settingsViewMode === 'workspace') {
                return workspaceSettings;
            }

            const currentSummary = this._currentSummary();
            if (!currentSummary) {
                return workspaceSettings;
            }

            return {
                ...workspaceSettings,
                template_key: this._normalizeTemplateKey(
                    currentSummary.template_key || workspaceSettings.template_key
                ),
                custom_prompt: currentSummary.custom_prompt ?? null,
            };
        }

        _beginSettingsEditSession() {
            if (!this.state.workspace || this.state.settingsViewMode === 'workspace') {
                return;
            }

            const displayedSettings = this._settingsFormValues();
            this.state.workspace.settings = {
                ...this.state.workspace.settings,
                template_key: this._normalizeTemplateKey(displayedSettings.template_key),
                custom_prompt: displayedSettings.custom_prompt ?? null,
            };
            this._syncLastCustomPrompt();
            this.state.settingsViewMode = 'workspace';
        }

        _resetSettingsEditSession() {
            if (!this.state.workspace) {
                return;
            }

            const currentSummary = this._currentSummary();
            this.state.settingsViewMode = 'selected';
            this.state.promptEditMode = false;
            this.state.promptEditValue = '';
            this.state.promptEditInitialValue = '';

            if (!currentSummary) {
                return;
            }

            this.state.workspace.settings = {
                ...this.state.workspace.settings,
                template_key: this._normalizeTemplateKey(currentSummary.template_key),
                custom_prompt: currentSummary.custom_prompt ?? null,
            };
            this._syncLastCustomPrompt();
        }

        _normalizeTemplateKey(templateKey) {
            const key = (templateKey || '').trim();
            if (!key || key === 'auto') {
                return 'meeting';
            }
            return key;
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

        _bringEditorIntoView(element) {
            if (!element || !this._detectSimpleMobileTabMotion()) {
                return;
            }
            window.setTimeout(() => {
                element.scrollIntoView({
                    block: 'start',
                    behavior: 'smooth',
                });
            }, 80);
        }

        _escapeHtml(value) {
            const div = document.createElement('div');
            div.textContent = value == null ? '' : String(value);
            return div.innerHTML;
        }

        _normalizeOptionalText(value) {
            return String(value || '').trim();
        }

        _sleep(ms) {
            return new Promise((resolve) => window.setTimeout(resolve, ms));
        }
    }

    window.RecordingWorkspace = RecordingWorkspace;
})();
