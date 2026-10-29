"""
AWS Cost Explorer — monthly cost fetch using the customer account's own IAM credentials.

COST CLASSIFICATION
--------------------
  cloud_service_cost  = all AWS-native charges (Usage, Tax, Support, RI fees,
                        Savings Plans, Credits, Refunds, Upfront Fees etc.)
  marketplace_cost    = AWS Marketplace third-party vendor charges only

DATA STATUS per month
----------------------
  fetched     – AWS returned valid non-zero cost
  zero        – AWS confirmed $0 for the month
  unavailable – DataUnavailableException or ValidationException (history limit)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import BotoCoreError, ClientError

if TYPE_CHECKING:
    from app.models import AwsAccount

log = logging.getLogger(__name__)

# Billing entities that identify AWS Marketplace third-party vendors
_MARKETPLACE_ENTITIES = {"aws marketplace", "aws marketplace, inc.", "amazon web services marketplace"}


def _is_marketplace_entity(name: str) -> bool:
    """Return True only for confirmed AWS Marketplace third-party vendors."""
    if not name:
        return False
    lower = name.strip().lower()
    # Blank / NoLegalEntityName = AWS native service, not marketplace
    if lower in ("", "nolegalentityname", "no legal entity name"):
        return False
    # Known marketplace entity names
    if lower in _MARKETPLACE_ENTITIES:
        return True
    # Third-party marketplace vendors typically do NOT contain "amazon" or "aws"
    # If it contains "amazon" or "aws" it's almost certainly a native service
    if "amazon" in lower or "aws " in lower:
        return False
    # Any other non-empty entity name = third-party marketplace vendor
    return True


# ── Date helpers ──────────────────────────────────────────────────────────────

def _first_day(d: date) -> date:
    return d.replace(day=1)


def build_date_range(contract_date: date) -> tuple[str, str]:
    """
    Returns (start_iso, end_iso) where:
      start = first day of contract month
      end   = first day of current month (exclusive — CE uses exclusive end)

    So the range covers contract_month → last completed month inclusive.
    """
    start = _first_day(contract_date)
    end   = date.today().replace(day=1)   # exclusive end = current month start
    if start >= end:
        raise ValueError(
            f"Contract date {contract_date} has no completed months yet "
            f"(current month starts {end})."
        )
    return start.isoformat(), end.isoformat()


def _month_range(start_str: str, end_str: str) -> list[tuple[str, str]]:
    """Return list of (month_start, month_end) pairs, each a full calendar month."""
    months = []
    cur = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    while cur < end:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        months.append((cur.isoformat(), min(nxt, end).isoformat()))
        cur = nxt
    return months


# ── CE query with full pagination ─────────────────────────────────────────────

_DATA_UNAVAIL = "DataUnavailableException"


def _ce_query(ce_client, start: str, end: str,
              ce_filter: dict | None,
              group_by: list[dict] | None = None,
              _error_ref: list | None = None) -> list[dict] | None:
    """
    Execute a Cost Explorer GetCostAndUsage query with full pagination.

    Returns merged ResultsByTime list on success.
    Returns None on DataUnavailableException or ValidationException.
    If _error_ref is a list, the error message is appended to it on unavailable.
    Raises ValueError for all other errors.
    """
    kwargs: dict = dict(
        TimePeriod={"Start": start, "End": end},
        Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
    )
    # Only add Filter if we have one — empty dict is invalid for CE API
    if ce_filter:
        kwargs["Filter"] = ce_filter
    if group_by:
        kwargs["GroupBy"] = group_by

    try:
        # ── Paginate through all results ──────────────────────────────────────
        # CE returns up to 100 groups per page; accounts with many marketplace
        # vendors can exceed this, causing silently truncated results without pagination.
        merged: dict[str, dict] = {}   # period_start → merged period dict

        while True:
            resp = ce_client.get_cost_and_usage(**kwargs)

            for period in resp.get("ResultsByTime", []):
                key = period["TimePeriod"]["Start"]
                if key not in merged:
                    # Deep copy the period structure
                    merged[key] = {
                        "TimePeriod": period["TimePeriod"],
                        "Total":      dict(period.get("Total", {})),
                        "Groups":     list(period.get("Groups", [])),
                        "Estimated":  period.get("Estimated", False),
                    }
                else:
                    # Merge: extend Groups list and add to Total
                    merged[key]["Groups"].extend(period.get("Groups", []))
                    for metric, val in period.get("Total", {}).items():
                        if metric in merged[key]["Total"]:
                            existing = float(merged[key]["Total"][metric].get("Amount", "0"))
                            additional = float(val.get("Amount", "0"))
                            merged[key]["Total"][metric]["Amount"] = str(existing + additional)
                        else:
                            merged[key]["Total"][metric] = dict(val)

            next_token = resp.get("NextPageToken")
            if not next_token:
                break
            kwargs["NextPageToken"] = next_token

        return list(merged.values())

    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg  = exc.response["Error"].get("Message", "")
        if code in (_DATA_UNAVAIL, "ValidationException"):
            log.warning("CE unavailable for %s->%s [%s]: %s", start, end, code, msg)
            if _error_ref is not None:
                _error_ref.append(f"{code}: {msg}")
            return None
        raise


# ── Build account filter ──────────────────────────────────────────────────────

def _account_filter(linked_account_id: str | None) -> dict | None:
    """Return a LINKED_ACCOUNT filter dict, or None if no account ID.
    AWS CE requires exactly 12-digit account IDs — zero-pad if shorter."""
    if not linked_account_id:
        return None
    # Zero-pad to 12 digits (AWS account IDs are always 12 digits;
    # some are stored/displayed without leading zeros)
    padded = linked_account_id.strip().zfill(12)
    return {
        "Dimensions": {
            "Key": "LINKED_ACCOUNT",
            "Values": [padded],
            "MatchOptions": ["EQUALS"],
        }
    }


# ── Per-month fetch ───────────────────────────────────────────────────────────

def _fetch_month(ce_client, m_start: str, m_end: str,
                 linked_account_id: str | None,
                 payer_account_id: str) -> dict:
    """
    Fetch ALL cost data for a single calendar month.

    Strategy:
    1. Query by RECORD_TYPE grouped to see exact breakdown, then filter to
       only the charge types that match what the AWS Console "total" shows:
       Usage, Fee, RIFee, SavingsPlanCoveredUsage, SavingsPlanRecurringFee,
       SavingsPlanUpfrontFee, DiscountedUsage, Credit, Refund, Other.
       EXCLUDED: Tax, Support (these inflate the cost beyond console view).

    2. Within kept charge types, classify by legal entity:
       - Confirmed marketplace entity → marketplace_cost
       - Everything else → cloud_service_cost

    3. If LINKED_ACCOUNT filter returns None (unavailable), return "unavailable"
       immediately — do NOT fall back without account filter.
    """
    acct_filter = _account_filter(linked_account_id)
    error_ref: list[str] = []

    # ── Step 1: query grouped by RECORD_TYPE to see breakdown ────────────────
    record_type_filter = acct_filter  # just the account filter, no record_type filter yet

    rt_results = _ce_query(
        ce_client, m_start, m_end,
        ce_filter=record_type_filter,
        group_by=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
        _error_ref=error_ref,
    )

    if rt_results is None:
        ce_msg = error_ref[0] if error_ref else "DataUnavailableException"
        log.warning("[Unavailable] %s  account=%s  payer=%s  reason=%s",
                    m_start[:7], linked_account_id, payer_account_id, ce_msg)
        return {
            "month":              _first_day(date.fromisoformat(m_start)),
            "cloud_service_cost": 0.0,
            "marketplace_cost":   0.0,
            "data_status":        "unavailable",
            "payer_account_id":   payer_account_id,
            "ce_error":           ce_msg,
        }

    # Log the full RECORD_TYPE breakdown so we can see what's included
    total_by_type: dict[str, float] = {}
    for period in rt_results:
        for group in period.get("Groups", []):
            rt   = group["Keys"][0] if group.get("Keys") else "Unknown"
            amt  = float(group["Metrics"]["UnblendedCost"]["Amount"])
            total_by_type[rt] = total_by_type.get(rt, 0.0) + amt
    if total_by_type:
        breakdown = "  ".join(f"{k}=${v:.2f}" for k, v in sorted(total_by_type.items()))
        log.info("[RecordType] %s  account=%s  %s", m_start[:7], linked_account_id, breakdown)

    # ── Step 2: query grouped by LEGAL_ENTITY_NAME, filtered to usage-only types ─
    # These are the charge types shown in the AWS Console "Services" total.
    # Tax and Support are excluded — they are tracked separately.
    _USAGE_RECORD_TYPES = [
        "Usage", "Fee", "RIFee", "Credit", "Refund", "Other",
        "DiscountedUsage",
        "SavingsPlanCoveredUsage", "SavingsPlanRecurringFee",
        "SavingsPlanUpfrontFee", "SavingsPlanNegation",
        "EdpDiscount", "BundledDiscount",
    ]

    rt_dim_filter: dict = {
        "Dimensions": {
            "Key": "RECORD_TYPE",
            "Values": _USAGE_RECORD_TYPES,
            "MatchOptions": ["EQUALS"],
        }
    }

    if acct_filter:
        combined_filter: dict = {"And": [acct_filter, rt_dim_filter]}
    else:
        combined_filter = rt_dim_filter

    results = _ce_query(
        ce_client, m_start, m_end,
        ce_filter=combined_filter,
        group_by=[{"Type": "DIMENSION", "Key": "LEGAL_ENTITY_NAME"}],
        _error_ref=error_ref,
    )

    if results is None:
        # Fallback: use the RECORD_TYPE totals we already have, just sum non-tax
        log.warning("[FallbackTotal] %s using record_type totals", m_start[:7])
        cloud = sum(v for k, v in total_by_type.items()
                    if k not in ("Tax", "Support"))
        mp    = 0.0
        cloud = round(cloud, 4)
        status = "fetched" if cloud != 0.0 else "zero"
        return {
            "month":              _first_day(date.fromisoformat(m_start)),
            "cloud_service_cost": cloud,
            "marketplace_cost":   mp,
            "data_status":        status,
            "payer_account_id":   payer_account_id,
        }

    cloud = 0.0
    mp    = 0.0

    for period in results:
        for group in period.get("Groups", []):
            entity = group["Keys"][0] if group.get("Keys") else ""
            amount = float(group["Metrics"]["UnblendedCost"]["Amount"])

            if amount == 0.0:
                continue

            if _is_marketplace_entity(entity):
                mp    += amount
                log.info("[MP]    %s  entity=%-35s  $%.4f", m_start[:7], entity, amount)
            else:
                cloud += amount
                if amount < 0:
                    log.info("[Credit/Refund] %s  entity=%-30s  $%.4f",
                             m_start[:7], entity, amount)

    # Also sum Total if no Groups (edge case)
    for period in results:
        total_amt = float(
            period.get("Total", {})
                  .get("UnblendedCost", {})
                  .get("Amount", "0")
        )
        if total_amt != 0.0 and not period.get("Groups"):
            cloud += total_amt

    # ── Fallback: if filtered query returned $0 but unfiltered had costs, ─────
    # retry without LINKED_ACCOUNT filter. This handles standalone accounts
    # where CE returns $0 when the account queries itself with a LINKED_ACCOUNT
    # filter — the account IS the root, so filtering to itself gives nothing.
    if cloud == 0.0 and mp == 0.0 and acct_filter and sum(total_by_type.values()) > 0:
        log.info("[Retry-NoFilter] %s  account=%s  filtered=$0 but unfiltered has costs — retrying without LINKED_ACCOUNT filter",
                 m_start[:7], linked_account_id)
        results_nf = _ce_query(
            ce_client, m_start, m_end,
            ce_filter=rt_dim_filter,   # no account filter
            group_by=[{"Type": "DIMENSION", "Key": "LEGAL_ENTITY_NAME"}],
        )
        if results_nf:
            for period in results_nf:
                for group in period.get("Groups", []):
                    entity = group["Keys"][0] if group.get("Keys") else ""
                    amount = float(group["Metrics"]["UnblendedCost"]["Amount"])
                    if amount == 0.0:
                        continue
                    if _is_marketplace_entity(entity):
                        mp    += amount
                        log.info("[MP-NF]    %s  entity=%-35s  $%.4f", m_start[:7], entity, amount)
                    else:
                        cloud += amount

    cloud = round(cloud, 4)
    mp    = round(mp, 4)
    status = "fetched" if (cloud != 0.0 or mp != 0.0) else "zero"

    log.info("[%s] %s  cloud=$%.2f  mp=$%.2f  account=%s",
             status.capitalize(), m_start[:7], cloud, mp, linked_account_id or "all")

    return {
        "month":              _first_day(date.fromisoformat(m_start)),
        "cloud_service_cost": cloud,
        "marketplace_cost":   mp,
        "data_status":        status,
        "payer_account_id":   payer_account_id,
    }


# ── Main entry point ──────────────────────────────────────────────────────────

def fetch_monthly_costs(account: "AwsAccount",
                        month_pairs: list[tuple[str, str]] | None = None) -> list[dict]:
    """
    Fetch monthly costs from contract date through last completed month.

    Parameters
    ----------
    account     : AwsAccount with IAM credentials stored directly
    month_pairs : optional pre-filtered list (start_iso, end_iso) from caller.
                  If None, fetches all months from contract_date to last month.
    """
    access_key = account.get_access_key_id()
    secret_key = account.get_secret_access_key()
    region     = account.region or "us-east-1"

    if not access_key or not secret_key:
        raise ValueError(
            f"No credentials found for account '{account.name}'. "
            "Add IAM credentials to this account."
        )

    # The 12-digit account ID used to filter CE to this account's costs only
    linked_account_id = (account.aws_account_id or "").strip() or None
    # Ensure 12-digit format (AWS requirement; stored IDs may lack leading zeros)
    if linked_account_id:
        linked_account_id = linked_account_id.zfill(12)
    payer_account_id  = linked_account_id or "direct"

    log.info("[Fetch] account=%s  account_id=%s  region=%s",
             account.name, payer_account_id, region)

    if month_pairs is None:
        start_str, end_str = build_date_range(account.contract_date)
        month_pairs = _month_range(start_str, end_str)

    if not month_pairs:
        log.info("[Fetch] account=%s — no months need fetching", account.name)
        return []

    log.info("[Fetch Start] account=%s  member=%s  payer=%s  months_to_fetch=%d",
             account.name, linked_account_id or "all",
             payer_account_id, len(month_pairs))

    try:
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        ce = session.client("ce", region_name="us-east-1")  # CE is global, us-east-1
    except (ClientError, BotoCoreError) as exc:
        raise ValueError(f"AWS connection error: {exc}") from exc
    finally:
        access_key = None
        secret_key = None

    results: list[dict] = []
    for m_start, m_end in month_pairs:
        try:
            row = _fetch_month(ce, m_start, m_end, linked_account_id, payer_account_id)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            msg  = exc.response["Error"]["Message"]
            raise ValueError(f"AWS API error [{code}]: {msg}") from exc
        except BotoCoreError as exc:
            raise ValueError(f"AWS connection error: {exc}") from exc
        results.append(row)

    fetched = sum(1 for r in results if r["data_status"] == "fetched")
    zeros   = sum(1 for r in results if r["data_status"] == "zero")
    unavail = sum(1 for r in results if r["data_status"] == "unavailable")
    log.info("[Fetch Done] account=%s  fetched=%d  zero=%d  unavailable=%d",
             account.name, fetched, zeros, unavail)

    return results

