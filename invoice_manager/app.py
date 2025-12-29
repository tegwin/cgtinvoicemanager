import os
from datetime import datetime, date
from decimal import Decimal
import csv
import io

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort
)
from flask_sqlalchemy import SQLAlchemy


# -----------------------
# Config & setup
# -----------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")

app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://root:password@localhost/invoicemanager"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

API_KEY = os.environ.get("API_KEY", "change-this-api-key")
db = SQLAlchemy(app)


# -----------------------
# Models
# -----------------------

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
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False,
        default=datetime.utcnow, onupdate=datetime.utcnow
    )

    invoices = db.relationship("Invoice", back_populates="customer")

    def __repr__(self):
        return f"<Customer {self.id} {self.name}>"


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.Enum("draft", "sent", "paid", "overdue", "cancelled",
                               name="invoice_status"),
                       nullable=False, default="draft")
    notes = db.Column(db.Text)
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
        total = sum((item.line_total for item in self.items), Decimal("0.00"))
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
    description = db.Column(db.String(255), nullable=False)
    quantity = db.Column(db.Numeric(10, 2), nullable=False)
    unit_price = db.Column(db.Numeric(10, 2), nullable=False)
    line_total = db.Column(db.Numeric(10, 2), nullable=False)

    invoice = db.relationship("Invoice", back_populates="items")

    def to_dict(self):
        return {
            "id": self.id,
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


# -----------------------
# Helpers
# -----------------------

def require_api_key():
    key = request.headers.get("X-API-Key")
    if not key or key != API_KEY:
        abort(401, description="Invalid API key")


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


# -----------------------
# Web UI routes
# -----------------------

@app.route("/")
def index():
    return redirect(url_for("list_invoices"))


# Customers

@app.route("/customers")
def list_customers():
    customers = Customer.query.order_by(Customer.name).all()
    return render_template("customers_list.html", customers=customers)


@app.route("/customers/new", methods=["GET", "POST"])
def new_customer():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Customer name is required", "danger")
            return render_template("customer_form.html", customer=None)

        customer = Customer(
            name=name,
            email=request.form.get("email"),
            phone=request.form.get("phone"),
            address_line1=request.form.get("address_line1"),
            address_line2=request.form.get("address_line2"),
            city=request.form.get("city"),
            postcode=request.form.get("postcode"),
            country=request.form.get("country"),
        )
        db.session.add(customer)
        db.session.commit()
        flash("Customer created", "success")
        return redirect(url_for("list_customers"))

    return render_template("customer_form.html", customer=None)


@app.route("/customers/<int:customer_id>/edit", methods=["GET", "POST"])
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

        if not customer.name:
            flash("Customer name is required", "danger")
            return render_template("customer_form.html", customer=customer)

        db.session.commit()
        flash("Customer updated", "success")
        return redirect(url_for("list_customers"))

    return render_template("customer_form.html", customer=customer)


# Invoices

@app.route("/invoices")
def list_invoices():
    invoices = Invoice.query.order_by(Invoice.created_at.desc()).all()
    return render_template("invoices_list.html", invoices=invoices)


@app.route("/invoices/new", methods=["GET", "POST"])
def new_invoice():
    customers = Customer.query.order_by(Customer.name).all()
    if request.method == "POST":
        return handle_invoice_form()
    return render_template("invoice_form.html", invoice=None, customers=customers)


@app.route("/invoices/<int:invoice_id>/edit", methods=["GET", "POST"])
def edit_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    customers = Customer.query.order_by(Customer.name).all()

    if request.method == "POST":
        return handle_invoice_form(invoice)
    return render_template("invoice_form.html", invoice=invoice, customers=customers)


def handle_invoice_form(invoice=None):
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
        return render_template("invoice_form.html", invoice=invoice, customers=customers)

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

    descriptions = request.form.getlist("item_description")
    quantities = request.form.getlist("item_quantity")
    unit_prices = request.form.getlist("item_unit_price")

    for desc, qty_str, price_str in zip(descriptions, quantities, unit_prices):
        desc = (desc or "").strip()
        if not desc:
            continue
        try:
            qty = Decimal(qty_str or "0")
            price = Decimal(price_str or "0")
        except Exception:
            continue

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
    db.session.commit()
    flash("Invoice saved", "success")
    return redirect(url_for("invoice_detail", invoice_id=invoice.id))


@app.route("/invoices/<int:invoice_id>")
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return render_template("invoice_detail.html", invoice=invoice)


@app.route("/invoices/<int:invoice_id>/payments/new", methods=["POST"])
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

    flash("Payment added", "success")
    return redirect(url_for("invoice_detail", invoice_id=invoice.id))


# -----------------------
# CSV Import Routes
# -----------------------

@app.route("/import/customers", methods=["GET", "POST"])
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

        if customer:
            customer.name = name
            customer.phone = (row.get("phone") or "").strip() or None
            customer.address_line1 = (row.get("address_line1") or "").strip() or None
            customer.address_line2 = (row.get("address_line2") or "").strip() or None
            customer.city = (row.get("city") or "").strip() or None
            customer.postcode = (row.get("postcode") or "").strip() or None
            customer.country = (row.get("country") or "").strip() or None
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
            )
            db.session.add(customer)
            created += 1

    db.session.commit()
    flash(f"Imported customers. Created: {created}, Updated: {updated}", "success")
    return redirect(url_for("list_customers"))


@app.route("/import/invoices", methods=["GET", "POST"])
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

    flash(f"Imported {count} invoices. {errors} rows skipped due to errors.", "success")
    return redirect(url_for("list_invoices"))


# -----------------------
# API endpoints
# -----------------------

@app.route("/api/invoices", methods=["POST"])
def api_create_invoice():
    require_api_key()
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
        customer = Customer(
            name=name,
            email=customer_data.get("email"),
            phone=customer_data.get("phone"),
            address_line1=customer_data.get("address_line1"),
            address_line2=customer_data.get("address_line2"),
            city=customer_data.get("city"),
            postcode=customer_data.get("postcode"),
            country=customer_data.get("country"),
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
        invoice_number=generate_invoice_number(),
        issue_date=issue_date,
        due_date=due_date,
        status=data.get("status", "sent"),
        notes=data.get("notes"),
    )
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
    require_api_key()
    invoice = Invoice.query.get_or_404(invoice_id)
    return jsonify(invoice.to_dict())


@app.route("/api/webhooks/payment", methods=["POST"])
def api_webhook_payment():
    require_api_key()
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


# -----------------------
# CLI helper
# -----------------------

@app.cli.command("init-db")
def init_db_command():
    db.create_all()
    print("DB initialized")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
