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

T22 final editorial QA lives alongside it under the ``final_`` prefix, and keeps
the same separation over the *assembled* T17 delivery rather than one shot:

``final_inputs``              canonical render selection and stale-lineage rejection
``final_deterministic``       measured media checks on the assembled output
``final_audio``               measured checks on the delivered final mix
``final_captions``            canonical manifest and delivered caption-asset checks
``final_evidence``            deterministic sampling, contact sheet and evidence
``final_rubric``              versioned configuration, dimensions and routing policy
``final_editorial_provider``  the provider-neutral editorial interface and registry
``final_fake_provider``       the deterministic fake used by tests and CI
``final_openai_adapter``      the configured production structured-output adapter
``final_gate``                finding recomputation, routing and the completion gate
``final_human_review``        bounded human adjudication of uncertain findings
``final_editorial``           restartable orchestration and persistence
``final_commands``            CLI, worker and activity entry points

T22 identifies and gates. It never makes a paid generation call and never starts
another creative repair loop.
"""

PIPELINE_VERSION = "visual-qa/1.0.0"
