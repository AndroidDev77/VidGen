"""Shared T15 failure types without adapter/pipeline import cycles.

The two submission failures below are deliberately siblings rather than
parent and child. They answer different questions, and conflating them is
what strands a shot:

* :class:`AmbiguousVideoSubmission` means *we cannot know* whether the
  provider created a task. Resubmitting could create - and bill - a duplicate,
  so the item is parked until a human reconciles it.
* :class:`VideoSubmissionNotSent` means the request *provably* never left this
  process, so no remote task can exist, nothing can be duplicated, and the
  item stays retryable.
"""


class AmbiguousVideoSubmission(RuntimeError):
    """Submission may have succeeded but no remote ID was received."""


class VideoSubmissionNotSent(RuntimeError):
    """The submission failed locally, before any request byte reached the provider.

    Only a failure that happened *before* the request could be written - a
    connection that was never established, a pool that never handed one out, an
    event loop that had already closed under the client - may be reported this
    way. Anything that could have been received and acted on by the provider is
    an :class:`AmbiguousVideoSubmission` instead.
    """
