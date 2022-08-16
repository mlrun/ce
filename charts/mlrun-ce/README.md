# MLRun Community Edition: MLRun Open Source Stack for MLOps

This Helm charts bundles open source software stack for advanced ML operations

## Chart Details

The Open source MLRun ce chart includes the following stack:

* Nuclio - https://github.com/nuclio/nuclio
* MLRun - https://github.com/mlrun/mlrun
* Jupyter - https://github.com/jupyter/notebook (+MLRun integrated)
* MPI Operator - https://github.com/kubeflow/mpi-operator
* Minio - https://github.com/minio/minio/tree/master/helm/minio
* Spark Operator - https://github.com/GoogleCloudPlatform/spark-on-k8s-operator
* Pipelines - https://github.com/kubeflow/pipelines
* Prometheus stack - https://github.com/prometheus-community/helm-charts

## Prerequisites

- Helm >=3.6 installed from [here](https://helm.sh/docs/intro/install/)

- Preprovisioned Kubernetes StorageClass
  
> In case your Kubernetes flavor is not shipped with a default StorageClass, you may use [local-path by Rancher](https://github.com/rancher/local-path-provisioner)
> 1. Install it via [this link](https://github.com/rancher/local-path-provisioner#installation)  
> 2. Set as default by executing `kubectl patch storageclass local-path -p '{"metadata": {"annotations":{"storageclass.kubernetes.io/is-default-class":"true"}}}'`


## Installing the Chart

Create a namespace for the deployed components:
```bash
kubectl create namespace mlrun
```

Add the mlrun ce helm chart repo
```bash
helm repo add mlrun https://mlrun.github.io/ce
```

To work with the open source MLRun stack, you must an accessible docker-registry. The registry's URL and credentials
are consumed by the applications via a pre-created secret

To create a secret with your docker-registry details:

```bash
kubectl --namespace mlrun create secret docker-registry registry-credentials \
    --docker-username <registry-username> \
    --docker-password <login-password> \
    --docker-server <server URL, e.g. https://index.docker.io/v1/ > \
    --docker-email <user-email>
```

To install the chart with the release name `my-mlrun` use the following command, 
note the reference to the pre-created `registry-credentials` secret in `global.registry.secretName`, 
and a `global.registry.url` with an appropriate registry URL which can be authenticated by this secret:

```bash
helm --namespace mlrun \
    install my-mlrun \
    --wait \
    --set global.registry.url=<registry URL e.g. index.docker.io/iguazio > \
    --set global.registry.secretName=registry-credentials \
    mlrun/mlrun-ce
```

## Installing MLRun-ce on minikube

The Open source MLRun ce uses node ports for simplicity. If your kubernetes cluster is running inside a VM, 
as is the case when using minikube, the kubernetes services exposed over node ports would not be available on 
your local interface, but instead, on the virtual machine's interface.
To accommodate for this, use the `global.externalHostAddress` value on the chart. For example, if you're using 
the ce inside a minikube cluster, add `--set global.externalHostAddress=$(minikube ip)` to the helm install command.

## Advanced Chart Configuration

Configurable values are documented in the `values.yaml`, and the `values.yaml` of all sub charts. 
Override those [in the normal methods](https://helm.sh/docs/chart_template_guide/values_files/).

To use the full version, override the helm install command using `-f override-full.yaml`


### Usage

Your applications are now available in your local browser:
- jupyter-notebook - http://nodeipaddress:30040
- nuclio - http://nodeipaddress:30050
- mlrun UI - http://nodeipaddress:30060
- mlrun API (external) - http://nodeipaddress:30070
- minio API - http://nodeipaddress:30080
- minio UI - http://nodeipaddress:30090
- pipeline UI - http://nodeipaddress:30100
- grafana UI - http://nodeipaddress:30110


> **Note:**
> The above links assume your Kubernetes cluster is exposed on localhost.
> If that's not the case, the different components will be available on `externalHostAddress`

### Start Working

- Open Jupyter Notebook on [**jupyter-notebook UI**](http://localhost:30040) and run the code in 
[**examples/mlrun_basics.ipynb**](https://github.com/mlrun/mlrun/blob/master/examples/mlrun_basics.ipynb) notebook.

> **Note:**
> - You can change the ports by providing values to the helm install command.
> - You can add and configure a k8s ingress-controller for better security and control over external access.


## Uninstalling the Chart
```bash
helm --namespace mlrun uninstall my-mlrun
```

#### Note on terminating pods and hanging resources
It is important to note that this chart generates several persistent volume claims and also provisions an NFS
provisioning server, to provide the user with persistency (via pvc) out of the box.
Because of the persistency of PV/PVC resources, after installing this chart, PVs and PVCs will be created,
And upon uninstallation, any hanging / terminating pods will hold the PVCs and PVs respectively, as those
Prevent their safe removal.
Because pods stuck in terminating state seem to be a never-ending plague in k8s, please note this,
And don't forget to clean the remaining PVCs and PVs

Handing stuck-at-terminating pods:
```bash
kubectl --namespace mlrun delete pod --force --grace-period=0 <pod-name>
```

Reclaim dangling persistency resources:

| WARNING: This will result in data loss! |
| --- |

```bash
# To list PVCs
$ kubectl --namespace mlrun get pvc
...

# To remove a PVC
$ kubectl --namespace mlrun delete pvc <pvc-name>
...

# To list PVs
$ kubectl --namespace mlrun get pv
...

# To remove a PVC
$ kubectl --namespace mlrun delete pvc <pv-name>

# Remove hostpath(s) used for mlrun (and possibly nfs). Those will be created, by default under /tmp, and will contain
# your release name, e.g.:
$ rm -rf my-mlrun-mlrun-ce-mlrun
...
```

### Using Kubeflow Pipelines

MLRun enables you to run your functions while saving outputs and artifacts in a way that is visible to Kubeflow Pipelines.
If you wish to use this capability you will need to install Kubeflow on your cluster.
Refer to the [**Kubeflow documentation**](https://www.kubeflow.org/docs/started/getting-started/) for more information.
