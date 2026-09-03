MLRUN_CE_IMAGE_PLATFORM ?= linux/amd64
MLRUN_CE_SPARK_IMAGE_TAG ?= gcr.io/iguazio/spark-app:3.5.6-scala2.12-java17-ubuntu-1
MLRUN_CE_SPARK_CUDA_IMAGE_TAG ?= gcr.io/iguazio/spark-app-cuda:3.5.6-scala2.12-java17-ubuntu-1
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
