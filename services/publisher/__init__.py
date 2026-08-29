"""T25 YouTube publication.

The pipeline uploads an approved, current, T22-passing VidGen render to the
connected user's own YouTube channel, restartably. Every module here has one
job:

* :mod:`~services.publisher.youtube` - the single official capability registry:
  endpoints, scopes, limits, quota units, retryable statuses, processing states
  and the verification date. Nothing else hard-codes any of them.
* :mod:`~services.publisher.contracts` - the provider-neutral boundary.
* :mod:`~services.publisher.providers` - failure classification, bounded
  retries and provider selection.
* :mod:`~services.publisher.youtube_adapter` - the production Data API adapter.
* :mod:`~services.publisher.fake_youtube` - the deterministic offline provider.
* :mod:`~services.publisher.oauth` - the PKCE web-server authorization flow.
* :mod:`~services.publisher.credentials` - the sealed credential envelope.
* :mod:`~services.publisher.eligibility` - who may publish, decided first.
* :mod:`~services.publisher.metadata` - deterministic drafts and stable identity.
* :mod:`~services.publisher.resumable` - the resumable upload driver.
* :mod:`~services.publisher.processing` - bounded processing polling.
* :mod:`~services.publisher.pipeline` - the restartable orchestration.
* :mod:`~services.publisher.commands` - composed entry points.
* :mod:`~services.publisher.projections` - bounded, credential-free views.
"""
