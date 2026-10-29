MLRUN_CE_IMAGE_PLATFORM ?= linux/amd64
MLRUN_CE_SPARK_IMAGE_TAG ?= quay.io/mlrun/spark-app:3.5.6-scala2.12-java17-ubuntu-1
MLRUN_CE_SPARK_CUDA_IMAGE_TAG ?= quay.io/mlrun/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1
CUDA_VERSION ?= 12.8.1

# Kept for backward compatibility with the pre-CUDA workflow.
MLRUN_CE_IMAGE_TAG ?= $(MLRUN_CE_SPARK_IMAGE_TAG)

.PHONY: build
build:
	docker build --platform="$(MLRUN_CE_IMAGE_PLATFORM)" -t "$(MLRUN_CE_IMAGE_TAG)" -f Dockerfile .

.PHONY: build-cuda
build-cuda:
	docker build --platform="$(MLRUN_CE_IMAGE_PLATFORM)" --build-arg CUDA_VERSION="$(CUDA_VERSION)" -t "$(MLRUN_CE_SPARK_CUDA_IMAGE_TAG)" -f Dockerfile.cuda .

.PHONY: build-all
build-all: build build-cuda

.PHONY: validate
validate:
	bash scripts/validate.sh "$(MLRUN_CE_IMAGE_TAG)"

.PHONY: validate-cuda
validate-cuda:
	bash scripts/validate.sh "$(MLRUN_CE_SPARK_CUDA_IMAGE_TAG)" --cuda

.PHONY: validate-all
validate-all: validate validate-cuda

# Spark 4 variables and targets.
REGISTRY ?= quay.io/mlrun
# Spark uses a multi-platform index; CUDA uses its linux/amd64 child manifest
# because the Spark 4 image pair is intentionally amd64-only.
SPARK4_BASE_IMAGE ?= spark@sha256:66e39dccde81909c23e5c56f4b465db6de2bdc38cf569aacc9e2380ee5005440
SPARK4_CUDA_BASE_IMAGE ?= nvidia/cuda@sha256:61f6c08f2b59036cb935e56d1e31a6b64e3ae2c7ddb86d33fa0b044c7917b719
MLRUN_CE_SPARK4_IMAGE_TAG ?= $(REGISTRY)/spark-app:4.2.0-scala2.13-java25-ubuntu-1
MLRUN_CE_SPARK4_CUDA_IMAGE_TAG ?= $(REGISTRY)/spark-app-cuda:4.2.0-scala2.13-java25-ubuntu-1

.PHONY: build-spark4
build-spark4:
	docker build --platform=linux/amd64 \
	  --build-arg SPARK4_BASE_IMAGE="$(SPARK4_BASE_IMAGE)" \
	  -t "$(MLRUN_CE_SPARK4_IMAGE_TAG)" -f Dockerfile.spark4 .

.PHONY: build-spark4-cuda
build-spark4-cuda:
	docker build --platform=linux/amd64 \
	  --build-arg SPARK4_BASE_IMAGE="$(SPARK4_BASE_IMAGE)" \
	  --build-arg SPARK4_CUDA_BASE_IMAGE="$(SPARK4_CUDA_BASE_IMAGE)" \
	  -t "$(MLRUN_CE_SPARK4_CUDA_IMAGE_TAG)" -f Dockerfile.spark4.cuda .

.PHONY: build-spark4-all
build-spark4-all: build-spark4 build-spark4-cuda

.PHONY: validate-spark4
validate-spark4:
	bash scripts/validate-spark4.sh "$(MLRUN_CE_SPARK4_IMAGE_TAG)"

.PHONY: validate-spark4-cuda
validate-spark4-cuda:
	bash scripts/validate-spark4.sh "$(MLRUN_CE_SPARK4_CUDA_IMAGE_TAG)" --cuda

.PHONY: validate-spark4-all
validate-spark4-all: validate-spark4 validate-spark4-cuda jar-parity-spark4 env-parity-spark4

.PHONY: jar-parity-spark4
jar-parity-spark4:
	bash scripts/jar-parity.sh "$(MLRUN_CE_SPARK4_IMAGE_TAG)" "$(MLRUN_CE_SPARK4_CUDA_IMAGE_TAG)"

.PHONY: env-parity-spark4
env-parity-spark4:
	bash scripts/env-parity.sh "$(MLRUN_CE_SPARK4_IMAGE_TAG)" "$(MLRUN_CE_SPARK4_CUDA_IMAGE_TAG)"
