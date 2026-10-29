-- Run this once to create the database
CREATE DATABASE IF NOT EXISTS cloud_pnl
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE cloud_pnl;

-- Tables are also created automatically by SQLAlchemy (db.create_all)
-- but you can also create them manually:

CREATE TABLE IF NOT EXISTS aws_accounts (
    id                    INT           NOT NULL AUTO_INCREMENT,
    name                  VARCHAR(120)  NOT NULL,
    aws_account_id        VARCHAR(20)   NULL UNIQUE COMMENT '12-digit member/child account ID',
    access_key_id_enc     TEXT          NULL COMMENT 'Fernet-encrypted IAM access key (legacy, kept for compat)',
    secret_access_key_enc TEXT          NULL COMMENT 'Fernet-encrypted IAM secret key (legacy, kept for compat)',
    region                VARCHAR(30)   NOT NULL DEFAULT 'us-east-1',
    contract_date         DATE          NOT NULL,
    is_active             TINYINT(1)    NOT NULL DEFAULT 1,
    is_manual             TINYINT(1)    NOT NULL DEFAULT 0,
    -- CUR S3 export configuration (optional — used when Cost Explorer returns $0)
    s3_cur_bucket         VARCHAR(120)  NULL COMMENT 'S3 bucket name for CUR export',
    s3_cur_prefix         VARCHAR(200)  NULL COMMENT 'S3 prefix/path for CUR files, e.g. wealwin/',
    s3_cur_region         VARCHAR(30)   NULL COMMENT 'AWS region of the S3 bucket, e.g. us-east-1',
    -- Cloud Service Provider: AWS | GCP | Azure
    csp                   VARCHAR(20)   NOT NULL DEFAULT 'AWS' COMMENT 'Cloud Service Provider',
    created_at            DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Management/payer account history per aws_account.
-- One aws_account can have multiple payers over time.
-- Only ONE payer can be active (is_active=1) at any point for a given aws_account.
CREATE TABLE IF NOT EXISTS aws_account_payers (
    id                      INT           NOT NULL AUTO_INCREMENT,
    aws_account_id          INT           NOT NULL COMMENT 'FK to aws_accounts.id (the member/child account)',
    payer_account_id        VARCHAR(20)   NOT NULL COMMENT '12-digit management/payer AWS account ID',
    management_account_name VARCHAR(120)  NULL     COMMENT 'Human-readable label for the payer',
    access_key_id_enc       TEXT          NULL     COMMENT 'Fernet-encrypted IAM access key for this payer',
    secret_access_key_enc   TEXT          NULL     COMMENT 'Fernet-encrypted IAM secret key for this payer',
    region                  VARCHAR(30)   NOT NULL DEFAULT 'us-east-1',
    is_active               TINYINT(1)   NOT NULL DEFAULT 0,
    valid_from              DATE          NOT NULL,
    valid_to                DATE          NULL     COMMENT 'NULL while still active',
    remarks                 TEXT          NULL,
    created_at              DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at              DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT fk_payer_aws_account
        FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS cost_records (
    id                    INT           NOT NULL AUTO_INCREMENT,
    aws_account_id        INT           NULL,
    contract_date         DATE          NOT NULL,
    consumption_month     DATE          NOT NULL,
    cloud_service_cost    DECIMAL(18,4) NOT NULL DEFAULT 0,
    marketplace_cost      DECIMAL(18,4) NOT NULL DEFAULT 0,
    distributor_discount  DECIMAL(18,4) NOT NULL DEFAULT 0,
    credit_amount         DECIMAL(18,4) NOT NULL DEFAULT 0,
    customer_discount     DECIMAL(18,4) NOT NULL DEFAULT 0,
    managed_services      DECIMAL(18,4) NOT NULL DEFAULT 0,
    cash_claim            DECIMAL(18,4) NOT NULL DEFAULT 0,
    conversion_rate       DECIMAL(10,6) NOT NULL DEFAULT 1,
    redington_credit_note DECIMAL(18,4) NOT NULL DEFAULT 0,
    remarks               TEXT,
    is_auto_fetched       TINYINT(1)    NOT NULL DEFAULT 0,
    cost_data_source      VARCHAR(80)   NULL     COMMENT '12-digit payer account ID that supplied this month cost',
    cost_status           VARCHAR(20)   NOT NULL DEFAULT 'manual',
    -- Split-month support: when a management account changes mid-month,
    -- two rows exist for the same (aws_account_id, consumption_month).
    -- split_month_group links them together (shared UUID string).
    -- is_split=0 = normal single-payer month row.
    -- is_split=1 = one portion of a split month (payer A half or payer B half).
    is_split              TINYINT(1)    NOT NULL DEFAULT 0 COMMENT '1 = part of a split-month pair',
    split_month_group     VARCHAR(36)   NULL     COMMENT 'UUID shared by all rows for the same split month',
    created_at            DATETIME      DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    CONSTRAINT fk_cost_aws_account
        FOREIGN KEY (aws_account_id) REFERENCES aws_accounts(id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
