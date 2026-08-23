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

FROM spark:3.5.6-scala2.12-java17-ubuntu

USER root

# Shared with the CUDA image.
COPY scripts/ce-customize.sh /tmp/ce-customize.sh
RUN chmod +x /tmp/ce-customize.sh && /tmp/ce-customize.sh && rm -f /tmp/ce-customize.sh

USER spark
