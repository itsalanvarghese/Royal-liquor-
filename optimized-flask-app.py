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
from typing import Dict, Tuple, Optional, Any
import threading
from decimal import Decimal
from collections import defaultdict
import aiohttp
import asyncio
from cachetools import TTLCache, LRUCache

# Configure logging with more detailed format
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Use TTLCache for inventory with 1-hour expiration
INVENTORY_CACHE = TTLCache(maxsize=10000, ttl=3600)
PRODUCT_CACHE = LRUCache(maxsize=1000)

class RateLimiter:
    def __init__(self, rate_limit: int, period: int):
        self.rate_limit = rate_limit
        self.period = period
        self.requests = []
        self.lock = threading.Lock()

    def is_allowed(self) -> Tuple[bool, Optional[int]]:
        now = time.time()
        with self.lock:
            # Remove old requests
            self.requests = [req for req in self.requests if now - req < self.period]
            
            if len(self.requests) < self.rate_limit:
                self.requests.append(now)
                return True, None
                
            oldest = self.requests[0]
            wait_time = int(oldest + self.period - now)
            return False, wait_time

# Global rate limiter instance (100 requests per minute)
RATE_LIMITER = RateLimiter(100, 60)

class PriceConverter:
    @staticmethod
    def to_decimal(price_str: str) -> Decimal:
        """Convert price string to Decimal with enhanced error handling"""
        try:
            if not price_str:
                return Decimal('0')
            # Remove currency symbols and whitespace
            cleaned = re.sub(r'[^\d.]', '', price_str)
            return Decimal(cleaned)
        except (ValueError, TypeError) as e:
            logger.error(f"Error converting price {price_str}: {str(e)}")
            return Decimal('0')

    @staticmethod
    def to_string(price: Decimal) -> str:
        """Convert Decimal price to formatted string"""
        return f"${price:.2f}"

class InventoryManager:
    def __init__(self, filename: str):
        self.filename = filename
        self.last_modified = None
        self.load_inventory()

    def load_inventory(self) -> None:
        """Load inventory with file modification checking"""
        try:
            current_mtime = os.path.getmtime(self.filename)
            
            # Only reload if file has been modified
            if self.last_modified != current_mtime:
                with open(self.filename, 'r', encoding='utf-8-sig') as file:
                    reader = csv.DictReader(file)
                    for row in reader:
                        barcode = row['Barcode']
                        INVENTORY_CACHE[barcode] = {
                            'name': row['Name'],
                            'price': PriceConverter.to_decimal(row['Price'])
                        }
                
                self.last_modified = current_mtime
                logger.info(f"Loaded {len(INVENTORY_CACHE)} products from inventory")
                
        except Exception as e:
            logger.error(f"Error loading inventory: {str(e)}")

class UPCValidator:
    @staticmethod
    def validate(barcode: str) -> Tuple[bool, str]:
        """Enhanced UPC validation with checksum verification"""
        if not barcode:
            return False, "Barcode cannot be empty"
            
        cleaned_barcode = re.sub(r'[\s-]', '', barcode)
        
        if not cleaned_barcode.isdigit():
            return False, "Invalid UPC format: must contain only digits"
        
        valid_lengths = {8, 12, 13}  # Using set for O(1) lookup
        if len(cleaned_barcode) not in valid_lengths:
            return False, f"Invalid UPC length: must be {', '.join(map(str, valid_lengths))} digits"
        
        if len(cleaned_barcode) == 12:
            if not UPCValidator._verify_checksum(cleaned_barcode):
                return False, "Invalid UPC: checksum verification failed"
        
        return True, cleaned_barcode

    @staticmethod
    def _verify_checksum(upc: str) -> bool:
        """Verify UPC checksum"""
        try:
            digits = [int(d) for d in upc]
            odd_sum = sum(digits[-2::-2])
            even_sum = sum(digits[-3::-2])
            total = (odd_sum * 3) + even_sum
            checksum = (10 - (total % 10)) % 10
            return checksum == digits[-1]
        except Exception:
            return False

class CartManager:
    def __init__(self):
        self.items: Dict[str, Dict] = defaultdict(dict)
        self.lock = threading.Lock()

    def add_item(self, barcode: str, quantity: int) -> Tuple[bool, str]:
        with self.lock:
            if barcode in self.items:
                self.items[barcode]['quantity'] += quantity
            else:
                product = INVENTORY_CACHE.get(barcode)
                if not product:
                    return False, "Product not found"
                    
                self.items[barcode] = {
                    'name': product['name'],
                    'price': product['price'],
                    'quantity': quantity
                }
            return True, "Item added successfully"

    def update_item(self, barcode: str, quantity: int) -> Tuple[bool, str]:
        with self.lock:
            if barcode not in self.items:
                return False, "Item not found in cart"
                
            if quantity <= 0:
                del self.items[barcode]
            else:
                self.items[barcode]['quantity'] = quantity
            return True, "Cart updated successfully"

    def clear(self) -> None:
        with self.lock:
            self.items.clear()

    def get_total(self) -> Decimal:
        return sum(
            item['price'] * item['quantity']
            for item in self.items.values()
        )

# Initialize managers
inventory_manager = InventoryManager('Inventory_Royal_Liquor.csv')
cart_manager = CartManager()

async def fetch_upc_data(barcode: str) -> Dict:
    """Asynchronous UPC lookup"""
    async with aiohttp.ClientSession() as session:
        try:
            url = f'https://api.upcitemdb.com/prod/trial/lookup?upc={urllib.parse.quote(barcode)}'
            headers = {
                'Authorization': f'Bearer {os.environ.get("UPCITEMDB_API_KEY")}',
                'Content-Type': 'application/json'
            }
            
            async with session.get(url, headers=headers, timeout=5) as response:
                if response.status == 429:
                    retry_after = int(response.headers.get('Retry-After', 5))
                    return {
                        'error': 'rate_limit',
                        'retry_after': retry_after
                    }
                    
                data = await response.json()
                return data
                
        except Exception as e:
            logger.error(f"Error fetching UPC data: {str(e)}")
            return {'error': str(e)}

@app.route('/lookup/<barcode>')
async def lookup_product(barcode: str):
    """Asynchronous product lookup with caching"""
    try:
        # Check cache first
        if barcode in PRODUCT_CACHE:
            return jsonify(PRODUCT_CACHE[barcode])
        
        # Validate UPC
        is_valid, result = UPCValidator.validate(barcode)
        if not is_valid:
            return jsonify({
                'found': False,
                'error': 'invalid_upc',
                'message': result
            }), 400
        
        # Check rate limit
        is_allowed, wait_time = RATE_LIMITER.is_allowed()
        if not is_allowed:
            return jsonify({
                'error': 'rate_limit',
                'message': f'Please wait {wait_time} seconds',
                'retry_after': wait_time
            }), 429
        
        # Check local inventory first
        product = INVENTORY_CACHE.get(result)
        if product:
            response = {
                'found': True,
                'name': product['name'],
                'price': PriceConverter.to_string(product['price']),
                'external': False,
                'barcode': result
            }
            PRODUCT_CACHE[barcode] = response
            return jsonify(response)
        
        # Fetch from external API
        external_data = await fetch_upc_data(result)
        if 'error' in external_data:
            return jsonify(external_data), 429
            
        response = format_external_product(external_data)
        PRODUCT_CACHE[barcode] = response
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"Error in lookup_product: {str(e)}")
        return jsonify({
            'error': 'server_error',
            'message': 'An unexpected error occurred'
        }), 500

def format_external_product(data: Dict) -> Dict:
    """Format external API response"""
    if not data.get('items'):
        return {
            'found': False,
            'message': 'Product not found'
        }
        
    item = data['items'][0]
    name = item.get('title', '')
    
    # Extract size and type using regex
    size_match = re.search(r'\d+(\.\d+)?\s*(ml|ML|L|oz|OZ|cl|CL)', name)
    size = size_match.group(0) if size_match else ""
    
    liquor_types = {'Vodka', 'Whiskey', 'Tequila', 'Rum', 'Gin', 'Brandy', 'Wine', 'Cognac', 'Bourbon'}
    product_type = next(
        (t for t in liquor_types if re.search(rf'\b{t}\b', name, re.IGNORECASE)),
        ""
    )
    
    if product_type:
        name = re.sub(rf'\b{product_type}\b', '', name, flags=re.IGNORECASE).strip()
    if size:
        name = name.replace(size, '').strip()
        
    return {
        'found': True,
        'name': f"✨{' '.join(filter(None, [name, product_type, size]))}",
        'description': item.get('description', ''),
        'upc': item.get('upc'),
        'external': True
    }

# Cart routes with improved error handling and response formatting
@app.route('/cart', methods=['GET'])
def get_cart():
    """Get current cart items with formatted response"""
    try:
        cart_list = [
            {
                'barcode': barcode,
                'name': item['name'],
                'price': PriceConverter.to_string(item['price']),
                'quantity': item['quantity'],
                'subtotal': PriceConverter.to_string(item['price'] * item['quantity'])
            }
            for barcode, item in cart_manager.items.items()
        ]
        
        return jsonify({
            'items': cart_list,
            'total': PriceConverter.to_string(cart_manager.get_total()),
            'count': len(cart_list)
        })
        
    except Exception as e:
        logger.error(f"Error getting cart: {str(e)}")
        return jsonify({'error': 'Failed to retrieve cart'}), 500

@app.route('/cart/add', methods=['POST'])
def add_to_cart():
    """Add item to cart with validation"""
    try:
        data = request.get_json()
        barcode = data.get('barcode')
        quantity = int(data.get('quantity', 1))
        
        if quantity <= 0:
            return jsonify({'error': 'Invalid quantity'}), 400
            
        success, message = cart_manager.add_item(barcode, quantity)
        if not success:
            return jsonify({'error': message}), 404
            
        return jsonify({
            'message': message,
            'cart_count': len(cart_manager.items)
        })
        
    except Exception as e:
        logger.error(f"Error adding to cart: {str(e)}")
        return jsonify({'error': 'Failed to add item to cart'}), 500

if __name__ == '__main__':
    app.run(debug=True)