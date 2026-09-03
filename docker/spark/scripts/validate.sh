#!/bin/bash

# Copyright 2025 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Local image checks that do not require a GPU.

set -euo pipefail

IMAGE="${1:?usage: validate.sh <image> [--cuda]}"
CUDA_MODE="${2:-}"

EXPECTED_JARS=(
  hadoop-aws-3.3.4.jar
  aws-java-sdk-bundle-1.12.262.jar
  wildfly-openssl-1.0.12.Final.jar
  gcs-connector-hadoop3-2.2.33-shaded.jar
  spark-bigquery-with-dependencies_2.12-0.43.1.jar
  hadoop-azure-3.3.4.jar
  azure-storage-7.0.1.jar
  hadoop-common-3.3.4.jar
  jetty-util-9.4.51.v20230217.jar
  jetty-util-ajax-9.4.51.v20230217.jar
)

run() {
  docker run --rm --platform "${MLRUN_CE_IMAGE_PLATFORM:-linux/amd64}" --entrypoint bash "$IMAGE" -c "$1"
}

echo "==> [$IMAGE] preserves the Spark entrypoint"
entrypoint="$(docker inspect "$IMAGE" --format '{{json .Config.Entrypoint}}')"
[[ "$entrypoint" == '["/opt/entrypoint.sh"]' ]] || { echo "FAIL: unexpected entrypoint: '$entrypoint'"; exit 1; }

echo "==> [$IMAGE] runs as the spark user"
whoami="$(run 'whoami')"
[[ "$whoami" == "spark" ]] || { echo "FAIL: expected spark user, got '$whoami'"; exit 1; }

echo "==> [$IMAGE] Java 17"
java_version="$(run 'java -version 2>&1')"
grep -q 'version "17\.' <<<"$java_version" || { echo "FAIL: Java is not version 17:"; echo "$java_version"; exit 1; }

echo "==> [$IMAGE] Spark 3.5.6 / Scala 2.12"
spark_version="$(run '$SPARK_HOME/bin/spark-submit --version 2>&1')"
grep -q 'version 3.5.6' <<<"$spark_version" || { echo "FAIL: Spark is not version 3.5.6:"; echo "$spark_version"; exit 1; }
grep -q 'Scala version 2.12' <<<"$spark_version" || { echo "FAIL: Scala is not version 2.12:"; echo "$spark_version"; exit 1; }

echo "==> [$IMAGE] Python 3.11"
python_version="$(run 'python3 --version 2>&1')"
grep -q 'Python 3.11' <<<"$python_version" || { echo "FAIL: Python is not version 3.11: $python_version"; exit 1; }

echo "==> [$IMAGE] connector JARs"
for jar in "${EXPECTED_JARS[@]}"; do
  run "test -f \$SPARK_HOME/jars/$jar" || { echo "FAIL: missing jar $jar"; exit 1; }
done

if [[ "$CUDA_MODE" == "--cuda" ]]; then
  expected_cuda_version="$(docker inspect "$IMAGE" --format '{{index .Config.Labels "com.iguazio.cuda-version"}}')"
  [[ -n "$expected_cuda_version" ]] || { echo "FAIL: CUDA version label is missing"; exit 1; }

  echo "==> [$IMAGE] CUDA $expected_cuda_version runtime"
  cuda_env="$(docker inspect "$IMAGE" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^CUDA_VERSION=' || true)"
  [[ "$cuda_env" == "CUDA_VERSION=$expected_cuda_version" ]] || { echo "FAIL: unexpected CUDA_VERSION: '$cuda_env'"; exit 1; }
  cuda_toolkit_version="${expected_cuda_version%.*}"
  nvcc_version="$(run 'nvcc --version 2>&1')"
  grep -Fq "release $cuda_toolkit_version" <<<"$nvcc_version" || { echo "FAIL: CUDA toolkit is not $cuda_toolkit_version: $nvcc_version"; exit 1; }

  echo "==> [$IMAGE] cuDNN 9.8 libraries present"
  run 'ldconfig -p | grep -q libcudnn.so.9' || { echo "FAIL: libcudnn.so.9 not found"; exit 1; }

  echo "==> [$IMAGE] NVIDIA_VISIBLE_DEVICES=all"
  visible_devices="$(docker inspect "$IMAGE" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^NVIDIA_VISIBLE_DEVICES=' || true)"
  [[ "$visible_devices" == "NVIDIA_VISIBLE_DEVICES=all" ]] || { echo "FAIL: unexpected NVIDIA_VISIBLE_DEVICES: '$visible_devices'"; exit 1; }

  echo "==> [$IMAGE] NVIDIA_DRIVER_CAPABILITIES=compute,utility"
  caps="$(docker inspect "$IMAGE" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^NVIDIA_DRIVER_CAPABILITIES=' || true)"
  [[ "$caps" == "NVIDIA_DRIVER_CAPABILITIES=compute,utility" ]] || { echo "FAIL: unexpected NVIDIA_DRIVER_CAPABILITIES: '$caps'"; exit 1; }
fi

echo "==> [$IMAGE] all checks passed"
