#include <Arduino.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include "addons/TokenHelper.h"
#include "addons/RTDBHelper.h"

#define WIFI_SSID "Totalplay-5BB1"
#define WIFI_PASSWORD "5BB13C41hajvn9Au"
#define API_KEY "AIzaSyDTEJ3mfhPKsC2gsp4VNutoCcZ-4naQNTs"
#define DATABASE_URL "estacionamientouanl-default-rtdb.firebaseio.com"

FirebaseData fbdo;
FirebaseAuth auth;
FirebaseConfig config;
bool signupOK = false;

// --- PINES DEL DRIVER MOTOR A PASOS ---
const int stepPin = 25;
const int dirPin  = 26;
const int enPin   = 27;

// --- PARÁMETROS ESTRICTOS DE MOVIMIENTO ---
const long PASOS_POR_60MM = 2000;
const unsigned int US_MOTOR = 1500;
const float PASOS_POR_CM = 500.0;

int movimientosRealizados = 0; // Cuenta de 0 a 3 (3 avances de 60mm)
float distInicioCM = -1.0;

void pulso() {
  digitalWrite(stepPin, HIGH);
  delayMicroseconds(US_MOTOR);
  digitalWrite(stepPin, LOW);
  delayMicroseconds(US_MOTOR);
}

float obtenerDistanciaUltrasonico() {
  if (Firebase.RTDB.getFloat(&fbdo, "Sensores/DistanciaActual")) {
    return fbdo.floatData();
  }
  return -1.0;
}

void regresarConCompensacionUltrasonico() {
  digitalWrite(dirPin, LOW); // Dirección de regreso

  float distFinalCM = obtenerDistanciaUltrasonico();
  // Por defecto regresamos los 3 ciclos + 1 ciclo extra (60mm = 3000 pasos) que le faltaba
  long pasosRegreso = (PASOS_POR_60MM * 3) + PASOS_POR_60MM;

  if (distInicioCM > 0 && distFinalCM > 0) {
    float cmRecorridosReales = abs(distFinalCM - distInicioCM);

    Serial.print("[ULTRASONICO] Inicio: ");
    Serial.print(distInicioCM);
    Serial.print(" cm | Final: ");
    Serial.print(distFinalCM);
    Serial.print(" cm | Real: ");
    Serial.print(cmRecorridosReales);
    Serial.println(" cm");

    if (cmRecorridosReales >= 2.0 && cmRecorridosReales <= 25.0) {
      // Calculamos los pasos medidos + los 3000 pasos (60mm) que le faltaban para llegar al inicio
      pasosRegreso = (long)(cmRecorridosReales * PASOS_POR_CM) + PASOS_POR_60MM;
      if (pasosRegreso > 300) {
        pasosRegreso -= 150; // Colchón de seguridad anti-choque
      }
      Serial.print("[COMPENSACION + 60MM] Regresando exactamente ");
      Serial.print(pasosRegreso);
      Serial.println(" pasos reales.");
    } else {
      pasosRegreso = (PASOS_POR_60MM * 3) + PASOS_POR_60MM;
      Serial.println("[AVISO] Rango ultrasonico fuera de limites. Regresando 3 ciclos + 60mm.");
    }
  } else {
    Serial.println("[AVISO] Sin datos de ultrasonico. Regresando 3 ciclos + 60mm.");
  }

  for (long i = 0; i < pasosRegreso; i++) {
    pulso();
  }
  Serial.println("Motor en el punto cero sin chocar.");
}

void setup() {
  Serial.begin(115200);

  pinMode(stepPin, OUTPUT);
  pinMode(dirPin, OUTPUT);
  pinMode(enPin, OUTPUT);
  digitalWrite(enPin, LOW); // Motor siempre energizado/frenado

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Conectando a Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(500);
  }
  Serial.println("\n¡Conectado!");

  config.api_key = API_KEY;
  config.database_url = DATABASE_URL;
  if (Firebase.signUp(&config, &auth, "", "")) {
    signupOK = true;
  }
  Firebase.begin(&config, &auth);
  Firebase.reconnectWiFi(true);
}

void loop() {
  if (Firebase.ready() && signupOK) {

    // --- 1. ESCUCHAR BOTONES DE RETROCESO DESDE LA PÁGINA WEB (1 o 2 CICLOS) ---
    int ciclosRetro = 0;
    if (Firebase.RTDB.getInt(&fbdo, "Control/retroceder_ciclos")) {
      ciclosRetro = fbdo.intData();
    }

    if (ciclosRetro > 0) {
      Firebase.RTDB.setInt(&fbdo, "Control/retroceder_ciclos", 0); // Limpiar orden en Firebase
      Serial.print("[WEB] Retrocediendo ");
      Serial.print(ciclosRetro);
      Serial.println(" ciclo(s) por desatasco...");

      digitalWrite(dirPin, LOW); // Hacia atrás
      long pasosDesatasco = PASOS_POR_60MM * ciclosRetro;
      for (long i = 0; i < pasosDesatasco; i++) {
        pulso();
      }

      movimientosRealizados = max(0, movimientosRealizados - ciclosRetro);
      Firebase.RTDB.setString(&fbdo, "Control/estado_sistema", "BUSCANDO");
      Firebase.RTDB.setInt(&fbdo, "Control/leido", 0);
      Serial.println("[WEB] Retroceso completado. Sistema en BUSCANDO.");
      return;
    }

    // --- 2. LECTURA NORMAL DE AVANCE CONTROLADO POR LA IA ---
    int leido = 0;
    if (Firebase.RTDB.getInt(&fbdo, "Control/leido")) {
      leido = fbdo.intData();
    }

    // SI LA IA DIO LA ORDEN DE AVANZAR (leido == 1)
    if (leido == 1) {
      Firebase.RTDB.setInt(&fbdo, "Control/leido", 2); // Bloqueo inmediato para evitar bucles

      // CASO A: Todavía nos quedan tramos por avanzar (movimientos 0, 1 y 2)
      if (movimientosRealizados < 3) {
        // En la primera parada tomamos la distancia base del ultrasónico
        if (movimientosRealizados == 0) {
          distInicioCM = obtenerDistanciaUltrasonico();
          Serial.print("Distancia inicial ultrasonico: ");
          Serial.print(distInicioCM);
          Serial.println(" cm");
        }

        Serial.print("Avanzando ciclo ");
        Serial.print(movimientosRealizados + 1);
        Serial.println(" (60 mm)...");

        Firebase.RTDB.setString(&fbdo, "Control/estado_sistema", "MOVIENDO");

        digitalWrite(dirPin, HIGH); // Hacia adelante
        for (long i = 0; i < PASOS_POR_60MM; i++) {
          pulso();
        }

        movimientosRealizados++;

        // Avisa a la cámara que ya llegó y debe buscar en la nueva posición
        Firebase.RTDB.setString(&fbdo, "Control/estado_sistema", "BUSCANDO");
        Firebase.RTDB.setInt(&fbdo, "Control/leido", 0); // Libera el leido a 0 para la IA
        Serial.println("Movimiento terminado. Esperando analisis de IA...");
      }
      // CASO B: Ya se completaron los 3 avances y la IA analizó el punto final
      else {
        Serial.println("Analisis final completado. Iniciando regreso a casa.");
        Firebase.RTDB.setString(&fbdo, "Control/estado_sistema", "REGRESANDO");

        delay(1000); // Pausa breve para estabilizar sensor ultrasónico final

        regresarConCompensacionUltrasonico();

        movimientosRealizados = 0;
        Firebase.RTDB.setString(&fbdo, "Control/estado_sistema", "FIN_CICLO");
        Firebase.RTDB.setInt(&fbdo, "Control/leido", -1);
        Serial.println("Ciclo completo finalizado. Sistema en espera.");
      }
    }
  }
}