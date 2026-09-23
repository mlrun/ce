# SeaweedFS Remote Gateway Examples

These overlays enable `seaweedfs.remote` so KFP pipeline artifacts stay on in-cluster SeaweedFS while syncing to external AWS S3 or Azure Blob in the background.

Both examples set `seaweedfs.allInOne.data.type: emptyDir` (no SeaweedFS PVC) for lab/dev testing. Production installs should omit that block and keep the chart default `persistentVolumeClaim`.

Copy the overlay, replace the `<PLACEHOLDER>` values (including credentials), then deploy. Do not commit real secrets to git.

## Verify remote sync (CEML-734)

After `helm upgrade --install`:

1. Config job succeeded: `kubectl -n <ns> logs job/<release>-seaweedfs-remote-config`
2. Gateway running: `kubectl -n <ns> get pods -l app.kubernetes.io/component=seaweedfs-remote-gateway`
3. Run a KFP pipeline from Jupyter
4. Confirm artifacts in the remote bucket/container (Azure portal or `aws s3 ls`)
5. Wait for gateway sync — cloud may lag behind the pipeline finish time

If the SeaweedFS all-in-one pod restarts with `emptyDir`, remote mount state is lost locally. Run `helm upgrade` again (same values) to re-run the config hook job, or artifacts already in cloud remain but KFP may not see them until remount succeeds.

## AWS S3

1. Copy and customize `seaweedfs-remote-s3-overlay.yaml` (`bucket`, `accessKey`, `secretKey`, `endpoint`, `region`).
2. Deploy:

```bash
helm upgrade --install mlrun-ce charts/mlrun-ce -n mlrun \
  -f <your-environment-values>.yaml \
  -f charts/mlrun-ce/examples/seaweedfs-remote-s3-overlay.yaml
```

## Azure Blob

1. Copy and customize `seaweedfs-remote-azure-overlay.yaml`.
2. Set `accountName`, `accountKey`, and `containerName` (option A), or set `connectionString` and leave `accountName`/`accountKey` empty (option B).
3. Ensure `seaweedfs.remote.bucket` matches `storage.azure.containerName`.
4. Deploy:

```bash
helm upgrade --install mlrun-ce charts/mlrun-ce -n mlrun \
  -f <your-environment-values>.yaml \
  -f charts/mlrun-ce/examples/seaweedfs-remote-azure-overlay.yaml
```

See also: [Kubeflow Pipelines object store configuration](https://www.kubeflow.org/docs/components/pipelines/operator-guides/configure-object-store/).
