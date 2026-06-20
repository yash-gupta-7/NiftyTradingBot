import os
import json
import glob
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv, set_key

app = Flask(__name__, static_folder='static')
CORS(app)

ENV_FILE = '.env'
RISK_FILE = 'data/risk_state.json'
LOGS_DIR = 'logs/paper_scalp'

def get_latest_trades_csv():
    """Finds the most recent scalp paper trade CSV file."""
    list_of_files = glob.glob(f'{LOGS_DIR}/scalp_*.csv')
    if not list_of_files:
        return None
    return max(list_of_files, key=os.path.getctime)

@app.route('/')
def serve_index():
    return send_from_directory(app.static_folder, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(app.static_folder, path)

@app.route('/api/stats', methods=['GET'])
def get_stats():
    """Returns the risk state and current bot stats."""
    stats = {
        'daily_pnl': 0,
        'total_pnl': 0,
        'total_trades': 0,
        'win_rate': 0.0,
        'consecutive_losses': 0,
        'has_api_key': False
    }
    
    # Check if API key is set
    load_dotenv(ENV_FILE, override=True)
    if os.getenv("GROWW_TOTP_TOKEN") and os.getenv("GROWW_TOTP_SECRET"):
        stats['has_api_key'] = True

    try:
        if os.path.exists(RISK_FILE):
            with open(RISK_FILE, 'r') as f:
                state = json.load(f)
                stats['daily_pnl'] = state.get('daily_pnl', 0)
                stats['total_pnl'] = state.get('total_pnl', 0)
                stats['total_trades'] = state.get('total_trades', 0)
                stats['consecutive_losses'] = state.get('consecutive_losses', 0)
                
                wins = state.get('total_wins', 0)
                if stats['total_trades'] > 0:
                    stats['win_rate'] = round((wins / stats['total_trades']) * 100, 1)
    except Exception as e:
        print(f"Error reading risk file: {e}")

    return jsonify(stats)

@app.route('/api/trades', methods=['GET'])
def get_trades():
    """Returns the most recent trades from the CSV logs."""
    latest_csv = get_latest_trades_csv()
    if not latest_csv:
        return jsonify([])
        
    try:
        df = pd.read_csv(latest_csv)
        # Sort so newest is first
        df = df.sort_index(ascending=False)
        # Only return the last 50 trades
        trades = df.head(50).to_dict(orient='records')
        return jsonify(trades)
    except Exception as e:
        print(f"Error reading trades CSV: {e}")
        return jsonify([])

@app.route('/api/key', methods=['POST'])
def update_api_key():
    """Updates the Groww TOTP API credentials in the .env file."""
    data = request.json
    token = data.get('token')
    secret = data.get('secret')
    
    if not token or not secret:
        return jsonify({'success': False, 'message': 'Token and Secret are required'}), 400
        
    try:
        if not os.path.exists(ENV_FILE):
            open(ENV_FILE, 'a').close()
            
        set_key(ENV_FILE, 'GROWW_TOTP_TOKEN', token)
        set_key(ENV_FILE, 'GROWW_TOTP_SECRET', secret)
        
        # Reload environment
        load_dotenv(ENV_FILE, override=True)
        return jsonify({'success': True, 'message': 'API Key successfully updated'})
    except Exception as e:
        print(f"Error updating API key: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500

if __name__ == '__main__':
    # Run on 0.0.0.0 to be accessible if hosted on a VM
    app.run(host='0.0.0.0', port=5005, debug=True)
