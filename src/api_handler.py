"""AWS Lambda handler for Event Ticketing & QR Check-in (Lambda Function URL + S3)."""

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
import re
import uuid

import boto3
from botocore.exceptions import ClientError

from src.ticket_crypto import sign_ticket, verify_signature

VALID_TIERS = {"REG", "VIP"}
ACTIVE_STATUSES = {"reserved", "paid", "checked-in"}
FINAL_STATUSES = {"checked-in", "cancelled", "expired"}

_S3_CLIENT = None


@dataclass(frozen=True)
class AppConfig:
    """Runtime settings loaded from Lambda environment variables."""

    bucket_name: str
    event_open: bool
    event_quota: int
    pay_window_minutes: int
    admin_api_key: str
    hmac_secret: str


class ApiError(Exception):
    """Structured API error mapped to JSON HTTP responses."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _get_s3():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        _S3_CLIENT = boto3.client("s3")
    return _S3_CLIENT


def _load_config() -> AppConfig:
    return AppConfig(
        bucket_name=os.environ.get("BUCKET_NAME", ""),
        event_open=os.environ.get("EVENT_OPEN", "true").strip().lower() in {"1", "true", "yes"},
        event_quota=int(os.environ.get("EVENT_QUOTA", "50")),
        pay_window_minutes=int(os.environ.get("PAY_WINDOW_MINUTES", "30")),
        admin_api_key=os.environ.get("ADMIN_API_KEY", "dev-admin-key"),
        hmac_secret=os.environ.get("HMAC_SECRET", "dev-hmac-secret"),
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _json_response(status_code: int, payload: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _error_response(status_code: int, code: str, message: str) -> dict:
    return _json_response(status_code, {"error": code, "message": message})


def _get_header(headers: dict, name: str) -> str | None:
    target = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == target:
            return v
    return None


def _parse_body(event: dict) -> dict:
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ApiError(400, "VALIDATION_ERROR", "Body harus berupa JSON yang valid") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "VALIDATION_ERROR", "Body JSON harus berupa object")
    return data


def _ticket_key(event_id: str, ticket_id: str) -> str:
    return f"tickets/{event_id}/{ticket_id}.json"


def _get_ticket_with_etag(s3, bucket: str, key: str) -> tuple[dict | None, str | None]:
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        data = json.loads(resp["Body"].read().decode("utf-8"))
        etag = resp["ETag"]
        return data, etag
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404"}:
            return None, None
        raise


def _put_ticket_if_match(s3, bucket: str, key: str, ticket: dict, etag: str) -> None:
    try:
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(ticket).encode("utf-8"),
            ContentType="application/json",
            IfMatch=etag,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"PreconditionFailed", "412"}:
            raise ApiError(
                409,
                "CONCURRENT_MODIFICATION",
                "Tiket sedang diubah oleh permintaan lain, silakan coba lagi",
            ) from exc
        raise


def _count_active_tickets(s3, bucket: str, event_id: str) -> int:
    paginator = s3.get_paginator("list_objects_v2")
    active = 0
    for page in paginator.paginate(Bucket=bucket, Prefix=f"tickets/{event_id}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".json"):
                continue
            ticket, _ = _get_ticket_with_etag(s3, bucket, key)
            if ticket and ticket.get("status") in ACTIVE_STATUSES:
                active += 1
    return active


def _handle_health(s3, cfg: AppConfig) -> dict:
    if not cfg.bucket_name:
        return _json_response(503, {"status": "error", "storage": "BUCKET_NAME belum diset"})
    try:
        s3.head_bucket(Bucket=cfg.bucket_name)
        return _json_response(200, {"status": "ok", "storage": "s3", "bucket": cfg.bucket_name})
    except Exception as exc:
        return _json_response(503, {"status": "error", "storage": str(exc)})


def _handle_create_ticket(s3, cfg: AppConfig, event_id: str, headers: dict, body: dict) -> dict:
    if not cfg.event_open:
        raise ApiError(409, "EVENT_CLOSED", "Penjualan tiket acara sedang ditutup")

    holder_name = str(body.get("holderName", "")).strip()
    holder_contact = str(body.get("holderContact", "")).strip()
    tier = str(body.get("tier", "")).strip().upper()

    if not holder_name or len(holder_name) > 100:
        raise ApiError(400, "VALIDATION_ERROR", "holderName wajib diisi (maksimal 100 karakter)")
    if not holder_contact.isdigit() or not (6 <= len(holder_contact) <= 32):
        raise ApiError(400, "VALIDATION_ERROR", "holderContact harus berupa angka (6-32 digit)")
    if tier not in VALID_TIERS:
        raise ApiError(400, "VALIDATION_ERROR", "tier harus REG atau VIP")

    idem_key = (_get_header(headers, "Idempotency-Key") or "").strip()
    ticket_id = idem_key or str(uuid.uuid4())
    if len(ticket_id) > 64 or "/" in ticket_id:
        raise ApiError(400, "VALIDATION_ERROR", "Idempotency-Key maksimal 64 karakter dan tanpa '/'")

    key = _ticket_key(event_id, ticket_id)
    existing, _ = _get_ticket_with_etag(s3, cfg.bucket_name, key)
    if existing is not None:
        return _json_response(200, existing)

    if _count_active_tickets(s3, cfg.bucket_name, event_id) >= cfg.event_quota:
        raise ApiError(409, "QUOTA_EXCEEDED", "Kuota tiket acara sudah penuh")

    now = _utc_now()
    expires_at = now + timedelta(minutes=cfg.pay_window_minutes)
    signature = sign_ticket(cfg.hmac_secret, event_id, ticket_id, holder_contact)

    ticket = {
        "ticketId": ticket_id,
        "eventId": event_id,
        "holderName": holder_name,
        "holderContact": holder_contact,
        "tier": tier,
        "status": "reserved",
        "signature": signature,
        "expiresAt": _iso_utc(expires_at),
        "checkedInAt": None,
        "createdAt": _iso_utc(now),
        "updatedAt": _iso_utc(now),
    }

    try:
        s3.put_object(
            Bucket=cfg.bucket_name,
            Key=key,
            Body=json.dumps(ticket).encode("utf-8"),
            ContentType="application/json",
            IfNoneMatch="*",
        )
        return _json_response(201, ticket)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"PreconditionFailed", "412"}:
            existing_after, _ = _get_ticket_with_etag(s3, cfg.bucket_name, key)
            if existing_after is not None:
                return _json_response(200, existing_after)
        raise


def _handle_list_tickets(s3, cfg: AppConfig, event_id: str, query_params: dict) -> dict:
    raw_limit = (query_params or {}).get("limit", "50")
    try:
        limit = int(raw_limit)
    except ValueError as exc:
        raise ApiError(400, "VALIDATION_ERROR", "limit harus berupa angka") from exc
    if not (1 <= limit <= 100):
        raise ApiError(400, "VALIDATION_ERROR", "limit harus di antara 1 dan 100")

    resp = s3.list_objects_v2(
        Bucket=cfg.bucket_name,
        Prefix=f"tickets/{event_id}/",
        MaxKeys=limit,
    )
    tickets = []
    for obj in resp.get("Contents", []):
        if not obj["Key"].endswith(".json"):
            continue
        ticket, _ = _get_ticket_with_etag(s3, cfg.bucket_name, obj["Key"])
        if ticket is not None:
            tickets.append(ticket)

    tickets.sort(key=lambda t: t.get("createdAt", ""), reverse=True)
    return _json_response(200, {"eventId": event_id, "tickets": tickets})


def _handle_get_ticket(s3, cfg: AppConfig, event_id: str, ticket_id: str) -> dict:
    ticket, _ = _get_ticket_with_etag(s3, cfg.bucket_name, _ticket_key(event_id, ticket_id))
    if ticket is None:
        raise ApiError(404, "NOT_FOUND", "Tiket tidak ditemukan")
    return _json_response(200, ticket)


def _handle_update_ticket(
    s3,
    cfg: AppConfig,
    event_id: str,
    ticket_id: str,
    headers: dict,
    body: dict,
) -> dict:
    new_name = body.get("holderName")
    target_status = body.get("status")

    if new_name is None and target_status is None:
        raise ApiError(400, "VALIDATION_ERROR", "Body harus mengisi holderName atau status")
    if new_name is not None and target_status is not None:
        raise ApiError(400, "VALIDATION_ERROR", "Ubah holderName dan status secara terpisah")

    if target_status is not None:
        if target_status not in {"paid", "checked-in"}:
            raise ApiError(400, "VALIDATION_ERROR", "status hanya boleh paid atau checked-in")
        api_key = _get_header(headers, "X-API-Key")
        if api_key != cfg.admin_api_key:
            raise ApiError(401, "UNAUTHORIZED", "X-API-Key panitia tidak valid")

    key = _ticket_key(event_id, ticket_id)
    ticket, etag = _get_ticket_with_etag(s3, cfg.bucket_name, key)
    if ticket is None or etag is None:
        raise ApiError(404, "NOT_FOUND", "Tiket tidak ditemukan")

    now = _utc_now()
    current_status = ticket["status"]

    if new_name is not None:
        cleaned = str(new_name).strip()
        if not cleaned or len(cleaned) > 100:
            raise ApiError(400, "VALIDATION_ERROR", "holderName wajib diisi (maksimal 100 karakter)")
        if current_status != "reserved":
            raise ApiError(409, "INVALID_TRANSITION", "holderName hanya bisa diubah saat status reserved")
        ticket["holderName"] = cleaned
    elif target_status == "paid":
        if current_status != "reserved":
            raise ApiError(409, "INVALID_TRANSITION", "Tiket harus berstatus reserved")
        expires_at = _parse_iso(ticket["expiresAt"])
        if now > expires_at:
            ticket["status"] = "expired"
            ticket["updatedAt"] = _iso_utc(now)
            _put_ticket_if_match(s3, cfg.bucket_name, key, ticket, etag)
            raise ApiError(409, "TICKET_EXPIRED", "Tiket sudah melewati batas waktu pembayaran")
        ticket["status"] = "paid"
    elif target_status == "checked-in":
        if current_status != "paid":
            raise ApiError(409, "INVALID_TRANSITION", "Tiket harus berstatus paid untuk check-in")
        sig = str(body.get("signature", "")).strip()
        if not verify_signature(cfg.hmac_secret, event_id, ticket_id, ticket["holderContact"], sig):
            raise ApiError(403, "INVALID_SIGNATURE", "Signature tiket tidak valid")
        ticket["status"] = "checked-in"
        ticket["checkedInAt"] = _iso_utc(now)

    ticket["updatedAt"] = _iso_utc(now)
    _put_ticket_if_match(s3, cfg.bucket_name, key, ticket, etag)
    return _json_response(200, ticket)


def _handle_delete_ticket(s3, cfg: AppConfig, event_id: str, ticket_id: str) -> dict:
    key = _ticket_key(event_id, ticket_id)
    ticket, etag = _get_ticket_with_etag(s3, cfg.bucket_name, key)
    if ticket is None or etag is None:
        raise ApiError(404, "NOT_FOUND", "Tiket tidak ditemukan")

    if ticket["status"] in FINAL_STATUSES:
        raise ApiError(
            409,
            "INVALID_TRANSITION",
            f"Tiket sudah berstatus akhir ({ticket['status']})",
        )

    now = _utc_now()
    ticket["status"] = "cancelled"
    ticket["updatedAt"] = _iso_utc(now)
    _put_ticket_if_match(s3, cfg.bucket_name, key, ticket, etag)
    return _json_response(200, ticket)


def handler(event: dict, _context) -> dict:
    """Entry point for Lambda Function URL HTTP requests."""
    cfg = _load_config()
    s3 = _get_s3()

    http_ctx = (event.get("requestContext") or {}).get("http") or {}
    method = (http_ctx.get("method") or event.get("httpMethod") or "GET").upper()
    path = (event.get("rawPath") or event.get("path") or "/").rstrip("/") or "/"
    headers = event.get("headers") or {}
    query_params = event.get("queryStringParameters") or {}

    try:
        if method == "GET" and path == "/health":
            return _handle_health(s3, cfg)

        if not cfg.bucket_name:
            raise ApiError(503, "CONFIG_ERROR", "BUCKET_NAME belum dikonfigurasi")

        collection_match = re.fullmatch(r"/events/([^/]+)/tickets", path)
        if collection_match:
            event_id = collection_match.group(1)
            if method == "POST":
                return _handle_create_ticket(s3, cfg, event_id, headers, _parse_body(event))
            if method == "GET":
                return _handle_list_tickets(s3, cfg, event_id, query_params)

        item_match = re.fullmatch(r"/events/([^/]+)/tickets/([^/]+)", path)
        if item_match:
            event_id, ticket_id = item_match.group(1), item_match.group(2)
            if method == "GET":
                return _handle_get_ticket(s3, cfg, event_id, ticket_id)
            if method == "PUT":
                return _handle_update_ticket(s3, cfg, event_id, ticket_id, headers, _parse_body(event))
            if method == "DELETE":
                return _handle_delete_ticket(s3, cfg, event_id, ticket_id)

        return _error_response(404, "NOT_FOUND", f"Route {method} {path} tidak ditemukan")
    except ApiError as exc:
        return _error_response(exc.status_code, exc.code, exc.message)
    except Exception as exc:
        print(f"Unhandled error: {exc!r}")
        return _error_response(500, "INTERNAL_ERROR", "Terjadi kesalahan internal server")

