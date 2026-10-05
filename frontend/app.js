// --- Configuration des graphiques Chart.js ---
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#334155';

let envChart, gasChart;

function initCharts() {
    const ctxEnv = document.getElementById('envChart').getContext('2d');
    envChart = new Chart(ctxEnv, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'Température (°C)',
                    borderColor: '#f87171',
                    backgroundColor: 'rgba(248, 113, 113, 0.1)',
                    borderWidth: 2,
                    data: [],
                    yAxisID: 'y',
                },
                {
                    label: 'Humidité (%)',
                    borderColor: '#38bdf8',
                    backgroundColor: 'rgba(56, 189, 248, 0.1)',
                    borderWidth: 2,
                    data: [],
                    yAxisID: 'y1',
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { type: 'linear', display: true, position: 'left', suggestedMin: 15, suggestedMax: 35 },
                y1: { type: 'linear', display: true, position: 'right', suggestedMin: 30, suggestedMax: 80, grid: { drawOnChartArea: false } }
            }
        }
    });

    const ctxGas = document.getElementById('gasChart').getContext('2d');
    gasChart = new Chart(ctxGas, {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Niveau de Gaz',
                borderColor: '#fbbf24',
                backgroundColor: 'rgba(251, 191, 36, 0.1)',
                borderWidth: 2,
                fill: true,
                data: []
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { suggestedMin: 0, suggestedMax: 1023 }
            }
        }
    });
}

// --- Mise à jour des données ---
async function fetchData() {
    try {
        const response = await fetch('http://localhost:8000/api/data/history?limit=30');
        const data = await response.json();
        const history = data.history;

        if (history.length === 0) return;

        // Mise à jour des labels (heures) et des données
        const labels = history.map(row => {
            const date = new Date(row.timestamp);
            return `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}:${date.getSeconds().toString().padStart(2, '0')}`;
        });
        
        const temps = history.map(row => row.temperature);
        const hums = history.map(row => row.humidite);
        const gases = history.map(row => row.gaz);

        envChart.data.labels = labels;
        envChart.data.datasets[0].data = temps;
        envChart.data.datasets[1].data = hums;
        envChart.update();

        gasChart.data.labels = labels;
        gasChart.data.datasets[0].data = gases;
        gasChart.update();

        // Mise à jour des statuts en direct (en prenant la dernière valeur reçue)
        const latest = history[history.length - 1];
        
        const statusPir = document.getElementById('status-pir');
        if (latest.presence === 1) {
            statusPir.textContent = "ALERTE INTRUSION";
            statusPir.className = "alert-status status-alert";
        } else {
            statusPir.textContent = "SÉCURISÉ";
            statusPir.className = "alert-status status-ok";
        }

        const statusGas = document.getElementById('status-gas');
        if (latest.gaz > 500) { // Seuil d'alerte exemple
            statusGas.textContent = "ALERTE GAZ";
            statusGas.className = "alert-status status-alert";
        } else {
            statusGas.textContent = "NORMAL";
            statusGas.className = "alert-status status-ok";
        }

        const statusRfid = document.getElementById('status-rfid');
        if (latest.rfid_uid) {
            statusRfid.textContent = latest.rfid_uid;
        }

    } catch (error) {
        console.error("Erreur de récupération des données:", error);
    }
}

// --- Initialisation ---
document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    fetchData();
    // Rafraîchir toutes les 3 secondes
    setInterval(fetchData, 3000);
});

// --- Stories : scénario actif + journal des événements ---
const BUZZER_TXT = { 0: 'SILENCIEUX', 1: 'BIP INTERMITTENT', 2: 'ALARME CONTINUE' };

async function fetchStatus() {
    try {
        const r = await fetch('http://localhost:8000/api/status');
        const st = await r.json();
        const d = st.decision;
        if (!d) return;
        document.getElementById('oled-l1').textContent = d.ligne1;
        document.getElementById('oled-l2').textContent = d.ligne2;
        document.getElementById('led-verte').className = 'led' + (d.led_verte ? ' on' : '');
        const bz = document.getElementById('buzzer-etat');
        bz.textContent = BUZZER_TXT[d.buzzer] || '?';
        bz.className = 'alert-status ' + (d.buzzer > 0 ? 'status-alert' : 'status-ok');
        document.getElementById('scenarios-actifs').textContent = d.scenarios.join(' + ');
        const env = document.getElementById('env-ia');
        if (st.env && st.env.niveau > 0) {
            env.textContent = (st.env.niveau === 2 ? 'CRITIQUE : ' : 'ANOMALIE : ') + st.env.categorie.toUpperCase();
            env.className = 'alert-status status-alert';
            env.title = st.env.raison + ' [' + st.env.modele + ']';
        } else {
            env.textContent = 'NORMAL';
            env.className = 'alert-status status-ok';
            env.title = '';
        }
    } catch (e) {
        console.error('Erreur /api/status:', e);
    }
}

async function fetchEvents() {
    try {
        const r = await fetch('http://localhost:8000/api/events?limit=15');
        const { events } = await r.json();
        const tbody = document.getElementById('events-body');
        tbody.innerHTML = '';
        for (const e of events) {
            const tr = document.createElement('tr');
            const heure = new Date(e.horodatage).toLocaleTimeString();
            const detail = e.membre ? e.membre : (e.categorie_env ? e.categorie_env : e.message_ecran);
            tr.innerHTML = `<td>${heure}</td><td><span class="alert-status severite-${e.severite}">${e.code_scenario}</span></td>` +
                           `<td></td><td></td>`;
            tr.children[2].textContent = detail;              // textContent : pas d'injection HTML
            if (!e.acquitte && e.severite !== 'INFO') {
                const b = document.createElement('button');
                b.className = 'ack-btn';
                b.textContent = 'Acquitter';
                b.onclick = async () => {
                    await fetch(`http://localhost:8000/api/events/${e.id_evenement}/ack`, { method: 'POST' });
                    fetchEvents();
                };
                tr.children[3].appendChild(b);
            }
            tbody.appendChild(tr);
        }
    } catch (e) {
        console.error('Erreur /api/events:', e);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    fetchStatus();
    fetchEvents();
    setInterval(fetchStatus, 1000);
    setInterval(fetchEvents, 3000);
});
