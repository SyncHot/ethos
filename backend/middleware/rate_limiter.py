import time
from flask import request, jsonify, current_app
from threading import Lock

class RateLimiter:
    def __init__(self, app=None, limit=300, window=60):
        self.requests = {}
        self.limit = limit
        self.window = window
        self.lock = Lock()
        if app:
            self.init_app(app)

    def init_app(self, app):
        app.before_request(self.check_limit)

    def check_limit(self):
        # Exclude local requests
        if request.remote_addr in ('127.0.0.1', '::1'):
            return

        ip = request.remote_addr
        now = time.time()
        
        with self.lock:
            # Lazy cleanup for this IP
            if ip in self.requests:
                self.requests[ip] = [t for t in self.requests[ip] if t > now - self.window]
            else:
                self.requests[ip] = []
            
            # Check limit
            if len(self.requests[ip]) >= self.limit:
                current_app.logger.warning(f"Rate limit exceeded for IP: {ip}")
                return jsonify({
                    'error': 'Too Many Requests', 
                    'message': 'Rate limit exceeded. Please try again later.'
                }), 429
            
            self.requests[ip].append(now)

            # Periodic cleanup (1% chance or if dict grows too large)
            if len(self.requests) > 1000:
                 self._cleanup(now)

    def _cleanup(self, now):
        keys_to_delete = []
        for ip, times in self.requests.items():
            valid_times = [t for t in times if t > now - self.window]
            if not valid_times:
                keys_to_delete.append(ip)
            else:
                self.requests[ip] = valid_times
        
        for key in keys_to_delete:
            del self.requests[key]
