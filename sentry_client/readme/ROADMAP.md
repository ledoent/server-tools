* **Sentry Loader Script as an SDK source** — the Loader
  (`js.sentry-cdn.com/<key>.min.js`) lets the SDK version, sampling and
  replay settings be managed from the Sentry UI. It owns `Sentry.init`,
  so wiring it up means an `onLoad` merge with the tier settings this
  module drives from the Settings page and a clear rule for which side
  wins. Candidate for a follow-up.
* **Span-level trace correlation** — `browserTracingIntegration` already
  sends `sentry-trace` / `baggage` headers on same-origin requests and the
  server-side `sentry` module's WSGI middleware continues the trace, so
  browser and server *errors* link as-is. Correlating *spans* only needs
  `sentry_traces_sample_rate` on the server side, which `sentry` exposes.
  Nothing to build here; documenting the setup end to end is the gap.
* **OWL error-boundary depth** — the current handler captures the
  failing component tree + props. Could also enrich with the action
  context (active model, record IDs, view type) by reading
  `env.services.action.currentController`. Optional polish.
* **Asset-bundle profiling preload** — the JS Self-Profiling API needs
  the `Document-Policy: js-profiling` HTTP header on the document
  response, which Odoo doesn't emit by default. CONFIGURE.md documents
  the nginx workaround; a small `ir.http.dispatch` hook in this module
  could set the header conditionally when Tier 3 profiling is on.
