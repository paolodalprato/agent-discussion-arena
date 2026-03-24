#!/usr/bin/env python3
"""
Agent Discussion Arena — Local Proxy Server
Routes AI API calls to Anthropic, OpenAI, or OpenAI-compatible providers.
Uses only Python standard library. Binds to 127.0.0.1 only.
"""

import json
import os
import signal
import ssl
import sys
import threading
import urllib.request
import urllib.error
import urllib.parse
from http.server import SimpleHTTPRequestHandler, HTTPServer

# ============ SSL CONTEXT ============
# Windows Python sometimes fails certificate verification.
# Try system certs first, fall back to certifi, then unverified as last resort.

def _build_ssl_context():
    ctx = ssl.create_default_context()
    try:
        # Test if default context works by checking it loads CA certs
        if ctx.get_ca_certs():
            return ctx
    except Exception:
        pass
    # Try certifi bundle if installed
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return ctx
    except ImportError:
        pass
    # Fallback: use system store explicitly (Windows)
    try:
        import _ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_default_certs()
        return ctx
    except Exception:
        pass
    # Last resort: return default context (may still work)
    return ssl.create_default_context()

SSL_CTX = _build_ssl_context()
_https_handler = urllib.request.HTTPSHandler(context=SSL_CTX)
URL_OPENER = urllib.request.build_opener(_https_handler)

DEFAULT_PORT = 8080

# ============ FALLBACK MODEL LISTS ============
# Used only when the API is unreachable or no API key is provided yet.

OPENAI_MODELS_FALLBACK = [
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
]


# ============ CORS HELPERS ============

def add_cors_headers(handler):
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")


def send_json(handler, data, status=200):
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    add_cors_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def send_error(handler, message, status=500):
    send_json(handler, {"error": message}, status)


# ============ PROVIDER ROUTING ============

def call_anthropic(api_key, model, system_prompt, messages):
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 2048,
        "system": system_prompt,
        "messages": messages,
    }
    return _do_request(url, headers, payload, provider="anthropic")


def call_openai(api_key, model, system_prompt, messages, base_url=None):
    url = (base_url.rstrip("/") if base_url else "https://api.openai.com") + "/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    all_messages = [{"role": "system", "content": system_prompt}] + messages
    payload = {
        "model": model,
        "max_tokens": 2048,
        "messages": all_messages,
    }
    return _do_request(url, headers, payload, provider="openai")


def _do_request(url, headers, payload, provider):
    sys.stderr.write(f"[proxy] → Calling {provider}: {url}\n")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with URL_OPENER.open(req, timeout=1200) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            msg = err_json.get("error", {}).get("message", err_body) if isinstance(err_json.get("error"), dict) else str(err_json.get("error", err_body))
        except Exception:
            msg = err_body
        sys.stderr.write(f"[proxy] {provider} API error ({e.code}): {msg}\n")
        raise RuntimeError(f"{provider} API error ({e.code}): {msg}")
    except urllib.error.URLError as e:
        sys.stderr.write(f"[proxy] Network error reaching {provider}: {e.reason}\n")
        raise RuntimeError(f"Network error reaching {provider}: {e.reason}")
    except Exception as e:
        sys.stderr.write(f"[proxy] Unexpected error calling {provider}: {type(e).__name__}: {e}\n")
        raise RuntimeError(f"Unexpected error calling {provider}: {e}")

    if provider == "anthropic":
        text = ""
        for block in body.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        usage = body.get("usage", {})
        return {
            "text": text,
            "usage": {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            },
        }
    else:
        choices = body.get("choices", [])
        text = choices[0]["message"]["content"] if choices else ""
        usage = body.get("usage", {})
        return {
            "text": text,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }


# ============ FETCH MODELS ============

def fetch_models(provider, api_key=None, base_url=None):
    if provider == "anthropic":
        if not api_key:
            return []  # Need API key to list Anthropic models
        url = "https://api.anthropic.com/v1/models"
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with URL_OPENER.open(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            models = sorted([m["id"] for m in body.get("data", [])])
            return models if models else []
        except Exception:
            return []

    if provider == "openai" and not api_key:
        return OPENAI_MODELS_FALLBACK

    url = (base_url.rstrip("/") if base_url else "https://api.openai.com") + "/v1/models"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with URL_OPENER.open(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        models = sorted([m["id"] for m in body.get("data", [])])
        return models if models else OPENAI_MODELS_FALLBACK
    except Exception:
        return OPENAI_MODELS_FALLBACK if provider == "openai" else []


# ============ REQUEST HANDLER ============

class ProxyHandler(SimpleHTTPRequestHandler):

    def _is_loopback(self):
        """Reject connections not originating from loopback."""
        client_ip = self.client_address[0]
        return client_ip in ("127.0.0.1", "::1")

    def _reject_non_local(self):
        if not self._is_loopback():
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden: only local connections allowed")
            return True
        return False

    def log_message(self, format, *args):
        # Suppress default logging to avoid leaking API keys from query strings
        msg = format % args
        if "api_key" in msg:
            msg = msg.split("api_key")[0] + "api_key=***"
        sys.stderr.write(f"[proxy] {msg}\n")

    def do_OPTIONS(self):
        self.send_response(204)
        add_cors_headers(self)
        self.end_headers()

    def do_GET(self):
        if self._reject_non_local():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            send_json(self, {"status": "ok", "server": "agent-discussion-arena-proxy"})
            return

        if path == "/api/models":
            params = urllib.parse.parse_qs(parsed.query)
            provider = params.get("provider", [""])[0]
            api_key = params.get("api_key", [""])[0]
            base_url = params.get("base_url", [""])[0]
            if not provider:
                send_error(self, "Missing provider parameter", 400)
                return
            try:
                models = fetch_models(provider, api_key, base_url)
                send_json(self, {"models": models})
            except Exception as e:
                send_error(self, str(e))
            return

        # Fall through to static file serving
        super().do_GET()

    def do_POST(self):
        if self._reject_non_local():
            return
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(content_length)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                send_error(self, "Invalid JSON body", 400)
                return

            provider = body.get("provider", "")
            api_key = body.get("api_key", "")
            model = body.get("model", "")
            system_prompt = body.get("system", "")
            messages = body.get("messages", [])
            base_url = body.get("base_url", "")

            if not provider or not model:
                send_error(self, "Missing required fields: provider, model", 400)
                return
            if not api_key and provider not in ("openai-compatible",):
                send_error(self, "Missing required field: api_key", 400)
                return

            try:
                if provider == "anthropic":
                    result = call_anthropic(api_key, model, system_prompt, messages)
                elif provider in ("openai", "openai-compatible"):
                    result = call_openai(api_key, model, system_prompt, messages, base_url if provider == "openai-compatible" else None)
                else:
                    send_error(self, f"Unknown provider: {provider}", 400)
                    return
                send_json(self, result)
            except RuntimeError as e:
                send_error(self, str(e))
            except Exception as e:
                send_error(self, f"Unexpected error: {e}")
            return

        send_error(self, "Not found", 404)


# ============ MAIN ============

def main():
    port = DEFAULT_PORT
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=")[1])
        elif arg.startswith("--port"):
            continue
        elif sys.argv[sys.argv.index(arg) - 1] == "--port":
            port = int(arg)
        else:
            try:
                port = int(arg)
            except ValueError:
                pass

    # Serve files from the script's directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    server = HTTPServer(("127.0.0.1", port), ProxyHandler)
    server.daemon_threads = True  # Allow Ctrl+C even during active requests

    def shutdown_handler(sig, frame):
        print("\nShutting down...")
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)

    print()
    print("┌──────────────────────────────────────────────────────┐")
    print("│        ⚡ Agent Discussion Arena — Proxy Server       │")
    print("├──────────────────────────────────────────────────────┤")
    print(f"│  URL: http://localhost:{port:<29}│")
    print("│                                                      │")
    print("│  Open the URL above in your browser.                 │")
    print("│  API keys are sent to providers only,                │")
    print("│  never logged or stored by this proxy.               │")
    print("│                                                      │")
    print("│  Press Ctrl+C to stop the server.                    │")
    print("└──────────────────────────────────────────────────────┘")
    print()

    server.serve_forever()
    server.server_close()
    print("Server stopped.")


if __name__ == "__main__":
    main()
