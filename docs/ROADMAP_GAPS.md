# Remaining roadmap gaps

This document records gaps that are **confirmed by inspection of the code on `main`** and are
deliberately outside T18b. T18b's scope was executing the product commands and workflows that
already exist: closing the paths where an API reported work as accepted, queued or running when
nothing would ever consume it. Everything below is real work the product still needs, and none of
it is a prerequisite for that invariant.

Each item names the evidence, the product impact, a proposed owner task, its dependencies, a
suggested priority, acceptance criteria, and which audience it blocks: **local use**, **staging**,
**public SaaS**, or **product polish**.

None of the tasks below is complete. Nothing in this document should be read as delivered.

---

## T23b — Runtime telemetry wiring

**Evidence.** Every pipeline defaults to `Metrics()` when none is injected, and no process injects a
shared one, so each pipeline gets its own registry and no process-wide view exists. Nine pipelines
build their tracer from `trace.NoOpTracerProvider()` directly - image generation, storyboard,
narration, animation, visual QA, repair, continuity references, final editorial QA and the
publisher - so those spans are discarded even when an exporter is configured. `vidgen/telemetry/bootstrap.py` installs an Application Insights exporter only when a
connection string is configured, and no test or deployment has verified that a span produced by a
worker actually arrives. `observability/` ships a Grafana and Prometheus profile whose dashboards
have never been checked against real series names.

**Product impact.** An operator cannot answer "is the pipeline healthy?" from data. Provider
latency, failure classes, budget pressure and queue depth are recorded in the database but not
exported, so alerting is impossible and a regression is only visible after a user reports it.

**Proposed task.** *T23b: runtime telemetry wiring.*

1. One process-wide metrics registry, created at bootstrap and injected, never constructed per call.
2. Prometheus or OTLP metric export from the API, the Temporal worker, the render worker and the
   control dispatcher.
3. Remove every pipeline-local no-op tracer; take the tracer from the configured provider.
4. A verification that an Application Insights span emitted by a worker is queryable.
5. Local Grafana fed by real series, with the shipped dashboards validated against them.
6. Alert rules validated against those series rather than against invented metric names.

**Dependencies.** None on T18b. The control dispatcher is one more process to instrument.

**Priority.** High for staging, essential before public SaaS.

**Acceptance criteria.** A local run produces non-empty Prometheus series for provider attempts,
failures, costs and command dispatch; every dashboard panel resolves; a deployed worker's span is
retrievable in Application Insights; no `NoOpTracerProvider` remains outside a test.

**Blocks.** Staging (operability), public SaaS (alerting). Not local use.

---

## T24b — Production readiness

**Evidence.** `apps/api/auth.py` trusts the `X-VidGen-User` header outright: any caller may claim
any subject, which is why staging ingress is private and why `README.md` says production
authentication does not exist. `vidgen/uploads/service.py` stages resumable upload parts on local
disk under `VIDGEN_UPLOAD_ROOT`, so an upload is bound to the replica that received its first part
and the deployment depends on sticky sessions. The Veo alternate-provider route reads
`VIDGEN_GOOGLE_ACCESS_TOKEN`, a short-lived token with no acquisition or refresh path. Redis is
provisioned by `infra/bicep` and configured in `.env.example`, but nothing in the repository
connects to it: there is no Redis client dependency, and rate limiting, concurrency limiting and
caching are all absent. T24 itself is explicitly not marked complete: no private staging deployment
has been performed.

**Product impact.** The product cannot be exposed to more than one trusted user, cannot scale the
API horizontally without sticky sessions, cannot use its alternate repair provider in a deployment,
and pays for a Redis instance that does nothing.

**Proposed task.** *T24b: production readiness.*

1. Production user authentication and account identity, replacing the trusted header.
2. Owner scoping derived from a verified identity rather than a request header.
3. Blob-backed resumable upload parts, so any replica can accept any part.
4. Removal of the sticky-session requirement, demonstrated by a two-replica upload.
5. Veo credential acquisition and refresh, or an explicit decision to drop the Veo route.
6. A decision on Redis: implement rate limiting, concurrency limiting and caching on it, or remove
   it from the infrastructure and the configuration.
7. A real private staging deployment with acceptance evidence.

**Dependencies.** None on T18b. Item 3 touches T05 upload staging; item 7 depends on 1-6.

**Priority.** Blocking for public SaaS. Not required for local use.

**Acceptance criteria.** An unauthenticated request is refused; a forged subject cannot read
another owner's project; an upload survives being routed to a different replica mid-stream; either
Redis is used by a named subsystem with tests or it no longer appears in the templates; a staging
deployment has run with its smoke test green and the evidence recorded.

**Blocks.** Public SaaS entirely. Staging partially (item 7).

---

## T25b — Publication-product completeness

**Evidence, verified against merged T25.** T25 *does* implement OAuth, resumable upload, caption
upload, thumbnail **upload**, processing polling, quota accounting, publication identity and
eligibility - those are complete and are not listed here. What is missing:

- **Thumbnail creation or selection.** `services/publisher/eligibility.py` accepts a
  `thumbnail_asset_id` and validates it, and `services/publisher/pipeline.py` uploads it, but
  nothing in the repository produces a thumbnail asset. There is no frame selection, no generation
  and no UI for choosing one, so in practice every publication is thumbnail-less.
- **Title generation.** `services/publisher/metadata.py` composes
  `"{project name} - animated recap"` and says so in its own docstring: "T25 does not generate
  titles, descriptions or tags."
- **Description generation.** The same function emits a fixed three-line template.
- **Tag and category suggestions.** `_tags_from` derives tags mechanically from the project name and
  visual style; the category is a constant.
- **Canonical publication metadata ownership.** The draft is created once and then owned by the
  user's edits. Nothing regenerates or re-derives it when the recap changes, so a revised render
  keeps the previous cut's description unless a human rewrites it.

**Product impact.** A published recap looks unfinished: no thumbnail, a generic title, a templated
description. For a comedy recap channel these are the fields that decide whether anyone watches.

**Proposed task.** *T25b: publication-product completeness.*

1. Thumbnail candidate selection from the render's own frames, with a deterministic fallback.
2. Optional generated thumbnail composition, behind the existing provider abstractions.
3. Model-generated title candidates bounded by the T25 capability limits, reviewed before upload.
4. Model-generated descriptions with the required AI-disclosure line preserved.
5. Tag and category suggestions from the episode analysis rather than the project name.
6. A single owner for canonical publication metadata across revisions of the same project.

**Dependencies.** T22 (the render must have passed), T23 (these are paid calls and need attempts,
budgets and reconciliation). Independent of T18b.

**Priority.** Medium. It is polish, but it is the polish the product is for.

**Acceptance criteria.** A publication has a thumbnail without a human supplying one; titles and
descriptions are generated, reviewed and within YouTube's limits; every generated field is a
recorded provider attempt with a cost; a revised render produces new metadata rather than inheriting
the previous cut's.

**Blocks.** Product polish. Not local use, staging or the SaaS security posture.

---

## Audio post-production

**Evidence.** `services/renderer` mixes narration against the source audio only. The T17 render
manifest carries an audio-stem structure that nothing populates with music, effects or stingers, and
`services/qa/final_audio.py` measures loudness and drift without any concept of a music bed. There
is no licensing or provenance metadata for third-party audio anywhere in the data model.

**Product impact.** The recap sounds like narration over clips. A comedy recap without music beds,
sound effects or comic stingers reads as unfinished regardless of how good the writing is.

**Proposed task.** *Audio post-production.*

1. Background music selection from a licensed library, or generation behind a provider abstraction.
2. Sound effects placed against storyboard beats.
3. Comedy stingers tied to the script's joke annotations.
4. Licensing and provenance metadata persisted per audio asset, including the attribution a
   publication must carry.
5. T17 audio-stem population, so the mix is reproducible from the manifest.
6. Deterministic ducking and mix policies, verified by the existing T22 audio measurements.

**Dependencies.** T13 (beat timing), T17 (the mix), T22 (the audio gate), T23 (cost, if generated).
Independent of T18b.

**Priority.** Medium-high for the product's stated purpose; it is the largest remaining quality gap
after publication metadata.

**Acceptance criteria.** A rendered recap carries a music bed, at least one placed effect and a
stinger; loudness and true-peak stay within the T22 thresholds with ducking applied; every audio
asset carries its licence and attribution; the mix is byte-reproducible from the manifest.

**Blocks.** Product polish.

---

## Optional narration providers

**Evidence.** T12 supports OpenAI and the deterministic fake narrator.
`scripts/generate_narration.py` additionally accepts an ElevenLabs provider, which the worker's
narration stage does not use. No VoiceStudio adapter exists on `main`, and T18b deliberately did not
add one: the voice-profile work made provider selection a product flow, not a new integration.

**Product impact.** None today. It is an option, and it is recorded so that a future decision is
made deliberately rather than by accident.

**Proposed task.** *Optional narration providers.*

1. Evaluate VoiceStudio (and any other candidate) against the existing `NarrationProvider`
   interface: voice inventory, latency, per-character cost, word-timing fidelity, licensing.
2. Define the adapter requirements: idempotency key handling, alignment output, failure
   classification, T23 attempt and cost accounting.
3. Decide whether to implement. **No commitment is made here.**

**Dependencies.** T12, T23. The T18b voice-profile catalog is where a new provider's voices would
appear, and it already supports externally provisioned voice IDs.

**Priority.** Low.

**Acceptance criteria.** A written evaluation exists and a decision is recorded. If implemented: the
adapter passes the existing T12 contract tests, its voices appear in the voice catalog, and no
existing project's narration identity changes.

**Blocks.** Nothing.

---

## Smaller confirmed gaps

These are narrower than a roadmap task and are recorded so they are not rediscovered.

| Gap | Evidence | Impact | Suggested home |
| --- | --- | --- | --- |
| The control dispatcher has no deployment definition | `infra/bicep` defines the API, web, Temporal worker and the finite jobs; `workers/control_dispatcher` is not among them | A deployed environment accepts commands that nothing dispatches | T24b, with the staging deployment |
| `CORRECT_SCRIPT_UPSTREAM` and `CORRECT_REFERENCE_T19` remediations are refused rather than executed | `services/control_plane/handlers.py` lists both in `UNSUPPORTED_REMEDIATION_TARGETS` | A T22 finding routed to either needs a human to start the corresponding edit; the API says so explicitly rather than pretending | A follow-up to T18b, once the editing flows can accept a routed finding |
| A remediation cannot name which shot a finding belongs to without the caller supplying it | The T22 report records findings, not their owning shot | The UI must ask the user which shot to repair | T22 follow-up: persist the owning shot on each finding |
