from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import aiohttp
import qrcode
import websockets
from aiohttp import web
from websockets.exceptions import ConnectionClosed

LOGGER = logging.getLogger("nexo_tunnel_agent")
DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
STATE_PATH = DATA_DIR / "agent_state.json"
WEB_PORT = 8099


@dataclass
class PairingState:
    home_id: str
    agent_token: str
    suggested_name: str


@dataclass
class AgentConfig:
    backend_url: str
    frontend_pairing_url: str
    ha_base_url: str
    ha_access_token: str
    use_supervisor_token: bool
    reconnect_delay_seconds: int
    heartbeat_interval_seconds: int
    pairing_state: PairingState


class NexoTunnelAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.stop_event = asyncio.Event()
        self.connected = False
        self.connected_since: float | None = None
        self.last_error = ""
        self.last_heartbeat_ts: float | None = None
        self.app = web.Application()
        self.http_runner: web.AppRunner | None = None
        self.http_session: aiohttp.ClientSession | None = None
        self.websocket_task: asyncio.Task[None] | None = None
        self._warned_missing_ha_token = False
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/qr", self.handle_qr)
        self.app.router.add_get("/api/status", self.handle_status)
        self.app.router.add_get("/health", self.handle_health)

    @property
    def pairing_url(self) -> str:
        base = self.config.frontend_pairing_url.rstrip("/")
        query = urlencode(
            {
                "source": "addon",
                "homeId": self.config.pairing_state.home_id,
                "agentToken": self.config.pairing_state.agent_token,
                "suggestedName": self.config.pairing_state.suggested_name,
            }
        )
        return f"{base}?{query}"

    @property
    def websocket_url(self) -> str:
        parsed = urlparse(self.config.backend_url)
        if parsed.scheme not in {"http", "https", "ws", "wss"}:
            raise ValueError(f"Unsupported backend_url scheme: {parsed.scheme or 'missing'}")

        scheme = {
            "http": "ws",
            "https": "wss",
            "ws": "ws",
            "wss": "wss",
        }[parsed.scheme]
        origin = f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        return (
            f"{origin}/tunnel?"
            + urlencode(
                {
                    "homeId": self.config.pairing_state.home_id,
                    "agentToken": self.config.pairing_state.agent_token,
                }
            )
        )

    def current_status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connectedSince": self.connected_since,
            "lastError": self.last_error,
            "lastHeartbeatTs": self.last_heartbeat_ts,
            "backendUrl": self.config.backend_url,
            "pairingUrl": self.pairing_url,
            "homeId": self.config.pairing_state.home_id,
            "suggestedName": self.config.pairing_state.suggested_name,
            "haBaseUrl": self.config.ha_base_url,
            "usesSupervisorToken": bool(self.resolve_ha_token(source_only=True) == "supervisor"),
        }

    async def push_ha_sensor(self) -> None:
        """Publica el estado del túnel como sensor en Home Assistant."""
        supervisor_token = os.getenv("SUPERVISOR_TOKEN", "")
        if not supervisor_token or not self.http_session:
            return
        attributes = {
            "friendly_name": "Nexo Tunnel",
            "home_id": self.config.pairing_state.home_id,
            "suggested_name": self.config.pairing_state.suggested_name,
            "backend_url": self.config.backend_url,
            "last_error": self.last_error or "Ninguno",
            "icon": "mdi:lan-connect" if self.connected else "mdi:lan-disconnect",
        }
        try:
            async with self.http_session.post(
                "http://supervisor/core/api/states/binary_sensor.nexo_tunnel_connected",
                headers={"Authorization": f"Bearer {supervisor_token}", "Content-Type": "application/json"},
                json={"state": "on" if self.connected else "off", "attributes": attributes},
            ) as resp:
                if resp.status not in (200, 201):
                    LOGGER.debug("Sensor push respondió con status %s", resp.status)
        except Exception as exc:
            LOGGER.debug("No se pudo publicar el sensor en HA: %s", exc)

    async def ha_sensor_loop(self) -> None:
        """Actualiza el sensor de HA periódicamente."""
        while not self.stop_event.is_set():
            await self.push_ha_sensor()
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=90)
        self.http_session = aiohttp.ClientSession(timeout=timeout)

        self.http_runner = web.AppRunner(self.app)
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, host="0.0.0.0", port=WEB_PORT)
        await site.start()
        LOGGER.info("Pairing UI listening on port %s", WEB_PORT)

        self.websocket_task = asyncio.create_task(self.tunnel_loop(), name="nexo-tunnel-loop")
        asyncio.create_task(self.ha_sensor_loop(), name="nexo-ha-sensor")

    async def stop(self) -> None:
        self.stop_event.set()

        if self.websocket_task:
            self.websocket_task.cancel()
            try:
                await self.websocket_task
            except asyncio.CancelledError:
                pass

        if self.http_session:
            await self.http_session.close()

        if self.http_runner:
            await self.http_runner.cleanup()

    async def tunnel_loop(self) -> None:
        while not self.stop_event.is_set():
            if self._looks_like_placeholder(self.config.backend_url):
                self.connected = False
                self.last_error = "backend_url is still using a placeholder value"
                await asyncio.sleep(self.config.reconnect_delay_seconds)
                continue

            try:
                ws_url = self.websocket_url
                LOGGER.info("Connecting to tunnel %s", ws_url)
                async with websockets.connect(ws_url, ping_interval=None, close_timeout=5, max_size=None) as websocket:
                    self.connected = True
                    self.connected_since = time.time()
                    self.last_error = ""
                    LOGGER.info("Tunnel connected for home_id=%s", self.config.pairing_state.home_id)

                    consumer = asyncio.create_task(self.consume_messages(websocket), name="nexo-consumer")
                    heartbeat = asyncio.create_task(self.send_heartbeat(websocket), name="nexo-heartbeat")
                    done, pending = await asyncio.wait(
                        {consumer, heartbeat},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )

                    for task in pending:
                        task.cancel()
                        with contextlib_suppress(asyncio.CancelledError):
                            await task

                    for task in done:
                        exc = task.exception()
                        if exc:
                            raise exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.connected_since = None
                self.last_error = str(exc)
                LOGGER.warning("Tunnel disconnected: %s", exc)
                await asyncio.sleep(self.config.reconnect_delay_seconds)
            finally:
                self.connected = False
                self.connected_since = None

    async def consume_messages(self, websocket: websockets.ClientConnection) -> None:
        async for raw_message in websocket:
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                LOGGER.warning("Ignoring invalid JSON payload from backend")
                continue

            message_type = message.get("type")
            if message_type == "proxy_command":
                await self.handle_proxy_command(websocket, message)
            elif message_type == "heartbeat_ack":
                self.last_heartbeat_ts = time.time()
            else:
                LOGGER.debug("Ignoring unsupported backend message type=%s", message_type)

    async def send_heartbeat(self, websocket: websockets.ClientConnection) -> None:
        while True:
            payload = {"type": "heartbeat", "ts": int(time.time() * 1000)}
            await websocket.send(json.dumps(payload))
            self.last_heartbeat_ts = time.time()
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def handle_proxy_command(self, websocket: websockets.ClientConnection, message: dict[str, Any]) -> None:
        request_id = str(message.get("requestId") or "")
        method = str(message.get("method") or "GET")
        path = str(message.get("path") or "/")
        headers = message.get("headers") or {}
        body = message.get("body")

        response_payload: dict[str, Any]
        try:
            status, response_body = await self.forward_to_home_assistant(method, path, headers, body)
            response_payload = {
                "type": "response",
                "requestId": request_id,
                "ok": status < 400,
                "status": status,
                "body": response_body,
            }
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Proxy command failed request_id=%s path=%s", request_id, path)
            response_payload = {
                "type": "response",
                "requestId": request_id,
                "ok": False,
                "status": 502,
                "error": str(exc),
            }

        await websocket.send(json.dumps(response_payload))

    async def forward_to_home_assistant(
        self,
        method: str,
        path: str,
        headers: dict[str, Any],
        body: Any,
    ) -> tuple[int, Any]:
        if not self.http_session:
            raise RuntimeError("HTTP session not initialized")

        upstream_url = self.build_home_assistant_url(path)
        request_headers, auth_source = self.build_forward_headers(headers)
        request_kwargs: dict[str, Any] = {}

        if body is not None:
            if isinstance(body, (dict, list)):
                request_kwargs["json"] = body
            elif isinstance(body, (str, bytes)):
                request_kwargs["data"] = body
            else:
                request_kwargs["data"] = json.dumps(body)

        async with self.http_session.request(method.upper(), upstream_url, headers=request_headers, **request_kwargs) as response:
            payload = await self.parse_response_body(response)
            if response.status != 401:
                return response.status, payload

            retry_headers, retry_source = self.build_forward_headers(headers, preferred_source=self.alternate_auth_source(auth_source))
            if retry_source and retry_source != auth_source:
                LOGGER.warning(
                    "Home Assistant respondió 401 para %s %s usando token '%s'; reintentando con '%s'",
                    method.upper(),
                    path,
                    auth_source or "none",
                    retry_source,
                )
                async with self.http_session.request(
                    method.upper(),
                    upstream_url,
                    headers=retry_headers,
                    **request_kwargs,
                ) as retry_response:
                    retry_payload = await self.parse_response_body(retry_response)
                    return retry_response.status, retry_payload

            LOGGER.warning(
                "Home Assistant respondió 401 para %s %s con token '%s' y base '%s'",
                method.upper(),
                path,
                auth_source or "none",
                self.config.ha_base_url,
            )
            return response.status, payload

    def build_home_assistant_url(self, path: str) -> str:
        normalized_path = path if path.startswith("/") else f"/{path}"
        base = self.config.ha_base_url.rstrip("/")
        return f"{base}{normalized_path}"

    def build_forward_headers(
        self,
        headers: dict[str, Any],
        preferred_source: str | None = None,
    ) -> tuple[dict[str, str], str]:
        blacklist = {"host", "connection", "content-length", "authorization"}
        forwarded = {
            str(key): str(value)
            for key, value in headers.items()
            if str(key).lower() not in blacklist and value is not None
        }

        token, source = self.resolve_ha_token_pair(preferred_source=preferred_source)
        if token:
            forwarded["Authorization"] = f"Bearer {token}"
            # Mejora compatibilidad al usar el proxy del Supervisor.
            if source == "supervisor":
                forwarded["X-Supervisor-Token"] = token
        elif not self._warned_missing_ha_token:
            self._warned_missing_ha_token = True
            LOGGER.warning(
                "No hay token configurado para Home Assistant. Configura ha_access_token o habilita use_supervisor_token con SUPERVISOR_TOKEN disponible."
            )

        if "Accept" not in forwarded:
            forwarded["Accept"] = "application/json"

        return forwarded, source

    @staticmethod
    def alternate_auth_source(source: str) -> str | None:
        if source == "supervisor":
            return "manual"
        if source == "manual":
            return "supervisor"
        return None

    def resolve_ha_token_pair(self, preferred_source: str | None = None) -> tuple[str, str]:
        supervisor_token = os.getenv("SUPERVISOR_TOKEN", "")
        manual_token = self.config.ha_access_token

        order: list[str]
        if preferred_source in {"supervisor", "manual"}:
            order = [preferred_source, "manual" if preferred_source == "supervisor" else "supervisor"]
        else:
            order = ["supervisor", "manual"] if self.config.use_supervisor_token else ["manual", "supervisor"]

        for source in order:
            if source == "supervisor" and self.config.use_supervisor_token and supervisor_token:
                return supervisor_token, "supervisor"
            if source == "manual" and manual_token:
                return manual_token, "manual"

        return "", ""

    def resolve_ha_token(self, source_only: bool = False) -> str:
        token, source = self.resolve_ha_token_pair()
        return source if source_only else token

    async def parse_response_body(self, response: aiohttp.ClientResponse) -> Any:
        raw = await response.read()
        if not raw:
            return None

        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return raw.decode("utf-8", errors="replace")

        return raw.decode("utf-8", errors="replace")

    async def handle_index(self, request: web.Request) -> web.Response:
        status = self.current_status()
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
        base_href = f"{ingress_path}/" if ingress_path else "/"
        tunnel_class = "status-ok" if status["connected"] else "status-ko"
        tunnel_text = "Conectado" if status["connected"] else "Desconectado"
        last_error = status["lastError"] or "Sin errores"
        html = f"""<!doctype html>
<html lang="es">
    <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <base href="{base_href}" />
    <title>Nexo Tunnel Agent</title>
    <style>
      body {{ font-family: system-ui, sans-serif; background: #020617; color: #e2e8f0; margin: 0; padding: 24px; }}
      .wrap {{ max-width: 980px; margin: 0 auto; display: grid; gap: 24px; }}
      .grid {{ display: grid; gap: 24px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); }}
      .card {{ background: #0f172a; border: 1px solid #1e293b; border-radius: 20px; padding: 20px; }}
      .label {{ color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: .12em; margin-top: 12px; }}
      .label:first-child {{ margin-top: 0; }}
      .value {{ margin-top: 4px; word-break: break-word; font-size: 15px; }}
      .status-ok {{ color: #34d399; }}
      .status-ko {{ color: #fb7185; }}
      .badge {{ display: inline-flex; align-items: center; gap: 6px; border-radius: 999px; padding: 4px 10px; font-size: 12px; font-weight: 500; }}
      .badge-ok {{ background: rgba(16,185,129,.12); border: 1px solid rgba(16,185,129,.3); color: #34d399; }}
      .badge-ko {{ background: rgba(251,113,133,.12); border: 1px solid rgba(251,113,133,.3); color: #fb7185; }}
      .dot {{ width: 8px; height: 8px; border-radius: 50%; }}
      .dot-ok {{ background: #34d399; }}
      .dot-ko {{ background: #fb7185; }}
      img {{ width: min(100%, 300px); border-radius: 16px; background: #fff; padding: 12px; display: block; }}
      code {{ display: block; background: #020617; border: 1px solid #1e293b; border-radius: 12px; padding: 10px; color: #67e8f9; overflow-wrap: anywhere; font-size: 12px; }}
      a {{ color: #67e8f9; }}
      .paired {{ background: rgba(16,185,129,.10); border: 1px solid rgba(16,185,129,.25); border-radius: 16px; padding: 16px; color: #6ee7b7; font-size: 14px; }}
      .not-paired {{ background: rgba(99,102,241,.08); border: 1px solid rgba(99,102,241,.20); border-radius: 16px; padding: 16px; color: #a5b4fc; font-size: 14px; }}
    </style>
  </head>
  <body>
    <div class="wrap">
      <div>
        <p class="label">Nexo Tunnel Agent</p>
        <h1 style="margin:4px 0 8px">Vinculación de Home Assistant</h1>
        <p style="color:#94a3b8;font-size:14px">Escanea el QR con la app Nexo en el móvil. El formulario se abrirá con los datos prellenados.</p>
      </div>

      <div class="grid">
        <section class="card">
          <p class="label">Estado del túnel</p>
          <div class="value">
            <span class="badge {'badge-ok' if status['connected'] else 'badge-ko'}">
              <span class="dot {'dot-ok' if status['connected'] else 'dot-ko'}"></span>
              {tunnel_text}
            </span>
          </div>

          <p class="label">homeId</p>
          <p class="value" id="home-id">{status['homeId']}</p>

          <p class="label">Nombre sugerido</p>
          <p class="value">{status['suggestedName']}</p>

          <p class="label">Estado de vinculación</p>
          <div id="pairing-status" class="value">
            <span style="color:#94a3b8;font-size:13px">Comprobando...</span>
          </div>

          <p class="label">Último error</p>
          <p class="value" style="color:#fb7185;font-size:13px">{last_error}</p>
        </section>

        <section class="card">
          <img alt="QR de vinculación" src="qr" />
          <p class="label" style="margin-top:16px">URL de vinculación</p>
          <code id="pairing-url">{status['pairingUrl']}</code>
          <p style="margin-top:12px;font-size:12px;color:#94a3b8">
            O abre: <a href="{status['pairingUrl']}" target="_blank" rel="noopener">enlace directo</a>
          </p>
        </section>
      </div>

      <section class="card">
        <p class="label">Estado JSON en tiempo real</p>
        <a href="api/status" style="font-size:13px">/api/status</a>
        <p style="margin-top:12px;font-size:13px;color:#94a3b8">Esta página se actualiza cada 8 segundos.</p>
      </section>
    </div>

    <script>
      async function refresh() {{
        try {{
          const r = await fetch('api/status');
          if (!r.ok) return;
          const s = await r.json();

          // Actualizar badge del túnel
          const badge = document.querySelector('.badge');
          if (badge) {{
            badge.textContent = s.connected ? 'Conectado' : 'Desconectado';
            badge.className = 'badge ' + (s.connected ? 'badge-ok' : 'badge-ko');
          }}

          // Actualizar estado de vinculación
          const pairingEl = document.getElementById('pairing-status');
          if (pairingEl) {{
            if (s.connected) {{
              pairingEl.innerHTML = '<div class="paired">✓ Túnel activo — esta Home ya está registrada y conectada.</div>';
            }} else {{
              pairingEl.innerHTML = '<div class="not-paired">Escanea el QR para registrar esta Home en Nexo.</div>';
            }}
          }}
        }} catch (e) {{
          console.warn('Status poll failed', e);
        }}
      }}

      refresh();
      setInterval(refresh, 8000);
    </script>
  </body>
</html>"""
        return web.Response(text=html, content_type="text/html")

    async def handle_qr(self, request: web.Request) -> web.Response:
        image = qrcode.make(self.pairing_url)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return web.Response(body=buffer.getvalue(), content_type="image/png")

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.current_status())

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", **self.current_status()})

    @staticmethod
    def _looks_like_placeholder(value: str) -> bool:
        lowered = value.strip().lower()
        return not lowered or "example" in lowered


class contextlib_suppress:  # pragma: no cover - tiny inline helper for runtime only
    def __init__(self, *exceptions: type[BaseException]) -> None:
        self.exceptions = exceptions

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, _tb) -> bool:
        return exc_type is not None and issubclass(exc_type, self.exceptions)


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        LOGGER.warning("Invalid JSON in %s; using empty configuration", path)
        return {}


def default_pairing_state() -> PairingState:
    return PairingState(
        home_id=f"ha_{uuid.uuid4().hex[:12]}",
        agent_token=f"{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}",
        suggested_name="Home Assistant",
    )


def ensure_pairing_state(options: dict[str, Any]) -> PairingState:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stored = load_json(STATE_PATH)
    generated = default_pairing_state()

    pairing_state = PairingState(
        home_id=str(options.get("home_id") or stored.get("home_id") or generated.home_id),
        agent_token=str(options.get("agent_token") or stored.get("agent_token") or generated.agent_token),
        suggested_name=str(options.get("suggested_name") or stored.get("suggested_name") or generated.suggested_name),
    )

    STATE_PATH.write_text(json.dumps(asdict(pairing_state), indent=2), encoding="utf-8")
    return pairing_state


def load_config() -> AgentConfig:
    options = load_json(OPTIONS_PATH)
    pairing_state = ensure_pairing_state(options)

    return AgentConfig(
        backend_url=str(options.get("backend_url") or os.getenv("BACKEND_URL") or ""),
        frontend_pairing_url=str(options.get("frontend_pairing_url") or os.getenv("FRONTEND_PAIRING_URL") or ""),
        ha_base_url=str(options.get("ha_base_url") or os.getenv("HA_BASE_URL") or "http://supervisor/core"),
        ha_access_token=str(options.get("ha_access_token") or os.getenv("HA_ACCESS_TOKEN") or ""),
        use_supervisor_token=bool(options.get("use_supervisor_token", True)),
        reconnect_delay_seconds=int(options.get("reconnect_delay_seconds", 5)),
        heartbeat_interval_seconds=int(options.get("heartbeat_interval_seconds", 20)),
        pairing_state=pairing_state,
    )


async def async_main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    config = load_config()
    agent = NexoTunnelAgent(config)
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib_suppress(NotImplementedError):
            loop.add_signal_handler(sig, agent.stop_event.set)

    await agent.start()
    LOGGER.info("Agent ready. Pairing URL: %s", agent.pairing_url)

    try:
        await agent.stop_event.wait()
    finally:
        await agent.stop()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

