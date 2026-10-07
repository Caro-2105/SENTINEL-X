# Patch firmware ESP : afficher les messages du back et faire sonner le buzzer

Le firmware actuel (broker `uMQTTBroker` sur l'ESP) répond aux demandes `esp/request/*` mais n'affiche pas les
messages du back (« Bienvenue Caroline », « INTRUSION »...) et ne fait pas sonner le buzzer.
Le pont `backend/pont_esp.py` envoie désormais chaque ordre du back sur le topic **`esp/cmd/ecran`**, au format texte :

    buzzer|ligne1|ligne2          ex.   2|INTRUSION|Personne inconnue      (buzzer 0 = off, 1 = bips, 2 = alarme)

Le back renvoie l'ordre à chaque changement et toutes les 2 secondes : l'ESP considère le message valide
6 s après la dernière réception.

## 1. Variables et fonction (avant `class Broker`)

```cpp
static const char* TOPIC_ECRAN = "esp/cmd/ecran";
static int alerteBuzzer = 0;
static unsigned long messageJusqua = 0, dernierBip = 0;

static void recevoirEcran(const char* data, uint32_t length) {
  char buf[64];
  size_t n = length < sizeof(buf) - 1 ? length : sizeof(buf) - 1;
  memcpy(buf, data, n); buf[n] = '\0';
  char* p1 = strchr(buf, '|');  if (!p1) return;  *p1 = '\0';
  char* p2 = strchr(p1 + 1, '|'); if (!p2) return; *p2 = '\0';
  alerteBuzzer = atoi(buf);
  char l[17];
  snprintf(l, sizeof(l), "%-16.16s", p1 + 1); lcd.setCursor(0, 0); lcd.print(l);
  snprintf(l, sizeof(l), "%-16.16s", p2 + 1); lcd.setCursor(0, 1); lcd.print(l);
  messageJusqua = millis() + 6000;
}
```

## 2. `updateLCD` : ne pas écraser le message

Ajouter en première ligne de `updateLCD(...)` :

```cpp
  if (millis() < messageJusqua) return;   // un message du back est affiché
```

## 3. `Broker::onData` : traiter le nouveau topic (au début de la fonction)

```cpp
  if (topic == TOPIC_ECRAN) { recevoirEcran(data, length); return; }
```

## 4. `initMQTT` : s'abonner (dans la fonction, après la boucle)

```cpp
  mqtt.subscribe(TOPIC_ECRAN);
```

## 5. `loop()` : buzzer

```cpp
void loop() {
  unsigned long now = millis();
  if (now >= messageJusqua) alerteBuzzer = 0;          // plus d'ordre récent : silence
  if (alerteBuzzer == 1 && now - dernierBip > 1000) { tone(Config::SPEAKER_PIN, 1000, 100); dernierBip = now; }
  if (alerteBuzzer == 2 && now - dernierBip > 300)  { tone(Config::SPEAKER_PIN, 2000, 200); dernierBip = now; }
}
```

---

# Ajouter l'humidité et le gaz aux réponses de l'ESP

Dans le firmware actuel, la table `mqttRoutes` ne contient que `hello`, `DIST` et `TEMP` : l'ESP ne sait donc pas
répondre pour l'humidité et le gaz, même si la carte capteurs les mesure. Ajouter deux lignes :

```cpp
static const MqttRoute mqttRoutes[] = {
  {"esp/request/hello", "hello", "esp/response/hello"},
  {"esp/request/dist",  "DIST",  "esp/response/dist"},
  {"esp/request/temp",  "TEMP",  "esp/response/temp"},
  {"esp/request/hum",   "HUM",   "esp/response/hum"},     // humidité (%)
  {"esp/request/gaz",   "GAZ",   "esp/response/gaz"}      // gaz, valeur brute (MQ-2)
};
```

Il faut aussi que la **carte capteurs** (celle qui répond sur le port série) comprenne les commandes `HUM` et `GAZ`
et réponde par un nombre sur une ligne, comme elle le fait déjà pour `TEMP` et `DIST`. Côté PC :

    python lancer.py --esp 10.235.154.64 --esp-capteurs temp,dist,hum,gaz
