"""
Safe migration script — creates missing tables and adds missing columns.
Run once after pulling new code that adds models or columns.

Usage:
    venv\\Scripts\\activate
    python migrate.py

What this does:
  1. Creates any missing tables (safe — never drops or modifies existing tables)
  2. Adds any missing columns to aws_accounts and cost_records
  3. Reports what was done

It is safe to run multiple times — each patch checks if the column already
exists before attempting to add it.
"""

from app import create_app, db
from sqlalchemy import text, inspect

app = create_app()

with app.app_context():
    inspector     = inspect(db.engine)
    existing_tabs = inspector.get_table_names()

    print("=== Cloud P&L Migration ===")
    print(f"DB:     {db.engine.url.host}:{db.engine.url.port}/{db.engine.url.database}")
    print(f"Tables: {existing_tabs}")

    # ── Step 1: create any missing tables ────────────────────────────────────
    db.create_all()
    print("\n[OK] db.create_all() — all missing tables created.")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def col_exists(table: str, col: str) -> bool:
        if table not in existing_tabs:
            return False
        return col in {c["name"] for c in inspector.get_columns(table)}

    def run_sql(conn, sql: str, description: str = "") -> None:
        label = description or sql[:80]
        print(f"  -> {label}")
        conn.execute(text(sql))

    # ── Step 2: aws_accounts column patches ──────────────────────────────────
    aws_patches = []

    # is_manual — manual accounts with no IAM keys
    if not col_exists("aws_accounts", "is_manual"):
        aws_patches.append((
            "ADD is_manual",
            "ALTER TABLE aws_accounts "
            "ADD COLUMN is_manual TINYINT(1) NOT NULL DEFAULT 0 "
            "COMMENT 'Manual account — no AWS credentials'"
        ))

    # csp — Cloud Service Provider (AWS / GCP / Azure)
    if not col_exists("aws_accounts", "csp"):
        aws_patches.append((
            "ADD csp",
            "ALTER TABLE aws_accounts "
            "ADD COLUMN csp VARCHAR(20) NOT NULL DEFAULT 'AWS' "
            "COMMENT 'Cloud Service Provider: AWS | GCP | Azure'"
        ))

    # CUR S3 export configuration
    if not col_exists("aws_accounts", "s3_cur_bucket"):
        aws_patches.append((
            "ADD s3_cur_bucket",
            "ALTER TABLE aws_accounts "
            "ADD COLUMN s3_cur_bucket VARCHAR(120) NULL "
            "COMMENT 'S3 bucket name for CUR export'"
        ))

    if not col_exists("aws_accounts", "s3_cur_prefix"):
        aws_patches.append((
            "ADD s3_cur_prefix",
            "ALTER TABLE aws_accounts "
            "ADD COLUMN s3_cur_prefix VARCHAR(200) NULL "
            "COMMENT 'S3 prefix/path for CUR files, e.g. wealwin/'"
        ))

    if not col_exists("aws_accounts", "s3_cur_region"):
        aws_patches.append((
            "ADD s3_cur_region",
            "ALTER TABLE aws_accounts "
            "ADD COLUMN s3_cur_region VARCHAR(30) NULL "
            "COMMENT 'AWS region of the S3 bucket'"
        ))

    # contract_end_date — last month of contract (NULL = open/active)
    if not col_exists("aws_accounts", "contract_end_date"):
        aws_patches.append((
            "ADD contract_end_date",
            "ALTER TABLE aws_accounts "
            "ADD COLUMN contract_end_date DATE NULL "
            "COMMENT 'Last month of contract; NULL = open-ended'"
        ))

    # billing_currency — USD for AWS, INR for GCP/Azure
    if not col_exists("aws_accounts", "billing_currency"):
        aws_patches.append((
            "ADD billing_currency",
            "ALTER TABLE aws_accounts "
            "ADD COLUMN billing_currency VARCHAR(3) NOT NULL DEFAULT 'USD' "
            "COMMENT 'USD = cost stored in USD; INR = cost stored in INR natively'"
        ))
    # Ensure credentials columns are nullable (older schema had NOT NULL)
    if col_exists("aws_accounts", "access_key_id_enc"):
        aws_patches.append((
            "MAKE access_key_id_enc nullable",
            "ALTER TABLE aws_accounts MODIFY COLUMN access_key_id_enc TEXT NULL"
        ))
    if col_exists("aws_accounts", "secret_access_key_enc"):
        aws_patches.append((
            "MAKE secret_access_key_enc nullable",
            "ALTER TABLE aws_accounts MODIFY COLUMN secret_access_key_enc TEXT NULL"
        ))

    # UNIQUE index on aws_account_id (prevents duplicate customer accounts)
    try:
        idxs = {i["name"] for i in inspector.get_indexes("aws_accounts")}
        if "uq_aws_account_id" not in idxs:
            aws_patches.append((
                "ADD UNIQUE index on aws_account_id",
                "ALTER TABLE aws_accounts "
                "ADD CONSTRAINT uq_aws_account_id UNIQUE (aws_account_id)"
            ))
    except Exception:
        pass  # index inspection is non-critical

    # ── Step 3: cost_records column patches ───────────────────────────────────
    cr_patches = []

    # invoice_override — optional manual invoice override (NULL = use formula)
    if not col_exists("cost_records", "invoice_override"):
        cr_patches.append((
            "ADD invoice_override",
            "ALTER TABLE cost_records "
            "ADD COLUMN invoice_override DECIMAL(18,4) NULL "
            "COMMENT 'Optional manual invoice override; NULL = use formula'"
        ))

    # aws_account_id FK
    if not col_exists("cost_records", "aws_account_id"):
        cr_patches.append((
            "ADD aws_account_id FK",
            "ALTER TABLE cost_records "
            "ADD COLUMN aws_account_id INT NULL, "
            "ADD CONSTRAINT fk_cost_aws_account "
            "FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id) ON DELETE SET NULL"
        ))

    # Fetch tracking
    if not col_exists("cost_records", "is_auto_fetched"):
        cr_patches.append((
            "ADD is_auto_fetched",
            "ALTER TABLE cost_records "
            "ADD COLUMN is_auto_fetched TINYINT(1) NOT NULL DEFAULT 0"
        ))

    if not col_exists("cost_records", "cost_data_source"):
        cr_patches.append((
            "ADD cost_data_source",
            "ALTER TABLE cost_records "
            "ADD COLUMN cost_data_source VARCHAR(80) NULL "
            "COMMENT 'Account/payer ID that supplied this month cost'"
        ))

    if not col_exists("cost_records", "cost_status"):
        cr_patches.append((
            "ADD cost_status",
            "ALTER TABLE cost_records "
            "ADD COLUMN cost_status VARCHAR(20) NOT NULL DEFAULT 'manual' "
            "COMMENT 'fetched | preserved | unavailable | zero | cur | manual'"
        ))

    # Split-month support
    if not col_exists("cost_records", "is_split"):
        cr_patches.append((
            "ADD is_split",
            "ALTER TABLE cost_records "
            "ADD COLUMN is_split TINYINT(1) NOT NULL DEFAULT 0 "
            "COMMENT '1 = part of a split-month pair'"
        ))

    if not col_exists("cost_records", "split_month_group"):
        cr_patches.append((
            "ADD split_month_group",
            "ALTER TABLE cost_records "
            "ADD COLUMN split_month_group VARCHAR(36) NULL "
            "COMMENT 'UUID shared by all rows for the same split month'"
        ))

    # ── Step 4: apply all patches ─────────────────────────────────────────────
    all_patches = aws_patches + cr_patches

    if all_patches:
        print(f"\n[INFO] Applying {len(all_patches)} schema patch(es)...")
        with db.engine.connect() as conn:
            for desc, sql in all_patches:
                try:
                    run_sql(conn, sql, desc)
                except Exception as exc:
                    print(f"  [SKIP] {desc}: {exc}")
            conn.commit()
        print("\n[OK] Schema patches applied.")
    else:
        print("\n[OK] Schema already up to date — no patches needed.")

    # ── Step 5: summary ───────────────────────────────────────────────────────
    print("\n=== Migration complete ===")
    print("aws_accounts columns:", [c["name"] for c in inspector.get_columns("aws_accounts")])
    if "cost_records" in existing_tabs:
        print("cost_records columns:", [c["name"] for c in inspector.get_columns("cost_records")])

