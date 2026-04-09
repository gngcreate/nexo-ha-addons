# Nexo Tunnel Agent Add-on

Add-on de Home Assistant para conectar una instancia local de HA con el backend de Nexo mediante WebSocket y túnel inverso.

## Funciones

- Mantiene una conexión persistente al backend `/tunnel`.
- Reenvía comandos del backend hacia la API local de Home Assistant.
- Expone una UI local en `http://<host>:8099` con estado del agente y QR de vinculación.
- Genera `home_id` y `agent_token` si no se configuran manualmente.

## Flujo de vinculación

1. Instala el add-on y configúralo con `backend_url` y `frontend_pairing_url`.
2. Abre la UI del add-on.
3. Escanea el QR desde la app/PWA de Nexo.
4. La app abrirá la pantalla de registro con `homeId` y `agentToken` prellenados.
5. Asigna un nombre y completa el alta.

