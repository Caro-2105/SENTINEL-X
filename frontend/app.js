// 127.0.0.1 et non "localhost" : sous Windows, "localhost" essaie d'abord l'IPv6 (::1), d'où des délais de 0,3 à 2 s.
const API = location.protocol.startsWith('http') ? location.origin : 'http://127.0.0.1:8000';
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
    if (nom === 'meteo') { meteoChart.resize(); fetchMeteo(); }
    if (nom === 'admin') chargerComptes();
}

// ---------------------------------------------------------------- Courbes
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#334155';

let envChart, gasChart, meteoChart;
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
// ---------------------------------------------------------------- Session
let token = null;
try { token = sessionStorage.getItem('sx_token'); } catch (e) { /* stockage indisponible : on se reconnectera */ }

// Appel à l'API avec le jeton de session ; un 401 renvoie à la mire de connexion.
async function api(chemin, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const r = await fetch(API + chemin, { ...options, headers });
    if (r.status === 401 && token) { deconnecter(false); throw new Error('session expirée'); }
    return r;
}

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

function initMeteoChart() {
    meteoChart = new Chart($('meteoChart').getContext('2d'), {
        type: 'line',
        data: { labels: [], datasets: [
            { label: 'Probabilité IA (%)', borderColor: '#f87171', backgroundColor: 'rgba(248,113,113,0.15)', borderWidth: 2, fill: true, data: [], yAxisID: 'y', spanGaps: false },
            { label: 'Rafales (km/h)', borderColor: '#38bdf8', borderWidth: 2, data: [], yAxisID: 'y1' },
        ] },
        options: { responsive: true, maintainAspectRatio: false, animation: false,
            interaction: { mode: 'index', intersect: false },
            elements: { line: { tension: 0.25 }, point: { radius: 0, hoverRadius: 4 } },
            scales: {
                x: { ticks: { maxTicksLimit: 10, maxRotation: 0 } },
                y: { type: 'linear', position: 'left', min: 0, max: 100, title: { display: true, text: '%' } },
                y1: { type: 'linear', position: 'right', min: 0, suggestedMax: 100, grid: { drawOnChartArea: false }, title: { display: true, text: 'km/h' } },
            } },
    });
}

async function fetchHistorique() {
    if (!token) return;
    try {
        const { history } = await (await api(`/api/data/history?limit=60`)).json();
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
    setBadge($('buzzer-etat'), BUZZER_TXT[d.buzzer] || '?', d.buzzer > 0 ? 'bad' : 'ok');
    setBadge($('scenarios-actifs'), d.scenarios.join(' + '), 'off');
}

function majTuiles(c, env) {
    const frais = c && (Date.now() / 1000 - c.ts) < PERIME_S;
    if (!frais) {
        ['status-temp', 'status-hum', 'status-pir', 'status-gas'].forEach(id => setBadge($(id), 'PAS DE DONNÉES', 'off'));
    } else {
        setBadge($('status-temp'), c.temperature == null ? '—' : c.temperature.toFixed(1) + ' °C', 'ok');
        setBadge($('status-hum'), c.humidite == null ? '—' : Math.round(c.humidite) + ' %', 'ok');
        setBadge($('status-pir'), c.presence === 1 ? 'PRÉSENCE' : 'RAS', c.presence === 1 ? 'warn' : 'ok');
        const g = c.gaz;
        setBadge($('status-gas'), g === null ? '—' : `${Math.round(g)}`, g >= 600 ? 'bad' : g >= 400 ? 'warn' : 'ok');
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
            img.src = cam.flux + '&t=' + Date.now();
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
    if (!camDemande || !token) return;
    $('cam-btn').disabled = true;
    try {
        await api(`/api/camera`, {
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
    if (!token) return;
    try {
        const t0 = performance.now();
        const st = await (await api(`/api/status`)).json();
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
    if (!token) return;
    try {
        const { events } = await (await api(`/api/events?limit=30`)).json();
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
                    await api(`/api/events/${e.id_evenement}/ack`, { method: 'POST' });
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

// ---------------------------------------------------------------- Mire d'authentification
let loginId = null, loginSeq = 0;

function apercuLogin(url) {
    const img = $('login-cam');
    if (url && !img.dataset.on) {
        img.dataset.on = '1';
        img.src = url + '&t=' + Date.now();
    } else if (!url && img.dataset.on) {
        img.dataset.on = '';
        img.removeAttribute('src');
    }
    $('login-cam-box').hidden = !url;
    $('login-scan').hidden = !!url;
}

function etapeLogin(n) {
    if (n !== 1) apercuLogin(null);
    $('etape1').className = 'etape ' + (n === 1 ? 'actif' : 'fait');
    $('etape2').className = 'etape ' + (n === 2 ? 'actif' : '');
    $('login-visage').hidden = n !== 1;
    $('login-form').hidden = n !== 2;
}

function messageLogin(texte, erreur = false) {
    $('login-msg').textContent = texte;
    $('login-err').textContent = erreur ? texte : '';
}

async function demarrerLogin() {
    const seq = ++loginSeq;                       // invalide les relevés d'une tentative précédente
    loginId = null;
    etapeLogin(1);
    $('login-retry').hidden = true;
    $('login-prog').style.width = '0%';
    $('login-err').textContent = '';
    $('login-mdp').value = '';
    messageLogin('Connexion au serveur…');
    try {
        const r = await fetch(`${API}/api/auth/face/start`, { method: 'POST' });
        loginId = (await r.json()).login_id;
        suivreVisage(seq);
    } catch (e) {
        messageLogin('Serveur injoignable : lancer « python main.py ».');
        setTimeout(() => { if (seq === loginSeq && !token) demarrerLogin(); }, 3000);
    }
}

async function suivreVisage(seq) {
    if (seq !== loginSeq || token) return;
    try {
        const st = await (await fetch(`${API}/api/auth/face/${loginId}`)).json();
        if (seq !== loginSeq) return;
        if (st.etape === 'mot_de_passe') {
            etapeLogin(2);
            $('login-salut').textContent = `Bonjour ${st.utilisateur}`;
            $('login-mdp').focus();
            return;                                // fin du suivi : on attend le mot de passe
        }
        if (st.etape === 'expire') {
            apercuLogin(null);
            messageLogin(st.message);
            $('login-retry').hidden = false;
            return;
        }
        messageLogin(st.message);
        apercuLogin(st.apercu || null);
        $('login-prog').style.width = Math.round((st.progression || 0) * 100) + '%';
    } catch (e) {
        messageLogin('Serveur injoignable…');
    }
    setTimeout(() => suivreVisage(seq), 400);
}

async function envoyerMotDePasse(ev) {
    ev.preventDefault();
    const bouton = $('login-ok');
    bouton.disabled = true;
    $('login-err').textContent = '';
    try {
        const r = await fetch(`${API}/api/auth/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ login_id: loginId, mot_de_passe: $('login-mdp').value }),
        });
        const corps = await r.json();
        if (r.ok) {
            token = corps.token;
            try { sessionStorage.setItem('sx_token', token); } catch (e) { /* ignoré */ }
            $('login-mdp').value = '';
            entrer(corps.utilisateur);
            return;
        }
        const d = corps.detail || {};
        $('login-err').textContent = d.message || 'Connexion refusée.';
        $('login-mdp').value = '';
        if (d.code === 'face_requise' || d.code === 'verrouille' || d.code === 'sans_mot_de_passe') {
            setTimeout(demarrerLogin, d.code === 'verrouille' ? Math.min((d.retry_after || 5) * 1000, 4000) : (d.code === 'sans_mot_de_passe' ? 6000 : 1500));
        } else {
            $('login-mdp').focus();
        }
    } catch (e) {
        $('login-err').textContent = 'Serveur injoignable.';
    } finally {
        bouton.disabled = false;
    }
}

// ---------------------------------------------------------------- Prévisions météo
let estAdmin = false;
const NIVEAUX_METEO = [['Aucun danger', 'ok'], ['Vigilance', 'warn'], ['Danger', 'bad']];

function msgMeteo(texte, ok = false) {
    const el = $('met-msg');
    el.textContent = texte;
    el.className = 'adm-msg ' + (ok ? 'ok' : 'err');
}

async function fetchMeteo() {
    if (!token) return;
    try {
        const r = await api('/api/meteo');
        if (!r.ok) return;
        afficherMeteo(await r.json());
    } catch (e) { console.error('Erreur /api/meteo:', e); }
}

function afficherMeteo(m) {
    $('met-form').hidden = !estAdmin;
    if (m.lieu) $('met-lieu').textContent = m.lieu.nom + (m.lieu.pays ? ' (' + m.lieu.pays + ')' : '');
    if (m.etat !== 'ok') {
        const txt = { chargement: 'Chargement des prévisions…', desactive: 'Prévisions désactivées (--sans-meteo).' }[m.etat]
            || ('Prévisions indisponibles : ' + (m.message || 'erreur'));
        setBadge($('met-niveau'), m.etat === 'chargement' ? '…' : '—', 'off');
        setBadge($('met-ia'), '—', 'off');
        $('met-actuel').textContent = '—';
        $('met-modele').textContent = txt;
        $('met-dangers').replaceChildren();
        return;
    }
    const [lib, cls] = NIVEAUX_METEO[m.niveau] || NIVEAUX_METEO[0];
    setBadge($('met-niveau'), lib, cls);
    const a = m.actuel;
    $('met-actuel').textContent = `${a.temperature} °C · ${a.humidite} % · rafales ${a.rafales} km/h · ${a.pression} hPa`;

    const ia = m.ia || {};
    if (ia.etat === 'pret') {
        setBadge($('met-ia'), `${Math.round(ia.proba_max * 100)} % max`, (NIVEAUX_METEO[ia.niveau] || NIVEAUX_METEO[0])[1]);
        const auc = ia.auc === null || ia.auc === undefined ? 'n/a' : ia.auc.toFixed(2);
        $('met-modele').textContent = `Modèle entraîné sur ${ia.exemples} heures d'historique (${ia.positifs} cas notables, `
            + `${(ia.taux_base * 100).toFixed(1)} % des heures). Fiabilité sur données jamais vues (AUC) : ${auc}.` 
            + (m.avertissement ? ' Dernière mise à jour impossible : ' + m.avertissement : '');
    } else {
        setBadge($('met-ia'), ia.etat === 'entrainement' ? 'Entraînement…' : 'Indisponible', 'off');
        $('met-modele').textContent = ia.message || '';
    }

    const corps = $('met-dangers');
    corps.replaceChildren();
    if (!m.dangers.length) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 4; td.textContent = 'Aucun danger prévu par les seuils sur 72 h.';
        tr.appendChild(td); corps.appendChild(tr);
    }
    for (const d of m.dangers) {
        const tr = document.createElement('tr');
        const cases = [d.libelle, null, new Date(d.quand).toLocaleString('fr-FR', { weekday: 'short', hour: '2-digit', minute: '2-digit' }), `${d.valeur} ${d.unite}`];
        for (const c of cases) {
            const td = document.createElement('td');
            if (c === null) {
                const b = document.createElement('span');
                b.className = 'badge ' + NIVEAUX_METEO[d.niveau][1];
                b.textContent = NIVEAUX_METEO[d.niveau][0];
                td.appendChild(b);
            } else td.textContent = c;
            tr.appendChild(td);
        }
        corps.appendChild(tr);
    }

    meteoChart.data.labels = m.serie.map(s => new Date(s.t).toLocaleString('fr-FR', { weekday: 'short', hour: '2-digit', minute: '2-digit' }));
    meteoChart.data.datasets[0].data = m.serie.map(s => s.proba === null ? null : Math.round(s.proba * 100));
    meteoChart.data.datasets[1].data = m.serie.map(s => s.rafales);
    meteoChart.update('none');
}

async function changerLieu(ev) {
    ev.preventDefault();
    $('met-ok').disabled = true;
    msgMeteo('Recherche du lieu…', true);
    try {
        const r = await api('/api/meteo/lieu', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                 body: JSON.stringify({ nom: $('met-nom').value }) });
        if (r.ok) {
            msgMeteo('Lieu appliqué : l\'IA va s\'entraîner sur son historique (1 à 2 minutes).', true);
            $('met-nom').value = '';
            fetchMeteo();
        } else msgMeteo(await erreurApi(r));
    } catch (e) { msgMeteo('Serveur injoignable.'); }
    finally { $('met-ok').disabled = false; }
}

// ---------------------------------------------------------------- Administration (comptes)
let moiLabel = null;

function msgAdmin(texte, ok = false) {
    const el = $('adm-msg');
    el.textContent = texte;
    el.className = 'adm-msg ' + (ok ? 'ok' : 'err');
}

async function erreurApi(r) {
    try {
        const d = (await r.json()).detail;
        return typeof d === 'string' ? d : (Array.isArray(d) ? 'Données invalides.' : (d && d.message) || 'Erreur.');
    } catch (e) { return 'Erreur ' + r.status; }
}

async function majAdmin(nom) {
    let admin = false;
    try {
        const r = await api('/api/auth/me');
        if (r.ok) admin = !!(await r.json()).admin;
    } catch (e) { /* ignoré */ }
    estAdmin = admin;
    $('met-form').hidden = !admin;
    $('nav-admin').hidden = !admin;
    if (!admin && $('tab-admin').classList.contains('active')) showTab('capteurs');
    if (admin) chargerComptes();
}

async function chargerComptes() {
    try {
        const r = await api('/api/admin/membres');
        if (!r.ok) return;
        const d = await r.json();
        moiLabel = d.moi;
        $('adm-amorcage').hidden = !d.amorcage;
        const corps = $('adm-body');
        corps.replaceChildren();
        for (const m of d.membres) {
            const tr = document.createElement('tr');
            tr.className = 'adm-row';
            const c1 = document.createElement('td');
            c1.textContent = m.nom === m.label ? m.label : `${m.nom} (${m.label})`;
            const c2 = document.createElement('td');
            const b = document.createElement('span');
            b.className = 'badge ' + (m.actif ? (m.a_mot_de_passe ? 'ok' : 'warn') : 'off');
            b.textContent = !m.actif ? 'Désactivé' : (m.a_mot_de_passe ? 'Actif' : 'Sans mot de passe');
            c2.appendChild(b);
            const c3 = document.createElement('td');
            c3.textContent = m.admin ? 'Admin' : 'Membre';
            const c4 = document.createElement('td');
            const bouton = (txt, fn) => {
                const x = document.createElement('button');
                x.className = 'mini'; x.type = 'button'; x.textContent = txt; x.onclick = fn;
                c4.appendChild(x); return x;
            };
            const soi = m.label === moiLabel;
            bouton(m.actif ? 'Désactiver' : 'Activer', () => modifierCompte(m.label, { actif: !m.actif })).disabled = soi && m.actif;
            bouton(m.admin ? 'Retirer admin' : 'Rendre admin', () => modifierCompte(m.label, { admin: !m.admin })).disabled = soi && m.admin;
            bouton('Mot de passe', () => saisirMdp(tr, c4, m.label));
            tr.append(c1, c2, c3, c4);
            corps.appendChild(tr);
        }
    } catch (e) { /* le prochain affichage de l'onglet réessaiera */ }
}

function saisirMdp(tr, cellule, label) {
    cellule.replaceChildren();
    const champ = document.createElement('input');
    champ.type = 'password'; champ.placeholder = 'Nouveau mot de passe'; champ.maxLength = 256;
    champ.autocomplete = 'new-password';
    const ok = document.createElement('button');
    ok.className = 'mini'; ok.type = 'button'; ok.textContent = 'OK'; ok.style.marginTop = '4px';
    const annuler = document.createElement('button');
    annuler.className = 'mini'; annuler.type = 'button'; annuler.textContent = 'Annuler';
    ok.onclick = () => modifierCompte(label, { mot_de_passe: champ.value }, 'Mot de passe de ' + label + ' modifié.');
    annuler.onclick = chargerComptes;
    cellule.append(champ, ok, annuler);
    champ.focus();
}

async function modifierCompte(label, corps, succes) {
    try {
        const r = await api('/api/admin/membres/' + encodeURIComponent(label), {
            method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(corps) });
        if (r.ok) msgAdmin(succes || 'Compte ' + label + ' mis à jour.', true);
        else msgAdmin(await erreurApi(r));
    } catch (e) { msgAdmin('Serveur injoignable.'); }
    majAdmin();
}

async function creerCompte(ev) {
    ev.preventDefault();
    $('adm-ok').disabled = true;
    try {
        const r = await api('/api/admin/membres', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label: $('adm-label').value, nom_affiche: $('adm-nom').value || null,
                                   mot_de_passe: $('adm-mdp').value, admin: $('adm-admin').checked }) });
        if (r.ok) {
            msgAdmin('Compte « ' + $('adm-label').value.trim() + ' » créé.', true);
            $('adm-form').reset();
            majAdmin();
        } else msgAdmin(await erreurApi(r));
    } catch (e) { msgAdmin('Serveur injoignable.'); }
    finally { $('adm-ok').disabled = false; }
}

function entrer(nom) {
    $('login').hidden = true;
    $('app').hidden = false;
    $('user-nom').textContent = nom || '';
    derniereSignature = null; derniereSigEvents = null;
    envChart.resize(); gasChart.resize();
    fetchStatus(); fetchHistorique(); fetchEvents();
    msgAdmin('');
    majAdmin(nom);
}

async function deconnecter(prevenirServeur = true) {
    const ancien = token;
    token = null;
    try { sessionStorage.removeItem('sx_token'); } catch (e) { /* ignoré */ }
    if (prevenirServeur && ancien) {
        try { await fetch(`${API}/api/auth/logout`, { method: 'POST', headers: { Authorization: 'Bearer ' + ancien } }); } catch (e) { /* ignoré */ }
    }
    $('app').hidden = true;
    $('login').hidden = false;
    camStable = null;
    demarrerLogin();
}

// ---------------------------------------------------------------- Démarrage
// Relance la tâche seulement quand la précédente est terminée : pas d'empilement de requêtes si le backend ralentit.
async function boucle(tache, periodeMs) {
    try { await tache(); } finally { setTimeout(() => boucle(tache, periodeMs), periodeMs); }
}

document.addEventListener('DOMContentLoaded', async () => {
    initCharts();
    initMeteoChart();
    document.querySelectorAll('nav button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
    $('cam-btn').onclick = envoyerCommandeCamera;
    $('logout-btn').onclick = () => deconnecter(true);
    $('login-form').onsubmit = envoyerMotDePasse;
    $('adm-form').onsubmit = creerCompte;
    $('met-form').onsubmit = changerLieu;
    $('login-retry').onclick = demarrerLogin;
    const depart = location.hash.slice(1);
    if (['capteurs', 'camera', 'evenements', 'meteo'].includes(depart)) showTab(depart);   // « admin » : seulement après contrôle du rôle

    boucle(fetchStatus, 1000);
    boucle(fetchHistorique, 2000);
    boucle(fetchEvents, 3000);
    boucle(fetchMeteo, 60000);

    // Session déjà ouverte dans cet onglet ? (le serveur reste seul juge de sa validité)
    if (token) {
        try {
            const r = await fetch(`${API}/api/auth/me`, { headers: { Authorization: 'Bearer ' + token } });
            if (r.ok) { entrer((await r.json()).utilisateur); return; }
        } catch (e) { /* serveur absent : mire de connexion */ }
        token = null;
        try { sessionStorage.removeItem('sx_token'); } catch (e) { /* ignoré */ }
    }
    // Mode développeur (serveur lancé avec --dev) : entrée directe, sans caméra
    let dev = false;
    try { dev = !!(await (await fetch(`${API}/api/health`)).json()).dev; } catch (e) { /* serveur absent */ }
    if (dev) {
        $('login-visage').hidden = true;
        $('login-dev').hidden = false;
        $('login-dev-btn').onclick = async () => {
            try {
                const r = await fetch(`${API}/api/auth/dev`, { method: 'POST' });
                if (!r.ok) { $('login-err').textContent = 'Mode dev indisponible.'; return; }
                const d = await r.json();
                token = d.token;
                try { sessionStorage.setItem('sx_token', token); } catch (e) { /* ignoré */ }
                entrer(d.utilisateur);
            } catch (e) { $('login-err').textContent = 'Serveur injoignable.'; }
        };
        $('login-dev-visage').onclick = (ev) => {
            ev.preventDefault();
            $('login-dev').hidden = true;
            $('login-visage').hidden = false;
            demarrerLogin();
        };
        return;
    }
    demarrerLogin();
});
