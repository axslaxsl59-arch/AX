from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
import json, re, os, time, threading
from html import unescape

PAGE = Path(__file__).resolve().parent / 'index.html'
RATE_LIMIT_LOCK = threading.Lock()
NEXT_CHECK_TIME = 0.0
ENDPOINT = 'https://discord.com/api/v9/unique-username/username-attempt-unauthed'

def check_social_profile(platform, username):
    """Best-effort public-profile lookup; never equate a missing page with claimability."""
    url = (f'https://www.tiktok.com/@{username}' if platform == 'tiktok'
           else f'https://www.instagram.com/{username}/')
    req = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    })
    try:
        response = urlopen(req, timeout=15)
        status = response.status
        page = response.read(2_000_000).decode('utf-8', 'replace')
    except HTTPError as err:
        status = err.code
        page = err.read(500_000).decode('utf-8', 'replace')
    except (URLError, TimeoutError, OSError):
        return 'unknown'

    if status in (403, 429) or status >= 500:
        return 'unknown'
    if status == 404:
        return 'not_found'
    if platform == 'tiktok':
        match = re.search(r'<script[^>]+id=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>', page, re.S)
        if match:
            try:
                data = json.loads(unescape(match.group(1)))
                state = data.get('__DEFAULT_SCOPE__', {}).get('webapp.user-detail', {})
                user = state.get('userInfo', {}).get('user', {})
                if isinstance(user, dict) and user.get('uniqueId', '').lower() == username.lower():
                    return 'found'
                if state.get('statusCode') in (10202, 10221, '10202', '10221'):
                    return 'not_found'
            except (ValueError, TypeError, AttributeError):
                pass
        return 'unknown'

    escaped = re.escape(username)
    profile_markers = (
        rf'"username"\s*:\s*"{escaped}".{{0,600}}?"(?:is_private|profile_pic_url|full_name)"',
        rf'"(?:is_private|profile_pic_url|full_name)".{{0,600}}?"username"\s*:\s*"{escaped}"'
    )
    if any(re.search(pattern, page, re.I | re.S) for pattern in profile_markers):
        return 'found'
    if status == 200 and (('Login • Instagram' in page) or ('accounts/login' in page) or ('challenge' in page.lower())):
        return 'unknown'
    return 'unknown'

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, value):
        data = json.dumps(value).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        assets = {
            '/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json; charset=utf-8'),
            '/service-worker.js': ('service-worker.js', 'application/javascript; charset=utf-8'),
            '/batman-face.svg': ('batman-face.svg', 'image/svg+xml; charset=utf-8'),
            '/night-knight.png': ('night-knight.png', 'image/png'),
            '/night-knight-home-preview.html': ('night-knight-home-preview.html', 'text/html; charset=utf-8')
        }
        if self.path in assets:
            filename, content_type = assets[self.path]
            try:
                data = (Path(__file__).resolve().parent / filename).read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-cache' if filename == 'service-worker.js' else 'public, max-age=3600')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path not in ('/', '/index.html'):
            self.send_error(404)
            return
        try:
            data = PAGE.read_bytes()
        except OSError:
            self.send_error(500, 'Website file not found')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if self.path not in ('/api/check', '/api/check-social'):
            self.send_error(404)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > 2048:
                self.send_json(400, {'error': 'invalid_request'})
                return
            body = json.loads(self.rfile.read(length))
            username = body.get('username', '')
            if self.path == '/api/check-social':
                platform = body.get('platform')
                if platform not in ('tiktok', 'instagram') or not isinstance(username, str) or not re.fullmatch(r'[a-z0-9._]{3,24}', username) or username.startswith('.') or username.endswith('.') or '..' in username:
                    self.send_json(400, {'error': 'invalid_username'})
                    return
                global NEXT_CHECK_TIME
                with RATE_LIMIT_LOCK:
                    now = time.monotonic()
                    if now < NEXT_CHECK_TIME:
                        self.send_json(429, {'error': 'local_rate_limit', 'retry_after': NEXT_CHECK_TIME - now})
                        return
                    NEXT_CHECK_TIME = now + 5.0
                state = check_social_profile(platform, username)
                self.send_json(200, {'state': state})
                return
            if not isinstance(username, str) or not re.fullmatch(r'[a-z0-9_.]{2,32}', username) or '..' in username:
                self.send_json(400, {'error': 'invalid_username'})
                return
            with RATE_LIMIT_LOCK:
                now = time.monotonic()
                if now < NEXT_CHECK_TIME:
                    self.send_json(429, {'error': 'local_rate_limit', 'retry_after': NEXT_CHECK_TIME - now})
                    return
                NEXT_CHECK_TIME = now + 5.0
            req = Request(ENDPOINT, data=json.dumps({'username': username}).encode(), headers={
                'Content-Type': 'application/json',
                'User-Agent': 'Mozilla/5.0 DiscordUsernameAvailabilityPage/1.0'
            }, method='POST')
            with urlopen(req, timeout=12) as response:
                result = json.loads(response.read())
            if not isinstance(result, dict) or not isinstance(result.get('taken'), bool):
                self.send_json(502, {'error': 'unexpected_discord_response'})
                return
            self.send_json(200, {'taken': result['taken']})
        except HTTPError as err:
            self.send_json(429 if err.code == 429 else 502, {'error': 'discord_rejected_request', 'upstream_status': err.code})
        except (URLError, TimeoutError):
            self.send_json(502, {'error': 'discord_unreachable'})
        except (json.JSONDecodeError, ValueError, TypeError):
            self.send_json(400, {'error': 'invalid_request'})
        except Exception:
            self.send_json(500, {'error': 'check_failed'})

    def log_message(self, fmt, *args):
        pass

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8765'))
    host = '0.0.0.0' if 'PORT' in os.environ else '127.0.0.1'
    ThreadingHTTPServer((host, port), Handler).serve_forever()



