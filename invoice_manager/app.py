import os
import csv
import io
from datetime import datetime, date
from decimal import Decimal
from functools import wraps
from secrets import token_hex

import requests
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort, session
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash


# --------------------------------------------------------------------
# App & DB config
# --------------------------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Default to Docker MySQL if DATABASE_URL not set
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://invoicemgr:invoicemgr@db/invoicemanager"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)


# --------------------------------------------------------------------
# Models
# --------------------------------------------------------------------

class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def set_password(self, raw: str):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


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
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
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
    due_date = db.Column(db.Date, nullable=False)
    status = db.Column(
        db.Enum("draft", "sent", "paid", "overdue", "cancelled", name="invoice_status"),
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
    items = db.relationship("InvoiceItem", back_populates="invoice",
                            cascade="all, delete-orphan")
    payments = db.relationship("Payment", back_populates="invoice",
                               cascade="all, delete-orphan")

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
        elif date.today() > self.due_date and self.status not in ("paid", "cancelled"):
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
            "due_date": self.due_date.isoformat(),
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
            "product_id": self.product_id,
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
    default_tax_rate = db.Column(db.Numeric(5, 2), nullable=False,
                                 default=Decimal("0.00"))
    outbound_webhook_url = db.Column(db.String(500))
    outbound_webhook_enabled = db.Column(db.Boolean, nullable=False, default=False)


class APIKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    key = db.Column(db.String(64), unique=True, nullable=False)
    can_read = db.Column(db.Boolean, nullable=False, default=True)
    can_write = db.Column(db.Boolean, nullable=False, default=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime)


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


@app.context_processor
def inject_globals():
    return {"current_user": current_user()}


def generate_api_key_value():
    return token_hex(32)


def require_api_key(permission: str = "read") -> APIKey:
    key_value = request.headers.get("X-API-Key") or request.args.get("api_key")
    if not key_value:
        abort(401, description="Missing API key")

    api_key = APIKey.query.filter_by(key=key_value, active=True).first()
    if not api_key:
        abort(401, description="Invalid or inactive API key")

    if permission == "read" and not api_key.can_read:
        abort(403, description="API key does not have read permission")
    if permission == "write" and not api_key.can_write:
        abort(403, description="API key does not have write permission")

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
        settings = Settings(id=1, default_tax_rate=Decimal("0.00"))
        db.session.add(settings)
        db.session.commit()
    return settings


def determine_tax_rate_for_customer(customer: Customer | None):
    settings = get_settings()
    if customer is None:
        return settings.default_tax_rate
    if customer.use_default_tax or customer.tax_rate is None:
        return settings.default_tax_rate
    return customer.tax_rate


def send_outbound_webhook(event_type: str, payload: dict):
    settings = get_settings()
    if not settings.outbound_webhook_enabled or not settings.outbound_webhook_url:
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
        print(f"[webhook] Sent {event_type} → {settings.outbound_webhook_url} "
              f"({resp.status_code})")
    except Exception as e:
        print(f"[webhook] Error sending {event_type} → "
              f"{settings.outbound_webhook_url}: {e}")


# --------------------------------------------------------------------
# Auth routes
# --------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("Invalid username or password", "danger")
            return render_template("login.html")

        session["user_id"] = user.id
        next_url = request.args.get("next") or url_for("list_invoices")
        return redirect(next_url)

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out", "success")
    return redirect(url_for("login"))


# --------------------------------------------------------------------
# Web UI routes
# --------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return redirect(url_for("list_invoices"))


# Settings ------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    settings = get_settings()
    if request.method == "POST":
        rate_str = (request.form.get("default_tax_rate") or "").strip()
        try:
            settings.default_tax_rate = Decimal(rate_str or "0")
        except Exception:
            flash("Invalid tax rate", "danger")
            return redirect(url_for("settings_view"))

        settings.outbound_webhook_enabled = bool(
            request.form.get("outbound_webhook_enabled")
        )
        settings.outbound_webhook_url = (
            request.form.get("outbound_webhook_url") or ""
        ).strip() or None

        db.session.commit()
        flash("Settings saved", "success")
        return redirect(url_for("settings_view"))
    return render_template("settings.html", settings=settings)


# API keys ------------------------------------------------------------

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
        can_read = bool(request.form.get("can_read"))
        can_write = bool(request.form.get("can_write"))

        if not name:
            flash("Name is required", "danger")
            return render_template("api_key_form.html", api_key=None)

        if not (can_read or can_write):
            flash("At least one permission (read or write) must be selected.",
                  "danger")
            return render_template("api_key_form.html", api_key=None)

        if not key_value:
            key_value = generate_api_key_value()

        if APIKey.query.filter_by(key=key_value).first():
            flash("That key value is already in use, please try again.", "danger")
            return render_template("api_key_form.html", api_key=None)

        api_key = APIKey(
            name=name,
            key=key_value,
            can_read=can_read,
            can_write=can_write,
            active=True,
        )
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
    example_key = APIKey.query.first()
    return render_template("api_docs.html", example_key=example_key)


# Customers -----------------------------------------------------------

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


# Products ------------------------------------------------------------

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
        description = (request.form.get("description") or "").strip()
        price_str = (request.form.get("unit_price") or "").strip()
        active = bool(request.form.get("active"))

        if not name:
            flash("Name is required", "danger")
            return render_template("product_form.html", product=None)

        try:
            unit_price = Decimal(price_str or "0")
        except Exception:
            flash("Invalid unit price", "danger")
            return render_template("product_form.html", product=None)

        product = Product(
            name=name,
            description=description or None,
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
        name = (request.form.get("name") or "").strip()
        description = (request.form.get("description") or "").strip()
        price_str = (request.form.get("unit_price") or "").strip()
        active = bool(request.form.get("active"))

        if not name:
            flash("Name is required", "danger")
            return render_template("product_form.html", product=product)

        try:
            unit_price = Decimal(price_str or "0")
        except Exception:
            flash("Invalid unit price", "danger")
            return render_template("product_form.html", product=product)

        product.name = name
        product.description = description or None
        product.unit_price = unit_price
        product.active = active

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


# Invoices ------------------------------------------------------------

@app.route("/invoices")
@login_required
def list_invoices():
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    return render_template("invoices_list.html", invoices=invoices)


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    if request.method == "POST":
        return handle_invoice_form(products)
    return render_template(
        "invoice_form.html",
        invoice=None,
        customers=customers,
        products=products,
        settings=get_settings(),
    )


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.filter_by(active=True).order_by(Product.name).all()

    if request.method == "POST":
        return handle_invoice_form(products, invoice)
    return render_template(
        "invoice_form.html",
        invoice=invoice,
        customers=customers,
        products=products,
        settings=get_settings(),
    )


def handle_invoice_form(products, invoice: Invoice | None = None):
    is_new = invoice is None

    customer_id = request.form.get("customer_id")
    issue_date_str = request.form.get("issue_date")
    due_date_str = request.form.get("due_date")
    status = request.form.get("status") or "draft"
    notes = request.form.get("notes")

    try:
        issue_date = datetime.strptime(issue_date_str, "%Y-%m-%d").date()
        due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
    except Exception:
        flash("Invalid dates", "danger")
        customers = Customer.query.order_by(Customer.name).all()
        return render_template(
            "invoice_form.html",
            invoice=invoice,
            customers=customers,
            products=products,
            settings=get_settings(),
        )

    customer = Customer.query.get(int(customer_id)) if customer_id else None

    if not invoice:
        invoice = Invoice(
            customer_id=customer_id,
            invoice_number=generate_invoice_number(),
            issue_date=issue_date,
            due_date=due_date,
            status=status,
            notes=notes,
        )
        db.session.add(invoice)
    else:
        invoice.customer_id = customer_id
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

    for pid_str, desc, qty_str, price_str in zip(
        product_ids, descriptions, quantities, unit_prices
    ):
        desc = (desc or "").strip()
        if not desc and not pid_str:
            continue

        product_obj = None
        if pid_str:
            try:
                product_obj = Product.query.get(int(pid_str))
            except Exception:
                product_obj = None

        if product_obj and not desc:
            desc = product_obj.name

        try:
            qty = Decimal(qty_str or "0")
            price = Decimal(price_str or "0")
        except Exception:
            continue

        line_total = qty * price
        item = InvoiceItem(
            invoice=invoice,
            product=product_obj,
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
    return render_template("invoice_detail.html", invoice=invoice)


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


# --------------------------------------------------------------------
# CSV Import routes (unchanged from earlier behaviour)
# --------------------------------------------------------------------

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
        "issue_date", "due_date",
        "item_description", "item_quantity", "item_unit_price"
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
                customer = Customer(
                    name=customer_name,
                    email=customer_email,
                )
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
    flash(f"Imported {count} invoices. {errors} rows skipped due to errors.",
          "success")
    return redirect(url_for("list_invoices"))


# --------------------------------------------------------------------
# Public API endpoints (require API key)
# --------------------------------------------------------------------

@app.route("/api/invoices", methods=["POST"])
def api_create_invoice():
    require_api_key(permission="write")
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
        due_date = datetime.strptime(data["due_date"], "%Y-%m-%d").date()
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
        db.session.add(InvoiceItem(
            invoice=invoice,
            description=desc,
            quantity=qty,
            unit_price=price,
            line_total=line_total,
        ))

    invoice.recalc_totals()
    db.session.commit()

    return jsonify(invoice.to_dict()), 201


@app.route("/api/invoices/<int:invoice_id>", methods=["GET"])
def api_get_invoice(invoice_id):
    require_api_key(permission="read")
    invoice = Invoice.query.get_or_404(invoice_id)
    return jsonify(invoice.to_dict())


@app.route("/api/invoices/<int:invoice_id>", methods=["PUT"])
def api_update_invoice(invoice_id):
    require_api_key(permission="write")
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
            invoice.issue_date = datetime.strptime(
                data["issue_date"], "%Y-%m-%d"
            ).date()
        except Exception:
            return jsonify({"error": "invalid issue_date"}), 400

    if "due_date" in data:
        try:
            invoice.due_date = datetime.strptime(
                data["due_date"], "%Y-%m-%d"
            ).date()
        except Exception:
            return jsonify({"error": "invalid due_date"}), 400

    if "status" in data and data["status"] in ["draft", "sent", "paid", "overdue", "cancelled"]:
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
            db.session.add(InvoiceItem(
                invoice=invoice,
                description=desc,
                quantity=qty,
                unit_price=price,
                line_total=line_total,
            ))

    invoice.recalc_totals()
    db.session.commit()
    return jsonify(invoice.to_dict())


@app.route("/api/webhooks/payment", methods=["POST"])
def api_webhook_payment():
    require_api_key(permission="write")
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

    return jsonify({
        "status": "ok",
        "invoice": invoice.to_dict(include_items=False),
        "payment": payment.to_dict()
    }), 201


# --------------------------------------------------------------------
# CLI helper
# --------------------------------------------------------------------

@app.cli.command("init-db")
def init_db_command():
    db.create_all()

    if not Settings.query.get(1):
        db.session.add(Settings(id=1, default_tax_rate=Decimal("0.00")))

    if not APIKey.query.first():
        default_key_value = generate_api_key_value()
        api_key = APIKey(
            name="Default key",
            key=default_key_value,
            can_read=True,
            can_write=True,
            active=True,
        )
        db.session.add(api_key)
        print("Created default API key:", default_key_value)

    if not User.query.filter_by(username="admin").first():
        admin = User(username="admin", is_admin=True)
        admin.set_password("admin123")  # NOTE: does NOT override existing admin
        db.session.add(admin)
        print("Created default admin user: admin / admin123")

    db.session.commit()
    print("DB initialized")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
