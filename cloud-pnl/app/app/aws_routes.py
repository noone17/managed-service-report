"""
REST API for AWS account management and cost fetching.

Endpoints (static routes MUST come before parametric routes):
  GET    /api/aws-accounts                      list accounts
  POST   /api/aws-accounts/fetch-all            fetch costs for ALL active accounts
  POST   /api/aws-accounts                      create account
  PUT    /api/aws-accounts/<id>                 update account
  DELETE /api/aws-accounts/<id>                 delete account
  POST   /api/aws-accounts/<id>/fetch           fetch costs from Cost Explorer
  PUT    /api/aws-accounts/<id>/cur-config       save CUR S3 config
  POST   /api/aws-accounts/<id>/import-cur      import from CUR S3
  GET    /api/aws-accounts/<id>/cur-diagnose    diagnose CUR S3 setup
"""

import uuid
import logging
from datetime import date, datetime
from flask import Blueprint, request, jsonify
from sqlalchemy.exc import SQLAlchemyError
from app import db
from app.models import AwsAccount, CostRecord

aws_bp = Blueprint("aws", __name__)
log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(value, field):
    if not value:
        return None, f"{field} is required"
    try:
        return datetime.strptime(value, "%Y-%m-%d").date(), None
    except ValueError:
        return None, f"{field} must be YYYY-MM-DD"


def _validate_access_key(key):
    return bool(key) and len(key) >= 16


# ── List accounts ─────────────────────────────────────────────────────────────

@aws_bp.route("/aws-accounts", methods=["GET"])
def list_accounts():
    try:
        query = AwsAccount.query.order_by(AwsAccount.name)
        if request.args.get("active_only", "").lower() == "true":
            query = query.filter_by(is_active=True)
        accounts = query.all()
        return jsonify([a.to_dict() for a in accounts]), 200
    except SQLAlchemyError as exc:
        return jsonify({"error": f"Database error: {exc}"}), 500


# ── Fetch All (MUST be before /<int:account_id> routes) ───────────────────────

@aws_bp.route("/aws-accounts/fetch-all", methods=["POST"])
def fetch_all_costs():
    """Fetch costs for ALL active non-manual accounts sequentially."""
    from app.aws_service import fetch_monthly_costs, build_date_range, _month_range
    from datetime import datetime as _dt

    accounts = AwsAccount.query.filter_by(
        is_active=True, is_manual=False
    ).order_by(AwsAccount.name).all()

    if not accounts:
        return jsonify({"message": "No active accounts to fetch.", "results": []}), 200

    results = []

    for account in accounts:
        acct_result = {
            "id":      account.id,
            "name":    account.name,
            "status":  "ok",
            "message": "",
            "summary": {},
        }

        if not account._access_key_id_enc:
            acct_result["status"]  = "skipped"
            acct_result["message"] = "No IAM credentials configured."
            results.append(acct_result)
            continue

        try:
            start_str, end_str = build_date_range(account.contract_date)
            all_months  = _month_range(start_str, end_str)
            today_month = date.today().replace(day=1)

            # Last completed month (one before today)
            if today_month.month == 1:
                last_completed = date(today_month.year - 1, 12, 1)
            else:
                last_completed = date(today_month.year, today_month.month - 1, 1)

            all_existing = CostRecord.query.filter_by(aws_account_id=account.id).all()
            existing_by_month = {}
            for r in all_existing:
                existing_by_month.setdefault(r.consumption_month, []).append(r)

            def _needs_fetch(month_date):
                # Always re-fetch the last completed month (billing finalisation)
                if month_date >= last_completed:
                    return True
                rows = existing_by_month.get(month_date, [])
                if not rows:
                    return True
                # Has real cost data → skip (historical protection)
                has_real = any(
                    float(r.cloud_service_cost) > 0 or float(r.marketplace_cost) > 0
                    for r in rows
                )
                if has_real:
                    return False
                # All $0 placeholders → re-fetch (ignore duplicate row count)
                return True

            months_to_fetch = [
                (ms, me) for ms, me in all_months
                if _needs_fetch(_dt.strptime(ms, "%Y-%m-%d").date())
            ]

            monthly_data = fetch_monthly_costs(account, month_pairs=months_to_fetch)

            payer_id = (account.aws_account_id or "direct").strip()
            summary = {
                "fetched": 0, "preserved": 0, "zero": 0,
                "unavailable": 0, "inserted": 0, "updated": 0,
                "skipped": len(all_months) - len(months_to_fetch),
            }

            # Last record with non-zero discounts → used to seed new months
            _last_a = (
                CostRecord.query
                .filter_by(aws_account_id=account.id)
                .filter(CostRecord.distributor_discount > 0)
                .order_by(CostRecord.consumption_month.desc())
                .first()
            ) or (
                CostRecord.query
                .filter_by(aws_account_id=account.id)
                .order_by(CostRecord.consumption_month.desc())
                .first()
            )

            def _defs_a():
                if _last_a:
                    return dict(
                        distributor_discount  = float(_last_a.distributor_discount),
                        customer_discount     = float(_last_a.customer_discount),
                        managed_services      = float(_last_a.managed_services),
                        redington_credit_note = 0.0,
                        credit_amount         = 0.0,
                        cash_claim            = 0.0,
                    )
                return {}

            for row in monthly_data:
                month_date = row["month"]
                aws_status = row["data_status"]
                aws_cloud  = row["cloud_service_cost"]
                aws_mp     = row["marketplace_cost"]

                existing_rows = (
                    CostRecord.query
                    .filter_by(aws_account_id=account.id, consumption_month=month_date)
                    .all()
                )
                existing = existing_rows[0] if existing_rows else None
                has_historical = (
                    existing is not None
                    and (float(existing.cloud_service_cost) > 0
                         or float(existing.marketplace_cost) > 0)
                )
                existing_for_payer = next(
                    (r for r in existing_rows if r.cost_data_source == payer_id), None
                )

                # Computed once — used by fetched, zero, AND unavailable branches
                is_recent = month_date >= last_completed

                if aws_status == "fetched":
                    if existing_for_payer:
                        has_real = (
                            float(existing_for_payer.cloud_service_cost) > 0
                            or float(existing_for_payer.marketplace_cost) > 0
                        )
                        safe = (
                            existing_for_payer.cost_status in ("unavailable", "zero")
                            or is_recent   # last completed + current month always updatable
                            or not has_real
                        )
                        if safe:
                            existing_for_payer.cloud_service_cost = round(aws_cloud, 4)
                            existing_for_payer.marketplace_cost   = round(aws_mp, 4)
                            existing_for_payer.cost_status        = "fetched"
                            existing_for_payer.is_auto_fetched    = True
                            summary["updated"] += 1
                        else:
                            existing_for_payer.cost_status = "preserved"
                            summary["preserved"] += 1
                        summary["fetched"] += 1
                    elif existing is None:
                        db.session.add(CostRecord(
                            aws_account_id=account.id,
                            contract_date=account.contract_date,
                            consumption_month=month_date,
                            cloud_service_cost=round(aws_cloud, 4),
                            marketplace_cost=round(aws_mp, 4),
                            is_auto_fetched=True,
                            cost_data_source=payer_id,
                            cost_status="fetched",
                            **_defs_a(),
                        ))
                        summary["inserted"] += 1
                        summary["fetched"]  += 1
                    else:
                        # Existing record from different source — update if $0, preserve if has real data
                        has_real_existing = (
                            float(existing.cloud_service_cost) > 0
                            or float(existing.marketplace_cost) > 0
                        )
                        if not has_real_existing or is_recent:
                            existing.cloud_service_cost = round(aws_cloud, 4)
                            existing.marketplace_cost   = round(aws_mp, 4)
                            existing.cost_data_source   = payer_id
                            existing.cost_status        = "fetched"
                            existing.is_auto_fetched    = True
                            summary["updated"] += 1
                            summary["fetched"]  += 1
                        else:
                            existing.cost_status = "preserved"
                            summary["preserved"] += 1

                elif aws_status == "zero":
                    if has_historical:
                        existing.cost_status = "preserved"
                        summary["preserved"] += 1
                    elif existing is not None:
                        existing.cost_data_source = payer_id
                        existing.cost_status      = "zero"
                        summary["zero"] += 1
                    else:
                        db.session.add(CostRecord(
                            aws_account_id=account.id,
                            contract_date=account.contract_date,
                            consumption_month=month_date,
                            cloud_service_cost=0,
                            marketplace_cost=0,
                            is_auto_fetched=True,
                            cost_data_source=payer_id,
                            cost_status="zero",
                            **_defs_a(),
                        ))
                        summary["zero"] += 1

                elif aws_status == "unavailable":
                    if has_historical and not is_recent:
                        # Preserve only truly old historical months
                        existing.cost_status = "preserved"
                        summary["preserved"] += 1
                    elif existing_for_payer is not None:
                        existing_for_payer.cost_data_source = payer_id
                        existing_for_payer.cost_status      = "unavailable"
                        summary["unavailable"] += 1
                    elif existing is not None:
                        existing.cost_data_source = payer_id
                        existing.cost_status      = "unavailable"
                        summary["unavailable"] += 1
                    else:
                        db.session.add(CostRecord(
                            aws_account_id=account.id,
                            contract_date=account.contract_date,
                            consumption_month=month_date,
                            cloud_service_cost=0,
                            marketplace_cost=0,
                            is_auto_fetched=True,
                            cost_data_source=payer_id,
                            cost_status="unavailable",
                            **_defs_a(),
                        ))
                        summary["unavailable"] += 1

            db.session.commit()
            acct_result["summary"] = summary
            acct_result["message"] = (
                f"Fetched: {summary['fetched']}, "
                f"Inserted: {summary['inserted']}, "
                f"Updated: {summary['updated']}, "
                f"Skipped: {summary['skipped']}, "
                f"Zero: {summary['zero']}, "
                f"Unavailable: {summary['unavailable']}."
            )

        except ValueError as exc:
            db.session.rollback()
            acct_result["status"]  = "error"
            acct_result["message"] = str(exc)
        except SQLAlchemyError as exc:
            db.session.rollback()
            acct_result["status"]  = "error"
            acct_result["message"] = f"Database error: {exc}"
        except Exception as exc:
            db.session.rollback()
            acct_result["status"]  = "error"
            acct_result["message"] = str(exc)

        results.append(acct_result)

    ok_count      = sum(1 for r in results if r["status"] == "ok")
    error_count   = sum(1 for r in results if r["status"] == "error")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")

    return jsonify({
        "message": (
            f"Fetch All complete. "
            f"OK: {ok_count}, Errors: {error_count}, "
            f"Skipped: {skipped_count} / {len(results)} accounts."
        ),
        "total":   len(results),
        "ok":      ok_count,
        "errors":  error_count,
        "skipped": skipped_count,
        "results": results,
    }), 200


# ── Create account ────────────────────────────────────────────────────────────

@aws_bp.route("/aws-accounts", methods=["POST"])
def create_account():
    payload    = request.get_json(silent=True) or {}
    name       = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 422

    is_manual  = bool(payload.get("is_manual", False))
    access_key = (payload.get("access_key_id") or "").strip()
    secret_key = (payload.get("secret_access_key") or "").strip()
    csp        = (payload.get("csp") or "AWS").strip().upper()
    # Auto-set billing currency: GCP/Azure bill in INR natively; AWS bills in USD
    billing_currency = (payload.get("billing_currency") or "").strip().upper()
    if not billing_currency:
        billing_currency = "INR" if csp in ("GCP", "AZURE") else "USD"
    # Zero-pad to 12 digits for AWS only — GCP/Azure IDs are not numeric
    raw_acct_id = (payload.get("aws_account_id") or "").strip()
    if raw_acct_id and csp == "AWS":
        aws_acct_id = raw_acct_id.zfill(12)
    else:
        aws_acct_id = raw_acct_id or None

    if not is_manual:
        if not _validate_access_key(access_key):
            return jsonify({"error": "access_key_id is required (min 16 characters)"}), 422
        if not secret_key:
            return jsonify({"error": "secret_access_key is required"}), 422

    contract_date, err = _parse_date(payload.get("contract_date"), "contract_date")
    if err:
        return jsonify({"error": err}), 422

    # contract_end_date is optional — only set when deactivating
    contract_end_date = None
    if payload.get("contract_end_date"):
        contract_end_date, err = _parse_date(payload["contract_end_date"], "contract_end_date")
        if err:
            return jsonify({"error": err}), 422

    if aws_acct_id:
        existing = AwsAccount.query.filter_by(aws_account_id=aws_acct_id).first()
        if existing:
            return jsonify({
                "error": f"Account ID {aws_acct_id} already exists as '{existing.name}'.",
                "existing_id": existing.id,
            }), 409

    try:
        account = AwsAccount(
            name             = name,
            aws_account_id   = aws_acct_id,
            region           = payload.get("region") or "us-east-1",
            contract_date    = contract_date,
            contract_end_date = contract_end_date,
            is_active        = bool(payload.get("is_active", True)),
            is_manual        = is_manual,
            csp              = csp,
            billing_currency = billing_currency,
            s3_cur_bucket  = (payload.get("s3_cur_bucket") or "").strip() or None,
            s3_cur_prefix  = (payload.get("s3_cur_prefix") or "").strip() or None,
            s3_cur_region  = (payload.get("s3_cur_region") or "").strip() or None,
        )
        if not is_manual:
            account.set_access_key_id(access_key)
            account.set_secret_access_key(secret_key)
        db.session.add(account)
        db.session.commit()
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 500
    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": f"Database error: {exc}"}), 500

    return jsonify(account.to_dict()), 201


# ── Update account ────────────────────────────────────────────────────────────

@aws_bp.route("/aws-accounts/<int:account_id>", methods=["PUT"])
def update_account(account_id):
    account = AwsAccount.query.get_or_404(account_id)
    payload = request.get_json(silent=True) or {}

    if (name := (payload.get("name") or "").strip()):
        account.name = name
    if "aws_account_id" in payload:
        raw = (payload.get("aws_account_id") or "").strip()
        # Zero-pad only for AWS accounts
        acct_csp = (payload.get("csp") or account.csp or "AWS").strip().upper()
        if raw and acct_csp == "AWS":
            account.aws_account_id = raw.zfill(12)
        else:
            account.aws_account_id = raw or None
    if payload.get("region"):
        account.region = payload["region"]
    if payload.get("contract_date"):
        contract_date, err = _parse_date(payload["contract_date"], "contract_date")
        if err:
            return jsonify({"error": err}), 422
        account.contract_date = contract_date
    if payload.get("is_active") is not None:
        account.is_active = bool(payload["is_active"])
    # contract_end_date: set when deactivating, clear when reactivating
    if "contract_end_date" in payload:
        if payload["contract_end_date"]:
            ced, err = _parse_date(payload["contract_end_date"], "contract_end_date")
            if err:
                return jsonify({"error": err}), 422
            account.contract_end_date = ced
        else:
            account.contract_end_date = None  # cleared on reactivation
    if "csp" in payload:
        account.csp = (payload["csp"] or "AWS").strip().upper()
    if "billing_currency" in payload:
        bc = (payload["billing_currency"] or "").strip().upper()
        account.billing_currency = bc if bc in ("USD", "INR") else account.billing_currency
    elif "csp" in payload:
        # Auto-update billing currency when CSP changes
        new_csp = account.csp
        if new_csp in ("GCP", "AZURE"):
            account.billing_currency = "INR"
        elif new_csp == "AWS":
            account.billing_currency = "USD"
    if "s3_cur_bucket" in payload:
        account.s3_cur_bucket = (payload["s3_cur_bucket"] or "").strip() or None
    if "s3_cur_prefix" in payload:
        account.s3_cur_prefix = (payload["s3_cur_prefix"] or "").strip() or None
    if "s3_cur_region" in payload:
        account.s3_cur_region = (payload["s3_cur_region"] or "").strip() or None

    access_key = (payload.get("access_key_id") or "").strip()
    secret_key = (payload.get("secret_access_key") or "").strip()
    if access_key or secret_key:
        if access_key and not _validate_access_key(access_key):
            return jsonify({"error": "access_key_id too short (min 16 chars)"}), 422
        try:
            if access_key:
                account.set_access_key_id(access_key)
            if secret_key:
                account.set_secret_access_key(secret_key)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 500

    try:
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": f"Database error: {exc}"}), 500

    return jsonify(account.to_dict()), 200


# ── Delete account ────────────────────────────────────────────────────────────

@aws_bp.route("/aws-accounts/<int:account_id>", methods=["DELETE"])
def delete_account(account_id):
    account = AwsAccount.query.get_or_404(account_id)
    try:
        db.session.delete(account)
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": f"Database error: {exc}"}), 500
    return jsonify({"message": "Account deleted. Cost records were kept."}), 200


# ── Fetch costs for a single account ─────────────────────────────────────────

@aws_bp.route("/aws-accounts/<int:account_id>/fetch", methods=["POST"])
def fetch_costs(account_id):
    account = AwsAccount.query.get_or_404(account_id)

    if not account.is_active:
        return jsonify({"error": "Account is inactive"}), 400
    if account.is_manual:
        return jsonify({"error": "Manual accounts cannot be fetched from Cost Explorer"}), 400
    if not account._access_key_id_enc:
        return jsonify({"error": "No IAM credentials stored. Edit the account to add keys."}), 400

    from app.aws_service import fetch_monthly_costs, build_date_range, _month_range
    from datetime import datetime as _dt

    try:
        start_str, end_str = build_date_range(account.contract_date)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    all_months  = _month_range(start_str, end_str)
    today_month = date.today().replace(day=1)

    # Last completed month — computed once, used by _needs_fetch AND the update loop
    if today_month.month == 1:
        last_completed = date(today_month.year - 1, 12, 1)
    else:
        last_completed = date(today_month.year, today_month.month - 1, 1)

    all_existing = CostRecord.query.filter_by(aws_account_id=account.id).all()
    existing_by_month = {}
    for r in all_existing:
        existing_by_month.setdefault(r.consumption_month, []).append(r)

    def _needs_fetch(month_date):
        if month_date >= last_completed:
            return True

        rows = existing_by_month.get(month_date, [])
        if not rows:
            return True

        has_real = any(
            float(r.cloud_service_cost) > 0 or float(r.marketplace_cost) > 0
            for r in rows
        )
        if has_real:
            return False

        return True

    months_to_fetch = [
        (ms, me) for ms, me in all_months
        if _needs_fetch(_dt.strptime(ms, "%Y-%m-%d").date())
    ]
    months_skipped = len(all_months) - len(months_to_fetch)

    try:
        monthly_data = fetch_monthly_costs(account, month_pairs=months_to_fetch)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    summary = {
        "fetched": 0, "preserved": 0, "zero": 0,
        "unavailable": 0, "inserted": 0, "updated": 0,
        "split": 0, "skipped": months_skipped,
    }
    month_results = []
    payer_id = (account.aws_account_id or "direct").strip()

    # Pull the most-recent existing record to use its discount settings as
    # defaults for any newly inserted months.  This way new months automatically
    # inherit Dist. Disc %, Cust. Disc %, Managed %, Conversion Rate, etc.
    _last = (
        CostRecord.query
        .filter_by(aws_account_id=account.id)
        .filter(CostRecord.distributor_discount > 0)   # prefer a non-zero row
        .order_by(CostRecord.consumption_month.desc())
        .first()
    ) or (
        CostRecord.query
        .filter_by(aws_account_id=account.id)
        .order_by(CostRecord.consumption_month.desc())
        .first()
    )

    def _default_discounts():
        """Return dict of discount fields from the last record (or zeros).
        Conversion rate is intentionally excluded — it must be set per-month globally."""
        if _last:
            return dict(
                distributor_discount  = float(_last.distributor_discount),
                customer_discount     = float(_last.customer_discount),
                managed_services      = float(_last.managed_services),
                redington_credit_note = 0.0,
                credit_amount         = 0.0,
                cash_claim            = 0.0,
            )
        return {}

    try:
        for row in monthly_data:
            month_date = row["month"]
            aws_status = row["data_status"]
            aws_cloud  = row["cloud_service_cost"]
            aws_mp     = row["marketplace_cost"]
            month_str  = month_date.isoformat()

            existing_rows = (
                CostRecord.query
                .filter_by(aws_account_id=account.id, consumption_month=month_date)
                .all()
            )
            existing = existing_rows[0] if existing_rows else None
            has_historical = (
                existing is not None
                and (float(existing.cloud_service_cost) > 0
                     or float(existing.marketplace_cost) > 0)
            )
            existing_for_this_payer = next(
                (r for r in existing_rows if r.cost_data_source == payer_id), None
            )

            # Computed once — used by fetched, zero, AND unavailable branches
            # Recent = last completed month or current month; always safe to overwrite
            is_recent = month_date >= last_completed

            if aws_status == "fetched":
                if existing_for_this_payer:
                    has_real = (
                        float(existing_for_this_payer.cloud_service_cost) > 0
                        or float(existing_for_this_payer.marketplace_cost) > 0
                    )
                    safe = (
                        existing_for_this_payer.cost_status in ("unavailable", "zero")
                        or is_recent   # last completed month + current month always updatable
                        or not has_real
                    )
                    if safe:
                        existing_for_this_payer.cloud_service_cost = round(aws_cloud, 4)
                        existing_for_this_payer.marketplace_cost   = round(aws_mp, 4)
                        existing_for_this_payer.cost_status        = "fetched"
                        existing_for_this_payer.is_auto_fetched    = True
                        summary["updated"] += 1
                        summary["fetched"] += 1
                        month_results.append({
                            "month": month_str, "status": "fetched", "action": "updated",
                            "cloud_service_cost": round(aws_cloud, 2),
                            "marketplace_cost":   round(aws_mp, 2),
                        })
                    else:
                        existing_for_this_payer.cost_status = "preserved"
                        summary["preserved"] += 1
                        month_results.append({
                            "month": month_str, "status": "preserved",
                            "reason": "Historical record preserved on re-fetch",
                        })
                elif existing is None:
                    record = CostRecord(
                        aws_account_id=account.id,
                        contract_date=account.contract_date,
                        consumption_month=month_date,
                        cloud_service_cost=round(aws_cloud, 4),
                        marketplace_cost=round(aws_mp, 4),
                        is_auto_fetched=True,
                        cost_data_source=payer_id,
                        cost_status="fetched",
                        **_default_discounts(),
                    )
                    db.session.add(record)
                    summary["inserted"] += 1
                    summary["fetched"]  += 1
                    month_results.append({
                        "month": month_str, "status": "fetched", "action": "inserted",
                        "cloud_service_cost": round(aws_cloud, 2),
                        "marketplace_cost":   round(aws_mp, 2),
                    })
                else:
                    # Existing record has a different cost_data_source.
                    # If it has real cost data → preserve it (don't overwrite from different payer).
                    # If it's a $0 placeholder (old payer, no data) → update it with fresh data.
                    has_real_existing = (
                        float(existing.cloud_service_cost) > 0
                        or float(existing.marketplace_cost) > 0
                    )
                    if not has_real_existing or is_recent:
                        existing.cloud_service_cost = round(aws_cloud, 4)
                        existing.marketplace_cost   = round(aws_mp, 4)
                        existing.cost_data_source   = payer_id
                        existing.cost_status        = "fetched"
                        existing.is_auto_fetched    = True
                        summary["updated"] += 1
                        summary["fetched"]  += 1
                        month_results.append({
                            "month": month_str, "status": "fetched", "action": "updated",
                            "cloud_service_cost": round(aws_cloud, 2),
                            "marketplace_cost":   round(aws_mp, 2),
                        })
                    else:
                        existing.cost_status = "preserved"
                        summary["preserved"] += 1
                        month_results.append({
                            "month": month_str, "status": "preserved",
                            "reason": "Different source with real data — preserved",
                        })

            elif aws_status == "zero":
                if has_historical:
                    existing.cost_status = "preserved"
                    summary["preserved"] += 1
                    month_results.append({
                        "month": month_str, "status": "preserved",
                        "reason": "AWS returned $0 but existing record preserved",
                    })
                elif existing is not None:
                    existing.cost_data_source = payer_id
                    existing.cost_status      = "zero"
                    summary["zero"] += 1
                    month_results.append({
                        "month": month_str, "status": "zero",
                        "reason": "Placeholder already exists — updated.",
                    })
                else:
                    db.session.add(CostRecord(
                        aws_account_id=account.id,
                        contract_date=account.contract_date,
                        consumption_month=month_date,
                        cloud_service_cost=0, marketplace_cost=0,
                        is_auto_fetched=True,
                        cost_data_source=payer_id,
                        cost_status="zero",
                        **_default_discounts(),
                    ))
                    summary["zero"] += 1
                    month_results.append({
                        "month": month_str, "status": "zero",
                        "reason": "AWS confirmed $0 — placeholder created. Use Import CUR or enter manually.",
                        "cost_data_source": payer_id,
                    })

            elif aws_status == "unavailable":
                ce_err = row.get("ce_error", "DataUnavailableException")
                if has_historical and not is_recent:
                    # Only preserve truly historical (old completed) months.
                    # Recent months (last completed + current) must NOT be frozen
                    # even if they previously had a partial/low value.
                    existing.cost_status = "preserved"
                    summary["preserved"] += 1
                    month_results.append({
                        "month": month_str, "status": "preserved",
                        "reason": f"CE unavailable ({ce_err}); historical record preserved",
                    })
                elif existing_for_this_payer is not None:
                    existing_for_this_payer.cost_data_source = payer_id
                    existing_for_this_payer.cost_status      = "unavailable"
                    summary["unavailable"] += 1
                    month_results.append({
                        "month": month_str, "status": "unavailable",
                        "reason": f"AWS CE error: {ce_err} — use Import CUR or enter manually.",
                    })
                elif existing is not None:
                    existing.cost_data_source = payer_id
                    existing.cost_status      = "unavailable"
                    summary["unavailable"] += 1
                    month_results.append({
                        "month": month_str, "status": "unavailable",
                        "reason": f"AWS CE error: {ce_err} — placeholder updated.",
                    })
                else:
                    db.session.add(CostRecord(
                        aws_account_id=account.id,
                        contract_date=account.contract_date,
                        consumption_month=month_date,
                        cloud_service_cost=0, marketplace_cost=0,
                        is_auto_fetched=True,
                        cost_data_source=payer_id,
                        cost_status="unavailable",
                        **_default_discounts(),
                    ))
                    summary["unavailable"] += 1
                    month_results.append({
                        "month": month_str, "status": "unavailable",
                        "reason": f"AWS CE error: {ce_err} — placeholder created. Use Import CUR or enter manually.",
                        "cost_data_source": payer_id,
                    })

        db.session.commit()

    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": f"Database error: {exc}"}), 500

    message = (
        f"Processed {len(months_to_fetch)} month(s) for '{account.name}' "
        f"({months_skipped} already had data and were skipped). "
        f"Fetched: {summary['fetched']}, Preserved: {summary['preserved']}, "
        f"Zero: {summary['zero']}, Unavailable: {summary['unavailable']}."
    )

    return jsonify({
        "success": True,
        "message": message,
        "summary": summary,
        "months":  month_results,
        "account": account.name,
    }), 200


# ── CUR S3 Configuration ──────────────────────────────────────────────────────

@aws_bp.route("/aws-accounts/<int:account_id>/cur-config", methods=["PUT"])
def update_cur_config(account_id):
    account = AwsAccount.query.get_or_404(account_id)
    payload = request.get_json(silent=True) or {}

    account.s3_cur_bucket = (payload.get("s3_cur_bucket") or "").strip() or None
    account.s3_cur_prefix = (payload.get("s3_cur_prefix") or "").strip() or None
    account.s3_cur_region = (payload.get("s3_cur_region") or "").strip() or None

    try:
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": f"Database error: {exc}"}), 500

    return jsonify({"message": "CUR config saved.", "account": account.to_dict()}), 200


# ── CUR S3 Import ─────────────────────────────────────────────────────────────

@aws_bp.route("/aws-accounts/<int:account_id>/import-cur", methods=["POST"])
def import_cur(account_id):
    account = AwsAccount.query.get_or_404(account_id)

    if not account.s3_cur_bucket:
        return jsonify({"error": "No S3 CUR bucket configured. Edit account to set S3 Bucket and Prefix."}), 400

    payload           = request.get_json(silent=True) or {}
    from_month        = (payload.get("from_month") or "").strip() or None
    to_month          = (payload.get("to_month")   or "").strip() or None
    overwrite_fetched = bool(payload.get("overwrite_fetched", False))

    from app.cur_service import import_cur_for_account

    try:
        monthly_data = import_cur_for_account(account, from_month=from_month, to_month=to_month)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    summary = {"inserted": 0, "updated": 0, "skipped": 0, "preserved": 0, "zero": 0}
    month_results = []

    try:
        for row in monthly_data:
            month_str  = row["month"]
            cur_status = row["data_status"]
            cur_cloud  = row["cloud_service_cost"]
            cur_mp     = row["marketplace_cost"]
            src_files  = row["source_files"]

            from datetime import datetime as _dt
            month_date = _dt.strptime(month_str, "%Y-%m-%d").date()

            existing_rows = (
                CostRecord.query
                .filter_by(aws_account_id=account.id, consumption_month=month_date)
                .all()
            )
            existing = existing_rows[0] if existing_rows else None
            has_ce   = (
                existing is not None
                and existing.cost_status in ("fetched", "merged")
                and (float(existing.cloud_service_cost) > 0
                     or float(existing.marketplace_cost) > 0)
            )
            has_any  = (
                existing is not None
                and (float(existing.cloud_service_cost) > 0
                     or float(existing.marketplace_cost) > 0)
            )

            if cur_status == "cur_found":
                if has_ce and not overwrite_fetched:
                    summary["skipped"] += 1
                    month_results.append({
                        "month": month_str, "status": "skipped",
                        "reason": "CE data exists — not overwritten",
                    })
                    continue
                if existing:
                    existing.cloud_service_cost = round(cur_cloud, 4)
                    existing.marketplace_cost   = round(cur_mp, 4)
                    existing.cost_data_source   = f"CUR:{account.s3_cur_bucket}/{account.s3_cur_prefix}"
                    existing.cost_status        = "cur"
                    existing.is_auto_fetched    = True
                    summary["updated"] += 1
                else:
                    db.session.add(CostRecord(
                        aws_account_id=account.id,
                        contract_date=account.contract_date,
                        consumption_month=month_date,
                        cloud_service_cost=round(cur_cloud, 4),
                        marketplace_cost=round(cur_mp, 4),
                        is_auto_fetched=True,
                        cost_data_source=f"CUR:{account.s3_cur_bucket}/{account.s3_cur_prefix}",
                        cost_status="cur",
                    ))
                    summary["inserted"] += 1
                month_results.append({
                    "month": month_str, "status": "cur",
                    "action": "updated" if existing else "inserted",
                    "cloud_service_cost": round(cur_cloud, 2),
                    "marketplace_cost":   round(cur_mp, 2),
                    "source_files": src_files,
                })
            elif cur_status == "cur_zero":
                if has_any:
                    if existing.cost_status not in ("fetched", "cur"):
                        existing.cost_status = "preserved"
                    summary["preserved"] += 1
                    month_results.append({
                        "month": month_str, "status": "preserved",
                        "reason": "CUR shows $0 but existing record preserved",
                    })
                else:
                    summary["zero"] += 1
                    month_results.append({
                        "month": month_str, "status": "zero",
                        "reason": "CUR shows $0; no record inserted",
                        "source_files": src_files,
                    })

        db.session.commit()

    except SQLAlchemyError as exc:
        db.session.rollback()
        return jsonify({"error": f"Database error: {exc}"}), 500

    message = (
        f"CUR import complete for '{account.name}'. "
        f"Inserted: {summary['inserted']}, Updated: {summary['updated']}, "
        f"Skipped: {summary['skipped']}, Preserved: {summary['preserved']}, Zero: {summary['zero']}."
    )
    return jsonify({
        "success":   True,
        "message":   message,
        "summary":   summary,
        "months":    month_results,
        "account":   account.name,
        "s3_bucket": account.s3_cur_bucket,
        "s3_prefix": account.s3_cur_prefix,
    }), 200


# ── CUR Diagnostic ────────────────────────────────────────────────────────────

@aws_bp.route("/aws-accounts/<int:account_id>/cur-diagnose", methods=["GET"])
def cur_diagnose(account_id):
    account = AwsAccount.query.get_or_404(account_id)

    if not account.s3_cur_bucket:
        return jsonify({"error": "No S3 CUR bucket configured for this account."}), 400

    import gzip as _gzip
    import csv  as _csv
    import io   as _io
    from app.cur_service import _get_s3_client, _list_cur_files

    bucket = account.s3_cur_bucket.strip()
    prefix = (account.s3_cur_prefix or "").strip()
    region = (account.s3_cur_region or account.region or "us-east-1").strip()

    try:
        s3 = _get_s3_client(account, region)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        cur_files = _list_cur_files(s3, bucket, prefix, None)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not cur_files:
        return jsonify({
            "bucket": bucket, "prefix": prefix, "files_found": [],
            "warnings": ["No CUR files found. Check bucket name, prefix and IAM s3:ListBucket permission."],
        }), 200

    sample_file = cur_files[0]["key"]
    try:
        resp = s3.get_object(Bucket=bucket, Key=sample_file)
        raw  = resp["Body"].read()
        if sample_file.lower().endswith(".gz"):
            raw = _gzip.decompress(raw)
        text = raw.decode("utf-8-sig", errors="replace")
    except Exception as exc:
        return jsonify({"error": f"Could not download {sample_file}: {exc}"}), 400

    reader  = _csv.DictReader(_io.StringIO(text))
    columns = reader.fieldnames or []
    sample_rows, account_ids, item_types, costs_seen = [], set(), set(), []

    for i, row in enumerate(reader):
        if i < 5:
            sample_rows.append(dict(row))
        if i < 200:
            for col in ["lineItem/UsageAccountId", "line_item_usage_account_id"]:
                v = (row.get(col) or "").strip()
                if v:
                    account_ids.add(v)
            for col in ["lineItem/LineItemType", "line_item_line_item_type"]:
                v = (row.get(col) or "").strip()
                if v:
                    item_types.add(v)
            for col in ["lineItem/UnblendedCost", "line_item_unblended_cost"]:
                v = (row.get(col) or "").strip()
                try:
                    if v:
                        costs_seen.append(float(v))
                except Exception:
                    pass
        if i >= 200:
            break

    col_map = {
        "period_start":   next((c for c in columns if c in ("bill/BillingPeriodStartDate", "bill_billing_period_start_date")), None),
        "account_id":     next((c for c in columns if c in ("lineItem/UsageAccountId", "line_item_usage_account_id")), None),
        "item_type":      next((c for c in columns if c in ("lineItem/LineItemType", "line_item_line_item_type")), None),
        "billing_entity": next((c for c in columns if c in ("bill/BillingEntity", "bill_billing_entity")), None),
        "cost":           next((c for c in columns if c in ("lineItem/UnblendedCost", "line_item_unblended_cost")), None),
    }

    warnings = []
    for k, v in col_map.items():
        if v is None:
            warnings.append(f"Column '{k}' not found. Parser will skip this file.")

    linked = (account.aws_account_id or "").strip()
    if linked and account_ids and linked not in account_ids:
        warnings.append(
            f"Account ID '{linked}' not found in first 200 rows. "
            f"IDs seen: {sorted(account_ids)}. "
            "The CUR may be for a different account."
        )

    non_zero = [c for c in costs_seen if c != 0.0]
    if not non_zero:
        warnings.append("All cost values in first 200 rows are 0.0.")

    return jsonify({
        "bucket":              bucket,
        "prefix":              prefix,
        "region":              region,
        "linked_account_id":   linked or None,
        "files_found":         cur_files,
        "sample_file":         sample_file,
        "columns":             columns,
        "col_map":             col_map,
        "sample_rows":         sample_rows,
        "account_id_values":   sorted(account_ids),
        "item_types_seen":     sorted(item_types),
        "non_zero_cost_count": len(non_zero),
        "warnings":            warnings,
    }), 200

