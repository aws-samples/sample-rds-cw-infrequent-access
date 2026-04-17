import boto3
import os
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── Environment Variables ──────────────────────────────────────────────────────
SES_SENDER_EMAIL    = os.environ.get("SES_SENDER_EMAIL", "").strip()
SES_RECIPIENT_EMAIL = os.environ.get("SES_RECIPIENT_EMAIL", "").strip()

CW_LOG_GROUP_PREFIX_INSTANCE = "/aws/rds/instance"
CW_LOG_GROUP_PREFIX_CLUSTER  = "/aws/rds/cluster"

POLL_INTERVAL_SECONDS = 15
POLL_MAX_ATTEMPTS     = 40  # 15s * 40 = 10 min max wait per resource


def get_clients():
    session = boto3.session.Session()
    region  = session.region_name
    return {
        "rds":    boto3.client("rds",    region_name=region),
        "logs":   boto3.client("logs",   region_name=region),
        "ses":    boto3.client("ses",    region_name=region),
        "events": boto3.client("events", region_name=region),
    }


def describe_rds_resource(rds_client, name):
    try:
        resp     = rds_client.describe_db_instances(DBInstanceIdentifier=name)
        instance = resp["DBInstances"][0]
        return {
            "id":         instance["DBInstanceIdentifier"],
            "engine":     instance["Engine"],
            "is_cluster": False,
            "status":     instance["DBInstanceStatus"],
        }
    except rds_client.exceptions.DBInstanceNotFoundFault:
        pass

    try:
        resp    = rds_client.describe_db_clusters(DBClusterIdentifier=name)
        cluster = resp["DBClusters"][0]
        return {
            "id":         cluster["DBClusterIdentifier"],
            "engine":     cluster["Engine"],
            "is_cluster": True,
            "status":     cluster["Status"],
        }
    except rds_client.exceptions.DBClusterNotFoundFault:
        pass

    raise ValueError(f"RDS resource '{name}' not found as instance or cluster.")


def wait_for_available(rds_client, name):
    """Poll until the resource status is 'available'."""
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        resource = describe_rds_resource(rds_client, name)
        status = resource["status"]
        if status == "available":
            logger.info(f"[{name}] Status is 'available' (attempt {attempt})")
            return resource
        logger.info(f"[{name}] Status: '{status}', waiting... (attempt {attempt}/{POLL_MAX_ATTEMPTS})")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"[{name}] Still not 'available' after {POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS}s")


def enable_rds_logging(rds_client, resource, log_types):
    if not log_types:
        logger.info(f"[{resource['id']}] No log types to re-enable.")
        return
    cfg = {"CloudwatchLogsExportConfiguration": {"EnableLogTypes": log_types}}
    if resource["is_cluster"]:
        rds_client.modify_db_cluster(
            DBClusterIdentifier=resource["id"], **cfg, ApplyImmediately=True
        )
    else:
        rds_client.modify_db_instance(
            DBInstanceIdentifier=resource["id"], **cfg, ApplyImmediately=True
        )
    logger.info(f"[{resource['id']}] Re-enabled log types: {log_types}")


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


def verify_log_group_class(logs_client, log_group_name, expected_class):
    paginator = logs_client.get_paginator("describe_log_groups")
    for page in paginator.paginate(logGroupNamePrefix=log_group_name):
        for lg in page.get("logGroups", []):
            if lg["logGroupName"] == log_group_name:
                return lg.get("logGroupClass") == expected_class
    return False


def cleanup_eventbridge_rule(events_client, rule_name):
    try:
        events_client.remove_targets(Rule=rule_name, Ids=["ReEnableLoggingLambda"])
        events_client.delete_rule(Name=rule_name)
        logger.info(f"Deleted one-time EventBridge rule: {rule_name}")
    except Exception as e:
        logger.warning(f"Could not clean up EventBridge rule '{rule_name}': {e}")


def process_rds_resource(clients, name, log_types, log_class):
    rds_client  = clients["rds"]
    logs_client = clients["logs"]

    logger.info(f"{'='*60}")
    logger.info(f"Re-enabling logging for: {name}")

    if not log_types:
        logger.warning(f"[{name}] No log types found in event payload — skipping re-enable.")

    # Poll until this specific resource is available
    resource = wait_for_available(rds_client, name)

    enable_rds_logging(rds_client, resource, log_types)

    log_group_names = get_log_group_names(logs_client, resource)
    verification    = {
        lg: verify_log_group_class(logs_client, lg, log_class)
        for lg in log_group_names
    }
    all_correct = all(verification.values()) if verification else False

    logger.info(f"[{name}] Log group class verification: {verification}")
    return {
        "resource":               name,
        "engine":                 resource["engine"],
        "is_cluster":             resource["is_cluster"],
        "log_types_enabled":      log_types,
        "log_group_verification": verification,
        "log_class":              log_class,
        "all_correct":            all_correct,
        "success":                True,
    }


def send_completion_email(ses_client, results, errors, log_class):
    if not SES_SENDER_EMAIL or not SES_RECIPIENT_EMAIL:
        logger.warning("SES_SENDER_EMAIL or SES_RECIPIENT_EMAIL not set — skipping email.")
        return

    logger.info(f"Attempting to send email from {SES_SENDER_EMAIL} to {SES_RECIPIENT_EMAIL}")

    all_ok  = not errors and all(r.get("success") and r.get("all_correct") for r in results)
    subject = (
        f"✅ RDS CloudWatch Log Group Migration to {log_class} — Completed Successfully"
        if all_ok
        else f"⚠️ RDS CloudWatch Log Group Migration to {log_class} — Completed with Errors"
    )

    rows = ""
    for r in results:
        status    = "✅ Success" if r.get("success") and r.get("all_correct") else "❌ Failed"
        lg_rows   = "".join(
            f"{lg}: {'✅ ' + log_class if ok else '❌ Wrong class'}<br>"
            for lg, ok in r.get("log_group_verification", {}).items()
        )
        log_types = ", ".join(r.get("log_types_enabled", [])) or "None"
        rows += f"""
        <tr>
          <td>{r['resource']}</td>
          <td>{r['engine']}</td>
          <td>{'Cluster' if r['is_cluster'] else 'Instance'}</td>
          <td>{lg_rows}</td>
          <td>{log_types}</td>
          <td>{status}</td>
        </tr>"""

    error_rows = ""
    for e in errors:
        error_rows += (
            f"<tr><td colspan='6' style='color:red;'>"
            f"<strong>{e['resource']}</strong>: {e['error']}</td></tr>"
        )

    body_html = f"""
    <html>
    <body>
      <h2>RDS CloudWatch Log Group Migration — {log_class}</h2>
      <p>The migration has completed. RDS logging has been re-enabled and log groups verified.</p>
      <table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;'>
        <tr style='background-color:#f2f2f2;'>
          <th>Resource</th>
          <th>Engine</th>
          <th>Type</th>
          <th>Log Groups (Class Verified)</th>
          <th>Log Types Re-enabled</th>
          <th>Status</th>
        </tr>
        {rows}
        {error_rows}
      </table>
      <br>
      <p style='color:grey;font-size:12px;'>
        This email was sent automatically by the RDS Log Group Migration Lambda.
      </p>
    </body>
    </html>
    """

    try:
        ses_client.send_email(
            Source=SES_SENDER_EMAIL,
            Destination={"ToAddresses": [SES_RECIPIENT_EMAIL]},
            Message={
                "Subject": {"Data": subject},
                "Body":    {"Html": {"Data": body_html}},
            },
        )
        logger.info(f"✅ Completion email sent successfully to {SES_RECIPIENT_EMAIL}")
    except Exception as e:
        logger.error(f"❌ Failed to send SES email: {str(e)}", exc_info=True)


def lambda_handler(event, context):
    clients       = get_clients()
    events_client = clients["events"]
    results       = []
    errors        = []

    # Read context passed from Lambda #1 via EventBridge
    log_types_map = event.get("log_types_map", {})
    log_class     = event.get("log_class", "INFREQUENT_ACCESS")
    rule_name     = event.get("rule_name")

    logger.info(f"Received log_types_map: {log_types_map}")
    logger.info(f"Target log class: {log_class}")

    if not log_types_map:
        logger.error("No log_types_map received in event — nothing to process.")
        return {"error": "No log_types_map in event payload."}

    resource_names = list(log_types_map.keys())
    logger.info(f"Resources to re-enable logging for: {resource_names}")

    # Process resources in parallel — each thread polls its own resource independently
    with ThreadPoolExecutor(max_workers=len(resource_names)) as executor:
        future_to_name = {
            executor.submit(
                process_rds_resource,
                clients,
                name,
                next(
                    (v for k, v in log_types_map.items() if k.lower() == name.lower()),
                    []
                ),
                log_class,
            ): name
            for name in resource_names
        }
        for future in as_completed(future_to_name):
            name = future_to_name[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                logger.error(f"[{name}] Error: {str(e)}", exc_info=True)
                errors.append({"resource": name, "error": str(e), "success": False})

    send_completion_email(clients["ses"], results, errors, log_class)

    # Self-cleanup: delete the one-time EventBridge rule that triggered this Lambda
    if rule_name:
        cleanup_eventbridge_rule(events_client, rule_name)

    summary = {
        "total_processed": len(resource_names),
        "total_succeeded": len(results),
        "total_failed":    len(errors),
        "succeeded":       results,
        "failed":          errors,
    }
    logger.info(f"Lambda #2 complete. Summary: {summary}")
    return summary
