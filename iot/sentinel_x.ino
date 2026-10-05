/*
 * SENTINEL-X : firmware ESP8266
 *  - Publie les capteurs sur  sentinel/sensors   (JSON, cf. backend/MQTT_SCHEMA.md)
 *  - Reçoit les ordres sur    sentinel/commandes (JSON) : LED verte, buzzer, texte OLED
 *
 * Bibliothèques (Gestionnaire de bibliothèques Arduino) :
 *   PubSubClient, ArduinoJson (v7), DHT sensor library + Adafruit Unified Sensor,
 *   Adafruit GFX, Adafruit SSD1306  (+ Adafruit MLX90614 si USE_MLX90614 = 1)
 */
#include <ESP8266WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <DHT.h>

// Capteur infrarouge de température de cible (cas "Terminator"). 0 = absent : le backend
// se rabat alors sur le PIR (infrarouge passif) pour confirmer la chaleur humaine.
#define USE_MLX90614 0
#if USE_MLX90614
#include <Adafruit_MLX90614.h>
Adafruit_MLX90614 mlx;
#endif

// --- Paramètres Wi-Fi ---
const char* ssid = "VOTRE_SSID_WIFI";
const char* password = "VOTRE_MOT_DE_PASSE";

// --- Paramètres MQTT ---
const char* mqtt_server = "IP_DU_PC_SERVEUR"; // Remplacez par l'IP de l'ordinateur qui fait tourner Mosquitto
const int mqtt_port = 1883;
const char* TOPIC_CAPTEURS = "sentinel/sensors";
const char* TOPIC_COMMANDES = "sentinel/commandes";

WiFiClient espClient;
PubSubClient client(espClient);

// --- Définition des Pins (A adapter selon votre câblage) ---
#define PIR_PIN D1
#define BUZZER_PIN D2
#define MQ2_PIN A0
#define DHT_PIN D7
#define DHT_TYPE DHT22          // DHT22 selon le sujet du workshop
#define LED_VERTE_PIN D0        // LED verte "accès autorisé" (+ résistance 220 ohms)
// Bus I2C de l'OLED : par défaut D2/D1 sur NodeMCU, mais D1 = PIR et D2 = buzzer ici.
// On déplace donc l'I2C sur D5 (SDA) / D6 (SCL) pour ne pas toucher à votre câblage existant.
#define I2C_SDA D5
#define I2C_SCL D6

#define OLED_LARGEUR 128
#define OLED_HAUTEUR 64
#define OLED_ADRESSE 0x3C

DHT dht(DHT_PIN, DHT_TYPE);
Adafruit_SSD1306 oled(OLED_LARGEUR, OLED_HAUTEUR, &Wire, -1);

// --- État des actionneurs (piloté par le serveur) ---
bool ledVerte = false;
int buzzerMode = 0;                         // 0 off / 1 bip intermittent / 2 alarme continue rapide
char ligne1[24] = "SENTINEL-X";
char ligne2[24] = "En attente serveur";
bool ecranAJour = false;                    // true => l'OLED doit être redessiné
unsigned long derniereCommandeMs = 0;
const unsigned long COMMANDE_TTL_MS = 15000; // sans ordre du serveur au-delà : retour en mode neutre

void dessinerEcran() {
  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1);
  oled.setCursor(0, 0);
  oled.print(F("SENTINEL-X "));
  oled.print(WiFi.status() == WL_CONNECTED ? F("WiFi ") : F("noWiFi "));
  oled.print(client.connected() ? F("MQTT") : F("noMQTT"));
  oled.drawLine(0, 10, OLED_LARGEUR, 10, SSD1306_WHITE);

  // Ligne 1 : grande police si elle tient (10 caractères), sinon police normale
  bool grand = strlen(ligne1) <= 10;
  oled.setTextSize(grand ? 2 : 1);
  oled.setCursor(0, grand ? 18 : 22);
  oled.print(ligne1);

  // Ligne 2
  oled.setTextSize(1);
  oled.setCursor(0, 46);
  oled.print(ligne2);
  oled.display();
}

void appliquerCommande(bool led, int buzzer, const char* l1, const char* l2) {
  ledVerte = led;
  buzzerMode = constrain(buzzer, 0, 2);
  if (strcmp(l1, ligne1) != 0 || strcmp(l2, ligne2) != 0) {
    strlcpy(ligne1, l1, sizeof(ligne1));
    strlcpy(ligne2, l2, sizeof(ligne2));
    ecranAJour = true;
  }
}

// Réception des ordres du serveur : {"led_verte":true,"buzzer":0,"ligne1":"...","ligne2":"..."}
void onMessage(char* topic, byte* payload, unsigned int length) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, length)) {
    Serial.println("Commande JSON invalide");
    return;
  }
  derniereCommandeMs = millis();
  appliquerCommande(doc["led_verte"] | false,
                    doc["buzzer"] | 0,
                    doc["ligne1"] | "SENTINEL-X",
                    doc["ligne2"] | "");
}

// Buzzer non bloquant : mode 1 = 200 ms / 1 s, mode 2 = 150 ms / 150 ms
void gererBuzzer() {
  unsigned long now = millis();
  bool actif = false;
  if (buzzerMode == 1) actif = (now % 1000) < 200;
  else if (buzzerMode == 2) actif = (now % 300) < 150;
  if (actif) tone(BUZZER_PIN, 2000);
  else noTone(BUZZER_PIN);
}

void setup_wifi() {
  delay(10);
  Serial.println();
  Serial.print("Connexion à ");
  Serial.println(ssid);
  WiFi.begin(ssid, password);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("\nWiFi connecté");
  Serial.print("Adresse IP: ");
  Serial.println(WiFi.localIP());
}

// Reconnexion MQTT NON bloquante (une tentative toutes les 5 s) pour que l'écran,
// le buzzer et le délai de sécurité continuent de tourner pendant une coupure.
void reconnect() {
  static unsigned long derniereTentative = 0;
  if (millis() - derniereTentative < 5000) return;
  derniereTentative = millis();

  Serial.print("Tentative de connexion MQTT...");
  String clientId = "SentinelX-ESP8266-";
  clientId += String(random(0xffff), HEX);
  if (client.connect(clientId.c_str())) {
    Serial.println("connecté");
    client.subscribe(TOPIC_COMMANDES);   // ordres du serveur (LED verte, buzzer, OLED)
    ecranAJour = true;
  } else {
    Serial.print("échec, rc=");
    Serial.println(client.state());
  }
}

void publierCapteurs(int pir) {
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  int gaz = analogRead(MQ2_PIN);

  JsonDocument doc;
  if (isnan(t)) doc["temperature"] = nullptr; else doc["temperature"] = round(t * 10) / 10.0;
  if (isnan(h)) doc["humidite"] = nullptr;    else doc["humidite"] = round(h * 10) / 10.0;
  doc["gaz"] = gaz;                 // MQ-2 : gaz combustibles et fumées
  doc["presence"] = pir;
  doc["rfid_uid"] = nullptr;        // lecteur RFID pas encore câblé
#if USE_MLX90614
  doc["ir_temp"] = round(mlx.readObjectTempC() * 10) / 10.0;   // chaleur de la cible
#endif

  char buf[256];
  size_t n = serializeJson(doc, buf, sizeof(buf));
  Serial.print("Publication message: ");
  Serial.println(buf);
  client.publish(TOPIC_CAPTEURS, (const uint8_t*)buf, n);
}

void setup() {
  Serial.begin(115200);

  // Initialisation des pins
  pinMode(PIR_PIN, INPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  pinMode(LED_VERTE_PIN, OUTPUT);
  digitalWrite(LED_VERTE_PIN, LOW);
  noTone(BUZZER_PIN);

  dht.begin();
  Wire.begin(I2C_SDA, I2C_SCL);
  if (!oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADRESSE)) {
    Serial.println("OLED introuvable (vérifier SDA/SCL/adresse)");
  }
#if USE_MLX90614
  mlx.begin();
#endif
  dessinerEcran();

  // Initialisation Réseau
  setup_wifi();
  client.setBufferSize(512);
  client.setServer(mqtt_server, mqtt_port);
  client.setCallback(onMessage);
  derniereCommandeMs = millis();
  ecranAJour = true;
}

void loop() {
  if (!client.connected()) {
    reconnect();
  }
  client.loop();

  unsigned long now = millis();

  // --- Sécurité : le serveur ne donne plus d'ordres -> état neutre explicite ---
  if (now - derniereCommandeMs > COMMANDE_TTL_MS &&
      (ledVerte || buzzerMode != 0 || strcmp(ligne2, "Aucun ordre serveur") != 0)) {
    appliquerCommande(false, 0, "SERVEUR HS", "Aucun ordre serveur");
  }

  // --- Actionneurs ---
  digitalWrite(LED_VERTE_PIN, ledVerte ? HIGH : LOW);
  gererBuzzer();
  static unsigned long dernierEcran = 0;
  if (ecranAJour || now - dernierEcran > 5000) {   // rafraîchissement périodique (état Wi-Fi/MQTT)
    ecranAJour = false;
    dernierEcran = now;
    dessinerEcran();
  }

  // --- Lecture du PIR + envoi des données ---
  int pirState = digitalRead(PIR_PIN);
  static int dernierPir = 0;
  static unsigned long lastMsg = 0;

  // Envoi toutes les 2 secondes, ET immédiatement à chaque changement de présence
  // ("dès que le détecteur de présence s'active", pas 2 s plus tard).
  if (client.connected() && (now - lastMsg > 2000 || pirState != dernierPir)) {
    lastMsg = now;
    dernierPir = pirState;
    publierCapteurs(pirState);
  }
}
