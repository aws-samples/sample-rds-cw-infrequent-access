
import boto3
import os
import time
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment Variables ──────────────────────────────────────────────────────
TAG_KEY                 = os.environ.get("TAG_KEY", "rds-ia-migration").strip()
TAG_VALUE               = os.environ.get("TAG_VALUE", "true").strip()
LOG_CLASS               = os.environ.get("LOG_CLASS", "INFREQUENT_ACCESS").strip().upper()
LOG_RETENTION_DAYS_STR  = os.environ.get("LOG_RETENTION_DAYS", "").strip()
LOG_RETENTION_DAYS      = int(LOG_RETENTION_DAYS_STR) if LOG_RETENTION_DAYS_STR.isdigit() else None
LAMBDA_ARN              = os.environ.get("LAMBDA_ARN", "").strip()
VERIFY_DELAY_MINUTES    = int(os.environ.get("VERIFY_DELAY_MINUTES", "5"))

VALID_LOG_CLASSES = {"INFREQUENT_ACCESS", "STANDARD"}

CW_LOG_GROUP_PREFIX_INSTANCE = "/aws/rds/instance"
CW_LOG_GROUP_PREFIX_CLUSTER  = "/aws/rds/cluster"


def get_clients():
    session = boto3.session.Session()
    region  = session.region_name
    return {
        "rds":    boto3.client("rds",    region_name=region),
        "logs":   boto3.client("logs",   region_name=region),
        "events": boto3.client("events", region_name=region),
        "lambda": boto3.client("lambda", region_name=region),
        "region": region,
    }


# ── Tag-based Discovery ────────────────────────────────────────────────────────

def get_tagged_rds_resources(rds_client):
    """
    Discover all RDS instances and clusters tagged with TAG_KEY=TAG_VALUE.
    Returns a list of resource names to process.
    """
    resources = []

    # Scan DB instances
    paginator = rds_client.get_paginator("describe_db_instances")
    for page in paginator.paginate():
        for instance in page.get("DBInstances", []):
            tags = {t["Key"]: t["Value"] for t in instance.get("TagList", [])}
            if tags.get(TAG_KEY, "").lower() == TAG_VALUE.lower():
                resources.append(instance["DBInstanceIdentifier"])
                logger.info(f"Found tagged instance: {instance['DBInstanceIdentifier']}")

    # Scan DB clusters
    paginator = rds_client.get_paginator("describe_db_clusters")
    for page in paginator.paginate():
        for cluster in page.get("DBClusters", []):
            tags = {t["Key"]: t["Value"] for t in cluster.get("TagList", [])}
            if tags.get(TAG_KEY, "").lower() == TAG_VALUE.lower():
                resources.append(cluster["DBClusterIdentifier"])
                logger.info(f"Found tagged cluster: {cluster['DBClusterIdentifier']}")

    return resources


# ── RDS Helpers ────────────────────────────────────────────────────────────────

def describe_rds_resource(rds_client, name):
    try:
        resp     = rds_client.describe_db_instances(DBInstanceIdentifier=name)
        instance = resp["DBInstances"][0]
        return {
            "id":                instance["DBInstanceIdentifier"],
            "engine":            instance["Engine"],
            "is_cluster":        False,
            "current_log_types": instance.get("EnabledCloudwatchLogsExports", []),
        }
    except rds_client.exceptions.DBInstanceNotFoundFault:
        pass

    try:
        resp    = rds_client.describe_db_clusters(DBClusterIdentifier=name)
        cluster = resp["DBClusters"][0]
        return {
            "id":                cluster["DBClusterIdentifier"],
            "engine":            cluster["Engine"],
            "is_cluster":        True,
            "current_log_types": cluster.get("EnabledCloudwatchLogsExports", []),
        }
    except rds_client.exceptions.DBClusterNotFoundFault:
        pass

    raise ValueError(f"RDS resource '{name}' not found as instance or cluster.")


def disable_rds_logging(rds_client, resource):
    log_types = resource["current_log_types"]
    if not log_types:
        logger.info(f"[{resource['id']}] No CloudWatch log exports currently enabled.")
        return
    cfg = {"CloudwatchLogsExportConfiguration": {"DisableLogTypes": log_types}}
    if resource["is_cluster"]:
        rds_client.modify_db_cluster(
            DBClusterIdentifier=resource["id"], **cfg, ApplyImmediately=True
        )
    else:
        rds_client.modify_db_instance(
            DBInstanceIdentifier=resource["id"], **cfg, ApplyImmediately=True
        )
    logger.info(f"[{resource['id']}] Disabled log types: {log_types}")


def get_log_group_names(logs_client, resource):
    prefix = (
        f"{CW_LOG_GROUP_PREFIX_CLUSTER}/{resource['id']}"
        if resource["is_cluster"]
        else f"{CW_LOG_GROUP_PREFIX_INSTANCE}/{resource['id']}"
    )
    log_groups = []
    paginator  = logs_client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=prefix):
        for lg in page.get("logGroups", []):
            log_groups.append(lg["logGroupName"])
    return log_groups


def delete_log_group(logs_client, log_group_name, resource_id):
    try:
        logs_client.delete_log_group(logGroupName=log_group_name)
        logger.info(f"[{resource_id}] Deleted log group: {log_group_name}")
    except logs_client.exceptions.ResourceNotFoundException:
        logger.warning(f"[{resource_id}] Log group not found: {log_group_name}")


def create_log_group(logs_client, log_group_name, resource_id):
    try:
        logs_client.create_log_group(
            logGroupName=log_group_name,
            logGroupClass=LOG_CLASS,
        )
        logger.info(f"[{resource_id}] Created {LOG_CLASS} log group: {log_group_name}")
    except logs_client.exceptions.ResourceAlreadyExistsException:
        logger.warning(f"[{resource_id}] Log group already exists: {log_group_name}")

    if LOG_RETENTION_DAYS:
        logs_client.put_retention_policy(
            logGroupName=log_group_name,
            retentionInDays=LOG_RETENTION_DAYS,
        )
        logger.info(f"[{resource_id}] Set retention: {LOG_RETENTION_DAYS} days on {log_group_name}")


# ── EventBridge Scheduling ─────────────────────────────────────────────────────

def schedule_reenable_lambda(clients, results):
    """
    Create a one-time EventBridge rule that fires VERIFY_DELAY_MINUTES from now
    and triggers Lambda #2 with the original log types per resource.
    """
    events_client = clients["events"]
    lambda_client = clients["lambda"]

    trigger_time = time.gmtime(time.time() + VERIFY_DELAY_MINUTES * 60)
    cron_expr    = (
        f"cron({trigger_time.tm_min} {trigger_time.tm_hour} "
        f"{trigger_time.tm_mday} {trigger_time.tm_mon} ? {trigger_time.tm_year})"
    )

    rule_name = f"rds-log-migration-{int(time.time())}"

    rule_response = events_client.put_rule(
        Name=rule_name,
        ScheduleExpression=cron_expr,
        State="ENABLED",
        Description=f"One-time rule to re-enable RDS logging after {LOG_CLASS} log group migration.",
    )
    rule_arn = rule_response["RuleArn"]
    logger.info(f"Created EventBridge rule '{rule_name}' with schedule: {cron_expr}")

    # Build map of resource ID -> original log types to pass to Lambda #2
    log_types_map = {
        r["resource"]: r["log_types_to_restore"]
        for r in results
    }
    logger.info(f"Passing log_types_map to Lambda #2: {log_types_map}")

    payload = {
        "rule_name":     rule_name,
        "log_types_map": log_types_map,
        "log_class":     LOG_CLASS,
    }

    events_client.put_targets(
        Rule=rule_name,
        Targets=[
            {
                "Id":    "ReEnableLoggingLambda",
                "Arn":   LAMBDA_ARN,
                "Input": json.dumps(payload),
            }
        ],
    )

    try:
        lambda_client.add_permission(
            FunctionName=LAMBDA_ARN,
            StatementId=f"allow-eb-{rule_name}",
            Action="lambda:InvokeFunction",
            Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
        logger.info(f"Granted EventBridge permission to invoke Lambda #2 for rule '{rule_name}'")
    except lambda_client.exceptions.ResourceConflictException:
        logger.warning("Lambda invoke permission already exists — skipping.")

    return rule_name


# ── Main Processing ────────────────────────────────────────────────────────────

def process_rds_resource(clients, name):
    rds_client  = clients["rds"]
    logs_client = clients["logs"]

    logger.info(f"{'='*60}")
    logger.info(f"Processing: {name}")

    resource           = describe_rds_resource(rds_client, name)
    original_log_types = list(resource["current_log_types"])

    logger.info(f"[{name}] Engine: {resource['engine']}, Cluster: {resource['is_cluster']}")
    logger.info(f"[{name}] Original log types to restore: {original_log_types}")
    logger.info(f"[{name}] Target log group class: {LOG_CLASS}")

    # Step 1: Disable logging
    disable_rds_logging(rds_client, resource)

    # Step 2: Delete and recreate log groups with the target LOG_CLASS
    log_group_names = get_log_group_names(logs_client, resource)
    logger.info(f"[{name}] Log groups found: {log_group_names}")

    for log_group_name in log_group_names:
        delete_log_group(logs_client, log_group_name, name)
        create_log_group(logs_client, log_group_name, name)

    logger.info(f"[{name}] Phase 1 complete. Lambda #2 will re-enable logging.")

    return {
        "resource":             resource["id"],
        "engine":               resource["engine"],
        "is_cluster":           resource["is_cluster"],
        "log_groups_migrated":  log_group_names,
        "log_types_to_restore": original_log_types,
    }


# ── Lambda Handler ─────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    if LOG_CLASS not in VALID_LOG_CLASSES:
        raise ValueError(f"Invalid LOG_CLASS '{LOG_CLASS}'. Must be one of: {VALID_LOG_CLASSES}")
    if not LAMBDA_ARN:
        raise ValueError("LAMBDA_ARN environment variable is not set.")

    clients    = get_clients()
    rds_client = clients["rds"]

    # Discover tagged RDS resources
    rds_names = get_tagged_rds_resources(rds_client)
    if not rds_names:
        logger.warning(f"No RDS resources found with tag {TAG_KEY}={TAG_VALUE}. Exiting.")
        return {
            "total_processed": 0,
            "total_succeeded": 0,
            "total_failed":    0,
            "succeeded":       [],
            "failed":          [],
            "message":         f"No RDS resources found with tag {TAG_KEY}={TAG_VALUE}.",
        }

    logger.info(f"Discovered {len(rds_names)} tagged RDS resource(s): {rds_names}")

    results = []
    errors  = []

    with ThreadPoolExecutor(max_workers=len(rds_names)) as executor:
        future_to_name = {
            executor.submit(process_rds_resource, clients, name): name
            for name in rds_names
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"[{name}] Error: {str(e)}", exc_info=True)
                errors.append({"resource": name, "error": str(e)})

    # Schedule Lambda #2 to re-enable logging after VERIFY_DELAY_MINUTES
    rule_name = schedule_reenable_lambda(clients, results)
    logger.info(f"Scheduled Lambda #2 via EventBridge rule: {rule_name}")

    summary = {
        "total_processed":  len(rds_names),
        "total_succeeded":  len(results),
        "total_failed":     len(errors),
        "succeeded":        results,
        "failed":           errors,
        "eventbridge_rule": rule_name,
    }
    logger.info(f"Lambda #1 complete. Summary: {summary}")
    return summary

