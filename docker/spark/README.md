# CE Spark images

This directory builds:

- `gcr.io/iguazio/spark-app:3.5.6-scala2.12-java17-ubuntu-1` (`Dockerfile`) --
  the regular CE Spark image.
- `gcr.io/iguazio/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1` (`Dockerfile.cuda`) --
  the same Spark distribution and CE customization on CUDA 12.8.1/cuDNN 9.8.

MLRun selects the CUDA image by appending `-cuda` to the configured repository.
Both images share `scripts/ce-customize.sh`.

## Build

```bash
make build
make build-cuda
make build-all
```

Override `MLRUN_CE_SPARK_IMAGE_TAG`, `MLRUN_CE_SPARK_CUDA_IMAGE_TAG`,
`MLRUN_CE_IMAGE_PLATFORM`, or `CUDA_VERSION` as needed.

## Validate

These checks verify image contents and metadata without requiring a GPU.
GPU visibility and Spark GPU scheduling require a GPU environment.

```bash
make validate
make validate-cuda
make validate-all
```

## Publish

Build and validate the CUDA image, then push it manually:

```bash
make build-cuda
make validate-cuda

gcloud auth configure-docker gcr.io
docker push gcr.io/iguazio/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1

docker inspect --format '{{index .RepoDigests 0}}' \
  gcr.io/iguazio/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1
```

Do not republish the existing regular image. Record the CUDA image digest and
the CE source commit.
