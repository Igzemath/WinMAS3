"""
Windows 11 ISO Checker & Downloader
- Vérifie l'activation Windows (registre + PowerShell)
- Permet d'activer Windows via HWID (massgrave.dev/get)
- Charge automatiquement les ISOs Windows 11 depuis massgrave.dev
- 3 sélecteurs en cascade : Version → Édition → Langue
- Routage par hébergeur :
    zerofs.link     → curl_cffi → FlareSolverr (auto start/stop) → Playwright headed
    buzzheavier.com → Playwright headless
- Téléchargement via aria2c (multi-connexions) + fallback requests
- Configuration externe via config.ini
"""

import subprocess
import threading
import re
import os
import sys
import time
import shutil
import winreg
import atexit
import configparser
import json
import requests
from bs4 import BeautifulSoup, Tag
import customtkinter as ctk
from tkinter import filedialog, messagebox
import webbrowser
import asyncio
from playwright.async_api import async_playwright

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ──────────────────────────────────────────────
# Constantes configurables
# ──────────────────────────────────────────────

# Timeout Playwright headed pour zerofs.link (secondes)
ZEROFS_HEADED_TIMEOUT_S = 180

# Port FlareSolverr local
FLARESOLVERR_PORT = 8191
FLARESOLVERR_URL  = f"http://localhost:{FLARESOLVERR_PORT}/v1"

# Délai max démarrage FlareSolverr (secondes)
FLARESOLVERR_START_TIMEOUT_S = 30

# Connexions aria2c
ARIA2_CONNECTIONS = 16
ARIA2_SPLIT       = 16

# Taille chunk requests fallback
CHUNK_SIZE = 1024 * 1024

# ──────────────────────────────────────────────
# Debug / logging terminal
# ──────────────────────────────────────────────

DEBUG = True


def dbg(tag: str, msg: str) -> None:
    if not DEBUG:
        return
    tag_colors = {
        "INFO":         "\033[36m",
        "OK":           "\033[32m",
        "WARN":         "\033[33m",
        "ERROR":        "\033[31m",
        "STEP":         "\033[35m",
        "NET":          "\033[34m",
        "DL":           "\033[96m",
        "PLAYWRIGHT":   "\033[95m",
        "ARIA2":        "\033[92m",
        "SCRAPE":       "\033[94m",
        "CURL_CFFI":    "\033[93m",
        "FLARESOLVERR": "\033[91m",
        "ZEROFS":       "\033[97m",
        "BUZZHEAVY":    "\033[96m",
        "ROUTE":        "\033[35m",
    }
    reset   = "\033[0m"
    color   = tag_colors.get(tag, "")
    ts      = time.strftime("%H:%M:%S")
    tag_fmt = f"[{tag}]".ljust(14)
    print(f"\033[90m{ts}\033[0m {color}{tag_fmt}{reset} {msg}", flush=True)


def dbg_sep(title: str = "") -> None:
    if not DEBUG:
        return
    if title:
        pad = max(0, (58 - len(title)) // 2)
        print(f"\033[35m{'━'*pad} {title} {'━'*pad}\033[0m", flush=True)
    else:
        print(f"\033[35m{'━'*60}\033[0m", flush=True)


# ──────────────────────────────────────────────
# Vérification / installation Playwright
# ──────────────────────────────────────────────

def check_playwright_browsers() -> dict:
    dbg("PLAYWRIGHT", "Vérification navigateurs Playwright…")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe    = p.chromium.executable_path
            exists = os.path.isfile(exe)
            dbg("PLAYWRIGHT", f"Chemin chromium : {exe}")
            if exists:
                dbg("OK", "Chromium Playwright ✓")
                return {"ok": True, "error": "", "chromium_path": exe}
            msg = f"Exécutable introuvable : {exe}"
            dbg("ERROR", msg)
            return {"ok": False, "error": msg, "chromium_path": exe}
    except Exception as e:
        msg = str(e)
        dbg("ERROR", f"Erreur vérification Playwright : {msg}")
        return {"ok": False, "error": msg, "chromium_path": ""}


def install_playwright_browsers(ui_callback=None) -> bool:
    dbg("PLAYWRIGHT", "Lancement 'playwright install chromium'…")
    if ui_callback:
        ui_callback("status", ("Installation Chromium (Playwright)…", COL["status_warn"]))
    try:
        cmd     = [sys.executable, "-m", "playwright", "install", "chromium"]
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding="utf-8", errors="replace",
        )
        for line in iter(process.stdout.readline, ""):
            line = line.strip()
            if line:
                dbg("PLAYWRIGHT", f"  install> {line}")
                if ui_callback:
                    ui_callback("status", (f"Playwright: {line}", COL["status_warn"]))
        process.wait()
        if process.returncode == 0:
            dbg("OK", "playwright install chromium — succès")
            if ui_callback:
                ui_callback("status", ("Chromium installé avec succès", COL["status_ok"]))
            return True
        dbg("ERROR", f"playwright install chromium — code {process.returncode}")
        if ui_callback:
            ui_callback("status", (
                f"Échec installation Chromium (code {process.returncode})",
                COL["status_err"]
            ))
        return False
    except Exception as e:
        dbg("ERROR", f"Erreur installation : {e}")
        if ui_callback:
            ui_callback("status", (f"Erreur installation : {e}", COL["status_err"]))
        return False


# ──────────────────────────────────────────────
# FlareSolverr — gestion du processus
# ──────────────────────────────────────────────

class FlareSolverrManager:
    """
    Gère le cycle de vie du binaire FlareSolverr :
    - Détection (à côté du script ou PATH)
    - Démarrage automatique
    - Vérification de disponibilité (ping HTTP)
    - Arrêt propre à la fermeture
    """

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._available: bool | None = None
        self._binary: str = ""
        self._lock = threading.Lock()
        atexit.register(self.stop)

    def find_binary(self) -> str:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(script_dir, "flaresolverr.exe"),
            os.path.join(script_dir, "FlareSolverr.exe"),
            os.path.join(script_dir, "flaresolverr", "flaresolverr.exe"),
            os.path.join(script_dir, "FlareSolverr", "FlareSolverr.exe"),
        ]
        in_path = shutil.which("flaresolverr") or shutil.which("FlareSolverr")
        if in_path:
            candidates.insert(0, in_path)
        for path in candidates:
            if os.path.isfile(path):
                dbg("FLARESOLVERR", f"Binaire trouvé : {path}")
                return path
        dbg("WARN", "FlareSolverr binaire introuvable :")
        for c in candidates:
            dbg("WARN", f"  {c}")
        return ""

    def _ping(self) -> bool:
        try:
            r = requests.get(f"http://localhost:{FLARESOLVERR_PORT}/", timeout=3)
            dbg("FLARESOLVERR", f"Ping HTTP {r.status_code}")
            return r.status_code < 500
        except Exception:
            return False

    def start(self) -> bool:
        with self._lock:
            if self._process and self._process.poll() is None:
                dbg("FLARESOLVERR", f"Processus déjà en cours (PID {self._process.pid})")
                return True
            if self._ping():
                dbg("FLARESOLVERR", "Instance externe déjà disponible")
                self._available = True
                return True
            binary = self.find_binary()
            if not binary:
                dbg("ERROR", "FlareSolverr introuvable — impossible de démarrer")
                self._available = False
                return False
            self._binary = binary
            dbg("FLARESOLVERR", f"Démarrage : {binary}")
            dbg("FLARESOLVERR", f"Port : {FLARESOLVERR_PORT}")
            env = os.environ.copy()
            env["PORT"]      = str(FLARESOLVERR_PORT)
            env["LOG_LEVEL"] = "info"
            try:
                self._process = subprocess.Popen(
                    [binary],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                    encoding="utf-8", errors="replace",
                    env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                dbg("FLARESOLVERR", f"PID : {self._process.pid}")
                threading.Thread(target=self._log_reader, daemon=True).start()
                deadline = time.time() + FLARESOLVERR_START_TIMEOUT_S
                attempt  = 0
                while time.time() < deadline:
                    attempt += 1
                    if self._process.poll() is not None:
                        dbg("ERROR", "FlareSolverr arrêté prématurément")
                        self._available = False
                        return False
                    if self._ping():
                        elapsed = time.time() - (deadline - FLARESOLVERR_START_TIMEOUT_S)
                        dbg("OK", f"FlareSolverr prêt en {elapsed:.1f}s (tentative #{attempt})")
                        self._available = True
                        return True
                    time.sleep(1)
                dbg("ERROR", f"FlareSolverr non disponible après {FLARESOLVERR_START_TIMEOUT_S}s")
                self._available = False
                self.stop()
                return False
            except Exception as e:
                dbg("ERROR", f"Erreur démarrage FlareSolverr : {e}")
                self._available = False
                return False

    def _log_reader(self):
        if not self._process or not self._process.stdout:
            return
        try:
            for line in iter(self._process.stdout.readline, ""):
                line = line.strip()
                if line:
                    dbg("FLARESOLVERR", f"  LOG> {line}")
        except Exception:
            pass

    def stop(self):
        with self._lock:
            if self._process and self._process.poll() is None:
                dbg("FLARESOLVERR", f"Arrêt processus PID {self._process.pid}…")
                try:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                        dbg("FLARESOLVERR", "Processus terminé proprement")
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        dbg("WARN", "Processus tué (SIGKILL)")
                except Exception as e:
                    dbg("ERROR", f"Erreur arrêt : {e}")
                finally:
                    self._process  = None
                    self._available = False

    def resolve(self, url: str, ui_callback=None) -> dict:
        dbg_sep("FlareSolverr resolve")
        dbg("FLARESOLVERR", f"URL : {url}")
        if ui_callback:
            ui_callback("status", ("FlareSolverr — résolution Cloudflare…", COL["status_warn"]))
        payload = {
            "cmd":        "request.get",
            "url":        url,
            "maxTimeout": ZEROFS_HEADED_TIMEOUT_S * 1000,
        }
        try:
            dbg("FLARESOLVERR", f"POST {FLARESOLVERR_URL}")
            resp = requests.post(
                FLARESOLVERR_URL,
                json=payload,
                timeout=ZEROFS_HEADED_TIMEOUT_S + 10,
            )
            data = resp.json()
            dbg("FLARESOLVERR", f"Status réponse : {data.get('status')}")
            if data.get("status") == "ok":
                solution     = data.get("solution", {})
                final_url    = solution.get("url", url)
                html         = solution.get("response", "")
                cookies_list = solution.get("cookies", [])
                user_agent   = solution.get("userAgent", "")
                cookie_str   = "; ".join(
                    f"{c['name']}={c['value']}" for c in cookies_list
                )
                dbg("OK",           "FlareSolverr OK")
                dbg("FLARESOLVERR", f"URL finale    : {final_url[:80]}")
                dbg("FLARESOLVERR", f"Cookies       : {len(cookies_list)}")
                dbg("FLARESOLVERR", f"User-Agent    : {user_agent[:60]}")
                dbg("FLARESOLVERR", f"HTML longueur : {len(html)}")
                return {
                    "ok":         True,
                    "html":       html,
                    "final_url":  final_url,
                    "cookies":    cookie_str,
                    "cookies_list": cookies_list,
                    "user_agent": user_agent,
                }
            else:
                msg = data.get("message", "Erreur inconnue")
                dbg("ERROR", f"FlareSolverr erreur : {msg}")
                return {
                    "ok": False, "html": "", "final_url": url,
                    "cookies": "", "cookies_list": [], "user_agent": "",
                }
        except Exception as e:
            dbg("ERROR", f"FlareSolverr exception : {e}")
            return {
                "ok": False, "html": "", "final_url": url,
                "cookies": "", "cookies_list": [], "user_agent": "",
            }


# Instance globale FlareSolverr
_flaresolverr = FlareSolverrManager()


# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────

DEFAULT_CONFIG = {
    "General": {"activation_method": "HWID"},
    "ISO": {
        "default_version":  "",
        "default_edition":  "Consumer",
        "default_language": "fr-fr",
    },
}

CONFIG_FILENAME = "config.ini"


def get_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


def load_config() -> configparser.ConfigParser:
    config      = configparser.ConfigParser()
    config_path = get_config_path()
    for section, values in DEFAULT_CONFIG.items():
        if not config.has_section(section):
            config.add_section(section)
        for key, value in values.items():
            config.set(section, key, str(value))
    if os.path.isfile(config_path):
        config.read(config_path, encoding="utf-8")
        modified = False
        for section, values in DEFAULT_CONFIG.items():
            if not config.has_section(section):
                config.add_section(section)
                modified = True
            for key, value in values.items():
                if not config.has_option(section, key):
                    config.set(section, key, str(value))
                    modified = True
        if modified:
            save_config(config)
    else:
        save_config(config)
    return config


def save_config(config: configparser.ConfigParser) -> None:
    config_path = get_config_path()
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("# ══════════════════════════════════════════════\n")
        f.write("# Windows 11 ISO Checker & Downloader — Configuration\n")
        f.write("# ══════════════════════════════════════════════\n\n")
        f.write("[General]\n")
        f.write("# HWID | Ohook | KMS38 | KMS\n")
        f.write(f"activation_method = {config.get('General', 'activation_method', fallback='HWID')}\n\n")
        f.write("[ISO]\n")
        f.write("# Version par défaut (ex: 24H2) — vide = plus récente\n")
        f.write(f"default_version = {config.get('ISO', 'default_version', fallback='')}\n")
        f.write("# Consumer | Business | Enterprise | Enterprise LTSC | Education\n")
        f.write(f"default_edition = {config.get('ISO', 'default_edition', fallback='Consumer')}\n")
        f.write("# ex: fr-fr | en-us | de-de | es-es\n")
        f.write(f"default_language = {config.get('ISO', 'default_language', fallback='fr-fr')}\n\n")


# ──────────────────────────────────────────────
# Palette de couleurs
# ──────────────────────────────────────────────

COL = {
    "bg_app":         "#0a0a0a",
    "bg_card":        "#141414",
    "bg_card_alt":    "#1a1a1a",
    "bg_input":       "#1e1e1e",
    "bg_result":      "#111111",
    "border":         "#2a2a2a",
    "border_light":   "#333333",
    "text_primary":   "#e0e0e0",
    "text_secondary": "#999999",
    "text_muted":     "#666666",
    "text_dim":       "#555555",
    "accent_blue":    "#4a90d9",
    "accent_blue_h":  "#3a7bc8",
    "accent_green":   "#5cb85c",
    "accent_green_h": "#4a9a4a",
    "accent_amber":   "#d4a843",
    "accent_amber_h": "#b8922e",
    "accent_red":     "#d9534f",
    "accent_red_h":   "#c9433f",
    "accent_purple":  "#7c6fbf",
    "accent_purple_h":"#6a5daa",
    "accent_teal":    "#5bc0be",
    "progress_bg":    "#1e1e1e",
    "progress_fill":  "#5cb85c",
    "btn_neutral":    "#2a2a2a",
    "btn_neutral_h":  "#383838",
    "status_ok":      "#5cb85c",
    "status_warn":    "#d4a843",
    "status_err":     "#d9534f",
    "status_info":    "#4a90d9",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

PAGE_URL = "https://massgrave.dev/windows_11_links"

# ──────────────────────────────────────────────
# Hébergeurs
# ──────────────────────────────────────────────

HOSTERS_RE = re.compile(r"zerofs\.link|buzzheavier\.com", re.IGNORECASE)

# ── Dans HOSTERS_CONFIG ──────────────────────────────────────────────────
HOSTERS_CONFIG = {
    "zerofs.link": {
        "pattern": re.compile(r"zerofs\.link", re.IGNORECASE),
        "name":    "zerofs.link",
        "chain":   ["curl_cffi", "flaresolverr", "playwright_headed"],
        "can_auto_download": True,
        "page_timeout":      60000,
        "download_timeout":  30000,
    },
    "buzzheavier.com": {
        "pattern": re.compile(r"buzzheavier\.com", re.IGNORECASE),
        "name":    "buzzheavier.com",
        "chain":   ["playwright_headed"],   # ← headed comme zerofs
        "can_auto_download": True,
        "cf_wait_ms":        2000,
        "page_timeout":      60000,
        "download_timeout":  30000,
    },
}

HOSTERS_PRIORITY = ["buzzheavier.com", "zerofs.link"]


def get_hoster_config(url: str) -> dict | None:
    for cfg in HOSTERS_CONFIG.values():
        if cfg["pattern"].search(url):
            return cfg
    return None


ACTIVATION_METHODS = {
    "HWID": {
        "label":    "HWID (permanent)",
        "desc":     "Activation permanente liée au matériel.\nGratuit, survit aux réinstallations.",
        "cmd_flag": "/HWID",
    },
    "Ohook": {
        "label":    "Ohook (Office)",
        "desc":     "Active Microsoft Office.\nFonctionne pour Office 2013-2024.",
        "cmd_flag": "/Ohook",
    },
    "KMS38": {
        "label":    "KMS38 (jusqu'en 2038)",
        "desc":     "Activation jusqu'au 19 janvier 2038.\nPas besoin de renouvellement.",
        "cmd_flag": "/KMS38",
    },
    "KMS": {
        "label":    "KMS (180 jours)",
        "desc":     "Activation classique KMS.\nSe renouvelle automatiquement.",
        "cmd_flag": "/KMS-ActAndRenewalTask",
    },
}

LANG_CODES = {
    "ar-sa": "Arabic",           "bg-bg": "Bulgarian",
    "cs-cz": "Czech",            "da-dk": "Danish",
    "de-de": "German",           "el-gr": "Greek",
    "en-gb": "English (UK)",     "en-us": "English (US)",
    "es-es": "Spanish (Spain)",  "es-mx": "Spanish (Mexico)",
    "et-ee": "Estonian",         "fi-fi": "Finnish",
    "fr-ca": "French (Canada)",  "fr-fr": "French (France)",
    "he-il": "Hebrew",           "hr-hr": "Croatian",
    "hu-hu": "Hungarian",        "it-it": "Italian",
    "ja-jp": "Japanese",         "ko-kr": "Korean",
    "lt-lt": "Lithuanian",       "lv-lv": "Latvian",
    "nb-no": "Norwegian",        "nl-nl": "Dutch",
    "pl-pl": "Polish",           "pt-br": "Portuguese (Brazil)",
    "pt-pt": "Portuguese (Portugal)",
    "ro-ro": "Romanian",         "ru-ru": "Russian",
    "sk-sk": "Slovak",           "sl-si": "Slovenian",
    "sr-latn-rs": "Serbian (Latin)",
    "sv-se": "Swedish",          "th-th": "Thai",
    "tr-tr": "Turkish",          "uk-ua": "Ukrainian",
    "zh-cn": "Chinese (Simplified)",
    "zh-tw": "Chinese (Traditional)",
}


# ──────────────────────────────────────────────
# Utilitaires
# ──────────────────────────────────────────────

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 ** 3:
        return f"{size_bytes / 1024**2:.1f} MB"
    else:
        return f"{size_bytes / 1024**3:.2f} GB"


def find_aria2c() -> str | None:
    found = shutil.which("aria2c")
    if found:
        dbg("INFO", f"aria2c trouvé (PATH) : {found}")
        return found
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aria2c.exe")
    if os.path.isfile(local):
        dbg("INFO", f"aria2c trouvé (local) : {local}")
        return local
    for pf in [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]:
        if pf:
            candidate = os.path.join(pf, "aria2", "aria2c.exe")
            if os.path.isfile(candidate):
                dbg("INFO", f"aria2c trouvé (ProgramFiles) : {candidate}")
                return candidate
    dbg("WARN", "aria2c introuvable — fallback requests")
    return None


def _zerofs_file_id(url: str) -> str:
    """https://zerofs.link/f/boErQPX/  →  boErQPX"""
    m = re.search(r"/f/([^/]+)/?", url)
    return m.group(1) if m else ""


# ──────────────────────────────────────────────
# Activation Windows
# ──────────────────────────────────────────────

def check_windows_activation() -> dict:
    dbg("INFO", "Vérification activation Windows…")
    info = {"nom_os": "", "etat_licence": "", "product_id": "", "active": False}
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
        ) as key:
            product_name = winreg.QueryValueEx(key, "ProductName")[0]
            try:
                build = int(winreg.QueryValueEx(key, "CurrentBuildNumber")[0])
            except (ValueError, FileNotFoundError):
                build = 0
            try:
                display_version = winreg.QueryValueEx(key, "DisplayVersion")[0]
            except FileNotFoundError:
                display_version = ""
            if build >= 22000 and "Windows 10" in product_name:
                product_name = product_name.replace("Windows 10", "Windows 11")
            info["nom_os"] = (
                f"{product_name} ({display_version}, Build {build})"
                if display_version else f"{product_name} (Build {build})"
            )
            try:
                info["product_id"] = winreg.QueryValueEx(key, "ProductId")[0]
            except FileNotFoundError:
                pass
            dbg("INFO", f"OS : {info['nom_os']}")
    except Exception as e:
        info["nom_os"] = "Lecture registre impossible"
        dbg("ERROR", f"Registre : {e}")
    try:
        cmd = (
            'powershell -NoProfile -Command "'
            "Get-CimInstance -ClassName SoftwareLicensingProduct "
            "-Filter \\\"ApplicationId='55c92734-d682-4d71-983e-d6ec3f16059f' "
            "AND PartialProductKey IS NOT NULL\\\" "
            "| Select-Object -First 1 -ExpandProperty LicenseStatus\""
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            shell=True, creationflags=0x08000000
        )
        code = result.stdout.strip()
        dbg("INFO", f"LicenseStatus : {repr(code)}")
        status_map = {
            "0": "Non licencié",          "1": "Activé (Licencié)",
            "2": "Grâce initiale",        "3": "Grâce supplémentaire",
            "4": "Grâce non authentique", "5": "Notification",
            "6": "Grâce étendue",
        }
        info["etat_licence"] = status_map.get(code, f"Code inconnu : {code}")
        info["active"]       = (code == "1")
        dbg("INFO" if info["active"] else "WARN", f"Licence : {info['etat_licence']}")
    except subprocess.TimeoutExpired:
        info["etat_licence"] = "Timeout PowerShell"
        dbg("ERROR", "Timeout PowerShell")
    except Exception as e:
        info["etat_licence"] = f"Erreur : {e}"
        dbg("ERROR", f"PowerShell : {e}")
    return info


def run_activation(method_key: str, callback) -> None:
    method     = ACTIVATION_METHODS[method_key]
    flag       = method["cmd_flag"]
    dbg("STEP", f"Activation MAS — {method['label']} ({flag})")
    ps_inner   = f"& ([ScriptBlock]::Create((irm https://get.activated.win))) {flag} /S"
    ps_escaped = ps_inner.replace('"', '\\"')
    cmd = [
        "powershell", "-NoProfile", "-Command",
        f'Start-Process powershell -Verb RunAs -Wait '
        f'-ArgumentList \'-NoProfile\',\'-Command\',\"{ps_escaped}\"'
    ]
    callback("activation_status", (f"Lancement ({method['label']})…", COL["status_warn"]))
    try:
        process = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300, creationflags=0x08000000
        )
        dbg("INFO", f"Code retour activation : {process.returncode}")
        if process.returncode == 0:
            callback("activation_status",
                ("Activation terminée — revérification…", COL["status_info"]))
        else:
            stderr = (process.stderr or "") + (process.stdout or "")
            if any(kw in stderr.lower() for kw in ["canceled", "annul", "refused"]):
                callback("activation_status",
                    ("Élévation admin refusée", COL["status_warn"]))
            else:
                callback("activation_status",
                    (f"Code retour {process.returncode}", COL["status_err"]))
    except subprocess.TimeoutExpired:
        callback("activation_status", ("Timeout > 5min", COL["status_warn"]))
    except Exception as e:
        callback("activation_status", (f"Erreur : {e}", COL["status_err"]))


# ──────────────────────────────────────────────
# Scraping ISOs
# ──────────────────────────────────────────────

def fetch_all_isos() -> list[dict]:
    dbg_sep("Scraping ISOs")
    dbg("SCRAPE", f"GET {PAGE_URL}")
    resp = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    dbg("SCRAPE", f"HTTP {resp.status_code} — {len(resp.text)} chars")
    soup      = BeautifulSoup(resp.text, "html.parser")
    all_links = soup.find_all("a", href=HOSTERS_RE)
    dbg("SCRAPE", f"Liens hébergeurs : {len(all_links)}")

    iso_links_by_file: dict[str, dict[str, str]] = {}
    for a_tag in all_links:
        href       = a_tag["href"].strip()
        filename   = _find_iso_filename(a_tag)
        if not filename:
            continue
        parsed = _parse_iso_filename(filename)
        if not parsed:
            continue
        hoster_cfg = get_hoster_config(href)
        if not hoster_cfg:
            continue
        hoster_name = hoster_cfg["name"]
        if filename not in iso_links_by_file:
            iso_links_by_file[filename] = {}
        if hoster_name not in iso_links_by_file[filename]:
            iso_links_by_file[filename][hoster_name] = href
            dbg("SCRAPE", f"  + {filename} [{hoster_name}]")

    dbg("SCRAPE", f"ISO uniques : {len(iso_links_by_file)}")
    all_isos     = []
    seen_primary = set()

    for filename, links_by_hoster in iso_links_by_file.items():
        parsed = _parse_iso_filename(filename)
        if not parsed:
            continue
        sorted_links = [
            {"hoster": name, "url": links_by_hoster[name]}
            for name in HOSTERS_PRIORITY
            if name in links_by_hoster
        ]
        if not sorted_links:
            continue
        primary     = sorted_links[0]
        primary_url = primary["url"]
        if primary_url in seen_primary:
            continue
        seen_primary.add(primary_url)
        iso_entry = {
            "nom_fichier":       filename,
            "langue_code":       parsed["langue_code"],
            "langue_nom":        parsed["langue_nom"],
            "edition":           parsed["edition"],
            "version":           parsed["version"],
            "arch":              parsed["arch"],
            "lien":              primary_url,
            "hoster":            primary["hoster"],
            "liens_alternatifs": sorted_links[1:],
            "can_download":      True,
        }
        hosters_str = " + ".join(l["hoster"] for l in sorted_links)
        dbg("SCRAPE", (
            f"  ✓ {filename} | {parsed['version']} | {parsed['edition']} "
            f"| {parsed['langue_code']} | [{hosters_str}]"
        ))
        all_isos.append(iso_entry)

    dbg("OK", f"fetch_all_isos — {len(all_isos)} ISO(s)")
    return all_isos


def _find_iso_filename(a_tag: Tag) -> str:
    for source in [a_tag.get_text(strip=True), a_tag.get("title", "")]:
        if source and ".iso" in source.lower():
            m = re.search(r"[\w._-]+\.iso", source, re.IGNORECASE)
            if m:
                return m.group(0).lower()
    for parent_tag in ["tr", "li", "p", "div", "dd", "section"]:
        parent = a_tag.find_parent(parent_tag)
        if parent:
            m = re.search(r"[\w._-]+\.iso", parent.get_text(), re.IGNORECASE)
            if m:
                return m.group(0).lower()
    if a_tag.parent:
        m = re.search(r"[\w._-]+\.iso", a_tag.parent.get_text(), re.IGNORECASE)
        if m:
            return m.group(0).lower()
    for sib in [a_tag.previous_sibling, a_tag.next_sibling]:
        if sib:
            s = sib.string if hasattr(sib, "string") else str(sib)
            if s:
                m = re.search(r"[\w._-]+\.iso", s, re.IGNORECASE)
                if m:
                    return m.group(0).lower()
    return ""


def _parse_iso_filename(filename: str) -> dict | None:
    filename_lower = filename.lower()
    lang_match     = re.match(
        r"^([a-z]{2}(?:-[a-z]{2,4}(?:-[a-z]{2})?)?)_", filename_lower
    )
    if not lang_match:
        return None
    langue_code = lang_match.group(1)
    langue_nom  = LANG_CODES.get(langue_code, langue_code.upper())
    ver_match   = re.search(r"(\d{2}h[12])", filename_lower)
    version     = ver_match.group(1).upper() if ver_match else "Inconnu"
    edition     = "Autre"
    for pattern, label in [
        (r"iot_enterprise_ltsc", "IoT Enterprise LTSC"),
        (r"iot_enterprise",      "IoT Enterprise"),
        (r"enterprise_ltsc",     "Enterprise LTSC"),
        (r"consumer",            "Consumer"),
        (r"business",            "Business"),
        (r"enterprise",          "Enterprise"),
        (r"education",           "Education"),
        (r"iot",                 "IoT"),
    ]:
        if re.search(pattern, filename_lower):
            edition = label
            break
    arch            = "ARM64" if "arm64" in filename_lower else "x64"
    display_edition = f"{edition} ({arch})" if arch == "ARM64" else edition
    return {
        "langue_code": langue_code,
        "langue_nom":  langue_nom,
        "edition":     display_edition,
        "version":     version,
        "arch":        arch,
    }


# ──────────────────────────────────────────────
# Script d'évasion Playwright
# ──────────────────────────────────────────────

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined, configurable: true
});
window.chrome = {
    runtime: {
        id: undefined, connect: () => {}, sendMessage: () => {},
        onMessage: { addListener: () => {}, removeListener: () => {} },
    },
    loadTimes: () => ({
        requestTime: Date.now()/1000, startLoadTime: Date.now()/1000,
        commitLoadTime: Date.now()/1000, finishDocumentLoadTime: Date.now()/1000,
        finishLoadTime: Date.now()/1000, firstPaintTime: Date.now()/1000,
        firstPaintAfterLoadTime: 0, navigationType: 'Other',
        wasFetchedViaSpdy: false, wasNpnNegotiated: false,
        npnNegotiatedProtocol: 'unknown', wasAlternateProtocolAvailable: false,
        connectionInfo: 'unknown',
    }),
    csi: () => ({ startE: Date.now(), onloadT: Date.now(), pageT: 1200, tran: 15 }),
    app: {
        isInstalled: false,
        InstallState: { DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed' },
        RunningState: { CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running' },
    },
};
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) =>
    p.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : _origQuery(p);
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const mk = (name, fn, desc, mt) => {
            const pl = Object.create(Plugin.prototype);
            Object.defineProperty(pl, 'name',        { get: () => name });
            Object.defineProperty(pl, 'filename',    { get: () => fn });
            Object.defineProperty(pl, 'description', { get: () => desc });
            Object.defineProperty(pl, 'length',      { get: () => 1 });
            const m = Object.create(MimeType.prototype);
            Object.defineProperty(m, 'type',          { get: () => mt });
            Object.defineProperty(m, 'suffixes',      { get: () => 'pdf' });
            Object.defineProperty(m, 'description',   { get: () => '' });
            Object.defineProperty(m, 'enabledPlugin', { get: () => pl });
            pl[0] = m;
            return pl;
        };
        const plugins = [
            mk('PDF Viewer',                'internal-pdf-viewer','','application/pdf'),
            mk('Chrome PDF Viewer',         'internal-pdf-viewer','','application/pdf'),
            mk('Chromium PDF Viewer',       'internal-pdf-viewer','','application/pdf'),
            mk('Microsoft Edge PDF Viewer', 'internal-pdf-viewer','','application/pdf'),
            mk('WebKit built-in PDF',       'internal-pdf-viewer','','application/pdf'),
        ];
        const pa = Object.create(PluginArray.prototype);
        plugins.forEach((p,i) => { pa[i] = p; });
        Object.defineProperty(pa, 'length', { get: () => plugins.length });
        pa.item      = i    => plugins[i] || null;
        pa.namedItem = name => plugins.find(p => p.name === name) || null;
        pa.refresh   = ()   => {};
        return pa;
    }, configurable: true,
});
Object.defineProperty(navigator, 'languages',
    { get: () => ['fr-FR','fr','en-US','en'], configurable: true });
Object.defineProperty(navigator, 'hardwareConcurrency',
    { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'deviceMemory',
    { get: () => 8, configurable: true });
Object.defineProperty(navigator, 'vendor',
    { get: () => 'Google Inc.', configurable: true });
const _getParam = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return _getParam.call(this, p);
};
"""


# ──────────────────────────────────────────────
# Résolution zerofs.link
# ──────────────────────────────────────────────

# ── Stratégie 1 : curl_cffi ──

def _zerofs_curl_cffi(hoster_url: str, ui_callback) -> dict | None:
    """
    Tente GET page + POST /download sans navigateur.
    Fonctionne si le serveur accepte les cookies CF sans token Turnstile JS.
    En pratique : teste rapidement avant de passer à FlareSolverr.
    """
    dbg_sep("zerofs — curl_cffi")
    ui_callback("status", ("zerofs.link — tentative curl_cffi…", COL["status_info"]))

    try:
        from curl_cffi import requests as cf_req
    except ImportError:
        dbg("WARN", "curl_cffi non installé (pip install curl_cffi) — skip")
        return None

    file_id = _zerofs_file_id(hoster_url)
    if not file_id:
        dbg("WARN", f"curl_cffi : file_id introuvable dans {hoster_url}")
        return None

    try:
        session = cf_req.Session(impersonate="chrome124")

        # GET page principale
        dbg("CURL_CFFI", f"GET {hoster_url}")
        resp_get = session.get(
            hoster_url,
            headers={
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language":           "fr-FR,fr;q=0.9,en-US;q=0.8",
                "Accept-Encoding":           "gzip, deflate, br",
                "Sec-Fetch-Dest":            "document",
                "Sec-Fetch-Mode":            "navigate",
                "Sec-Fetch-Site":            "none",
                "Upgrade-Insecure-Requests": "1",
            },
            timeout=30,
            allow_redirects=True,
        )
        dbg("CURL_CFFI", f"HTTP {resp_get.status_code} — {len(resp_get.text)} chars")

        if resp_get.status_code in (403, 429, 503):
            dbg("WARN", f"curl_cffi : HTTP {resp_get.status_code} — réseau CF bloqué")
            return None

        body_lower = resp_get.text.lower()
        if any(p in body_lower for p in [
            "just a moment", "checking your browser",
            "enable javascript and cookies",
            "verifying you are human",
        ]):
            dbg("WARN", "curl_cffi : challenge CF page-level — skip")
            return None

        dbg("OK", "curl_cffi : page obtenue ✓")

        # Extraire CSRF token
        soup_cf    = BeautifulSoup(resp_get.text, "html.parser")
        csrf_input = soup_cf.find("input", {"name": "csrfmiddlewaretoken"})
        csrf_token = csrf_input.get("value", "") if csrf_input else ""
        dbg("CURL_CFFI", f"CSRF : {csrf_token[:20]}… ({'OK' if csrf_token else 'ABSENT'})")
        if not csrf_token:
            dbg("WARN", "curl_cffi : CSRF absent")
            return None

        cookies_dict = dict(resp_get.cookies)
        user_token   = cookies_dict.get("usertoken", "")

        # POST /download avec token Turnstile vide (test)
        download_url = f"https://zerofs.link/f/{file_id}/download"
        dbg("CURL_CFFI", f"POST {download_url}")
        resp_post = session.post(
            download_url,
            headers={
                "Accept":         "application/json, */*",
                "Content-Type":   "application/x-www-form-urlencoded; charset=UTF-8",
                "X-CSRFToken":    csrf_token,
                "X-User-Token":   user_token,
                "Referer":        hoster_url,
                "Origin":         "https://zerofs.link",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "HX-Request":     "true",
                "HX-Current-URL": hoster_url,
                "HX-Target":      "none",
            },
            data={
                "csrfmiddlewaretoken": csrf_token,
                "turnstile_token":     "",
            },
            timeout=15,
        )
        dbg("CURL_CFFI", f"POST HTTP {resp_post.status_code}")
        dbg("CURL_CFFI", f"Body : {resp_post.text[:200]}")

        if resp_post.status_code == 200:
            try:
                data   = resp_post.json()
                dl_url = data.get("url", "")
                if dl_url:
                    dbg("OK", f"curl_cffi POST direct OK : {dl_url[:80]}")
                    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
                    ui_callback("status", ("curl_cffi — succès ✓", COL["status_ok"]))
                    return {
                        "url":     dl_url,
                        "cookies": cookie_str,
                        "referer": hoster_url,
                        "hoster":  "zerofs.link",
                    }
            except Exception:
                pass

        dbg("WARN", "curl_cffi : token Turnstile JS requis — impossible sans navigateur")
        return None

    except Exception as e:
        dbg("ERROR", f"curl_cffi exception : {e}")
        return None


# ── Stratégie 2 : FlareSolverr ──

def _zerofs_flaresolverr(hoster_url: str, ui_callback) -> dict | None:
    """
    FlareSolverr charge la page avec un vrai Chrome, résout Turnstile,
    retourne le HTML + cookies.
    On extrait le token Turnstile depuis l'input hidden, puis POST /download.
    """
    dbg_sep("zerofs — FlareSolverr")
    ui_callback("status", ("zerofs.link — démarrage FlareSolverr…", COL["status_warn"]))

    ok = _flaresolverr.start()
    if not ok:
        dbg("WARN", "FlareSolverr indisponible — skip")
        ui_callback("status", ("FlareSolverr indisponible", COL["status_warn"]))
        return None

    ui_callback("status", ("FlareSolverr — résolution Turnstile…", COL["status_warn"]))
    result = _flaresolverr.resolve(hoster_url, ui_callback)

    if not result["ok"]:
        dbg("WARN", "FlareSolverr : résolution échouée")
        return None

    html         = result["html"]
    cookies_list = result["cookies_list"]
    user_agent   = result["user_agent"] or HEADERS["User-Agent"]
    cookies_dict = {c["name"]: c["value"] for c in cookies_list}
    cookie_str   = result["cookies"]
    user_token   = cookies_dict.get("usertoken", "")

    # Parser le HTML
    soup = BeautifulSoup(html, "html.parser")

    csrf_input = soup.find("input", {"name": "csrfmiddlewaretoken"})
    csrf_token = csrf_input.get("value", "") if csrf_input else ""
    dbg("FLARESOLVERR", f"CSRF token : {csrf_token[:20]}… ({'OK' if csrf_token else 'ABSENT'})")

    ts_input = soup.find("input", {"id": "turnstile-token"})
    ts_token = ts_input.get("value", "") if ts_input else ""
    dbg("FLARESOLVERR", f"Turnstile token : {ts_token[:30] if ts_token else 'ABSENT'}")
    dbg("FLARESOLVERR", f"cf_clearance   : {'présent' if 'cf_clearance' in cookies_dict else 'absent'}")
    dbg("FLARESOLVERR", f"usertoken      : {'présent' if user_token else 'absent'}")

    if not csrf_token:
        dbg("WARN", "FlareSolverr : CSRF absent — HTML inutilisable")
        return None

    if not ts_token:
        dbg("WARN", "FlareSolverr : Turnstile token absent dans HTML")
        dbg("INFO", "FlareSolverr ne résout pas Turnstile widget → Playwright headed")
        return None

    # POST /download
    file_id      = _zerofs_file_id(hoster_url)
    download_url = f"https://zerofs.link/f/{file_id}/download"
    dbg("FLARESOLVERR", f"POST {download_url}")

    try:
        post_resp = requests.post(
            download_url,
            headers={
                "Accept":         "application/json, */*",
                "Content-Type":   "application/x-www-form-urlencoded; charset=UTF-8",
                "X-CSRFToken":    csrf_token,
                "X-User-Token":   user_token,
                "Referer":        hoster_url,
                "Origin":         "https://zerofs.link",
                "User-Agent":     user_agent,
                "Cookie":         cookie_str,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
                "HX-Request":     "true",
                "HX-Current-URL": hoster_url,
                "HX-Target":      "none",
            },
            data={
                "csrfmiddlewaretoken": csrf_token,
                "turnstile_token":     ts_token,
            },
            timeout=30,
        )
        dbg("FLARESOLVERR", f"POST HTTP {post_resp.status_code}")
        dbg("FLARESOLVERR", f"Body : {post_resp.text[:300]}")

        if post_resp.status_code == 200:
            try:
                resp_data = post_resp.json()
                dl_url    = resp_data.get("url", "")
                if dl_url:
                    dbg("OK", f"FlareSolverr POST OK : {dl_url[:80]}")
                    ui_callback("status", ("FlareSolverr — succès ✓", COL["status_ok"]))
                    return {
                        "url":     dl_url,
                        "cookies": cookie_str,
                        "referer": hoster_url,
                        "hoster":  "zerofs.link",
                    }
                else:
                    dbg("WARN", f"FlareSolverr : JSON sans 'url' : {resp_data}")
            except Exception as e:
                dbg("WARN", f"FlareSolverr : JSON invalide : {e}")
        else:
            dbg("WARN", f"FlareSolverr POST échoué : HTTP {post_resp.status_code}")

    except Exception as e:
        dbg("ERROR", f"FlareSolverr POST exception : {e}")

    return None


# ── Stratégie 3 : Playwright headed ──

async def _zerofs_playwright_headed_async(
    hoster_url: str, ui_callback
) -> dict:
    """
    Playwright headed (fenêtre visible) :
    1. Charge la page — Turnstile se résout automatiquement ou manuellement
    2. Poll #download-btn jusqu'à disabled=False (= Turnstile résolu)
    3. Clique le bouton — htmx POST /download
    4. Intercepte la réponse XHR JSON {"url": "..."} → URL directe
    """
    dbg_sep("zerofs — Playwright headed")
    dbg("PLAYWRIGHT", f"URL     : {hoster_url}")
    dbg("PLAYWRIGHT", f"Timeout : {ZEROFS_HEADED_TIMEOUT_S}s")
    ui_callback("status", (
        "zerofs.link — navigateur (Turnstile)…", COL["status_warn"]
    ))

    file_id      = _zerofs_file_id(hoster_url)
    download_url = f"https://zerofs.link/f/{file_id}/download"
    dbg("PLAYWRIGHT", f"Endpoint POST cible : {download_url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--use-gl=angle",
                "--use-angle=swiftshader",
                "--window-size=1280,800",
                "--window-position=100,100",
                "--disable-automation",
            ],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            locale="fr-FR",
            timezone_id="Europe/Paris",
            accept_downloads=False,
            permissions=["notifications"],
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        # Interception réponse XHR POST /download
        captured_dl_url = []

        async def on_response(response):
            resp_url = response.url
            if (
                f"/f/{file_id}/download" in resp_url
                or (resp_url.rstrip("/").endswith("/download")
                    and "zerofs.link" in resp_url)
            ):
                dbg("NET", f"← XHR /download : HTTP {response.status} — {resp_url[:80]}")
                if response.status == 200:
                    try:
                        body = await response.text()
                        dbg("NET", f"  Body : {body[:300]}")
                        data = json.loads(body)
                        dl   = data.get("url", "")
                        if dl:
                            dbg("OK", f"URL extraite depuis XHR : {dl[:80]}")
                            captured_dl_url.append(dl)
                        else:
                            dbg("WARN", f"XHR JSON sans 'url' : {data}")
                    except Exception as e:
                        dbg("WARN", f"  XHR parse erreur : {e}")
                else:
                    try:
                        body = await response.text()
                        dbg("WARN", f"  XHR erreur {response.status} : {body[:200]}")
                    except Exception:
                        pass

        page.on("response", on_response)

        # Navigation
        dbg("PLAYWRIGHT", f"Navigation → {hoster_url}")
        try:
            await page.goto(
                hoster_url, timeout=60000, wait_until="domcontentloaded"
            )
        except Exception as e:
            dbg("WARN", f"goto : {type(e).__name__} — continuer")

        title = await page.title()
        dbg("PLAYWRIGHT", f"Titre : {title!r}")

        # Poll bouton + attente XHR
        dbg("PLAYWRIGHT", f"Poll #download-btn (max {ZEROFS_HEADED_TIMEOUT_S}s)…")
        ui_callback("status", (
            f"Turnstile — résolution (max {ZEROFS_HEADED_TIMEOUT_S}s)…",
            COL["status_warn"]
        ))

        deadline      = asyncio.get_event_loop().time() + ZEROFS_HEADED_TIMEOUT_S
        btn_clicked   = False
        last_log_rem  = -1

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            # DEBUG — à retirer après
            try:
                btn_count = await page.locator("#download-btn").count()
                if btn_count > 0:
                    btn_disabled_attr = await page.locator("#download-btn").get_attribute("disabled")
                    btn_class = await page.locator("#download-btn").get_attribute("class")
                    btn_text = await page.locator("#download-btn").inner_text()
                    dbg("PLAYWRIGHT", f"  btn count={btn_count} disabled={btn_disabled_attr!r} class={btn_class!r} text={btn_text[:40]!r}")
                else:
                    dbg("PLAYWRIGHT", "  #download-btn introuvable dans le DOM")
                
                # État Turnstile
                ts_iframe = await page.locator('iframe[src*="challenges.cloudflare.com"]').count()
                ts_widget = await page.locator('.cf-turnstile').count()
                ts_input  = await page.locator('#cf-chl-widget-iboin_response').count()
                ts_value  = ""
                if ts_input > 0:
                    ts_value = await page.locator('#cf-chl-widget-iboin_response').get_attribute("value") or ""
                dbg("PLAYWRIGHT", f"  Turnstile iframe={ts_iframe} widget={ts_widget} input={ts_input} value={ts_value[:30]!r}")
            except Exception as _de:
                dbg("PLAYWRIGHT", f"  DEBUG erreur : {_de}")
    
            # URL capturée ?
            if captured_dl_url:
                dbg("OK", f"URL capturée : {captured_dl_url[-1][:80]}")
                break

            if not btn_clicked:
                try:
                    btn_loc      = page.locator("#download-btn")
                    btn_disabled = await btn_loc.get_attribute("disabled")
                    is_active    = btn_disabled is None

                    if is_active:
                        dbg("OK", "Bouton activé — Turnstile résolu ✓")
                        ui_callback("status", (
                            "Turnstile résolu — clic Download…", COL["status_ok"]
                        ))
                        await asyncio.sleep(0.3)
                        await btn_loc.click()
                        dbg("PLAYWRIGHT", "Clic #download-btn effectué")
                        btn_clicked = True
                        ui_callback("status", (
                            "Attente réponse serveur…", COL["status_info"]
                        ))
                        # Attente XHR jusqu'à 20s
                        xhr_deadline = asyncio.get_event_loop().time() + 20
                        while asyncio.get_event_loop().time() < xhr_deadline:
                            await asyncio.sleep(0.3)
                            if captured_dl_url:
                                break
                        if not captured_dl_url:
                            dbg("WARN", "Pas de réponse XHR après 20s — retry possible")
                            btn_clicked = False  # Permettre un retry
                except Exception as e:
                    dbg("WARN", f"Poll bouton : {e}")

            remaining = int(deadline - asyncio.get_event_loop().time())
            if remaining != last_log_rem and remaining % 20 == 0:
                last_log_rem = remaining
                dbg("PLAYWRIGHT", f"  … ({remaining}s restantes)")
                if not btn_clicked:
                    ui_callback("status", (
                        f"Turnstile — attente ({remaining}s)…", COL["status_warn"]
                    ))

        # Cookies
        cookies    = await context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        dbg("PLAYWRIGHT", f"Cookies : {len(cookies)}")
        await browser.close()
        dbg("PLAYWRIGHT", "Navigateur fermé")

        if not captured_dl_url:
            raise RuntimeError(
                f"Playwright headed : URL non capturée après {ZEROFS_HEADED_TIMEOUT_S}s.\n"
                "Turnstile non résolu dans le délai imparti.\n"
                f"Augmentez ZEROFS_HEADED_TIMEOUT_S (actuellement {ZEROFS_HEADED_TIMEOUT_S}s)."
            )

        final_url = captured_dl_url[-1]
        dbg("OK", f"Playwright headed OK : {final_url[:80]}")
        ui_callback("status", ("Playwright headed — succès ✓", COL["status_ok"]))
        return {
            "url":     final_url,
            "cookies": cookie_str,
            "referer": hoster_url,
            "hoster":  "zerofs.link",
        }


def _zerofs_playwright_headed(hoster_url: str, ui_callback) -> dict | None:
    dbg("ZEROFS", "Lancement Playwright headed (dernier recours)…")
    try:
        return asyncio.run(
            _zerofs_playwright_headed_async(hoster_url, ui_callback)
        )
    except Exception as e:
        dbg("ERROR", f"Playwright headed : {e}")
        ui_callback("status", (f"Playwright headed échoué : {e}", COL["status_err"]))
        return None


# ── Routeur zerofs.link ──

def resolve_zerofs(hoster_url: str, ui_callback) -> dict:
    dbg_sep("ROUTEUR zerofs.link")
    dbg("ZEROFS", f"URL     : {hoster_url}")
    dbg("ZEROFS", f"file_id : {_zerofs_file_id(hoster_url)}")

    dbg("ZEROFS", "Étape 1/3 — curl_cffi")
    result = _zerofs_curl_cffi(hoster_url, ui_callback)
    if result:
        dbg("OK", "zerofs résolu via curl_cffi ✓")
        return result

    dbg("ZEROFS", "Étape 2/3 — FlareSolverr")
    result = _zerofs_flaresolverr(hoster_url, ui_callback)
    if result:
        dbg("OK", "zerofs résolu via FlareSolverr ✓")
        return result

    dbg("ZEROFS", "Étape 3/3 — Playwright headed")
    result = _zerofs_playwright_headed(hoster_url, ui_callback)
    if result:
        dbg("OK", "zerofs résolu via Playwright headed ✓")
        return result

    raise RuntimeError(
        "zerofs.link : toutes les tentatives ont échoué.\n"
        "curl_cffi + FlareSolverr + Playwright headed."
    )


# ──────────────────────────────────────────────
# Résolution buzzheavier.com — Playwright headless
# ──────────────────────────────────────────────

async def _buzzheavier_try_api(file_id: str, referer: str) -> str:
    """
    Tente de résoudre l'URL directe via l'API publique buzzheavier
    sans navigateur. Retourne l'URL ou '' si échec.
    """
    dbg("BUZZHEAVY", f"API directe — file_id : {file_id}")
    endpoints = [
        f"https://buzzheavier.com/api/v1/files/{file_id}",
        f"https://buzzheavier.com/{file_id}/download",
        f"https://buzzheavier.com/api/download/{file_id}",
    ]
    for ep in endpoints:
        try:
            dbg("BUZZHEAVY", f"  GET {ep}")
            r = requests.get(
                ep,
                headers={**HEADERS, "Referer": referer},
                allow_redirects=False,
                timeout=10,
            )
            dbg("BUZZHEAVY", f"  → HTTP {r.status_code}")
            if r.status_code in (301, 302, 303, 307, 308):
                loc = r.headers.get("location", "")
                if loc and loc != referer:
                    dbg("OK", f"  API redirect → {loc[:80]}")
                    return loc
            elif r.status_code == 200:
                try:
                    data = r.json()
                    url  = data.get("url", "") or data.get("download_url", "")
                    if url:
                        dbg("OK", f"  API JSON → {url[:80]}")
                        return url
                except Exception:
                    pass
        except Exception as e:
            dbg("WARN", f"  {ep} : {e}")
    return ""

async def _buzzheavier_playwright_async(
    hoster_url: str, hoster_cfg: dict, ui_callback
) -> dict:
    dbg_sep("buzzheavier — Playwright headless")
    dbg("BUZZHEAVY", f"URL : {hoster_url}")
    ui_callback("status", ("buzzheavier.com — résolution…", COL["status_info"]))

    page_timeout = hoster_cfg.get("page_timeout", 60000)
    cf_wait      = hoster_cfg.get("cf_wait_ms", 3000)

    # ── Étape 0 : tenter API directe sans navigateur ─────────────────────
    # buzzheavier expose /api/v1/files/{id} → URL CDN directe
    file_id = hoster_url.rstrip("/").split("/")[-1]
    dbg("BUZZHEAVY", f"file_id : {file_id}")

    direct = await _buzzheavier_try_api(file_id, hoster_url)
    if direct:
        dbg("OK", f"API directe réussie : {direct[:80]}")
        ui_callback("status", ("buzzheavier — API directe ✓", COL["status_ok"]))
        return {
            "url":     direct,
            "cookies": "",
            "referer": hoster_url,
            "hoster":  "buzzheavier.com",
        }

    # ── Étape 1 : Playwright — extraire URL depuis le DOM ────────────────
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1280,800",
            ],
        )
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            locale="fr-FR",
            timezone_id="Europe/Paris",
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        # Interception — capturer toute URL CDN dans les réponses
        captured_url: list[str] = []

        async def on_response(response):
            url    = response.url
            status = response.status
            # Réponse /download avec redirect
            if "/download" in url and "buzzheavier.com" in url:
                dbg("NET", f"  /download → HTTP {status} : {url[:80]}")
                if status in (301, 302, 303, 307, 308):
                    location = response.headers.get("location", "")
                    if location:
                        dbg("OK", f"  Redirect → {location[:80]}")
                        captured_url.append(location)
                elif status == 200:
                    try:
                        body = await response.text()
                        if '"url"' in body:
                            data = json.loads(body)
                            dl = data.get("url", "")
                            if dl:
                                captured_url.append(dl)
                    except Exception:
                        pass
            # Requête CDN directe
            if any(cdn in url for cdn in [
                "b-cdn.net", "storage.buzz", "cdn.buzzheavier",
                "s3.amazonaws", "r2.cloudflarestorage",
                "blob.core.windows", "storage.googleapis",
            ]):
                if not any(x in url for x in ["analytics", "tracking", "pixel"]):
                    dbg("OK", f"  CDN intercepté : {url[:80]}")
                    captured_url.append(url)

        page.on("response", on_response)

        # Navigation
        dbg("BUZZHEAVY", f"Navigation → {hoster_url}")
        try:
            await page.goto(
                hoster_url, timeout=page_timeout, wait_until="networkidle"
            )
        except Exception as e:
            dbg("WARN", f"networkidle : {type(e).__name__} — retry domcontentloaded")
            try:
                await page.goto(
                    hoster_url, timeout=page_timeout, wait_until="domcontentloaded"
                )
            except Exception as e2:
                dbg("WARN", f"domcontentloaded : {type(e2).__name__} — continuer")

        # Attente CF Bot Management
        if cf_wait:
            dbg("BUZZHEAVY", f"Attente CF ({cf_wait}ms)…")
            await page.wait_for_timeout(cf_wait)

        title = await page.title()
        dbg("BUZZHEAVY", f"Titre : {title!r}")

        final_url  = ""
        cookie_str = ""

        # ── Stratégie A : extraire href des liens DOM ─────────────────────
        # "Copy download link" et "Open in browser instead" ont l'URL directe
        dbg("BUZZHEAVY", "Stratégie A — extraction href DOM…")
        try:
            all_links = await page.evaluate("""
                () => {
                    const results = [];
                    document.querySelectorAll('a').forEach(a => {
                        results.push({
                            text: a.textContent.trim(),
                            href: a.href,
                            title: a.title || '',
                        });
                    });
                    return results;
                }
            """)
            dbg("BUZZHEAVY", f"  Tous les liens ({len(all_links)}) :")
            for lnk in all_links:
                dbg("BUZZHEAVY", f"    [{lnk['text'][:50]!r}] → {lnk['href'][:80]}")

            # Chercher "Copy download link" ou "Open in browser"
            priority_texts = [
                "copy download link",
                "open in browser",
                "direct link",
                "mirror",
            ]
            for lnk in all_links:
                text_lower = lnk["text"].lower()
                href       = lnk["href"]
                if not href or href == hoster_url or href.startswith("javascript"):
                    continue
                for pt in priority_texts:
                    if pt in text_lower and href.startswith("http"):
                        dbg("OK", f"  Lien direct trouvé [{lnk['text']!r}] : {href[:80]}")
                        final_url = href
                        break
                if final_url:
                    break
        except Exception as e:
            dbg("WARN", f"  Extraction DOM : {e}")

        # ── Stratégie B : __NEXT_DATA__ / variables JS globales ──────────
        if not final_url:
            dbg("BUZZHEAVY", "Stratégie B — __NEXT_DATA__ / JS globals…")
            try:
                next_data = await page.evaluate("""
                    () => {
                        try {
                            const el = document.getElementById('__NEXT_DATA__');
                            return el ? el.textContent : '';
                        } catch(e) { return ''; }
                    }
                """)
                if next_data:
                    dbg("BUZZHEAVY", f"  __NEXT_DATA__ ({len(next_data)} chars)")
                    # Chercher URL de téléchargement dans le JSON
                    patterns = [
                        r'"downloadUrl"\s*:\s*"([^"]+)"',
                        r'"url"\s*:\s*"(https://[^"]+\.iso[^"]*)"',
                        r'"directUrl"\s*:\s*"([^"]+)"',
                        r'"fileUrl"\s*:\s*"([^"]+)"',
                        r'"link"\s*:\s*"(https://[^"]+)"',
                    ]
                    for pat in patterns:
                        m = re.search(pat, next_data)
                        if m:
                            candidate = m.group(1)
                            dbg("OK", f"  URL dans __NEXT_DATA__ : {candidate[:80]}")
                            final_url = candidate
                            break
            except Exception as e:
                dbg("WARN", f"  __NEXT_DATA__ : {e}")

        # ── Stratégie C : regex sur HTML brut ────────────────────────────
        if not final_url:
            dbg("BUZZHEAVY", "Stratégie C — regex HTML brut…")
            try:
                html = await page.content()
                cdn_patterns = [
                    r'https://[a-z0-9.\-]+\.b-cdn\.net/[^\s"\'<>\\]+',
                    r'https://[a-z0-9.\-]+\.buzzheavier\.com/[^\s"\'<>\\]+',
                    r'https://[a-z0-9.\-]+/[^\s"\'<>\\]+\.iso[^\s"\'<>\\]*',
                    r'"(https://[^"]+/download[^"]*)"',
                ]
                for pattern in cdn_patterns:
                    matches = re.findall(pattern, html)
                    for m in matches:
                        candidate = m if isinstance(m, str) else m[0]
                        # Exclure la page elle-même
                        if candidate != hoster_url and "buzzheavier.com/mtsp" not in candidate:
                            dbg("OK", f"  URL regex HTML : {candidate[:80]}")
                            final_url = candidate
                            break
                    if final_url:
                        break
            except Exception as e:
                dbg("WARN", f"  Regex HTML : {e}")

        # ── Stratégie D : clic + interception réseau (token frais) ───────
        # Uniquement si les stratégies sans clic ont échoué
        if not final_url:
            dbg("BUZZHEAVY", "Stratégie D — clic avec token frais…")
            ui_callback("status", (
                "buzzheavier — tentative clic direct…", COL["status_warn"]
            ))
            # Récupérer les cookies de session actuels avant le clic
            cookies_before = await context.cookies()
            cookie_str_before = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies_before
            )
            dbg("BUZZHEAVY", f"  Cookies session : {len(cookies_before)}")

            # Extraire le token t= de l'URL /download dans le DOM
            # (le lien "Download File" contient déjà l'URL avec token)
            try:
                download_href = await page.evaluate("""
                    () => {
                        // Chercher le lien avec token t= déjà intégré
                        const links = Array.from(document.querySelectorAll('a'));
                        for (const a of links) {
                            if (a.href && a.href.includes('/download?t=')) {
                                return a.href;
                            }
                        }
                        return '';
                    }
                """)
                if download_href:
                    dbg("BUZZHEAVY", f"  URL /download?t= trouvée dans DOM : {download_href[:80]}")
                    # Tenter requête directe avec cookies de session
                    cookies_d = {c["name"]: c["value"] for c in cookies_before}
                    try:
                        r = requests.get(
                            download_href,
                            headers={
                                **HEADERS,
                                "Referer": hoster_url,
                                "Cookie":  cookie_str_before,
                            },
                            cookies=cookies_d,
                            allow_redirects=False,
                            timeout=15,
                        )
                        dbg("BUZZHEAVY", f"  GET /download?t= → HTTP {r.status_code}")
                        if r.status_code in (301, 302, 303, 307, 308):
                            location = r.headers.get("location", "")
                            if location:
                                dbg("OK", f"  Redirect vers CDN : {location[:80]}")
                                final_url = location
                        elif r.status_code == 200:
                            try:
                                data = r.json()
                                dl   = data.get("url", "")
                                if dl:
                                    final_url = dl
                            except Exception:
                                pass
                    except Exception as e:
                        dbg("WARN", f"  GET /download?t= : {e}")
            except Exception as e:
                dbg("WARN", f"  Extraction href download?t= : {e}")

            # Si toujours rien : clic Playwright + attente réseau
            if not final_url:
                selectors = [
                    'a:has-text("Download")',
                    'a:has-text("download")',
                    'a[href*="/download"]',
                ]
                for selector in selectors:
                    try:
                        count = await page.locator(selector).count()
                        if count == 0:
                            continue
                        captured_url.clear()
                        await page.locator(selector).first.click()
                        dbg("BUZZHEAVY", f"  Cliqué : {selector!r}")
                        deadline = asyncio.get_event_loop().time() + 10
                        while asyncio.get_event_loop().time() < deadline:
                            await asyncio.sleep(0.3)
                            if captured_url:
                                break
                        if captured_url:
                            final_url = captured_url[-1]
                            break
                    except Exception as e:
                        dbg("WARN", f"  {selector!r} : {e}")
                        continue

        # ── Cookies finaux ────────────────────────────────────────────────
        cookies    = await context.cookies()
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        dbg("BUZZHEAVY", f"Cookies finaux : {len(cookies)}")

        await browser.close()
        dbg("BUZZHEAVY", "Navigateur fermé")

    if not final_url:
        raise RuntimeError(
            "buzzheavier.com : URL introuvable après toutes les stratégies.\n"
            "A) href DOM  B) __NEXT_DATA__  C) regex HTML  D) clic + réseau"
        )

    dbg("OK", f"buzzheavier résolu : {final_url[:80]}")
    ui_callback("status", ("buzzheavier.com — succès ✓", COL["status_ok"]))
    return {
        "url":     final_url,
        "cookies": cookie_str,
        "referer": hoster_url,
        "hoster":  "buzzheavier.com",
    }

# ── Nouvelle fonction headed buzzheavier ─────────────────────────────────
# ══════════════════════════════════════════════════════════════════════════
# buzzheavier.com — Playwright HEADED UNIQUEMENT
# ══════════════════════════════════════════════════════════════════════════

async def _buzzheavier_playwright_headed_async(
    hoster_url: str, hoster_cfg: dict, ui_callback
) -> dict:
    dbg_sep("buzzheavier — Playwright")
    dbg("BUZZHEAVY", f"URL  : {hoster_url}")
    ui_callback("status", ("buzzheavier.com — résolution…", COL["status_info"]))

    page_timeout = hoster_cfg.get("page_timeout", 60000)
    file_id      = hoster_url.rstrip("/").split("/")[-1]

    # Étape 0 : API directe
    direct = await _buzzheavier_try_api(file_id, hoster_url)
    if direct:
        dbg("OK", f"API directe : {direct[:80]}")
        ui_callback("status", ("buzzheavier — API directe ✓", COL["status_ok"]))
        return {
            "url": direct, "cookies": "",
            "referer": hoster_url, "hoster": "buzzheavier.com"
        }

    # Args communs
    COMMON_ARGS = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--window-size=1366,768",
        "--enable-webgl",
        "--enable-gpu",
        "--lang=fr-FR",
        "--disable-crash-reporter",
        "--disable-breakpad",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    IGNORE_ARGS = [
        "--enable-automation",
        "--disable-extensions",
    ]

    CONTEXT_OPTS = dict(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1366, "height": 768},
        locale="fr-FR",
        timezone_id="Europe/Paris",
        color_scheme="dark",
        accept_downloads=False,
        extra_http_headers={
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not-A.Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    )

    STEALTH_MINIMAL = """
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined, configurable: true
        });
        if (!window.chrome) { window.chrome = { runtime: {} }; }
        const _origQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (p) =>
            p.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : _origQuery(p);
    """

    async def _run_session(headless: bool, offscreen: bool) -> dict | None:
        """
        Lance une session Playwright.
        headless=True  → pas de fenêtre du tout (peut être bloqué par CF)
        offscreen=True → fenêtre réelle mais invisible (hors écran, 1×1)
        """
        mode_label = (
            "headless" if headless
            else ("hors-écran" if offscreen else "visible")
        )
        dbg("BUZZHEAVY", f"Tentative mode : {mode_label}")
        ui_callback("status", (
            f"buzzheavier — mode {mode_label}…", COL["status_info"]
        ))

        launch_args = list(COMMON_ARGS)
        if headless:
            launch_args.append("--headless=new")
        elif offscreen:
            # Fenêtre réelle mais hors écran et minuscule
            launch_args = [
                a for a in launch_args
                if "--window-size" not in a
            ]
            launch_args += [
                "--window-size=1,1",
                "--window-position=-32000,-32000",
            ]

        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=headless,
                args=launch_args,
                ignore_default_args=IGNORE_ARGS,
            )
            context = await browser.new_context(**CONTEXT_OPTS)
            await context.add_init_script(STEALTH_MINIMAL)
            page = await context.new_page()

            captured_ts_url: list[str] = []
            captured_dl_url: list[str] = []

            async def on_request(request):
                url = request.url
                if "ts.buzzheavier.com" in url:
                    dbg("OK", f"ts.buzzheavier request : {url[:100]}")
                    captured_ts_url.append(url)
                if any(cdn in url for cdn in [
                    "b-cdn.net", "storage.buzz", "cdn.buzzheavier",
                    "s3.amazonaws", "r2.cloudflarestorage",
                ]):
                    dbg("OK", f"CDN request : {url[:100]}")
                    captured_ts_url.append(url)

            async def on_response(response):
                url    = response.url
                status = response.status
                if "/download" in url and "buzzheavier.com" in url:
                    dbg("NET", f"← {status} /download : {url[:100]}")
                    if status == 204:
                        captured_dl_url.append(url)
                    elif status in (301, 302, 303, 307, 308):
                        loc = response.headers.get("location", "")
                        if loc:
                            captured_ts_url.append(loc)
                    elif status == 200:
                        try:
                            body = await response.text()
                            if '"url"' in body:
                                data = json.loads(body)
                                dl = data.get("url", "")
                                if dl:
                                    captured_ts_url.append(dl)
                        except Exception:
                            pass
                if "ts.buzzheavier.com" in url:
                    dbg("OK", f"← {status} ts.buzzheavier : {url[:100]}")
                    if url not in captured_ts_url:
                        captured_ts_url.append(url)

            page.on("request",  on_request)
            page.on("response", on_response)

            # Navigation
            try:
                await page.goto(
                    hoster_url, timeout=page_timeout, wait_until="networkidle"
                )
            except Exception as e:
                dbg("WARN", f"networkidle : {type(e).__name__}")
                try:
                    await page.goto(
                        hoster_url, timeout=page_timeout,
                        wait_until="domcontentloaded"
                    )
                except Exception:
                    pass

            await page.wait_for_timeout(3000)
            title = await page.title()
            dbg("BUZZHEAVY", f"Titre ({mode_label}) : {title!r}")

            # Vérifier si CF a bloqué (page vide ou challenge)
            html = await page.content()
            cf_blocked = any(p in html.lower() for p in [
                "just a moment", "checking your browser",
                "enable javascript", "cf-browser-verification",
                "ray id",
            ]) or len(html) < 500

            if cf_blocked and not headless:
                dbg("WARN", f"CF détecté en mode {mode_label} — abandon")
                await browser.close()
                return None

            # Clic Download
            selectors = [
                'a:has-text("Download File")',
                'a:has-text("Download")',
                'button:has-text("Download")',
                'a[href*="/download"]',
                '.download-btn',
                '#download-btn',
            ]

            for selector in selectors:
                try:
                    count = await page.locator(selector).count()
                    if count == 0:
                        continue
                    dbg("BUZZHEAVY", f"Clic : {selector!r}")
                    loc = page.locator(selector).first
                    try:
                        await loc.scroll_into_view_if_needed()
                        await loc.wait_for(state="visible", timeout=3000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(300)
                    await loc.click()
                    dbg("BUZZHEAVY", "Cliqué ✓")
                    break
                except Exception as e:
                    dbg("WARN", f"{selector!r} : {e}")
                    continue

            # Attente ts.buzzheavier (30s)
            deadline = asyncio.get_event_loop().time() + 30
            while asyncio.get_event_loop().time() < deadline:
                await asyncio.sleep(0.3)
                if captured_ts_url:
                    break

            final_url = captured_ts_url[-1] if captured_ts_url else ""

            # Fallback cookies
            if not final_url and captured_dl_url:
                dbg("BUZZHEAVY", "Fallback cookies CF…")
                cookies_raw    = await context.cookies()
                cookies_dict   = {c["name"]: c["value"] for c in cookies_raw}
                cookie_str_tmp = "; ".join(f"{k}={v}" for k, v in cookies_dict.items())
                try:
                    r = requests.get(
                        captured_dl_url[-1],
                        headers={
                            **HEADERS,
                            "Referer": hoster_url,
                            "Cookie":  cookie_str_tmp,
                        },
                        cookies=cookies_dict,
                        allow_redirects=True,
                        timeout=20,
                    )
                    dbg("BUZZHEAVY", f"Fallback → HTTP {r.status_code} {r.url[:80]}")
                    if "ts.buzzheavier.com" in str(r.url):
                        final_url = str(r.url)
                except Exception as e:
                    dbg("WARN", f"Fallback : {e}")

            cookies_final = await context.cookies()
            cookie_str    = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies_final
            )

            await browser.close()
            dbg("BUZZHEAVY", f"Navigateur fermé ({mode_label})")

            if final_url:
                dbg("OK", f"Succès ({mode_label}) : {final_url[:100]}")
                return {
                    "url":     final_url,
                    "cookies": cookie_str,
                    "referer": hoster_url,
                    "hoster":  "buzzheavier.com",
                }
            return None

    # ── Séquence : headless → hors-écran ────────────────────────────────
    # 1. Tenter headless (invisible, propre)
    dbg("BUZZHEAVY", "Essai 1/2 — headless…")
    result = await _run_session(headless=True, offscreen=False)
    if result:
        ui_callback("status", ("buzzheavier.com — succès ✓", COL["status_ok"]))
        return result

    # 2. Fallback hors-écran (fenêtre réelle mais invisible)
    dbg("BUZZHEAVY", "Essai 2/2 — fenêtre hors-écran…")
    ui_callback("status", (
        "buzzheavier — mode furtif (hors-écran)…", COL["status_warn"]
    ))
    result = await _run_session(headless=False, offscreen=True)
    if result:
        ui_callback("status", ("buzzheavier.com — succès ✓", COL["status_ok"]))
        return result

    raise RuntimeError(
        "buzzheavier.com : URL ts.buzzheavier.com non capturée.\n"
        "headless + hors-écran ont échoué."
    )

def resolve_buzzheavier(hoster_url: str, ui_callback) -> dict:
    dbg_sep("ROUTEUR buzzheavier.com")
    dbg("BUZZHEAVY", f"URL : {hoster_url}")
    hoster_cfg = get_hoster_config(hoster_url)
    return asyncio.run(
        _buzzheavier_playwright_headed_async(hoster_url, hoster_cfg, ui_callback)
    )

async def _intercept_download_url(
    page, selectors: list[str], timeout_ms: int
) -> str:
    captured = []

    async def on_request(request):
        url = request.url
        if re.search(r"\.(iso|bin|img)(\?|$)", url, re.IGNORECASE):
            dbg("NET", f"  ISO capturé : {url[:80]}")
            captured.append(url)
        elif re.search(r"(cdn|download|storage|blob|object|s3)", url, re.IGNORECASE):
            if request.resource_type in ("document", "other"):
                dbg("NET", f"  CDN capturé : {url[:80]}")
                captured.append(url)

    page.on("request", on_request)
    for selector in selectors:
        try:
            await page.locator(selector).first.wait_for(state="visible", timeout=3000)
            await page.locator(selector).first.click()
            await page.wait_for_timeout(3000)
            if captured:
                return captured[-1]
        except Exception:
            continue
    await page.wait_for_timeout(2000)
    return captured[-1] if captured else ""

# ──────────────────────────────────────────────
# Routeur principal
# ──────────────────────────────────────────────

def resolve_by_hoster(iso_url: str, ui_callback) -> dict:
    dbg_sep("ROUTEUR PRINCIPAL")
    dbg("ROUTE", f"URL : {iso_url[:80]}")
    hoster_cfg = get_hoster_config(iso_url)
    if not hoster_cfg:
        raise ValueError(f"Hébergeur non reconnu : {iso_url}")
    hoster_name = hoster_cfg["name"]
    dbg("ROUTE", f"Hébergeur : {hoster_name}")
    dbg("ROUTE", f"Chaîne    : {hoster_cfg['chain']}")
    if hoster_name == "zerofs.link":
        return resolve_zerofs(iso_url, ui_callback)
    elif hoster_name == "buzzheavier.com":
        return resolve_buzzheavier(iso_url, ui_callback)
    else:
        raise NotImplementedError(f"Hébergeur non implémenté : {hoster_name}")


# ──────────────────────────────────────────────
# Vérification URL directe
# ──────────────────────────────────────────────

def verify_url(url: str, cookies: str = "", referer: str = "") -> dict:
    dbg("NET", f"Vérification URL : {url[:80]}")
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    if cookies:
        headers["Cookie"] = cookies
    try:
        resp           = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        content_length = int(resp.headers.get("content-length", 0))
        dbg("NET", f"HEAD → {resp.status_code} — {format_size(content_length)}")
        if resp.status_code >= 400:
            dbg("WARN", "HEAD ≥ 400 — retry GET stream")
            resp = requests.get(
                url, headers=headers, timeout=15, allow_redirects=True, stream=True
            )
            content_length = int(resp.headers.get("content-length", 0))
            dbg("NET", f"GET → {resp.status_code} — {format_size(content_length)}")
            resp.close()
        if str(resp.url) != url:
            dbg("NET", f"Redirection : {str(resp.url)[:80]}")
        return {
            "ok":        resp.status_code < 400,
            "status":    resp.status_code,
            "size":      content_length,
            "final_url": str(resp.url),
        }
    except Exception as e:
        dbg("ERROR", f"verify_url : {e}")
        return {"ok": False, "status": 0, "size": 0, "final_url": url}


# ──────────────────────────────────────────────
# Téléchargement aria2c
# ──────────────────────────────────────────────

def download_with_aria2(
    direct_url, dest_path, filename, aria2c_path,
    ui_callback, cancel_event, cookies="", referer=""
) -> bool:
    dest_dir  = os.path.dirname(os.path.abspath(dest_path))
    dest_name = os.path.basename(dest_path)
    dbg("ARIA2", f"Démarrage — {ARIA2_CONNECTIONS} connexions")
    dbg("ARIA2", f"  URL  : {direct_url[:80]}")
    dbg("ARIA2", f"  Dest : {dest_path}")
    cmd = [
        aria2c_path, direct_url,
        f"--dir={dest_dir}", f"--out={dest_name}",
        f"--split={ARIA2_SPLIT}",
        f"--max-connection-per-server={ARIA2_CONNECTIONS}",
        "--min-split-size=1M", "--max-concurrent-downloads=1",
        "--file-allocation=none", "--summary-interval=1",
        "--download-result=full", "--console-log-level=notice",
        f"--user-agent={HEADERS['User-Agent']}",
        "--allow-overwrite=true", "--auto-file-renaming=false",
        "--check-certificate=false", "--max-tries=3",
        "--retry-wait=3", "--timeout=60", "--connect-timeout=30",
    ]
    if referer:
        cmd.append(f"--referer={referer}")
    if cookies:
        cmd.append(f"--header=Cookie: {cookies}")
    ui_callback("status", (f"aria2c — {ARIA2_CONNECTIONS} connexions", COL["status_ok"]))
    process = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, creationflags=0x08000000,
        encoding="utf-8", errors="replace",
    )
    dbg("ARIA2", f"PID {process.pid}")
    progress_re = re.compile(
        r"\[#\w+\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?DL:([\d.]+\w+)"
    )
    last_pct    = -1
    error_lines = []
    try:
        for line in iter(process.stdout.readline, ""):
            if cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                for f in [dest_path, dest_path + ".aria2"]:
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                ui_callback("cancelled", None)
                return True
            line = line.strip()
            if not line:
                continue
            if any(kw in line.lower() for kw in
                   ["error","fail","refused","403","404","timeout"]):
                dbg("ARIA2", f"  ! {line}")
                error_lines.append(line)
            elif line.startswith("[#"):
                match = progress_re.search(line)
                if match:
                    pct = int(match.group(3))
                    if pct % 10 == 0 and pct != last_pct:
                        dbg("DL", (
                            f"  {pct}% — {match.group(1)}/{match.group(2)}"
                            f" @ {match.group(4)}/s"
                        ))
                    if pct != last_pct:
                        last_pct = pct
                        ui_callback("aria2_progress", {
                            "downloaded":  match.group(1),
                            "total":       match.group(2),
                            "percent":     pct,
                            "speed":       match.group(4),
                            "connections": ARIA2_CONNECTIONS,
                        })
            else:
                dbg("ARIA2", f"  {line}")
        process.wait()
        dbg("ARIA2", f"Code retour : {process.returncode}")
        if process.returncode == 0:
            final_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
            dbg("OK", f"aria2c OK — {format_size(final_size)}")
            ui_callback("done", (final_size, dest_path))
            return True
        else:
            for f in [dest_path, dest_path + ".aria2"]:
                try:
                    os.remove(f)
                except OSError:
                    pass
            err = f"Code {process.returncode}"
            if error_lines:
                err += f" — {error_lines[-1]}"
            dbg("WARN", f"aria2c échoué : {err}")
            ui_callback("status", (f"aria2c échoué ({err}), fallback…", COL["status_warn"]))
            return False
    except Exception as e:
        dbg("ERROR", f"Exception aria2c : {e}")
        try:
            process.terminate()
        except OSError:
            pass
        for f in [dest_path, dest_path + ".aria2"]:
            try:
                os.remove(f)
            except OSError:
                pass
        ui_callback("status", (f"aria2c erreur ({e}), fallback…", COL["status_warn"]))
        return False


# ──────────────────────────────────────────────
# Téléchargement requests (fallback)
# ──────────────────────────────────────────────

def download_with_requests(
    direct_url, dest_path, filename,
    ui_callback, cancel_event, cookies="", referer=""
):
    dbg("DL", f"requests — {direct_url[:80]}")
    ui_callback("status", ("Téléchargement (connexion unique)…", COL["status_warn"]))
    session = requests.Session()
    session.headers.update(HEADERS)
    if referer:
        session.headers["Referer"] = referer
    if cookies:
        session.headers["Cookie"] = cookies
    resp = session.get(direct_url, stream=True, timeout=60, allow_redirects=True)
    resp.raise_for_status()
    total_size = int(resp.headers.get("content-length", 0))
    dbg("DL", f"  Taille : {format_size(total_size)}")
    downloaded = 0
    last_pct   = -1
    ui_callback("detail", (filename, total_size, dest_path))
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
            if cancel_event.is_set():
                f.close()
                try:
                    os.remove(dest_path)
                except OSError:
                    pass
                ui_callback("cancelled", None)
                return
            f.write(chunk)
            downloaded += len(chunk)
            if total_size > 0:
                pct = downloaded / total_size
                pi  = int(pct * 100)
                if pi % 10 == 0 and pi != last_pct:
                    dbg("DL", f"  {pi}% — {format_size(downloaded)}/{format_size(total_size)}")
                    last_pct = pi
                ui_callback("progress", (pct, downloaded, total_size))
            else:
                ui_callback("progress_unknown", downloaded)
    dbg("OK", f"requests OK — {format_size(downloaded)}")
    ui_callback("done", (downloaded, dest_path))


# ──────────────────────────────────────────────
# Worker principal
# ──────────────────────────────────────────────

def download_worker(iso: dict, dest_path: str, ui_callback, cancel_event):
    filename = iso["nom_fichier"]
    dbg_sep(f"download_worker")
    dbg("STEP", f"Fichier : {filename}")
    dbg("STEP", f"Hoster  : {iso['hoster']}")
    dbg("STEP", f"URL     : {iso['lien']}")
    alts = iso.get("liens_alternatifs", [])
    if alts:
        dbg("STEP", f"Alts    : {[a['hoster']+' '+a['url'] for a in alts]}")

    try:
        # Phase 1 : résolution
        dbg_sep("Phase 1 — résolution")
        result = resolve_by_hoster(iso["lien"], ui_callback)
        if not result or not result.get("url"):
            raise ValueError("Résolution : résultat vide")

        direct_url  = result["url"]
        cookies     = result.get("cookies", "")
        referer     = result.get("referer", iso["lien"])
        hoster_used = result.get("hoster", "")
        dbg("OK", f"URL directe ({hoster_used}) : {direct_url[:80]}")

        if cancel_event.is_set():
            ui_callback("cancelled", None)
            return

        # Phase 2 : vérification
        dbg_sep("Phase 2 — vérification")
        ui_callback("status", ("Vérification du lien…", COL["status_warn"]))
        check = verify_url(direct_url, cookies, referer)
        if check["final_url"] != direct_url:
            direct_url = check["final_url"]
        ui_callback("detail", (filename, check["size"], dest_path))
        dbg("INFO", f"Taille : {format_size(check['size'])}")

        if cancel_event.is_set():
            ui_callback("cancelled", None)
            return

        # Phase 3 : téléchargement
        dbg_sep("Phase 3 — téléchargement")
        aria2c_path = find_aria2c()
        success     = False
        if aria2c_path:
            ui_callback("engine", f"aria2c ({ARIA2_CONNECTIONS} conn.) — {hoster_used}")
            success = download_with_aria2(
                direct_url, dest_path, filename, aria2c_path,
                ui_callback, cancel_event, cookies=cookies, referer=referer
            )
        if not success and not cancel_event.is_set():
            lbl = (
                f"requests (installez aria2 pour accélérer) — {hoster_used}"
                if not aria2c_path
                else f"requests (fallback aria2c) — {hoster_used}"
            )
            ui_callback("engine", lbl)
            download_with_requests(
                direct_url, dest_path, filename,
                ui_callback, cancel_event, cookies=cookies, referer=referer
            )
        dbg_sep("download_worker terminé")

    except Exception as e:
        import traceback
        dbg("ERROR", f"Exception :\n{traceback.format_exc()}")
        ui_callback("error", str(e))


# ──────────────────────────────────────────────
# Interface
# ──────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Windows 11 — Activation & ISO Downloader")
        self.geometry("1080x920")
        self.minsize(960, 840)
        self.configure(fg_color=COL["bg_app"])

        dbg_sep("DÉMARRAGE UI")
        dbg("INFO", f"Python {sys.version}")
        dbg("INFO", f"Répertoire : {os.path.dirname(os.path.abspath(__file__))}")

        self.config_ini   = load_config()
        self._pref_version  = self.config_ini.get("ISO", "default_version",  fallback="").strip().upper()
        self._pref_edition  = self.config_ini.get("ISO", "default_edition",  fallback="Consumer").strip()
        self._pref_language = self.config_ini.get("ISO", "default_language", fallback="fr-fr").strip().lower()

        self.all_isos: list[dict] = []
        self._download_cancel = threading.Event()
        self._downloading     = False
        self._download_path   = ""
        self._is_activated    = False
        self._activating      = False
        self._playwright_ok   = False

        self._build_ui()
        self._apply_config()
        self.after(100, self._on_check_activation)
        self.after(200, self._start_loading_isos)
        threading.Thread(target=self._check_playwright_startup, daemon=True).start()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        dbg("INFO", "Fermeture — arrêt FlareSolverr…")
        _flaresolverr.stop()
        self.destroy()

    # ── Playwright ──

    def _check_playwright_startup(self):
        result = check_playwright_browsers()
        self._playwright_ok = result["ok"]
        if not result["ok"]:
            dbg("WARN", f"Playwright manquant : {result['error'][:100]}")
            self.after(0, self._prompt_playwright_install, result["error"])
        else:
            dbg("OK", "Playwright prêt ✓")

    def _prompt_playwright_install(self, error: str):
        answer = messagebox.askyesno(
            "Playwright — Navigateur manquant",
            f"Chromium (Playwright) n'est pas installé.\n\n"
            f"Erreur : {error[:120]}\n\n"
            f"Nécessaire pour télécharger les ISOs.\n\n"
            f"Installer maintenant ?",
            icon="warning"
        )
        if answer:
            threading.Thread(target=self._install_playwright_bg, daemon=True).start()

    def _install_playwright_bg(self):
        success = install_playwright_browsers(self._dl_callback)
        self._playwright_ok = success
        msg   = "Chromium installé ✓" if success else "Échec — lancez 'playwright install chromium'"
        color = COL["status_ok"] if success else COL["status_err"]
        self.after(0, lambda: self.dl_status.configure(text=msg, text_color=color))

    # ── Config ──

    def _apply_config(self):
        cfg_method = self.config_ini.get(
            "General", "activation_method", fallback="HWID"
        ).strip().upper()
        method_map = {k.upper(): k for k in ACTIVATION_METHODS}
        method_key = method_map.get(cfg_method, "HWID")
        self.combo_method.set(ACTIVATION_METHODS[method_key]["label"])
        self.method_desc.configure(text=ACTIVATION_METHODS[method_key]["desc"])

    def _make_separator(self, parent):
        sep = ctk.CTkFrame(parent, height=1, fg_color=COL["border"])
        sep.pack(fill="x", padx=16, pady=(4, 4))
        return sep

    # ── UI ──

    def _build_ui(self):
        # Titre
        title_frame = ctk.CTkFrame(self, fg_color="transparent")
        title_frame.pack(fill="x", padx=30, pady=(18, 4))
        ctk.CTkLabel(
            title_frame, text="Windows 11",
            font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"),
            text_color=COL["text_primary"]
        ).pack(side="left")
        ctk.CTkLabel(
            title_frame, text="  Vérification & ISOs",
            font=ctk.CTkFont(family="Segoe UI", size=28, weight="normal"),
            text_color=COL["text_muted"]
        ).pack(side="left")
        ctk.CTkFrame(self, height=1, fg_color=COL["border"]).pack(
            fill="x", padx=30, pady=(8, 12)
        )

        # Zone haute
        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.pack(fill="x", padx=30, pady=(0, 8))
        top_row.columnconfigure(0, weight=1)
        top_row.columnconfigure(1, weight=1)

        # ── Activation état ──
        af = ctk.CTkFrame(
            top_row, corner_radius=10, fg_color=COL["bg_card"],
            border_width=1, border_color=COL["border"]
        )
        af.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        act_header = ctk.CTkFrame(af, fg_color="transparent")
        act_header.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(
            act_header, text="ÉTAT D'ACTIVATION",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=COL["text_muted"]
        ).pack(side="left")
        self.btn_check = ctk.CTkButton(
            act_header, text="Revérifier",
            command=self._on_check_activation,
            fg_color=COL["btn_neutral"], hover_color=COL["btn_neutral_h"],
            text_color=COL["text_primary"], corner_radius=6, height=26, width=100,
            font=ctk.CTkFont(size=11)
        )
        self.btn_check.pack(side="right")
        self.act_status = ctk.CTkLabel(
            af, text="Vérification en cours…",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=COL["status_warn"], anchor="w"
        )
        self.act_status.pack(padx=16, pady=(4, 2), anchor="w")
        self.act_details = ctk.CTkLabel(
            af, text="",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COL["text_secondary"], anchor="w", justify="left"
        )
        self.act_details.pack(padx=16, pady=(0, 12), anchor="w")

        # ── Activer Windows ──
        self.activate_frame = ctk.CTkFrame(
            top_row, corner_radius=10, fg_color=COL["bg_card"],
            border_width=1, border_color=COL["border"]
        )
        self.activate_frame.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        activate_header = ctk.CTkFrame(self.activate_frame, fg_color="transparent")
        activate_header.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(
            activate_header, text="ACTIVER WINDOWS",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=COL["text_muted"]
        ).pack(side="left")
        self.activate_badge = ctk.CTkLabel(
            activate_header, text="",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=COL["text_dim"]
        )
        self.activate_badge.pack(side="right")
        method_row = ctk.CTkFrame(self.activate_frame, fg_color="transparent")
        method_row.pack(fill="x", padx=16, pady=(4, 2))
        ctk.CTkLabel(
            method_row, text="Méthode",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=COL["text_secondary"]
        ).pack(side="left", padx=(0, 8))
        method_labels = [ACTIVATION_METHODS[k]["label"] for k in ACTIVATION_METHODS]
        self.combo_method = ctk.CTkComboBox(
            method_row, values=method_labels, state="readonly",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            dropdown_font=ctk.CTkFont(family="Segoe UI", size=11),
            width=200, corner_radius=4,
            fg_color=COL["bg_input"], border_color=COL["border_light"],
            button_color=COL["btn_neutral"], button_hover_color=COL["btn_neutral_h"],
            dropdown_fg_color=COL["bg_card_alt"], dropdown_hover_color=COL["btn_neutral"],
            text_color=COL["text_primary"],
            command=self._on_method_changed
        )
        self.combo_method.pack(side="left", padx=4, fill="x", expand=True)
        self.combo_method.set(method_labels[0])
        self.method_desc = ctk.CTkLabel(
            self.activate_frame,
            text=ACTIVATION_METHODS["HWID"]["desc"],
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=COL["text_dim"], anchor="w", justify="left"
        )
        self.method_desc.pack(padx=16, pady=(2, 4), anchor="w")
        self.activation_status = ctk.CTkLabel(
            self.activate_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=COL["text_secondary"], anchor="w"
        )
        self.activation_status.pack(padx=16, pady=(0, 2), anchor="w")
        activate_btn_f = ctk.CTkFrame(self.activate_frame, fg_color="transparent")
        activate_btn_f.pack(padx=16, pady=(2, 12))
        self.btn_activate = ctk.CTkButton(
            activate_btn_f, text="Activer Windows",
            command=self._on_activate,
            fg_color=COL["accent_blue"], hover_color=COL["accent_blue_h"],
            text_color="#ffffff", corner_radius=6, height=32, width=150,
            font=ctk.CTkFont(size=12, weight="bold")
        )
        self.btn_activate.pack(side="left", padx=(0, 6))
        self.btn_mas_info = ctk.CTkButton(
            activate_btn_f, text="massgrave.dev",
            command=lambda: webbrowser.open("https://massgrave.dev"),
            fg_color=COL["btn_neutral"], hover_color=COL["btn_neutral_h"],
            text_color=COL["text_secondary"], corner_radius=6, height=32, width=120,
            font=ctk.CTkFont(size=11)
        )
        self.btn_mas_info.pack(side="left")

        # ── Zone ISO ──
        iso_f = ctk.CTkFrame(
            self, corner_radius=10, fg_color=COL["bg_card"],
            border_width=1, border_color=COL["border"]
        )
        iso_f.pack(fill="both", expand=True, padx=30, pady=(0, 16))
        iso_header = ctk.CTkFrame(iso_f, fg_color="transparent")
        iso_header.pack(fill="x", padx=20, pady=(14, 4))
        ctk.CTkLabel(
            iso_header, text="TÉLÉCHARGEMENT ISO",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=COL["text_muted"]
        ).pack(side="left")
        self.iso_status = ctk.CTkLabel(
            iso_header, text="Chargement…",
            font=ctk.CTkFont(size=11), text_color=COL["status_warn"], anchor="e"
        )
        self.iso_status.pack(side="right", padx=6)
        self.progress_load = ctk.CTkProgressBar(
            iso_f, mode="indeterminate", height=2,
            progress_color=COL["text_muted"], fg_color=COL["bg_input"]
        )
        self.progress_load.pack(fill="x", padx=20, pady=(0, 8))
        self.progress_load.start()

        # Sélecteurs
        self.combo_version = self.combo_edition = self.combo_langue = None
        self.version_count = self.edition_count = self.langue_count = None
        selectors_frame = ctk.CTkFrame(iso_f, fg_color="transparent")
        selectors_frame.pack(fill="x", padx=16, pady=(0, 4))
        for i, label in enumerate(["Version", "Édition", "Langue"]):
            row = ctk.CTkFrame(
                selectors_frame, fg_color=COL["bg_card_alt"],
                corner_radius=6, border_width=1, border_color=COL["border"]
            )
            row.pack(fill="x", padx=4, pady=2)
            ctk.CTkLabel(
                row, text=label,
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
                text_color=COL["text_secondary"], width=80, anchor="w"
            ).pack(side="left", padx=(14, 8), pady=8)
            cb_name = ["_on_version_changed", "_on_edition_changed", "_on_langue_changed"][i]
            combo   = ctk.CTkComboBox(
                row, values=["Chargement…" if i == 0 else "—"],
                state="disabled",
                font=ctk.CTkFont(family="Segoe UI", size=12),
                dropdown_font=ctk.CTkFont(family="Segoe UI", size=11),
                width=340, corner_radius=4,
                fg_color=COL["bg_input"], border_color=COL["border_light"],
                button_color=COL["btn_neutral"], button_hover_color=COL["btn_neutral_h"],
                dropdown_fg_color=COL["bg_card_alt"], dropdown_hover_color=COL["btn_neutral"],
                text_color=COL["text_primary"],
                command=getattr(self, cb_name)
            )
            combo.pack(side="left", padx=4, pady=8, fill="x", expand=True)
            combo.set("Chargement…" if i == 0 else "—")
            count_lbl = ctk.CTkLabel(
                row, text="", font=ctk.CTkFont(size=10),
                text_color=COL["text_dim"], width=90, anchor="e"
            )
            count_lbl.pack(side="right", padx=14)
            if i == 0:
                self.combo_version, self.version_count = combo, count_lbl
            elif i == 1:
                self.combo_edition, self.edition_count = combo, count_lbl
            else:
                self.combo_langue, self.langue_count = combo, count_lbl

        # Résultat ISO
        self._make_separator(iso_f)
        result_f = ctk.CTkFrame(
            iso_f, fg_color=COL["bg_result"], corner_radius=6,
            border_width=1, border_color=COL["border"]
        )
        result_f.pack(fill="x", padx=20, pady=(4, 4))
        self.iso_name_label = ctk.CTkLabel(
            result_f, text="Chargement…",
            font=ctk.CTkFont(family="Consolas", size=12, weight="bold"),
            text_color=COL["text_primary"], anchor="w", wraplength=880
        )
        self.iso_name_label.pack(padx=14, pady=(10, 2), anchor="w")
        self.link_label = ctk.CTkLabel(
            result_f, text="",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=COL["text_dim"], anchor="w", wraplength=880, justify="left"
        )
        self.link_label.pack(padx=14, pady=(0, 2), anchor="w")
        self.dl_strategy_label = ctk.CTkLabel(
            result_f, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=COL["text_muted"], anchor="w", wraplength=880, justify="left"
        )
        self.dl_strategy_label.pack(padx=14, pady=(0, 10), anchor="w")

        # Boutons ISO
        btn_f = ctk.CTkFrame(iso_f, fg_color="transparent")
        btn_f.pack(pady=(6, 4))
        self.btn_copy = ctk.CTkButton(
            btn_f, text="Copier le lien", command=self._on_copy,
            fg_color=COL["accent_purple"], hover_color=COL["accent_purple_h"],
            text_color="#ffffff", corner_radius=6, height=34, width=130,
            font=ctk.CTkFont(size=12, weight="bold"), state="disabled"
        )
        self.btn_copy.pack(side="left", padx=3)
        self.btn_open = ctk.CTkButton(
            btn_f, text="Ouvrir dans le navigateur", command=self._on_open,
            fg_color=COL["accent_amber"], hover_color=COL["accent_amber_h"],
            text_color="#1a1a1a", corner_radius=6, height=34, width=190,
            font=ctk.CTkFont(size=12, weight="bold"), state="disabled"
        )
        self.btn_open.pack(side="left", padx=3)
        self.btn_download = ctk.CTkButton(
            btn_f, text="Télécharger l'ISO", command=self._on_download,
            fg_color=COL["accent_green"], hover_color=COL["accent_green_h"],
            text_color="#1a1a1a", corner_radius=6, height=34, width=170,
            font=ctk.CTkFont(size=13, weight="bold"), state="disabled"
        )
        self.btn_download.pack(side="left", padx=3)
        self.btn_reload = ctk.CTkButton(
            btn_f, text="↻", command=self._start_loading_isos,
            fg_color=COL["btn_neutral"], hover_color=COL["btn_neutral_h"],
            text_color=COL["text_secondary"], corner_radius=6, height=34, width=40,
            font=ctk.CTkFont(size=16)
        )
        self.btn_reload.pack(side="left", padx=3)

        # Zone téléchargement
        self._make_separator(iso_f)
        self.dl_frame = ctk.CTkFrame(
            iso_f, fg_color=COL["bg_card_alt"], corner_radius=8,
            border_width=1, border_color=COL["border"]
        )
        self.dl_frame.pack(fill="x", padx=20, pady=(4, 14))
        self.dl_engine_label = ctk.CTkLabel(
            self.dl_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=COL["text_muted"], anchor="w"
        )
        self.dl_engine_label.pack(padx=14, pady=(10, 0), anchor="w")
        self.dl_status = ctk.CTkLabel(
            self.dl_frame, text="Aucun téléchargement en cours",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=COL["text_secondary"], anchor="w"
        )
        self.dl_status.pack(padx=14, pady=(2, 2), anchor="w")
        self.dl_detail = ctk.CTkLabel(
            self.dl_frame, text="",
            font=ctk.CTkFont(family="Consolas", size=10),
            text_color=COL["text_dim"], anchor="w", justify="left"
        )
        self.dl_detail.pack(padx=14, pady=(0, 4), anchor="w")
        self.dl_progress = ctk.CTkProgressBar(
            self.dl_frame, mode="determinate", height=8,
            progress_color=COL["progress_fill"], fg_color=COL["progress_bg"],
            corner_radius=4
        )
        self.dl_progress.pack(fill="x", padx=14, pady=(2, 4))
        self.dl_progress.set(0)
        self.dl_percent = ctk.CTkLabel(
            self.dl_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=COL["text_primary"]
        )
        self.dl_percent.pack(padx=14, pady=(0, 2))
        self.dl_speed = ctk.CTkLabel(
            self.dl_frame, text="",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=COL["accent_teal"]
        )
        self.dl_speed.pack(padx=14, pady=(0, 4))
        dl_btn_f = ctk.CTkFrame(self.dl_frame, fg_color="transparent")
        dl_btn_f.pack(pady=(2, 10))
        self.btn_cancel = ctk.CTkButton(
            dl_btn_f, text="Annuler", command=self._on_cancel_download,
            fg_color=COL["accent_red"], hover_color=COL["accent_red_h"],
            text_color="#ffffff", corner_radius=6, height=30, width=110,
            font=ctk.CTkFont(size=12, weight="bold"), state="disabled"
        )
        self.btn_cancel.pack(side="left", padx=3)
        self.btn_open_folder = ctk.CTkButton(
            dl_btn_f, text="Ouvrir le dossier", command=self._on_open_folder,
            fg_color=COL["btn_neutral"], hover_color=COL["btn_neutral_h"],
            text_color=COL["text_primary"], corner_radius=6, height=30, width=140,
            font=ctk.CTkFont(size=12), state="disabled"
        )
        self.btn_open_folder.pack(side="left", padx=3)

        # Statut outils
        aria2  = find_aria2c()
        fs_bin = _flaresolverr.find_binary()
        parts  = []
        parts.append("aria2c ✓" if aria2 else "aria2c ✗ (installez pour accélérer)")
        parts.append("FlareSolverr ✓" if fs_bin else "FlareSolverr ✗ (binaire absent)")
        self.dl_engine_label.configure(
            text=" | ".join(parts),
            text_color=COL["text_muted"] if (aria2 and fs_bin) else COL["status_warn"]
        )

    # ── Helpers ──

    def _pick_best(self, values: list[str], preference: str,
                   fallback_prefs: list[str] = None) -> str:
        if not values:
            return ""
        pref_lower = preference.lower().strip()
        if pref_lower:
            for v in values:
                if v.lower().strip() == pref_lower:
                    return v
            for v in values:
                if v.lower().strip().startswith(pref_lower):
                    return v
        if fallback_prefs:
            for fb in fallback_prefs:
                fb_lower = fb.lower().strip()
                for v in values:
                    if v.lower().strip() == fb_lower or v.lower().strip().startswith(fb_lower):
                        return v
        return values[0]

    def _update_iso_display(self, iso: dict | None):
        if not iso:
            self.iso_name_label.configure(text="Aucun ISO pour cette sélection")
            self.link_label.configure(text="")
            self.dl_strategy_label.configure(text="")
            self.btn_copy.configure(state="disabled")
            self.btn_open.configure(state="disabled")
            self.btn_download.configure(state="disabled")
            return

        self.iso_name_label.configure(text=iso["nom_fichier"])

        link_lines = [f"[{iso['hoster']}]  {iso['lien']}"]
        for alt in iso.get("liens_alternatifs", []):
            link_lines.append(f"[{alt['hoster']}]  {alt['url']}")
        self.link_label.configure(text="\n".join(link_lines))

        hoster_cfg = get_hoster_config(iso["lien"])
        if hoster_cfg:
            chain     = hoster_cfg.get("chain", [])
            chain_str = " → ".join(chain)
            self.dl_strategy_label.configure(
                text=f"Résolution : {chain_str}",
                text_color=COL["status_info"]
            )
        else:
            self.dl_strategy_label.configure(text="")

        self.btn_copy.configure(state="normal")
        self.btn_open.configure(state="normal")
        self.btn_download.configure(
            state="normal" if not self._downloading else "disabled"
        )

    # ── Méthode activation ──

    def _on_method_changed(self, _=None):
        label = self.combo_method.get()
        for key, info in ACTIVATION_METHODS.items():
            if info["label"] == label:
                self.method_desc.configure(text=info["desc"])
                break

    # ── Activation ──

    def _on_activate(self):
        if self._activating:
            return
        self._activating = True
        self.btn_activate.configure(state="disabled", text="Activation…")
        self.activation_status.configure(text="", text_color=COL["text_secondary"])
        label = self.combo_method.get()
        method_key = next(
            (k for k, v in ACTIVATION_METHODS.items() if v["label"] == label), "HWID"
        )
        threading.Thread(target=self._t_activate, args=(method_key,), daemon=True).start()

    def _t_activate(self, method_key):
        run_activation(method_key, self._activation_callback)
        time.sleep(5)
        self._activation_callback("activation_status",
            ("Revérification…", COL["status_info"]))
        info = check_windows_activation()
        self.after(0, self._u_act, info)
        self._activation_callback("activation_status", (
            ("Activation réussie !", COL["status_ok"])
            if info["active"]
            else ("L'activation ne semble pas avoir fonctionné", COL["status_err"])
        ))
        self.after(0, self._finish_activation)

    def _activation_callback(self, event_type, data):
        self.after(0, self._process_activation_event, event_type, data)

    def _process_activation_event(self, event_type, data):
        if event_type == "activation_status":
            text, color = data
            self.activation_status.configure(text=text, text_color=color)

    def _finish_activation(self):
        self._activating = False
        if not self._is_activated:
            self.btn_activate.configure(state="normal", text="Activer Windows")

    # ── Vérification activation ──

    def _on_check_activation(self):
        self.btn_check.configure(state="disabled")
        self.act_status.configure(
            text="Vérification en cours…", text_color=COL["status_warn"]
        )
        self.act_details.configure(text="")
        threading.Thread(target=self._t_act, daemon=True).start()

    def _t_act(self):
        self.after(0, self._u_act, check_windows_activation())

    def _u_act(self, info: dict):
        self.btn_check.configure(state="normal")
        self._is_activated = info["active"]
        if info["active"]:
            self.act_status.configure(
                text="Windows est activé", text_color=COL["status_ok"]
            )
            self.activate_badge.configure(text="ACTIVÉ", text_color=COL["status_ok"])
            self.btn_activate.configure(
                state="disabled",
                fg_color=COL["btn_neutral"], hover_color=COL["btn_neutral_h"],
                text_color=COL["text_dim"], text="Déjà activé"
            )
            self.combo_method.configure(state="disabled")
            self.activation_status.configure(
                text="Aucune action nécessaire", text_color=COL["text_dim"]
            )
        else:
            self.act_status.configure(
                text="Windows n'est PAS activé", text_color=COL["status_err"]
            )
            self.activate_badge.configure(
                text="NON ACTIVÉ", text_color=COL["status_err"]
            )
            self.btn_activate.configure(
                state="normal",
                fg_color=COL["accent_blue"], hover_color=COL["accent_blue_h"],
                text_color="#ffffff", text="Activer Windows"
            )
            self.combo_method.configure(state="readonly")
            self.activation_status.configure(
                text="", text_color=COL["text_secondary"]
            )
        lines = []
        if info["nom_os"]:       lines.append(f"Produit :     {info['nom_os']}")
        if info["etat_licence"]: lines.append(f"Licence :     {info['etat_licence']}")
        if info["product_id"]:   lines.append(f"Product ID :  {info['product_id']}")
        self.act_details.configure(text="\n".join(lines))

    # ── Chargement ISOs ──

    def _start_loading_isos(self):
        dbg("STEP", "Chargement ISOs…")
        self.btn_reload.configure(state="disabled")
        self.iso_status.configure(text="Chargement…", text_color=COL["status_warn"])
        self.progress_load.pack(fill="x", padx=20, pady=(0, 8))
        self.progress_load.start()
        for c in [self.combo_version, self.combo_edition, self.combo_langue]:
            c.configure(state="disabled")
        self.iso_name_label.configure(text="Chargement…")
        self.link_label.configure(text="")
        self.dl_strategy_label.configure(text="")
        self.btn_copy.configure(state="disabled")
        self.btn_open.configure(state="disabled")
        self.btn_download.configure(state="disabled")
        threading.Thread(target=self._t_isos, daemon=True).start()

    def _t_isos(self):
        try:
            isos = fetch_all_isos()
            self.after(0, self._u_isos, isos, None)
        except Exception as e:
            import traceback
            dbg("ERROR", f"fetch_all_isos :\n{traceback.format_exc()}")
            self.after(0, self._u_isos, [], str(e))

    def _u_isos(self, isos, error):
        self.progress_load.stop()
        self.progress_load.pack_forget()
        self.btn_reload.configure(state="normal")
        if error:
            self.iso_status.configure(
                text=f"Erreur : {error}", text_color=COL["status_err"]
            )
            self.iso_name_label.configure(text="Erreur de chargement")
            return
        if not isos:
            self.iso_status.configure(
                text="Aucun ISO trouvé", text_color=COL["status_warn"]
            )
            self.iso_name_label.configure(text="Aucun ISO disponible")
            return

        self.all_isos = isos
        hosters_count = {}
        for iso in isos:
            h = iso["hoster"]
            hosters_count[h] = hosters_count.get(h, 0) + 1
        hosters_str = " | ".join(
            f"{h}: {c}" for h, c in sorted(hosters_count.items())
        )
        dbg("OK", f"{len(isos)} ISO(s) — {hosters_str}")
        self.iso_status.configure(
            text=f"{len(isos)} ISO(s)  —  {hosters_str}",
            text_color=COL["status_ok"]
        )
        self._populate_versions()

    # ── Cascade sélecteurs ──

    def _populate_versions(self):
        versions = sorted(set(i["version"] for i in self.all_isos), reverse=True)
        self.combo_version.configure(state="readonly", values=versions)
        self.version_count.configure(text=f"{len(versions)} version(s)")
        default = self._pick_best(versions, self._pref_version)
        dbg("INFO", f"Versions : {versions} → {default}")
        self.combo_version.set(default)
        self._on_version_changed(default)

    def _on_version_changed(self, _=None):
        ver      = self.combo_version.get()
        filtered = [i for i in self.all_isos if i["version"] == ver]
        editions = sorted(set(i["edition"] for i in filtered))
        self.combo_edition.configure(state="readonly", values=editions)
        self.edition_count.configure(text=f"{len(editions)} édition(s)")
        default = self._pick_best(editions, self._pref_edition, ["Consumer", "Business"])
        dbg("INFO", f"Éditions ({ver}) : {editions} → {default}")
        self.combo_edition.set(default)
        self._on_edition_changed()

    def _on_edition_changed(self, _=None):
        ver      = self.combo_version.get()
        ed       = self.combo_edition.get()
        filtered = [
            i for i in self.all_isos
            if i["version"] == ver and i["edition"] == ed
        ]
        langues_raw     = sorted(set(i["langue_code"] for i in filtered))
        langues_display = [
            f"{c}  —  {LANG_CODES.get(c, c.upper())}" for c in langues_raw
        ]
        self.combo_langue.configure(
            state="readonly",
            values=langues_display if langues_display else ["—"]
        )
        self.langue_count.configure(text=f"{len(langues_display)} langue(s)")
        default = self._pick_best(
            langues_display, self._pref_language, ["fr-fr", "en-us", "en-gb"]
        )
        dbg("INFO", f"Langues ({ver}/{ed}) : {len(langues_raw)} → {default}")
        self.combo_langue.set(default)
        self._on_langue_changed()

    def _on_langue_changed(self, _=None):
        self._update_iso_display(self._get_current_iso())

    def _get_current_iso(self) -> dict | None:
        ver          = self.combo_version.get()
        ed           = self.combo_edition.get()
        lang_display = self.combo_langue.get()
        lang_code    = (
            lang_display.split("—")[0].strip()
            if "—" in lang_display else lang_display
        )
        return next(
            (i for i in self.all_isos
             if i["version"] == ver
             and i["edition"] == ed
             and i["langue_code"] == lang_code),
            None
        )

    # ── Actions ISO ──

    def _on_copy(self):
        iso = self._get_current_iso()
        if iso:
            self.clipboard_clear()
            self.clipboard_append(iso["lien"])
            dbg("INFO", f"Lien copié : {iso['lien']}")
            self.btn_copy.configure(text="Copié !")
            self.after(2000, lambda: self.btn_copy.configure(text="Copier le lien"))

    def _on_open(self):
        iso = self._get_current_iso()
        if iso:
            dbg("INFO", f"Ouverture : {iso['lien']}")
            webbrowser.open(iso["lien"])

    # ── Téléchargement ──

    def _on_download(self):
        if self._downloading:
            return
        iso = self._get_current_iso()
        if not iso:
            return

        if not self._playwright_ok:
            dbg("WARN", "Playwright non disponible")
            answer = messagebox.askyesno(
                "Playwright manquant",
                "Chromium (Playwright) n'est pas installé.\n"
                "Nécessaire pour télécharger les ISOs.\n\n"
                "Installer maintenant ?",
                icon="warning"
            )
            if answer:
                threading.Thread(
                    target=self._install_playwright_bg, daemon=True
                ).start()
            return

        dest = filedialog.asksaveasfilename(
            title="Enregistrer l'ISO",
            defaultextension=".iso",
            initialfile=iso["nom_fichier"],
            filetypes=[("ISO files", "*.iso"), ("All files", "*.*")]
        )
        if not dest:
            return

        dbg("STEP", f"Destination : {dest}")
        self._download_path = dest
        self._download_cancel.clear()
        self._downloading = True
        self.btn_download.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_open_folder.configure(state="disabled")
        self.dl_progress.set(0)
        self.dl_percent.configure(text="")
        self.dl_speed.configure(text="")
        self.dl_detail.configure(text="")

        threading.Thread(
            target=download_worker,
            args=(iso, dest, self._dl_callback, self._download_cancel),
            daemon=True
        ).start()

    def _dl_callback(self, event_type, data):
        self.after(0, self._process_dl_event, event_type, data)

    def _process_dl_event(self, event_type, data):
        if event_type == "status":
            text, color = data
            self.dl_status.configure(text=text, text_color=color)
        elif event_type == "engine":
            self.dl_engine_label.configure(
                text=f"Moteur : {data}", text_color=COL["text_muted"]
            )
        elif event_type == "detail":
            filename, total_size, dest_path = data
            lines = [f"Fichier : {filename}"]
            if total_size:
                lines.append(f"Taille : {format_size(total_size)}")
            lines.append(f"Destination : {dest_path}")
            self.dl_detail.configure(text="\n".join(lines))
        elif event_type == "progress":
            pct, downloaded, total = data
            self.dl_progress.set(pct)
            self.dl_percent.configure(
                text=f"{pct*100:.1f}%  —  {format_size(downloaded)} / {format_size(total)}"
            )
        elif event_type == "aria2_progress":
            pct = data["percent"] / 100.0
            self.dl_progress.set(pct)
            self.dl_percent.configure(
                text=f"{data['percent']}%  —  {data['downloaded']} / {data['total']}"
            )
            self.dl_speed.configure(
                text=f"{data['speed']}/s  —  {data['connections']} connexions"
            )
        elif event_type == "progress_unknown":
            self.dl_percent.configure(text=f"{format_size(data)} téléchargé(s)")
        elif event_type == "done":
            self._downloading = False
            dbg("OK", f"Terminé : {format_size(data[0])}")
            self.dl_status.configure(
                text="Téléchargement terminé ✓", text_color=COL["status_ok"]
            )
            self.dl_progress.set(1.0)
            self.dl_percent.configure(text=f"Total : {format_size(data[0])}")
            self.dl_speed.configure(text="")
            self.btn_cancel.configure(state="disabled")
            self.btn_open_folder.configure(state="normal")
            self._download_path = data[1]
            self.btn_download.configure(state="normal")
        elif event_type == "cancelled":
            self._downloading = False
            self.dl_status.configure(
                text="Téléchargement annulé", text_color=COL["status_warn"]
            )
            self.dl_progress.set(0)
            self.dl_percent.configure(text="")
            self.dl_speed.configure(text="")
            self.btn_cancel.configure(state="disabled")
            self.btn_download.configure(state="normal")
        elif event_type == "error":
            self._downloading = False
            dbg("ERROR", f"Erreur DL : {data}")
            self.dl_status.configure(
                text=f"Erreur : {data}", text_color=COL["status_err"]
            )
            self.dl_speed.configure(text="")
            self.btn_cancel.configure(state="disabled")
            self.btn_download.configure(state="normal")

    def _on_cancel_download(self):
        dbg("WARN", "Annulation demandée")
        self._download_cancel.set()
        self.btn_cancel.configure(state="disabled")
        self.dl_status.configure(
            text="Annulation en cours…", text_color=COL["status_warn"]
        )

    def _on_open_folder(self):
        if self._download_path:
            folder = os.path.dirname(os.path.abspath(self._download_path))
            if os.path.isdir(folder):
                dbg("INFO", f"Ouverture dossier : {folder}")
                os.startfile(folder)


# ──────────────────────────────────────────────
# Point d'entrée
# ──────────────────────────────────────────────

if __name__ == "__main__":
    os.system("")  # Activer ANSI Windows Terminal
    dbg_sep("Windows 11 ISO Checker & Downloader")
    dbg("INFO", f"Python {sys.version}")
    dbg("INFO", f"Répertoire : {os.path.dirname(os.path.abspath(__file__))}")
    dbg("INFO", f"zerofs.link  → curl_cffi → FlareSolverr → Playwright headed")
    dbg("INFO", f"buzzheavier  → Playwright headless")
    dbg("INFO", f"Timeout headed    : {ZEROFS_HEADED_TIMEOUT_S}s")
    dbg("INFO", f"FlareSolverr port : {FLARESOLVERR_PORT}")

    pw = check_playwright_browsers()
    dbg("OK" if pw["ok"] else "WARN",
        "Playwright — Chromium ✓" if pw["ok"]
        else "Playwright — Chromium manquant → python -m playwright install chromium")

    fs_bin = _flaresolverr.find_binary()
    dbg("OK" if fs_bin else "WARN",
        f"FlareSolverr ✓ : {fs_bin}" if fs_bin
        else "FlareSolverr — binaire absent (zerofs: curl_cffi → Playwright headed)")

    aria2 = find_aria2c()
    dbg("OK" if aria2 else "WARN",
        f"aria2c ✓ : {aria2}" if aria2
        else "aria2c absent — fallback requests (plus lent)")

    try:
        from curl_cffi import requests as _cf_test
        dbg("OK", "curl_cffi ✓")
    except ImportError:
        dbg("WARN", "curl_cffi absent — pip install curl_cffi")

    dbg_sep()
    print()

    app = App()
    app.mainloop()