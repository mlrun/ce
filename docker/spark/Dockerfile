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

RUN set -ex; \
    apt-get update; \
    apt-get install -y --no-install-recommends software-properties-common curl ca-certificates gnupg; \
    add-apt-repository -y ppa:deadsnakes/ppa; \
    apt-get update; \
    apt-get install -y python3.11 python3-distutils git; \
    rm -rf /var/lib/apt/lists/*

RUN curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py && python3.11 get-pip.py

RUN curl -f -o /opt/spark/jars/hadoop-aws-3.3.4.jar -LO https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar
RUN curl -f -o /opt/spark/jars/aws-java-sdk-bundle-1.12.262.jar -LO https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar
RUN curl -f -o /opt/spark/jars/wildfly-openssl-1.0.12.Final.jar -LO https://repo1.maven.org/maven2/org/wildfly/openssl/wildfly-openssl/1.0.12.Final/wildfly-openssl-1.0.12.Final.jar
RUN curl -f -o /opt/spark/jars/gcs-connector-hadoop3-2.2.33-shaded.jar -LO https://repo1.maven.org/maven2/com/google/cloud/bigdataoss/gcs-connector/hadoop3-2.2.33/gcs-connector-hadoop3-2.2.33-shaded.jar
RUN curl -f -o /opt/spark/jars/spark-bigquery-with-dependencies_2.12-0.43.1.jar -LO https://repo1.maven.org/maven2/com/google/cloud/spark/spark-bigquery-with-dependencies_2.12/0.43.1/spark-bigquery-with-dependencies_2.12-0.43.1.jar
RUN curl -f -o /opt/spark/jars/hadoop-azure-3.3.4.jar -LO https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-azure/3.3.4/hadoop-azure-3.3.4.jar
RUN curl -f -o /opt/spark/jars/azure-storage-7.0.1.jar -LO https://repo1.maven.org/maven2/com/microsoft/azure/azure-storage/7.0.1/azure-storage-7.0.1.jar
RUN curl -f -o /opt/spark/jars/hadoop-common-3.3.4.jar -LO  https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-common/3.3.4/hadoop-common-3.3.4.jar
RUN curl -f -o /opt/spark/jars/jetty-util-9.4.51.v20230217.jar -LO https://repo1.maven.org/maven2/org/eclipse/jetty/jetty-util/9.4.51.v20230217/jetty-util-9.4.51.v20230217.jar
RUN curl -f -o /opt/spark/jars/jetty-util-ajax-9.4.51.v20230217.jar -LO https://repo1.maven.org/maven2/org/eclipse/jetty/jetty-util-ajax/9.4.51.v20230217/jetty-util-ajax-9.4.51.v20230217.jar

RUN update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

RUN ln -s /usr/bin/python3 /usr/bin/python

RUN mkdir /home/spark && chown spark /home/spark

USER spark
