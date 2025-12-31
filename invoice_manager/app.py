import os
import json
import secrets
import hashlib
from datetime import datetime, date
from decimal import Decimal

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    jsonify,
    send_file,
    make_response,
)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

# ------------------------------------------------------------------------------
# App / DB setup
# ------------------------------------------------------------------------------

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-key")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://invoicemgr:invoicemgr@db:3306/invoicemanager",
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------

class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(120), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="admin")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    phone = db.Column(db.String(100))
    address_line1 = db.Column(db.String(255))
    address_line2 = db.Column(db.String(255))
    city = db.Column(db.String(100))
    postcode = db.Column(db.String(50))
    country = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    invoices = db.relationship("Invoice", back_populates="customer")


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    items = db.relationship("InvoiceItem", back_populates="product")


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(50), nullable=False, unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text)

    subtotal = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    tax_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    tax_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    balance_due = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    customer = db.relationship("Customer", back_populates="invoices")
    items = db.relationship("InvoiceItem", back_populates="invoice", cascade="all,delete-orphan")
    payments = db.relationship("Payment", back_populates="invoice", cascade="all,delete-orphan")


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    description = db.Column(db.String(255), nullable=True)
    qty = db.Column(db.Numeric(10, 2), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    invoice = db.relationship("Invoice", back_populates="items")
    product = db.relationship("Product", back_populates="items")


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    payment_date = db.Column(db.Date, nullable=True)
    method = db.Column(db.String(100))
    reference = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    invoice = db.relationship("Invoice", back_populates="payments")


class APIKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    key_id = db.Column(db.String(64), nullable=False, unique=True)
    key_hash = db.Column(db.String(128), nullable=False)
    can_read = db.Column(db.Boolean, nullable=False, default=True)
    can_write = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    default_tax_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    outbound_webhook_url = db.Column(db.String(500), nullable=True)
    outbound_webhook_enabled = db.Column(db.Boolean, nullable=False, default=False)
    outbound_webhook_events = db.Column(db.Text, nullable=False, default="")

    brand_name = db.Column(db.String(255), nullable=True)
    logo_url = db.Column(db.String(500), nullable=True)

    payment_terms_days = db.Column(db.Integer, nullable=False, default=0)
    use_global_payment_terms = db.Column(db.Boolean, nullable=False, default=False)


# ------------------------------------------------------------------------------
# Login manager
# ------------------------------------------------------------------------------

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def get_settings() -> Settings:
    settings = Settings.query.get(1)
    if not settings:
        settings = Settings(
            id=1,
            default_tax_rate=Decimal("0.00"),
            outbound_webhook_enabled=False,
            outbound_webhook_events="",
        )
        db.session.add(settings)
        db.session.commit()
    return settings


@app.context_processor
def inject_globals():
    """Make settings + datetime available in all templates."""
    return {"settings": get_settings(), "datetime": datetime}


def generate_invoice_number() -> str:
    today = date.today().strftime("%Y%m%d")
    count = Invoice.query.filter(
        db.func.date_format(Invoice.created_at, "%Y%m%d") == today
    ).count()
    return f"INV-{today}-{count+1:04d}"


def calculate_invoice_totals(invoice: Invoice) -> None:
    subtotal = Decimal("0.00")
    for item in invoice.items:
        item.line_total = (item.qty or 0) * (item.unit_price or 0)
        subtotal += item.line_total

    settings = get_settings()
    tax_rate = invoice.tax_rate if invoice.tax_rate is not None else settings.default_tax_rate
    tax_amount = (subtotal * (tax_rate or 0) / Decimal("100")).quantize(Decimal("0.01"))
    total = subtotal + tax_amount

    paid = sum(p.amount for p in invoice.payments or [])
    balance = total - paid

    invoice.subtotal = subtotal
    invoice.tax_rate = tax_rate
    invoice.tax_amount = tax_amount
    invoice.total = total
    invoice.balance_due = balance


def hash_api_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_api_key_pair() -> tuple[str, str]:
    """Return (key_id, raw_key). key_hash is stored separately."""
    key_id = secrets.token_hex(6)  # short id shown in UI
    raw_key = secrets.token_hex(32)
    return key_id, raw_key


def require_api_key(can_read: bool = False, can_write: bool = False):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header_key = request.headers.get("X-API-Key")
            if not header_key:
                return jsonify({"error": "Missing X-API-Key header"}), 401

            hashed = hash_api_key(header_key)
            api_key = APIKey.query.filter_by(key_hash=hashed, active=True).first()
            if not api_key:
                return jsonify({"error": "Invalid API key"}), 401

            if can_read and not api_key.can_read:
                return jsonify({"error": "API key does not have read permission"}), 403
            if can_write and not api_key.can_write:
                return jsonify({"error": "API key does not have write permission"}), 403

            api_key.last_used_at = datetime.utcnow()
            db.session.commit()

            return fn(*args, **kwargs)

        return wrapper

    return decorator


def invoice_to_dict(inv: Invoice) -> dict:
    return {
        "id": inv.id,
        "number": inv.number,
        "status": inv.status,
        "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "subtotal": float(inv.subtotal or 0),
        "tax_rate": float(inv.tax_rate or 0),
        "tax_amount": float(inv.tax_amount or 0),
        "total": float(inv.total or 0),
        "balance_due": float(inv.balance_due or 0),
        "customer": {
            "id": inv.customer.id if inv.customer else None,
            "name": inv.customer.name if inv.customer else None,
            "email": inv.customer.email if inv.customer else None,
        }
        if inv.customer
        else None,
    }


# ------------------------------------------------------------------------------
# CLI: init-db
# ------------------------------------------------------------------------------

@app.cli.command("init-db")
def init_db_command():
    """Initialize database with default admin and API key."""
    db.create_all()

    # Ensure settings row
    get_settings()

    # Default admin
    admin = User.query.filter_by(username="admin").first()
    if not admin:
        admin = User(username="admin", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)
        print("Created default admin user: admin / admin123")

    # Default API key
    existing_key = APIKey.query.first()
    if not existing_key:
        key_id, raw_key = generate_api_key_pair()
        api_key = APIKey(
            name="Default key",
            key_id=key_id,
            key_hash=hash_api_key(raw_key),
            can_read=True,
            can_write=True,
            active=True,
        )
        db.session.add(api_key)
        print("Created default API key:", raw_key)

    db.session.commit()
    print("DB initialized")


# ------------------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("list_invoices"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("Invalid username or password.", "danger")
            return redirect(url_for("login"))

        login_user(user)
        return redirect(request.args.get("next") or url_for("list_invoices"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ------------------------------------------------------------------------------
# Settings
# ------------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    settings = get_settings()

    if request.method == "POST":
        # Branding
        settings.brand_name = (request.form.get("brand_name") or "").strip() or None
        settings.logo_url = (request.form.get("logo_url") or "").strip() or None

        # Tax/payment
        try:
            tax_raw = request.form.get("default_tax_rate", "").strip()
            settings.default_tax_rate = Decimal(tax_raw or "0")
        except Exception:
            settings.default_tax_rate = Decimal("0")

        settings.use_global_payment_terms = bool(
            request.form.get("use_global_payment_terms")
        )

        try:
            days_raw = request.form.get("payment_terms_days", "").strip()
            settings.payment_terms_days = int(days_raw or 0)
        except Exception:
            settings.payment_terms_days = 0

        # Webhook basics
        settings.outbound_webhook_url = (
            request.form.get("outbound_webhook_url") or ""
        ).strip() or None
        settings.outbound_webhook_enabled = bool(
            request.form.get("outbound_webhook_enabled")
        )

        # Webhook events – from checkboxes
        events_list = request.form.getlist("outbound_webhook_events")
        clean_events = sorted(set(e.strip() for e in events_list if e.strip()))
        settings.outbound_webhook_events = ",".join(clean_events)

        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_view"))

    return render_template("settings.html", settings=settings)


# ------------------------------------------------------------------------------
# Basic pages
# ------------------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return redirect(url_for("list_invoices"))


# ------------------------------------------------------------------------------
# Customers
# ------------------------------------------------------------------------------

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
            flash("Name is required.", "danger")
            return redirect(url_for("new_customer"))

        customer = Customer(
            name=name,
            email=request.form.get("email") or None,
            phone=request.form.get("phone") or None,
            address_line1=request.form.get("address_line1") or None,
            address_line2=request.form.get("address_line2") or None,
            city=request.form.get("city") or None,
            postcode=request.form.get("postcode") or None,
            country=request.form.get("country") or None,
        )
        db.session.add(customer)
        db.session.commit()
        flash("Customer created.", "success")
        return redirect(url_for("list_customers"))

    return render_template("customer_form.html")


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
@login_required
def edit_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    if request.method == "POST":
        customer.name = request.form.get("name", "").strip()
        customer.email = request.form.get("email") or None
        customer.phone = request.form.get("phone") or None
        customer.address_line1 = request.form.get("address_line1") or None
        customer.address_line2 = request.form.get("address_line2") or None
        customer.city = request.form.get("city") or None
        customer.postcode = request.form.get("postcode") or None
        customer.country = request.form.get("country") or None
        db.session.commit()
        flash("Customer updated.", "success")
        return redirect(url_for("list_customers"))
    return render_template("customer_form.html", customer=customer)


# ------------------------------------------------------------------------------
# Products
# ------------------------------------------------------------------------------

@app.route("/products")
@login_required
def list_products():
    products = Product.query.order_by(Product.name).all()
    return render_template("products_list.html", products=products)


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def new_product():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.", "danger")
            return redirect(url_for("new_product"))
        try:
            unit_price = Decimal(request.form.get("unit_price") or "0")
        except Exception:
            unit_price = Decimal("0")

        product = Product(
            name=name,
            description=request.form.get("description") or None,
            unit_price=unit_price,
            active=bool(request.form.get("active")),
        )
        db.session.add(product)
        db.session.commit()
        flash("Product created.", "success")
        return redirect(url_for("list_products"))

    return render_template("product_form.html")


@app.route("/products/<int:product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)

    if request.method == "POST":
        product.name = request.form.get("name", "").strip()
        product.description = request.form.get("description") or None
        try:
            product.unit_price = Decimal(request.form.get("unit_price") or "0")
        except Exception:
            product.unit_price = Decimal("0")
        product.active = bool(request.form.get("active"))
        db.session.commit()
        flash("Product updated.", "success")
        return redirect(url_for("list_products"))

    return render_template("product_form.html", product=product)


# ------------------------------------------------------------------------------
# Invoices
# ------------------------------------------------------------------------------

@app.route("/invoices")
@login_required
def list_invoices():
    status_filter = request.args.get("status", "").strip().lower()

    query = Invoice.query
    if status_filter:
        query = query.filter(Invoice.status == status_filter)

    invoices = query.order_by(Invoice.created_at.desc()).all()
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
    settings = get_settings()

    if request.method == "POST":
        customer_id = int(request.form.get("customer_id"))
        issue_date_str = request.form.get("issue_date")
        due_date_str = request.form.get("due_date") or None

        issue_date = (
            datetime.strptime(issue_date_str, "%Y-%m-%d").date()
            if issue_date_str
            else date.today()
        )
        if due_date_str:
            due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
        elif settings.use_global_payment_terms and settings.payment_terms_days:
            due_date = issue_date + timedelta(days=settings.payment_terms_days)
        else:
            due_date = None

        invoice = Invoice(
            number=generate_invoice_number(),
            customer_id=customer_id,
            status=request.form.get("status") or "draft",
            issue_date=issue_date,
            due_date=due_date,
            notes=request.form.get("notes") or None,
            tax_rate=settings.default_tax_rate,
        )

        # line items
        line_product_ids = request.form.getlist("line_product_id")
        line_descriptions = request.form.getlist("line_description")
        line_qtys = request.form.getlist("line_qty")
        line_unit_prices = request.form.getlist("line_unit_price")

        for idx in range(len(line_descriptions)):
            desc = (line_descriptions[idx] or "").strip()
            if not desc and not line_product_ids[idx]:
                continue

            product_id = int(line_product_ids[idx]) if line_product_ids[idx] else None
            try:
                qty = Decimal(line_qtys[idx] or "1")
            except Exception:
                qty = Decimal("1")
            try:
                unit_price = Decimal(line_unit_prices[idx] or "0")
            except Exception:
                unit_price = Decimal("0")

            item = InvoiceItem(
                product_id=product_id,
                description=desc or None,
                qty=qty,
                unit_price=unit_price,
            )
            invoice.items.append(item)

        db.session.add(invoice)
        db.session.flush()  # get invoice.id for payments

        # optional payment on create
        payment_amount_raw = (request.form.get("payment_amount") or "").strip()
        if payment_amount_raw:
            try:
                amount = Decimal(payment_amount_raw)
                payment_date_str = request.form.get("payment_date")
                payment_date = (
                    datetime.strptime(payment_date_str, "%Y-%m-%d").date()
                    if payment_date_str
                    else date.today()
                )
                payment = Payment(
                    invoice_id=invoice.id,
                    amount=amount,
                    payment_date=payment_date,
                    method=request.form.get("payment_method") or None,
                    reference=request.form.get("payment_reference") or None,
                )
                invoice.payments.append(payment)
            except Exception:
                pass

        calculate_invoice_totals(invoice)
        db.session.commit()
        flash("Invoice created.", "success")
        return redirect(url_for("invoice_detail", invoice_id=invoice.id))

    return render_template(
        "invoice_form.html",
        invoice=None,
        customers=customers,
        products=products,
    )


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    customers = Customer.query.order_by(Customer.name).all()
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    settings = get_settings()

    if request.method == "POST":
        invoice.customer_id = int(request.form.get("customer_id"))
        invoice.status = request.form.get("status") or invoice.status
        invoice.notes = request.form.get("notes") or None

        issue_date_str = request.form.get("issue_date")
        due_date_str = request.form.get("due_date") or None

        invoice.issue_date = (
            datetime.strptime(issue_date_str, "%Y-%m-%d").date()
            if issue_date_str
            else invoice.issue_date
        )
        if due_date_str:
            invoice.due_date = datetime.strptime(due_date_str, "%Y-%m-%d").date()
        elif settings.use_global_payment_terms and settings.payment_terms_days:
            invoice.due_date = invoice.issue_date + timedelta(
                days=settings.payment_terms_days
            )
        else:
            invoice.due_date = None

        # replace items
        invoice.items.clear()

        line_product_ids = request.form.getlist("line_product_id")
        line_descriptions = request.form.getlist("line_description")
        line_qtys = request.form.getlist("line_qty")
        line_unit_prices = request.form.getlist("line_unit_price")

        for idx in range(len(line_descriptions)):
            desc = (line_descriptions[idx] or "").strip()
            if not desc and not line_product_ids[idx]:
                continue

            product_id = int(line_product_ids[idx]) if line_product_ids[idx] else None
            try:
                qty = Decimal(line_qtys[idx] or "1")
            except Exception:
                qty = Decimal("1")
            try:
                unit_price = Decimal(line_unit_prices[idx] or "0")
            except Exception:
                unit_price = Decimal("0")

            item = InvoiceItem(
                product_id=product_id,
                description=desc or None,
                qty=qty,
                unit_price=unit_price,
            )
            invoice.items.append(item)

        # optional new payment
        payment_amount_raw = (request.form.get("payment_amount") or "").strip()
        if payment_amount_raw:
            try:
                amount = Decimal(payment_amount_raw)
                payment_date_str = request.form.get("payment_date")
                payment_date = (
                    datetime.strptime(payment_date_str, "%Y-%m-%d").date()
                    if payment_date_str
                    else date.today()
                )
                payment = Payment(
                    invoice_id=invoice.id,
                    amount=amount,
                    payment_date=payment_date,
                    method=request.form.get("payment_method") or None,
                    reference=request.form.get("payment_reference") or None,
                )
                invoice.payments.append(payment)
            except Exception:
                pass

        calculate_invoice_totals(invoice)
        db.session.commit()
        flash("Invoice updated.", "success")
        return redirect(url_for("invoice_detail", invoice_id=invoice.id))

    return render_template(
        "invoice_form.html",
        invoice=invoice,
        customers=customers,
        products=products,
    )


@app.route("/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return render_template("invoice_detail.html", invoice=invoice)


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
@login_required
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    db.session.delete(invoice)
    db.session.commit()
    flash("Invoice deleted.", "success")
    return redirect(url_for("list_invoices"))


@app.route("/invoices/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    html = render_template("invoice_print.html", invoice=invoice)
    response = make_response(html)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


# ------------------------------------------------------------------------------
# API Keys UI
# ------------------------------------------------------------------------------

@app.route("/api-keys")
@login_required
def api_keys_list():
    keys = APIKey.query.order_by(APIKey.created_at).all()
    return render_template("api_keys.html", api_keys=keys)


@app.route("/api-keys/create", methods=["POST"])
@login_required
def api_keys_create():
    name = (request.form.get("name") or "").strip() or "New key"
    can_read = bool(request.form.get("can_read"))
    can_write = bool(request.form.get("can_write"))

    key_id, raw_key = generate_api_key_pair()
    api_key = APIKey(
        name=name,
        key_id=key_id,
        key_hash=hash_api_key(raw_key),
        can_read=can_read,
        can_write=can_write,
        active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    flash(f"API key created. Make sure you copy it now: {raw_key}", "success")
    return redirect(url_for("api_keys_list"))


@app.route("/api-keys/<int:key_id>/revoke", methods=["POST"])
@login_required
def api_keys_revoke(key_id):
    key = APIKey.query.get_or_404(key_id)
    key.active = False
    db.session.commit()
    flash("API key revoked.", "success")
    return redirect(url_for("api_keys_list"))


@app.route("/api-keys/<int:key_id>/delete", methods=["POST"])
@login_required
def api_keys_delete(key_id):
    key = APIKey.query.get_or_404(key_id)
    db.session.delete(key)
    db.session.commit()
    flash("API key deleted.", "success")
    return redirect(url_for("api_keys_list"))


@app.route("/api-docs")
@login_required
def api_docs():
    return render_template("api_docs.html")


# ------------------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------------------

# Existing POST /api/invoices should already be present in your old file.
# Here is a safe version that matches what we've been using.

@app.route("/api/invoices", methods=["POST"])
@require_api_key(can_write=True)
def api_create_invoice():
    data = request.get_json(force=True, silent=True) or {}
    customer_id = data.get("customer_id")
    if not customer_id:
        return jsonify({"error": "customer_id is required"}), 400

    customer = Customer.query.get(customer_id)
    if not customer:
        return jsonify({"error": "Customer not found"}), 404

    settings = get_settings()
    issue_date = (
        datetime.fromisoformat(data.get("issue_date")).date()
        if data.get("issue_date")
        else date.today()
    )

    if data.get("due_date"):
        due_date = datetime.fromisoformat(data["due_date"]).date()
    elif settings.use_global_payment_terms and settings.payment_terms_days:
        from datetime import timedelta

        due_date = issue_date + timedelta(days=settings.payment_terms_days)
    else:
        due_date = None

    invoice = Invoice(
        number=generate_invoice_number(),
        customer_id=customer.id,
        status=data.get("status") or "draft",
        issue_date=issue_date,
        due_date=due_date,
        notes=data.get("notes"),
        tax_rate=settings.default_tax_rate,
    )

    for item_data in data.get("items", []):
        desc = (item_data.get("description") or "").strip()
        product_id = item_data.get("product_id")
        if not desc and not product_id:
            continue

        try:
            qty = Decimal(str(item_data.get("qty", "1")))
        except Exception:
            qty = Decimal("1")
        try:
            unit_price = Decimal(str(item_data.get("unit_price", "0")))
        except Exception:
            unit_price = Decimal("0")

        item = InvoiceItem(
            product_id=product_id,
            description=desc or None,
            qty=qty,
            unit_price=unit_price,
        )
        invoice.items.append(item)

    db.session.add(invoice)
    db.session.flush()

    # Optional payment
    payment_data = data.get("payment")
    if payment_data and payment_data.get("amount"):
        try:
            amount = Decimal(str(payment_data["amount"]))
            pay_date = (
                datetime.fromisoformat(payment_data["payment_date"]).date()
                if payment_data.get("payment_date")
                else date.today()
            )
            payment = Payment(
                invoice_id=invoice.id,
                amount=amount,
                payment_date=pay_date,
                method=payment_data.get("method"),
                reference=payment_data.get("reference"),
            )
            invoice.payments.append(payment)
        except Exception:
            pass

    calculate_invoice_totals(invoice)
    db.session.commit()

    return jsonify(invoice_to_dict(invoice)), 201


# NEW: GET /api/invoices (this was giving you 405 before)
@app.route("/api/invoices", methods=["GET"])
@require_api_key(can_read=True)
def api_list_invoices():
    invoices = Invoice.query.order_by(Invoice.id.desc()).all()
    return jsonify([invoice_to_dict(inv) for inv in invoices])


# You can later add /api/customers, /api/products, etc. in a similar style.


# ------------------------------------------------------------------------------
# Imports (minimal stubs so menu links work)
# ------------------------------------------------------------------------------

@app.route("/import/customers", methods=["GET", "POST"])
@login_required
def import_customers():
    # Keep minimal so the page loads without errors.
    return render_template("import_customers.html")


@app.route("/import/invoices", methods=["GET", "POST"])
@login_required
def import_invoices():
    return render_template("import_invoices.html")


# ------------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
