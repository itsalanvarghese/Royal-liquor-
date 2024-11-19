from flask import Flask, render_template, jsonify, request
import csv
import os
import requests
import time
import urllib.parse
import re
from datetime import datetime, timedelta
from functools import lru_cache
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Load inventory data from CSV
def load_inventory():
    try:
        inventory = {}
        with open('Inventory_Royal_Liquor.csv', 'r', encoding='utf-8-sig') as file:
            reader = csv.DictReader(file)
            for row in reader:
                inventory[row['Barcode']] = {
                    'name': row['Name'],
                    'price': row['Price']
                }
        logger.info(f"Loaded {len(inventory)} products from inventory")
        return inventory
    except Exception as e:
        logger.error(f"Error loading inventory: {str(e)}")
        return {}

INVENTORY = load_inventory()

# Purchase order storage
PURCHASE_ORDERS = {}
CART_ITEMS = {}

# Enhanced rate limiting configuration
RATE_LIMIT = {
    'last_request': datetime.min,
    'cooldown': timedelta(seconds=2),
    'retry_after': 0,
    'max_retries': 3,
    'requests_remaining': 100,
    'reset_time': None,
    'error_count': 0,
    'max_errors': 5,
    'error_reset': timedelta(minutes=5)
}

def validate_upc(barcode):
    """Enhanced UPC validation with detailed error messages"""
    if not barcode:
        return False, "Barcode cannot be empty"
        
    # Remove any hyphens or spaces
    cleaned_barcode = re.sub(r'[\s-]', '', barcode)
    
    # Check if the cleaned barcode contains only digits
    if not cleaned_barcode.isdigit():
        return False, "Invalid UPC format: must contain only digits"
    
    # Check length (UPC-A is 12 digits, UPC-E is 8 digits, EAN-13 is 13 digits)
    valid_lengths = [8, 12, 13]
    if len(cleaned_barcode) not in valid_lengths:
        return False, f"Invalid UPC length: must be {', '.join(map(str, valid_lengths))} digits"
    
    # Check checksum for UPC-A (12 digits)
    if len(cleaned_barcode) == 12:
        try:
            digits = [int(d) for d in cleaned_barcode]
            checksum = (10 - ((3 * sum(digits[-2::-2]) + sum(digits[-3::-2])) % 10)) % 10
            if checksum != digits[-1]:
                return False, "Invalid UPC: checksum verification failed"
        except Exception as e:
            logger.error(f"Error validating UPC checksum: {str(e)}")
            return False, "Error validating UPC checksum"
    
    return True, cleaned_barcode

def format_product_name(name, product_type="", size=""):
    """Format product name with additional details"""
    try:
        parts = []
        if name:
            parts.append(name.strip())
        if product_type:
            parts.append(product_type.strip())
        if size:
            parts.append(f"- {size.strip()}")
        return f"✨{' '.join(parts)}" if parts else ""
    except Exception as e:
        logger.error(f"Error formatting product name: {str(e)}")
        return name if name else ""

def parse_price(price_str):
    """Convert price string to float with enhanced error handling"""
    try:
        if not price_str:
            return 0.0
        return float(price_str.replace('$', '').replace(',', ''))
    except (ValueError, AttributeError) as e:
        logger.error(f"Error parsing price {price_str}: {str(e)}")
        return 0.0

def check_rate_limit():
    """Enhanced rate limit checking with error count tracking"""
    current_time = datetime.now()
    
    # Reset error count if enough time has passed
    if current_time - RATE_LIMIT['last_request'] > RATE_LIMIT['error_reset']:
        RATE_LIMIT['error_count'] = 0
    
    # Check if too many errors have occurred
    if RATE_LIMIT['error_count'] >= RATE_LIMIT['max_errors']:
        wait_time = (RATE_LIMIT['last_request'] + RATE_LIMIT['error_reset'] - current_time).total_seconds()
        if wait_time > 0:
            return False, {
                'error': 'rate_limit',
                'message': f'Too many errors. Please wait {int(wait_time)} seconds.',
                'retry_after': int(wait_time)
            }
    
    # Check standard rate limiting
    time_since_last = current_time - RATE_LIMIT['last_request']
    if time_since_last < RATE_LIMIT['cooldown']:
        remaining = (RATE_LIMIT['cooldown'] - time_since_last).total_seconds()
        return False, {
            'error': 'rate_limit',
            'message': f'Please wait {int(remaining)} seconds before trying again',
            'retry_after': int(remaining)
        }
    
    if RATE_LIMIT['requests_remaining'] <= 0 and RATE_LIMIT['reset_time']:
        wait_time = (RATE_LIMIT['reset_time'] - current_time).total_seconds()
        if wait_time > 0:
            return False, {
                'error': 'rate_limit',
                'message': f'API rate limit exceeded. Please wait {int(wait_time)} seconds.',
                'retry_after': int(wait_time)
            }
    
    return True, None

@app.route('/lookup/<barcode>')
def lookup_product(barcode):
    """Enhanced product lookup with better error handling"""
    try:
        # Validate UPC format
        is_valid, validation_result = validate_upc(barcode)
        if not is_valid:
            return jsonify({
                'found': False,
                'error': 'invalid_upc',
                'message': validation_result
            }), 400
        
        # Check rate limiting
        is_allowed, rate_limit_response = check_rate_limit()
        if not is_allowed:
            return jsonify(rate_limit_response), 429
        
        # First check local inventory
        product = INVENTORY.get(validation_result)
        if product:
            return jsonify({
                'found': True,
                'name': product['name'],
                'price': product['price'],
                'external': False,
                'barcode': validation_result
            })
        
        # If not found locally, try UPCItemDB with caching
        external_result = cached_upcitemdb_search(validation_result)
        
        if external_result:
            if 'error' in external_result:
                status_code = 429 if external_result['error'] == 'rate_limit' else 400
                return jsonify(external_result), status_code
            return jsonify(external_result)
        
        return jsonify({
            'found': False,
            'message': 'Product not found in local inventory or external database'
        }), 404
        
    except Exception as e:
        logger.error(f"Error in lookup_product: {str(e)}")
        RATE_LIMIT['error_count'] += 1
        return jsonify({
            'error': 'server_error',
            'message': 'An unexpected error occurred'
        }), 500

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/cart', methods=['GET'])
def get_cart():
    """Get current cart items"""
    cart_list = []
    total = 0.0
    
    for barcode, item in CART_ITEMS.items():
        price = parse_price(item['price'])
        subtotal = price * item['quantity']
        total += subtotal
        
        cart_list.append({
            'barcode': barcode,
            'name': item['name'],
            'price': item['price'],
            'quantity': item['quantity'],
            'subtotal': f"${subtotal:.2f}"
        })
    
    return jsonify({
        'items': cart_list,
        'total': f"${total:.2f}",
        'count': len(cart_list)
    })

@app.route('/cart/add', methods=['POST'])
def add_to_cart():
    """Add item to cart"""
    data = request.get_json()
    barcode = data.get('barcode')
    quantity = int(data.get('quantity', 1))
    
    if barcode in CART_ITEMS:
        CART_ITEMS[barcode]['quantity'] += quantity
    else:
        product = INVENTORY.get(barcode, {})
        if not product:
            return jsonify({'error': 'Product not found'}), 404
            
        CART_ITEMS[barcode] = {
            'name': product['name'],
            'price': product['price'],
            'quantity': quantity
        }
    
    return jsonify({'message': 'Item added to cart', 'cart_count': len(CART_ITEMS)})

@app.route('/cart/update', methods=['PUT'])
def update_cart_item():
    """Update cart item quantity"""
    data = request.get_json()
    barcode = data.get('barcode')
    quantity = int(data.get('quantity', 0))
    
    if barcode not in CART_ITEMS:
        return jsonify({'error': 'Item not found in cart'}), 404
        
    if quantity <= 0:
        del CART_ITEMS[barcode]
    else:
        CART_ITEMS[barcode]['quantity'] = quantity
    
    return jsonify({'message': 'Cart updated', 'cart_count': len(CART_ITEMS)})

@app.route('/cart/clear', methods=['POST'])
def clear_cart():
    """Clear all items from cart"""
    CART_ITEMS.clear()
    return jsonify({'message': 'Cart cleared'})

@app.route('/order/create', methods=['POST'])
def create_order():
    """Create a purchase order from cart items"""
    if not CART_ITEMS:
        return jsonify({'error': 'Cart is empty'}), 400
        
    order_number = generate_order_number()
    total = sum(parse_price(item['price']) * item['quantity'] for item in CART_ITEMS.values())
    
    order = {
        'order_number': order_number,
        'date': datetime.now().isoformat(),
        'items': [{
            'barcode': barcode,
            'name': item['name'],
            'price': item['price'],
            'quantity': item['quantity'],
            'subtotal': f"${parse_price(item['price']) * item['quantity']:.2f}"
        } for barcode, item in CART_ITEMS.items()],
        'total': f"${total:.2f}"
    }
    
    PURCHASE_ORDERS[order_number] = order
    CART_ITEMS.clear()
    
    return jsonify({
        'message': 'Order created successfully',
        'order': order
    })

@app.route('/order/<order_number>', methods=['GET'])
def get_order(order_number):
    """Get order details by order number"""
    order = PURCHASE_ORDERS.get(order_number)
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    return jsonify(order)

@lru_cache(maxsize=1000)
def cached_upcitemdb_search(barcode):
    """Cache UPCItemDB responses to reduce API calls"""
    return _search_upcitemdb(barcode)

def update_rate_limits(headers):
    """Update rate limit information from response headers"""
    RATE_LIMIT['requests_remaining'] = int(headers.get('x-ratelimit-remaining', 100))
    if 'x-ratelimit-reset' in headers:
        RATE_LIMIT['reset_time'] = datetime.fromtimestamp(int(headers['x-ratelimit-reset']))

def _search_upcitemdb(barcode):
    """Internal function to handle UPCItemDB API calls with improved error handling"""
    # Validate UPC format
    is_valid, result = validate_upc(barcode)
    if not is_valid:
        return {
            'error': 'invalid_upc',
            'message': result
        }
    
    # Check rate limiting
    current_time = datetime.now()
    time_since_last = current_time - RATE_LIMIT['last_request']
    
    if time_since_last < RATE_LIMIT['cooldown']:
        remaining = (RATE_LIMIT['cooldown'] - time_since_last).total_seconds()
        return {
            'error': 'rate_limit',
            'message': f'Please wait {int(remaining)} seconds before trying again',
            'retry_after': int(remaining)
        }

    if RATE_LIMIT['requests_remaining'] <= 0 and RATE_LIMIT['reset_time']:
        wait_time = (RATE_LIMIT['reset_time'] - current_time).total_seconds()
        if wait_time > 0:
            return {
                'error': 'rate_limit',
                'message': f'API rate limit exceeded. Please wait {int(wait_time)} seconds.',
                'retry_after': int(wait_time)
            }

    retries = 0
    while retries < RATE_LIMIT['max_retries']:
        try:
            # Properly encode the barcode parameter
            encoded_barcode = urllib.parse.quote(result)
            url = f'https://api.upcitemdb.com/prod/trial/lookup?upc={encoded_barcode}'
            
            headers = {
                'Authorization': f'Bearer {os.environ.get("UPCITEMDB_API_KEY")}',
                'Content-Type': 'application/json'
            }
            
            response = requests.get(
                url,
                headers=headers,
                timeout=5  # 5 seconds timeout
            )
            
            # Update rate limit information
            update_rate_limits(response.headers)
            RATE_LIMIT['last_request'] = current_time
            
            if response.status_code == 429:  # Too Many Requests
                retry_after = int(response.headers.get('Retry-After', 5))
                RATE_LIMIT['cooldown'] = timedelta(seconds=retry_after)
                return {
                    'error': 'rate_limit',
                    'message': f'Rate limit exceeded. Please wait {retry_after} seconds.',
                    'retry_after': retry_after
                }
            
            if response.status_code == 400:  # Bad Request
                return {
                    'error': 'invalid_query',
                    'message': 'Invalid UPC code or query format'
                }
            
            response.raise_for_status()
            data = response.json()
            
            if not data.get('items'):
                return {
                    'found': False,
                    'message': 'No product found for this UPC code'
                }
            
            item = data['items'][0]
            
            # Extract size and type
            size = ""
            name = item.get('title', '')
            product_type = ""
            
            # Improved size detection
            size_match = re.search(r'\d+(\.\d+)?\s*(ml|ML|L|oz|OZ|cl|CL)', name)
            if size_match:
                size = size_match.group(0)
                name = name.replace(size, '').strip()
            
            # Enhanced product type detection
            liquor_types = ['Vodka', 'Whiskey', 'Tequila', 'Rum', 'Gin', 'Brandy', 'Wine', 'Cognac', 'Bourbon']
            for ltype in liquor_types:
                if re.search(rf'\b{ltype}\b', name, re.IGNORECASE):
                    product_type = ltype
                    name = re.sub(rf'\b{ltype}\b', '', name, flags=re.IGNORECASE).strip()
                    break
            
            formatted_name = format_product_name(name, product_type, size)
            
            return {
                'found': True,
                'name': formatted_name,
                'description': item.get('description', ''),
                'upc': item.get('upc', barcode),
                'external': True
            }
            
        except requests.exceptions.Timeout:
            retries += 1
            if retries < RATE_LIMIT['max_retries']:
                wait_time = 2 ** retries
                time.sleep(wait_time)
            else:
                return {
                    'error': 'timeout',
                    'message': 'Request timed out after multiple attempts'
                }
                
        except requests.exceptions.RequestException as e:
            retries += 1
            if retries < RATE_LIMIT['max_retries']:
                wait_time = 2 ** retries
                time.sleep(wait_time)
            else:
                return {
                    'error': 'api_error',
                    'message': f'API Error: {str(e)}'
                }
    
    return {
        'error': 'max_retries',
        'message': 'Maximum retry attempts reached'
    }

def generate_order_number():
    """Generate a unique order number"""
    timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
    return f"PO-{timestamp}"

if __name__ == '__main__':
    app.run(debug=True)