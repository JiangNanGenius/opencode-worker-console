# Contributing

Use Python 3.9-compatible standard-library code and vanilla browser JavaScript. Run `python3 scripts/run_tests.py` and the JavaScript checks from the README before submitting a pull request. The runner isolates default configuration and state for the suite and child processes.

Keep provider credentials, machine paths, task state and personal test artifacts out of commits. Add regression tests for changes to dispatch, recovery, quota interpretation, authentication and workspace integration. Treat upstream model output as untrusted data.

Test new UI actions against a temporary project. Never use a production repository to test write permissions or session deletion. Model calls cost real quota; the unit test suite must stay offline. Standalone smoke checks must set `DELEGATE_STATE` and `DELEGATE_CONFIG` to temporary paths **before importing** project modules. Never use the live install as a test fixture.

Quota adapters should preserve unknown values, currency and window identity, respect caching, and use fixed documented provider endpoints. Unsupported usage APIs must not prevent a provider from being used for delegation.
