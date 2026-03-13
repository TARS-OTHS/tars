#!/usr/bin/env python3
"""API server for dashboard - handles messaging and task management."""

import json
import subprocess
import os
import uuid
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone

PORT = int(os.environ.get('API_PORT', '8766'))
UI_PORT = int(os.environ.get('PORT', '8765'))
BIND = os.environ.get('BIND_HOST', '0.0.0.0')
DASHBOARD_DIR = Path(__file__).parent
RESPONSES_DIR = DASHBOARD_DIR / "responses"
TASKS_FILE = DASHBOARD_DIR / "tasks.json"

RESPONSES_DIR.mkdir(exist_ok=True)

class APIHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def do_OPTIONS(self):
        self._send_json({})
    
    def do_GET(self):
        # Health check
        if self.path == '/' or self.path == '/health':
            self._send_json({'ok': True, 'status': 'running'})
            return

        # Check for response
        if self.path.startswith('/response/'):
            msg_id = self.path.split('/')[-1].split('?')[0]
            response_file = RESPONSES_DIR / f"{msg_id}.json"
            
            if response_file.exists():
                try:
                    with open(response_file) as f:
                        data = json.load(f)
                    self._send_json({'ok': True, 'ready': True, **data})
                except:
                    self._send_json({'ok': True, 'ready': False})
            else:
                self._send_json({'ok': True, 'ready': False})
        
        # Get tasks
        elif self.path == '/tasks':
            try:
                if TASKS_FILE.exists():
                    with open(TASKS_FILE) as f:
                        tasks = json.load(f)
                else:
                    tasks = {"pending": [], "in_progress": [], "completed": []}
                self._send_json({'ok': True, 'tasks': tasks})
            except Exception as e:
                self._send_json({'ok': False, 'error': str(e)}, 500)
        
        # Ops alerts - read
        elif self.path == '/ops-alerts':
            try:
                alerts_file = DASHBOARD_DIR / "alerts.json"
                alerts = []
                if alerts_file.exists():
                    with open(alerts_file) as f:
                        alerts = json.load(f)
                self._send_json({'ok': True, 'alerts': alerts[-50:]})  # last 50
            except Exception as e:
                self._send_json({'ok': True, 'alerts': [], 'error': str(e)})

        # System resource usage
        elif self.path == '/system-stats':
            try:
                stats = {}
                # CPU load
                with open('/proc/loadavg') as f:
                    parts = f.read().split()
                    stats['load'] = {'1m': parts[0], '5m': parts[1], '15m': parts[2]}
                # Memory
                meminfo = {}
                with open('/proc/meminfo') as f:
                    for line in f:
                        parts = line.split(':')
                        if len(parts) == 2:
                            key = parts[0].strip()
                            val = parts[1].strip().split()[0]
                            meminfo[key] = int(val)
                total = meminfo.get('MemTotal', 1)
                avail = meminfo.get('MemAvailable', 0)
                stats['memory'] = {
                    'total_mb': round(total / 1024),
                    'used_mb': round((total - avail) / 1024),
                    'available_mb': round(avail / 1024),
                    'percent': round((total - avail) / total * 100, 1)
                }
                # Disk
                st = os.statvfs('/')
                disk_total = st.f_blocks * st.f_frsize
                disk_free = st.f_bavail * st.f_frsize
                disk_used = disk_total - disk_free
                stats['disk'] = {
                    'total_gb': round(disk_total / (1024**3), 1),
                    'used_gb': round(disk_used / (1024**3), 1),
                    'free_gb': round(disk_free / (1024**3), 1),
                    'percent': round(disk_used / disk_total * 100, 1)
                }
                # Uptime
                with open('/proc/uptime') as f:
                    uptime_secs = float(f.read().split()[0])
                    days = int(uptime_secs // 86400)
                    hours = int((uptime_secs % 86400) // 3600)
                    stats['uptime'] = f"{days}d {hours}h"
                self._send_json({'ok': True, 'stats': stats})
            except Exception as e:
                self._send_json({'ok': False, 'error': str(e)}, 500)

        # Proxy memory health from agent-services
        elif self.path == '/memory-health':
            try:
                import urllib.request
                req = urllib.request.urlopen(os.environ.get('MEMORY_API_URL', 'http://172.17.0.1:8897') + '/status', timeout=3)
                data = json.loads(req.read())
                self._send_json({
                    'ok': True,
                    'memory': {
                        'stats': data.get('memory_stats', {}),
                        'tree_regen': data.get('tree_regen', {}),
                        'session_states': data.get('session_states', []),
                        'services': data.get('services', {}),
                    }
                })
            except Exception as e:
                self._send_json({'ok': True, 'memory': {'error': f'Memory API unavailable: {e}'}})
        
        else:
            self._send_json({'ok': False, 'error': 'Not found'}, 404)
    
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        
        # Send message to agent
        if self.path == '/send':
            try:
                agent = body.get('agent', 'main')
                message = body.get('message', '')
                
                if not message:
                    self._send_json({'ok': False, 'error': 'No message'}, 400)
                    return
                
                # Generate message ID
                msg_id = str(uuid.uuid4())[:8]
                
                # Run agent message in background thread
                def run_agent():
                    try:
                        result = subprocess.run(
                            [os.environ.get('OPENCLAW_BIN', '/usr/local/bin/openclaw'), 'agent', '--agent', agent, '--session-id', f'dashboard-{agent}', '--message', message],
                            capture_output=True,
                            text=True,
                            timeout=120,
                            env={**os.environ, 'PATH': os.environ.get('NPM_GLOBAL_BIN', '/usr/local/bin') + ':' + os.environ.get('PATH', '')}
                        )
                        
                        response_data = {
                            'agent': agent,
                            'message': message,
                            'response': result.stdout if result.returncode == 0 else result.stderr,
                            'success': result.returncode == 0,
                            'timestamp': datetime.now(timezone.utc).isoformat()
                        }
                        
                        with open(RESPONSES_DIR / f"{msg_id}.json", 'w') as f:
                            json.dump(response_data, f)
                            
                    except Exception as e:
                        with open(RESPONSES_DIR / f"{msg_id}.json", 'w') as f:
                            json.dump({
                                'agent': agent,
                                'message': message,
                                'response': f'Error: {e}',
                                'success': False,
                                'timestamp': datetime.now(timezone.utc).isoformat()
                            }, f)
                
                thread = threading.Thread(target=run_agent)
                thread.start()
                
                self._send_json({'ok': True, 'messageId': msg_id, 'agent': agent})
                
            except Exception as e:
                self._send_json({'ok': False, 'error': str(e)}, 500)
        
        # Add task
        elif self.path == '/tasks/add':
            try:
                title = body.get('title', '')
                agent = body.get('agent')
                status = body.get('status', 'pending')
                
                if not title:
                    self._send_json({'ok': False, 'error': 'No title'}, 400)
                    return
                
                tasks = {"pending": [], "in_progress": [], "completed": []}
                if TASKS_FILE.exists():
                    with open(TASKS_FILE) as f:
                        tasks = json.load(f)
                
                task = {
                    "id": str(uuid.uuid4())[:8],
                    "title": title,
                    "agent": agent,
                    "created": datetime.now(timezone.utc).isoformat()
                }
                
                if status in tasks:
                    tasks[status].append(task)
                else:
                    tasks["pending"].append(task)
                
                with open(TASKS_FILE, 'w') as f:
                    json.dump(tasks, f, indent=2)
                
                self._send_json({'ok': True, 'task': task})
                
            except Exception as e:
                self._send_json({'ok': False, 'error': str(e)}, 500)
        
        # Update task status
        elif self.path == '/tasks/update':
            try:
                task_id = body.get('id')
                new_status = body.get('status')
                
                if not task_id or not new_status:
                    self._send_json({'ok': False, 'error': 'Missing id or status'}, 400)
                    return
                
                tasks = {"pending": [], "in_progress": [], "completed": []}
                if TASKS_FILE.exists():
                    with open(TASKS_FILE) as f:
                        tasks = json.load(f)
                
                # Find and move task
                task = None
                for status_list in ['pending', 'in_progress', 'completed']:
                    for t in tasks[status_list]:
                        if t.get('id') == task_id:
                            task = t
                            tasks[status_list].remove(t)
                            break
                    if task:
                        break
                
                if task and new_status in tasks:
                    tasks[new_status].append(task)
                    with open(TASKS_FILE, 'w') as f:
                        json.dump(tasks, f, indent=2)
                    self._send_json({'ok': True, 'task': task})
                else:
                    self._send_json({'ok': False, 'error': 'Task not found'}, 404)
                    
            except Exception as e:
                self._send_json({'ok': False, 'error': str(e)}, 500)
        
        # Ops alerts - write
        elif self.path == '/ops-alerts':
            try:
                alerts_file = DASHBOARD_DIR / "alerts.json"
                alerts = []
                if alerts_file.exists():
                    with open(alerts_file) as f:
                        alerts = json.load(f)
                
                alert = {
                    'id': str(uuid.uuid4())[:8],
                    'level': body.get('level', 'info'),  # info, warning, error
                    'source': body.get('source', 'unknown'),
                    'message': body.get('message', ''),
                    'timestamp': body.get('timestamp', datetime.now(timezone.utc).isoformat())
                }
                alerts.append(alert)
                
                # Keep last 200
                if len(alerts) > 200:
                    alerts = alerts[-200:]
                
                with open(alerts_file, 'w') as f:
                    json.dump(alerts, f, indent=2)
                
                self._send_json({'ok': True, 'alert': alert})
            except Exception as e:
                self._send_json({'ok': False, 'error': str(e)}, 500)
        
        else:
            self._send_json({'ok': False, 'error': 'Not found'}, 404)
    
    def log_message(self, format, *args):
        print(f"[API] {args[0]}")

class UIHandler(BaseHTTPRequestHandler):
    """Serves the dashboard UI (index.html and static files) on the UI port."""

    def _guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {
            '.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript',
            '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png',
            '.jpg': 'image/jpeg', '.ico': 'image/x-icon', '.woff2': 'font/woff2',
        }.get(ext, 'application/octet-stream')

    def do_GET(self):
        # Map URL path to file
        req_path = self.path.split('?')[0].split('#')[0]
        if req_path == '/' or req_path == '':
            file_path = DASHBOARD_DIR / 'index.html'
        else:
            file_path = DASHBOARD_DIR / req_path.lstrip('/')

        # Security: prevent directory traversal
        try:
            file_path = file_path.resolve()
            if not str(file_path).startswith(str(DASHBOARD_DIR.resolve())):
                self.send_error(403)
                return
        except Exception:
            self.send_error(400)
            return

        if file_path.is_file():
            content = file_path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', self._guess_type(str(file_path)))
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        else:
            # SPA fallback: serve index.html for unknown routes
            index = DASHBOARD_DIR / 'index.html'
            if index.is_file():
                content = index.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)

    def log_message(self, format, *args):
        print(f"[UI] {args[0]}")


def main():
    api_server = HTTPServer((BIND, PORT), APIHandler)
    ui_server = HTTPServer((BIND, UI_PORT), UIHandler)

    # Run API server in a background thread
    api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
    api_thread.start()
    print(f"API server running on http://{BIND}:{PORT}")

    # UI server runs in main thread
    print(f"UI server running on http://{BIND}:{UI_PORT}")
    ui_server.serve_forever()

if __name__ == '__main__':
    main()
