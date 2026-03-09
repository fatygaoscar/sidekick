# Plan: Convert Sidekick into a PWA-first web app that preserves an app-store path

## Summary

Sidekick is a good PWA candidate because it is already a mobile-focused, single-origin web app with static assets served by FastAPI, but it is not yet installable and has no offline shell. The first phase should make it a high-quality PWA now while preserving a clean path to packaged mobile apps later.

### Benefits of doing this

- Users can install Sidekick from the browser to the home screen and launch it in a standalone app-like window.
- Startup feels faster because core shell assets can be cached locally.
- The app can show an offline fallback instead of failing hard when the network drops.
- Icons, splash behavior, theme color, and standalone display make the product feel more like an app.
- This also forces the frontend into a cleaner "app shell + network data" model, which helps later packaging.

### Is this a step toward a real app-store app?

Yes, but only partially.

- For Android, a solid PWA is a practical precursor to Play Store packaging via Trusted Web Activity or a Capacitor wrapper. Google's documentation explicitly positions PWAs as publishable to Google Play through TWA when they meet installability criteria.
- For iOS, a PWA is not itself an App Store submission path. Apple's review guidance explicitly warns that websites served inside an iOS app and limited web interactions do not make a quality app. So a PWA helps with product polish and architecture, but App Store publication will still require a native wrapper and enough iOS-specific value to survive review.
- For Sidekick specifically, a PWA is the right next step, but it should be treated as "web app hardening for packaging later," not "we will automatically be ready for the App Store."

Sources:
- Android Trusted Web Activity overview: https://developer.android.com/develop/ui/views/layout/webapps/trusted-web-activities
- PWA install/store guidance: https://web.dev/learn/pwa/installation
- Apple App Review guidance on web-content-only apps: https://developer.apple.com/app-store/review/

## Current-state observations that drive the plan

- `web/index.html` and `web/recordings.html` have no manifest link, no theme-color metadata, and no service worker registration.
- There is no `manifest.webmanifest`, `sw.js`, or offline page.
- Both pages load `marked` from a CDN, which weakens offline reliability and install quality.
- The app depends on networked APIs and WebSocket recording/transcription, so the first PWA phase should not promise offline recording or offline export.
- FastAPI already serves static assets cleanly and can easily expose manifest/service-worker/offline routes.

## Scope

### In scope for Phase 1

- Installable PWA with complete manifest
- Service worker with app-shell caching and safe runtime caching
- Offline fallback page
- Local hosting of all shell dependencies needed for first load
- App-like metadata for Android and iOS home-screen install
- PWA-safe UX adjustments for recording/search/history/workspace routes
- Basic install-prompt handling for supported browsers
- Architecture choices that keep later Capacitor/TWA packaging straightforward

### Out of scope for Phase 1

- True offline recording with deferred upload
- Background recording
- Push notifications
- Background sync of export/transcription jobs
- App Store or Play Store packaging itself
- On-device transcription/summarization

## Implementation plan

### 1. Add PWA metadata and install surface

Create a web manifest and wire it into both entry pages.

Deliverables:
- Add `web/manifest.webmanifest`
- Add `<link rel="manifest" href="/manifest.webmanifest">` to `web/index.html` and `web/recordings.html`
- Add theme metadata:
  - `<meta name="theme-color" ...>`
  - Apple mobile-web-app tags for standalone launch
- Add/install proper icon set:
  - 192x192
  - 512x512
  - maskable icon variant if feasible
  - apple-touch-icon
- Set manifest fields:
  - `name`: `Sidekick`
  - `short_name`: `Sidekick`
  - `start_url`: `/`
  - `scope`: `/`
  - `display`: `standalone`
  - `background_color` and `theme_color` aligned with current branding
  - shortcuts for `Record` and `History`
  - screenshots only if later needed for richer install surfaces

Decision:
- Use `display: standalone`, not `fullscreen`, because Sidekick benefits from normal OS affordances and this maps better to later wrapper behavior.

### 2. Register a service worker with conservative caching

Add a service worker that caches only the stable shell and uses network-first behavior for dynamic API content.

Deliverables:
- Add `web/sw.js`
- Register it from a shared JS bootstrap loaded by both pages, or in both page scripts directly
- Version cache names explicitly so deploys invalidate predictably

Caching policy:
- `Cache First` for app shell:
  - `/`
  - `/recordings`
  - CSS
  - local JS bundles
  - icons
  - manifest
  - offline page
  - vendored `marked`
- `Network First` for HTML navigations so the UI stays fresh when online
- `Stale While Revalidate` or `Cache First` for static images/icons
- `Network Only` for:
  - `/api/*`
  - `/ws/audio`
  - export/transcription/search endpoints
- Optional small fallback cache for last successful `/api/recordings` response only if kept read-only and clearly non-authoritative

Decision:
- Do not cache API responses broadly in Phase 1. Sidekick's data is user content and frequently updated; stale summaries/transcripts would create more risk than value.

### 3. Add an offline fallback experience

Create an offline page and explicit messaging around unsupported offline actions.

Deliverables:
- Add `web/offline.html`
- Service worker returns offline page for navigation requests when network fails and no cached page is available
- Add clear copy:
  - recordings/search/export require connection
  - previously visited pages may still open
  - reconnect to resume normal behavior

UX rules:
- If user opens the app offline from the home screen, they should still see a branded Sidekick page instead of a browser error.
- If recording is attempted without connectivity, show a direct status message instead of silently failing.

Decision:
- Offline mode is shell-only. Do not fake recording availability.

### 4. Vendor third-party frontend dependencies needed for first load

Remove CDN dependence for shell-critical assets.

Deliverables:
- Stop loading `marked` from jsDelivr in both HTML files
- Vendor `marked` into `web/js/vendor/` or bundle it into an existing local JS asset
- Ensure the service worker precaches it

Reason:
- A PWA that still depends on a CDN for first render is fragile and undermines offline/install behavior.

### 5. Make FastAPI serve PWA assets cleanly

Add explicit routes and headers for manifest/service worker/offline page.

Deliverables:
- In `src/api/app.py`, add routes for:
  - `GET /manifest.webmanifest`
  - `GET /sw.js`
  - `GET /offline`
- Serve `sw.js` from root scope, not under `/static`, so it can control the full origin
- Set correct content types:
  - `application/manifest+json`
  - `application/javascript`
- Keep `Cache-Control` sensible:
  - short/no-store for HTML
  - versioned caching for static assets
  - service worker script either no-store or short TTL so updates propagate

Important implementation note:
- Because the app currently rewrites `?v=` query strings in served HTML, service worker registration should use a stable root URL (`/sw.js`) and internal cache names should handle invalidation.

### 6. Add install UX for Chromium browsers, with graceful iOS fallback

Provide a minimal install prompt flow without overbuilding it.

Deliverables:
- Capture `beforeinstallprompt` where supported
- Show a lightweight "Install Sidekick" CTA only when the browser makes the event available
- For iOS Safari, show an instructional sheet using "Add to Home Screen" language only when the user is on iOS and not already standalone
- Do not nag repeatedly; remember dismissal locally

Decision:
- Install UX should be subtle and secondary. The app's primary CTA remains recording.

### 7. Audit PWA safety around recording and lifecycle behavior

Review current lifecycle code against installed-app behavior.

Areas to validate:
- `beforeunload` and history guard behavior in standalone mode
- `visibilitychange` reconnect logic for installed launches
- what happens if app is backgrounded mid-recording
- WebSocket reconnect messaging after temporary app suspension
- whether pagehide handling is too aggressive for installed contexts

Planned output:
- Keep current behavior for Phase 1, but explicitly test standalone/background flows and adjust only if they cause obvious regressions.

Decision:
- No attempt to support background recording in PWA mode. If app suspension stops a recording, the UI should explain that connection/foreground activity is required.

### 8. Prepare the codebase for later store packaging

Do not package now, but avoid decisions that make packaging harder.

Packaging direction:
- Android: prefer Trusted Web Activity first if the app remains mostly web-only
- iOS: prefer Capacitor-style native wrapper later if store publication remains a goal

Preparation tasks:
- Keep all app URLs under one origin and one scope
- Avoid browser-only assumptions in install/lifecycle code where possible
- Centralize environment-specific shell bootstrapping so wrapper-specific hooks can be added later
- Keep permissions flows explicit and user-triggered, especially microphone access

Decision:
- Assume "same hosted backend" remains the architecture for the packaged app. No on-device AI model path is planned here.

## Important changes to public APIs, interfaces, and assets

### New public routes

Add:
- `GET /manifest.webmanifest`
- `GET /sw.js`
- `GET /offline`

### New public static assets

Add:
- `web/manifest.webmanifest`
- `web/sw.js`
- `web/offline.html`
- icon assets for PWA and Apple install
- local vendored JS for `marked` if not bundled elsewhere

### No API contract changes in Phase 1

Existing JSON endpoints stay unchanged:
- `/api/recordings`
- `/api/search/recordings`
- `/api/recordings/{id}`
- export/transcription/speaker endpoints
- `WS /ws/audio`

## Acceptance criteria

The Phase 1 work is complete when all of the following are true:

- Chrome/Edge on Android can offer installation for Sidekick.
- Installed launch opens without browser chrome and uses Sidekick icons/themeing.
- First load works entirely from same-origin assets; no shell-critical CDN dependency remains.
- If the network drops after install, the app shows a branded offline page instead of a browser error.
- Recording/search/export paths clearly indicate they require connectivity.
- No regressions in normal online recording flow.
- Lighthouse PWA checks pass at a practical level for installability and offline fallback.
- The resulting architecture can later be wrapped without changing the core frontend routing model.

## Test cases and scenarios

### Installability

- Android Chrome shows installability after visiting the app
- Installed app launches to `/`
- Shortcut entry launches `Record`
- Shortcut entry launches `History`
- iOS Safari shows correct Add to Home Screen guidance
- Relaunch from home screen opens in standalone mode

### Offline shell

- Open app once online, then relaunch offline: offline fallback loads
- Visit `Recordings`, then go offline and relaunch: shell still opens
- Offline direct navigation to `/recordings` shows usable fallback behavior
- Offline refresh from installed app does not produce blank white screen or browser network error

### Online-only features

- Attempt recording while offline shows explicit failure state
- Search while offline shows explicit failure state
- Export/re-summarize while offline shows explicit failure state
- Existing recording list load handles offline cleanly without misleading stale state

### Lifecycle and recording risk

- Start recording in browser tab, background the app, then foreground it
- Start recording in installed app, switch apps briefly, then return
- Temporary network loss during recording gives a clear state update
- History/back-guard behavior still prevents accidental navigation while recording

### Update behavior

- Deploy new CSS/JS version and confirm service worker update does not strand old shell indefinitely
- New manifest/icon changes propagate after update
- HTML still respects current cache-busting strategy

## Risks and mitigations

### Risk: confusing "offline capable" with "offline functional"

Mitigation:
- Message clearly that recording, search, transcription, and export need connectivity in Phase 1.

### Risk: stale cached UI after deploy

Mitigation:
- Version caches explicitly and keep navigations network-first.

### Risk: installed-mode lifecycle differs from browser-tab lifecycle

Mitigation:
- Test standalone mode separately and keep service worker conservative.

### Risk: iOS App Store assumption creep

Mitigation:
- Treat PWA as a platform-hardening step, not as proof of App Store readiness.

## Assumptions and defaults chosen

- Primary goal: balance immediate PWA value with future store packaging.
- Offline scope: shell-only, not offline recording.
- Target publication path later: iOS + Android.
- Backend architecture later: same hosted FastAPI backend, not on-device processing.
- Android packaging later: Trusted Web Activity first, unless native features materially expand.
- iOS packaging later: native wrapper required; PWA alone is not considered sufficient for App Store publication.
- No authentication redesign is included in this phase.
- No changes to recording/export/search API schemas are required in this phase.
