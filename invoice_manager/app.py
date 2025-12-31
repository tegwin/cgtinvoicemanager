import csv
import io
import os
from datetime import datetime, timedelta
from decimal import Decimal

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_required,
    login_user,
    logout_user,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
import secrets
import hashlib
import hmac
import requests
import json

# ------------------------------------------------------------------------------
# App & DB setup
# ------------------------------------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev_secret_key")

db_user = os.environ.get("MYSQL_USER", "invoicemgr")
db_password = os.environ.get("MYSQL_PASSWORD", "invoicemgr")
db_host = os.environ.get("MYSQL_HOST", "db")
db_name = os.environ.get("MYSQL_DATABASE", "invoicemanager")
app.config[
    "SQLALCHEMY_DATABASE_URI"
] = f"mysql+pymysql://{db_user}:{db_password}@{db_host}/{db_name}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# ------------------------------------------------------------------------------
# Models
# ------------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    # Added in DB: email column
    email = db.Column(db.String(255), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    # Added in DB: role column
    role = db.Column(db.String(20), nullable=False, default="admin")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def get_id(self):
        return str(self.id)


class Customer(db.Model):
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255))
    phone = db.Column(db.String(100))
    tax_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)  # <-- add this
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


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    default_tax_rate = db.Column(db.Numeric(5, 2), nullable=True)
    outbound_webhook_url = db.Column(db.String(512), nullable=True)
    outbound_webhook_enabled = db.Column(db.Boolean, nullable=False, default=False)
    # Events is stored as a simple comma separated string, e.g. "invoice_created,invoice_paid"
    outbound_webhook_events = db.Column(db.Text, nullable=False, default="")
    brand_name = db.Column(db.String(255), nullable=True)
    logo_url = db.Column(db.String(512), nullable=True)

    # Payment terms settings
    payment_terms_days = db.Column(db.Integer, nullable=True)
    use_global_payment_terms = db.Column(db.Boolean, nullable=False, default=False)

    # Home company info + currency
    company_name = db.Column(db.String(255))
    company_address_line1 = db.Column(db.String(255))
    company_address_line2 = db.Column(db.String(255))
    company_city = db.Column(db.String(255))
    company_postcode = db.Column(db.String(50))
    company_country = db.Column(db.String(100))
    company_phone = db.Column(db.String(50))
    company_email = db.Column(db.String(255))
    company_tax_id = db.Column(db.String(100))
    currency_symbol = db.Column(db.String(10), default="£")


class APIKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    key_id = db.Column(db.String(32), nullable=False, unique=True)
    key_hash = db.Column(db.String(64), nullable=False)
    can_read = db.Column(db.Boolean, nullable=False, default=True)
    can_write = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)


# *** FIXED MODEL HERE ***
class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    # Map Python attribute `number` to the existing DB column `invoice_number`
    number = db.Column("invoice_number", db.String(50), nullable=False, unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="draft")
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=True)
    notes = db.Column(db.Text)

    # Map to existing *_amount columns
    subtotal = db.Column("subtotal_amount", db.Numeric(10, 2), nullable=False, default=0)
    tax_rate = db.Column(db.Numeric(5, 2), nullable=False, default=0)
    tax_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total = db.Column("total_amount", db.Numeric(10, 2), nullable=False, default=0)
    balance_due = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    customer = db.relationship("Customer", back_populates="invoices")
    items = db.relationship(
        "InvoiceItem", back_populates="invoice", cascade="all,delete-orphan"
    )
    payments = db.relationship(
        "Payment", back_populates="invoice", cascade="all,delete-orphan"
    )


class InvoiceItem(db.Model):
    __tablename__ = "invoice_items"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=True)
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), nullable=False, default=1)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    line_total = db.Column(db.Numeric(10, 2), nullable=False, default=0)

    invoice = db.relationship("Invoice", back_populates="items")
    product = db.relationship("Product")


class Payment(db.Model):
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    payment_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    method = db.Column(db.String(50))
    notes = db.Column(db.Text)

    invoice = db.relationship("Invoice", back_populates="payments")


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def get_settings() -> Settings:
    settings = Settings.query.get(1)
    if not settings:
        settings = Settings(id=1, default_tax_rate=Decimal("20.00"))
        db.session.add(settings)
        db.session.commit()
    return settings


def create_default_user_and_key():
    if not User.query.first():
        admin = User(username="admin", role="admin")
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()
        print("Created default admin user: admin / admin123")

    if not APIKey.query.first():
        raw_key = secrets.token_hex(32)
        key_id = raw_key[:12]
        key_hash = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        api_key = APIKey(
            name="Default key", key_id=key_id, key_hash=key_hash, can_read=True, can_write=True
        )
        db.session.add(api_key)
        db.session.commit()
        print(f"Created default API key: {raw_key}")


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


@app.context_processor
def inject_settings():
    return {"settings": get_settings(), "datetime": datetime}


def require_role(*roles):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role not in roles:
                abort(403)
            return fn(*args, **kwargs)

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorator


# ------------------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            # Session timeout 10 minutes
            session_lifetime = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "10"))
            app.permanent_session_lifetime = timedelta(minutes=session_lifetime)
            return redirect(request.args.get("next") or url_for("list_invoices"))

        flash("Invalid username or password", "danger")
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ------------------------------------------------------------------------------
# Customers
# ------------------------------------------------------------------------------

@app.route("/customers")
@login_required
def list_customers():
    customers = Customer.query.order_by(Customer.name.asc()).all()
    return render_template("customers_list.html", customers=customers)


@app.route("/customers/new", methods=["GET", "POST"])
@login_required
def new_customer():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Customer name is required", "danger")
            return redirect(url_for("new_customer"))

        customer = Customer(
            name=name,
            email=(request.form.get("email") or "").strip(),
            address_line1=(request.form.get("address_line1") or "").strip(),
            address_line2=(request.form.get("address_line2") or "").strip(),
            city=(request.form.get("city") or "").strip(),
            postcode=(request.form.get("postcode") or "").strip(),
            country=(request.form.get("country") or "").strip(),
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
        customer.email = (request.form.get("email") or "").strip()
        customer.address_line1 = (request.form.get("address_line1") or "").strip()
        customer.address_line2 = (request.form.get("address_line2") or "").strip()
        customer.city = (request.form.get("city") or "").strip()
        customer.postcode = (request.form.get("postcode") or "").strip()
        customer.country = (request.form.get("country") or "").strip()
        db.session.commit()
        flash("Customer updated", "success")
        return redirect(url_for("list_customers"))

    return render_template("customer_form.html", customer=customer)


@app.route("/customers/<int:customer_id>/delete", methods=["POST"])
@login_required
@require_role("admin", "accountant")
def delete_customer(customer_id):
    customer = Customer.query.get_or_404(customer_id)
    db.session.delete(customer)
    db.session.commit()
    flash("Customer deleted", "success")
    return redirect(url_for("list_customers"))


# ------------------------------------------------------------------------------
# Products
# ------------------------------------------------------------------------------

@app.route("/products")
@login_required
def list_products():
    products = Product.query.order_by(Product.name.asc()).all()
    return render_template("products_list.html", products=products)


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def new_product():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        if not name:
            flash("Product name is required", "danger")
            return redirect(url_for("new_product"))

        product = Product(
            name=name,
            description=(request.form.get("description") or "").strip(),
            unit_price=Decimal(request.form.get("unit_price") or "0"),
            active=bool(request.form.get("active")),
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
        product.description = (request.form.get("description") or "").strip()
        product.unit_price = Decimal(request.form.get("unit_price") or "0")
        product.active = bool(request.form.get("active"))
        db.session.commit()
        flash("Product updated", "success")
        return redirect(url_for("list_products"))

    return render_template("product_form.html", product=product)


@app.route("/products/<int:product_id>/delete", methods=["POST"])
@login_required
@require_role("admin", "accountant")
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    db.session.delete(product)
    db.session.commit()
    flash("Product deleted", "success")
    return redirect(url_for("list_products"))


# ------------------------------------------------------------------------------
# Invoice helpers
# ------------------------------------------------------------------------------

def calculate_invoice_totals(invoice: Invoice):
    subtotal = Decimal("0.00")
    for item in invoice.items:
        item.line_total = (item.quantity or 0) * (item.unit_price or 0)
        subtotal += item.line_total

    settings = get_settings()
    tax_rate = settings.default_tax_rate or Decimal("0.00")
    tax_amount = (subtotal * tax_rate / Decimal("100.00")).quantize(Decimal("0.01"))
    total = subtotal + tax_amount

    payments_total = sum((p.amount or 0) for p in invoice.payments)
    balance_due = total - payments_total

    invoice.subtotal = subtotal
    invoice.tax_rate = tax_rate
    invoice.tax_amount = tax_amount
    invoice.total = total
    invoice.balance_due = balance_due


def next_invoice_number():
    last = Invoice.query.order_by(Invoice.id.desc()).first()
    if not last or not last.number:
        return "INV-0001"
    try:
        prefix, num = last.number.split("-")
        n = int(num) + 1
        return f"{prefix}-{n:04d}"
    except Exception:
        return f"INV-{last.id + 1:04d}"


# ------------------------------------------------------------------------------
# Invoices
# ------------------------------------------------------------------------------

@app.route("/")
@login_required
def home():
    return redirect(url_for("list_invoices"))


@app.route("/invoices")
@login_required
def list_invoices():
    status_filter = request.args.get("status", "open")
    query = Invoice.query

    if status_filter == "open":
        query = query.filter(Invoice.status != "paid")
    elif status_filter == "paid":
        query = query.filter(Invoice.status == "paid")
    elif status_filter == "draft":
        query = query.filter(Invoice.status == "draft")
    # "all" shows everything

    invoices = query.order_by(Invoice.created_at.desc()).all()
    return render_template(
        "invoices_list.html", invoices=invoices, status_filter=status_filter
    )


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required
def new_invoice():
    customers = Customer.query.order_by(Customer.name.asc()).all()
    products = Product.query.filter_by(active=True).order_by(Product.name.asc()).all()

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        customer = Customer.query.get(customer_id)
        if not customer:
            flash("Customer is required", "danger")
            return redirect(url_for("new_invoice"))

        invoice = Invoice(
            customer=customer,
            number=next_invoice_number(),
            issue_date=datetime.strptime(request.form.get("issue_date"), "%Y-%m-%d").date(),
            due_date=(
                datetime.strptime(request.form.get("due_date"), "%Y-%m-%d").date()
                if request.form.get("due_date")
                else None
            ),
            status=request.form.get("status") or "draft",
            notes=request.form.get("notes") or "",
        )

        # Items
        line_count = int(request.form.get("line_count") or "0")
        for i in range(line_count):
            desc = request.form.get(f"items-{i}-description") or ""
            qty = Decimal(request.form.get(f"items-{i}-quantity") or "0")
            unit_price = Decimal(request.form.get(f"items-{i}-unit_price") or "0")
            product_id = request.form.get(f"items-{i}-product_id") or None
            if not desc and qty == 0 and unit_price == 0:
                continue

            item = InvoiceItem(
                description=desc,
                quantity=qty,
                unit_price=unit_price,
            )
            if product_id:
                item.product = Product.query.get(product_id)
            invoice.items.append(item)

        # Payment (optional)
        payment_amount = request.form.get("payment_amount")
        if payment_amount:
            pay = Payment(
                amount=Decimal(payment_amount),
                payment_date=datetime.strptime(
                    request.form.get("payment_date"), "%Y-%m-%d"
                ).date(),
                method=request.form.get("payment_method") or "",
                notes=request.form.get("payment_notes") or "",
            )
            invoice.payments.append(pay)

        calculate_invoice_totals(invoice)
        db.session.add(invoice)
        db.session.commit()

        flash("Invoice created", "success")
        return redirect(url_for("invoice_detail", invoice_id=invoice.id))

    # Default dates
    today = datetime.utcnow().date()
    settings = get_settings()
    if settings.use_global_payment_terms and settings.payment_terms_days:
        default_due = today + timedelta(days=settings.payment_terms_days)
    else:
        default_due = today

    return render_template(
        "invoice_form.html",
        invoice=None,
        customers=customers,
        products=products,
        today=today,
        default_due=default_due,
    )


@app.route("/invoices/<int:invoice_id>")
@login_required
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return render_template("invoice_detail.html", invoice=invoice)


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
@login_required
def edit_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    customers = Customer.query.order_by(Customer.name.asc()).all()
    products = Product.query.filter_by(active=True).order_by(Product.name.asc()).all()

    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        customer = Customer.query.get(customer_id)
        if not customer:
            flash("Customer is required", "danger")
            return redirect(url_for("edit_invoice", invoice_id=invoice.id))

        invoice.customer = customer
        invoice.issue_date = datetime.strptime(
            request.form.get("issue_date"), "%Y-%m-%d"
        ).date()
        due_date_val = request.form.get("due_date")
        invoice.due_date = (
            datetime.strptime(due_date_val, "%Y-%m-%d").date() if due_date_val else None
        )
        invoice.status = request.form.get("status") or invoice.status
        invoice.notes = request.form.get("notes") or ""

        # Clear existing items & rebuild
        invoice.items.clear()
        line_count = int(request.form.get("line_count") or "0")
        for i in range(line_count):
            desc = request.form.get(f"items-{i}-description") or ""
            qty = Decimal(request.form.get(f"items-{i}-quantity") or "0")
            unit_price = Decimal(request.form.get(f"items-{i}-unit_price") or "0")
            product_id = request.form.get(f"items-{i}-product_id") or None
            if not desc and qty == 0 and unit_price == 0:
                continue

            item = InvoiceItem(
                description=desc,
                quantity=qty,
                unit_price=unit_price,
            )
            if product_id:
                item.product = Product.query.get(product_id)
            invoice.items.append(item)

        # Payments on edit (single payment entry for now)
        payment_amount = request.form.get("payment_amount")
        payment_id = request.form.get("payment_id")
        if payment_amount:
            if payment_id:
                # Update existing
                payment = Payment.query.get(payment_id)
                if payment and payment.invoice_id == invoice.id:
                    payment.amount = Decimal(payment_amount)
                    payment.payment_date = datetime.strptime(
                        request.form.get("payment_date"), "%Y-%m-%d"
                    ).date()
                    payment.method = request.form.get("payment_method") or ""
                    payment.notes = request.form.get("payment_notes") or ""
            else:
                # New payment
                pay = Payment(
                    amount=Decimal(payment_amount),
                    payment_date=datetime.strptime(
                        request.form.get("payment_date"), "%Y-%m-%d"
                    ).date(),
                    method=request.form.get("payment_method") or "",
                    notes=request.form.get("payment_notes") or "",
                )
                invoice.payments.append(pay)

        calculate_invoice_totals(invoice)
        db.session.commit()
        flash("Invoice updated", "success")
        return redirect(url_for("invoice_detail", invoice_id=invoice.id))

    # For date pickers
    today = datetime.utcnow().date()
    return render_template(
        "invoice_form.html",
        invoice=invoice,
        customers=customers,
        products=products,
        today=today,
        default_due=invoice.due_date or today,
    )


@app.route("/invoices/<int:invoice_id>/delete", methods=["POST"])
@login_required
@require_role("admin", "accountant")
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    db.session.delete(invoice)
    db.session.commit()
    flash("Invoice deleted", "success")
    return redirect(url_for("list_invoices"))


# ------------------------------------------------------------------------------
# PDF printing
# ------------------------------------------------------------------------------

def draw_invoice_pdf(c, invoice: Invoice, settings: Settings):
    width, height = A4
    margin = 20 * mm
    c.setFillColor(HexColor("#0f172a"))
    c.rect(0, 0, width, height, stroke=0, fill=1)

    # Card background
    c.setFillColor(HexColor("#020617"))
    c.roundRect(margin, margin, width - 2 * margin, height - 2 * margin, 10, stroke=0, fill=1)

    x = margin + 20
    y = height - margin - 40

    # Brand / company
    c.setFillColor(HexColor("#e5e7eb"))
    brand = settings.company_name or settings.brand_name or "Invoice Manager"
    c.setFont("Helvetica-Bold", 18)
    c.drawString(x, y, brand)

    y -= 18
    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#9ca3af"))
    if settings.company_address_line1:
        c.drawString(x, y, settings.company_address_line1)
        y -= 12
    if settings.company_address_line2:
        c.drawString(x, y, settings.company_address_line2)
        y -= 12
    city_line = ", ".join(
        [p for p in [settings.company_city, settings.company_postcode] if p]
    )
    if city_line:
        c.drawString(x, y, city_line)
        y -= 12
    if settings.company_country:
        c.drawString(x, y, settings.company_country)
        y -= 12
    if settings.company_phone:
        c.drawString(x, y, f"Tel: {settings.company_phone}")
        y -= 12
    if settings.company_email:
        c.drawString(x, y, f"Email: {settings.company_email}")
        y -= 12
    if settings.company_tax_id:
        c.drawString(x, y, f"Tax ID: {settings.company_tax_id}")
        y -= 12

    # Invoice header
    right_x = width - margin - 200
    y_header = height - margin - 40

    c.setFillColor(HexColor("#e5e7eb"))
    c.setFont("Helvetica-Bold", 16)
    c.drawRightString(width - margin - 20, y_header, "INVOICE")

    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#9ca3af"))
    y_header -= 16
    c.drawRightString(width - margin - 20, y_header, f"Invoice #: {invoice.number}")
    y_header -= 14
    c.drawRightString(
        width - margin - 20, y_header, f"Issue date: {invoice.issue_date.isoformat()}"
    )
    y_header -= 14
    if invoice.due_date:
        c.drawRightString(
            width - margin - 20, y_header, f"Due date: {invoice.due_date.isoformat()}"
        )

    # Bill to
    y_bill = y - 40
    c.setFillColor(HexColor("#e5e7eb"))
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x, y_bill, "Bill to")
    y_bill -= 14
    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#f9fafb"))
    c.drawString(x, y_bill, invoice.customer.name)
    y_bill -= 12

    c.setFillColor(HexColor("#9ca3af"))
    if invoice.customer.address_line1:
        c.drawString(x, y_bill, invoice.customer.address_line1)
        y_bill -= 12
    if invoice.customer.address_line2:
        c.drawString(x, y_bill, invoice.customer.address_line2)
        y_bill -= 12
    city_line = ", ".join(
        [p for p in [invoice.customer.city, invoice.customer.postcode] if p]
    )
    if city_line:
        c.drawString(x, y_bill, city_line)
        y_bill -= 12
    if invoice.customer.country:
        c.drawString(x, y_bill, invoice.customer.country)
        y_bill -= 12
    if invoice.customer.email:
        c.drawString(x, y_bill, f"Email: {invoice.customer.email}")
        y_bill -= 12

    # Line items table
    table_top = y_bill - 30
    left = x
    right = width - margin - 20

    c.setFont("Helvetica", 8)
    c.setFillColor(HexColor("#9ca3af"))
    c.drawString(left, table_top, "Description")
    c.drawString(left + 260, table_top, "Qty")
    c.drawString(left + 310, table_top, "Unit price")
    c.drawRightString(right, table_top, "Line total")

    c.setStrokeColor(HexColor("#1f2937"))
    c.line(left, table_top - 4, right, table_top - 4)

    currency = settings.currency_symbol or "£"

    y_row = table_top - 16
    c.setFillColor(HexColor("#e5e7eb"))
    for item in invoice.items:
        if y_row < margin + 80:
            c.showPage()
            y_row = height - margin - 80
        c.drawString(left, y_row, item.description[:60])
        c.drawString(left + 260, y_row, f"{item.quantity}")
        c.drawString(left + 310, y_row, f"{currency}{item.unit_price:.2f}")
        c.drawRightString(right, y_row, f"{currency}{item.line_total:.2f}")
        y_row -= 14

    # Totals box
    totals_top = y_row - 20
    box_left = right - 180
    box_right = right
    box_bottom = totals_top - 80

    c.setFillColor(HexColor("#020617"))
    c.roundRect(box_left, box_bottom, box_right - box_left, totals_top - box_bottom, 6, 1, 1)

    c.setFont("Helvetica", 9)
    c.setFillColor(HexColor("#9ca3af"))
    line_y = totals_top - 16
    c.drawString(box_left + 8, line_y, "Subtotal")
    c.setFillColor(HexColor("#e5e7eb"))
    c.drawRightString(box_right - 10, line_y, f"{currency}{invoice.subtotal:.2f}")

    line_y -= 14
    c.setFillColor(HexColor("#9ca3af"))
    c.drawString(box_left + 8, line_y, f"VAT ({invoice.tax_rate:.2f}%)")
    c.setFillColor(HexColor("#e5e7eb"))
    c.drawRightString(box_right - 10, line_y, f"{currency}{invoice.tax_amount:.2f}")

    line_y -= 14
    c.setFillColor(HexColor("#9ca3af"))
    c.drawString(box_left + 8, line_y, "Total")
    c.setFillColor(HexColor("#f97316"))
    c.drawRightString(box_right - 10, line_y, f"{currency}{invoice.total:.2f}")

    line_y -= 14
    c.setFillColor(HexColor("#9ca3af"))
    c.drawString(box_left + 8, line_y, "Balance due")
    c.setFillColor(HexColor("#e5e7eb"))
    c.drawRightString(box_right - 10, line_y, f"{currency}{invoice.balance_due:.2f}")

    # Notes
    if invoice.notes:
        notes_y = box_bottom - 30
        c.setFillColor(HexColor("#e5e7eb"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(left, notes_y, "Notes")
        notes_y -= 14
        c.setFont("Helvetica", 8)
        c.setFillColor(HexColor("#9ca3af"))
        for line in invoice.notes.splitlines():
            if notes_y < margin + 40:
                c.showPage()
                notes_y = height - margin - 40
            c.drawString(left, notes_y, line[:90])
            notes_y -= 12


@app.route("/invoices/<int:invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    settings = get_settings()

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    draw_invoice_pdf(c, invoice, settings)
    c.showPage()
    c.save()
    buffer.seek(0)

    filename = f"invoice-{invoice.number}.pdf"
    return send_file(
        buffer,
        as_attachment=False,
        download_name=filename,
        mimetype="application/pdf",
    )


# ------------------------------------------------------------------------------
# Settings & Webhooks
# ------------------------------------------------------------------------------

def parse_webhook_events_from_form(form):
    events = []
    for key in ["invoice_created", "invoice_updated", "invoice_paid"]:
        if form.get(f"webhook_event_{key}"):
            events.append(key)
    return ",".join(events)


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_view():
    settings = get_settings()

    if request.method == "POST":
        section = request.form.get("section") or "general"

        if section == "general":
            tax_rate_val = request.form.get("default_tax_rate")
            settings.default_tax_rate = (
                Decimal(tax_rate_val) if tax_rate_val not in (None, "") else None
            )
            settings.brand_name = (request.form.get("brand_name") or "").strip()
            settings.logo_url = (request.form.get("logo_url") or "").strip()

            use_global = bool(request.form.get("use_global_payment_terms"))
            settings.use_global_payment_terms = use_global
            if use_global:
                days_val = request.form.get("payment_terms_days") or ""
                settings.payment_terms_days = int(days_val or "0")
            else:
                settings.payment_terms_days = None

            flash("General settings updated", "success")

        elif section == "webhook":
            url_val = (request.form.get("outbound_webhook_url") or "").strip()
            enabled = bool(request.form.get("outbound_webhook_enabled"))
            events_str = parse_webhook_events_from_form(request.form)

            settings.outbound_webhook_url = url_val or None
            settings.outbound_webhook_enabled = enabled
            settings.outbound_webhook_events = events_str or ""

            flash("Webhook settings updated", "success")

        elif section == "company":
            settings.company_name = (request.form.get("company_name") or "").strip()
            settings.company_address_line1 = (
                request.form.get("company_address_line1") or ""
            ).strip()
            settings.company_address_line2 = (
                request.form.get("company_address_line2") or ""
            ).strip()
            settings.company_city = (request.form.get("company_city") or "").strip()
            settings.company_postcode = (
                request.form.get("company_postcode") or ""
            ).strip()
            settings.company_country = (
                request.form.get("company_country") or ""
            ).strip()
            settings.company_phone = (request.form.get("company_phone") or "").strip()
            settings.company_email = (request.form.get("company_email") or "").strip()
            settings.company_tax_id = (
                request.form.get("company_tax_id") or ""
            ).strip()
            settings.currency_symbol = (
                request.form.get("currency_symbol") or "£"
            ).strip()

            flash("Company settings updated", "success")

        db.session.commit()
        return redirect(url_for("settings_view"))

    selected_events = (settings.outbound_webhook_events or "").split(",") if settings.outbound_webhook_events else []

    return render_template(
        "settings.html",
        settings=settings,
        selected_events=selected_events,
    )


def send_webhook_event(event_type: str, payload: dict):
    settings = get_settings()
    if not settings.outbound_webhook_enabled:
        return
    if not settings.outbound_webhook_url:
        return
    events = (settings.outbound_webhook_events or "").split(",")
    if event_type not in events:
        return

    try:
        headers = {"Content-Type": "application/json"}
        requests.post(settings.outbound_webhook_url, headers=headers, data=json.dumps(payload), timeout=5)
    except Exception as exc:
        print(f"Webhook error: {exc}")


# ------------------------------------------------------------------------------
# API Keys & Docs
# ------------------------------------------------------------------------------

def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key_pair():
    raw_key = secrets.token_hex(32)
    key_id = raw_key[:12]
    return key_id, hash_api_key(raw_key), raw_key


@app.route("/api-keys")
@login_required
@require_role("admin", "accountant")
def api_keys_list():
    keys = APIKey.query.order_by(APIKey.created_at.desc()).all()
    return render_template("api_keys.html", keys=keys)


@app.route("/api-keys/new", methods=["POST"])
@login_required
@require_role("admin", "accountant")
def api_keys_new():
    name = (request.form.get("name") or "").strip() or "Unnamed key"
    can_read = bool(request.form.get("can_read"))
    can_write = bool(request.form.get("can_write"))

    key_id, key_hash, raw_key = generate_api_key_pair()
    api_key = APIKey(
        name=name,
        key_id=key_id,
        key_hash=key_hash,
        can_read=can_read,
        can_write=can_write,
        active=True,
    )
    db.session.add(api_key)
    db.session.commit()

    flash(f"API key created. This is the only time you will see it: {raw_key}", "success")
    return redirect(url_for("api_keys_list"))


@app.route("/api-keys/<int:key_id>/toggle", methods=["POST"])
@login_required
@require_role("admin", "accountant")
def api_keys_toggle(key_id):
    key = APIKey.query.get_or_404(key_id)
    key.active = not key.active
    db.session.commit()
    flash("API key updated", "success")
    return redirect(url_for("api_keys_list"))


@app.route("/api-docs")
@login_required
def api_docs():
    settings = get_settings()
    base_url = request.url_root.rstrip("/")
    currency = settings.currency_symbol or "£"
    return render_template("api_docs.html", base_url=base_url, currency=currency)


# ------------------------------------------------------------------------------
# API auth + helpers
# ------------------------------------------------------------------------------

def get_api_key_from_header():
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None, None
    raw = auth.split(" ", 1)[1].strip()
    if len(raw) < 12:
        return None, None
    key_id = raw[:12]
    return key_id, raw


def require_api_key(write=False):
    def decorator(fn):
        def wrapper(*args, **kwargs):
            key_id, raw_key = get_api_key_from_header()
            if not key_id:
                return jsonify({"error": "Missing or invalid API key"}), 401

            key = APIKey.query.filter_by(key_id=key_id, active=True).first()
            if not key:
                return jsonify({"error": "Invalid API key"}), 401

            if write and not key.can_write:
                return jsonify({"error": "API key does not have write permission"}), 403

            expected_hash = key.key_hash
            if not hmac.compare_digest(expected_hash, hash_api_key(raw_key)):
                return jsonify({"error": "Invalid API key"}), 401

            key.last_used_at = datetime.utcnow()
            db.session.commit()
            return fn(*args, **kwargs)

        wrapper.__name__ = fn.__name__
        return wrapper

    return decorator


def invoice_to_dict(inv: Invoice):
    return {
        "id": inv.id,
        "number": inv.number,
        "customer_id": inv.customer_id,
        "status": inv.status,
        "issue_date": inv.issue_date.isoformat() if inv.issue_date else None,
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "notes": inv.notes,
        "subtotal": float(inv.subtotal),
        "tax_rate": float(inv.tax_rate),
        "tax_amount": float(inv.tax_amount),
        "total": float(inv.total),
        "balance_due": float(inv.balance_due),
    }


# ------------------------------------------------------------------------------
# API endpoints
# ------------------------------------------------------------------------------

@app.route("/api/invoices", methods=["POST"])
@require_api_key(write=True)
def api_create_invoice():
    data = request.get_json(force=True, silent=True) or {}
    customer_id = data.get("customer_id")
    customer = Customer.query.get(customer_id)
    if not customer:
        return jsonify({"error": "customer_id is required and must exist"}), 400

    issue_date_str = data.get("issue_date")
    due_date_str = data.get("due_date")
    try:
        issue_date = datetime.fromisoformat(issue_date_str).date() if issue_date_str else datetime.utcnow().date()
        due_date = datetime.fromisoformat(due_date_str).date() if due_date_str else None
    except ValueError:
        return jsonify({"error": "Invalid date format (use ISO 8601)"}), 400

    invoice = Invoice(
        customer=customer,
        number=data.get("number") or next_invoice_number(),
        issue_date=issue_date,
        due_date=due_date,
        status=data.get("status") or "draft",
        notes=data.get("notes") or "",
    )

    items = data.get("items") or []
    for item_data in items:
        desc = item_data.get("description") or ""
        qty = Decimal(str(item_data.get("quantity") or "0"))
        unit_price = Decimal(str(item_data.get("unit_price") or "0"))
        product_id = item_data.get("product_id")

        if not desc and qty == 0 and unit_price == 0:
            continue

        item = InvoiceItem(
            description=desc,
            quantity=qty,
            unit_price=unit_price,
        )
        if product_id:
            item.product = Product.query.get(product_id)
        invoice.items.append(item)

    payments = data.get("payments") or []
    for p in payments:
        amount = Decimal(str(p.get("amount") or "0"))
        if amount <= 0:
            continue
        date_str = p.get("payment_date")
        pay_date = (
            datetime.fromisoformat(date_str).date() if date_str else datetime.utcnow().date()
        )
        payment = Payment(
            amount=amount,
            payment_date=pay_date,
            method=p.get("method") or "",
            notes=p.get("notes") or "",
        )
        invoice.payments.append(payment)

    calculate_invoice_totals(invoice)
    db.session.add(invoice)
    db.session.commit()

    send_webhook_event("invoice_created", {"invoice_id": invoice.id, "number": invoice.number})

    return jsonify(invoice_to_dict(invoice)), 201


@app.route("/api/invoices", methods=["GET"])
@require_api_key(write=False)
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
    return render_template("import_customers.html")


@app.route("/import/invoices", methods=["GET", "POST"])
@login_required
def import_invoices():
    return render_template("import_invoices.html")


# ------------------------------------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        create_default_user_and_key()
    app.run(host="0.0.0.0", port=5000, debug=True)
