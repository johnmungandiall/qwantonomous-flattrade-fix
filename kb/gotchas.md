# Gotchas — traps specific to this repo. Read before editing.

- ZeroMQ Port 5555 bind conflicts in `websocket_proxy/server.py#WebSocketProxy.__init__`: The proxy SUB socket binds `ZMQ_HOST:ZMQ_PORT` (port 5555). Running multiple OpenAlgo instances or rapid restarts without port release causes `ZMQError: Address in use`. Use `is_port_in_use` with a grace wait window.
- Flattrade WebSocket Auth Timing in `broker/flattrade/streaming/flattrade_websocket.py#FlattradeWebSocket._on_open`: Subscribing before server returns `AUTH_SUCCESS` causes Flattrade to send a close frame (`opcode=8`). `on_open` and resubscribe callbacks must only fire inside `_handle_auth_response` after authentication succeeds.
- Flattrade Single WS Connection Limit in `services/order_update_service.py#_POLLING_BROKERS`: Flattrade allows only 1 active WebSocket connection per user ID. Running a dedicated order-update WS evicts the market-data WS (and vice versa) with opcode 8. Flattrade must use polling for order updates.
- Frontend WebSocket Auto-Reconnect in `frontend/src/lib/MarketDataManager.ts#MarketDataManager`: Do not gate reconnects on `!event.wasClean`, as server close frames (ping timeout, idle close) mark `wasClean=true` and would otherwise permanently stall auto-reconnect.

Cite the code that proves each trap as `path#symbol` so [[conventions]] checks pass.
