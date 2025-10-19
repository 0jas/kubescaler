import kopf
import kubernetes
import datetime
import pytz
import logging
import json
import os
from typing import Tuple, Dict, Any, Optional

# --- Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Read the number of backups to retain from an environment variable, with a default.
MAX_BACKUPS_TO_RETAIN = int(os.environ.get('MAX_BACKUPS_TO_RETAIN', 5))

# Constants for annotations and labels
ANNOTATION_NS_CONTROL = 'ks_scale'
ANNOTATION_SCALE_UP = 'ks_scale_up'
ANNOTATION_SCALE_DOWN = 'ks_scale_down'
CONFIGMAP_PREFIX = 'ks-backup'
OPERATOR_LABEL = 'app.kubernetes.io/managed-by'
OPERATOR_VALUE = 'kubescaler'

# --- Kubernetes API Clients ---
try:
    kubernetes.config.load_incluster_config()
except kubernetes.config.ConfigException:
    kubernetes.config.load_kube_config()

core_v1 = kubernetes.client.CoreV1Api()
apps_v1 = kubernetes.client.AppsV1Api()
autoscaling_v2 = kubernetes.client.AutoscalingV2Api()
batch_v1 = kubernetes.client.BatchV1Api()




# --- Main Operator Logic ---

# Background task for periodic reconciliation
import threading
import atexit

reconciliation_thread = None
reconciliation_stop_event = threading.Event()


def cleanup():
    """Cleanup function called on process exit."""
    global reconciliation_thread
    logging.info("Stopping reconciliation thread...")
    reconciliation_stop_event.set()
    if reconciliation_thread and reconciliation_thread.is_alive():
        reconciliation_thread.join(timeout=5)
    logging.info("Reconciliation thread stopped")


atexit.register(cleanup)


def background_reconciliation_task(logger: logging.Logger) -> None:
    """
    Background thread that runs reconciliation every 60 seconds.

    Args:
        logger: The logger instance for logging messages.
    """
    while not reconciliation_stop_event.is_set():
        try:
            now_utc = datetime.datetime.now(pytz.utc)
            logger.debug(f"Reconciliation check running at {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}...")
            run_reconciliation(logger)
        except Exception as e:
            logger.error(f"An error occurred during reconciliation: {e}", exc_info=True)

        # Sleep for 60 seconds or until stop event is set
        reconciliation_stop_event.wait(60)


@kopf.on.startup()
def start_reconciliation_task(logger: logging.Logger, **kwargs: Any) -> None:
    """
    Start the background reconciliation thread on operator startup.

    Args:
        logger: The logger instance provided by kopf.
        **kwargs: Arbitrary keyword arguments passed by the kopf framework.
    """
    global reconciliation_thread
    logger.info("KubeScaler Operator is starting up...")
    logger.info(f"Configuration: MAX_BACKUPS_TO_RETAIN = {MAX_BACKUPS_TO_RETAIN}")

    # Start background reconciliation thread
    reconciliation_thread = threading.Thread(
        target=background_reconciliation_task,
        args=(logger,),
        daemon=True,
        name="kubescaler-reconciliation"
    )
    reconciliation_thread.start()
    logger.info("Started background reconciliation task")


def run_reconciliation(logger: logging.Logger) -> None:
    """
    Execute the reconciliation process for all eligible namespaces.

    Args:
        logger: The logger instance for logging messages.
    """
    now_utc = datetime.datetime.now(pytz.utc)

    try:
        namespaces = get_eligible_namespaces(logger)
        for ns_name in namespaces:
            logger.debug(f"Processing namespace: {ns_name}")
            process_resources(logger, ns_name, now_utc)
    except Exception as e:
        logger.error(f"An error occurred during reconciliation: {e}", exc_info=True)


# --- Schedule Parsing and Checking ---

def parse_schedule(schedule_annotation: str) -> Tuple[str, str, str, str, Optional[str]]:
    """
    Parse a flexible schedule annotation string into its component parts.

    The format is a semicolon-separated string that can contain specs for
    year, month, day of week, day of month, and time.
    Example: "2026;Dec;Fri;13;09:00"

    Args:
        schedule_annotation (str): The full annotation string.

    Returns:
        A tuple of (year_spec, month_spec, day_spec, date_spec, time_spec).
        Each spec is a string, or '*' if not specified. Time can be None.
    """
    if not schedule_annotation:
        return "*", "*", "*", "*", None

    parts = [p.strip().lower() for p in schedule_annotation.split(';')]

    # Check if last part contains time format (HH:MM)
    time_spec = None
    if parts and ':' in parts[-1]:
        time_spec = parts[-1]
        parts = parts[:-1]

    year_spec, month_spec, day_spec, date_spec = "*", "*", "*", "*"

    for part in parts:
        if not part:
            continue

        # Year: 4-digit number
        if len(part) == 4 and part.isdigit():
            year_spec = part
        # Month or Day of week: contains letters
        elif any(c.isalpha() for c in part):
            if any(month in part for month in ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
                                                  'jul', 'aug', 'sep', 'oct', 'nov', 'dec']):
                month_spec = part
            else:
                day_spec = part
        # Date: digits and commas only
        elif all(c.isdigit() or c == ',' for c in part):
            date_spec = part

    return year_spec, month_spec, day_spec, date_spec, time_spec


def is_schedule_active(schedule_annotation: str, now_utc: datetime.datetime) -> bool:
    """
    Determine if a schedule is active at the current moment.

    Compares the current UTC time, date, day of week, month, and year
    against the specifications in the parsed schedule.

    Args:
        schedule_annotation: The string value from a resource's annotation.
        now_utc: A timezone-aware datetime object (current UTC time).

    Returns:
        True if the current time matches the schedule, False otherwise.
    """
    if not schedule_annotation:
        return False

    year_spec, month_spec, day_spec, date_spec, time_spec = parse_schedule(schedule_annotation)

    # 1. Time must match
    if time_spec is None or now_utc.strftime('%H:%M') != time_spec:
        return False

    # 2. Year must match if specified
    current_year = now_utc.strftime('%Y')
    if year_spec != '*' and current_year != year_spec:
        return False

    # 3. Month must match if specified
    current_month = now_utc.strftime('%b').lower()
    if month_spec != '*':
        month_list = [m.strip() for m in month_spec.split(',')]
        if not any(current_month.startswith(m[:3]) for m in month_list):
            return False

    # 4. Day of week must match if specified
    current_day = now_utc.strftime('%a').lower()
    if day_spec != '*':
        day_list = [d.strip() for d in day_spec.split(',')]
        if not any(current_day.startswith(d[:3]) for d in day_list):
            return False

    # 5. Date of month must match if specified
    current_date = str(now_utc.day)
    if date_spec != '*':
        date_list = {d.strip() for d in date_spec.split(',')}
        if current_date not in date_list:
            return False

    return True

# --- Resource Processing Logic ---

def get_eligible_namespaces(logger: logging.Logger) -> list:
    """
    Retrieve a list of namespaces that are eligible for scaling.

    A namespace is considered eligible if it does not have the annotation
    `ks_scale: "Disable"` and its name does not start with `kube-`. This
    implements an opt-out model where namespaces are processed by default.

    Args:
        logger: The logger instance for logging skipped namespaces.

    Returns:
        A list of strings, where each string is the name of an eligible namespace.
    """
    eligible_namespaces = []
    try:
        all_namespaces = core_v1.list_namespace()
        for ns in all_namespaces.items:
            ns_name = ns.metadata.name
            annotations = ns.metadata.annotations or {}

            # Skip system namespaces and disabled namespaces
            if ns_name.startswith('kube-'):
                logger.debug(f"Skipping system namespace: {ns_name}")
                continue

            if annotations.get(ANNOTATION_NS_CONTROL) == 'Disable':
                logger.debug(f"Skipping disabled namespace: {ns_name}")
                continue

            eligible_namespaces.append(ns_name)
    except kubernetes.client.ApiException as e:
        if e.status == 403:
            logger.error(f"Permission denied listing namespaces. Ensure kubescaler-sa has 'list' and 'watch' "
                        f"permissions on namespaces: {e.reason}")
        else:
            logger.error(f"Failed to list namespaces: {e.status} {e.reason}")

    return eligible_namespaces


def process_resources(logger: logging.Logger, namespace: str, now_utc: datetime.datetime) -> None:
    """
    Orchestrate the processing of all supported resource types in a namespace.

    This function iterates through a dictionary of supported Kubernetes kinds
    (Deployment, StatefulSet, etc.) and calls the generic processing function
    for each resource found in the given namespace.

    Args:
        logger: The logger instance.
        namespace: The name of the namespace to process.
        now_utc: The current UTC time, passed down to scheduling functions.
    """
    resource_processors = {
        'Deployment': (apps_v1.list_namespaced_deployment, scale_deployment, get_deployment_state),
        'StatefulSet': (apps_v1.list_namespaced_stateful_set, scale_statefulset, get_statefulset_state),
        'HorizontalPodAutoscaler': (autoscaling_v2.list_namespaced_horizontal_pod_autoscaler, scale_hpa, get_hpa_state),
        'CronJob': (batch_v1.list_namespaced_cron_job, scale_cronjob, get_cronjob_state),
    }

    for kind, (lister, scaler, state_getter) in resource_processors.items():
        try:
            resources = lister(namespace)
            for item in resources.items:
                process_single_resource(logger, namespace, item, kind, now_utc, scaler, state_getter)
        except kubernetes.client.ApiException as e:
            if e.status == 403:
                logger.warning(f"Permission denied listing {kind}s in namespace {namespace}. "
                              f"Ensure kubescaler-sa has 'get', 'list', and 'watch' permissions for {kind}.")
            else:
                logger.error(f"Failed to list {kind}s in namespace {namespace}: {e.status} {e.reason}")
        except Exception as e:
            logger.error(f"Unexpected error listing {kind}s in namespace {namespace}: {e}")


def process_single_resource(logger: logging.Logger, namespace: str, resource: Any, kind: str,
                            now_utc: datetime.datetime, scaler_func: callable, state_getter_func: callable) -> None:
    """
    Apply scaling logic to a single Kubernetes resource.

    This is the core decision-making function. It checks a resource's
    annotations for scale-up/scale-down schedules. If the current time
    matches a schedule, it triggers the appropriate action: backup and
    scale-down, or restore and scale-up.

    Args:
        logger: The logger instance.
        namespace: The namespace of the resource.
        resource: The Kubernetes resource object from the client library.
        kind: The kind of the resource (e.g., 'Deployment').
        now_utc: The current UTC time.
        scaler_func: The function to call to perform the scaling action.
        state_getter_func: The function to call to get the resource's current state.
    """
    name = resource.metadata.name
    annotations = resource.metadata.annotations or {}

    if annotations.get(ANNOTATION_NS_CONTROL) == 'Disable':
        return

    scale_up_annotation = annotations.get(ANNOTATION_SCALE_UP)
    scale_down_annotation = annotations.get(ANNOTATION_SCALE_DOWN)

    # Check scale-down first to avoid conflicts
    if is_schedule_active(scale_down_annotation, now_utc):
        logger.info(
            f"SCALING DOWN {kind} '{name}' in namespace '{namespace}' as per schedule '{scale_down_annotation}'.")
        try:
            current_state = state_getter_func(namespace, name)
            if current_state:
                backup_state(logger, namespace, kind, name, current_state)
                scaler_func(logger, namespace, name, 'down', None)
        except Exception as e:
            logger.error(f"Failed to scale down {kind} {name}: {e}", exc_info=True)

    elif is_schedule_active(scale_up_annotation, now_utc):
        logger.info(f"SCALING UP {kind} '{name}' in namespace '{namespace}' as per schedule '{scale_up_annotation}'.")
        try:
            restored_state = find_latest_backup_state(logger, namespace, kind, name)
            if not restored_state:
                logger.warning(f"No backup state found for {kind} {name}. Cannot scale up.")
            else:
                scaler_func(logger, namespace, name, 'up', restored_state)
        except Exception as e:
            logger.error(f"Failed to scale up {kind} {name}: {e}", exc_info=True)


# --- State Management and Pruning ---

def prune_old_backups(logger: logging.Logger, namespace: str, kind: str, name: str) -> None:
    """
    Delete old backup ConfigMaps for a resource, retaining a configured number.

    This function lists all backup ConfigMaps for a specific resource, sorts them
    by creation time, and deletes the oldest ones, ensuring that only the
    `MAX_BACKUPS_TO_RETAIN` most recent copies are kept.

    Args:
        logger: The logger instance.
        namespace: The namespace of the resource.
        kind: The kind of the resource (e.g., 'Deployment').
        name: The name of the resource.
    """
    if MAX_BACKUPS_TO_RETAIN <= 0:
        return

    try:
        label_selector = f"{OPERATOR_LABEL}={OPERATOR_VALUE},resource-kind={kind},resource-name={name}"
        config_maps = core_v1.list_namespaced_config_map(namespace=namespace, label_selector=label_selector)

        if len(config_maps.items) <= MAX_BACKUPS_TO_RETAIN:
            return

        sorted_cms = sorted(config_maps.items, key=lambda cm: cm.metadata.creation_timestamp or datetime.datetime.min)
        cms_to_delete = sorted_cms[:-MAX_BACKUPS_TO_RETAIN]

        for cm in cms_to_delete:
            cm_name = cm.metadata.name
            logger.info(f"Pruning old backup ConfigMap: {cm_name}")
            try:
                core_v1.delete_namespaced_config_map(name=cm_name, namespace=namespace)
            except kubernetes.client.ApiException as e:
                logger.error(f"Failed to delete ConfigMap {cm_name}: {e}")

    except kubernetes.client.ApiException as e:
        logger.error(f"Failed to prune old backups for {kind}/{name}: {e}")


def backup_state(logger: logging.Logger, namespace: str, kind: str, name: str, state: Dict[str, Any]) -> None:
    """
    Save the current state of a resource to a new timestamped ConfigMap.

    This function is called just before a resource is scaled down. It creates
    a new ConfigMap with identifying labels and the resource's state stored
    as a JSON string. After a successful backup, it triggers the pruning
    of old backups.

    Args:
        logger: The logger instance.
        namespace: The namespace to create the ConfigMap in.
        kind: The kind of the resource being backed up.
        name: The name of the resource being backed up.
        state: A dictionary representing the resource's state to be saved.
    """
    now_utc_str = datetime.datetime.now(pytz.utc).strftime('%Y%m%d-%H%M%S')
    cm_name = f"{CONFIGMAP_PREFIX}-{kind.lower()}-{name}-{now_utc_str}"
    key_name = f"{kind.lower()}-{name}"

    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": cm_name,
            "namespace": namespace,
            "labels": {
                OPERATOR_LABEL: OPERATOR_VALUE,
                "resource-kind": kind,
                "resource-name": name
            }
        },
        "data": {key_name: json.dumps(state)}
    }

    try:
        core_v1.create_namespaced_config_map(namespace=namespace, body=body)
        logger.info(f"State for {kind} {name} saved in ConfigMap '{cm_name}'.")
        prune_old_backups(logger, namespace, kind, name)
    except kubernetes.client.ApiException as e:
        logger.error(f"Failed to create backup ConfigMap for {kind} {name}: {e}")


def find_latest_backup_state(logger: logging.Logger, namespace: str, kind: str, name: str) -> Optional[Dict[str, Any]]:
    """
    Find the most recent backup ConfigMap for a resource and retrieve its state.

    This function lists ConfigMaps in the namespace using labels set during the
    backup process, sorts them by creation timestamp, and returns the data
    from the newest one.

    Args:
        logger: The logger instance.
        namespace: The namespace where the backups are stored.
        kind: The kind of the resource to restore.
        name: The name of the resource to restore.

    Returns:
        A dictionary containing the restored state, or None if no backup is found.
    """
    try:
        label_selector = f"{OPERATOR_LABEL}={OPERATOR_VALUE},resource-kind={kind},resource-name={name}"
        config_maps = core_v1.list_namespaced_config_map(namespace=namespace, label_selector=label_selector)

        if not config_maps.items:
            logger.info(f"No backup ConfigMaps found for {kind} {name}.")
            return None

        latest_cm = sorted(config_maps.items,
                          key=lambda cm: cm.metadata.creation_timestamp or datetime.datetime.min,
                          reverse=True)[0]
        key_name = f"{kind.lower()}-{name}"
        state_json = latest_cm.data.get(key_name) if latest_cm.data else None

        if state_json:
            logger.info(f"Found latest backup state for {kind} {name} in ConfigMap '{latest_cm.metadata.name}'.")
            return json.loads(state_json)
        else:
            logger.warning(f"Backup ConfigMap '{latest_cm.metadata.name}' has no data for key '{key_name}'.")
            return None

    except kubernetes.client.ApiException as e:
        logger.error(f"Could not find backup ConfigMaps for {kind} {name}: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse backup state JSON for {kind} {name}: {e}")
        return None


# --- State Getters ---

def get_deployment_state(namespace: str, name: str) -> Optional[Dict[str, Any]]:
    """Fetch the current replica count of a Deployment."""
    try:
        dep = apps_v1.read_namespaced_deployment(name, namespace)
        return {'replicas': dep.spec.replicas or 1}
    except kubernetes.client.ApiException as e:
        return None


def get_statefulset_state(namespace: str, name: str) -> Optional[Dict[str, Any]]:
    """Fetch the current replica count of a StatefulSet."""
    try:
        sts = apps_v1.read_namespaced_stateful_set(name, namespace)
        return {'replicas': sts.spec.replicas or 1}
    except kubernetes.client.ApiException as e:
        return None


def get_hpa_state(namespace: str, name: str) -> Optional[Dict[str, Any]]:
    """
    Fetch the entire specification of a HorizontalPodAutoscaler.

    This function reads the HPA and returns its full `spec` as a dictionary,
    which is necessary for perfect re-creation. It handles the case where the
    HPA might not exist (e.g., already deleted) by returning None.

    Args:
        namespace: The namespace of the HPA.
        name: The name of the HPA.

    Returns:
        A dictionary containing the HPA's spec, or None if the HPA is not found.
    """
    try:
        hpa = autoscaling_v2.read_namespaced_horizontal_pod_autoscaler(name, namespace)
        return {"spec": hpa.spec.to_dict()}
    except kubernetes.client.ApiException as e:
        if e.status == 404:
            return None  # HPA doesn't exist, which is a valid state.
        return None


def get_cronjob_state(namespace: str, name: str) -> Optional[Dict[str, Any]]:
    """Fetch the suspend status of a CronJob."""
    try:
        cj = batch_v1.read_namespaced_cron_job(name, namespace)
        return {'suspend': cj.spec.suspend or False}
    except kubernetes.client.ApiException as e:
        return None


# --- Scalers ---

def scale_deployment(logger: logging.Logger, namespace: str, name: str, direction: str,
                     state: Optional[Dict[str, Any]]) -> None:
    """Scale a Deployment up or down by patching its replica count."""
    try:
        replicas = 0 if direction == 'down' else (state.get('replicas', 1) if state else 1)
        patch = {'spec': {'replicas': replicas}}
        apps_v1.patch_namespaced_deployment(name, namespace, body=patch)
        logger.info(f"Patched Deployment {name} replicas to {replicas}.")
    except kubernetes.client.ApiException as e:
        if e.status == 403:
            logger.error(f"Permission denied patching Deployment {name} in namespace {namespace}. "
                        f"Ensure kubescaler-sa has 'patch' and 'update' permissions for deployments.")
        else:
            logger.error(f"Failed to scale Deployment {name}: {e}")


def scale_statefulset(logger: logging.Logger, namespace: str, name: str, direction: str,
                      state: Optional[Dict[str, Any]]) -> None:
    """Scale a StatefulSet up or down by patching its replica count."""
    try:
        replicas = 0 if direction == 'down' else (state.get('replicas', 1) if state else 1)
        patch = {'spec': {'replicas': replicas}}
        apps_v1.patch_namespaced_stateful_set(name, namespace, body=patch)
        logger.info(f"Patched StatefulSet {name} replicas to {replicas}.")
    except kubernetes.client.ApiException as e:
        logger.error(f"Failed to scale StatefulSet {name}: {e}")


def scale_hpa(logger: logging.Logger, namespace: str, name: str, direction: str,
              state: Optional[Dict[str, Any]]) -> None:
    """
    Delete a HorizontalPodAutoscaler on scale-down and re-create it on scale-up.

    This function implements a "true hibernation" for HPAs. Instead of patching,
    it completely removes the HPA during off-hours and then creates a new one
    from the backed-up specification during on-hours.

    Args:
        logger: The logger instance.
        namespace: The namespace of the HPA.
        name: The name of the HPA.
        direction: 'up' or 'down'.
        state: The backed-up state dictionary, required for 'up' direction.
    """
    if direction == 'down':
        try:
            logger.info(f"Deleting HPA {name} in namespace {namespace}.")
            autoscaling_v2.delete_namespaced_horizontal_pod_autoscaler(name, namespace)
        except kubernetes.client.ApiException as e:
            if e.status == 404:
                logger.warning(f"HPA {name} not found for deletion, already gone.")
            else:
                logger.error(f"Failed to delete HPA {name}: {e}")
    else:  # 'up'
        if not state or 'spec' not in state:
            logger.error(f"Cannot re-create HPA {name}, no valid state was backed up.")
            return

        hpa_body = {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": name, "namespace": namespace},
            "spec": state['spec']
        }
        try:
            logger.info(f"Re-creating HPA {name} in namespace {namespace}.")
            autoscaling_v2.create_namespaced_horizontal_pod_autoscaler(namespace, hpa_body)
        except kubernetes.client.ApiException as e:
            if e.status == 409:  # Conflict
                logger.warning(f"HPA {name} already exists. Skipping re-creation.")
            else:
                logger.error(f"Failed to re-create HPA {name}: {e}")


def scale_cronjob(logger: logging.Logger, namespace: str, name: str, direction: str,
                  state: Optional[Dict[str, Any]]) -> None:
    """Suspend or unsuspend a CronJob by patching its spec."""
    try:
        suspend = True if direction == 'down' else (state.get('suspend', False) if state else False)
        patch = {'spec': {'suspend': suspend}}
        batch_v1.patch_namespaced_cron_job(name, namespace, body=patch)
        logger.info(f"Patched CronJob {name} suspend to {suspend}.")
    except kubernetes.client.ApiException as e:
        logger.error(f"Failed to scale CronJob {name}: {e}")