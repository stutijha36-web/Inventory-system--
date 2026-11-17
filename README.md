# Inventory Management System (Flask + MySQL)

## Setup
1. `pip install -r requirements.txt`
2. Import the schema: `mysql -u root -p < schema.sql`
3. Set env vars if needed: `DB_HOST DB_USER DB_PASSWORD DB_NAME SECRET_KEY`
4. `python app.py` -> http://127.0.0.1:5000  (default login **admin / admin123**, stored hashed; change it)

## Modules -> where
| Module | Routes |
|---|---|
| Admin login/logout | /login, /logout (werkzeug hashed passwords) |
| Products / Categories / Suppliers | /products/, /categories/, /suppliers/ (+ add, edit, delete, search); /categories/<id>/products |
| Stock | /stock, /stock/adjust, /stock/history, low-stock alerts on dashboard |
| Purchases | /purchases, /purchases/new (stock auto-increases) |
| Sales | /sales, /sales/new (stock auto-decreases, blocked if insufficient), /sales/<id>/invoice |
| Reports | /reports (daily, monthly, purchases, stock, low-stock, product-wise) |

## Design notes
- Purchases/sales run in one DB transaction with row locks (`FOR UPDATE`): any error rolls back everything.
- Every stock change (opening, manual edit, adjustment, purchase, sale) writes to `stock_history`.
- Tables: admin, products, categories, suppliers, purchases, purchase_items, sales, sale_items, stock_history.

## Test checklist
1. Login with wrong/right credentials; logout blocks pages.
2. CRUD for category, supplier, product; search by name and ID; deleting a used product shows an error.
3. Purchase 20 units -> stock +20, history row, total = sum(qty x cost).
4. Sale of more than available -> rejected, nothing saved. Valid sale -> stock decreases, invoice opens.
5. Set quantity <= low-stock level -> appears on dashboard, stock page and low-stock report.
6. Reports match the sales/purchases you entered; stock history balance equals product quantity.
