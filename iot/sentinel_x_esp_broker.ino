// =====================================================================================
// SENTINEL-X : firmware ESP8266 (NodeMCU) — mini-broker MQTT + écran LCD + haut-parleur
//
// Version avec FILE D'ATTENTE : les messages MQTT reçus sont seulement rangés dans une file ;
// tout le travail bloquant (attente de la carte capteurs sur la liaison série, bips, écran)
// se fait dans loop(). Il ne faut JAMAIS appeler yield()/delay() longuement dans onData :
// cette fonction est appelée par la pile réseau, où yield() provoque un plantage + redémarrage.
//
// PROTOCOLE (le PC se connecte à ce mini-broker sur le port 1883) :
//   PC -> ESP   esp/requetes/<capteur>   capteur = temp | dist | hum | gaz   (contenu ignoré)
//   ESP -> PC   esp/response/<capteur>   {"result":"<valeur>"}               (valeur renvoyée par la carte capteurs)
//   PC -> ESP   esp/function/screen      {"str":"<ligne 1 sur 16 car.><ligne 2>"}  (32 car. max, sans guillemet)
//                                        {"str":""} = écran vide, silencieux
//   PC -> ESP   esp/function/granted     mélodie « accès accordé »
//   PC -> ESP   esp/function/denied      mélodie « accès refusé »
//   ESP -> PC   esp/presence             {"result":"<cm>"} publié de lui-même quand la distance < 50 cm
//
// Liaison série vers la carte capteurs (9600 bauds) : l'ESP envoie TEMP, DIST, HUM ou GAZ
// (une ligne) et attend une ligne contenant la valeur (2 s maximum).
//
// À renseigner avant de flasher : WIFI_SSID et WIFI_PASS (ne pas pousser le mot de passe sur git).
// Au démarrage, l'adresse IP de l'ESP s'affiche quelques secondes sur l'écran.
// =====================================================================================
#include <ESP8266WiFi.h>
#include <uMQTTBroker.h>
#include <LiquidCrystal.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

// ---------------------------------------------------------
// Configuration
// ---------------------------------------------------------
namespace Config {
    constexpr char WIFI_SSID[] = "VOTRE_SSID";
    constexpr char WIFI_PASS[] = "VOTRE_MOT_DE_PASSE";

    constexpr uint8_t PIN_LCD_RS = D1;
    constexpr uint8_t PIN_LCD_EN = D2;
    constexpr uint8_t PIN_LCD_D4 = D5;
    constexpr uint8_t PIN_LCD_D5 = D6;
    constexpr uint8_t PIN_LCD_D6 = D7;
    constexpr uint8_t PIN_LCD_D7 = D8;

    constexpr uint8_t PIN_SPEAKER = D0;

    constexpr uint32_t SERIAL_BAUD = 9600;
    constexpr uint32_t SERIAL_TIMEOUT_MS = 2000;
    constexpr uint32_t DIST_CHECK_INTERVAL_MS = 2000;
    constexpr float DIST_THRESHOLD_CM = 50.0f;
    constexpr uint32_t IP_DISPLAY_MS = 4000;
}

// ---------------------------------------------------------
// Utilitaires
// ---------------------------------------------------------
namespace Utils {
    // Extrait la valeur de "str" dans {"str":"..."} (le JSON doit être compact : "str":"...")
    void extract_json_str(const char* json, char* output, size_t max_len) {
        const char* start;
        const char* end;
        size_t len;

        output[0] = '\0';
        start = strstr(json, "\"str\":\"");
        if (!start) {
            return;
        }
        start += 7;
        end = strchr(start, '"');
        if (!end) {
            return;
        }
        len = end - start;
        if (len >= max_len) {
            len = max_len - 1;
        }
        memcpy(output, start, len);
        output[len] = '\0';
    }

    void to_uppercase(char* str) {
        while (*str) {
            if (*str >= 'a' && *str <= 'z') {
                *str -= 32;
            }
            str++;
        }
    }

    // Seuls ces capteurs sont acceptés : un nom inconnu bloquerait 2 s l'attente série pour rien.
    bool is_known_sensor(const char* name) {
        return strcmp(name, "temp") == 0 || strcmp(name, "dist") == 0 ||
               strcmp(name, "hum") == 0 || strcmp(name, "gaz") == 0;
    }
}

// ---------------------------------------------------------
// Périphériques : écran LCD 16x2 et haut-parleur
// ---------------------------------------------------------
namespace Hardware {
    struct Note {
        uint16_t freq;
        uint32_t dur;
    };

    static LiquidCrystal lcd(Config::PIN_LCD_RS, Config::PIN_LCD_EN,
                             Config::PIN_LCD_D4, Config::PIN_LCD_D5,
                             Config::PIN_LCD_D6, Config::PIN_LCD_D7);

    void init() {
        pinMode(Config::PIN_SPEAKER, OUTPUT);
        digitalWrite(Config::PIN_SPEAKER, LOW);
        lcd.begin(16, 2);
        lcd.clear();
    }

    void reset_speaker() {
        noTone(Config::PIN_SPEAKER);
        digitalWrite(Config::PIN_SPEAKER, LOW);
    }

    void play_sequence(const Note* sequence, size_t length) {
        const Note* current = sequence;
        const Note* end = sequence + length;

        while (current < end) {
            if (current->freq > 0) {
                tone(Config::PIN_SPEAKER, current->freq, current->dur);
            } else {
                noTone(Config::PIN_SPEAKER);
            }
            delay(current->dur);
            current++;
        }
        reset_speaker();
    }

    void play_short_sound() {
        static const Note seq[] = {{1000, 100}};
        play_sequence(seq, 1);
    }

    void play_granted_sound() {
        static const Note seq[] = {{880, 150}, {1046, 200}};
        play_sequence(seq, 2);
    }

    void play_denied_sound() {
        static const Note seq[] = {{440, 150}, {349, 200}};
        play_sequence(seq, 2);
    }

    // Les 16 premiers caractères vont sur la ligne 1, les 16 suivants sur la ligne 2.
    void display_text(const char* text) {
        char line[17];
        size_t len;

        lcd.clear();
        if (!text || *text == '\0') {
            return;
        }
        len = strlen(text);
        snprintf(line, sizeof(line), "%.16s", text);
        lcd.setCursor(0, 0);
        lcd.print(line);
        if (len > 16) {
            snprintf(line, sizeof(line), "%.16s", text + 16);
            lcd.setCursor(0, 1);
            lcd.print(line);
        }
    }
}

// ---------------------------------------------------------
// Liaison série (UART) vers la carte capteurs
// ---------------------------------------------------------
namespace SerialComm {
    void flush_serial() {
        while (Serial.available() > 0) {
            Serial.read();
        }
    }

    bool wait_for_serial(char* buffer, size_t max_len) {
        uint32_t start_time = millis();
        size_t index = 0;
        char c;

        buffer[0] = '\0';
        while ((millis() - start_time) < Config::SERIAL_TIMEOUT_MS) {
            while (Serial.available() > 0) {
                c = Serial.read();
                if (c == '\n' || c == '\r') {
                    if (index > 0) {
                        buffer[index] = '\0';
                        return true;
                    }
                    continue;
                }
                if (index < max_len - 1) {
                    buffer[index++] = c;
                }
            }
            yield();   // sans danger : on est dans loop(), jamais dans un callback réseau
        }
        return false;
    }

    bool request_data(const char* command, char* output, size_t max_len) {
        flush_serial();
        Serial.println(command);
        return wait_for_serial(output, max_len);
    }
}

// ---------------------------------------------------------
// MQTT : prototype de publication
// ---------------------------------------------------------
namespace MqttApp {
    void publish(const char* topic, const char* payload);
}

// ---------------------------------------------------------
// File d'attente des travaux (remplie par le réseau, vidée par loop())
// ---------------------------------------------------------
namespace Jobs {
    enum Kind : uint8_t { SENSOR, SCREEN, GRANTED, DENIED };

    struct Job {
        Kind kind;
        char arg[40];     // nom du capteur ou texte de l'écran
    };

    constexpr uint8_t QUEUE_SIZE = 8;
    static Job queue[QUEUE_SIZE];
    static uint8_t head = 0;
    static uint8_t tail = 0;

    bool push(Kind kind, const char* arg) {
        uint8_t next = (head + 1) % QUEUE_SIZE;

        if (next == tail) {
            return false;                       // file pleine : on ignore (jamais bloquant)
        }
        queue[head].kind = kind;
        snprintf(queue[head].arg, sizeof(queue[head].arg), "%s", arg ? arg : "");
        head = next;
        return true;
    }

    bool pop(Job& out) {
        if (tail == head) {
            return false;
        }
        out = queue[tail];
        tail = (tail + 1) % QUEUE_SIZE;
        return true;
    }

    bool pending() {
        return tail != head;
    }
}

// ---------------------------------------------------------
// Exécution des travaux (dans loop() uniquement)
// ---------------------------------------------------------
namespace Routing {
    void run_sensor(const char* sensor) {
        char response_topic[32];
        char serial_cmd[16];
        char raw_value[32];
        char json_response[64];

        snprintf(serial_cmd, sizeof(serial_cmd), "%s", sensor);
        Utils::to_uppercase(serial_cmd);
        snprintf(response_topic, sizeof(response_topic), "esp/response/%s", sensor);
        if (SerialComm::request_data(serial_cmd, raw_value, sizeof(raw_value))) {
            snprintf(json_response, sizeof(json_response), "{\"result\":\"%s\"}", raw_value);
            MqttApp::publish(response_topic, json_response);
        }
    }

    void run_job(const Jobs::Job& job) {
        switch (job.kind) {
            case Jobs::SENSOR:
                run_sensor(job.arg);
                break;
            case Jobs::SCREEN:
                if (job.arg[0] != '\0') {
                    Hardware::play_short_sound();
                }
                Hardware::display_text(job.arg);
                break;
            case Jobs::GRANTED:
                Hardware::play_granted_sound();
                break;
            case Jobs::DENIED:
                Hardware::play_denied_sound();
                break;
        }
    }

    void run_pending() {
        Jobs::Job job;

        while (Jobs::pop(job)) {
            run_job(job);
            yield();
        }
    }

    // Appelé par le réseau : ne fait que ranger le travail dans la file (rapide, sans attente).
    void handle_message(const char* topic, const char* payload) {
        char text[33];

        if (strncmp(topic, "esp/requetes/", 13) == 0) {
            const char* sensor = topic + 13;
            if (Utils::is_known_sensor(sensor)) {
                Jobs::push(Jobs::SENSOR, sensor);
            }
            return;
        }
        if (strncmp(topic, "esp/function/", 13) == 0) {
            const char* func = topic + 13;
            if (strcmp(func, "screen") == 0) {
                Utils::extract_json_str(payload, text, sizeof(text));
                Jobs::push(Jobs::SCREEN, text);
            } else if (strcmp(func, "granted") == 0) {
                Jobs::push(Jobs::GRANTED, "");
            } else if (strcmp(func, "denied") == 0) {
                Jobs::push(Jobs::DENIED, "");
            }
            return;
        }
    }
}

// ---------------------------------------------------------
// Mini-broker MQTT interne
// ---------------------------------------------------------
class LocalBroker : public uMQTTBroker {
public:
    virtual bool onConnect(IPAddress addr, uint16_t client_count) {
        return true;
    }

    virtual void onData(String topic, const char *data, uint32_t length) {
        char payload[64];
        size_t cpy_len;

        cpy_len = length < 63 ? length : 63;
        memcpy(payload, data, cpy_len);
        payload[cpy_len] = '\0';
        Routing::handle_message(topic.c_str(), payload);
    }
};

namespace MqttApp {
    static LocalBroker broker;

    void init() {
        broker.init();
        broker.subscribe("esp/requetes/#");
        broker.subscribe("esp/function/#");
    }

    void publish(const char* topic, const char* payload) {
        broker.publish(topic, (uint8_t*)payload, strlen(payload), 0, false);
    }
}

// ---------------------------------------------------------
// Tâche de fond : présence (publiée seule quand la distance est sous le seuil)
// ---------------------------------------------------------
namespace Background {
    static uint32_t last_check = 0;

    void check_presence() {
        uint32_t current_time = millis();
        char raw_value[32];
        char json_response[64];
        float dist;

        if ((current_time - last_check) < Config::DIST_CHECK_INTERVAL_MS) {
            return;
        }
        last_check = current_time;
        if (SerialComm::request_data("DIST", raw_value, sizeof(raw_value))) {
            dist = atof(raw_value);
            if (dist > 0.0f && dist < Config::DIST_THRESHOLD_CM) {
                snprintf(json_response, sizeof(json_response), "{\"result\":\"%s\"}", raw_value);
                MqttApp::publish("esp/presence", json_response);
            }
        }
    }
}

// ---------------------------------------------------------
// Réseau, setup et loop
// ---------------------------------------------------------
namespace Network {
    void init_wifi() {
        WiFi.mode(WIFI_STA);
        WiFi.begin(Config::WIFI_SSID, Config::WIFI_PASS);
        while (WiFi.status() != WL_CONNECTED) {
            delay(100);
        }
    }
}

void setup() {
    Serial.begin(Config::SERIAL_BAUD);
    Hardware::init();
    Hardware::display_text("Connexion Wi-Fi");
    Network::init_wifi();
    MqttApp::init();
    Hardware::display_text(WiFi.localIP().toString().c_str());   // l'IP à saisir côté PC (--esp)
    delay(Config::IP_DISPLAY_MS);
    Hardware::display_text("");
}

void loop() {
    Routing::run_pending();          // 1) d'abord les demandes du PC
    if (!Jobs::pending()) {
        Background::check_presence();  // 2) sinon la détection de présence
    }
    yield();
}
