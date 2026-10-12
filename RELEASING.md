# Releasing

Nodes install the SDK from a release file, pinned to one version:

```
cbr-pipeline-sdk @ https://github.com/datakaveri/cbr-pipeline-sdk/releases/download/v2.2.0/cbr_pipeline_sdk-2.2.0-py3-none-any.whl
```

## Make a release

1. Bump `version` in `pyproject.toml` and merge it.
2. Tag that commit and push the tag:

   ```bash
   git tag v2.3.0 && git push origin v2.3.0
   ```

3. The `Release` workflow checks the tag matches the version, runs the tests, builds the wheel and
   source archive and attaches them to a new GitHub release.
4. To move a node to the new version, change the version in the link in its `requirements.txt` (or
   Dockerfile) and rebuild its image.

Don't delete or replace a published release: nodes pin its link, and a rebuilt node would stop
building. For a fix, publish a new version.
