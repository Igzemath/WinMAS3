"""
Windows 11 ISO Checker & Downloader
- Vérifie l'activation Windows (registre + PowerShell)
- Permet d'activer Windows via HWID (massgrave.dev/get)
- Charge automatiquement les ISOs Windows 11 depuis massgrave.dev
- 3 sélecteurs en cascade : Version → Édition → Langue
- Téléchargement via Playwright (résolution lien) + aria2c (multi-connexions)
- Fallback requests si aria2c non disponible ou en échec
- Configuration externe via config.ini
"""

import subprocess
import threading
import re
import os
import shutil
import winreg
import configparser
import requests
from bs4 import BeautifulSoup, Tag
import customtkinter as ctk
from tkinter import filedialog
import webbrowser
import asyncio
from playwright.async_api import async_playwright

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ──────────────────────────────────────────────
# Configuration par défaut
# ──────────────────────────────────────────────

DEFAULT_CONFIG = {
    "General": {
        "activation_method": "HWID",
    },
    "ISO": {
        "default_version": "",
        "default_edition": "Consumer",
        "default_language": "fr-fr",
    },
}

CONFIG_FILENAME = "config.ini"


def get_config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILENAME)


def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
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
        f.write("# Windows 11 ISO Checker & Downloader\n")
        f.write("# Configuration\n")
        f.write("# ══════════════════════════════════════════════\n\n")

        f.write("[General]\n\n")
        f.write("# Méthode d'activation par défaut\n")
        f.write("# Valeurs possibles : HWID, Ohook, KMS38, KMS\n")
        f.write("#   HWID  = Activation permanente liée au matériel (recommandé)\n")
        f.write("#   Ohook = Active Microsoft Office (2013-2024)\n")
        f.write("#   KMS38 = Activation jusqu'au 19 janvier 2038\n")
        f.write("#   KMS   = Activation classique 180 jours (renouvellement auto)\n")
        f.write(f"activation_method = {config.get('General', 'activation_method', fallback='HWID')}\n\n")

        f.write("[ISO]\n\n")
        f.write("# Version par défaut (ex: 24H2, 23H2)\n")
        f.write("# Laisser vide pour sélectionner automatiquement la plus récente\n")
        f.write(f"default_version = {config.get('ISO', 'default_version', fallback='')}\n\n")
        f.write("# Édition par défaut\n")
        f.write("# Valeurs possibles : Consumer, Business, Enterprise, Enterprise LTSC,\n")
        f.write("#   Education, IoT Enterprise, IoT Enterprise LTSC\n")
        f.write("# Les variantes ARM64 s'écrivent : Consumer (ARM64), Business (ARM64), etc.\n")
        f.write(f"default_edition = {config.get('ISO', 'default_edition', fallback='Consumer')}\n\n")
        f.write("# Langue par défaut (code langue)\n")
        f.write("# Exemples : fr-fr, en-us, en-gb, de-de, es-es, it-it, pt-br, ja-jp, zh-cn\n")
        f.write("# Liste complète : ar-sa, bg-bg, cs-cz, da-dk, de-de, el-gr, en-gb, en-us,\n")
        f.write("#   es-es, es-mx, et-ee, fi-fi, fr-ca, fr-fr, he-il, hr-hr, hu-hu, it-it,\n")
        f.write("#   ja-jp, ko-kr, lt-lt, lv-lv, nb-no, nl-nl, pl-pl, pt-br, pt-pt, ro-ro,\n")
        f.write("#   ru-ru, sk-sk, sl-si, sr-latn-rs, sv-se, th-th, tr-tr, uk-ua, zh-cn, zh-tw\n")
        f.write(f"default_language = {config.get('ISO', 'default_language', fallback='fr-fr')}\n\n")


# ──────────────────────────────────────────────
# Palette de couleurs sobre
# ──────────────────────────────────────────────
COL = {
    "bg_app":        "#0a0a0a",
    "bg_card":       "#141414",
    "bg_card_alt":   "#1a1a1a",
    "bg_input":      "#1e1e1e",
    "bg_result":     "#111111",
    "border":        "#2a2a2a",
    "border_light":  "#333333",

    "text_primary":  "#e0e0e0",
    "text_secondary": "#999999",
    "text_muted":    "#666666",
    "text_dim":      "#555555",

    "accent_blue":   "#4a90d9",
    "accent_blue_h": "#3a7bc8",
    "accent_green":  "#5cb85c",
    "accent_green_h": "#4a9a4a",
    "accent_amber":  "#d4a843",
    "accent_amber_h": "#b8922e",
    "accent_red":    "#d9534f",
    "accent_red_h":  "#c9433f",
    "accent_purple": "#7c6fbf",
    "accent_purple_h": "#6a5daa",
    "accent_teal":   "#5bc0be",

    "progress_bg":   "#1e1e1e",
    "progress_fill": "#5cb85c",

    "btn_neutral":   "#2a2a2a",
    "btn_neutral_h": "#383838",

    "status_ok":     "#5cb85c",
    "status_warn":   "#d4a843",
    "status_err":    "#d9534f",
    "status_info":   "#4a90d9",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

PAGE_URL = "https://massgrave.dev/windows_11_links"
CHUNK_SIZE = 1024 * 1024
ARIA2_CONNECTIONS = 16
ARIA2_SPLIT = 16

MAS_CMD = 'irm https://get.activated.win | iex'

ACTIVATION_METHODS = {
    "HWID": {
        "label": "HWID (permanent)",
        "desc": "Activation permanente liée au matériel.\nGratuit, survit aux réinstallations.",
        "cmd_flag": "/HWID",
    },
    "Ohook": {
        "label": "Ohook (Office)",
        "desc": "Active Microsoft Office.\nFonctionne pour Office 2013-2024.",
        "cmd_flag": "/Ohook",
    },
    "KMS38": {
        "label": "KMS38 (jusqu'en 2038)",
        "desc": "Activation jusqu'au 19 janvier 2038.\nPas besoin de renouvellement.",
        "cmd_flag": "/KMS38",
    },
    "KMS": {
        "label": "KMS (180 jours)",
        "desc": "Activation classique KMS.\nSe renouvelle automatiquement.",
        "cmd_flag": "/KMS-ActAndRenewalTask",
    },
}

LANG_CODES = {
    "ar-sa": "Arabic", "bg-bg": "Bulgarian", "cs-cz": "Czech",
    "da-dk": "Danish", "de-de": "German", "el-gr": "Greek",
    "en-gb": "English (UK)", "en-us": "English (US)",
    "es-es": "Spanish (Spain)", "es-mx": "Spanish (Mexico)",
    "et-ee": "Estonian", "fi-fi": "Finnish",
    "fr-ca": "French (Canada)", "fr-fr": "French (France)",
    "he-il": "Hebrew", "hr-hr": "Croatian", "hu-hu": "Hungarian",
    "it-it": "Italian", "ja-jp": "Japanese", "ko-kr": "Korean",
    "lt-lt": "Lithuanian", "lv-lv": "Latvian", "nb-no": "Norwegian",
    "nl-nl": "Dutch", "pl-pl": "Polish",
    "pt-br": "Portuguese (Brazil)", "pt-pt": "Portuguese (Portugal)",
    "ro-ro": "Romanian", "ru-ru": "Russian", "sk-sk": "Slovak",
    "sl-si": "Slovenian", "sr-latn-rs": "Serbian (Latin)",
    "sv-se": "Swedish", "th-th": "Thai", "tr-tr": "Turkish",
    "uk-ua": "Ukrainian", "zh-cn": "Chinese (Simplified)",
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
        return found
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aria2c.exe")
    if os.path.isfile(local):
        return local
    for pf in [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]:
        if pf:
            candidate = os.path.join(pf, "aria2", "aria2c.exe")
            if os.path.isfile(candidate):
                return candidate
    return None


# ──────────────────────────────────────────────
# Activation Windows
# ──────────────────────────────────────────────

def check_windows_activation() -> dict:
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
            if display_version:
                info["nom_os"] = f"{product_name} ({display_version}, Build {build})"
            else:
                info["nom_os"] = f"{product_name} (Build {build})"
            try:
                info["product_id"] = winreg.QueryValueEx(key, "ProductId")[0]
            except FileNotFoundError:
                pass
    except Exception:
        info["nom_os"] = "Lecture registre impossible"
    try:
        cmd = (
            'powershell -NoProfile -Command "'
            "Get-CimInstance -ClassName SoftwareLicensingProduct "
            "-Filter \\\"ApplicationId='55c92734-d682-4d71-983e-d6ec3f16059f' "
            "AND PartialProductKey IS NOT NULL\\\" "
            "| Select-Object -First 1 -ExpandProperty LicenseStatus"
            '"'
        )
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=10,
            shell=True, creationflags=0x08000000
        )
        code = result.stdout.strip()
        status_map = {
            "0": "Non licencié", "1": "Activé (Licencié)",
            "2": "Grâce initiale", "3": "Grâce supplémentaire",
            "4": "Grâce non authentique", "5": "Notification",
            "6": "Grâce étendue",
        }
        info["etat_licence"] = status_map.get(code, f"Code inconnu : {code}")
        info["active"] = (code == "1")
    except subprocess.TimeoutExpired:
        info["etat_licence"] = "Timeout PowerShell"
    except Exception as e:
        info["etat_licence"] = f"Erreur : {e}"
    return info


def run_activation(method_key: str, callback) -> None:
    method = ACTIVATION_METHODS[method_key]
    ps_inner = 'irm https://get.activated.win | iex'
    cmd = [
        "powershell", "-NoProfile", "-Command",
        (
            f"Start-Process powershell -Verb RunAs -Wait "
            f"-ArgumentList '-NoProfile','-Command','{ps_inner}'"
        )
    ]
    callback("activation_status", ("Lancement de l'activation…", COL["status_warn"]))
    try:
        process = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300, creationflags=0x08000000
        )
        if process.returncode == 0:
            callback("activation_status", (
                "Script lancé — vérifiez la fenêtre PowerShell admin",
                COL["status_info"]
            ))
        else:
            stderr = process.stderr.strip() if process.stderr else ""
            if "canceled" in stderr.lower() or "annul" in stderr.lower():
                callback("activation_status", (
                    "Élévation refusée par l'utilisateur", COL["status_warn"]
                ))
            else:
                callback("activation_status", (
                    f"Erreur PowerShell (code {process.returncode})", COL["status_err"]
                ))
    except subprocess.TimeoutExpired:
        callback("activation_status", ("Timeout — le script prend trop de temps", COL["status_warn"]))
    except Exception as e:
        callback("activation_status", (f"Erreur : {e}", COL["status_err"]))


# ──────────────────────────────────────────────
# Scraping ISOs
# ──────────────────────────────────────────────

def fetch_all_isos() -> list[dict]:
    resp = requests.get(PAGE_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    all_links = soup.find_all("a", href=re.compile(r"buzzheavier", re.IGNORECASE))
    all_isos = []
    for a_tag in all_links:
        href = a_tag["href"].strip()
        filename = _find_iso_filename(a_tag)
        if not filename:
            continue
        parsed = _parse_iso_filename(filename)
        if not parsed:
            continue
        all_isos.append({
            "nom_fichier": filename,
            "langue_code": parsed["langue_code"],
            "langue_nom": parsed["langue_nom"],
            "edition": parsed["edition"],
            "version": parsed["version"],
            "arch": parsed["arch"],
            "lien": href,
        })
    seen = set()
    unique = []
    for iso in all_isos:
        if iso["lien"] not in seen:
            seen.add(iso["lien"])
            unique.append(iso)
    return unique


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
    lang_match = re.match(
        r"^([a-z]{2}(?:-[a-z]{2,4}(?:-[a-z]{2})?)?)_", filename_lower
    )
    if not lang_match:
        return None
    langue_code = lang_match.group(1)
    langue_nom = LANG_CODES.get(langue_code, langue_code.upper())
    ver_match = re.search(r"(\d{2}h[12])", filename_lower)
    version = ver_match.group(1).upper() if ver_match else "Inconnu"
    edition = "Autre"
    for pattern, label in [
        (r"iot_enterprise_ltsc", "IoT Enterprise LTSC"),
        (r"iot_enterprise", "IoT Enterprise"),
        (r"enterprise_ltsc", "Enterprise LTSC"),
        (r"consumer", "Consumer"),
        (r"business", "Business"),
        (r"enterprise", "Enterprise"),
        (r"education", "Education"),
        (r"iot", "IoT"),
    ]:
        if re.search(pattern, filename_lower):
            edition = label
            break
    arch = "ARM64" if "arm64" in filename_lower else "x64"
    display_edition = f"{edition} ({arch})" if arch == "ARM64" else edition
    return {
        "langue_code": langue_code,
        "langue_nom": langue_nom,
        "edition": display_edition,
        "version": version,
        "arch": arch,
    }


# ──────────────────────────────────────────────
# Résolution lien direct (Playwright)
# ──────────────────────────────────────────────

def resolve_direct_url(buzzheavier_url: str, ui_callback) -> dict:
    ui_callback("status", ("Lancement du navigateur…", COL["status_warn"]))

    async def _resolve():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(user_agent=HEADERS["User-Agent"])
            page = await context.new_page()
            await page.goto(buzzheavier_url, timeout=60000, wait_until="networkidle")
            ui_callback("status", ("Clic sur Download…", COL["status_warn"]))
            async with page.expect_download(timeout=30000) as dl_info:
                await page.locator(
                    'a:has-text("Download"), button:has-text("Download")'
                ).first.click()
            download = await dl_info.value
            final_url = download.url
            await download.cancel()
            cookies = await context.cookies()
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
            await browser.close()
            return {"url": final_url, "cookies": cookie_str, "referer": buzzheavier_url}

    return asyncio.run(_resolve())


# ──────────────────────────────────────────────
# Vérification URL
# ──────────────────────────────────────────────

def verify_url(url: str, cookies: str = "", referer: str = "") -> dict:
    headers = dict(HEADERS)
    if referer:
        headers["Referer"] = referer
    if cookies:
        headers["Cookie"] = cookies
    try:
        resp = requests.head(url, headers=headers, timeout=15, allow_redirects=True)
        content_length = int(resp.headers.get("content-length", 0))
        if resp.status_code >= 400:
            resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True, stream=True)
            content_length = int(resp.headers.get("content-length", 0))
            resp.close()
        return {"ok": resp.status_code < 400, "status": resp.status_code,
                "size": content_length, "final_url": resp.url}
    except Exception:
        return {"ok": False, "status": 0, "size": 0, "final_url": url}


# ──────────────────────────────────────────────
# Téléchargement aria2c
# ──────────────────────────────────────────────

def download_with_aria2(
    direct_url, dest_path, filename, aria2c_path,
    ui_callback, cancel_event, cookies="", referer=""
) -> bool:
    dest_dir = os.path.dirname(os.path.abspath(dest_path))
    dest_name = os.path.basename(dest_path)
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
    progress_re = re.compile(
        r"\[#\w+\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?DL:([\d.]+\w+)"
    )
    last_pct = -1
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
            if any(kw in line.lower() for kw in ["error", "fail", "refused", "403", "404", "timeout"]):
                error_lines.append(line)
            match = progress_re.search(line)
            if match:
                pct = int(match.group(3))
                if pct != last_pct:
                    last_pct = pct
                    ui_callback("aria2_progress", {
                        "downloaded": match.group(1), "total": match.group(2),
                        "percent": pct, "speed": match.group(4),
                        "connections": ARIA2_CONNECTIONS,
                    })
        process.wait()
        if process.returncode == 0:
            final_size = os.path.getsize(dest_path) if os.path.exists(dest_path) else 0
            ui_callback("done", (final_size, dest_path))
            return True
        else:
            for f in [dest_path, dest_path + ".aria2"]:
                try:
                    os.remove(f)
                except OSError:
                    pass
            error_detail = f"Code retour: {process.returncode}"
            if error_lines:
                error_detail += f"\n{error_lines[-1]}"
            ui_callback("status", (
                f"aria2c a échoué ({error_detail}), fallback requests…",
                COL["status_warn"]
            ))
            return False
    except Exception as e:
        try:
            process.terminate()
        except OSError:
            pass
        for f in [dest_path, dest_path + ".aria2"]:
            try:
                os.remove(f)
            except OSError:
                pass
        ui_callback("status", (f"aria2c erreur ({e}), fallback requests…", COL["status_warn"]))
        return False


# ──────────────────────────────────────────────
# Téléchargement requests (fallback)
# ──────────────────────────────────────────────

def download_with_requests(
    direct_url, dest_path, filename,
    ui_callback, cancel_event, cookies="", referer=""
):
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
    downloaded = 0
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
                ui_callback("progress", (pct, downloaded, total_size))
            else:
                ui_callback("progress_unknown", downloaded)
    ui_callback("done", (downloaded, dest_path))


# ──────────────────────────────────────────────
# Worker principal
# ──────────────────────────────────────────────

def download_worker(buzzheavier_url, dest_path, filename, ui_callback, cancel_event):
    try:
        result = resolve_direct_url(buzzheavier_url, ui_callback)
        if not result or not result.get("url"):
            raise ValueError("Impossible d'obtenir le lien direct.")
        direct_url = result["url"]
        cookies = result.get("cookies", "")
        referer = result.get("referer", buzzheavier_url)
        if cancel_event.is_set():
            ui_callback("cancelled", None)
            return
        ui_callback("status", ("Vérification du lien…", COL["status_warn"]))
        check = verify_url(direct_url, cookies, referer)
        if check["final_url"] != direct_url:
            direct_url = check["final_url"]
        if check["size"] > 0:
            ui_callback("detail", (filename, check["size"], dest_path))
        else:
            ui_callback("detail", (filename, 0, dest_path))
        if cancel_event.is_set():
            ui_callback("cancelled", None)
            return
        aria2c_path = find_aria2c()
        success = False
        if aria2c_path:
            ui_callback("engine", f"aria2c ({ARIA2_CONNECTIONS} connexions)")
            success = download_with_aria2(
                direct_url, dest_path, filename, aria2c_path,
                ui_callback, cancel_event, cookies=cookies, referer=referer
            )
        if not success and not cancel_event.is_set():
            if not aria2c_path:
                ui_callback("engine", "requests (installez aria2 pour accélérer)")
            else:
                ui_callback("engine", "requests (fallback après échec aria2c)")
            download_with_requests(
                direct_url, dest_path, filename,
                ui_callback, cancel_event, cookies=cookies, referer=referer
            )
    except Exception as e:
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

        self.config = load_config()

        # Préférences ISO depuis la config
        self._pref_version = self.config.get("ISO", "default_version", fallback="").strip().upper()
        self._pref_edition = self.config.get("ISO", "default_edition", fallback="Consumer").strip()
        self._pref_language = self.config.get("ISO", "default_language", fallback="fr-fr").strip().lower()

        self.all_isos: list[dict] = []
        self._download_cancel = threading.Event()
        self._downloading = False
        self._download_path = ""
        self._is_activated = False
        self._activating = False
        self._build_ui()
        self._apply_config()
        self.after(100, self._on_check_activation)
        self.after(200, self._start_loading_isos)

    def _apply_config(self):
        cfg_method = self.config.get(
            "General", "activation_method", fallback="HWID"
        ).strip().upper()
        method_map = {k.upper(): k for k in ACTIVATION_METHODS}
        method_key = method_map.get(cfg_method, "HWID")
        method_label = ACTIVATION_METHODS[method_key]["label"]
        self.combo_method.set(method_label)
        self.method_desc.configure(text=ACTIVATION_METHODS[method_key]["desc"])

    def _make_separator(self, parent):
        sep = ctk.CTkFrame(parent, height=1, fg_color=COL["border"])
        sep.pack(fill="x", padx=16, pady=(4, 4))
        return sep

    def _build_ui(self):
        # ═══ TITRE ═══
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

        ctk.CTkFrame(
            self, height=1, fg_color=COL["border"]
        ).pack(fill="x", padx=30, pady=(8, 12))

        # ═══ ZONE HAUTE ═══
        top_row = ctk.CTkFrame(self, fg_color="transparent")
        top_row.pack(fill="x", padx=30, pady=(0, 8))
        top_row.columnconfigure(0, weight=1)
        top_row.columnconfigure(1, weight=1)

        # ── GAUCHE : État d'activation ──
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
            text_color=COL["text_primary"],
            corner_radius=6, height=26, width=100,
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
            af, text="", font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=COL["text_secondary"], anchor="w", justify="left"
        )
        self.act_details.pack(padx=16, pady=(0, 12), anchor="w")

        # ── DROITE : Activer Windows ──
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
            self.activate_frame, text=ACTIVATION_METHODS["HWID"]["desc"],
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
            text_color="#ffffff",
            corner_radius=6, height=32, width=150,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.btn_activate.pack(side="left", padx=(0, 6))

        self.btn_mas_info = ctk.CTkButton(
            activate_btn_f, text="massgrave.dev",
            command=lambda: webbrowser.open("https://massgrave.dev"),
            fg_color=COL["btn_neutral"], hover_color=COL["btn_neutral_h"],
            text_color=COL["text_secondary"],
            corner_radius=6, height=32, width=120,
            font=ctk.CTkFont(size=11),
        )
        self.btn_mas_info.pack(side="left")

        # ═══ ISO ═══
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

        # ── Sélecteurs ──
        self.combo_version = None
        self.combo_edition = None
        self.combo_langue = None
        self.version_count = None
        self.edition_count = None
        self.langue_count = None

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
            combo = ctk.CTkComboBox(
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
                self.combo_version = combo
                self.version_count = count_lbl
            elif i == 1:
                self.combo_edition = combo
                self.edition_count = count_lbl
            else:
                self.combo_langue = combo
                self.langue_count = count_lbl

        # ── Résultat ──
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
        self.link_label.pack(padx=14, pady=(0, 10), anchor="w")

        # ── Boutons ISO ──
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

        # ═══ ZONE TÉLÉCHARGEMENT ═══
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

        aria2 = find_aria2c()
        if aria2:
            self.dl_engine_label.configure(
                text="aria2c détecté — téléchargement multi-connexions activé",
                text_color=COL["text_muted"])
        else:
            self.dl_engine_label.configure(
                text="aria2c non trouvé — installez-le pour accélérer",
                text_color=COL["status_warn"])

    # ── Helpers sélection par défaut ──

    def _pick_best(self, values: list[str], preference: str, fallback_prefs: list[str] = None) -> str:
        """
        Choisit la meilleure valeur dans `values` :
        1. `preference` si présente (comparaison insensible à la casse)
        2. Premier match dans `fallback_prefs`
        3. Premier élément de la liste
        """
        if not values:
            return ""

        # Recherche exacte (insensible à la casse)
        pref_lower = preference.lower().strip()
        if pref_lower:
            for v in values:
                if v.lower().strip() == pref_lower:
                    return v
            # Recherche partielle (startswith)
            for v in values:
                if v.lower().strip().startswith(pref_lower):
                    return v

        # Fallback
        if fallback_prefs:
            for fb in fallback_prefs:
                fb_lower = fb.lower().strip()
                for v in values:
                    if v.lower().strip() == fb_lower or v.lower().strip().startswith(fb_lower):
                        return v

        return values[0]

    # ── Méthode d'activation ──

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
        method_key = "HWID"
        for key, info in ACTIVATION_METHODS.items():
            if info["label"] == label:
                method_key = key
                break
        threading.Thread(target=self._t_activate, args=(method_key,), daemon=True).start()

    def _t_activate(self, method_key):
        run_activation(method_key, self._activation_callback)
        import time
        time.sleep(3)
        info = check_windows_activation()
        self.after(0, self._u_act, info)
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
        self.act_status.configure(text="Vérification en cours…", text_color=COL["status_warn"])
        self.act_details.configure(text="")
        threading.Thread(target=self._t_act, daemon=True).start()

    def _t_act(self):
        info = check_windows_activation()
        self.after(0, self._u_act, info)

    def _u_act(self, info: dict):
        self.btn_check.configure(state="normal")
        self._is_activated = info["active"]
        if info["active"]:
            self.act_status.configure(text="Windows est activé", text_color=COL["status_ok"])
            self.activate_badge.configure(text="ACTIVÉ", text_color=COL["status_ok"])
            self.btn_activate.configure(
                state="disabled",
                fg_color=COL["btn_neutral"], hover_color=COL["btn_neutral_h"],
                text_color=COL["text_dim"], text="Déjà activé"
            )
            self.combo_method.configure(state="disabled")
            self.activation_status.configure(text="Aucune action nécessaire", text_color=COL["text_dim"])
        else:
            self.act_status.configure(text="Windows n'est PAS activé", text_color=COL["status_err"])
            self.activate_badge.configure(text="NON ACTIVÉ", text_color=COL["status_err"])
            self.btn_activate.configure(
                state="normal",
                fg_color=COL["accent_blue"], hover_color=COL["accent_blue_h"],
                text_color="#ffffff", text="Activer Windows"
            )
            self.combo_method.configure(state="readonly")
            self.activation_status.configure(text="", text_color=COL["text_secondary"])
        lines = []
        if info["nom_os"]:
            lines.append(f"Produit :  {info['nom_os']}")
        if info["etat_licence"]:
            lines.append(f"Licence :  {info['etat_licence']}")
        if info["product_id"]:
            lines.append(f"Product ID :  {info['product_id']}")
        self.act_details.configure(text="\n".join(lines))

    # ── Chargement ISOs ──

    def _start_loading_isos(self):
        self.btn_reload.configure(state="disabled")
        self.iso_status.configure(text="Chargement…", text_color=COL["status_warn"])
        self.progress_load.pack(fill="x", padx=20, pady=(0, 8))
        self.progress_load.start()
        for c in [self.combo_version, self.combo_edition, self.combo_langue]:
            c.configure(state="disabled")
        self.iso_name_label.configure(text="Chargement…")
        self.link_label.configure(text="")
        self.btn_copy.configure(state="disabled")
        self.btn_open.configure(state="disabled")
        self.btn_download.configure(state="disabled")
        threading.Thread(target=self._t_isos, daemon=True).start()

    def _t_isos(self):
        try:
            isos = fetch_all_isos()
            self.after(0, self._u_isos, isos, None)
        except Exception as e:
            self.after(0, self._u_isos, [], str(e))

    def _u_isos(self, isos, error):
        self.progress_load.stop()
        self.progress_load.pack_forget()
        self.btn_reload.configure(state="normal")
        if error:
            self.iso_status.configure(text=f"Erreur : {error}", text_color=COL["status_err"])
            self.iso_name_label.configure(text="Erreur de chargement")
            return
        if not isos:
            self.iso_status.configure(text="Aucun ISO trouvé", text_color=COL["status_warn"])
            self.iso_name_label.configure(text="Aucun ISO disponible")
            return
        self.all_isos = isos
        self.iso_status.configure(
            text=f"{len(isos)} ISO(s) disponibles", text_color=COL["status_ok"])
        self._populate_versions()

    # ── Cascade sélecteurs ──

    def _populate_versions(self):
        versions = sorted(set(i["version"] for i in self.all_isos), reverse=True)
        self.combo_version.configure(state="readonly", values=versions)
        self.version_count.configure(text=f"{len(versions)} version(s)")

        # Choisir la version : préférence config > plus récente
        default = self._pick_best(versions, self._pref_version)
        self.combo_version.set(default)
        self._on_version_changed(default)

    def _on_version_changed(self, _=None):
        ver = self.combo_version.get()
        filtered = [i for i in self.all_isos if i["version"] == ver]
        editions = sorted(set(i["edition"] for i in filtered))
        self.combo_edition.configure(state="readonly", values=editions)
        self.edition_count.configure(text=f"{len(editions)} édition(s)")

        # Choisir l'édition : préférence config > Consumer > Business
        default = self._pick_best(editions, self._pref_edition, ["Consumer", "Business"])
        self.combo_edition.set(default)
        self._on_edition_changed()

    def _on_edition_changed(self, _=None):
        ver = self.combo_version.get()
        ed = self.combo_edition.get()
        filtered = [
            i for i in self.all_isos
            if i["version"] == ver and i["edition"] == ed
        ]
        langues_raw = sorted(set(i["langue_code"] for i in filtered))
        langues_display = [
            f"{c}  —  {LANG_CODES.get(c, c.upper())}" for c in langues_raw
        ]
        self.combo_langue.configure(
            state="readonly",
            values=langues_display if langues_display else ["—"]
        )
        self.langue_count.configure(text=f"{len(langues_display)} langue(s)")

        # Choisir la langue : préférence config > fr-fr > en-us > en-gb
        default = self._pick_best(
            langues_display, self._pref_language, ["fr-fr", "en-us", "en-gb"]
        )
        self.combo_langue.set(default)
        self._on_langue_changed()

    def _on_langue_changed(self, _=None):
        iso = self._get_current_iso()
        if iso:
            self.iso_name_label.configure(text=iso["nom_fichier"])
            self.link_label.configure(text=iso["lien"])
            self.btn_copy.configure(state="normal")
            self.btn_open.configure(state="normal")
            self.btn_download.configure(
                state="normal" if not self._downloading else "disabled")
        else:
            self.iso_name_label.configure(text="Aucun ISO pour cette sélection")
            self.link_label.configure(text="")
            self.btn_copy.configure(state="disabled")
            self.btn_open.configure(state="disabled")
            self.btn_download.configure(state="disabled")

    def _get_current_iso(self) -> dict | None:
        ver = self.combo_version.get()
        ed = self.combo_edition.get()
        lang_display = self.combo_langue.get()
        lang_code = (
            lang_display.split("—")[0].strip()
            if "—" in lang_display else lang_display
        )
        for iso in self.all_isos:
            if (iso["version"] == ver
                    and iso["edition"] == ed
                    and iso["langue_code"] == lang_code):
                return iso
        return None

    # ── Actions ISO ──

    def _on_copy(self):
        iso = self._get_current_iso()
        if iso:
            self.clipboard_clear()
            self.clipboard_append(iso["lien"])
            self.btn_copy.configure(text="Copié !")
            self.after(2000, lambda: self.btn_copy.configure(text="Copier le lien"))

    def _on_open(self):
        iso = self._get_current_iso()
        if iso:
            webbrowser.open(iso["lien"])

    # ── Téléchargement ──

    def _on_download(self):
        if self._downloading:
            return
        iso = self._get_current_iso()
        if not iso:
            return
        dest = filedialog.asksaveasfilename(
            title="Enregistrer l'ISO",
            defaultextension=".iso",
            initialfile=iso["nom_fichier"],
            filetypes=[("ISO files", "*.iso"), ("All files", "*.*")]
        )
        if not dest:
            return
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
            args=(
                iso["lien"], dest, iso["nom_fichier"],
                self._dl_callback, self._download_cancel
            ),
            daemon=True
        ).start()

    def _dl_callback(self, event_type, data):
        self.after(0, self._process_dl_event, event_type, data)

    def _process_dl_event(self, event_type, data):
        if event_type == "status":
            text, color = data
            self.dl_status.configure(text=text, text_color=color)
        elif event_type == "engine":
            self.dl_engine_label.configure(text=f"Moteur : {data}", text_color=COL["text_muted"])
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
                text=f"{pct*100:.1f}%  —  {format_size(downloaded)} / {format_size(total)}")
        elif event_type == "aria2_progress":
            pct = data["percent"] / 100.0
            self.dl_progress.set(pct)
            self.dl_percent.configure(
                text=f"{data['percent']}%  —  {data['downloaded']} / {data['total']}")
            self.dl_speed.configure(
                text=f"{data['speed']}/s  —  {data['connections']} connexions")
        elif event_type == "progress_unknown":
            self.dl_percent.configure(text=f"{format_size(data)} téléchargé(s)")
        elif event_type == "done":
            self._downloading = False
            self.dl_status.configure(text="Téléchargement terminé", text_color=COL["status_ok"])
            self.dl_progress.set(1.0)
            self.dl_percent.configure(text=f"Total : {format_size(data[0])}")
            self.dl_speed.configure(text="")
            self.btn_download.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            self.btn_open_folder.configure(state="normal")
            self._download_path = data[1]
        elif event_type == "cancelled":
            self._downloading = False
            self.dl_status.configure(text="Téléchargement annulé", text_color=COL["status_warn"])
            self.dl_progress.set(0)
            self.dl_percent.configure(text="")
            self.dl_speed.configure(text="")
            self.btn_download.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
        elif event_type == "error":
            self._downloading = False
            self.dl_status.configure(text=f"Erreur : {data}", text_color=COL["status_err"])
            self.dl_speed.configure(text="")
            self.btn_download.configure(state="normal")
            self.btn_cancel.configure(state="disabled")

    def _on_cancel_download(self):
        self._download_cancel.set()
        self.btn_cancel.configure(state="disabled")
        self.dl_status.configure(text="Annulation en cours…", text_color=COL["status_warn"])

    def _on_open_folder(self):
        if self._download_path:
            folder = os.path.dirname(os.path.abspath(self._download_path))
            if os.path.isdir(folder):
                os.startfile(folder)


if __name__ == "__main__":
    app = App()
    app.mainloop()