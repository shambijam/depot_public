// ===== Configuration & Variables Globales =====
const socket = io();
let equityChart = null;
let equityData = [];
let logsPaused = false;
let maxDataPoints = 50;

// NOUVEAU: Graphiques Stats Trading
let equityEvolutionChart = null;
let hourlyPerformanceChart = null;
let currentEquityPeriod = 7;

// ===== Initialisation =====
document.addEventListener('DOMContentLoaded', function() {
    console.log('Dashboard chargé - Connexion au serveur...');
    initializeChart();
    initializeTradingStatsCharts();  // NOUVEAU
    setupEventListeners();
    setupStatsEventListeners();  // NOUVEAU
    requestInitialData();
});

// ===== WebSocket Events =====
socket.on('connect', function() {
    console.log('✅ Connecté au serveur dashboard');
    addLog('Connecté au dashboard', 'SUCCESS');
});

socket.on('disconnect', function() {
    console.log('❌ Déconnecté du serveur');
    addLog('Déconnecté du serveur', 'ERROR');
    updateConnectionStatus(false);
});

socket.on('bot_update', function(data) {
    console.log('📊 Mise à jour reçue:', data);
    updateDashboard(data);
});

// ===== Fonctions de Mise à Jour =====
function updateDashboard(data) {
    // Mettre à jour le statut
    updateStatus(data);

    // Mettre à jour les performances
    updatePerformance(data);

    // Mettre à jour les positions
    updatePositions(data.positions || []);

    // Mettre à jour les signaux
    updateSignals(data.signals || []);

    // Mettre à jour les logs
    if (!logsPaused && data.logs) {
        updateLogs(data.logs);
    }

    // Mettre à jour le graphique
    updateEquityChart(data.equity || 0);

    // NOUVEAU: Mettre à jour les statistiques de trading
    if (data.trading_stats) {
        updateTradingStats(data.trading_stats);
    }
}

function updateStatus(data) {
    // Statut du bot
    const statusEl = document.getElementById('bot-status');
    if (statusEl) {
        statusEl.textContent = data.status === 'running' ? 'En Marche' : 'Arrêté';
        statusEl.className = 'status-value ' + (data.status === 'running' ? 'status-running' : 'status-stopped');
    }

    // Mode
    const modeEl = document.getElementById('bot-mode');
    if (modeEl) {
        modeEl.textContent = data.mode || 'DEMO';
    }

    // Statut MT5
    const mt5StatusEl = document.getElementById('mt5-status');
    if (mt5StatusEl) {
        mt5StatusEl.textContent = data.mt5_connected ? 'Connecté' : 'Déconnecté';
        mt5StatusEl.className = 'status-value ' + (data.mt5_connected ? 'status-connected' : 'status-disconnected');
    }

    // Uptime
    const uptimeEl = document.getElementById('uptime');
    if (uptimeEl && data.uptime) {
        uptimeEl.textContent = data.uptime;
    }

    // Dernière mise à jour
    const lastUpdateEl = document.getElementById('last-update');
    if (lastUpdateEl && data.last_update) {
        lastUpdateEl.textContent = data.last_update;
    }
}

function updatePerformance(data) {
    // Balance
    const balanceEl = document.getElementById('balance');
    if (balanceEl) {
        balanceEl.textContent = formatCurrency(data.balance || 0);
    }

    // Équité
    const equityEl = document.getElementById('equity');
    if (equityEl) {
        equityEl.textContent = formatCurrency(data.equity || 0);
    }

    // Profit/Perte
    const profitLossEl = document.getElementById('profit-loss');
    if (profitLossEl) {
        const profitLoss = data.profit_loss || 0;
        profitLossEl.textContent = formatCurrency(profitLoss);

        // Changer la couleur selon profit/perte
        const card = profitLossEl.closest('.stat-card');
        if (card) {
            if (profitLoss >= 0) {
                card.classList.remove('negative');
                card.style.borderLeftColor = 'var(--success-color)';
            } else {
                card.classList.add('negative');
                card.style.borderLeftColor = 'var(--danger-color)';
            }
        }
    }

    // Trades aujourd'hui
    const tradesTodayEl = document.getElementById('trades-today');
    if (tradesTodayEl) {
        tradesTodayEl.textContent = data.trades_today || 0;
    }

    // Marge
    const marginEl = document.getElementById('margin');
    if (marginEl) {
        marginEl.textContent = formatCurrency(data.margin || 0);
    }

    // Marge libre
    const freeMarginEl = document.getElementById('free-margin');
    if (freeMarginEl) {
        freeMarginEl.textContent = formatCurrency(data.free_margin || 0);
    }

    // Niveau de marge
    const marginLevelEl = document.getElementById('margin-level');
    if (marginLevelEl) {
        const marginLevel = data.margin_level || 0;
        marginLevelEl.textContent = marginLevel.toFixed(2) + '%';

        // Changer la couleur selon le niveau
        if (marginLevel < 100) {
            marginLevelEl.style.color = 'var(--danger-color)';
        } else if (marginLevel < 200) {
            marginLevelEl.style.color = 'var(--warning-color)';
        } else {
            marginLevelEl.style.color = 'var(--success-color)';
        }
    }
}

function updatePositions(positions) {
    const emptyState = document.getElementById('positions-empty');
    const table = document.getElementById('positions-table');
    const tbody = document.getElementById('positions-tbody');

    if (positions.length === 0) {
        emptyState.style.display = 'block';
        table.style.display = 'none';
        return;
    }

    emptyState.style.display = 'none';
    table.style.display = 'table';

    // Vider le tbody
    tbody.innerHTML = '';

    // Ajouter chaque position
    positions.forEach(pos => {
        const row = document.createElement('tr');

        const profitClass = pos.profit >= 0 ? 'position-profit-positive' : 'position-profit-negative';
        const typeClass = pos.type === 'ACHAT' ? 'position-buy' : 'position-sell';

        row.innerHTML = `
            <td>${pos.ticket}</td>
            <td><strong>${pos.symbol}</strong></td>
            <td class="${typeClass}">${pos.type}</td>
            <td>${pos.volume}</td>
            <td>${pos.price_open.toFixed(5)}</td>
            <td>${pos.price_current.toFixed(5)}</td>
            <td>${pos.sl ? pos.sl.toFixed(5) : '-'}</td>
            <td>${pos.tp ? pos.tp.toFixed(5) : '-'}</td>
            <td class="${profitClass}">${formatCurrency(pos.profit)}</td>
            <td>${pos.time}</td>
        `;

        tbody.appendChild(row);
    });
}

function updateSignals(signals) {
    const container = document.getElementById('signals-container');

    if (signals.length === 0) {
        container.innerHTML = '<div class="empty-state">Aucun signal actif</div>';
        return;
    }

    container.innerHTML = '';

    signals.forEach(signal => {
        const signalCard = document.createElement('div');
        signalCard.className = `signal-card signal-${signal.type.toLowerCase()}`;

        signalCard.innerHTML = `
            <div class="signal-header">
                <span class="signal-symbol">${signal.symbol}</span>
                <span class="signal-type" style="background: ${signal.type === 'BUY' ? 'var(--success-color)' : 'var(--danger-color)'}">
                    ${signal.type}
                </span>
            </div>
            <div class="signal-details">
                <div><strong>Stratégie:</strong> ${signal.strategy || 'N/A'}</div>
                <div><strong>Score:</strong> ${signal.score || 'N/A'}</div>
                <div><strong>Timeframe:</strong> ${signal.timeframe || 'N/A'}</div>
                <div><strong>Prix:</strong> ${signal.price ? signal.price.toFixed(5) : 'N/A'}</div>
            </div>
        `;

        container.appendChild(signalCard);
    });
}

function updateLogs(logs) {
    const container = document.getElementById('logs-container');

    // Garder seulement les 50 derniers logs
    const logsToShow = logs.slice(-50);

    container.innerHTML = '';

    logsToShow.forEach(log => {
        const logEntry = document.createElement('div');
        const levelClass = `log-${log.level.toLowerCase()}`;
        logEntry.className = `log-entry ${levelClass}`;

        logEntry.innerHTML = `
            <span class="log-time">${log.timestamp.split(' ')[1]}</span>
            <span class="log-level">${log.level}</span>
            <span class="log-message">${log.message}</span>
        `;

        container.appendChild(logEntry);
    });

    // Auto-scroll vers le bas
    container.scrollTop = container.scrollHeight;
}

function updateConnectionStatus(connected) {
    const statusEl = document.getElementById('bot-status');
    if (!connected && statusEl) {
        statusEl.textContent = 'Déconnecté';
        statusEl.className = 'status-value status-stopped';
    }
}

// ===== Graphique Chart.js =====
function initializeChart() {
    const ctx = document.getElementById('equity-chart');
    if (!ctx) return;

    equityChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Équité (€)',
                data: [],
                borderColor: '#3b82f6',
                backgroundColor: 'rgba(59, 130, 246, 0.1)',
                borderWidth: 2,
                tension: 0.4,
                fill: true
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: {
                        color: '#f1f5f9'
                    }
                }
            },
            scales: {
                x: {
                    ticks: {
                        color: '#94a3b8'
                    },
                    grid: {
                        color: 'rgba(51, 65, 85, 0.5)'
                    }
                },
                y: {
                    ticks: {
                        color: '#94a3b8',
                        callback: function(value) {
                            return value.toFixed(2) + ' €';
                        }
                    },
                    grid: {
                        color: 'rgba(51, 65, 85, 0.5)'
                    }
                }
            }
        }
    });
}

function updateEquityChart(equity) {
    if (!equityChart) return;

    const now = new Date().toLocaleTimeString('fr-FR');

    // Ajouter le nouveau point
    equityData.push({ time: now, value: equity });

    // Garder seulement les N derniers points
    if (equityData.length > maxDataPoints) {
        equityData.shift();
    }

    // Mettre à jour le graphique
    equityChart.data.labels = equityData.map(d => d.time);
    equityChart.data.datasets[0].data = equityData.map(d => d.value);
    equityChart.update('none'); // 'none' pour éviter les animations à chaque update
}

// ===== Event Listeners =====
function setupEventListeners() {
    // Bouton effacer logs
    const clearLogsBtn = document.getElementById('clear-logs');
    if (clearLogsBtn) {
        clearLogsBtn.addEventListener('click', function() {
            const container = document.getElementById('logs-container');
            container.innerHTML = '<div class="log-entry log-info"><span class="log-time">--:--:--</span><span class="log-level">INFO</span><span class="log-message">Logs effacés</span></div>';
        });
    }

    // Bouton pause logs
    const pauseLogsBtn = document.getElementById('pause-logs');
    if (pauseLogsBtn) {
        pauseLogsBtn.addEventListener('click', function() {
            logsPaused = !logsPaused;
            this.textContent = logsPaused ? 'Reprendre' : 'Pause';
            this.style.background = logsPaused ? 'var(--warning-color)' : 'var(--primary-color)';
        });
    }
}

// ===== Fonctions Utilitaires =====
function formatCurrency(value) {
    return new Intl.NumberFormat('fr-FR', {
        style: 'currency',
        currency: 'EUR',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
    }).format(value);
}

function addLog(message, level = 'INFO') {
    const container = document.getElementById('logs-container');
    if (!container || logsPaused) return;

    const logEntry = document.createElement('div');
    const levelClass = `log-${level.toLowerCase()}`;
    logEntry.className = `log-entry ${levelClass}`;

    const now = new Date().toLocaleTimeString('fr-FR');

    logEntry.innerHTML = `
        <span class="log-time">${now}</span>
        <span class="log-level">${level}</span>
        <span class="log-message">${message}</span>
    `;

    container.appendChild(logEntry);
    container.scrollTop = container.scrollHeight;

    // Limiter le nombre de logs affichés
    const logs = container.querySelectorAll('.log-entry');
    if (logs.length > 100) {
        logs[0].remove();
    }
}

function requestInitialData() {
    socket.emit('request_update');
}

// ===== NOUVEAU: Fonctions Statistiques de Trading =====

function initializeTradingStatsCharts() {
    // Graphique Evolution Capital
    const equityCtx = document.getElementById('equity-evolution-chart');
    if (equityCtx) {
        equityEvolutionChart = new Chart(equityCtx, {
            type: 'line',
            data: {
                labels: [],
                datasets: [
                    {
                        label: 'Capital (€)',
                        data: [],
                        borderColor: '#10b981',
                        backgroundColor: 'rgba(16, 185, 129, 0.1)',
                        borderWidth: 2,
                        tension: 0.3,
                        fill: true
                    },
                    {
                        label: 'PnL Cumulé (€)',
                        data: [],
                        borderColor: '#3b82f6',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        borderWidth: 2,
                        tension: 0.3,
                        fill: true
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#f1f5f9' } },
                    tooltip: {
                        mode: 'index',
                        intersect: false
                    }
                },
                scales: {
                    x: {
                        ticks: { color: '#94a3b8' },
                        grid: { color: 'rgba(51, 65, 85, 0.5)' }
                    },
                    y: {
                        ticks: {
                            color: '#94a3b8',
                            callback: (value) => value.toFixed(2) + ' €'
                        },
                        grid: { color: 'rgba(51, 65, 85, 0.5)' }
                    }
                }
            }
        });
    }

    // Graphique Performance Horaire
    const hourlyCtx = document.getElementById('hourly-performance-chart');
    if (hourlyCtx) {
        hourlyPerformanceChart = new Chart(hourlyCtx, {
            type: 'bar',
            data: {
                labels: Array.from({length: 24}, (_, i) => `${i}h`),
                datasets: [{
                    label: 'PnL Moyen (pips)',
                    data: new Array(24).fill(0),
                    backgroundColor: (context) => {
                        const value = context.parsed.y;
                        return value >= 0 ? 'rgba(16, 185, 129, 0.7)' : 'rgba(239, 68, 68, 0.7)';
                    },
                    borderColor: (context) => {
                        const value = context.parsed.y;
                        return value >= 0 ? '#10b981' : '#ef4444';
                    },
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { labels: { color: '#f1f5f9' } },
                    tooltip: {
                        callbacks: {
                            afterLabel: (context) => {
                                const hour = context.dataIndex;
                                const data = hourlyPerformanceChart.hourlyData?.[hour];
                                if (data) {
                                    return `Trades: ${data.trades} | Win Rate: ${(data.win_rate * 100).toFixed(1)}%`;
                                }
                                return '';
                            }
                        }
                    }
                },
                scales: {
                    x: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(51, 65, 85, 0.5)' } },
                    y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(51, 65, 85, 0.5)' } }
                }
            }
        });
    }
}

function setupStatsEventListeners() {
    // Changement de période pour equity evolution
    const periodSelector = document.getElementById('equity-period-selector');
    if (periodSelector) {
        periodSelector.addEventListener('change', function() {
            currentEquityPeriod = parseInt(this.value);
            fetchTradingStats(currentEquityPeriod);
        });
    }
}

function updateTradingStats(stats) {
    // Vérifier si données disponibles
    if (!stats || !stats.has_data) {
        showStatsEmptyState();
        return;
    }

    hideStatsEmptyState();

    // Stats Globales
    const global = stats.global || {};

    // Win Rate
    const winrateEl = document.getElementById('stats-winrate');
    if (winrateEl) {
        const winRate = (global.win_rate || 0) * 100;
        winrateEl.textContent = winRate.toFixed(1) + '%';

        // Couleur selon performance
        if (winRate >= 60) {
            winrateEl.style.color = 'var(--success-color)';
        } else if (winRate >= 45) {
            winrateEl.style.color = 'var(--warning-color)';
        } else {
            winrateEl.style.color = 'var(--danger-color)';
        }
    }

    const winrateDetailEl = document.getElementById('stats-winrate-detail');
    if (winrateDetailEl) {
        winrateDetailEl.textContent = `${global.wins || 0}W / ${global.losses || 0}L / ${global.be || 0}BE`;
    }

    // Ratio R/R
    const rrEl = document.getElementById('stats-avg-rr');
    if (rrEl) {
        rrEl.textContent = (global.avg_rr || 0).toFixed(2);
    }

    // Plus gros gain
    const bigWinEl = document.getElementById('stats-biggest-win');
    const bigWinUsdEl = document.getElementById('stats-biggest-win-usd');
    if (bigWinEl && global.biggest_win_pips !== undefined) {
        bigWinEl.textContent = '+' + global.biggest_win_pips.toFixed(1) + ' pips';
        if (bigWinUsdEl) {
            bigWinUsdEl.textContent = '+$' + (global.biggest_win_usd || 0).toFixed(2);
        }
    }

    // Plus grosse perte
    const bigLossEl = document.getElementById('stats-biggest-loss');
    const bigLossUsdEl = document.getElementById('stats-biggest-loss-usd');
    if (bigLossEl && global.biggest_loss_pips !== undefined) {
        bigLossEl.textContent = global.biggest_loss_pips.toFixed(1) + ' pips';
        if (bigLossUsdEl) {
            bigLossUsdEl.textContent = '-$' + Math.abs(global.biggest_loss_usd || 0).toFixed(2);
        }
    }

    // Profit par symbole
    updateProfitBySymbol(stats.by_symbol || {});

    // Graphique Evolution Capital
    updateEquityEvolutionChart(stats.equity_evolution || {});

    // Graphique Performance Horaire
    updateHourlyPerformanceChart(stats.hourly_performance || {});
}

function updateProfitBySymbol(bySymbol) {
    const container = document.getElementById('stats-symbols-grid');
    if (!container) return;

    if (Object.keys(bySymbol).length === 0) {
        container.innerHTML = '<div class="empty-state">Aucune donnée disponible</div>';
        return;
    }

    container.innerHTML = '';

    // Trier par PnL décroissant
    const symbols = Object.entries(bySymbol).sort((a, b) => b[1].total_pnl_usd - a[1].total_pnl_usd);

    symbols.forEach(([symbol, data]) => {
        const card = document.createElement('div');
        card.className = 'symbol-stat-card';

        const pnl = data.total_pnl_usd || 0;
        const winRate = (data.win_rate || 0) * 100;
        const pnlClass = pnl >= 0 ? 'positive' : 'negative';

        card.innerHTML = `
            <div class="symbol-header">
                <span class="symbol-name">${symbol}</span>
                <span class="symbol-pnl ${pnlClass}">${pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} USD</span>
            </div>
            <div class="symbol-details">
                <div><strong>Trades:</strong> ${data.trades || 0}</div>
                <div><strong>Win Rate:</strong> ${winRate.toFixed(1)}%</div>
                <div><strong>Avg PnL:</strong> ${(data.avg_pnl_pips || 0).toFixed(1)} pips</div>
            </div>
        `;

        container.appendChild(card);
    });
}

function updateEquityEvolutionChart(equityData) {
    if (!equityEvolutionChart || !equityData.dates) return;

    equityEvolutionChart.data.labels = equityData.dates;
    equityEvolutionChart.data.datasets[0].data = equityData.equity || [];
    equityEvolutionChart.data.datasets[1].data = equityData.cumulative_pnl || [];
    equityEvolutionChart.update('none');
}

function updateHourlyPerformanceChart(hourlyData) {
    if (!hourlyPerformanceChart) return;

    const data = new Array(24).fill(0);

    for (let hour = 0; hour < 24; hour++) {
        if (hourlyData[hour]) {
            data[hour] = hourlyData[hour].avg_pnl || 0;
        }
    }

    hourlyPerformanceChart.data.datasets[0].data = data;
    hourlyPerformanceChart.hourlyData = hourlyData;  // Stocker pour tooltips
    hourlyPerformanceChart.update('none');
}

function showStatsEmptyState() {
    const emptyState = document.getElementById('stats-empty-state');
    if (emptyState) emptyState.style.display = 'block';

    // Cacher les autres éléments
    const grids = ['stats-global-grid', 'stats-symbols-container', 'stats-charts-container'];
    grids.forEach(className => {
        const el = document.querySelector('.' + className);
        if (el) el.style.display = 'none';
    });
}

function hideStatsEmptyState() {
    const emptyState = document.getElementById('stats-empty-state');
    if (emptyState) emptyState.style.display = 'none';

    const grids = ['stats-global-grid', 'stats-symbols-container', 'stats-charts-container'];
    grids.forEach(className => {
        const el = document.querySelector('.' + className);
        if (el) el.style.display = '';
    });
}

function fetchTradingStats(days = 7) {
    fetch(`/api/trading-stats?days=${days}`)
        .then(response => response.json())
        .then(data => updateTradingStats(data))
        .catch(error => console.error('Erreur récupération stats:', error));
}

// ===== Auto-refresh périodique =====
setInterval(() => {
    if (socket.connected) {
        socket.emit('request_update');
    }
}, 5000); // Demander une mise à jour toutes les 5 secondes
