import psycopg2
import csv
import datetime
import os
import time

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5433"))   # port exposé par Docker (voir infra/docker-compose.yml)
DB_NAME = "sentinelx"
DB_USER = "aether"
DB_PASS = "aether_password"
ARCHIVE_DIR = "../archives"

def get_db_connection():
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASS
    )

def archive_and_cleanup():
    """Exporte les données vieilles de plus de 2 heures en CSV et les supprime."""
    if not os.path.exists(ARCHIVE_DIR):
        os.makedirs(ARCHIVE_DIR)

    conn = get_db_connection()
    cur = conn.cursor()

    # Calcul de la date limite (ex: il y a 2 heures)
    threshold = datetime.datetime.now() - datetime.timedelta(hours=2)
    
    print(f"[{datetime.datetime.now()}] Lancement de l'archivage (données avant {threshold})")

    # 1. Sélectionner les vieilles données
    cur.execute("SELECT id, timestamp, temperature, humidite, gaz, presence FROM sensor_data WHERE timestamp < %s", (threshold,))
    rows = cur.fetchall()

    if not rows:
        print("Aucune donnée à archiver.")
        cur.close()
        conn.close()
        return

    # 2. Sauvegarder dans un fichier CSV
    filename = f"{ARCHIVE_DIR}/export_sentinel_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    with open(filename, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', 'timestamp', 'temperature', 'humidite', 'gaz', 'presence'])
        writer.writerows(rows)
    print(f"✅ {len(rows)} lignes sauvegardées dans {filename}")

    # 3. Supprimer de la base de données
    cur.execute("DELETE FROM sensor_data WHERE timestamp < %s", (threshold,))
    conn.commit()
    print(f"🗑️ {len(rows)} lignes supprimées de la base de données PostgreSQL.")

    cur.close()
    conn.close()

if __name__ == "__main__":
    print("🕰️ Démarrage du service d'archivage (vérification toutes les heures)...")
    while True:
        try:
            archive_and_cleanup()
        except Exception as e:
            print(f"❌ Erreur lors de l'archivage: {e}")
        
        # Attendre 1 heure avant la prochaine vérification
        time.sleep(3600)
