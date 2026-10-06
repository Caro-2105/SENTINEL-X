import threading

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor, Json
import datetime
import os

from scenarios import SCENARIOS

# Configuration de la base de données
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "5433"))
DB_NAME = "sentinelx"
DB_USER = "aether"
DB_PASS = "aether_password"

POOL_MAX = 10
_pool = None
_pool_lock = threading.Lock()
_places = threading.BoundedSemaphore(POOL_MAX)   # les appels attendent une place au lieu d'échouer


class _ConnexionPool:
    """Même interface qu'une connexion psycopg2, mais close() la rend au pool au lieu de la fermer."""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *a, **k):
        return self._conn.cursor(*a, **k)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        if self._conn is not None:
            _pool.putconn(self._conn)       # putconn annule toute transaction restée ouverte
            self._conn = None
            _places.release()

    def __del__(self):                      # si une requête échoue avant close(), la connexion revient quand même
        try:
            self.close()
        except Exception:
            pass


def get_db_connection():
    """Connexion réutilisée depuis un pool : ouvrir une connexion à chaque appel coûtait des centaines de ms."""
    global _pool
    if not _places.acquire(timeout=10):
        print("❌ Base de données saturée : plus de connexion disponible après 10 s")
        return None
    place_prise = True
    try:
        with _pool_lock:
            if _pool is None:
                _pool = pg_pool.ThreadedConnectionPool(
                    1, POOL_MAX, host=DB_HOST, port=DB_PORT, database=DB_NAME, user=DB_USER, password=DB_PASS)
        for _ in range(8):
            conn = _pool.getconn()
            try:                            # base/Docker redémarrés : une connexion du pool peut être morte
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.rollback()
                place_prise = False         # la place est rendue par close()
                return _ConnexionPool(conn)
            except psycopg2.Error:
                _pool.putconn(conn, close=True)
        raise psycopg2.OperationalError("aucune connexion valide dans le pool")
    except UnicodeDecodeError:
        # Sous Windows, un PostgreSQL installé en local répond en français (cp1252) et psycopg2
        # ne sait pas lire son message d'erreur : on est probablement connecté au MAUVAIS serveur.
        print(f"❌ Connexion à {DB_HOST}:{DB_PORT} refusée avec un message non-UTF8 : un PostgreSQL "
              "Windows local occupe sans doute le port. Voir `netstat -ano | findstr :5433`, "
              "ou changer le port Docker et définir DB_PORT.")
        return None
    except Exception as e:
        print(f"❌ Erreur de connexion à la base de données: {e}")
        return None
    finally:
        if place_prise:
            _places.release()

def init_db():
    conn = get_db_connection()
    if conn is None:
        return

    cur = conn.cursor()
    # Création de la table principale
    cur.execute('''
        CREATE TABLE IF NOT EXISTS sensor_data (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            temperature REAL,
            humidite REAL,
            gaz INTEGER,
            presence INTEGER,
            rfid_uid VARCHAR(50)
        )
    ''')
    # Température infrarouge de la cible (optionnelle, capteur IR du "cas Terminator")
    cur.execute("ALTER TABLE sensor_data ADD COLUMN IF NOT EXISTS ir_temp REAL")

    # --- Tables des stories (voir docs Merise) ---
    cur.execute('''
        CREATE TABLE IF NOT EXISTS membre (
            id_membre   SERIAL PRIMARY KEY,
            label_ia    VARCHAR(60) NOT NULL UNIQUE,
            nom_affiche VARCHAR(40) NOT NULL,
            rfid_uid    VARCHAR(50) UNIQUE,
            actif       BOOLEAN NOT NULL DEFAULT TRUE,
            cree_le     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS scenario (
            code_scenario VARCHAR(10) PRIMARY KEY,
            categorie     VARCHAR(15) NOT NULL CHECK (categorie IN ('ACCES','ENVIRONNEMENT','SYSTEME')),
            libelle       VARCHAR(120) NOT NULL,
            severite      VARCHAR(10) NOT NULL CHECK (severite IN ('INFO','WARNING','CRITICAL')),
            led_verte     BOOLEAN NOT NULL,
            buzzer        SMALLINT NOT NULL CHECK (buzzer BETWEEN 0 AND 2),
            ligne1_ecran  VARCHAR(21) NOT NULL,
            ligne2_ecran  VARCHAR(21) NOT NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS detection_visage (
            id_detection SERIAL PRIMARY KEY,
            horodatage   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            label_ia     VARCHAR(60),
            confiance    REAL CHECK (confiance BETWEEN 0 AND 1),
            connu        BOOLEAN NOT NULL,
            id_membre    INTEGER REFERENCES membre(id_membre) ON DELETE SET NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS evenement (
            id_evenement  SERIAL PRIMARY KEY,
            horodatage    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            code_scenario VARCHAR(10) NOT NULL REFERENCES scenario(code_scenario),
            id_mesure     INTEGER REFERENCES sensor_data(id) ON DELETE SET NULL,
            id_detection  INTEGER REFERENCES detection_visage(id_detection) ON DELETE SET NULL,
            categorie_env VARCHAR(15),
            score_ia      REAL,
            modele_ia     VARCHAR(60),
            message_ecran VARCHAR(45) NOT NULL,
            instantane    JSONB,
            acquitte      BOOLEAN NOT NULL DEFAULT FALSE,
            acquitte_le   TIMESTAMP
        )
    ''')
    # Catalogue des scénarios : source unique = scenarios.SCENARIOS
    for code, s in SCENARIOS.items():
        cur.execute('''
            INSERT INTO scenario (code_scenario, categorie, libelle, severite, led_verte, buzzer, ligne1_ecran, ligne2_ecran)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (code_scenario) DO UPDATE SET
                categorie = EXCLUDED.categorie, libelle = EXCLUDED.libelle, severite = EXCLUDED.severite,
                led_verte = EXCLUDED.led_verte, buzzer = EXCLUDED.buzzer,
                ligne1_ecran = EXCLUDED.ligne1_ecran, ligne2_ecran = EXCLUDED.ligne2_ecran
        ''', (code, s["categorie"], s["libelle"], s["severite"], s["led_verte"], s["buzzer"],
              s["ligne1"], s["ligne2"]))
    conn.commit()
    cur.close()
    conn.close()
    print("✅ Base de données initialisée avec succès.")

def insert_sensor_data(temperature, humidite, gaz, presence, rfid_uid, ir_temp=None):
    """Insère une mesure et renvoie son id (None si la base est indisponible)."""
    conn = get_db_connection()
    if conn is None:
        return None

    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sensor_data (temperature, humidite, gaz, presence, rfid_uid, ir_temp) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
        (temperature, humidite, gaz, presence, rfid_uid, ir_temp)
    )
    id_mesure = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return id_mesure

def get_latest_data(limit=50):
    """Récupère les dernières entrées pour afficher les graphiques."""
    conn = get_db_connection()
    if conn is None:
        return []

    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT * FROM sensor_data ORDER BY timestamp DESC LIMIT %s", (limit,))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    # On renvoie dans l'ordre chronologique pour le graphique
    return list(reversed(rows))

def get_recent_series(seconds=300):
    """Mesures des `seconds` dernières secondes (ordre chronologique), pour l'IA analytique.

    `ts` = epoch du timestamp stocké (cohérent d'une ligne à l'autre, seule la différence compte).
    """
    conn = get_db_connection()
    if conn is None:
        return []
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT EXTRACT(EPOCH FROM timestamp)::float8 AS ts, temperature, humidite, gaz "
        "FROM sensor_data WHERE timestamp >= CURRENT_TIMESTAMP - make_interval(secs => %s) "
        "ORDER BY timestamp", (seconds,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

# ----------------------------------------------------------------------------------
# Stories : membres, détections visage, événements
# ----------------------------------------------------------------------------------
def load_membres():
    """{label_ia: {"id", "nom", "actif"}} pour le moteur de scénarios."""
    conn = get_db_connection()
    if conn is None:
        return {}
    cur = conn.cursor()
    cur.execute("SELECT id_membre, label_ia, nom_affiche, actif FROM membre")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return {r[1]: {"id": r[0], "nom": r[2], "actif": r[3]} for r in rows}

def ensure_membre(label_ia):
    """Auto-enregistre un membre reconnu par le modèle IA s'il n'existe pas encore."""
    conn = get_db_connection()
    if conn is None:
        return
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO membre (label_ia, nom_affiche) VALUES (%s, %s) ON CONFLICT (label_ia) DO NOTHING",
        (label_ia, label_ia[:40].title())
    )
    conn.commit()
    cur.close()
    conn.close()

def insert_detection(label_ia, confiance, connu, id_membre=None):
    conn = get_db_connection()
    if conn is None:
        return None
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO detection_visage (label_ia, confiance, connu, id_membre) "
        "VALUES (%s, %s, %s, %s) RETURNING id_detection",
        (label_ia, confiance, connu, id_membre)
    )
    id_detection = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return id_detection

def insert_evenement(code_scenario, message_ecran, id_mesure=None, id_detection=None,
                     categorie_env=None, score_ia=None, modele_ia=None, instantane=None):
    conn = get_db_connection()
    if conn is None:
        return None
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO evenement (code_scenario, id_mesure, id_detection, categorie_env, "
        "score_ia, modele_ia, message_ecran, instantane) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id_evenement",
        (code_scenario, id_mesure, id_detection, categorie_env, score_ia, modele_ia,
         message_ecran, Json(instantane) if instantane is not None else None)
    )
    id_evenement = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return id_evenement

def get_evenements(limit=50):
    conn = get_db_connection()
    if conn is None:
        return []
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(
        "SELECT e.id_evenement, e.horodatage, e.code_scenario, s.libelle, s.severite, "
        "       m.nom_affiche AS membre, e.categorie_env, e.score_ia, e.modele_ia, "
        "       e.message_ecran, e.instantane, e.acquitte "
        "FROM evenement e "
        "JOIN scenario s ON s.code_scenario = e.code_scenario "
        "LEFT JOIN detection_visage d ON d.id_detection = e.id_detection "
        "LEFT JOIN membre m ON m.id_membre = d.id_membre "
        "ORDER BY e.horodatage DESC LIMIT %s", (limit,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def acquitter_evenement(id_evenement):
    conn = get_db_connection()
    if conn is None:
        return False
    cur = conn.cursor()
    cur.execute(
        "UPDATE evenement SET acquitte = TRUE, acquitte_le = %s WHERE id_evenement = %s",
        (datetime.datetime.now(), id_evenement)
    )
    ok = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()
    return ok
