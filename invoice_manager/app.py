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


app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL",
    "mysql+pymysql://root:password@localhost/invoicemanager"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

API_KEY = os.environ.get("API_KEY", "change-this-api-key")
db = SQLAlchemy(app)


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
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    invoices = db.relationship("Invoice", back_populates="customer")


class Invoice(db.Model):
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    invoice_number = db.Column(db.String(50), unique=True, nullable=False)
    issue_date = db.Column(db.Date, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    status = db.Column(db.Enum("draft", "sent", "paid", "overdue", "cancelled", name="invoice_status"),
                       nullable=False, default="draft")
    notes = db.Column(db.Text)
    subtotal_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    tax_rate = db.Column(db.Numeric(5, 2))
    tax_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    total_amount = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    balance_due = db.Column(db.Numeric(10, 2), nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    customer = db.relationship("Customer", back_populates="invoices")
    items = db.relationship("InvoiceItem", back_populates="invoice", cascade="all, delete-orphan")
    payments = db.relationship("Payment", back_populates="invoice", cascade="all, delete-orphan")

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


class Settings(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    default_tax_rate = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal("0.00"))


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


def get_settings():
    settings = Settings.query.get(1)
    if not settings:
        settings = Settings(id=1, default_tax_rate=Decimal("0.00"))
        db.session.add(settings)
        db.session.commit()
    return settings


def determine_tax_rate_for_customer(customer):
    settings = get_settings()
    if customer is None:
        return settings.default_tax_rate
    if customer.use_default_tax or customer.tax_rate is None:
        return settings.default_tax_rate
    return customer.tax_rate


@app.route("/")
def index():
    return redirect(url_for("list_invoices"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    settings = get_settings()
    if request.method == "POST":
        rate_str = (request.form.get("default_tax_rate") or "").strip()
        try:
            settings.default_tax_rate = Decimal(rate_str or "0")
            db.session.commit()
            flash("Settings saved", "success")
        except Exception:
            flash("Invalid tax rate", "danger")
        return redirect(url_for("settings"))
    return render_template("settings.html", settings=settings)

# (Rest of app code omitted in this shortened version)
if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
