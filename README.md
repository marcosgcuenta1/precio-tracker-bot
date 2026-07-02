# 👟 Bot vigila-precios

Rastrea **URLs de producto concretas** y te avisa por **Telegram** cuando el
precio **baja**. Nace para vigilar unas zapatillas en Deporvillage, pero sirve
para cualquier producto: solo añades su URL en `config.yaml`.

## Cómo decide avisar
- Avisa **en cada bajada** respecto al último precio que vio.
- Opcional: pon `target_price` en un producto para que avise además cuando baje
  de ese valor.
- Marca 🏆 cuando el precio es el **mínimo histórico** que ha registrado.

El precio se lee del **dato estructurado JSON-LD** de la página (fiable), con un
regex de respaldo. Usa un navegador headless (Playwright) para esquivar el
anti-bot básico.

---

## 1. Instalación (ya hecha)
```powershell
cd C:\Users\marco\precio-tracker-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

## 2. Crear el bot de Telegram (NUEVO)
1. En Telegram, habla con **@BotFather** → `/newbot` → copia el **token**.
2. Escríbele algo al bot recién creado (para poder recibir mensajes).
3. `copy .env.example .env` y pega el token (tu `chat_id` ya está puesto).

## 3. Añadir productos
Edita `config.yaml`:
```yaml
products:
  - name: "NNormal Tomir 2.0"
    url: "https://www.deporvillage.com/zapatillas-nnormal-tomir-2-0-azul-grisaceo-blanco"
    target_price: 150     # opcional
```

## 4. Ejecutar
```powershell
.\.venv\Scripts\python.exe bot.py --test    # te manda el precio actual (prueba)
.\.venv\Scripts\python.exe bot.py --once     # una pasada (avisa solo si bajó)
.\.venv\Scripts\python.exe bot.py            # bucle continuo
```

## Dejarlo corriendo 24/7
- **Local:** deja `python bot.py` abierto, o una **Tarea Programada** con `--once`.
- **GitHub Actions:** igual que tus otros bots (cron con `--once`, `state.json`
  versionado). Aviso: algunas tiendas bloquean la IP de los runners; Deporvillage
  habrá que probarlo. Si bloquea, se queda en local.

> Si una web deja de dar precio, lanza `bot.py --once --debug` y revisa el
> `debug_0.html` para ajustar la extracción.
