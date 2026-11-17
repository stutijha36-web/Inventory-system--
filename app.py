"""Inventory Management System """
import os
import threading
import webbrowser
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from functools import wraps

import mysql.connector
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-secret")
DB = dict(host=os.environ.get("DB_HOST", "localhost"),
          user=os.environ.get("DB_USER", "root"),
          password=os.environ.get("DB_PASSWORD", "Study@2026"),
          database=os.environ.get("DB_NAME", "inventory_db"))


# ---------------------------------------------------------------- DB helpers
def conn():
    return mysql.connector.connect(**DB)


def query(sql, args=(), one=False, as_dict=True):
    c = conn(); cur = c.cursor(dictionary=as_dict)
    try:
        cur.execute(sql, args)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        cur.close(); c.close()


@contextmanager
def tx():
    """Transaction: commits on success, rolls back on any error."""
    c = conn(); cur = c.cursor(dictionary=True)
    try:
        yield cur
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        cur.close(); c.close()


def log_stock(cur, pid, change, balance, reason):
    cur.execute("INSERT INTO stock_history(product_id,change_qty,balance,reason) "
                "VALUES(%s,%s,%s,%s)", (pid, change, balance, reason))


def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if "admin" not in session:
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper


# ---------------------------------------------------------------- 1. Auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = query("SELECT * FROM admin WHERE username=%s",
                  (request.form["username"].strip(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear(); session["admin"] = u["username"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    one = lambda s: query(s, one=True, as_dict=False)[0]
    stats = dict(
        products=one("SELECT COUNT(*) FROM products"),
        suppliers=one("SELECT COUNT(*) FROM suppliers"),
        today_sales=one("SELECT COALESCE(SUM(total),0) FROM sales WHERE DATE(sale_date)=CURDATE()"),
        low=one("SELECT COUNT(*) FROM products WHERE quantity<=reorder_level"))
    low = query("SELECT id,name,quantity,reorder_level FROM products "
                "WHERE quantity<=reorder_level ORDER BY quantity LIMIT 10")
    return render_template("dashboard.html", stats=stats, low=low)


# ------------------------------------------- 2/3/4. Products, Categories, Suppliers
CFG = {
    "products": dict(
        title="Products", table="products",
        cols=[("id", "ID"), ("name", "Name"), ("category", "Category"), ("price", "Price"),
              ("quantity", "Qty"), ("reorder_level", "Low-stock level")],
        fields=[("name", "Name", "text"), ("category_id", "Category", "select"),
                ("price", "Price", "number"), ("quantity", "Quantity", "int"),
                ("reorder_level", "Low-stock level", "int")],
        select="SELECT p.*, c.name AS category FROM products p "
               "LEFT JOIN categories c ON c.id=p.category_id",
        search=["p.id", "p.name"]),
    "categories": dict(
        title="Categories", table="categories",
        cols=[("id", "ID"), ("name", "Name"), ("description", "Description")],
        fields=[("name", "Name", "text"), ("description", "Description", "text")],
        select="SELECT * FROM categories", search=["id", "name"],
        extra="/categories/{id}/products"),
    "suppliers": dict(
        title="Suppliers", table="suppliers",
        cols=[("id", "ID"), ("name", "Name"), ("phone", "Phone"),
              ("email", "Email"), ("address", "Address")],
        fields=[("name", "Name", "text"), ("phone", "Phone", "text"),
                ("email", "Email", "text"), ("address", "Address", "text")],
        select="SELECT * FROM suppliers", search=["id", "name", "phone"]),
}


def parse(cfg):
    vals = {}
    for k, label, t in cfg["fields"]:
        v = request.form.get(k, "").strip()
        if t == "int":
            vals[k] = int(v or 0)
        elif t == "number":
            vals[k] = Decimal(v or 0)
        elif t == "select":
            vals[k] = int(v) if v else None
        else:
            vals[k] = v or None
        if t in ("int", "number") and vals[k] < 0:
            raise ValueError(f"{label} cannot be negative.")
    if not vals.get("name"):
        raise ValueError("Name is required.")
    return vals


@app.route("/<ent>/")
@login_required
def crud_list(ent):
    cfg = CFG.get(ent) or abort(404)
    q = request.args.get("q", "").strip()
    sql, args = cfg["select"], []
    if q:
        sql += " WHERE " + " OR ".join(f"CAST({c} AS CHAR) LIKE %s" for c in cfg["search"])
        args = [f"%{q}%"] * len(cfg["search"])
    rows = query(sql + " ORDER BY 1 DESC", args)
    return render_template("list.html", cfg=cfg, ent=ent, rows=rows, q=q, heading=cfg["title"], search=True)


@app.route("/categories/<int:id>/products")
@login_required
def category_products(id):
    cat = query("SELECT * FROM categories WHERE id=%s", (id,), one=True) or abort(404)
    cfg = CFG["products"]
    rows = query(cfg["select"] + " WHERE p.category_id=%s ORDER BY p.name", (id,))
    return render_template("list.html", cfg=cfg, ent="products", rows=rows, q="",
                           heading=f"Products in category: {cat['name']}", search=False)


@app.route("/<ent>/add", methods=["GET", "POST"])
@app.route("/<ent>/<int:id>/edit", methods=["GET", "POST"])
@login_required
def crud_form(ent, id=None):
    cfg = CFG.get(ent) or abort(404)
    row = {}
    if id:
        row = query(f"SELECT * FROM {cfg['table']} WHERE id=%s", (id,), one=True) or abort(404)
    if request.method == "POST":
        try:
            vals = parse(cfg)
            with tx() as cur:
                if id:
                    if ent == "products":
                        cur.execute("SELECT quantity FROM products WHERE id=%s FOR UPDATE", (id,))
                        diff = vals["quantity"] - cur.fetchone()["quantity"]
                        if diff:
                            log_stock(cur, id, diff, vals["quantity"], "Manual edit")
                    cur.execute(f"UPDATE {cfg['table']} SET "
                                + ", ".join(f"{k}=%s" for k in vals) + " WHERE id=%s",
                                (*vals.values(), id))
                else:
                    cur.execute(f"INSERT INTO {cfg['table']} ({', '.join(vals)}) VALUES ("
                                + ", ".join(["%s"] * len(vals)) + ")", tuple(vals.values()))
                    if ent == "products" and vals["quantity"]:
                        log_stock(cur, cur.lastrowid, vals["quantity"], vals["quantity"], "Opening stock")
            flash("Saved successfully.", "ok")
            return redirect(url_for("crud_list", ent=ent))
        except (ValueError, ArithmeticError) as e:
            flash(str(e) if isinstance(e, ValueError) else "Invalid number entered.", "error")
        except mysql.connector.IntegrityError:
            flash("Duplicate or invalid value.", "error")
        row = request.form
    opts = {"category_id": query("SELECT id,name FROM categories ORDER BY name")}
    return render_template("form.html", cfg=cfg, ent=ent, row=row, opts=opts, id=id)


@app.route("/<ent>/<int:id>/delete", methods=["POST"])
@login_required
def crud_delete(ent, id):
    cfg = CFG.get(ent) or abort(404)
    try:
        with tx() as cur:
            cur.execute(f"DELETE FROM {cfg['table']} WHERE id=%s", (id,))
        flash("Deleted.", "ok")
    except mysql.connector.IntegrityError:
        flash("Cannot delete: record is used in purchases/sales.", "error")
    return redirect(url_for("crud_list", ent=ent))


# ---------------------------------------------------------------- 5. Stock
@app.route("/stock")
@login_required
def stock():
    rows = query("SELECT id,name,quantity,reorder_level FROM products ORDER BY name")
    return render_template("stock.html", rows=rows)


@app.route("/stock/adjust", methods=["POST"])
@login_required
def stock_adjust():
    try:
        pid, qty = int(request.form["product_id"]), int(request.form["quantity"])
        mode, note = request.form["mode"], request.form.get("note", "").strip()
        with tx() as cur:
            cur.execute("SELECT quantity FROM products WHERE id=%s FOR UPDATE", (pid,))
            cur_qty = cur.fetchone()["quantity"]
            new = cur_qty + qty if mode == "add" else qty
            if new < 0:
                raise ValueError("Stock cannot go below zero.")
            cur.execute("UPDATE products SET quantity=%s WHERE id=%s", (new, pid))
            log_stock(cur, pid, new - cur_qty, new, note or ("Stock added" if mode == "add" else "Stock set"))
        flash("Stock updated.", "ok")
    except (ValueError, TypeError, KeyError) as e:
        flash(str(e) or "Invalid input.", "error")
    return redirect(url_for("stock"))


@app.route("/stock/history")
@login_required
def stock_history():
    pid = request.args.get("product")
    sql = ("SELECT h.created_at, p.name, h.change_qty, h.balance, h.reason FROM stock_history h "
           "JOIN products p ON p.id=h.product_id")
    rows = query(sql + (" WHERE h.product_id=%s" if pid else "") + " ORDER BY h.id DESC LIMIT 500",
                 (pid,) if pid else (), as_dict=False)
    return render_template("report.html", title="Stock History", filters=[], links=None, total=None,
                           headers=["Date", "Product", "Change", "Balance", "Reason"], rows=rows)


# ------------------------------------------------- 6. Purchases / 7. Sales
def product_json():
    return [dict(id=p["id"], name=p["name"], price=float(p["price"]), qty=p["quantity"])
            for p in query("SELECT id,name,price,quantity FROM products ORDER BY name")]


def read_items(merge=False):
    items = []
    for pid, qt, co in zip(request.form.getlist("product_id"), request.form.getlist("quantity"),
                           request.form.getlist("unit_cost") or [""] * 999):
        if pid and qt:
            q, cost = int(qt), Decimal(co or 0)
            if q <= 0 or cost < 0:
                raise ValueError("Quantities must be positive and costs non-negative.")
            items.append([int(pid), q, cost])
    if not items:
        raise ValueError("Add at least one product.")
    if merge:  # combine duplicate products so stock check is accurate
        m = {}
        for pid, q, _ in items:
            m[pid] = m.get(pid, 0) + q
        items = [[p, q, None] for p, q in m.items()]
    return items


@app.route("/purchases")
@login_required
def purchases():
    rows = query("SELECT p.id, s.name AS party, p.purchase_date AS dt, p.total FROM purchases p "
                 "JOIN suppliers s ON s.id=p.supplier_id ORDER BY p.id DESC")
    return render_template("txlist.html", kind="purchase", rows=rows)


@app.route("/purchases/new", methods=["GET", "POST"])
@login_required
def purchase_new():
    if request.method == "POST":
        try:
            items = read_items()
            sup = int(request.form["supplier_id"])
            with tx() as cur:
                cur.execute("INSERT INTO purchases(supplier_id,total) VALUES(%s,0)", (sup,))
                pid_ = cur.lastrowid; total = Decimal(0)
                for pid, q, cost in items:
                    cur.execute("SELECT quantity FROM products WHERE id=%s FOR UPDATE", (pid,))
                    bal = cur.fetchone()["quantity"] + q
                    sub = cost * q; total += sub
                    cur.execute("INSERT INTO purchase_items(purchase_id,product_id,quantity,unit_cost,subtotal)"
                                " VALUES(%s,%s,%s,%s,%s)", (pid_, pid, q, cost, sub))
                    cur.execute("UPDATE products SET quantity=%s WHERE id=%s", (bal, pid))
                    log_stock(cur, pid, q, bal, f"Purchase #{pid_}")
                cur.execute("UPDATE purchases SET total=%s WHERE id=%s", (total, pid_))
            flash(f"Purchase #{pid_} recorded. Stock increased.", "ok")
            return redirect(url_for("purchases"))
        except (ValueError, KeyError, ArithmeticError) as e:
            flash(str(e) or "Invalid input.", "error")
    return render_template("trans.html", kind="purchase", products=product_json(),
                           suppliers=query("SELECT id,name FROM suppliers ORDER BY name"))


@app.route("/sales")
@login_required
def sales():
    rows = query("SELECT id, invoice_no, COALESCE(customer_name,'Walk-in') AS party, sale_date AS dt, total "
                 "FROM sales ORDER BY id DESC")
    return render_template("txlist.html", kind="sale", rows=rows)


@app.route("/sales/new", methods=["GET", "POST"])
@login_required
def sale_new():
    if request.method == "POST":
        try:
            items = read_items(merge=True)
            name = request.form.get("customer_name", "").strip() or None
            with tx() as cur:
                cur.execute("INSERT INTO sales(customer_name,total) VALUES(%s,0)", (name,))
                sid = cur.lastrowid; total = Decimal(0); inv = f"INV-{sid:05d}"
                for pid, q, _ in items:
                    cur.execute("SELECT name,price,quantity FROM products WHERE id=%s FOR UPDATE", (pid,))
                    p = cur.fetchone()
                    if not p:
                        raise ValueError("Product not found.")
                    if p["quantity"] < q:   # prevent overselling (whole sale rolls back)
                        raise ValueError(f"Insufficient stock for '{p['name']}' (available: {p['quantity']}, requested: {q}).")
                    sub = p["price"] * q; total += sub
                    cur.execute("INSERT INTO sale_items(sale_id,product_id,quantity,unit_price,subtotal) "
                                "VALUES(%s,%s,%s,%s,%s)", (sid, pid, q, p["price"], sub))
                    cur.execute("UPDATE products SET quantity=quantity-%s WHERE id=%s", (q, pid))
                    log_stock(cur, pid, -q, p["quantity"] - q, f"Sale {inv}")
                cur.execute("UPDATE sales SET total=%s, invoice_no=%s WHERE id=%s", (total, inv, sid))
            return redirect(url_for("invoice", id=sid))
        except (ValueError, ArithmeticError) as e:
            flash(str(e) or "Invalid input.", "error")
    return render_template("trans.html", kind="sale", products=product_json(), suppliers=[])


@app.route("/sales/<int:id>/invoice")
@login_required
def invoice(id):
    sale = query("SELECT * FROM sales WHERE id=%s", (id,), one=True) or abort(404)
    items = query("SELECT p.name, i.quantity, i.unit_price, i.subtotal FROM sale_items i "
                  "JOIN products p ON p.id=i.product_id WHERE i.sale_id=%s", (id,))
    return render_template("invoice.html", sale=sale, items=items)


# ---------------------------------------------------------------- 8. Reports
@app.route("/reports")
@login_required
def reports_home():
    links = [(url_for("report", kind=k), t) for k, t in [
        ("daily", "Daily Sales"), ("monthly", "Monthly Sales"), ("purchases", "Purchases"),
        ("stock", "Available Stock"), ("lowstock", "Low-Stock Products"), ("productsales", "Product-wise Sales")]]
    return render_template("report.html", title="Reports", links=links, filters=[], headers=[], rows=[], total=None)


@app.route("/reports/<kind>")
@login_required
def report(kind):
    today = date.today().isoformat()
    d = request.args.get("date", today); m = request.args.get("month", today[:7])
    f = request.args.get("from", today[:8] + "01"); t = request.args.get("to", today)
    rng = "DATE({c}) BETWEEN %s AND %s"
    R = {
        "daily": ("Daily Sales Report", [("date", "date", d)], ["Invoice", "Customer", "Time", "Total"],
                  "SELECT invoice_no, COALESCE(customer_name,'Walk-in'), sale_date, total FROM sales "
                  "WHERE DATE(sale_date)=%s ORDER BY id", (d,), 3),
        "monthly": ("Monthly Sales Report", [("month", "month", m)], ["Date", "Invoices", "Total"],
                    "SELECT DATE(sale_date), COUNT(*), SUM(total) FROM sales "
                    "WHERE DATE_FORMAT(sale_date,'%%Y-%%m')=%s GROUP BY DATE(sale_date) ORDER BY 1", (m,), 2),
        "purchases": ("Purchase Report", [("from", "date", f), ("to", "date", t)],
                      ["ID", "Supplier", "Date", "Total"],
                      "SELECT p.id, s.name, p.purchase_date, p.total FROM purchases p JOIN suppliers s "
                      "ON s.id=p.supplier_id WHERE " + rng.format(c="p.purchase_date") + " ORDER BY p.id",
                      (f, t), 3),
        "stock": ("Available Stock Report", [], ["ID", "Product", "Category", "Qty", "Price", "Stock Value"],
                  "SELECT p.id, p.name, COALESCE(c.name,'-'), p.quantity, p.price, p.quantity*p.price "
                  "FROM products p LEFT JOIN categories c ON c.id=p.category_id ORDER BY p.name", (), 5),
        "lowstock": ("Low-Stock Report", [], ["ID", "Product", "Qty", "Low-stock level"],
                     "SELECT id, name, quantity, reorder_level FROM products "
                     "WHERE quantity<=reorder_level ORDER BY quantity", (), None),
        "productsales": ("Product-wise Sales Report", [("from", "date", f), ("to", "date", t)],
                         ["Product", "Units Sold", "Revenue"],
                         "SELECT p.name, SUM(i.quantity), SUM(i.subtotal) FROM sale_items i "
                         "JOIN sales s ON s.id=i.sale_id JOIN products p ON p.id=i.product_id WHERE "
                         + rng.format(c="s.sale_date") + " GROUP BY p.id, p.name ORDER BY 3 DESC", (f, t), 2),
    }
    if kind not in R:
        abort(404)
    title, filters, headers, sql, args, sumcol = R[kind]
    rows = query(sql, args, as_dict=False)
    total = sum(r[sumcol] or 0 for r in rows) if sumcol is not None else None
    return render_template("report.html", title=title, filters=filters, headers=headers,
                           rows=rows, total=total, links=None)


# ---------------------------------------------------------------- startup
def init_admin():
    """Create default admin (admin / admin123) with a hashed password if none exists."""
    if not query("SELECT id FROM admin LIMIT 1"):
        with tx() as cur:
            cur.execute("INSERT INTO admin(username,password_hash) VALUES(%s,%s)",
                        ("admin", generate_password_hash("admin123")))
        print("Default admin created: admin / admin123  (change it!)")


if __name__ == "__main__":
    init_admin()
    if os.environ.get("WERKZEUG_RUN_MAIN") !="true":
        threading.Timer(1.5,lambda: webbrowser.open("http://127.0.01:5000")).start()
    app.run(debug=True)
