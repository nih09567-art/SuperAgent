# Cross-process Checkpoint recovery benchmark

This benchmark proves a narrow prototype claim: after a durable Scheduler checkpoint, the current Python process can terminate abruptly and a fresh Python process can load the checkpoint, restore protected Artifact payloads, skip completed steps, and continue from the first unfinished step. Dataset v2 also binds the confirmed email result to the project `email.dispatch.receipt@v1` contract so a downstream step consumes a Schema-valid persisted receipt.

It freezes six cases in `cases.json`, runs one uninterrupted baseline per case, then runs three interrupted trials per case (18 interrupted trials total). The crash worker calls `os._exit(86)` immediately after the target checkpoint has been written by the real `CheckpointManager`.

Run:

```powershell
.\.venv\Scripts\python.exe experiments\checkpoint_restart\benchmark.py `
  --output-dir .artifacts\quantitative\<run-id>\checkpoint-restart `
  --repeats 3
```

The Scheduler, CheckpointManager, protected Artifact payload store, persistent receipt store and reconciliation logic are project code. Agent/tool outputs are deterministic test doubles; no production remote Agent or email service is invoked.
