from flask import Flask, render_template, request, jsonify
import re
import pandas as pd
import os
from werkzeug.utils import secure_filename

# trunofficial for phone lookup
try:
    import trunofficial
    TRUNOICIAL_AVAILABLE = True
except ImportError:
    TRUNOICIAL_AVAILABLE = False

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ALLOWED_EXTENSIONS = {'txt', 'pdf', 'docx'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_file(file):
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)
    text = ""
    ext = filename.rsplit('.', 1)[1].lower()
    
    try:
        if ext == 'txt':
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        elif ext == 'pdf':
            import pdfplumber
            with pdfplumber.open(filepath) as pdf:
                for page in pdf.pages:
                    text += (page.extract_text() or "") + "\n"
        elif ext == 'docx':
            from docx import Document
            doc = Document(filepath)
            text = "\n".join([para.text for para in doc.paragraphs])
    except:
        pass
    finally:
        if os.path.exists(filepath):
            os.remove(filepath)
    return text

def parse_mobile_money_statement(text):
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    transactions = []
    
    date_pattern = r'(\d{1,2}-[A-Za-z]{3}-\d{2,4})'
    time_pattern = r'(\d{1,2}:\d{2}:\d{2}\s*[AP]M)'
    
    i = 0
    while i < len(lines):
        line = lines[i]
        date_match = re.search(date_pattern, line)
        if date_match:
            current_date = date_match.group(1)
            time_match = re.search(time_pattern, line)
            if not time_match and i + 1 < len(lines):
                time_match = re.search(time_pattern, lines[i + 1])
            current_time = time_match.group(1) if time_match else None

            trans_types = ['Send Money', 'Cash In', 'Cash Out', 'Make Payment', 'Receive Money']
            trans_type = next((t for t in trans_types if t in line), "Unknown")

            details = ""
            out_amt = in_amt = balance = 0.0
            phone = None

            j = i + 1
            while j < len(lines) and not re.search(date_pattern, lines[j]):
                dline = lines[j]
                details += dline + " "

                amounts = re.findall(r'(\d+\.?\d*)', dline)
                if amounts:
                    try:
                        val = float(amounts[-1])
                        if any(x in trans_type for x in ['Send Money', 'Cash Out', 'Make Payment']):
                            out_amt = val
                        elif any(x in trans_type for x in ['Cash In', 'Receive Money']):
                            in_amt = val
                        if 'Balance' in dline:
                            balance = val
                    except:
                        pass

                phone_match = re.search(r'(\d{11})', dline)
                if phone_match:
                    phone = phone_match.group(1)

                j += 1

            if out_amt > 0 or in_amt > 0:
                transactions.append({
                    'date': current_date,
                    'time': current_time,
                    'type': trans_type,
                    'details': details.strip()[:120],
                    'counterparty': phone,
                    'out': round(out_amt, 2),
                    'in': round(in_amt, 2),
                    'balance': round(balance, 2)
                })
            i = j
        else:
            i += 1

    df = pd.DataFrame(transactions)
    if not df.empty:
        df['datetime'] = pd.to_datetime(df['date'] + ' ' + df['time'].fillna(''), 
                                      format='%d-%b-%y %I:%M:%S %p', errors='coerce')
        df = df.dropna(subset=['datetime'])
    return df


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/analyze', methods=['POST'])
def analyze():
    phone_search = request.form.get('phone_number', '').strip()
    from_date = request.form.get('from_date')
    to_date = request.form.get('to_date')
    text_data = request.form.get('text_data', '').strip()
    file = request.files.get('file')

    if file and file.filename and allowed_file(file.filename):
        raw_text = extract_text_from_file(file)
    elif text_data:
        raw_text = text_data
    else:
        return jsonify({"error": "Please upload a file or paste statement text"}), 400

    df = parse_mobile_money_statement(raw_text)
    if df.empty:
        return jsonify({"error": "No transactions found. Please check the statement format."}), 400

    # Date Range Filter
    if from_date:
        df = df[df['datetime'] >= pd.to_datetime(from_date)]
    if to_date:
        df = df[df['datetime'] <= pd.to_datetime(to_date) + pd.Timedelta(days=1)]

    if df.empty:
        return jsonify({"error": "No transactions found in the selected date range"}), 400

    total_sent = float(df['out'].sum())
    total_received = float(df['in'].sum())
    net_flow = total_received - total_sent

    counterparty_stats = {}
    if phone_search:
        df_cp = df[df['counterparty'] == phone_search]
        counterparty_stats = {
            "phone": phone_search,
            "total_sent_to": round(float(df_cp['out'].sum()), 2),
            "total_received_from": round(float(df_cp['in'].sum()), 2),
            "net": round(float(df_cp['in'].sum()) - float(df_cp['out'].sum()), 2),
            "count": len(df_cp)
        }

    result = {
        "total_sent": round(total_sent, 2),
        "total_received": round(total_received, 2),
        "net_flow": round(net_flow, 2),
        "total_transactions": len(df),
        "counterparty_analysis": counterparty_stats,
        "transactions": df.to_dict('records')
    }
    return jsonify(result)


@app.route('/lookup_owner', methods=['POST'])
def lookup_owner():
    if not TRUNOICIAL_AVAILABLE:
        return jsonify({"error": "Phone lookup service unavailable"}), 400

    data = request.get_json()
    phone = data.get('phone')
    if not phone or not re.match(r'^\d{11}$', phone):
        return jsonify({"error": "Invalid phone number"}), 400

    try:
        result = trunofficial.search(phone)
        name = result.name if hasattr(result, 'name') and result.name else "Name Not Found"
        return jsonify({"phone": phone, "owner_name": name})
    except Exception as e:
        return jsonify({"error": "Could not fetch owner information"}), 500


if __name__ == '__main__':
    print("Mobile Money Analyzer Started Successfully")
    app.run(debug=True)
