"""Bot vigila-precios: rastrea URLs de producto concretas y avisa por Telegram
cuando el precio BAJA (o baja de un objetivo opcional).

Uso:
    python bot.py            # bucle continuo (cada poll_interval_minutes)
    python bot.py --once     # una sola pasada (para cron / GitHub Actions)
    python bot.py --test     # te manda el precio actual de cada producto ahora
    python bot.py --once --debug   # guarda el HTML de cada pagina
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import logging
import argparse

import yaml
import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.yaml")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)-7s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("precios")


# --------------------------------------------------------------------------- #
# Estado
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text,
                                     "parse_mode": "HTML"}, timeout=20)
        if not r.ok:
            log.error("Telegram %s: %s", r.status_code, r.text)
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo enviar a Telegram: %s", e)


# --------------------------------------------------------------------------- #
# Extraccion de precio
# --------------------------------------------------------------------------- #

def _find_price(obj):
    """Busca recursivamente un precio en JSON-LD. Prioriza 'lowPrice' (el mas
    barato de un AggregateOffer = comparador con varias tiendas); si no, 'price'."""
    if isinstance(obj, dict):
        if "offers" in obj:
            p = _find_price(obj["offers"])
            if p:
                return p
        for key in ("lowPrice", "price"):   # lowPrice primero: el mas barato del mercado
            if key in obj:
                try:
                    return float(str(obj[key]).replace(",", "."))
                except (ValueError, TypeError):
                    pass
        for v in obj.values():
            p = _find_price(v)
            if p:
                return p
    elif isinstance(obj, list):
        for v in obj:
            p = _find_price(v)
            if p:
                return p
    return None


def extract_price(html: str) -> float | None:
    """Saca el precio de la pagina. Prioriza JSON-LD (dato estructurado, fiable);
    si no, cae a un regex sobre "price": ..."""
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                         html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:  # noqa: BLE001
            continue
        price = _find_price(data)
        if price and 1 < price < 100000:
            return price
    # Fallback 1: microdata (itemprop="price" content="170.00"), p.ej. Cuylás
    m = re.search(r'itemprop="price"[^>]*content="([0-9]+(?:[.,][0-9]{1,2})?)"', html)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            pass
    # Fallback 2: cualquier "price": ... en JSON embebido
    m = re.search(r'"price"\s*:\s*"?([0-9]+(?:[.,][0-9]{1,2})?)', html)
    if m:
        try:
            return float(m.group(1).replace(",", "."))
        except ValueError:
            return None
    return None


def extract_size_info(html: str, size_label: str) -> dict | None:
    """Busca el selector de tallas embebido ("sizes":[{...}]) que usa la tienda
    oficial de NNormal/Camper y devuelve disponibilidad/cantidad/precio de la
    talla pedida (etiqueta UK, p.ej. "8.5" = EU 42 2/3). None si no hay selector."""
    for m in re.finditer(r'"sizes"\s*:\s*\[', html):
        start = m.end() - 1
        depth, end = 0, None
        for k in range(start, min(len(html), start + 40000)):
            c = html[k]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
        if end is None:
            continue
        try:
            arr = json.loads(html[start:end])
        except Exception:  # noqa: BLE001
            continue
        for s in arr:
            if isinstance(s, dict) and str(s.get("value")) == str(size_label):
                price = None
                praw = s.get("price")
                if isinstance(praw, dict):
                    try:
                        price = float(praw.get("current"))
                    except (TypeError, ValueError):
                        pass
                return {
                    "available": bool(s.get("available")),
                    "quantity": s.get("quantity"),
                    "price": price,
                }
    return None


def fetch_page(page, url: str, debug_name: str = "", save_debug: bool = False) -> str | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        html = page.content()
    except Exception as e:  # noqa: BLE001
        log.warning("Error al cargar %s: %s", url, e)
        return None
    if save_debug and debug_name:
        with open(os.path.join(BASE_DIR, f"debug_{debug_name}.html"),
                  "w", encoding="utf-8") as f:
            f.write(html)
    return html


# --------------------------------------------------------------------------- #
# Pasada
# --------------------------------------------------------------------------- #

def run_once(cfg: dict, token: str, chat_id: str,
             debug: bool = False, test_mode: bool = False) -> None:
    state = load_state()
    products = cfg.get("products", [])
    alerts = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT, locale="es-ES",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()
        for i, prod in enumerate(products):
            name = prod.get("name", prod["url"])
            url = prod["url"]
            target = prod.get("target_price")
            size = prod.get("size_uk")   # talla UK del selector oficial (8.5 = EU 42 2/3)

            html = fetch_page(page, url, debug_name=str(i), save_debug=debug)
            price = extract_price(html) if html else None
            size_info = extract_size_info(html, size) if (html and size) else None
            if size_info and size_info.get("price"):
                price = size_info["price"]   # precio de TU talla, si la web lo da

            if price is None:
                log.warning("[%s] sin precio (¿agotado o cambió la web?)", name)
                if test_mode:
                    send_telegram(token, chat_id,
                                  f"⚠️ <b>{name}</b>\nNo he podido leer el precio "
                                  f"ahora mismo.\n{url}")
                continue

            entry = state.get(url, {})
            prev = entry.get("last_price")
            hist_min = entry.get("min_price", price)
            prev_avail = entry.get("size_available")
            avail = size_info.get("available") if size_info else None
            size_txt = f" | talla {size} UK: {'✅ en stock' if avail else '❌ agotada'}" if size_info else ""
            log.info("[%s] %.2f€ (antes %s)%s", name, price,
                     f"{prev:.2f}€" if prev else "—", size_txt)

            if test_mode:
                send_telegram(token, chat_id,
                              f"🔎 <b>{name}</b>\n💶 Precio ahora: <b>{price:.2f}€</b>"
                              + (f"\n👟 Talla {size} UK (EU 42 2/3): "
                                 f"{'✅ en stock' if avail else '❌ agotada'}" if size_info else "")
                              + (f"\n🎯 Objetivo: {target}€" if target else "")
                              + f'\n\n<a href="{url}">Ver producto</a>')
            else:
                # Avisos de stock de TU talla (solo si la web da info de talla)
                if size_info and prev_avail is not None and avail != prev_avail:
                    alerts += 1
                    if avail:
                        stock_msg = (f"✅ <b>¡Tu talla vuelve a estar disponible!</b> "
                                     f"(talla {size} UK / EU 42 2/3) a {price:.2f}€")
                    else:
                        stock_msg = f"❌ Tu talla ({size} UK / EU 42 2/3) se ha agotado."
                    send_telegram(token, chat_id,
                                  f"👟 <b>{name}</b>\n{stock_msg}\n\n"
                                  f'<a href="{url}">Ver producto</a>')

                # Avisos de precio: solo si tu talla esta disponible (o no hay info de talla)
                if avail is not False:
                    dropped = prev is not None and price < prev
                    below_target = target is not None and price <= target
                    if dropped or below_target:
                        alerts += 1
                        if dropped:
                            diff = prev - price
                            motivo = f"📉 ¡Ha bajado! {prev:.2f}€ → <b>{price:.2f}€</b> (−{diff:.2f}€)"
                        else:
                            motivo = f"🎯 Por debajo de tu objetivo de {target}€"
                        extra = "  🏆 mínimo histórico" if price <= hist_min else ""
                        talla_l = f"\n👟 Talla {size} UK (EU 42 2/3) en stock" if size_info else ""
                        send_telegram(token, chat_id,
                                      f"👟 <b>{name}</b>\n{motivo}{extra}{talla_l}\n\n"
                                      f'<a href="{url}">Comprar</a>')
                        log.info("AVISO: %s a %.2f€", name, price)

            state[url] = {
                "name": name,
                "last_price": price,
                "min_price": min(hist_min, price),
                "size_available": avail,
            }
        browser.close()

    if not test_mode:
        save_state(state)
    log.info("Pasada terminada: %d productos, %d avisos.",
             len(products), alerts if not test_mode else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bot vigila-precios")
    parser.add_argument("--once", action="store_true", help="una pasada y salir")
    parser.add_argument("--test", action="store_true",
                        help="manda el precio actual de cada producto (comprobar envio)")
    parser.add_argument("--debug", action="store_true", help="guarda el HTML de cada pagina")
    args = parser.parse_args()

    load_dotenv(os.path.join(BASE_DIR, ".env"))
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID en .env.")
        sys.exit(1)

    with open(CONFIG_FILE, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    if args.test:
        run_once(cfg, token, chat_id, debug=args.debug, test_mode=True)
        return
    if args.once:
        run_once(cfg, token, chat_id, debug=args.debug)
        return

    interval = cfg.get("poll_interval_minutes", 180) * 60
    log.info("Bucle: revision cada %d min. Ctrl+C para parar.",
             cfg.get("poll_interval_minutes", 180))
    while True:
        try:
            run_once(cfg, token, chat_id, debug=args.debug)
        except KeyboardInterrupt:
            log.info("Parado por el usuario.")
            break
        except Exception as e:  # noqa: BLE001
            log.exception("Error en la pasada: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
