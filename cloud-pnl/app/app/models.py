from app import db
from datetime import datetime


# ─────────────────────────────────────────────────────────────────────────────
# AWS Account  (the actual customer / member account — NEVER duplicated)
# ─────────────────────────────────────────────────────────────────────────────

class AwsAccount(db.Model):
    __tablename__ = "aws_accounts"

    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name           = db.Column(db.String(120), nullable=False)

    # 12-digit member/child AWS account ID — UNIQUE, prevents duplicate accounts
    aws_account_id = db.Column(db.String(20), nullable=True, unique=True)

    # Legacy credential columns kept for backward compatibility.
    # New code reads credentials from the active aws_account_payers row.
    _access_key_id_enc     = db.Column("access_key_id_enc",     db.Text, nullable=True)
    _secret_access_key_enc = db.Column("secret_access_key_enc", db.Text, nullable=True)

    region        = db.Column(db.String(30),  nullable=False, default="us-east-1")
    contract_date = db.Column(db.Date,        nullable=False)
    contract_end_date = db.Column(db.Date,    nullable=True)   # NULL = open / active contract
    is_active     = db.Column(db.Boolean,     nullable=False, default=True)
    is_manual     = db.Column(db.Boolean,     nullable=False, default=False)

    # ── CUR S3 configuration (optional) ──────────────────────────────────────
    # When Cost Explorer returns $0 for historical months (e.g. after a payer
    # change), CUR files in S3 can be used to fill the gaps.
    # The same IAM credentials (active_payer) are used — add s3:GetObject and
    # s3:ListBucket to the IAM policy.
    s3_cur_bucket = db.Column(db.String(120), nullable=True)   # e.g. cloud-p-l
    s3_cur_prefix = db.Column(db.String(200), nullable=True)   # e.g. wealwin/
    s3_cur_region = db.Column(db.String(30),  nullable=True)   # e.g. us-east-1

    # Cloud Service Provider — AWS | GCP | Azure (default AWS)
    csp              = db.Column(db.String(20),  nullable=False, default="AWS")
    # Billing currency — USD for AWS (converted via conversion_rate), INR for GCP/Azure
    billing_currency = db.Column(db.String(3),   nullable=False, default="USD")

    created_at    = db.Column(db.DateTime,    default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime,    default=datetime.utcnow,
                              onupdate=datetime.utcnow)

    # Relationships
    cost_records = db.relationship(
        "CostRecord", backref="aws_account", lazy=True,
        foreign_keys="CostRecord.aws_account_id",
    )
    # Note: aws_account_payers table still exists in DB for historical data
    # but is no longer used in application logic.

    # ── Credential helpers (legacy — still used for backward compat) ──────────

    def set_access_key_id(self, plaintext):
        from app.crypto import encrypt
        self._access_key_id_enc = encrypt(plaintext)

    def get_access_key_id(self):
        from app.crypto import decrypt
        return decrypt(self._access_key_id_enc)

    def set_secret_access_key(self, plaintext):
        from app.crypto import encrypt
        self._secret_access_key_enc = encrypt(plaintext)

    def get_secret_access_key(self):
        from app.crypto import decrypt
        return decrypt(self._secret_access_key_enc)

    def masked_access_key(self):
        """Return a masked version of the stored access key."""
        if not self._access_key_id_enc:
            return "-"
        raw = self.get_access_key_id()
        return (raw[:4] + "*" * (len(raw) - 4)) if raw else "****"

    def to_dict(self):
        return {
            "id":                   self.id,
            "name":                 self.name,
            "aws_account_id":       self.aws_account_id,
            "access_key_id_masked": self.masked_access_key(),
            "region":               self.region,
            "contract_date":        self.contract_date.isoformat() if self.contract_date else None,
            "contract_end_date":    self.contract_end_date.isoformat() if self.contract_end_date else None,
            "is_active":            self.is_active,
            "is_manual":            self.is_manual,
            "csp":                  self.csp or "AWS",
            "billing_currency":     self.billing_currency or "USD",
            "s3_cur_bucket":        self.s3_cur_bucket,
            "s3_cur_prefix":        self.s3_cur_prefix,
            "s3_cur_region":        self.s3_cur_region,
            "created_at":           self.created_at.isoformat() if self.created_at else None,
            "updated_at":           self.updated_at.isoformat() if self.updated_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# AWS Account Payer  (management/payer account history)
# ─────────────────────────────────────────────────────────────────────────────

class AwsAccountPayer(db.Model):
    __tablename__ = "aws_account_payers"

    id                      = db.Column(db.Integer, primary_key=True, autoincrement=True)
    aws_account_id          = db.Column(
        db.Integer,
        db.ForeignKey("aws_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 12-digit management/payer AWS account ID
    payer_account_id        = db.Column(db.String(20), nullable=False)
    management_account_name = db.Column(db.String(120), nullable=True)

    # Per-payer encrypted credentials
    _access_key_id_enc      = db.Column("access_key_id_enc",     db.Text, nullable=True)
    _secret_access_key_enc  = db.Column("secret_access_key_enc", db.Text, nullable=True)

    region     = db.Column(db.String(30),  nullable=False, default="us-east-1")
    is_active  = db.Column(db.Boolean,     nullable=False, default=False)
    valid_from = db.Column(db.Date,        nullable=False)
    valid_to   = db.Column(db.Date,        nullable=True)   # NULL while active
    remarks    = db.Column(db.Text,        nullable=True)
    created_at = db.Column(db.DateTime,    default=datetime.utcnow)
    updated_at = db.Column(db.DateTime,    default=datetime.utcnow,
                           onupdate=datetime.utcnow)

    # ── Credential helpers ────────────────────────────────────────────────────

    def set_access_key_id(self, plaintext):
        from app.crypto import encrypt
        self._access_key_id_enc = encrypt(plaintext)

    def get_access_key_id(self):
        from app.crypto import decrypt
        return decrypt(self._access_key_id_enc) if self._access_key_id_enc else ""

    def set_secret_access_key(self, plaintext):
        from app.crypto import encrypt
        self._secret_access_key_enc = encrypt(plaintext)

    def get_secret_access_key(self):
        from app.crypto import decrypt
        return decrypt(self._secret_access_key_enc) if self._secret_access_key_enc else ""

    def masked_access_key(self):
        if not self._access_key_id_enc:
            return "-"
        raw = self.get_access_key_id()
        return (raw[:4] + "*" * (len(raw) - 4)) if raw else "****"

    def to_dict(self):
        return {
            "id":                      self.id,
            "aws_account_id":          self.aws_account_id,
            "payer_account_id":        self.payer_account_id,
            "management_account_name": self.management_account_name,
            "access_key_id_masked":    self.masked_access_key(),
            "region":                  self.region,
            "is_active":               self.is_active,
            "valid_from":              self.valid_from.isoformat() if self.valid_from else None,
            "valid_to":                self.valid_to.isoformat()   if self.valid_to   else None,
            "remarks":                 self.remarks,
            "created_at":              self.created_at.isoformat() if self.created_at else None,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Cost Record
# ─────────────────────────────────────────────────────────────────────────────

class CostRecord(db.Model):
    """
    Monthly cost record for a member/customer AWS account.

    FORMULAS
    --------
    total_consumption   = cloud_service_cost + marketplace_cost

    distributor_disc_amt = cloud_service_cost * (distributor_discount / 100)
      (applies on cloud cost ONLY, not marketplace)

    credit_amount        = flat USD (deducted from cloud cost only)

    customer_disc_amt    = total_consumption * (customer_discount / 100)
    managed_services_amt = total_consumption * (managed_services  / 100)

    ILIOS Spend  = total_consumption
                   - distributor_disc_amt
                   - credit_amount
                   - managed_services_amt
                   - customer_disc_amt
                   - redington_credit_note

    Invoice to Customer = total_consumption
                          - customer_disc_amt
                          + managed_services_amt

    ILIOS Margin = invoice_to_customer - ilios_spend

    All USD values * conversion_rate = INR equivalent
    """

    __tablename__ = "cost_records"

    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    aws_account_id = db.Column(
        db.Integer, db.ForeignKey("aws_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )

    contract_date     = db.Column(db.Date, nullable=False)
    consumption_month = db.Column(db.Date, nullable=False)

    cloud_service_cost = db.Column(db.Numeric(18, 4), nullable=False, default=0)
    marketplace_cost   = db.Column(db.Numeric(18, 4), nullable=False, default=0)

    distributor_discount = db.Column(db.Numeric(8, 4), nullable=False, default=0)
    customer_discount    = db.Column(db.Numeric(8, 4), nullable=False, default=0)
    managed_services     = db.Column(db.Numeric(8, 4), nullable=False, default=0)

    credit_amount         = db.Column(db.Numeric(18, 4), nullable=False, default=0)
    cash_claim            = db.Column(db.Numeric(18, 4), nullable=False, default=0)
    redington_credit_note = db.Column(db.Numeric(18, 4), nullable=False, default=0)

    conversion_rate = db.Column(db.Numeric(10, 4), nullable=False, default=1)
    remarks         = db.Column(db.Text, nullable=True)
    is_auto_fetched = db.Column(db.Boolean, nullable=False, default=False)

    # 12-digit payer account ID that supplied this month's cost data
    cost_data_source = db.Column(db.String(80), nullable=True)

    # fetched | preserved | unavailable | manual
    cost_status = db.Column(db.String(20), nullable=False, default="manual")

    # ── Split-month support ───────────────────────────────────────────────────
    # When a management account changes mid-month, two (or more) rows exist for
    # the same (aws_account_id, consumption_month) — one per payer segment.
    # is_split=True  → this row is one portion of a split month.
    # split_month_group → UUID string shared by all rows of the same split month.
    is_split          = db.Column(db.Boolean,    nullable=False, default=False)
    split_month_group = db.Column(db.String(36), nullable=True)   # UUID

    # Optional invoice override — NULL = use formula; set = use this value instead
    invoice_override  = db.Column(db.Numeric(18, 4), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def total_consumption(self):
        return float(self.cloud_service_cost) + float(self.marketplace_cost)

    @property
    def distributor_discount_amt(self):
        return float(self.cloud_service_cost) * float(self.distributor_discount) / 100.0

    @property
    def customer_discount_amt(self):
        # Customer discount applies on cloud service cost only (not marketplace)
        return float(self.cloud_service_cost) * float(self.customer_discount) / 100.0

    @property
    def managed_services_amt(self):
        return self.total_consumption * float(self.managed_services) / 100.0

    @property
    def ilios_spend(self):
        """
        What ILIOS actually pays to the cloud provider.
        Deductions: distributor discount, credits, redington credit note.
        Does NOT include managed services or customer discount
        (those affect the invoice to customer, not ILIOS's own cost).

        Note: redington_credit_note is stored in INR (distributor credit note).
        All other values are in the account's billing currency (USD for AWS).
        We normalise redington_credit_note to the same currency via conversion_rate.
        """
        fx = float(self.conversion_rate) or 1.0
        # For USD accounts: redington_credit_note stored in INR → convert to USD
        # For INR accounts: fx=1, so division is a no-op
        red_normalised = float(self.redington_credit_note) / fx
        return (
            self.total_consumption
            - self.distributor_discount_amt
            - float(self.credit_amount)
            - red_normalised
        )

    @property
    def invoice_to_customer(self):
        """
        What the customer is billed.
        Uses invoice_override if explicitly set, otherwise calculates from formula.
        Customer discount reduces the bill; managed services fee is added.
        """
        if self.invoice_override is not None:
            return float(self.invoice_override)
        return (
            self.total_consumption
            - self.customer_discount_amt
            + self.managed_services_amt
        )

    @property
    def ilios_margin(self):
        """
        ILIOS profit = what customer pays minus what ILIOS pays,
        plus any cash claim received.
        """
        return self.invoice_to_customer - self.ilios_spend + float(self.cash_claim)

    @property
    def fx(self):
        return float(self.conversion_rate)

    @property
    def total_consumption_inr(self):
        return self.total_consumption * self.fx

    @property
    def ilios_spend_inr(self):
        return self.ilios_spend * self.fx

    @property
    def invoice_to_customer_inr(self):
        return self.invoice_to_customer * self.fx

    @property
    def ilios_margin_inr(self):
        return self.ilios_margin * self.fx

    def to_dict(self):
        return {
            "id":               self.id,
            "aws_account_id":   self.aws_account_id,
            "aws_account_name": self.aws_account.name if self.aws_account else None,
            "aws_child_account_id": self.aws_account.aws_account_id if self.aws_account else None,
            "contract_date":    self.contract_date.isoformat() if self.contract_date else None,
            "consumption_month":self.consumption_month.isoformat() if self.consumption_month else None,

            "cloud_service_cost":    float(self.cloud_service_cost),
            "marketplace_cost":      float(self.marketplace_cost),
            "distributor_discount":  float(self.distributor_discount),
            "customer_discount":     float(self.customer_discount),
            "managed_services":      float(self.managed_services),
            "credit_amount":         float(self.credit_amount),
            "cash_claim":            float(self.cash_claim),
            "redington_credit_note": float(self.redington_credit_note),
            "conversion_rate":       float(self.conversion_rate),
            "remarks":               self.remarks,
            "is_auto_fetched":       self.is_auto_fetched,
            "cost_data_source":      self.cost_data_source,
            "cost_status":           self.cost_status,
            "invoice_override":      float(self.invoice_override) if self.invoice_override is not None else None,
            "is_split":              self.is_split,
            "split_month_group":     self.split_month_group,

            "distributor_discount_amt": round(self.distributor_discount_amt, 4),
            "customer_discount_amt":    round(self.customer_discount_amt,    4),
            "managed_services_amt":     round(self.managed_services_amt,     4),

            "total_consumption":   round(self.total_consumption,   4),
            "ilios_spend":         round(self.ilios_spend,         4),
            "invoice_to_customer": round(self.invoice_to_customer, 4),
            "ilios_margin":        round(self.ilios_margin,        4),

            "total_consumption_inr":   round(self.total_consumption_inr,   2),
            "ilios_spend_inr":         round(self.ilios_spend_inr,         2),
            "invoice_to_customer_inr": round(self.invoice_to_customer_inr, 2),
            "ilios_margin_inr":        round(self.ilios_margin_inr,        2),

            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

