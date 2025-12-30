import os
from datetime import datetime, date
from decimal import Decimal
import csv
import io
from secrets import token_hex

import requests

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort, session
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import generate_password_hash, check_password_hash

# -------------------------------------------------
# Config & setup
# -------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    # Docker compose default
    "mysql+pymysql://invoicemgr:invoicemgr@db/invoicemanager"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# -------------------------------------------------
# Models
# -------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


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
        db.DateTime, nullable=False,
        default=datetime.utcnow, onupdate=datetime.utcnow
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
        db.DateTime, nullable=False,
        default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def __repr__(self):
        return f"<Product {self.id} {self.name}>"


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date)
    status = db.Column(
        db.Enum("draft", "sent", "paid", "overdue", "cancelled",
                name="invoice_status"),
        nullable=False, default="draft"
    )
    notes = db.Column(db.Text)

    subtotal_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    tax_rate = db.Column(db.Numeric(5, 2))
    tax_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    balance_due = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False,
        default=datetime.utcnow, onupdate=datetime.utcnow
    )

    customer = db.relationship("Customer", back_populates="invoices")
    items = db.relationship(
        "InvoiceItem", back_populates="invoice",
        cascade="all, delete-orphan"
    )
    payments = db.relationship(
        "Payment", back_populates="invoice",
        cascade="all, delete-orphan"
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
        elif self.due_date and date.today() > self.due_date and self.status not in ("paid", "cancelled"):
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
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"))
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    line_total = db.Column(db.Numeric(10, 2), nullable=False)

    invoice = db.relationship("Invoice", back_populates="items")
    product = db.relationship("Product")

    def to_dict(self):
        return {
            "id": self.id,
            "invoice_id": self.invoice_id,
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


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    default_tax_rate = db.Column(db.Numeric(5, 2),
                                 nullable=False,
                                 default=Decimal("0.00"))

    outbound_webhook_url = db.Column(db.String(500))
    outbound_webhook_enabled = db.Column(db.Boolean, nullable=False, default=False)
    # comma-separated list of event names
    outbound_webhook_events = db.Column(db.String(255), nullable=False,
                                        default="invoice.created,invoice.updated,payment.recorded")

    brand_name = db.Column(db.String(255))
    logo_url = db.Column(db.String(500))

    payment_terms_days = db.Column(db.Integer, nullable=False, default=0)
    use_global_payment_terms = db.Column(db.Boolean, nullable=False, default=False)


class APIKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    key = db.Column(db.String(64), unique=True, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime)


# -------------------------------------------------
# Helpers
# -------------------------------------------------

def generate_api_key_value():
    return token_hex(32)


def require_api_key():
    key_value = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key_value:
        abort(401, description="Missing API key")
    api_key = APIKey.query.filter_by(key=key_value, active=True).first()
    if not api_key:
        abort(401, description="Invalid or inactive API key")
    api_key.last_used_at = datetime.utcnow()
    db.session.commit()
    return api_key


def generate_invoice_number():
    today_str = date.today().strftime("%Y%m%d")
    last = (
        Invoice.query
        .filter(Invoice.invoice_number.like(f"INV-{today_str}-%"))
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


def get_settings():
    settings = Settings.query.get(1)
    if not settings:
        settings = Settings(
            id=1,
            default_tax_rate=Decimal("0.00"),
            outbound_webhook_enabled=False,
            outbound_webhook_events="invoice.created,invoice.updated,payment.recorded",
            payment_terms_days=0,
            use_global_payment_terms=False,
        )
        db.session.add(settings)
        db.session.commit()

    # ensure new columns have defaults if DB was older
    changed = False
    if settings.outbound_webhook_events is None:
        settings.outbound_webhook_events = "invoice.created,invoice.updated,payment.recorded"
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


def determine_tax_rate_for_customer(customer):
    settings = get_settings()
    if customer is None:
        return settings.default_tax_rate
    if customer.use_default_tax or customer.tax_rate is None:
        return settings.default_tax_rate
    return customer.tax_rate


def apply_global_due_date(issue_date: date, settings: Settings) -> date | None:
    if not settings.use_global_payment_terms or not settings.payment_terms_days:
        return None
    try:
        days = int(settings.payment_terms_days)
    except Exception:
        return None
    return issue_date + datetime.timedelta(days=days)


def send_outbound_webhook(event_type: str, payload: dict):
    """
    Send a JSON webhook to the configured outbound_webhook_url.
    Obeys enabled flag and selected events.
    Never raises back to the caller – errors are just printed.
    """
    settings = get_settings()
    if not settings.outbound_webhook_enabled or not settings.outbound_webhook_url:
        return

    # filter events
    allowed = [
        e.strip() for e in (settings.outbound_webhook_events or "").split(",")
        if e.strip()
    ]
    if allowed and event_type not in allowed:
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
        print(f"[webhook] Sent {event_type} → {settings.outbound_webhook_url} ({resp.status_code})")
    except Exception as e:
        print(f"[webhook] Error sending {event_type} → {settings.outbound_webhook_url}: {e}")


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
        if user and user.check_password(password):
            login_user(user)
            flash("Logged in", "success")
            next_url = request.args.get("next") or url_for("list_invoices")
            return redirect(next_url)
        flash("Invalid username or password", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logged out", "success")
    return redirect(url_for("login"))


# -------------------------------------------------
# Web UI routes
# -------------------------------------------------

@app.route("/")
@login_required
def index():
    return redirect(url_for("list_invoices"))


# Settings ------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    settings = get_settings()

    if request.method == "POST":
        # default tax
        rate_str = (request.form.get("default_tax_rate") or "").strip()
        try:
            settings.default_tax_rate = Decimal(rate_str or "0")
        except Exception:
            flash("Invalid tax rate", "danger")
            return redirect(url_for("settings_view"))

        # payment terms
        settings.use_global_payment_terms = bool(request.form.get("use_global_terms"))
        days_str = (request.form.get("payment_terms_days") or "").strip()
        try:
            settings.payment_terms_days = int(days_str or "0")
        except Exception:
            settings.payment_terms_days = 0

        # outbound webhook config
        settings.outbound_webhook_enabled = bool(
            request.form.get("outbound_webhook_enabled")
        )
        settings.outbound_webhook_url = (request.form.get("outbound_webhook_url") or "").strip() or None

        selected_events = request.form.getlist("webhook_events")
        settings.outbound_webhook_events = ",".join(selected_events)

        db.session.commit()
        flash("Settings saved", "success")
        return redirect(url_for("settings_view"))

    selected_events = (
        (settings.outbound_webhook_events or "").split(",")
        if settings.outbound_webhook_events else []
    )
    selected_events = [e.strip() for e in selected_events if e.strip()]

    return render_template(
        "settings.html",
        settings=settings,
        webhook_events_selected=selected_events,
    )


# API Keys management --------------------------------------

@app.route("/api-keys")
@login_required
def api_keys_list():
    api_keys = APIKey.query.order_by(APIKey.created_at.desc()).all()
    return render_template("api_keys_list.html", api_keys=api_keys)


@app.route("/api-keys/new", methods=["GET", "POST"])
@login_required
def api_keys_new():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        key_value = (request.form.get("key") or "").strip()
        if not name:
            flash("Name is required", "danger")
            return render_template("api_key_form.html", api_key=None)

        if not key_value:
            key_value = generate_api_key_value()

        if APIKey.query.filter_by(key=key_value).first():
            flash("That key value is already in use, please try again.", "danger")
            return render_template("api_key_form.html", api_key=None)

        api_key = APIKey(name=name, key=key_value, active=True)
        db.session.add(api_key)
        db.session.commit()

        session["new_api_key"] = key_value
        session["new_api_key_name"] = name

        flash("API key created", "success")
        return redirect(url_for("api_keys_list"))

    return render_template("api_key_form.html", api_key=None)


@app.route("/api-keys/<int:key_id>/toggle", methods=["POST"])
@login_required
def api_keys_toggle(key_id):
    api_key = APIKey.query.get_or_404(key_id)
    api_key.active = not api_key.active
    db.session.commit()
    flash("API key status updated", "success")
    return redirect(url_for("api_keys_list"))


@app.route("/api-keys/<int:key_id>/delete", methods=["POST"])
@login_required
def api_keys_delete(key_id):
    api_key = APIKey.query.get_or_404(key_id)
    db.session.delete(api_key)
    db.session.commit()
    flash("API key deleted", "success")
    return redirect(url_for("api_keys_list"))


@app.route("/api/docs")
@login_required
def api_docs():
    return render_template("api_docs.html")


# Customers -----------------------------------------------

@app.route("/customers")
@login_required
def list_customers():
    customers = Customer.query.order_by(Customer.name).all()
    return render_template("customers_list.html", customers=customers)


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
def new_customer():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
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
        customer.name = request.form.get("name", "").strip()
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


# Products -----------------------------------------------

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
        desc = (request.form.get("description") or "").strip()
        price_str = (request.form.get("unit_price") or "").strip()

        if not name:
            flash("Product name is required", "danger")
            return render_template("product_form.html", product=None)

        try:
            price = Decimal(price_str or "0")
        except Exception:
            flash("Invalid unit price", "danger")
            return render_template("product_form.html", product=None)

        product = Product(
            name=name,
            description=desc,
            unit_price=price,
            active=True,
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
        name = (request.form.get("name") or "").strip()
        desc = (request.form.get("description") or "").strip()
        price_str = (request.form.get("unit_price") or "").strip()
        active = bool(request.form.get("active"))

        if not name:
            flash("Product name is required", "danger")
            return render_template("product_form.html", product=product)

        try:
            price = Decimal(price_str or "0")
        except Exception:
            flash("Invalid unit price", "danger")
            return render_template("product_form.html", product=product)

        product.name = name
        product.description = desc
        product.unit_price = price
        product.active = active

        db.session.commit()
        flash("Product updated", "success")
        return redirect(url_for("list_products"))

    return render_template("product_form.html", product=product)


# Invoices -----------------------------------------------

@app.route("/invoices")
@login_required
def list_invoices():
    status_filter = request.args.get("status") or "open"
    q = Invoice.query

    if status_filter == "open":
        q = q.filter(Invoice.status.in_(["draft", "sent", "overdue"]))
    elif status_filter in ["draft", "sent", "paid", "overdue", "cancelled"]:
        q = q.filter_by(status=status_filter)

    invoices = q.order_by(Invoice.created_at.desc()).all()
    return render_template("invoices_list.html", invoices=invoices, status_filter=status_filter)


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    settings = get_settings()

    if request.method == "POST":
        return handle_invoice_form(customers, products, settings)

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
    settings = get_settings()

    if request.method == "POST":
        return handle_invoice_form(customers, products, settings, invoice)

    return render_template(
        "invoice_form.html",
        invoice=invoice,
        customers=customers,
        products=products,
        settings=settings,
    )


def handle_invoice_form(customers, products, settings, invoice=None):
    is_new = invoice is None

    customer_id = request.form.get("customer_id")
    issue_date_str = request.form.get("issue_date")
    due_date_str = request.form.get("due_date") or ""

    status = request.form.get("status") or "draft"
    notes = request.form.get("notes")

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
        # optional – you can extend this later to auto-calc based on settings
        due_date = None

    customer = Customer.query.get(int(customer_id)) if customer_id else None
    if not customer:
        flash("Customer is required", "danger")
        return render_template(
            "invoice_form.html",
            invoice=invoice,
            customers=customers,
            products=products,
            settings=settings,
        )

    if not invoice:
        invoice = Invoice(
            customer_id=customer.id,
            invoice_number=generate_invoice_number(),
            issue_date=issue_date,
            due_date=due_date,
            status=status,
            notes=notes,
        )
        db.session.add(invoice)
    else:
        invoice.customer_id = customer.id
        invoice.issue_date = issue_date
        invoice.due_date = due_date
        invoice.status = status
        invoice.notes = notes
        invoice.items.clear()

    invoice.tax_rate = determine_tax_rate_for_customer(customer)

    product_ids = request.form.getlist("item_product_id")
    descriptions = request.form.getlist("item_description")
    quantities = request.form.getlist("item_quantity")
    unit_prices = request.form.getlist("item_unit_price")

    for prod_id_str, desc, qty_str, price_str in zip(
        product_ids, descriptions, quantities, unit_prices
    ):
        desc = (desc or "").strip()
        prod_id = int(prod_id_str) if prod_id_str else None

        if not desc and not prod_id:
            continue

        try:
            qty = Decimal(qty_str or "0")
            price = Decimal(price_str or "0")
        except Exception:
            continue

        if qty == 0 and price == 0:
            continue

        product = Product.query.get(prod_id) if prod_id else None
        if product and not desc:
            desc = product.name

        line_total = qty * price
        item = InvoiceItem(
            invoice=invoice,
            product=product,
            description=desc,
            quantity=qty,
            unit_price=price,
            line_total=line_total,
        )
        db.session.add(item)

    invoice.recalc_totals()
    db.session.commit()

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
    settings = get_settings()
    return render_template("invoice_detail.html", invoice=invoice, settings=settings)


@app.route("/invoices/<int:invoice_id>/print")
@login_required
def invoice_print(invoice_id):
    """
    Clean HTML invoice suitable for emailing or 'Print to PDF'.
    """
    invoice = Invoice.query.get_or_404(invoice_id)
    settings = get_settings()
    return render_template("invoice_print.html", invoice=invoice, settings=settings)


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
            {
                "invoice": invoice.to_dict(include_items=False),
                "payment": payment.to_dict(),
            },
        )
    except Exception:
        pass

    flash("Payment added", "success")
    return redirect(url_for("invoice_detail", invoice_id=invoice.id))


# -------------------------------------------------
# CSV Import (unchanged from before)
# -------------------------------------------------
# (keeping behaviour the same – not repeating here for brevity in this explanation)
# Make sure you keep your existing import_customers and import_invoices
# functions below this comment in your real file.


# -------------------------------------------------
# API endpoints
# -------------------------------------------------
# (for now unchanged; next step we can expand to full GET/POST/PUT
# for customers/products/invoices/payments etc.)

# -------------------------------------------------
# CLI helper
# -------------------------------------------------

@app.cli.command("init-db")
def init_db_command():
    db.create_all()

    if not Settings.query.get(1):
        db.session.add(Settings(id=1, default_tax_rate=Decimal("0.00")))
        print("Created default Settings row")

    if not APIKey.query.first():
        default_key_value = generate_api_key_value()
        api_key = APIKey(name="Default key", key=default_key_value, active=True)
        db.session.add(api_key)
        print("Created default API key:", default_key_value)

    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", is_admin=True)
        admin.set_password("admin123")
        db.session.add(admin)
        print("Created default admin user: admin / admin123")

    db.session.commit()
    print("DB initialized")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
