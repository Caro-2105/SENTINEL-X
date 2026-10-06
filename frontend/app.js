// 127.0.0.1 et non "localhost" : sous Windows, "localhost" essaie d'abord l'IPv6 (::1), d'où des délais de 0,3 à 2 s.
const API = 'http://127.0.0.1:8000';
const FENETRE_S = 90;            // les courbes montrent les 90 dernières secondes
const $ = (id) => document.getElementById(id);
const BUZZER_TXT = { 0: 'SILENCIEUX', 1: 'BIP INTERMITTENT', 2: 'ALARME CONTINUE' };

function setBadge(el, texte, classe) {
    el.textContent = texte;
    el.className = 'badge ' + classe;
}

// ---------------------------------------------------------------- Onglets
function showTab(nom) {
    document.querySelectorAll('nav button').forEach(b => b.classList.toggle('active', b.dataset.tab === nom));
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-' + nom));
    try { history.replaceState(null, '', '#' + nom); } catch (e) { /* ouvert en file:// */ }
    if (nom === 'capteurs') { envChart.resize(); gasChart.resize(); }
}

// ---------------------------------------------------------------- Courbes
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#334155';

let envChart, gasChart;
let derniereSignature = null;
let derniereSigEvents = null;

// Pas d'animation : sinon la courbe "rejoue" son entrée à chaque rafraîchissement (illisible)
const COMMUN = {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'index', intersect: false },
    plugins: { tooltip: { callbacks: { title: (items) => hms(items[0].parsed.x) } } },
    elements: { line: { tension: 0 }, point: { radius: 0, hoverRadius: 4 } },
};
const hms = (t) => new Date(t * 1000).toLocaleTimeString('fr-FR');
const X_AXIS = {
    type: 'linear',
    ticks: { autoSkip: false, maxRotation: 0, callback: (v) => hms(v) },
    // Graduations aux multiples de 15 s d'horloge : une étiquette reste attachée à son instant pendant que la fenêtre avance
    afterBuildTicks: (axe) => {
        const pas = 15, ticks = [];
        for (let t = Math.ceil(axe.min / pas) * pas; t <= axe.max; t += pas) ticks.push({ value: t });
        axe.ticks = ticks;
    },
};

// Échelle adaptée aux valeurs affichées, avec une amplitude minimale (sinon le bruit du capteur paraît énorme)
// Les bornes sont arrondies à un "pas" : l'échelle ne bouge que par paliers, pas à chaque mesure.
function ajusterAxe(options, axe, valeurs, ecartMin, pas, borneBasse = null) {
    const v = valeurs.filter(x => x !== null && x !== undefined && !isNaN(x));
    if (!v.length) return;
    let lo = Math.min(...v), hi = Math.max(...v);
    if (hi - lo < ecartMin) {
        const c = (hi + lo) / 2;
        lo = c - ecartMin / 2; hi = c + ecartMin / 2;
    }
    lo = Math.floor(lo / pas) * pas;
    hi = Math.ceil(hi / pas) * pas;
    if (borneBasse !== null) lo = Math.max(borneBasse, lo);
    const sc = options.scales[axe];
    // Hystérésis : on ne rétrécit l'échelle que si elle est devenue nettement trop large
    if (sc.min !== undefined && sc.max !== undefined && lo >= sc.min && hi <= sc.max
        && (sc.max - sc.min) < 2.5 * (hi - lo)) return;
    sc.min = lo;
    sc.max = hi;
}

function initCharts() {
    envChart = new Chart($('envChart').getContext('2d'), {
        type: 'line',
        data: { datasets: [
            { label: 'Température (°C)', borderColor: '#f87171', borderWidth: 2, data: [], yAxisID: 'y' },
            { label: 'Humidité (%)', borderColor: '#38bdf8', borderWidth: 2, data: [], yAxisID: 'y1' },
        ] },
        options: { ...COMMUN, scales: {
            x: X_AXIS,
            y: { type: 'linear', position: 'left' },
            y1: { type: 'linear', position: 'right', grid: { drawOnChartArea: false } },
        } },
    });
    gasChart = new Chart($('gasChart').getContext('2d'), {
        type: 'line',
        data: { datasets: [
            { label: 'Niveau de gaz', borderColor: '#fbbf24', backgroundColor: 'rgba(251,191,36,0.1)', borderWidth: 2, fill: true, data: [] },
        ] },
        options: { ...COMMUN, scales: { x: X_AXIS, y: { type: 'linear' } } },
    });
}

async function fetchHistorique() {
    try {
        const { history } = await (await fetch(`${API}/api/data/history?limit=60`)).json();
        if (!history.length) return;
        const signature = history[history.length - 1].timestamp;
        if (signature === derniereSignature) return;      // aucune nouvelle mesure : on ne touche à rien
        derniereSignature = signature;
        const t = history.map(r => new Date(r.timestamp).getTime() / 1000);
        const fin = t[t.length - 1], debut = fin - FENETRE_S;
        const garder = history.filter((r, i) => t[i] >= debut);
        const tg = t.filter(x => x >= debut);
        const temps = garder.map(r => r.temperature);
        const hums = garder.map(r => r.humidite);
        const gaz = garder.map(r => r.gaz);
        const pts = (valeurs) => valeurs.map((y, i) => ({ x: tg[i], y }));
        for (const c of [envChart, gasChart]) {
            c.options.scales.x.min = debut;
            c.options.scales.x.max = fin;
        }

        envChart.data.datasets[0].data = pts(temps);
        envChart.data.datasets[1].data = pts(hums);
        ajusterAxe(envChart.options, 'y', temps, 2, 1);         // °C : au moins 2 °C d'amplitude, paliers de 1 °C
        ajusterAxe(envChart.options, 'y1', hums, 6, 5, 0);      // %  : au moins 6 points, paliers de 5
        envChart.update('none');

        gasChart.data.datasets[0].data = pts(gaz);
        ajusterAxe(gasChart.options, 'y', gaz, 60, 50, 0);      // gaz : au moins 60 unités, paliers de 50
        gasChart.update('none');
    } catch (e) {
        console.error('Erreur /api/data/history:', e);
    }
}

// ---------------------------------------------------------------- État en direct (1 s)
const PERIME_S = 10;   // une trame capteurs plus vieille est considérée absente

function majBandeauEsp(d) {
    if (!d) return;
    $('oled-l1').textContent = d.ligne1;
    $('oled-l2').textContent = d.ligne2;
    $('led-verte').className = 'led' + (d.led_verte ? ' on' : '');
    setBadge($('buzzer-etat'), BUZZER_TXT[d.buzzer] || '?', d.buzzer > 0 ? 'bad' : 'ok');
    setBadge($('scenarios-actifs'), d.scenarios.join(' + '), 'off');
}

function majTuiles(c, env) {
    const frais = c && (Date.now() / 1000 - c.ts) < PERIME_S;
    if (!frais) {
        ['status-pir', 'status-gas'].forEach(id => setBadge($(id), 'PAS DE DONNÉES', 'off'));
    } else {
        setBadge($('status-pir'), c.presence === 1 ? 'PRÉSENCE' : 'RAS', c.presence === 1 ? 'warn' : 'ok');
        const g = c.gaz;
        setBadge($('status-gas'), g === null ? '—' : `${Math.round(g)}`, g >= 600 ? 'bad' : g >= 400 ? 'warn' : 'ok');
        if (c.rfid_uid) setBadge($('status-rfid'), c.rfid_uid, 'off');
    }
    const ia = $('env-ia');
    if (env && env.niveau > 0) {
        setBadge(ia, (env.niveau === 2 ? 'CRITIQUE : ' : 'ANOMALIE : ') + env.categorie.toUpperCase(), env.niveau === 2 ? 'bad' : 'warn');
        ia.title = env.raison + ' [' + env.modele + ']';
    } else {
        setBadge(ia, env ? 'NORMAL' : '—', env ? 'ok' : 'off');
        ia.title = '';
    }
}

// ---------------------------------------------------------------- Onglet caméra
let camDemande = null;   // action du bouton : "on" ou "auto"

let camStable = null, camCandidat = null, camCompte = 0;

function majCamera(camBrute, vision) {
    const cle = (c) => !c || !c.en_ligne ? 'hors' : (c.actif ? 'on-' + c.mode : 'off');
    if (camStable === null || cle(camBrute) === cle(camStable)) {
        camStable = camBrute; camCandidat = null; camCompte = 0;
    } else {
        if (camCandidat === cle(camBrute)) camCompte++; else { camCandidat = cle(camBrute); camCompte = 1; }
        if (camCompte >= 2) { camStable = camBrute; camCandidat = null; camCompte = 0; }
    }
    const cam = camStable;
    const vue = $('cam-view'), img = $('cam-img'), btn = $('cam-btn');
    const ot = $('cam-over-t'), os = $('cam-over-s');
    let live = false;

    if (!cam || !cam.en_ligne) {
        setBadge($('cam-badge'), 'SERVICE HORS LIGNE', 'off');
        ot.textContent = 'Service caméra injoignable';
        os.textContent = 'Lance « python vision.py » dans le dossier backend.';
        $('cam-msg').textContent = '';
        btn.disabled = true; btn.textContent = 'Activer la caméra'; camDemande = null;
    } else if (cam.actif) {
        live = true;
        const forcee = cam.mode === 'manuel';
        setBadge($('cam-badge'), forcee ? 'ACTIVE · FORCÉE' : 'ACTIVE · PRÉSENCE DÉTECTÉE', 'ok');
        $('cam-msg').textContent = forcee ? 'Extinction automatique après 5 min.' : '';
        btn.disabled = !forcee;
        btn.textContent = forcee ? 'Revenir en automatique' : 'Caméra active';
        camDemande = forcee ? 'auto' : null;
        if (!img.dataset.on) {
            img.dataset.on = '1';
            img.src = cam.flux + '?t=' + Date.now();
        }
    } else {
        setBadge($('cam-badge'), 'ÉTEINTE', 'off');
        ot.textContent = 'Caméra éteinte';
        os.textContent = cam.erreur ? cam.erreur : 'Aucune présence détectée.';
        $('cam-msg').textContent = '';
        btn.disabled = false; btn.textContent = 'Activer la caméra'; camDemande = 'on';
    }

    vue.classList.toggle('live', live);
    if (!live && img.dataset.on) {          // coupe la connexion au flux
        img.dataset.on = '';
        img.removeAttribute('src');
    }

    // Identification en cours (résultat de l'IA de reconnaissance)
    const ident = $('cam-ident');
    if (live && vision && (Date.now() / 1000 - vision.ts) < 5) {
        const nom = vision.label ? vision.label : 'personne';
        const txt = vision.connu ? `Membre reconnu : ${nom}` : (vision.visages > 0 ? 'Visage non reconnu' : 'Aucun visage');
        ident.innerHTML = '';
        const b = document.createElement('span');
        b.className = 'badge ' + (vision.connu ? 'ok' : (vision.visages > 0 ? 'bad' : 'off'));
        b.textContent = `${txt} (${Math.round(vision.confiance * 100)} %)`;
        ident.appendChild(b);
    } else {
        ident.textContent = '';
    }
}

async function envoyerCommandeCamera() {
    if (!camDemande) return;
    $('cam-btn').disabled = true;
    try {
        await fetch(`${API}/api/camera`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: camDemande }),
        });
    } catch (e) {
        console.error('Erreur /api/camera:', e);
    }
    setTimeout(fetchStatus, 800);
}

async function fetchStatus() {
    try {
        const t0 = performance.now();
        const st = await (await fetch(`${API}/api/status`)).json();
        const ms = Math.round(performance.now() - t0);
        setBadge($('liaison'), `Backend : en ligne (${ms} ms)`, ms > 500 ? 'warn' : 'ok');
        majBandeauEsp(st.decision);
        majTuiles(st.capteurs, st.env);
        majCamera(st.camera, st.vision);
    } catch (e) {
        setBadge($('liaison'), 'Backend : hors ligne', 'bad');
    }
}

// ---------------------------------------------------------------- Journal des événements
async function fetchEvents() {
    try {
        const { events } = await (await fetch(`${API}/api/events?limit=30`)).json();
        const sig = JSON.stringify(events.map(e => [e.id_evenement, e.acquitte]));
        if (sig === derniereSigEvents) return;
        derniereSigEvents = sig;
        const tbody = $('events-body');
        tbody.innerHTML = '';
        for (const e of events) {
            const tr = document.createElement('tr');
            tr.innerHTML = '<td></td><td><span class="badge"></span></td><td></td><td></td>';
            tr.children[0].textContent = new Date(e.horodatage).toLocaleTimeString('fr-FR');
            const code = tr.children[1].firstChild;
            code.textContent = e.code_scenario;
            code.classList.add(e.severite);
            tr.children[2].textContent = e.membre || e.categorie_env || e.message_ecran;   // textContent : pas d'injection HTML
            if (!e.acquitte && e.severite !== 'INFO') {
                const b = document.createElement('button');
                b.className = 'ack-btn';
                b.textContent = 'Acquitter';
                b.onclick = async () => {
                    await fetch(`${API}/api/events/${e.id_evenement}/ack`, { method: 'POST' });
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

// ---------------------------------------------------------------- Démarrage
// Relance la tâche seulement quand la précédente est terminée : pas d'empilement de requêtes si le backend ralentit.
async function boucle(tache, periodeMs) {
    try { await tache(); } finally { setTimeout(() => boucle(tache, periodeMs), periodeMs); }
}

document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    document.querySelectorAll('nav button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
    $('cam-btn').onclick = envoyerCommandeCamera;
    const depart = location.hash.slice(1);
    if (['capteurs', 'camera', 'evenements'].includes(depart)) showTab(depart);

    boucle(fetchStatus, 1000);
    boucle(fetchHistorique, 2000);
    boucle(fetchEvents, 3000);
});
