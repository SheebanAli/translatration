import base64, json, os, shutil, subprocess, threading, time
from contextlib import asynccontextmanager

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from dotenv import load_dotenv
load_dotenv()

import hvac
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from hvac.exceptions import Forbidden, VaultError
from pydantic import BaseModel, Field


class Config:
    DEBUG = os.getenv("DEBUG_PRINT", "1") == "1"
    ALLOWED_AUD = {"translation-service", "mobile-app"}

    @classmethod
    def log(cls, *a):
        cls.DEBUG and print(*a, flush=True)

    @staticmethod
    def need(name):
        value = os.getenv(name)
        if not value:
            raise ValueError(f"{name} not set")
        return value

    @staticmethod
    def choose_backend() -> str:
        """TRANSLATION_BACKEND=openai|local skips the prompt. Otherwise ask 1 or 2."""
        env = os.getenv("TRANSLATION_BACKEND", "").strip().lower()
        if env in ("openai", "local"):
            return env
        try:
            return "local" if input("Backend: 1 = OpenAI, 2 = Local LLM -> ").strip() == "2" else "openai"
        except EOFError:  # no terminal (docker/service)
            return "openai"


class IatGuard:
    """iat must be within clock skew AND newer than the last accepted iat for that aud."""

    def __init__(self, skew):
        self.skew, self.last, self.lock = skew, {}, threading.Lock()  # single process only

    def check(self, aud, iat):
        if abs(int(time.time()) - iat) > self.skew: # ya ni server time - cleint ka diff 30 seconds say ziyda hai tu reject kardo
            raise HTTPException(401, f"iat outside allowed skew ({self.skew}s)")
        with self.lock:
            if iat <= self.last.get(aud, 0): # phela wala say bara ho tabhi chal warna reject kardo
                raise HTTPException(401, "iat already used or stale")
            self.last[aud] = iat


class VaultTokens:
    def __init__(self, allowed_aud):
        self.allowed_aud = allowed_aud
        self.role_id, self.secret_id = Config.need("VAULT_ROLE_ID"), Config.need("VAULT_SECRET_ID")
        self.v = hvac.Client(url=Config.need("VAULT_ADDR"), verify=False)
        self.v.token = Config.need("VAULT_TOKEN")
    def _login(self):
        self.v.auth.approle.login(role_id=self.role_id, secret_id=self.secret_id)

    def _call(self, fn):
        """Login if needed; re-login once if the Vault token expired."""
        try:
            if not self.v.token:
                self._login()
            try:
                return fn()
            except Forbidden:
                self._login()
                return fn()
        except Forbidden:
            raise HTTPException(403, "AppRole policy needs read on identity/oidc/token/<aud> "
                                     "and update on identity/oidc/introspect")
        except VaultError as e:
            raise HTTPException(502, f"Vault error: {e}")

    def issue_and_verify(self, aud):
        if aud not in self.allowed_aud:
            raise HTTPException(403, "Unknown audience")

        token = self._call(lambda: self.v.adapter.get(f"/v1/identity/oidc/token/{aud}"))["data"]["token"]
        print("token hai jo vault aa raha hai=",token)
        r = self._call(lambda: self.v.adapter.post("/v1/identity/oidc/introspect",
                                                   json={"token": token, "client_id": aud}))
        print("r hai jo vault aa raha hai=",r)
        if not r.get("active", (r.get("data") or {}).get("active")):
            raise HTTPException(401, "Vault rejected the token")

        body = token.split(".")[1]
        print("body hai jo vault aa rahi hai=",body)#ya body hai jwt token jo payload hai 
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))#ya body ko decode kar raha
        print("claim=",claims)
        Config.log("[CLAIMS]", json.dumps(claims))

        now = int(time.time())
        if claims.get("aud") != aud:
            raise HTTPException(401, "Audience mismatch")
        if claims.get("exp", 0) < now or claims.get("nbf", 0) > now + 1:
            raise HTTPException(401, "Token expired or not yet valid")
        return claims


class OpenAITranslator:
    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(api_key=Config.need("OPENAI_API_KEY"))

    def translate(self, urdu: str) -> str:
        r = self.client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.0, max_tokens=100,
            messages=[{"role": "system", "content": "Convert the given Urdu address to Roman Urdu. "
                                                    "Return ONLY the Roman Urdu text."},
                      {"role": "user", "content": urdu}])
        return r.choices[0].message.content.strip()


class LocalLLMTranslator:
    """Uses transliterate() from urdu1.py (must be in the same folder as app.py)."""

    def __init__(self):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location("urdu1", pathlib.Path(__file__).parent / "urdu1.py")
        self.urdu1 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.urdu1)
        print("[LOCAL LLM] Loading model (first run downloads it) ...", flush=True)
        self.urdu1._prepare()
        print("[LOCAL LLM] Model ready.", flush=True)

    def translate(self, urdu: str) -> str:
        roman, _doubts = self.urdu1.transliterate(urdu)
        return roman


class TranslatorFactory:
    @staticmethod
    def create(backend):
        return OpenAITranslator() if backend == "openai" else LocalLLMTranslator()


class Tunnel:
    """Start tunnel-client and call OpenAI Responses API through the MCP tunnel."""

    MODEL = "gpt-4o-mini"
    SERVER_LABEL = "private_data_service"

    def __init__(self):
        from openai import OpenAI
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        self.tunnel_id = os.environ.get("OPENAI_TUNNEL_ID")
        self.proc = None

    def running(self):
        return subprocess.run(["pgrep", "-f", "tunnel-client-bin"], capture_output=True).returncode == 0

    def start(self):
        """Optional: start tunnel-client if a tunnel id is configured."""
        if not self.tunnel_id:
            return
        if self.running():
            print("[TUNNEL] already running", flush=True)
            return
        binary = shutil.which("tunnel-client") or shutil.which("tunnel-client-bin")
        if not binary:
            print("[TUNNEL] tunnel-client not found in PATH", flush=True)
            return
        cmd = [binary, "run", "--control-plane.tunnel-id", self.tunnel_id,
               "--mcp.server-url", os.getenv("TUNNEL_SERVER_URL", "http://localhost:8000/mcp"),
               "--control-plane.poll-timeout", os.getenv("TUNNEL_CONTROL_PLANE_POLL_TIMEOUT", "30s"),
               "--control-plane.poll-deadline-guardrail",
               os.getenv("TUNNEL_CONTROL_PLANE_POLL_GUARDRAIL", "15s")]
        try:
            logfile = open(os.getenv("TUNNEL_LOG_PATH", "tunnel.log"), "a")
            self.proc = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            print(f"[TUNNEL] started, PID {self.proc.pid}", flush=True)
        except Exception as e:
            print(f"[TUNNEL] failed to start: {e}", flush=True)

    def create_response(self, data):
        if not self.tunnel_id:
            raise HTTPException(503, "OPENAI_TUNNEL_ID is not set")
        response = self.client.responses.create(
            model=self.MODEL,
            input=f"Review this test record and explain its status: {data}",
            tools=[{"type": "mcp", "server_label": self.SERVER_LABEL, "tunnel_id": self.tunnel_id}],
        )
        return response.output_text


class TranslateRequest(BaseModel):
    aud: str = Field(min_length=1, max_length=100)
    urdu: str = Field(min_length=1, max_length=500)
    iat: int = Field(gt=0)  # client unix time; must be newer than the last one for this aud


class TunnelReviewRequest(BaseModel):
    data: dict = Field(default_factory=lambda: {
        "customer_reference": "TEST-001",
        "status": "Pending onboarding",
    })


class TranslationService:
    def __init__(self, backend: str):
        self.backend = backend
        self.vault = VaultTokens(Config.ALLOWED_AUD)
        self.iat = IatGuard(int(os.getenv("IAT_SKEW_SECONDS", "30")))
        self.translator = None
        self.translator_error = None
        self.ready = threading.Event()
        self.tunnel = Tunnel()

    def load_translator(self):
        """Runs in a background thread so the server starts right away."""
        try:
            self.translator = TranslatorFactory.create(self.backend)
            print("[STARTUP] Translator ready.", flush=True)
        except Exception as e:
            self.translator_error = e
            print(f"[STARTUP] Translator failed: {e}", flush=True)
        finally:
            self.ready.set()

    def translate(self, req: TranslateRequest):
        if not self.ready.wait(timeout=300):
            raise HTTPException(503, "Translator still loading, retry shortly")
        if self.translator_error or self.translator is None:
            raise HTTPException(503, f"Translator failed to start: {self.translator_error}")

        self.iat.check(req.aud, req.iat)
        claims = self.vault.issue_and_verify(req.aud)
        try:
            roman = self.translator.translate(req.urdu)
        except Exception as e:
            raise HTTPException(502, f"Translation error: {type(e).__name__}: {e}")
        return {"aud": claims["aud"], "iat": req.iat, "roman_urdu": roman, "backend": self.backend}

    def health(self):
        v = self.vault.v
        return {
            "vault_authenticated": bool(v.token) and v.is_authenticated(),
            "backend": self.backend,
            "translator_ready": self.ready.is_set() and self.translator is not None,
            "translator_error": str(self.translator_error) if self.translator_error else None,
            "tunnel_id": self.tunnel.tunnel_id,
            "tunnel_running": self.tunnel.running(),
        }

    def tunnel_review(self, req: TunnelReviewRequest):
        try:
            return {"output_text": self.tunnel.create_response(req.data), "model": self.tunnel.MODEL}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Tunnel response error: {type(e).__name__}: {e}")


class MCP:
    VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}
    TOOL = {
        "name": "translate_urdu_address",
        "description": "Translate an Urdu address to Roman Urdu using the configured translator and Vault token checks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "aud": {"type": "string", "description": "Allowed audience: translation-service or mobile-app."},
                "urdu": {"type": "string", "description": "Urdu text to transliterate."},
                "iat": {"type": "integer", "description": "Current Unix timestamp in seconds; must be fresh and newer than the previous timestamp for this audience."},
            },
            "required": ["aud", "urdu", "iat"],
            "additionalProperties": False,
        },
    }

    def __init__(self, service: TranslationService):
        self.service = service

    @staticmethod
    def _rpc(rid, *, result=None, error=None, status=200):
        body = {"jsonrpc": "2.0", "id": rid}
        body["error" if error else "result"] = error or result
        return JSONResponse(body, status_code=status)

    async def handle(self, request: Request):
        """Stateless MCP Streamable HTTP endpoint for the OpenAI tunnel client."""
        try:
            message = await request.json()
        except Exception:
            return self._rpc(None, error={"code": -32700, "message": "Parse error"}, status=400)

        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            rid = message.get("id") if isinstance(message, dict) else None
            return self._rpc(rid, error={"code": -32600, "message": "Invalid Request"}, status=400)

        method, rid, params = message.get("method"), message.get("id"), message.get("params") or {}
        if rid is None:
            return Response(status_code=202)

        if method == "initialize":
            ver = params.get("protocolVersion")
            result = {
                "protocolVersion": ver if ver in self.VERSIONS else "2025-03-26",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "urdu-address-translator", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [self.TOOL]}
        elif method == "tools/call":
            name = params.get("name")
            if name != self.TOOL["name"]:
                result = {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}
            else:
                try:
                    roman = self.service.translate(TranslateRequest(**(params.get("arguments") or {})))["roman_urdu"]
                    result = {"content": [{"type": "text", "text": roman}], "isError": False}
                except HTTPException as exc:
                    result = {"content": [{"type": "text", "text": str(exc.detail)}], "isError": True}
                except Exception as exc:
                    result = {"content": [{"type": "text", "text": f"Invalid tool arguments: {exc}"}], "isError": True}
        else:
            return self._rpc(rid, error={"code": -32601, "message": f"Method not found: {method}"})

        return self._rpc(rid, result=result)


class Application:
    def __init__(self):
        self.backend = Config.choose_backend()
        print(f"[STARTUP] Backend: {self.backend.upper()}", flush=True)
        self.service = TranslationService(self.backend)
        self.mcp = MCP(self.service)
        self.app = FastAPI(title=f"Vault JWT -> Urdu Transliteration ({self.backend.upper()})",
                           lifespan=self.lifespan)
        self.app.add_api_route("/translate", self.service.translate, methods=["POST"])
        self.app.add_api_route("/health", self.service.health, methods=["GET"])
        self.app.add_api_route("/tunnel/review", self.service.tunnel_review, methods=["POST"])
        self.app.add_api_route("/mcp", self.mcp.handle, methods=["POST"])

    @asynccontextmanager
    async def lifespan(self, _app: FastAPI):
        threading.Thread(target=self.service.load_translator, daemon=True).start()
        self.service.tunnel.start()
        yield

    def run(self):
        import pathlib
        import ssl
        import uvicorn

        certs = pathlib.Path(__file__).resolve().parent / "certs"
        ciphers = (
            "ECDHE-RSA-AES256-GCM-SHA384:"
            "ECDHE-RSA-AES128-GCM-SHA256:"
            "ECDHE-RSA-CHACHA20-POLY1305"
        )
        host = os.getenv("BIND_HOST", "0.0.0.0")
        port = int(os.getenv("BIND_PORT", "8500"))
        print(
            f"[STARTUP] mTLS https://{host}:{port} "
            f"(client certificate required, CA={certs / 'ca.crt'})",
            flush=True,
        )
        uvicorn.run(
            self.app,
            host=host,
            port=port,
            ssl_certfile=str(certs / "server.crt"),
            ssl_keyfile=str(certs / "server.key"),
            ssl_ca_certs=str(certs / "ca.crt"),
            ssl_cert_reqs=ssl.CERT_REQUIRED,
            ssl_version=ssl.PROTOCOL_TLS_SERVER,
            ssl_ciphers=ciphers,
            log_level="info",
        )


if __name__ == "__main__":
    Application().run()
else:
    app = Application().app
