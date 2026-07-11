# Extracción Manual de Sesión de DeepSeek

Esta guía detalla el método más seguro y robusto para capturar e importar una sesión activa de DeepSeek en el API Gateway. 

Al utilizar este método manual, se evita por completo abrir navegadores automatizados (como Playwright) en el servidor o VPS, lo cual **bypassea el firewall (WAF) de Cloudflare / AWS** de DeepSeek y previene que bloqueen o suspendan tus cuentas por comportamientos de login automatizados.

---

## Paso 1: Obtener el JSON de Sesión desde tu Navegador

1. Abre tu navegador web habitual (Chrome, Edge, Firefox, etc.).
2. Ve a [chat.deepseek.com](https://chat.deepseek.com/) e inicia sesión normalmente con tu cuenta.
3. Abre las herramientas de desarrollador presionando **F12** (o `Ctrl + Shift + I`).
4. Ve a la pestaña **Consola (Console)**.
5. Si es la primera vez que pegas código en la consola, verás una advertencia de seguridad de tu navegador. Para permitir el pegado, escribe literalmente **`allow pasting`** (o **`permitir pegar`** si tu navegador está en español) en la consola y presiona **Enter**.
6. Copia y pega el siguiente script en la consola y presiona **Enter**:

```javascript
(function() {
  const tokenRaw = window.localStorage.getItem('userToken');
  if (!tokenRaw) { 
    console.error("¡No se encontró la sesión de DeepSeek! Asegúrate de haber iniciado sesión."); 
    return; 
  }
  const token = JSON.parse(tokenRaw).value;
  
  const cookies = {};
  document.cookie.split(';').forEach(c => {
    const parts = c.split('=');
    if (parts.length === 2) {
      cookies[parts[0].trim()] = parts[1].trim();
    }
  });
  
  const session = {
    token: token,
    cookies: cookies,
    user_agent: navigator.userAgent,
    captured_at: Math.floor(Date.now() / 1000)
  };
  
  console.log("%c=== COPIA EL SIGUIENTE BLOQUE JSON ===", "color: green; font-weight: bold;");
  console.log(JSON.stringify(session, null, 2));
})();
```

7. Copia todo el bloque de texto JSON generado (incluyendo las llaves `{` y `}`).

---

## Paso 2: Configurar el Servidor con la Sesión

Tienes dos métodos para aplicar esta sesión en tu servidor (local o VPS):

### Método A: Configuración Manual (Local o VPS con acceso a archivos)
Reemplaza o crea el archivo **`session/session.json`** en el directorio raíz de tu proyecto pegando el JSON copiado en el paso anterior. Debería lucir así:

```json
{
  "token": "GgmA+basfT7hLq6GYkUdauE...",
  "cookies": {
    "smidV2": "2026051015...",
    ".thumbcache_6b2e5...": "T9UywMxG..."
  },
  "user_agent": "Mozilla/5.0 ... Chrome/150.0.0.0 Safari/537.36",
  "captured_at": 1783738303
}
```

*Una vez guardado el archivo, reinicia el servidor para que lea la nueva sesión inmediatamente.*

---

### Método B: Importación Gráfica (Recomendado para VPS)
Puedes usar Swagger UI para subir tu sesión de forma segura:

1. Guarda el bloque JSON copiado en un archivo llamado `session.json` en tu computadora.
2. Ve al Swagger UI de tu servidor (ej: `https://tu-dominio.com/docs` o `http://localhost:8000/docs`).
3. Busca el endpoint **`POST /v1/session/import`** y haz clic en **Try it out**.
4. En el campo **`file`**, selecciona tu archivo `session.json`.
5. Si tienes configurada una clave de seguridad, ingrésala en el campo de cabecera **`X-Import-Key`**.
6. Haz clic en **Execute**.

El servidor cargará la sesión de forma inmediata y limpiará los clientes antiguos en memoria sin necesidad de reiniciar el proceso del servidor.
