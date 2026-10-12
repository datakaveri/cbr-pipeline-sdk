# cbr-pipeline-sdk

Python helpers for **custom nodes** of the CBR no-code pipeline editor. A node is a
container that the platform starts with a `NODE_CONTEXT` environment variable
describing its inputs, outputs and settings; this package takes care of the
repetitive parts. It uses only the Python standard library.

```bash
pip install cbr-pipeline-sdk
```

The import name is `pipeline_sdk`:

```python
from pipeline_sdk import context

def handler(ctx, workdir):
    src = ctx.input().download(workdir)          # the node's input file
    result = process(src, ctx.config)            # your code
    ctx.output("result").upload_file(result)     # the declared output
    return {"rowCount": 123}                     # added to the final status line

context.run(handler)                             # parses NODE_CONTEXT, sets the exit code
```

## Modules

Each module imports only the ones above it.

| Module | Purpose |
|---|---|
| `util` | Shared HTTP helper with retries, and the error type |
| `env` | The only module that reads environment variables; typed accessors with clear errors, and `get_secret(name)` for secrets the user stored in the editor |
| `storage` | Read and write files by path through the platform backend |
| `context` | Parses `NODE_CONTEXT`; `run(handler)` handles the working directory, the status line and the exit code |
| `boto3_compat` | A boto3-shaped client for older code, routed through `storage` |
| `log_mask` | Replaces any secret fetched with `get_secret` by `[REDACTED]` in what the node prints (best effort) |

## Notes

- It is meant to run **inside a platform node**: it needs the environment the platform
  injects (`NODE_CONTEXT`, `BACKEND_URL`, `API_KEY`, ...) and a reachable backend.
- Nodes hold no storage credentials; every read and write goes through the backend,
  authenticated with the node owner's API key.
- Log masking covers the Python process's stdout/stderr only; never print a secret on purpose.
