import os
import json
import csv
import io
import secrets
import hashlib
from datetime import datetime, date, timedelta

from decimal import Decimal

import requests
from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    abort,
    send_file,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    current_user,
    login_required,
    UserMixin,
)
from werkzeug.security import generate_password_hash, check_password_hash

# -------------------------------------------------
# App & config
# -------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# In Docker, DATABASE_URL will be set to mysql+pymysql://invoicemgr:invoicemgr@db/invoicemanager
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://invoicemgr:invoicemgr@db/invoicemanager",
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------------------------------------
# Models
# -------------------------------------------------

class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    default_tax_rate = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("0.00"))

    # Outbound webhook config
    outbound_webhook_url = db.Column(db.String(500))
    outbound_webhook_enabled = db.Column(db.Boolean, nullable=False, default=False)
    # Comma-separated list of event names: invoice.created, invoice.updated, payment.recorded
    outbound_webhook_events = db.Column(db.String(255), nullable=False, default="")

    # Branding
    brand_name = db.Column(db.String(255))
    logo_url = db.Column(db.String(500))

    # Payment terms
    payment_terms_days = db.Column(db.Integer, nullable=False, default=0)
    use_global_payment_terms = db.Column(db.Boolean, nullable=False, default=False)

    @classmethod
    def load(cls):
        settings = cls.query.get(1)
        if not settings:
            settings = cls(
                id=1,
                default_tax_rate=Decimal("0.00"),
                outbound_webhook_enabled=False,
                outbound_webhook_events="",
                payment_terms_days=0,
                use_global_payment_terms=False,
            )
            db.session.add(settings)
            db.session.commit()
        # Defensive: avoid NULL in NOT NULL column
        changed = False
        if settings.outbound_webhook_events is None:
            settings.outbound_webhook_events = ""
            changed = True
        if settings.payment_terms_days is None:
            settings.payment_terms_days = 0
            changed = True
        if settings.use_global_payment_terms is None:
            settings.use_global_payment_terms = False
            changed = True
        if changed:
            db.session.commit()
        return settings

    def selected_webhook_events(self):
        if not self.outbound_webhook_events:
            return set()
        return {
            e.strip()
            for e in self.outbound_webhook_events.split(",")
            if e.strip()
        }


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    phone = db.Column(db.String(50))
    address_line1 = db.Column(db.String(255))
    address_line2 = db.Column(db.String(255))
    city = db.Column(db.String(100))
    postcode = db.Column(db.String(50))
    country = db.Column(db.String(100))

    tax_rate = db.Column(db.Numeric(5, 2))
    use_default_tax = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    invoices = db.relationship("Invoice", back_populates="customer")

    def __repr__(self):
        return f"<Customer {self.id} {self.name}>"


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    def __repr__(self):
        return f"<Product {self.id} {self.name}>"


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    status = db.Column(
        db.Enum("draft", "sent", "paid", "overdue", "cancelled", name="invoice_status"),
        nullable=False,
        default="draft",
    )
    notes = db.Column(db.Text)

    subtotal_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    tax_rate = db.Column(db.Numeric(5, 2))
    tax_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    balance_due = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    customer = db.relationship("Customer", back_populates="invoices")
    items = db.relationship(
        "InvoiceItem",
        back_populates="invoice",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    payments = db.relationship(
        "Payment",
        back_populates="invoice",
        cascade="all, delete-orphan",
    )

    def recalc_totals(self):
        subtotal = sum((item.line_total for item in self.items), Decimal("0.00"))
        self.subtotal_amount = subtotal

        rate = self.tax_rate if self.tax_rate is not None else Decimal("0.00")
        tax_amount = (subtotal * rate / Decimal("100.00")).quantize(Decimal("0.01"))
        self.tax_amount = tax_amount

        total = subtotal + tax_amount
        self.total_amount = total

        paid = sum((p.amount for p in self.payments), Decimal("0.00"))
        self.balance_due = total - paid

        if self.balance_due <= 0 and total > 0:
            self.status = "paid"
        elif self.due_date and date.today() > self.due_date and self.status not in (
            "paid",
            "cancelled",
        ):
            self.status = "overdue"
        else:
            if self.status not in ("cancelled", "paid"):
                self.status = "sent"

    def to_dict(self, include_items=True, include_payments=True):
        data = {
            "id": self.id,
            "invoice_number": self.invoice_number,
            "customer_id": self.customer_id,
            "customer_name": self.customer.name if self.customer else None,
            "issue_date": self.issue_date.isoformat(),
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "status": self.status,
            "notes": self.notes,
            "subtotal_amount": float(self.subtotal_amount),
            "tax_rate": float(self.tax_rate) if self.tax_rate is not None else None,
            "tax_amount": float(self.tax_amount),
            "total_amount": float(self.total_amount),
            "balance_due": float(self.balance_due),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if include_items:
            data["items"] = [item.to_dict() for item in self.items]
        if include_payments:
            data["payments"] = [p.to_dict() for p in self.payments]
        return data


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    line_total = db.Column(db.Numeric(10, 2), nullable=False)

    invoice = db.relationship("Invoice", back_populates="items")
    product = db.relationship("Product")

    def to_dict(self):
        return {
            "id": self.id,
            "product_id": self.product_id,
            "product_name": self.product.name if self.product else None,
            "description": self.description,
            "quantity": float(self.quantity),
            "unit_price": float(self.unit_price),
            "line_total": float(self.line_total),
        }


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False)
    method = db.Column(db.String(100))
    external_reference = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    invoice = db.relationship("Invoice", back_populates="payments")

    def to_dict(self):
        return {
            "id": self.id,
            "invoice_id": self.invoice_id,
            "amount": float(self.amount),
            "payment_date": self.payment_date.isoformat(),
            "method": self.method,
            "external_reference": self.external_reference,
            "created_at": self.created_at.isoformat(),
        }


class ApiKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)

    # Short ID used in UI
    key_id = db.Column(db.String(32), unique=True, nullable=False)
    # SHA256 hash of the actual secret
    key_hash = db.Column(db.String(64), unique=True, nullable=False)

    can_read = db.Column(db.Boolean, nullable=False, default=True)
    can_write = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime)


# -------------------------------------------------
# Login manager
# -------------------------------------------------

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# -------------------------------------------------
# Helper functions
# -------------------------------------------------

def generate_invoice_number():
    today_str = date.today().strftime("%Y%m%d")
    last = (
        Invoice.query.filter(Invoice.invoice_number.like(f"INV-{today_str}-%"))
        .order_by(Invoice.id.desc())
        .first()
    )
    if not last:
        seq = 1
    else:
        try:
            seq = int(last.invoice_number.split("-")[-1]) + 1
        except Exception:
            seq = 1
    return f"INV-{today_str}-{seq:04d}"


def determine_tax_rate_for_customer(customer: Customer | None):
    settings = Settings.load()
    if customer is None:
        return settings.default_tax_rate
    if customer.use_default_tax or customer.tax_rate is None:
        return settings.default_tax_rate
    return customer.tax_rate


def apply_global_payment_terms(issue_date: date, settings: Settings) -> date | None:
    if not settings.use_global_payment_terms or not settings.payment_terms_days:
        return None
    return issue_date + timedelta(days=int(settings.payment_terms_days))


def send_outbound_webhook(event_type: str, payload: dict):
    """
    Send JSON webhook if enabled and event selected.
    """
    settings = Settings.load()
    if not settings.outbound_webhook_enabled or not settings.outbound_webhook_url:
        return

    selected = settings.selected_webhook_events()
    # If some events are ticked and this one isn't, skip
    if selected and event_type not in selected:
        return

    body = {
        "event": event_type,
        "data": payload,
        "sent_at": datetime.utcnow().isoformat() + "Z",
    }

    try:
        resp = requests.post(
            settings.outbound_webhook_url,
            json=body,
            timeout=5,
        )
        print(
            f"[webhook] Sent {event_type} -> {settings.outbound_webhook_url} "
            f"({resp.status_code})"
        )
    except Exception as e:
        print(f"[webhook] Error sending {event_type}: {e}")


# ----- API key auth helpers -----

def _find_api_key(raw_value: str) -> ApiKey | None:
    key_hash = hashlib.sha256(raw_value.encode("utf-8")).hexdigest()
    return ApiKey.query.filter_by(key_hash=key_hash, active=True).first()


def require_api_key(write: bool = False) -> ApiKey:
    raw = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not raw:
        abort(401, description="Missing API key")

    api_key = _find_api_key(raw)
    if not api_key:
        abort(401, description="Invalid or inactive API key")

    if write and not api_key.can_write:
        abort(403, description="API key does not have write permission")

    api_key.last_used_at = datetime.utcnow()
    db.session.commit()
    return api_key


# -------------------------------------------------
# Context processors
# -------------------------------------------------

@app.context_processor
def inject_settings_and_datetime():
    settings = Settings.load()
    return {"settings": settings, "datetime": datetime}


# -------------------------------------------------
# Auth routes
# -------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("list_invoices"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("Invalid username or password", "danger")
            return render_template("login.html")

        login_user(user)
        flash("Logged in", "success")
        next_url = request.args.get("next")
        return redirect(next_url or url_for("list_invoices"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "success")
    return redirect(url_for("login"))


# -------------------------------------------------
# Basic navigation
# -------------------------------------------------

@app.route("/")
@login_required
def index():
    return redirect(url_for("list_invoices"))


# -------------------------------------------------
# Settings & API docs
# -------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    settings = Settings.load()

    if request.method == "POST":
        # General
        brand_name = (request.form.get("brand_name") or "").strip() or None
        logo_url = (request.form.get("logo_url") or "").strip() or None

        # Tax
        rate_str = (request.form.get("default_tax_rate") or "").strip()
        try:
            settings.default_tax_rate = Decimal(rate_str or "0")
        except Exception:
            flash("Invalid default tax rate", "danger")
            return redirect(url_for("settings_view"))

        # Payment terms
        use_global = bool(request.form.get("use_global_payment_terms"))
        days_str = (request.form.get("payment_terms_days") or "").strip()
        try:
            days_val = int(days_str or "0")
        except Exception:
            days_val = 0

        settings.use_global_payment_terms = use_global
        settings.payment_terms_days = days_val

        settings.brand_name = brand_name
        settings.logo_url = logo_url

        # Webhook
        settings.outbound_webhook_enabled = bool(
            request.form.get("outbound_webhook_enabled")
        )
        url_val = (request.form.get("outbound_webhook_url") or "").strip()
        settings.outbound_webhook_url = url_val or None

        selected_events = []
        if request.form.get("event_invoice_created"):
            selected_events.append("invoice.created")
        if request.form.get("event_invoice_updated"):
            selected_events.append("invoice.updated")
        if request.form.get("event_payment_recorded"):
            selected_events.append("payment.recorded")

        # Store as comma-separated string, never None
        settings.outbound_webhook_events = ",".join(selected_events)

        db.session.commit()
        flash("Settings saved", "success")
        return redirect(url_for("settings_view"))

    selected = settings.selected_webhook_events()
    return render_template(
        "settings.html",
        settings=settings,
        selected_events=selected,
    )


@app.route("/api/docs")
@login_required
def api_docs():
    settings = Settings.load()
    # You already have a nice html; this just renders it with settings + brand
    return render_template("api_docs.html", settings=settings)


# -------------------------------------------------
# API keys UI
# -------------------------------------------------

@app.route("/api-keys", methods=["GET", "POST"])
@login_required
def api_keys_list():
    settings = Settings.load()
    new_key_plain = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            name = "Unnamed key"

        can_read = bool(request.form.get("perm_read"))
        can_write = bool(request.form.get("perm_write"))
        if not can_read and not can_write:
            can_read = True

        raw_key = secrets.token_hex(32)
        key_id = raw_key[:12]
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

        api_key = ApiKey(
            name=name,
            key_id=key_id,
            key_hash=key_hash,
            can_read=can_read,
            can_write=can_write,
            active=True,
            created_at=datetime.utcnow(),
        )
        db.session.add(api_key)
        db.session.commit()

        new_key_plain = raw_key
        flash("New API key created. Copy it now – it won't be shown again.", "success")

    api_keys = ApiKey.query.order_by(ApiKey.created_at.desc()).all()
    return render_template(
        "api_keys.html",
        settings=settings,
        api_keys=api_keys,
        new_key_plain=new_key_plain,
    )


@app.route("/api-keys/<int:key_id>/revoke", methods=["POST"])
@login_required
def api_key_revoke(key_id):
    Settings.load()  # just ensure exists
    key = ApiKey.query.get_or_404(key_id)
    if key.active:
        key.active = False
        db.session.commit()
        flash("API key revoked.", "success")
    else:
        flash("API key was already revoked.", "info")
    return redirect(url_for("api_keys_list"))


@app.route("/api-keys/<int:key_id>/delete", methods=["POST"])
@login_required
def api_key_delete(key_id):
    Settings.load()
    key = ApiKey.query.get_or_404(key_id)
    db.session.delete(key)
    db.session.commit()
    flash("API key deleted.", "success")
    return redirect(url_for("api_keys_list"))


# -------------------------------------------------
# Customers
# -------------------------------------------------

@app.route("/customers")
@login_required
def list_customers():
    customers = Customer.query.order_by(Customer.name).all()
    return render_template("customers_list.html", customers=customers)


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
def new_customer():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Customer name is required", "danger")
            return render_template("customer_form.html", customer=None)

        use_default_tax = bool(request.form.get("use_default_tax"))
        tax_rate_str = (request.form.get("tax_rate") or "").strip()
        tax_rate = None
        if tax_rate_str:
            try:
                tax_rate = Decimal(tax_rate_str)
            except Exception:
                tax_rate = None

        customer = Customer(
            name=name,
            email=request.form.get("email"),
            phone=request.form.get("phone"),
            address_line1=request.form.get("address_line1"),
            address_line2=request.form.get("address_line2"),
            city=request.form.get("city"),
            postcode=request.form.get("postcode"),
            country=request.form.get("country"),
            use_default_tax=use_default_tax,
            tax_rate=None if use_default_tax else tax_rate,
        )
        db.session.add(customer)
        db.session.commit()
        flash("Customer created", "success")
        return redirect(url_for("list_customers"))

    return render_template("customer_form.html", customer=None)


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)

    if request.method == "POST":
        customer.name = (request.form.get("name") or "").strip()
        customer.email = request.form.get("email")
        customer.phone = request.form.get("phone")
        customer.address_line1 = request.form.get("address_line1")
        customer.address_line2 = request.form.get("address_line2")
        customer.city = request.form.get("city")
        customer.postcode = request.form.get("postcode")
        customer.country = request.form.get("country")

        use_default_tax = bool(request.form.get("use_default_tax"))
        tax_rate_str = (request.form.get("tax_rate") or "").strip()
        tax_rate = None
        if tax_rate_str:
            try:
                tax_rate = Decimal(tax_rate_str)
            except Exception:
                tax_rate = None

        customer.use_default_tax = use_default_tax
        customer.tax_rate = None if use_default_tax else tax_rate

        if not customer.name:
            flash("Customer name is required", "danger")
            return render_template("customer_form.html", customer=customer)

        db.session.commit()
        flash("Customer updated", "success")
        return redirect(url_for("list_customers"))

    return render_template("customer_form.html", customer=customer)


# -------------------------------------------------
# Products
# -------------------------------------------------

@app.route("/products")
@login_required
def list_products():
    products = Product.query.order_by(Product.name).all()
    return render_template("products_list.html", products=products)


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def new_product():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Product name is required", "danger")
            return render_template("product_form.html", product=None)

        price_str = (request.form.get("unit_price") or "").strip()
        try:
            unit_price = Decimal(price_str or "0")
        except Exception:
            unit_price = Decimal("0.00")

        active = bool(request.form.get("active"))

        product = Product(
            name=name,
            description=request.form.get("description"),
            unit_price=unit_price,
            active=active,
        )
        db.session.add(product)
        db.session.commit()
        flash("Product created", "success")
        return redirect(url_for("list_products"))

    return render_template("product_form.html", product=None)


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)

    if request.method == "POST":
        product.name = (request.form.get("name") or "").strip()
        product.description = request.form.get("description")

        price_str = (request.form.get("unit_price") or "").strip()
        try:
            product.unit_price = Decimal(price_str or "0")
        except Exception:
            product.unit_price = Decimal("0.00")

        product.active = bool(request.form.get("active"))

        if not product.name:
            flash("Product name is required", "danger")
            return render_template("product_form.html", product=product)

        db.session.commit()
        flash("Product updated", "success")
        return redirect(url_for("list_products"))

    return render_template("product_form.html", product=product)


@app.route("/products/<int:product_id>/toggle", methods=["POST"])
@login_required
def toggle_product(product_id):
    product = Product.query.get_or_404(product_id)
    product.active = not product.active
    db.session.commit()
    flash("Product status updated", "success")
    return redirect(url_for("list_products"))


# -------------------------------------------------
# Invoices
# -------------------------------------------------

@app.route("/invoices")
@login_required
def list_invoices():
    status_filter = request.args.get("status") or "open"
    query = Invoice.query.join(Customer).order_by(Invoice.created_at.desc())

    if status_filter == "open":
        query = query.filter(Invoice.status.in_(["draft", "sent", "overdue"]))
    elif status_filter == "draft":
        query = query.filter(Invoice.status == "draft")
    elif status_filter == "sent":
        query = query.filter(Invoice.status == "sent")
    elif status_filter == "overdue":
        query = query.filter(Invoice.status == "overdue")
    elif status_filter == "paid":
        query = query.filter(Invoice.status == "paid")

    invoices = query.all()
    return render_template(
        "invoices_list.html",
        invoices=invoices,
        status_filter=status_filter,
    )


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    settings = Settings.load()

    if request.method == "POST":
        return _handle_invoice_form(customers, products, settings)

    return render_template(
        "invoice_form.html",
        invoice=None,
        customers=customers,
        products=products,
        settings=settings,
    )


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    settings = Settings.load()

    if request.method == "POST":
        return _handle_invoice_form(customers, products, settings, invoice=invoice)

    return render_template(
        "invoice_form.html",
        invoice=invoice,
        customers=customers,
        products=products,
        settings=settings,
    )


def _handle_invoice_form(customers, products, settings, invoice: Invoice | None = None):
    is_new = invoice is None

    customer_id = request.form.get("customer_id")
    issue_date_str = request.form.get("issue_date")
    due_date_str = request.form.get("due_date") or ""

    try:
        issue_date = datetime.strptime(issue_date_str, "%Y-%m-%d").date()
    except Exception:
        flash("Invalid issue date", "danger")
        return render_template(
            "invoice_form.html",
            invoice=invoice,
            customers=customers,
            products=products,
            settings=settings,
        )

    customer = Customer.query.get(int(customer_id)) if customer_id else None

    if due_date_str:
        try:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
        except Exception:
            flash("Invalid due date", "danger")
            return render_template(
                "invoice_form.html",
                invoice=invoice,
                customers=customers,
                products=products,
                settings=settings,
            )
    else:
        # Use global payment terms if enabled, otherwise allow null
        if settings.use_global_payment_terms and settings.payment_terms_days:
            due_date = issue_date + timedelta(days=int(settings.payment_terms_days))
        else:
            due_date = None

    status = request.form.get("status") or "draft"
    notes = request.form.get("notes")

    if invoice is None:
        invoice = Invoice(
            customer_id=customer.id if customer else None,
            invoice_number=generate_invoice_number(),
            issue_date=issue_date,
            due_date=due_date,
            status=status,
            notes=notes,
        )
        db.session.add(invoice)
    else:
        invoice.customer_id = customer.id if customer else None
        invoice.issue_date = issue_date
        invoice.due_date = due_date
        invoice.status = status
        invoice.notes = notes
        invoice.items.clear()

    invoice.tax_rate = determine_tax_rate_for_customer(customer)

    descriptions = request.form.getlist("item_description")
    quantities = request.form.getlist("item_quantity")
    unit_prices = request.form.getlist("item_unit_price")
    product_ids = request.form.getlist("item_product_id") or []

    for idx, (desc, qty_str, price_str) in enumerate(
        zip(descriptions, quantities, unit_prices)
    ):
        desc = (desc or "").strip()
        if not desc:
            continue
        try:
            qty = Decimal(qty_str or "0")
            price = Decimal(price_str or "0")
        except Exception:
            continue

        line_total = qty * price

        product_id = None
        if idx < len(product_ids):
            raw_pid = product_ids[idx]
            if raw_pid:
                try:
                    product_id = int(raw_pid)
                except Exception:
                    product_id = None

        item = InvoiceItem(
            invoice=invoice,
            product_id=product_id,
            description=desc,
            quantity=qty,
            unit_price=price,
            line_total=line_total,
        )
        db.session.add(item)

    invoice.recalc_totals()
    db.session.commit()

    # Webhook
    try:
        event_name = "invoice.created" if is_new else "invoice.updated"
        send_outbound_webhook(event_name, invoice.to_dict())
    except Exception:
        pass

    flash("Invoice saved", "success")
    return redirect(url_for("invoice_detail", invoice_id=invoice.id))


@app.route("/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return render_template("invoice_detail.html", invoice=invoice)


@app.route("/invoices/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    """
    Renders a printable HTML invoice; your template can trigger browser print
    or you can pipe it through wkhtmltopdf/WeasyPrint externally if you like.
    """
    invoice = Invoice.query.get_or_404(invoice_id)
    return render_template("invoice_pdf.html", invoice=invoice)


@app.route("/invoices/<int:invoice_id>/payments/new", methods=["POST"])
@login_required
def add_payment(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)

    amount_str = request.form.get("amount")
    payment_date_str = request.form.get("payment_date")
    method = request.form.get("method")
    external_reference = request.form.get("external_reference")

    try:
        amount = Decimal(amount_str)
        payment_date = datetime.strptime(payment_date_str, "%Y-%m-%d").date()
    except Exception:
        flash("Invalid payment data", "danger")
        return redirect(url_for("invoice_detail", invoice_id=invoice.id))

    payment = Payment(
        invoice=invoice,
        amount=amount,
        payment_date=payment_date,
        method=method,
        external_reference=external_reference,
    )
    db.session.add(payment)
    invoice.recalc_totals()
    db.session.commit()

    try:
        send_outbound_webhook(
            "payment.recorded",
            {"invoice": invoice.to_dict(include_items=False), "payment": payment.to_dict()},
        )
    except Exception:
        pass

    flash("Payment added", "success")
    return redirect(url_for("invoice_detail", invoice_id=invoice.id))


# -------------------------------------------------
# Imports
# -------------------------------------------------

@app.route("/import/customers", methods=["GET", "POST"])
@login_required
def import_customers():
    if request.method == "GET":
        return render_template("import_customers.html")

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Please choose a CSV file.", "danger")
        return redirect(url_for("import_customers"))

    try:
        content = file.read().decode("utf-8-sig")
        f = io.StringIO(content)
        reader = csv.DictReader(f)
    except Exception as e:
        flash(f"Could not read CSV file: {e}", "danger")
        return redirect(url_for("import_customers"))

    if not reader.fieldnames:
        flash("CSV file has no header row.", "danger")
        return redirect(url_for("import_customers"))

    required = ["name"]
    missing = [c for c in required if c not in reader.fieldnames]
    if missing:
        flash(f"Missing required columns: {', '.join(missing)}", "danger")
        return redirect(url_for("import_customers"))

    created = 0
    updated = 0

    for row in reader:
        name = (row.get("name") or "").strip()
        if not name:
            continue

        email = (row.get("email") or "").strip() or None

        customer = None
        if email:
            customer = Customer.query.filter_by(email=email).first()

        use_default_tax = True
        tax_rate = None

        if customer:
            customer.name = name
            customer.phone = (row.get("phone") or "").strip() or None
            customer.address_line1 = (row.get("address_line1") or "").strip() or None
            customer.address_line2 = (row.get("address_line2") or "").strip() or None
            customer.city = (row.get("city") or "").strip() or None
            customer.postcode = (row.get("postcode") or "").strip() or None
            customer.country = (row.get("country") or "").strip() or None
            customer.use_default_tax = use_default_tax
            customer.tax_rate = tax_rate
            updated += 1
        else:
            customer = Customer(
                name=name,
                email=email,
                phone=(row.get("phone") or "").strip() or None,
                address_line1=(row.get("address_line1") or "").strip() or None,
                address_line2=(row.get("address_line2") or "").strip() or None,
                city=(row.get("city") or "").strip() or None,
                postcode=(row.get("postcode") or "").strip() or None,
                country=(row.get("country") or "").strip() or None,
                use_default_tax=use_default_tax,
                tax_rate=tax_rate,
            )
            db.session.add(customer)
            created += 1

    db.session.commit()
    flash(f"Imported customers. Created: {created}, Updated: {updated}", "success")
    return redirect(url_for("list_customers"))


@app.route("/import/invoices", methods=["GET", "POST"])
@login_required
def import_invoices():
    if request.method == "GET":
        return render_template("import_invoices.html")

    file = request.files.get("file")
    if not file or file.filename == "":
        flash("Please choose a CSV file.", "danger")
        return redirect(url_for("import_invoices"))

    try:
        content = file.read().decode("utf-8-sig")
        f = io.StringIO(content)
        reader = csv.DictReader(f)
    except Exception as e:
        flash(f"Could not read CSV file: {e}", "danger")
        return redirect(url_for("import_invoices"))

    if not reader.fieldnames:
        flash("CSV file has no header row.", "danger")
        return redirect(url_for("import_invoices"))

    required = [
        "issue_date",
        "due_date",
        "item_description",
        "item_quantity",
        "item_unit_price",
    ]
    missing = [c for c in required if c not in reader.fieldnames]
    if missing:
        flash(f"Missing required columns: {', '.join(missing)}", "danger")
        return redirect(url_for("import_invoices"))

    count = 0
    errors = 0

    for row in reader:
        try:
            customer_email = (row.get("customer_email") or "").strip() or None
            customer_name = (row.get("customer_name") or "").strip() or None

            if not (customer_email or customer_name):
                errors += 1
                continue

            customer = None
            if customer_email:
                customer = Customer.query.filter_by(email=customer_email).first()

            if not customer:
                if not customer_name:
                    errors += 1
                    continue
                customer = Customer(name=customer_name, email=customer_email)
                db.session.add(customer)
                db.session.flush()

            issue_date = datetime.strptime(row["issue_date"], "%Y-%m-%d").date()
            due_date = datetime.strptime(row["due_date"], "%Y-%m-%d").date()

            status = (row.get("status") or "sent").strip() or "sent"
            if status not in ["draft", "sent", "paid", "overdue", "cancelled"]:
                status = "sent"

            notes = (row.get("notes") or "").strip() or None

            invoice_number = (row.get("invoice_number") or "").strip()
            if not invoice_number:
                invoice_number = generate_invoice_number()

            invoice = Invoice(
                customer=customer,
                invoice_number=invoice_number,
                issue_date=issue_date,
                due_date=due_date,
                status=status,
                notes=notes,
            )
            invoice.tax_rate = determine_tax_rate_for_customer(customer)

            db.session.add(invoice)
            db.session.flush()

            desc = (row.get("item_description") or "").strip()
            if not desc:
                errors += 1
                continue

            qty = Decimal(str(row.get("item_quantity", "0")))
            price = Decimal(str(row.get("item_unit_price", "0")))
            line_total = qty * price

            item = InvoiceItem(
                invoice=invoice,
                description=desc,
                quantity=qty,
                unit_price=price,
                line_total=line_total,
            )
            db.session.add(item)

            invoice.recalc_totals()
            count += 1

        except Exception:
            errors += 1
            continue

    db.session.commit()

    flash(
        f"Imported {count} invoices. {errors} rows skipped due to errors.",
        "success",
    )
    return redirect(url_for("list_invoices"))


# -------------------------------------------------
# API endpoints
# -------------------------------------------------

# --- Customers API ---

@app.route("/api/customers", methods=["GET", "POST"])
def api_customers():
    if request.method == "GET":
        require_api_key(write=False)
        customers = Customer.query.order_by(Customer.id).all()
        return jsonify(
            [
                {
                    "id": c.id,
                    "name": c.name,
                    "email": c.email,
                    "phone": c.phone,
                }
                for c in customers
            ]
        )

    # POST – create
    require_api_key(write=True)
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    customer = Customer(
        name=name,
        email=data.get("email"),
        phone=data.get("phone"),
        address_line1=data.get("address_line1"),
        address_line2=data.get("address_line2"),
        city=data.get("city"),
        postcode=data.get("postcode"),
        country=data.get("country"),
    )
    db.session.add(customer)
    db.session.commit()
    return jsonify({"id": customer.id, "name": customer.name}), 201


@app.route("/api/customers/<int:customer_id>", methods=["GET"])
def api_customer_detail(customer_id):
    require_api_key(write=False)
    c = Customer.query.get_or_404(customer_id)
    return jsonify(
        {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "phone": c.phone,
            "address_line1": c.address_line1,
            "address_line2": c.address_line2,
            "city": c.city,
            "postcode": c.postcode,
            "country": c.country,
        }
    )


# --- Products API ---

@app.route("/api/products", methods=["GET", "POST"])
def api_products():
    if request.method == "GET":
        require_api_key(write=False)
        products = Product.query.order_by(Product.name).all()
        return jsonify(
            [
                {
                    "id": p.id,
                    "name": p.name,
                    "description": p.description,
                    "unit_price": float(p.unit_price),
                    "active": bool(p.active),
                }
                for p in products
            ]
        )

    require_api_key(write=True)
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    try:
        unit_price = Decimal(str(data.get("unit_price", "0")))
    except Exception:
        unit_price = Decimal("0.00")

    active = bool(data.get("active", True))

    product = Product(
        name=name,
        description=data.get("description"),
        unit_price=unit_price,
        active=active,
    )
    db.session.add(product)
    db.session.commit()
    return jsonify({"id": product.id, "name": product.name}), 201


@app.route("/api/products/<int:product_id>", methods=["GET"])
def api_product_detail(product_id):
    require_api_key(write=False)
    p = Product.query.get_or_404(product_id)
    return jsonify(
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "unit_price": float(p.unit_price),
            "active": bool(p.active),
        }
    )


# --- Invoices API (basic) ---

@app.route("/api/invoices", methods=["POST"])
def api_create_invoice():
    require_api_key(write=True)
    data = request.get_json(force=True)

    customer_id = data.get("customer_id")
    customer_data = data.get("customer")

    if customer_id:
        customer = Customer.query.get(customer_id)
        if not customer:
            return jsonify({"error": "customer_id not found"}), 400
    elif customer_data:
        name = (customer_data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "customer.name is required"}), 400

        use_default_tax = customer_data.get("use_default_tax", True)
        tax_rate = customer_data.get("tax_rate", None)
        tax_rate_dec = None
        if tax_rate is not None:
            try:
                tax_rate_dec = Decimal(str(tax_rate))
            except Exception:
                tax_rate_dec = None

        customer = Customer(
            name=name,
            email=customer_data.get("email"),
            phone=customer_data.get("phone"),
            address_line1=customer_data.get("address_line1"),
            address_line2=customer_data.get("address_line2"),
            city=customer_data.get("city"),
            postcode=customer_data.get("postcode"),
            country=customer_data.get("country"),
            use_default_tax=use_default_tax,
            tax_rate=None if use_default_tax else tax_rate_dec,
        )
        db.session.add(customer)
        db.session.flush()
    else:
        return jsonify({"error": "customer_id or customer object required"}), 400

    try:
        issue_date = datetime.strptime(data["issue_date"], "%Y-%m-%d").date()
        due_date = (
            datetime.strptime(data["due_date"], "%Y-%m-%d").date()
            if data.get("due_date")
            else None
        )
    except Exception:
        return jsonify({"error": "issue_date and due_date must be YYYY-MM-DD"}), 400

    invoice = Invoice(
        customer=customer,
        invoice_number=data.get("invoice_number") or generate_invoice_number(),
        issue_date=issue_date,
        due_date=due_date,
        status=data.get("status", "sent"),
        notes=data.get("notes"),
    )

    if "tax_rate" in data and data["tax_rate"] is not None:
        try:
            invoice.tax_rate = Decimal(str(data["tax_rate"]))
        except Exception:
            invoice.tax_rate = determine_tax_rate_for_customer(customer)
    else:
        invoice.tax_rate = determine_tax_rate_for_customer(customer)

    db.session.add(invoice)

    items_data = data.get("items", [])
    if not items_data:
        return jsonify({"error": "At least one item is required"}), 400

    for item_data in items_data:
        desc = (item_data.get("description") or "").strip()
        if not desc:
            continue
        qty = Decimal(str(item_data.get("quantity", "0")))
        price = Decimal(str(item_data.get("unit_price", "0")))
        line_total = qty * price

        product_id = item_data.get("product_id")
        pid = None
        if product_id:
            try:
                pid = int(product_id)
            except Exception:
                pid = None

        db.session.add(
            InvoiceItem(
                invoice=invoice,
                product_id=pid,
                description=desc,
                quantity=qty,
                unit_price=price,
                line_total=line_total,
            )
        )

    invoice.recalc_totals()
    db.session.commit()

    try:
        send_outbound_webhook("invoice.created", invoice.to_dict())
    except Exception:
        pass

    return jsonify(invoice.to_dict()), 201


@app.route("/api/invoices/<int:invoice_id>", methods=["GET"])
def api_get_invoice(invoice_id):
    require_api_key(write=False)
    invoice = Invoice.query.get_or_404(invoice_id)
    return jsonify(invoice.to_dict())


@app.route("/api/invoices/<int:invoice_id>", methods=["PUT"])
def api_update_invoice(invoice_id):
    require_api_key(write=True)
    invoice = Invoice.query.get_or_404(invoice_id)
    data = request.get_json(force=True)

    if "customer_id" in data:
        customer_id = data.get("customer_id")
        if customer_id:
            customer = Customer.query.get(customer_id)
            if not customer:
                return jsonify({"error": "customer_id not found"}), 400
            invoice.customer_id = customer_id
            if "tax_rate" not in data:
                invoice.tax_rate = determine_tax_rate_for_customer(customer)

    if "issue_date" in data:
        try:
            invoice.issue_date = datetime.strptime(data["issue_date"], "%Y-%m-%d").date()
        except Exception:
            return jsonify({"error": "invalid issue_date"}), 400

    if "due_date" in data:
        if data["due_date"]:
            try:
                invoice.due_date = datetime.strptime(
                    data["due_date"], "%Y-%m-%d"
                ).date()
            except Exception:
                return jsonify({"error": "invalid due_date"}), 400
        else:
            invoice.due_date = None

    if "status" in data and data["status"] in [
        "draft",
        "sent",
        "paid",
        "overdue",
        "cancelled",
    ]:
        invoice.status = data["status"]

    if "notes" in data:
        invoice.notes = data["notes"]

    if "tax_rate" in data:
        if data["tax_rate"] is None:
            customer = invoice.customer
            invoice.tax_rate = determine_tax_rate_for_customer(customer)
        else:
            try:
                invoice.tax_rate = Decimal(str(data["tax_rate"]))
            except Exception:
                return jsonify({"error": "invalid tax_rate"}), 400

    if "items" in data:
        invoice.items.clear()
        items_data = data.get("items") or []
        for item_data in items_data:
            desc = (item_data.get("description") or "").strip()
            if not desc:
                continue
            qty = Decimal(str(item_data.get("quantity", "0")))
            price = Decimal(str(item_data.get("unit_price", "0")))
            line_total = qty * price

            product_id = item_data.get("product_id")
            pid = None
            if product_id:
                try:
                    pid = int(product_id)
                except Exception:
                    pid = None

            db.session.add(
                InvoiceItem(
                    invoice=invoice,
                    product_id=pid,
                    description=desc,
                    quantity=qty,
                    unit_price=price,
                    line_total=line_total,
                )
            )

    invoice.recalc_totals()
    db.session.commit()

    try:
        send_outbound_webhook("invoice.updated", invoice.to_dict())
    except Exception:
        pass

    return jsonify(invoice.to_dict())


# --- Inbound webhook for payment ---

@app.route("/api/webhooks/payment", methods=["POST"])
def api_webhook_payment():
    require_api_key(write=True)
    data = request.get_json(force=True)

    invoice_number = data.get("invoice_number")
    if not invoice_number:
        return jsonify({"error": "invoice_number is required"}), 400

    invoice = Invoice.query.filter_by(invoice_number=invoice_number).first()
    if not invoice:
        return jsonify({"error": "Invoice not found"}), 404

    try:
        amount = Decimal(str(data["amount"]))
        payment_date = datetime.strptime(data["payment_date"], "%Y-%m-%d").date()
    except Exception:
        return jsonify({"error": "Invalid amount or payment_date"}), 400

    payment = Payment(
        invoice=invoice,
        amount=amount,
        payment_date=payment_date,
        method=data.get("method"),
        external_reference=data.get("external_reference"),
    )
    db.session.add(payment)
    invoice.recalc_totals()
    db.session.commit()

    try:
        send_outbound_webhook(
            "payment.recorded",
            {
                "invoice": invoice.to_dict(include_items=False),
                "payment": payment.to_dict(),
            },
        )
    except Exception:
        pass

    return (
        jsonify(
            {
                "status": "ok",
                "invoice": invoice.to_dict(include_items=False),
                "payment": payment.to_dict(),
            }
        ),
        201,
    )


# -------------------------------------------------
# CLI init
# -------------------------------------------------

@app.cli.command("init-db")
def init_db_command():
    db.create_all()

    # Settings row
    if not Settings.query.get(1):
        db.session.add(Settings(id=1, default_tax_rate=Decimal("0.00")))

    # Default admin user
    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin")
        admin.set_password("admin123")
        db.session.add(admin)
        print("Created default admin user: admin / admin123")

    # Default API key
    if not ApiKey.query.first():
        raw_key = secrets.token_hex(32)
        key_id = raw_key[:12]
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        api_key = ApiKey(
            name="Default key",
            key_id=key_id,
            key_hash=key_hash,
            can_read=True,
            can_write=True,
            active=True,
        )
        db.session.add(api_key)
        print("Created default API key:", raw_key)

    db.session.commit()
    print("DB initialized")


# -------------------------------------------------
# Main
# -------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
