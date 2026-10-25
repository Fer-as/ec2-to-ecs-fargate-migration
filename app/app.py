"""
Legacy Flask Application
A simple Python Flask API running on EC2 behind Nginx.
This is the "before" state that students will migrate to ECS.

Storage is selected with STORAGE_BACKEND (see storage.py):
  memory   - original in-process storage (default, one process only)
  dynamodb - shared, durable storage for several workers/containers/tasks
"""

import os
import logging
from datetime import datetime
from flask import Flask, jsonify, request
from werkzeug.exceptions import BadRequest, HTTPException

from storage import InsufficientStock, ProductNotFound, StoreBusy, create_store

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# App metadata
APP_NAME = os.getenv('APP_NAME', 'legacy-api')
APP_VERSION = os.getenv('APP_VERSION', '1.0.0')
ENVIRONMENT = os.getenv('ENVIRONMENT', 'production')

# Products, orders and order IDs live in the selected storage backend
store = create_store()
logger.info("Storage backend: %s", type(store).__name__)


def is_integer(value):
    """True for JSON integers. bool is a subclass of int in Python, so exclude it."""
    return isinstance(value, int) and not isinstance(value, bool)


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint (liveness: does not touch storage)"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "service": APP_NAME,
        "version": APP_VERSION,
        "environment": ENVIRONMENT
    }), 200


@app.route('/ready', methods=['GET'])
def ready():
    """Readiness: 200 only if the storage backend is reachable and initialised."""
    try:
        store.check_ready()
    except Exception:
        logger.exception("Readiness check failed")
        return jsonify({"status": "not ready"}), 503
    return jsonify({"status": "ready"}), 200


@app.route('/api/v1/products', methods=['GET'])
def get_products():
    """Get all products"""
    logger.info("GET /api/v1/products")
    products = store.list_products()
    return jsonify({
        "products": products,
        "count": len(products)
    }), 200


@app.route('/api/v1/products/<int:product_id>', methods=['GET'])
def get_product(product_id):
    """Get a specific product"""
    logger.info(f"GET /api/v1/products/{product_id}")
    product = store.get_product(product_id)
    if not product:
        return jsonify({"error": "Product not found"}), 404
    return jsonify(product), 200


@app.route('/api/v1/orders', methods=['GET'])
def get_orders():
    """Get all orders"""
    logger.info("GET /api/v1/orders")
    orders = store.list_orders()
    return jsonify({
        "orders": orders,
        "count": len(orders)
    }), 200


@app.route('/api/v1/orders', methods=['POST'])
def create_order():
    """Create a new order"""
    logger.info("POST /api/v1/orders")

    try:
        data = request.get_json()
        if not data:
            raise BadRequest("No JSON data provided")
        if not isinstance(data, dict):
            raise BadRequest("JSON body must be an object")

        product_id = data.get('product_id')
        quantity = data.get('quantity', 1)

        if not product_id:
            raise BadRequest("product_id is required")
        if not is_integer(product_id):
            raise BadRequest("product_id must be an integer")
        if not is_integer(quantity) or quantity < 1:
            raise BadRequest("quantity must be a positive integer")

        try:
            order = store.create_order(product_id, quantity, datetime.utcnow().isoformat())
        except ProductNotFound:
            return jsonify({"error": "Product not found"}), 404
        except InsufficientStock:
            return jsonify({"error": "Insufficient stock"}), 400
        except StoreBusy:
            logger.warning("Order not created: storage write contention, retries exhausted")
            return jsonify({"error": "Service temporarily unavailable, please retry"}), 503

        logger.info(f"Order created: {order['id']}")
        return jsonify(order), 201

    except HTTPException as e:
        # Client errors such as 400 (bad JSON) and 415 (not JSON) keep their status code.
        return jsonify({"error": str(e)}), e.code
    except Exception:
        logger.exception("Error creating order")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/api/v1/stats', methods=['GET'])
def get_stats():
    """Get application statistics"""
    logger.info("GET /api/v1/stats")
    total_products, total_orders, total_revenue = store.stats()
    return jsonify({
        "total_products": total_products,
        "total_orders": total_orders,
        "total_revenue": total_revenue,
        "timestamp": datetime.utcnow().isoformat()
    }), 200


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(error):
    response = jsonify({"error": "Method not allowed"})
    response.status_code = 405
    if error.valid_methods:
        response.headers["Allow"] = ", ".join(error.valid_methods)
    return response


@app.errorhandler(500)
def internal_error(error):
    logger.error("Internal server error: %r", getattr(error, "original_exception", error))
    return jsonify({"error": "Internal server error"}), 500


if __name__ == '__main__':
    # This is for development only
    # Production uses gunicorn via wsgi.py
    app.run(host='0.0.0.0', port=5000, debug=False)
