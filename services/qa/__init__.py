"""T20 semantic visual QA.

The package keeps the pipeline stages separate on purpose:

``contracts``      authoritative input selection and structured lineage failures
``sampler``        deterministic frame selection
``deterministic``  measured technical checks that run before any paid request
``identity``       T19 character-identity comparison inputs
``continuity``     T13/T19 continuity comparison inputs
``visual_agent``   the provider-neutral semantic evaluator interface
``rubric``         versioned weights, thresholds and the repair-code taxonomy
``scoring``        score recomputation, outcome and routing recommendation
``adjudication``   the bounded second opinion
``evidence``       evidence assembly and the contact-sheet mapping
``pipeline``       restartable orchestration and persistence
``commands``       CLI and activity entry points

T20 identifies repairs. It never executes one: T21 owns repair and fallback
routing.
"""

PIPELINE_VERSION = "visual-qa/1.0.0"
