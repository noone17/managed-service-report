"""
CUR (Cost and Usage Report) S3 import service.

Reads CUR CSV/GZIP files from an S3 bucket and aggregates monthly costs
per account. Used when Cost Explorer returns $0 for historical months
(typically after a management/payer account change).

CUR FILE STRUCTURE
------------------
AWS writes CUR files to S3 in this layout:

  <prefix>/<report-name>/<YYYYMMDD-YYYYMMDD>/
      <report-name>-Manifest.json
      <report-name>-1.csv.gz   (or .csv / .parquet)
      <report-name>-2.csv.gz
      ...

The date range in the folder name is the billing period, e.g.:
  wealwin/20250101-20250201/

KEY COLUMNS USED
----------------
  bill_billing_period_start_date   → month (YYYY-MM-01)
  line_item_usage_account_id       → 12-digit account ID (filter)
  line_item_line_item_type         → Usage, Tax, Fee, Credit, etc.
  bill_billing_entity              → "AWS" or "AWS Marketplace" etc.
  line_item_unblended_cost         → actual cost amount

MARKETPLACE DETECTION
---------------------
  bill_billing_entity != 'AWS'         → marketplace cost
  bill_billing_entity == 'AWS'         → cloud service cost

CREDENTIALS
-----------
Uses the same IAM credentials as the active payer.
The IAM user/role needs these additional permissions:
  s3:GetObject   on  arn:aws:s3:::<bucket>/<prefix>*
  s3:ListBucket  on  arn:aws:s3:::<bucket>

USAGE
-----
  from app.cur_service import import_cur_for_account
  results = import_cur_for_account(account, from_month="2024-01", to_month="2024-12")
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import logging
from collections import defaultdict
from datetime import date
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError as BotoClientError

if TYPE_CHECKING:
    from app.models import AwsAccount

log = logging.getLogger(__name__)

# CUR line-item types that represent actual usage charges
_USAGE_TYPES = {"Usage", "DiscountedUsage", "SavingsPlanCoveredUsage",
                "SavingsPlanRecurringFee", "Fee", "RIFee"}

# Billing entities that indicate AWS Marketplace (not native AWS)
_MARKETPLACE_ENTITIES = {"AWS Marketplace", "AWS Marketplace, Inc."}


def _is_marketplace(billing_entity: str) -> bool:
    """Return True if the billing entity is AWS Marketplace."""
    if not billing_entity:
        return False
    be = billing_entity.strip()
    return be in _MARKETPLACE_ENTITIES or "marketplace" in be.lower()


def _month_key(period_start: str) -> str:
    """
    Convert bill_billing_period_start_date to YYYY-MM-01 string.
    Handles formats: 2025-01-01 00:00:00 UTC, 2025-01-01T00:00:00Z, 2025-01-01
    """
    if not period_start:
        return ""
    ds = period_start.strip().split("T")[0].split(" ")[0]  # get date part
    parts = ds.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}-01"
    return ""


def _get_s3_client(account: "AwsAccount", region: str):
    """Build an S3 boto3 client using the account's own IAM credentials."""
    if account._access_key_id_enc:
        ak = account.get_access_key_id()
        sk = account.get_secret_access_key()
    else:
        raise ValueError(
            f"No credentials found for account '{account.name}'. "
            "Add IAM credentials to use CUR import."
        )

    session = boto3.Session(
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        region_name=region or "us-east-1",
    )
    ak = sk = None  # clear from memory immediately
    return session.client("s3")


def _list_cur_files(s3, bucket: str, prefix: str, linked_account_id: str | None) -> list[dict]:
    """
    List all CUR CSV/GZIP files under the given S3 prefix.

    Returns a list of dicts:
      { "key": str, "billing_period": "YYYY-MM" }

    CUR layout under prefix:
      <prefix>/<YYYYMMDD-YYYYMMDD>/<report-files>.csv.gz
    OR (Data Exports / CUR 2.0):
      <prefix>/data/<YYYYMMDD-YYYYMMDD>/<files>.csv.gz
    """
    prefix = prefix.rstrip("/") + "/"
    found = []

    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                lower = key.lower()
                # Skip manifest and non-data files
                if "manifest" in lower:
                    continue
                if not (lower.endswith(".csv.gz") or lower.endswith(".csv")
                        or lower.endswith(".gz")):
                    continue
                # Extract billing period from the folder name (YYYYMMDD-YYYYMMDD)
                parts = key.split("/")
                billing_period = ""
                for part in parts:
                    # Matches patterns like 20250101-20250201 or 20250101-20250131
                    if len(part) == 17 and part[8] == "-" and part[:8].isdigit():
                        year  = part[:4]
                        month = part[4:6]
                        billing_period = f"{year}-{month}"
                        break
                found.append({"key": key, "billing_period": billing_period})

    except BotoClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"]["Message"]
        raise ValueError(f"S3 error listing CUR files [{code}]: {msg}") from exc

    log.info("[CUR] Found %d file(s) under s3://%s/%s", len(found), bucket, prefix)
    return found


def _parse_cur_file(s3, bucket: str, key: str,
                    linked_account_id: str | None) -> dict[str, dict]:
    """
    Download and parse a single CUR CSV(.gz) file.

    Returns a dict keyed by month string (YYYY-MM-01):
      {
        "2025-01-01": {
          "cloud_service_cost": float,
          "marketplace_cost":   float,
        },
        ...
      }
    """
    log.info("[CUR] Parsing s3://%s/%s", bucket, key)
    try:
        resp = s3.get_object(Bucket=bucket, Key=key)
        raw  = resp["Body"].read()
    except BotoClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"]["Message"]
        raise ValueError(f"S3 error downloading {key} [{code}]: {msg}") from exc

    # Decompress if gzip
    if key.lower().endswith(".gz"):
        raw = gzip.decompress(raw)

    text    = raw.decode("utf-8-sig", errors="replace")
    reader  = csv.DictReader(io.StringIO(text))

    # Normalise header names — CUR 1.0 uses camelCase, CUR 2.0 uses snake_case
    # We map both to a common set of keys.
    month_data: dict[str, dict] = defaultdict(
        lambda: {"cloud_service_cost": 0.0, "marketplace_cost": 0.0}
    )

    rows_parsed = 0
    for row in reader:
        # ── Resolve column names (CUR 1.0 vs CUR 2.0) ────────────────────────
        period_start = (
            row.get("bill/BillingPeriodStartDate")
            or row.get("bill_billing_period_start_date")
            or ""
        )
        acct_id = (
            row.get("lineItem/UsageAccountId")
            or row.get("line_item_usage_account_id")
            or ""
        ).strip()
        item_type = (
            row.get("lineItem/LineItemType")
            or row.get("line_item_line_item_type")
            or ""
        ).strip()
        billing_entity = (
            row.get("bill/BillingEntity")
            or row.get("bill_billing_entity")
            or "AWS"
        ).strip()
        cost_str = (
            row.get("lineItem/UnblendedCost")
            or row.get("line_item_unblended_cost")
            or "0"
        ).strip()

        # ── Filter ────────────────────────────────────────────────────────────
        # If we know the child account ID, only count rows for that account
        if linked_account_id and acct_id and acct_id != linked_account_id:
            continue

        # Only include actual usage-type line items
        if item_type and item_type not in _USAGE_TYPES:
            continue

        # Parse cost
        try:
            cost = float(cost_str)
        except (ValueError, TypeError):
            cost = 0.0
        if cost == 0.0:
            continue

        month = _month_key(period_start)
        if not month:
            continue

        if _is_marketplace(billing_entity):
            month_data[month]["marketplace_cost"] += cost
        else:
            month_data[month]["cloud_service_cost"] += cost

        rows_parsed += 1

    log.info("[CUR] Parsed %d qualifying rows from %s", rows_parsed, key)
    return dict(month_data)


def import_cur_for_account(
    account: "AwsAccount",
    from_month: str | None = None,
    to_month:   str | None = None,
) -> list[dict]:
    """
    Main entry point. Import monthly costs from S3 CUR files for an account.

    Parameters
    ----------
    account     : AwsAccount — must have s3_cur_bucket configured
    from_month  : optional "YYYY-MM" filter (inclusive)
    to_month    : optional "YYYY-MM" filter (inclusive)

    Returns
    -------
    List of monthly result dicts:
      {
        "month":              "2025-01-01",   ← ISO date, first of month
        "cloud_service_cost": float,
        "marketplace_cost":   float,
        "source_files":       int,            ← how many CUR files contributed
        "data_status":        "cur_found" | "cur_zero",
      }
    """
    bucket = (account.s3_cur_bucket or "").strip()
    prefix = (account.s3_cur_prefix or "").strip()
    region = (account.s3_cur_region or account.region or "us-east-1").strip()

    if not bucket:
        raise ValueError(
            f"No S3 CUR bucket configured for account '{account.name}'. "
            "Edit the account and set the S3 bucket name."
        )

    linked_account_id = (account.aws_account_id or "").strip() or None

    log.info(
        "[CUR Import] account=%s  member=%s  bucket=%s  prefix=%s  region=%s",
        account.name, linked_account_id or "all", bucket, prefix, region,
    )

    s3 = _get_s3_client(account, region)

    # List all CUR files
    cur_files = _list_cur_files(s3, bucket, prefix, linked_account_id)
    if not cur_files:
        raise ValueError(
            f"No CUR files found in s3://{bucket}/{prefix}. "
            "Check the bucket name, prefix, and IAM permissions."
        )

    # Parse and aggregate by month across all files
    aggregated: dict[str, dict] = defaultdict(
        lambda: {"cloud_service_cost": 0.0, "marketplace_cost": 0.0, "source_files": 0}
    )

    for f in cur_files:
        bp = f["billing_period"]   # YYYY-MM (may be empty for unusual layouts)

        # Month-range filter — skip files outside the requested range
        if bp:
            if from_month and bp < from_month[:7]:
                continue
            if to_month and bp > to_month[:7]:
                continue

        try:
            month_data = _parse_cur_file(s3, bucket, f["key"], linked_account_id)
        except ValueError as exc:
            log.warning("[CUR] Skipping file %s: %s", f["key"], exc)
            continue

        for month_str, costs in month_data.items():
            # Month-range filter on actual data
            m_ym = month_str[:7]  # YYYY-MM
            if from_month and m_ym < from_month[:7]:
                continue
            if to_month and m_ym > to_month[:7]:
                continue

            aggregated[month_str]["cloud_service_cost"] += costs["cloud_service_cost"]
            aggregated[month_str]["marketplace_cost"]   += costs["marketplace_cost"]
            aggregated[month_str]["source_files"]       += 1

    if not aggregated:
        raise ValueError(
            f"CUR files found in S3 but no qualifying rows for account "
            f"'{account.name}' (member ID: {linked_account_id or 'unknown'}) "
            f"in the requested date range. "
            "Check that the CUR report includes this account's usage."
        )

    results = []
    for month_str in sorted(aggregated.keys()):
        d = aggregated[month_str]
        cloud = round(d["cloud_service_cost"], 4)
        mp    = round(d["marketplace_cost"],   4)
        results.append({
            "month":              month_str,
            "cloud_service_cost": cloud,
            "marketplace_cost":   mp,
            "source_files":       d["source_files"],
            "data_status":        "cur_found" if (cloud > 0 or mp > 0) else "cur_zero",
        })

    log.info(
        "[CUR Import Done] account=%s  months=%d  total_cloud=%.2f  total_mp=%.2f",
        account.name,
        len(results),
        sum(r["cloud_service_cost"] for r in results),
        sum(r["marketplace_cost"]   for r in results),
    )
    return results
