import os
import sqlite3
import requests
from flask import Flask, request, jsonify
from functools import wraps

app = Flask(__name__)

# Configuration
SHOP = os.getenv("SHOPIFY_STORE", "").replace(
    "https://", ""
).replace("http://", "").strip("/")

TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")
API_KEY = os.getenv("API_SECRET")

VERSION = "2026-10"
DB = "shopify_api.db"


# ---------------- DATABASE ----------------

def init_db():
    with sqlite3.connect(DB) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                request_id TEXT PRIMARY KEY,
                shopify_order_id TEXT NOT NULL,
                status TEXT DEFAULT 'PENDING',
                transaction_id TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                transaction_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)

init_db()


# ---------------- SECURITY ----------------

def auth_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        key = request.headers.get("X-API-Key")

        if not API_KEY or key != API_KEY:
            return jsonify({
                "success": False,
                "error": "Unauthorized API access"
            }), 401

        return func(*args, **kwargs)
    return wrapper


# ---------------- SHOPIFY GRAPHQL ----------------

def shopify(query, variables=None):

    if not SHOP or not TOKEN:
        raise Exception("Shopify credentials not configured")

    url = (
        f"https://{SHOP}/admin/api/"
        f"{VERSION}/graphql.json"
    )

    response = requests.post(
        url,
        headers={
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": TOKEN
        },
        json={
            "query": query,
            "variables": variables or {}
        },
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    if result.get("errors"):
        raise Exception(str(result["errors"]))

    return result["data"]


def check_errors(payload):
    errors = payload.get("userErrors", [])
    if errors:
        raise Exception(str(errors))


# ---------------- HOME ----------------

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "success": True,
        "message": "Shopify API Server Running",
        "version": VERSION
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"})


# ---------------- TEST SHOP CONNECTION ----------------

@app.route("/api/shop", methods=["GET"])
@auth_required
def shop_info():

    try:
        data = shopify("""
            query {
                shop {
                    name
                    myshopifyDomain
                    currencyCode
                }
            }
        """)

        return jsonify({
            "success": True,
            "shop": data["shop"]
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- CREATE ORDER ----------------

@app.route("/api/orders/create", methods=["POST"])
@auth_required
def create_order():

    data = request.get_json(silent=True) or {}

    request_id = data.get("request_id")
    items = data.get("items", [])
    email = data.get("email")

    if not request_id or not isinstance(request_id, str):
        return jsonify({"error": "request_id required"}), 400

    if not items or len(items) > 50:
        return jsonify({"error": "Valid items required"}), 400

    # Prevent duplicate order creation
    with sqlite3.connect(DB) as con:
        old = con.execute(
            "SELECT shopify_order_id, status FROM orders WHERE request_id=?",
            (request_id,)
        ).fetchone()

    if old:
        return jsonify({
            "success": True,
            "duplicate": True,
            "order_id": old[0],
            "status": old[1]
        })

    line_items = []

    for item in items:
        variant_id = item.get("variant_id")
        quantity = item.get("quantity", 1)

        if not variant_id or not isinstance(quantity, int) or quantity < 1:
            return jsonify({"error": "Invalid product item"}), 400

        if str(variant_id).startswith("gid://"):
            gid = str(variant_id)
        else:
            gid = f"gid://shopify/ProductVariant/{variant_id}"

        line_items.append({
            "variantId": gid,
            "quantity": quantity
        })

    try:
        query = """
        mutation CreateOrder($order: OrderCreateOrderInput!) {
            orderCreate(order: $order) {
                order {
                    id
                    name
                    displayFinancialStatus
                }
                userErrors {
                    field
                    message
                }
            }
        }
        """

        variables = {
            "order": {
                "email": email,
                "lineItems": line_items,
                "financialStatus": "PENDING",
                "tags": ["External-Gateway", "Payment-Pending"]
            }
        }

        result = shopify(query, variables)["orderCreate"]

        check_errors(result)

        order = result.get("order")

        if not order:
            raise Exception("Shopify did not return an order")

        # Save mapping
        with sqlite3.connect(DB) as con:
            con.execute("""
                INSERT INTO orders
                (request_id, shopify_order_id, status)
                VALUES (?, ?, ?)
            """, (request_id, order["id"], "PENDING"))

        return jsonify({
            "success": True,
            "order_id": order["id"],
            "order_name": order["name"],
            "payment_status": "PENDING"
        }), 201

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- PAYMENT SUCCESS ----------------

@app.route("/api/payments/success", methods=["POST"])
@auth_required
def payment_success():

    data = request.get_json(silent=True) or {}

    order_id = data.get("order_id")
    transaction_id = data.get("transaction_id")

    if not order_id or not transaction_id:
        return jsonify({
            "error": "order_id and transaction_id required"
        }), 400

    try:
        with sqlite3.connect(DB) as con:
            old = con.execute(
                "SELECT status, order_id FROM payments WHERE transaction_id=?",
                (transaction_id,)
            ).fetchone()

            if old:
                if old[1] != order_id:
                    return jsonify({"error": "Transaction ID conflict"}), 409

                return jsonify({
                    "success": old[0] == "SUCCESS",
                    "duplicate": True,
                    "status": old[0]
                })

            known_order = con.execute(
                "SELECT status FROM orders WHERE shopify_order_id=?",
                (order_id,)
            ).fetchone()

            if not known_order:
                return jsonify({"error": "Unknown order"}), 404

            if known_order[0] != "PENDING":
                return jsonify({"error": "Order is not pending"}), 409

            con.execute("""
                INSERT INTO payments VALUES (?, ?, ?)
            """, (transaction_id, order_id, "PROCESSING"))

        result = shopify("""
            mutation MarkPaid($input: OrderMarkAsPaidInput!) {
                orderMarkAsPaid(input: $input) {
                    order {
                        id
                        displayFinancialStatus
                    }
                    userErrors {
                        field
                        message
                    }
                }
            }
        """, {
            "input": {"id": order_id}
        })["orderMarkAsPaid"]

        check_errors(result)

        order = result.get("order")

        if not order:
            raise Exception("Shopify did not confirm payment")

        with sqlite3.connect(DB) as con:
            con.execute("""
                UPDATE orders SET status='PAID',
                transaction_id=? WHERE shopify_order_id=?
            """, (transaction_id, order_id))

            con.execute("""
                UPDATE payments SET status='SUCCESS'
                WHERE transaction_id=?
            """, (transaction_id,))

        return jsonify({
            "success": True,
            "order_id": order_id,
            "transaction_id": transaction_id,
            "payment_status": order["displayFinancialStatus"]
        })

    except Exception as e:
        with sqlite3.connect(DB) as con:
            con.execute("""
                UPDATE payments SET status='ERROR'
                WHERE transaction_id=?
            """, (transaction_id,))

        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- CANCEL ORDER ----------------

@app.route("/api/orders/cancel", methods=["POST"])
@auth_required
def cancel_order():

    data = request.get_json(silent=True) or {}

    order_id = data.get("order_id")

    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    try:
        with sqlite3.connect(DB) as con:
            old = con.execute(
                "SELECT status FROM orders WHERE shopify_order_id=?",
                (order_id,)
            ).fetchone()

        if not old:
            return jsonify({"error": "Unknown order"}), 404

        if old[0] == "PAID":
            return jsonify({
                "error": "Paid order cannot be cancelled automatically here. Refund policy required."
            }), 409

        result = shopify("""
            mutation CancelOrder(
                $orderId: ID!,
                $reason: OrderCancelReason!,
                $restock: Boolean!
            ) {
                orderCancel(
                    orderId: $orderId,
                    reason: $reason,
                    restock: $restock
                ) {
                    job {
                        id
                        done
                    }
                    orderCancelUserErrors {
                        field
                        message
                    }
                }
            }
        """, {
            "orderId": order_id,
            "reason": "DECLINED",
            "restock": True
        })["orderCancel"]

        errors = result.get("orderCancelUserErrors", [])

        if errors:
            raise Exception(str(errors))

        with sqlite3.connect(DB) as con:
            con.execute("""
                UPDATE orders SET status='CANCEL_REQUESTED'
                WHERE shopify_order_id=?
            """, (order_id,))

        return jsonify({
            "success": True,
            "order_id": order_id,
            "status": "CANCEL_REQUESTED",
            "job": result.get("job")
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------- ORDER STATUS ----------------

@app.route("/api/orders/status", methods=["GET"])
@auth_required
def order_status():

    order_id = request.args.get("order_id")

    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    try:
        data = shopify("""
            query GetOrder($id: ID!) {
                order(id: $id) {
                    id
                    name
                    displayFinancialStatus
                    displayFulfillmentStatus
                    cancelledAt
                    totalPriceSet {
                        shopMoney {
                            amount
                            currencyCode
                        }
                    }
                }
            }
        """, {"id": order_id})

        if not data.get("order"):
            return jsonify({"error": "Order not found"}), 404

        return jsonify({
            "success": True,
            "order": data["order"]
        })

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", 10000))
    )
