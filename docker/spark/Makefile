MLRUN_CE_IMAGE_PLATFORM ?= linux/amd64
MLRUN_CE_IMAGE_TAG ?= gcr.io/iguazio/spark-app:3.5.6-scala2.12-java17-ubuntu

.PHONY: build
build:
	docker build --platform="$(MLRUN_CE_IMAGE_PLATFORM)" -t "$(MLRUN_CE_IMAGE_TAG)" .
