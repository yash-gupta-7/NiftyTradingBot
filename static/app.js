document.addEventListener('DOMContentLoaded', () => {
    
    // --- Elements ---
    const dailyPnlEl = document.getElementById('daily-pnl');
    const totalPnlEl = document.getElementById('total-pnl');
    const winRateEl = document.getElementById('win-rate');
    const totalTradesEl = document.getElementById('total-trades');
    const tradesBodyEl = document.getElementById('trades-body');
    
    const botStatusEl = document.getElementById('bot-status');
    const statusTextEl = document.getElementById('status-text');
    
    const apiKeyForm = document.getElementById('api-key-form');
    const notificationEl = document.getElementById('notification');

    // --- Formatters ---
    const formatCurrency = (val) => {
        const num = parseFloat(val);
        if (isNaN(num)) return '₹0';
        const formatted = new Intl.NumberFormat('en-IN', {
            style: 'currency',
            currency: 'INR',
            maximumFractionDigits: 0
        }).format(num);
        return formatted;
    };

    const applyColorClass = (el, val) => {
        el.classList.remove('profit', 'loss');
        if (val > 0) el.classList.add('profit');
        if (val < 0) el.classList.add('loss');
    };

    // --- API Calls ---
    
    const fetchStats = async () => {
        try {
            const res = await fetch('/api/stats');
            if (!res.ok) throw new Error('Network response was not ok');
            const data = await res.json();
            
            // Update Status Badge
            if (data.has_api_key) {
                botStatusEl.classList.remove('disconnected');
                statusTextEl.textContent = 'API Connected';
            } else {
                botStatusEl.classList.add('disconnected');
                statusTextEl.textContent = 'Missing API Key';
            }

            // Update Stats
            dailyPnlEl.textContent = formatCurrency(data.daily_pnl);
            applyColorClass(dailyPnlEl, data.daily_pnl);
            
            totalPnlEl.textContent = formatCurrency(data.total_pnl);
            applyColorClass(totalPnlEl, data.total_pnl);
            
            winRateEl.textContent = `${data.win_rate}%`;
            totalTradesEl.textContent = data.total_trades;
            
        } catch (error) {
            console.error('Error fetching stats:', error);
            botStatusEl.classList.add('disconnected');
            statusTextEl.textContent = 'Offline';
        }
    };

    const fetchTrades = async () => {
        try {
            const res = await fetch('/api/trades');
            if (!res.ok) throw new Error('Network response was not ok');
            const trades = await res.json();
            
            tradesBodyEl.innerHTML = '';
            
            if (trades.length === 0) {
                tradesBodyEl.innerHTML = '<tr><td colspan="7" class="loading-cell">No trades found.</td></tr>';
                return;
            }

            trades.forEach(trade => {
                const tr = document.createElement('tr');
                
                // Direction Badge
                let dirClass = '';
                let dirText = trade.direction || 'UNKNOWN';
                if (dirText.includes('CALL')) dirClass = 'call';
                else if (dirText.includes('PUT')) dirClass = 'put';
                
                // PnL Styling
                let pnlClass = '';
                const netPnl = parseFloat(trade.net_pnl);
                if (netPnl > 0) pnlClass = 'profit';
                if (netPnl < 0) pnlClass = 'loss';

                tr.innerHTML = `
                    <td>${trade.time || '-'}</td>
                    <td><span class="badge ${dirClass}">${dirText.replace('BUY_', '')}</span></td>
                    <td>${trade.strike || '-'}</td>
                    <td>₹${trade.entry_premium || '0'}</td>
                    <td>₹${trade.exit_premium || '0'}</td>
                    <td class="${pnlClass}">${formatCurrency(netPnl)}</td>
                    <td>${trade.exit_reason || '-'}</td>
                `;
                tradesBodyEl.appendChild(tr);
            });
            
        } catch (error) {
            console.error('Error fetching trades:', error);
            tradesBodyEl.innerHTML = '<tr><td colspan="7" class="loading-cell">Failed to load trades.</td></tr>';
        }
    };

    // --- Event Listeners ---
    
    apiKeyForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const token = document.getElementById('totp-token').value;
        const secret = document.getElementById('totp-secret').value;
        const btn = document.getElementById('save-key-btn');
        
        btn.textContent = 'Saving...';
        btn.disabled = true;
        
        try {
            const res = await fetch('/api/key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ token, secret })
            });
            const data = await res.json();
            
            notificationEl.classList.remove('hidden', 'success', 'error');
            
            if (data.success) {
                notificationEl.textContent = 'Configuration saved successfully!';
                notificationEl.classList.add('success');
                apiKeyForm.reset();
                fetchStats(); // Refresh status
            } else {
                notificationEl.textContent = data.message || 'Failed to save configuration.';
                notificationEl.classList.add('error');
            }
        } catch (error) {
            notificationEl.classList.remove('hidden', 'success');
            notificationEl.textContent = 'Network error occurred.';
            notificationEl.classList.add('error');
        } finally {
            btn.textContent = 'Save Configuration';
            btn.disabled = false;
            
            // Hide notification after 4 seconds
            setTimeout(() => {
                notificationEl.classList.add('hidden');
            }, 4000);
        }
    });

    // --- Initialization ---
    fetchStats();
    fetchTrades();
    
    // Auto refresh every 30 seconds
    setInterval(() => {
        fetchStats();
        fetchTrades();
    }, 30000);
});
