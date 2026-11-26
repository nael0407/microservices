"""Asynchronous S3 Event Notification consumer for ticket audit logging."""

from datetime import datetime, timezone
import json
import urllib.parse

import boto3

_S3_CLIENT = None


def _get_s3():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        _S3_CLIENT = boto3.client("s3")
    return _S3_CLIENT


def handler(event: dict, _context) -> dict:
    """Process S3 ObjectCreated events on tickets/ and write audit records to audit/."""
    s3 = _get_s3()
    processed = 0

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        raw_key = record["s3"]["object"]["key"]
        key = urllib.parse.unquote_plus(raw_key)

        if not key.startswith("tickets/") or not key.endswith(".json"):
            continue

        resp = s3.get_object(Bucket=bucket, Key=key)
        ticket = json.loads(resp["Body"].read().decode("utf-8"))

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        audit_record = {
            "auditedAt": now_iso,
            "sourceKey": key,
            "eventName": record.get("eventName", "ObjectCreated"),
            "eventId": ticket.get("eventId"),
            "ticketId": ticket.get("ticketId"),
            "status": ticket.get("status"),
            "tier": ticket.get("tier"),
            "updatedAt": ticket.get("updatedAt"),
        }

        print(json.dumps({"audit": audit_record}))

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        audit_key = f"audit/{ticket.get('eventId', 'unknown')}/{stamp}-{ticket.get('ticketId', 'unknown')}.json"
        s3.put_object(
            Bucket=bucket,
            Key=audit_key,
            Body=json.dumps(audit_record).encode("utf-8"),
            ContentType="application/json",
        )
        processed += 1

    return {"processed": processed}

