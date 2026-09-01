# SeaweedFS Remote Gateway Examples

These overlays enable `seaweedfs.remote` so KFP pipeline artifacts stay on in-cluster SeaweedFS while syncing to external AWS S3 or Azure Blob in the background.

Copy the overlay, replace the `<PLACEHOLDER>` values (including credentials), then deploy. Do not commit real secrets to git.

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
