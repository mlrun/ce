# CE Spark images

This directory builds:

- `quay.io/mlrun/spark-app:3.5.6-scala2.12-java17-ubuntu-1` (`Dockerfile`) --
  the regular CE Spark image.
- `quay.io/mlrun/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1` (`Dockerfile.cuda`) --
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

Build and validate the CUDA image, then push it manually (requires write access to the `mlrun` organization on Quay):

```bash
make build-cuda
make validate-cuda

docker login quay.io
docker push quay.io/mlrun/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1

docker inspect --format '{{index .RepoDigests 0}}' \
  quay.io/mlrun/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1
```

Do not republish the existing regular image. Record the CUDA image digest and
the CE source commit.

## Spark 4

The Spark 4 images target `linux/amd64` and use Spark 4.2.0, Scala 2.13,
Hadoop 3.5.0, Temurin 25.0.4+7, and Python 3.11:

- `spark-app:4.2.0-scala2.13-java25-ubuntu-1` uses the digest-pinned Spark
  base configured by `SPARK4_BASE_IMAGE` in the Makefile.
- `spark-app-cuda:4.2.0-scala2.13-java25-ubuntu-1` copies Spark and Java from
  that base into the digest-pinned `SPARK4_CUDA_BASE_IMAGE` (CUDA 12.8.1,
  cuDNN 9.8.0.87-1).

Both images install the connector artifacts listed in
`scripts/jars-4.2.0.txt`: Hadoop 3.5.0 connectors for S3A, ABFS, and GCS,
their required AWS and Azure dependencies, and the Scala 2.13 BigQuery
connector. These restore the connector family shipped by the published Spark
3 image so Spark 4 can serve as a like-for-like platform image. Local
validation checks packaging, class resolution, duplicate versions, and
CPU/CUDA parity. Authenticated provider testing and BigQuery compatibility
with Spark 4.2 are deferred to the corresponding activation work.

`hadoop-gcp-3.5.0` provides
`org.apache.hadoop.fs.gs.GoogleHadoopFileSystem`, replacing the former
`com.google.cloud.hadoop.fs.gcs.GoogleHadoopFileSystem` class. It does not
provide a replacement `AbstractFileSystem` implementation. Consumers using
the old `fs.gs.impl` or `fs.AbstractFileSystem.gs.impl` configuration must
update or remove it.

`hadoop-aws-3.5.0` uses AWS SDK v2. Hadoop remaps several common SDK v1
credential-provider names, but not
`com.amazonaws.auth.DefaultAWSCredentialsProviderChain`, arbitrary
`com.amazonaws.*` providers, or custom SDK v1 implementations. Consumers
using those providers must migrate their configuration.

MLRun selects the CPU repository and tag through `MLRUN_SPARK_APP_IMAGE` and
`MLRUN_SPARK_APP_IMAGE_TAG`, and derives the CUDA repository by appending
`-cuda`. mlefi resolves both repositories and extracts the Spark version from
the tag with `^(.+?)-scala.*$`; for example,
`4.2.0-scala2.13-java25-ubuntu-1` yields `4.2.0`. These recipes do not change
the configured defaults.

### Build

```bash
make build-spark4
make build-spark4-cuda
make build-spark4-all
```

`REGISTRY` defaults to `quay.io/mlrun`. The Makefile is the source of truth
for both pinned base-image digests.

### Validate

```bash
make validate-spark4-all
```

The validators check Spark, Scala, Java, Hadoop, Python, the connector
inventory, image metadata, and CUDA metadata. The aggregate target also checks
CPU/CUDA environment parity and compares every `$SPARK_HOME/jars` entry by
SHA-256. It does not authenticate to AWS, Azure, or GCP services.

### Publish (manual, JFrog)

Publication is manual. First check that neither
`spark-app:4.2.0-scala2.13-java25-ubuntu-1` nor
`spark-app-cuda:4.2.0-scala2.13-java25-ubuntu-1` already exists in
`mckinsey-ig4-next-gen-docker-local.jfrog.io`. Never overwrite an existing
immutable tag.

```bash
make REGISTRY=mckinsey-ig4-next-gen-docker-local.jfrog.io build-spark4-all
make REGISTRY=mckinsey-ig4-next-gen-docker-local.jfrog.io validate-spark4-all

docker login mckinsey-ig4-next-gen-docker-local.jfrog.io
docker push mckinsey-ig4-next-gen-docker-local.jfrog.io/spark-app:4.2.0-scala2.13-java25-ubuntu-1
docker push mckinsey-ig4-next-gen-docker-local.jfrog.io/spark-app-cuda:4.2.0-scala2.13-java25-ubuntu-1

docker inspect --format '{{index .RepoDigests 0}}' \
  mckinsey-ig4-next-gen-docker-local.jfrog.io/spark-app:4.2.0-scala2.13-java25-ubuntu-1
docker inspect --format '{{index .RepoDigests 0}}' \
  mckinsey-ig4-next-gen-docker-local.jfrog.io/spark-app-cuda:4.2.0-scala2.13-java25-ubuntu-1
```

Record both repository digests, then pull each image back by digest and
rerun validation against the digest references. A local image ID is not a
published digest.

### Jira evidence (ML-13080)

Attach to ML-13080:

- immutable tags and repository digests;
- pull-by-digest and image-inspection output;
- `spark-submit --version` and `java -version` output;
- CPU and CUDA JAR SHA-256 inventories and the parity result;
- the pinned base-image references from the Makefile;
- the source commit;
- the connector inventory and deferred provider-validation status documented
  above.
