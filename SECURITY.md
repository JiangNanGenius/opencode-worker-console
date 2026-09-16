# Security

Worker Desk is a single-user local application. Keep both its console and OpenCode server on loopback. It is not designed for internet exposure or mutually untrusted users.

Credentials stay in the existing OpenCode credential store and the owner-only runtime directory. The browser receives a local session cookie, never the OpenCode server password or provider credentials. Do not include keys, private prompts or logs in issues or screenshots.

Workers run with the calling user's filesystem privileges. Auto Approve is enabled by default in this dedicated service: all tool permissions allow execution without prompts. Task scopes and read-only intent are cooperative instructions, checked through resulting changes, not a sandbox. You can disable Auto Approve for native tool scopes and exact shell allowlists; shell commands can still do anything their dependencies can do. OpenCode's native UI also provides actions outside the worker queue.

Tasks, code snapshots and reports remain private local data. Do not commit the runtime state directory. Permanent session deletion requires explicit title confirmation and checks for active descendants. It removes OpenCode history, while the worker ledger and evidence remain.

For a suspected vulnerability, use the repository's private vulnerability reporting channel if available. Otherwise open an issue requesting a private contact without disclosing exploit details or personal data.
