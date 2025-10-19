# KubeScaler Operator
![Docker Image Version](https://img.shields.io/docker/v/0jas/kubescaler)
![Docker Pulls](https://img.shields.io/docker/pulls/0jas/kubescaler)

**Scale Smart, Save Big** - Intelligent workload scheduling for Kubernetes

**KubeScaler** is a lightweight, annotation-driven Kubernetes operator for scaling workloads based on a fine-grained schedule. It helps you save costs by scaling down applications during off-peak hours and scaling them back up for business hours, with precise control over timing.

## ✨ Features

  - **Advanced Time-Based Scaling**: Define schedules with specific times, days of the week, dates, and months.
  - **Broad Resource Support**: Manages Deployments, StatefulSets, HorizontalPodAutoscalers (HPAs), and CronJobs.
  - **Stateful Scaling**: Automatically backs up resource states (replica counts, HPA specs) to a ConfigMap before scaling down and restores them when scaling up.
  - **True HPA Hibernation**: On scale-down, HPAs are **deleted**. On scale-up, they are **re-created** from the backup, preserving the exact original configuration.
  - **Automatic State Pruning**: Retains a configurable number of recent backups (default is 5) and automatically deletes older ones to prevent clutter.
  - **Namespace & Resource Control**: Enable or disable scaling for entire namespaces or opt-out specific, critical applications.
  - **Safe by Design**: Ignores all `kube-*` system namespaces and runs with the minimum required permissions (least privilege principle).

-----

## ⚙️ How It Works

The operator runs a reconciliation loop every 60 seconds:

1.  **Scan Namespaces**: It finds all namespaces it's configured to watch (see Configuration section).
2.  **Parse Schedules**: For each resource, it parses the `ks_scale_up` and `ks_scale_down` annotations.
3.  **Match Schedule**: It compares the current UTC time, day, date, and month against the parsed schedule.
4.  **Take Action**:
      * **On Scale Down**:
        1.  It reads the current state of the resource (e.g., the full HPA spec).
        2.  It creates a new timestamped ConfigMap to save this state.
        3.  It scales the resource down (e.g., sets replicas to 0, or **deletes the HPA**).
        4.  It prunes any backup ConfigMaps for that resource older than the configured retention limit.
      * **On Scale Up**:
        1.  It finds the most recent backup ConfigMap for that resource.
        2.  It reads the saved state.
        3.  It scales the resource up, restoring it to its original state (e.g., **re-creating the HPA** with the saved spec).

-----

## 🚀 Installation Guide

### Prerequisites

  - A running Kubernetes cluster (v1.21+).
  - `kubectl` configured to your cluster.
  - Access to a container registry.

### Step 1: Build and Push the Docker Image

From the root of this repository, build and push the operator image.

```bash
docker build -t 0jas/kubescaler:latest .  # Replace with your repository
docker push 0jas/kubescaler:latest        # Replace with your repository
```

### Step 2: Update and Apply Manifests

1.  Open `kubernetes/03_operator-deployment.yaml` and update the `image` to point to the one you just pushed.
2.  Apply all manifests:

<!-- end list -->

```bash
kubectl apply -f kubernetes/01_operator-namespace.yaml
kubectl apply -f kubernetes/02_rbac.yaml
kubectl apply -f kubernetes/03_operator-deployment.yaml
```

### Step 3: Verify the Installation

Check that the operator pod is running and view its logs.

```bash
kubectl get pods -n kubescaler-system
kubectl logs -n kubescaler-system -l app=kubescaler-operator -f
```

-----

## 🔧 Configuration

You can configure the operator's behavior by editing the `kubernetes/03_operator-deployment.yaml` file.

### Backup Retention

The number of state backups to keep is controlled by the `MAX_BACKUPS_TO_RETAIN` environment variable.

| Environment Variable    | Default | Description                                                                 |
| ----------------------- | ------- | --------------------------------------------------------------------------- |
| `MAX_BACKUPS_TO_RETAIN` | `5`     | The number of backup ConfigMaps to retain per resource. Older ones are deleted. |

```yaml
# kubernetes/03_operator-deployment.yaml
...
        env:
        - name: MAX_BACKUPS_TO_RETAIN
          value: "5"
...
```

### Controlling Operator Scope (Namespace Monitoring)

You can control which namespaces the operator monitors by setting the `args` in the container spec.

#### To Monitor All Namespaces

Use the `--all-namespaces` flag. This is the most common configuration.

```yaml
# kubernetes/03_operator-deployment.yaml
...
        args:
        - "--all-namespaces"
...
```

#### To Monitor Multiple Specific Namespaces

Repeat the `--namespace` flag for each namespace you want to watch.

```yaml
# kubernetes/03_operator-deployment.yaml
...
        args:
        - "--namespace=workloads"
        - "--namespace=staging-apps"
        - "--namespace=data-pipelines"
...
```

#### To Monitor Only a Single Namespace

Provide a single `--namespace` flag.

```yaml
# kubernetes/03_operator-deployment.yaml
...
        args:
        - "--namespace=default"
...
```

-----

## 📝 Usage

To use the operator, simply add annotations to your namespaces or resources.

### Resource-Level Scheduling

Add these annotations to your Deployments, StatefulSets, HPAs, or CronJobs.

| Annotation      | Value     | Effect                                                                    |
| --------------- | --------- | ------------------------------------------------------------------------- |
| `ks_scale_up`   | (string)  | The schedule to scale the resource up. See format below.                  |
| `ks_scale_down` | (string)  | The schedule to scale the resource down. See format below.                |
| `ks_scale`      | `Disable` | Excludes this specific resource from scaling. This takes precedence.      |


### Advanced Scheduling Format

The schedule format is a powerful, semicolon-separated string. All times are in **UTC**. Parts can be omitted, and the parser will intelligently identify them.

**Format:** `[YEAR;][MONTH;][DAY_OF_WEEK;][DATE_OF_MONTH;]TIME_OF_DAY`

-   **`TIME_OF_DAY`** (Required): The time in `HH:MM` format.
-   **`DATE_OF_MONTH`** (Optional): `*`, a number (`15`), or a list (`1,15`).
-   **`DAY_OF_WEEK`** (Optional): `*`, a 3-letter day (`Mon`), or a list (`Mon,Wed,Fri`).
-   **`MONTH`** (Optional): `*`, a 3-letter month (`Dec`), or a list (`Jun,Jul,Aug`).
-   **`YEAR`** (Optional): `*`, or a 4-digit year (e.g., `2026`).

#### Examples

-   **Scale down every day at 6 PM UTC:**
    ```yaml
    annotations:
      ks_scale_down: "18:00"
    ```

-   **Scale up only on weekdays during summer (June, July, August):**
    ```yaml
    annotations:
      ks_scale_up: "Jun,Jul,Aug;Mon,Tue,Wed,Thu,Fri;*;08:00"
    ```

-   **Scale down on the 1st of every month at midnight, but only in December:**
    ```yaml
    annotations:
      ks_scale_down: "Dec;1;00:00"
    ```

-   **NEW: Scale down for a one-time event (e.g., New Year's Day 2026):**
    ```yaml
    annotations:
      ks_scale_down: "2026;Jan;1;00:01"
    ```
    
-----

## 📝 Complete Usage Example

Here is a practical example of how to use KubeScaler to manage a typical development environment. This manifest defines a namespace containing a web server, an autoscaler, a critical database, and a one-time job.

### `example-environment.yaml`

```yaml
# A dedicated namespace for our development environment.
apiVersion: v1
kind: Namespace
metadata:
  name: dev-environment
---
# A web server Deployment that should only run during work hours on weekdays.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dev-web-server
  namespace: dev-environment
  annotations:
    # Scale up on weekdays at 8 AM UTC.
    ks_scale_up: "Mon,Tue,Wed,Thu,Fri;08:00"
    # Scale down on weekdays at 7 PM UTC.
    ks_scale_down: "Mon,Tue,Wed,Thu,Fri;19:00"
spec:
  replicas: 3 # The operator will back up and restore this value.
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
      - name: nginx
        image: nginx:latest
---
# An HPA for the web server that should also be disabled during off-hours.
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: dev-web-server-hpa
  namespace: dev-environment
  annotations:
    # Use the same schedule as the Deployment.
    ks_scale_up: "Mon,Tue,Wed,Thu,Fri;08:00"
    ks_scale_down: "Mon,Tue,Wed,Thu,Fri;19:00"
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: dev-web-server
  minReplicas: 3
  maxReplicas: 10 # The operator will back up and restore the entire spec.
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 80
---
# A critical database that must NEVER be scaled down.
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: dev-database
  namespace: dev-environment
  annotations:
    # This annotation explicitly protects this resource from the operator.
    ks_scale: "Disable"
spec:
  replicas: 1
  # ... (rest of statefulset spec) ...
---
# NEW: A one-time CronJob for an end-of-year report.
# We want to automatically suspend it after its run in 2026.
apiVersion: batch/v1
kind: CronJob
metadata:
  name: end-of-year-2025-report
  namespace: dev-environment
  annotations:
    # This job should be suspended (disabled) forever after Jan 1st, 2026.
    ks_scale_down: "2026;Jan;1;00:01"
spec:
  schedule: "0 0 1 1 *" # Runs at midnight on January 1st.
  jobTemplate:
    spec:
      template:
        spec:
          containers:
          - name: report-generator
            image: busybox
            command: ["echo", "Running end of year report"]
          restartPolicy: OnFailure
```

### What Will Happen

After applying this manifest, KubeScaler will perform the following actions based on the schedule:

  - **Every weekday (Mon-Fri) at 19:00 UTC (Scale Down):**

    1.  A backup ConfigMap is created for both the Deployment and the HPA.
    2.  The `dev-web-server` Deployment's replicas will be patched to **0**.
    3.  The `dev-web-server-hpa` object will be completely **deleted** from the cluster.
    4.  The `dev-database` StatefulSet will be **ignored** and will continue running.

  - **Every weekday (Mon-Fri) at 08:00 UTC (Scale Up):**

    1.  The operator finds the latest backup for the `dev-web-server` Deployment.
    2.  The Deployment is restored to its original state (**3** replicas).
    3.  The operator finds the latest backup for the HPA.
    4.  The `dev-web-server-hpa` is **re-created** with its full original configuration (`min: 3`, `max: 10`, etc.).

  - **Once on January 1st, 2026, at 00:01 UTC (One-Time Schedule):**

    1.  The `end-of-year-2025-report` CronJob will be **suspended** (by setting `spec.suspend: true`).
    2.  This effectively disables the CronJob from running ever again, as there is no corresponding `ks_scale_up` annotation.