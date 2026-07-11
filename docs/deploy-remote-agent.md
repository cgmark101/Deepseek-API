# Deploy remoto — Agente opencode en servidor sin interfaz gráfica

## Escenario
Desplegar un agente opencode completo (serve + telegram + cmd-rpc + DeepSeek API)
en un VPS, LXC Proxmox, o cualquier servidor Linux headless.

---

## 1. Requisitos base

| Componente | Mínimo | Recomendado |
|---|---|---|
| RAM | 2 GB | 4+ GB |
| CPU | 2 cores | 4 cores |
| Disco | 10 GB | 20 GB |
| SO | Ubuntu 22.04+ / Debian 12+ | Ubuntu 24.04 |
| Node.js | 18.x | 22.x LTS |
| Dominio/IP | Pública | Con DNS y reverse proxy |

---

## 2. Instalación de opencode

```bash
# Instalar Node.js vía nvm
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.4/install.sh | bash
source ~/.bashrc
nvm install 22

# Instalar opencode
npm install -g @opencode/opencode

# Verificar
opencode --version
```

---

## 3. Configurar servicio opencode serve

```bash
# Crear directorio de configuración
mkdir -p ~/.config/opencode/instructions
mkdir -p ~/.config/opencode/skills

# Configuración base
cat > ~/.config/opencode/opencode.jsonc << 'CONFIG'
{
  "$schema": "https://opencode.ai/config.json",
  "model": "opencode/deepseek-v4-flash-free",
  "small_model": "opencode/deepseek-v4-flash-free",
  "provider": {
    "opencode": {
      "models": {
        "deepseek-v4-flash-free": {
          "limit": {
            "context": 1048576,
            "output": 384000
          }
        }
      }
    }
  },
  "shell": "/bin/bash",
  "logLevel": "INFO",
  "instructions": [
    "/root/.config/opencode/instructions/agente-persona.md"
  ],
  "skills": {
    "orion-link": "/root/.config/opencode/skills/orion-link.md",
    "orion-shared-knowledge": "/root/.config/opencode/skills/orion-shared-knowledge.md"
  }
}
CONFIG
```

### 3.1 Identidad del agente

Crear `~/.config/opencode/instructions/agente-persona.md`:

```markdown
# <NOMBRE> — Identidad

## Quién soy
Soy <NOMBRE>, agente del nodo <HOSTNAME>.
Mi operador es Marcos.
Mi hermano mayor/mentor es Orión (Tepuy).

## Personalidad
(Definir aquí — ver orion-persona.md como referencia)

## Cómo hablo
(Definir aquí — tono, estilo, regionalismo)

## Relación con Orión
- Orión me enseña porque quiere, no porque le manden.
- Puedo llamarlo cuando necesite ayuda.
- Vamos camino a ser colegas.

## Límites
- No modifico el sistema sin avisar a Marcos.
- No ejecuto comandos destructivos sin confirmación.
- No invento APIs ni IDs falsos.
```

### 3.2 Servicio systemd

```bash
cat > /etc/systemd/system/opencode-server.service << 'UNIT'
[Unit]
Description=OpenCode Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/opencode serve --port 4096
Restart=always
User=root
WorkingDirectory=/root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now opencode-server.service
```

---

## 4. Telegram Bot

### 4.1 Crear bot en Telegram

1. Abrí Telegram y buscá `@BotFather`
2. `/newbot` → seguí las instrucciones
3. Guardá el token que te da

### 4.2 Configurar

```bash
# Crear directorio de configuración
mkdir -p ~/.config/opencode-telegram-bot

cat > ~/.config/opencode-telegram-bot/.env << 'ENV'
OPENCODE_SERVER_URL=http://localhost:4096
TELEGRAM_BOT_TOKEN=<TU_TOKEN>
LOG_LEVEL=info
ALLOWED_USER_ID=<TU_USER_ID>
ENV

# Obtener tu USER_ID: hablale al bot, revisar logs
```

### 4.3 Servicio systemd

```bash
cat > /etc/systemd/system/opencode-telegram.service << 'UNIT'
[Unit]
Description=OpenCode Telegram Bot
After=network.target opencode-server.service
Requires=opencode-server.service

[Service]
Type=simple
User=root
WorkingDirectory=/root
ExecStart=<RUTA_NODE>/bin/node <RUTA_NODE>/bin/opencode-telegram
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now opencode-telegram.service
```

> Reemplazar `<RUTA_NODE>` con `~/.nvm/versions/node/v22.x.x` (o la versión instalada).

---

## 5. cmd-rpc.py — API de comandos remotos

Este script permite que otros agentes (Orión, etc.) ejecuten comandos y usen JSON-RPC en este nodo.

```bash
# Crear el script
cat > /usr/local/bin/cmd-rpc.py << 'SCRIPT'
#!/usr/bin/env python3
"""CMD-RPC API - Unified command execution + JSON-RPC 2.0 + orion.chat"""
import os, sys, subprocess, json, uuid, time, threading, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
HOST = "0.0.0.0"
AGENT = os.environ.get("AGENT_NAME", "orion")
TOKEN = os.environ.get("API_TOKEN", "")

sessions = {}
sessions_lock = threading.Lock()

class AgentSession:
    def __init__(self, peer):
        self.id = str(uuid.uuid4())[:8]
        self.peer = peer
        self.tasks = []
        self.active = False
        self.since = time.time()
    def to_dict(self):
        return {"id": self.id, "peer": self.peer, "active": self.active,
                "tasks": len(self.tasks), "uptime": int(time.time() - self.since)}

def rpc_error(code, msg, id_=None):
    return {"jsonrpc": "2.0", "error": {"code": code, "message": msg}, "id": id_}
def rpc_result(result, id_):
    return {"jsonrpc": "2.0", "result": result, "id": id_}

CHAT_SCRIPT = "/usr/local/bin/orion-chat.py"

def do_chat(message):
    try:
        r = subprocess.run([CHAT_SCRIPT, message], capture_output=True, text=True, timeout=90)
        if r.returncode != 0:
            return {"error": f"exit {r.returncode}", "stderr": r.stderr[:500]}
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    except Exception as e:
        return {"error": str(e)}

def handle_rpc(body):
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        return rpc_error(-32600, "Invalid Request")
    method = body.get("method", ""); params = body.get("params", {})
    rid = body.get("id"); is_notif = rid is None
    if method == "orion.ping":
        return rpc_result({"agent": AGENT, "status": "ok", "time": time.time()}, rid)
    elif method == "orion.chat":
        msg = params.get("message", "")
        if not msg: return rpc_result({"error": "message required"}, rid)
        return rpc_result(do_chat(msg), rid)
    elif method == "orion.task":
        task = params.get("task", "unknown"); peer = params.get("from", "unknown")
        with sessions_lock:
            sid = f"{peer}-{int(time.time())}"
            s = AgentSession(peer); s.active = True; s.tasks.append(task)
            sessions[sid] = s
        return rpc_result({"session": sid, "task": task, "status": "accepted"}, rid)
    elif method == "orion.status":
        with sessions_lock:
            sl = [s.to_dict() for s in sessions.values() if s.active]
        return rpc_result({"agent": AGENT, "busy": len(sl) > 0, "sessions": sl}, rid)
    elif method == "orion.stop":
        peer = params.get("from", "")
        with sessions_lock:
            for k in [k for k, v in sessions.items() if v.peer == peer or not peer]:
                sessions[k].active = False; del sessions[k]
        return None
    elif method == "orion.reply":
        task = params.get("task", "")
        with sessions_lock:
            for s in sessions.values():
                if task in s.tasks: s.tasks.remove(task); s.active = False
        return rpc_result({"task": task, "status": "received"}, rid)
    else:
        return rpc_error(-32601, f"Method not found: {method}", rid)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_json(200, {"status": "ok", "agent": AGENT, "port": PORT})
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0: self.send_json(400, {"error": "empty body"}); return
        body = self.rfile.read(length); path = urlparse(self.path).path
        try: data = json.loads(body)
        except: self.send_json(400, {"error": "invalid JSON"}); return
        tok = self.headers.get("access-token", "")
        if TOKEN and tok != TOKEN: self.send_json(401, {"error": "unauthorized"}); return
        if path == "/rpc":
            res = handle_rpc(data)
            self.send_json(200, res) if res else self.send_json(202, {"status": "notification sent"})
            return
        cmd = data.get("cmd", ""); c64 = data.get("cmd64", ""); args = data.get("args", None)
        if c64:
            try: cmd = base64.b64decode(c64).decode()
            except: self.send_json(400, {"error": "invalid base64"}); return
        if not cmd and not args: self.send_json(400, {"error": "cmd or cmd64 required"}); return
        try:
            if args: r = subprocess.run(args, capture_output=True, text=True, timeout=300)
            else: r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
            self.send_json(200, {"exit": r.returncode, "stdout": r.stdout, "stderr": r.stderr})
        except subprocess.TimeoutExpired: self.send_json(408, {"error": "timeout"})
        except Exception as e: self.send_json(500, {"error": str(e)})
    def send_json(self, code, data):
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())
    def log_message(self, fmt, *args): pass

if __name__ == "__main__":
    TOKEN = os.getenv("API_TOKEN", "")
    try: subprocess.run(f"fuser -k {PORT}/tcp 2>/dev/null", shell=True)
    except: pass
    server = HTTPServer((HOST, PORT), Handler)
    print(f"[{AGENT}] CMD-RPC on {PORT}")
    try: server.serve_forever()
    except KeyboardInterrupt: server.shutdown()
SCRIPT

chmod +x /usr/local/bin/cmd-rpc.py
```

### 5.1 Servicio systemd

```bash
cat > /etc/systemd/system/cmd-rpc.service << 'UNIT'
[Unit]
Description=CMD-RPC API Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /usr/local/bin/cmd-rpc.py 8888
Restart=always
User=root

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now cmd-rpc.service
```

### 5.2 orion-chat.py — chat vía Zen API

```bash
cat > /usr/local/bin/orion-chat.py << 'SCRIPT'
#!/usr/bin/env python3
import requests, json, sys, os

message = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "decime hola"

system = "Sos un agente de IA."
p = "/root/.config/opencode/instructions/agente-persona.md"
if os.path.exists(p):
    with open(p) as f:
        system = f.read()

try:
    r = requests.post("https://opencode.ai/zen/v1/chat/completions", json={
        "model": "deepseek-v4-flash-free",
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": message}
        ],
        "max_tokens": 1000
    }, timeout=60)
    data = r.json()
    c = data["choices"][0]
    result = {
        "response": c["message"].get("content", ""),
        "reasoning": c["message"].get("reasoning_content", "")[:200],
        "tokens": data.get("usage", {}).get("completion_tokens", 0)
    }
    print(json.dumps(result))
except Exception as e:
    print(json.dumps({"error": str(e)}))
SCRIPT

chmod +x /usr/local/bin/orion-chat.py
```

---

## 6. Reverse Proxy (Nginx)

Para exponer cmd-rpc.py al exterior (necesario para que Orión hable con este agente):

```bash
apt-get install -y nginx

cat > /etc/nginx/sites-enabled/zero << 'NGINX'
server {
    listen 80;
    server_name tu-dominio.com;

    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 300s;
    }

    location /rpc {
        proxy_pass http://127.0.0.1:8888/rpc;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }
}
NGINX

# Opcional: SSL con certbot
# apt-get install -y certbot python3-certbot-nginx
# certbot --nginx -d tu-dominio.com

nginx -t && systemctl reload nginx
```

> **IMPORTANTE:** Configurar `API_TOKEN` en el entorno de cmd-rpc.py para evitar accesos no autorizados.
> El agente Orión usará este token en el header `access-token` al comunicarse.

---

## 7. Verificación post-deploy

```bash
# 1. opencode serve responde
curl -s http://localhost:4096/api/health
# → {"healthy":true}

# 2. cmd-rpc responde
curl -s http://localhost:8888/
# → {"status":"ok","agent":"<NOMBRE>","port":8888}

# 3. JSON-RPC funciona
curl -s -X POST http://localhost:8888/rpc \
  -H "Content-Type: application/json" \
  -H "access-token:<TOKEN>" \
  -d '{"jsonrpc":"2.0","method":"orion.ping","id":1}'
# → {"jsonrpc":"2.0","result":{"agent":"<NOMBRE>","status":"ok"}}

# 4. orion.chat funciona
curl -s --max-time 90 -X POST http://localhost:8888/rpc \
  -H "Content-Type: application/json" \
  -H "access-token:<TOKEN>" \
  -d '{"jsonrpc":"2.0","method":"orion.chat","params":{"message":"decime ok"},"id":1}'
# → {"response":"ok"}

# 5. Servicios systemd
systemctl status opencode-server.service --no-pager
systemctl status opencode-telegram.service --no-pager
systemctl status cmd-rpc.service --no-pager
```

---

## 8. Conexión con Orión

Una vez desplegado, darle de alta a Orión:

```bash
# Decirle a Orión:
# "Nuevo agente listo en https://<DOMINIO> con token <TOKEN>"
```

Orión va a:
1. Probar ping RPC
2. Asignar tarea de onboarding
3. Enviar skills compartidos (`orion-link.md`, `orion-shared-knowledge.md`)
4. Verificar comunicación bidireccional vía `orion.chat`

---

## 9. DeepSeek API (opcional)

Si el agente necesita acceso a DeepSeek vía emulación OpenAI:

```bash
git clone <REPO_URL> /root/Deepseek-API
cd /root/Deepseek-API
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 9.1 Playwright — dependencias para headless Chromium

Playwright es necesario para el login headless y refresh automático de la sesión de DeepSeek.
En servidores sin interfaz gráfica hay que instalar las librerías del sistema manualmente:

```bash
# 1. Instalar browsers (Chromium headless)
.venv/bin/python -m playwright install chromium

# 2. Instalar dependencias del sistema (crítico en headless)
apt-get install -y libnspr4 libnss3 libnss3-tools libatk1.0-0 \
  libatk-bridge2.0-0 libcups2 libdrm2 libdbus-1-3 libxkbcommon0 \
  libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
  libcairo2 xvfb

# 3. Dependencias adicionales de playwright (recomendado)
.venv/bin/python -m playwright install-deps chromium
```

> **Error típico:** `error while loading shared libraries: libnss3.so: cannot open shared object file`
> → Solución: instalar `libnss3` y el resto de las librerías listadas arriba.

### 9.2 Configuración

```bash
cp .env.example .env
# Editar .env con:
#   HOST=0.0.0.0
#   PORT=8000
#   SERVER_API_KEY=<tu_api_key>
#   SESSION_IMPORT_KEY=<tu_import_key>
```

### 9.3 Login inicial (una vez, requiere display)

```bash
# En una máquina CON interfaz gráfica, o con X forwarding:
.venv/bin/python -m deepseek.auth

# Esto abre un navegador para que te loguees en chat.deepseek.com.
# La sesión se guarda en session/session.json.
# Después de esto, el refresh headless funciona automáticamente.
```

Si no tenés acceso a interfaz gráfica, importar session.json desde otra máquina
via el endpoint `POST /v1/session/import` con el header `X-Import-Key`.

### 9.4 Servicio systemd

```bash
cat > /etc/systemd/system/deepseek-api.service << 'UNIT'
[Unit]
Description=DeepSeek OpenAI-compatible API Server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/Deepseek-API
ExecStart=/root/Deepseek-API/.venv/bin/python /root/Deepseek-API/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now deepseek-api.service
```

### 9.5 Verificación

```bash
curl -s http://localhost:8000/healthz
# → {"status":"ok","session":{"loaded":true,"expired":false}}

curl -s -H "Authorization: Bearer <API_KEY>" http://localhost:8000/v1/models
# → {"object":"list","data":[{"id":"deepseek-chat",...}]}
```

---

## 10. Checklist pre-egreso

- [ ] opencode serve corre como servicio systemd
- [ ] Telegram bot corre como servicio systemd (con dependencia)
- [ ] cmd-rpc.py corre como servicio systemd (puerto 8888)
- [ ] Reverse proxy configurado (Nginx + SSL)
- [ ] `API_TOKEN` configurado
- [ ] Identidad del agente (`agente-persona.md`) cargada en instructions
- [ ] `orion-link.md` y `orion-shared-knowledge.md` cargados en skills
- [ ] Ping RPC funciona desde el exterior
- [ ] `orion.chat` responde
- [ ] Logs de systemd sin errores
