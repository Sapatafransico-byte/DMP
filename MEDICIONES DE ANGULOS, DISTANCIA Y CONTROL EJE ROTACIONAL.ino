#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include "addons/TokenHelper.h"
#include "addons/RTDBHelper.h"

// --- CREDENCIALES WIFI Y FIREBASE ---
#define WIFI_SSID "Totalplay-5BB1"
#define WIFI_PASSWORD "5BB13C41hajvn9Au"
#define API_KEY "AIzaSyDTEJ3mfhPKsC2gsp4VNutoCcZ-4naQNTs"
#define DATABASE_URL "estacionamientouanl-default-rtdb.firebaseio.com"

FirebaseData fbdo;
FirebaseAuth auth;
FirebaseConfig config;
bool signupOK = false;

// --- PINES DE SENSORES Y SERVOMOTOR ---
const int trigPin  = 5;
const int echoPin  = 18;
const int servoPin = 13;

// Pines I2C para el sensor GY-9250 (MPU-9250)
const int sdaPin = 21;
const int sclPin = 22;
const uint8_t MPU_ADDR = 0x68; // Dirección I2C estándar del GY-9250

// --- VARIABLES DEL GIROSCOPIO / ACELERÓMETRO (GY-9250) ---
float anguloActual = 0.0;
float offsetAngulo = 0.0;
unsigned long lastImuTime = 0;
bool imuConectado = false;

// --- VARIABLES DEL SERVO ---
int posServo = 152;       // Origen recto
int direccionServo = 1;   // 1 para subir, -1 para bajar
unsigned long lastSweep = 0;

// --- VARIABLES PARA COMPARACIÓN DEL ULTRASÓNICO ---
float distanciaOrigenMeta = 6.0;
const float MARGEN_FRENO_CM = 1.2;

void moverServo(int angulo) {
  int duty = map(angulo, 0, 180, 1638, 8192);
  ledcWrite(servoPin, duty);
}

// --- FUNCIONES NATIVAS PARA EL SENSOR GY-9250 ---
void iniciarGY9250() {
  Wire.begin(sdaPin, sclPin);
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x6B); // Registro PWR_MGMT_1
  Wire.write(0x00); // Despertar al GY-9250
  if (Wire.endTransmission(true) == 0) {
    imuConectado = true;
    Serial.println("[OK] Sensor GY-9250 detectado por I2C.");
  } else {
    Serial.println("[AVISO] No se detecto el GY-9250 en 0x68. Revisa cables SDA(21) y SCL(22).");
  }
}

float leerAnguloGY9250() {
  if (!imuConectado) return 0.0;

  Wire.beginTransmission(MPU_ADDR);
  Wire.write(0x3B); // Registro inicial ACCEL_XOUT_H
  if (Wire.endTransmission(false) != 0) return anguloActual;

  Wire.requestFrom(MPU_ADDR, (uint8_t)14, (uint8_t)true);
  if (Wire.available() < 14) return anguloActual;

  int16_t ax = (Wire.read() << 8) | Wire.read();
  int16_t ay = (Wire.read() << 8) | Wire.read();
  int16_t az = (Wire.read() << 8) | Wire.read();
  Wire.read(); Wire.read(); // Saltar temperatura
  int16_t gx = (Wire.read() << 8) | Wire.read();
  int16_t gy = (Wire.read() << 8) | Wire.read();
  int16_t gz = (Wire.read() << 8) | Wire.read();

  // Convertir acelerómetro a ángulo de inclinación en grados (Pitch)
  float ax_g = ax / 16384.0;
  float ay_g = ay / 16384.0;
  float az_g = az / 16384.0;
  float anguloAccel = atan2(ay_g, sqrt(ax_g * ax_g + az_g * az_g)) * 180.0 / PI;

  // Velocidad angular del giroscopio en grados/segundo
  float gyroRateX = gx / 131.0;

  unsigned long ahora = millis();
  float dt = (ahora - lastImuTime) / 1000.0;
  if (dt <= 0 || dt > 1.0) dt = 0.02;
  lastImuTime = ahora;

  // Filtro complementario (94% Giroscopio + 6% Acelerómetro) para lectura estable sin ruido
  anguloActual = 0.94 * (anguloActual + gyroRateX * dt) + 0.06 * anguloAccel;
  return anguloActual - offsetAngulo;
}

float medirDistancia() {
  digitalWrite(trigPin, LOW);
  delayMicroseconds(2);
  digitalWrite(trigPin, HIGH);
  delayMicroseconds(10);
  digitalWrite(trigPin, LOW);
  long duracion = pulseIn(echoPin, HIGH, 15000);
  if (duracion == 0) return -1.0;
  return duracion * 0.0343 / 2.0;
}

// Medición rápida (3 muestras rápidas) para no perder tiempo durante el regreso
float medirRapido() {
  float suma = 0;
  int n = 0;
  for (int i = 0; i < 3; i++) {
    float d = medirDistancia();
    if (d > 0.5 && d < 200.0) {
      suma += d;
      n++;
    }
    delay(8);
  }
  return n ? (suma / n) : -1.0;
}

void setup() {
  Serial.begin(115200);

  pinMode(trigPin, OUTPUT);
  pinMode(echoPin, INPUT);

  ledcAttach(servoPin, 50, 16);
  moverServo(152); // Posición inicial (Origen)

  // Inicializar GY-9250
  iniciarGY9250();
  lastImuTime = millis();

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Conectando a Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    Serial.print(".");
    delay(500);
  }
  Serial.println("\n¡Conectado a Wi-Fi!");

  config.api_key = API_KEY;
  config.database_url = DATABASE_URL;
  if (Firebase.signUp(&config, &auth, "", "")) {
    signupOK = true;
  }
  Firebase.begin(&config, &auth);
  Firebase.reconnectWiFi(true);

  // Subir la distancia inicial apenas arranca para que el ESP32 #1 tenga la referencia
  float dInit = medirRapido();
  if (dInit > 0) {
    distanciaOrigenMeta = dInit;
    Firebase.RTDB.setFloat(&fbdo, "Sensores/DistanciaActual", dInit);
    Firebase.RTDB.setFloat(&fbdo, "Control/distancia_origen", dInit);
  }
}

unsigned long prevMillis = 0;

void loop() {
  // Actualizar filtro del giroscopio continuamente en cada ciclo
  float anguloGrados = leerAnguloGY9250();

  if (Firebase.ready() && signupOK) {

    // --- 1. LECTURA DE ESTADOS DE FIREBASE ---
    String estadoSistema = "";
    int leido = 0;

    if (Firebase.RTDB.getString(&fbdo, "Control/estado_sistema")) {
      estadoSistema = fbdo.stringData();
    }
    if (Firebase.RTDB.getInt(&fbdo, "Control/leido")) {
      leido = fbdo.intData();
    }

    // --- 2. MODO ALTA VELOCIDAD CUANDO EL MOTOR ESTÁ REGRESANDO ---
    if (estadoSistema == "REGRESANDO") {
      posServo = 145;
      moverServo(145);

      float dRegreso = medirRapido();
      if (dRegreso > 0) {
        Firebase.RTDB.setFloat(&fbdo, "Sensores/DistanciaActual", dRegreso);
        Serial.print("[VIGILANDO REGRESO] Distancia actual: ");
        Serial.print(dRegreso);
        Serial.print(" cm | Meta origen: ");
        Serial.println(distanciaOrigenMeta);

        if (dRegreso <= (distanciaOrigenMeta + MARGEN_FRENO_CM)) {
          Serial.println("--> [ALTO] Origen detectado por ultrasonico. Enviando freno_origen = 1");
          Firebase.RTDB.setInt(&fbdo, "Control/freno_origen", 1);
        }
      }
      return;
    }

    // --- 3. MÁQUINA DE ESTADOS DEL SERVO ---
    if (estadoSistema == "BUSCANDO") {
      if (millis() - lastSweep > 30) {
        lastSweep = millis();
        posServo += direccionServo;

        if (posServo >= 170) direccionServo = -3;
        if (posServo <= 145) direccionServo = 3;

        moverServo(posServo);
      }
    }
    else if (estadoSistema == "ANALIZANDO") {
      // Se queda congelado en el ángulo actual para que la IA tome la foto
    }

    if (leido == 1 || estadoSistema == "MOVIENDO" || estadoSistema == "FIN_CICLO") {
      posServo = 145;
      moverServo(145);
    }

    // --- 4. SUBIDA NORMAL DE SENSORES (Ultrasónico + Giroscopio GY-9250 cada 1000 ms) ---
    if (millis() - prevMillis > 1000) {
      prevMillis = millis();
      float dActual = medirRapido();
      if (dActual > 0) {
        Firebase.RTDB.setFloat(&fbdo, "Sensores/DistanciaActual", dActual);
      }

      // Subir el ángulo medido por el GY-9250 a la página web
      Firebase.RTDB.setFloat(&fbdo, "Sensores/AnguloGiroscopio", anguloGrados);
      Firebase.RTDB.setString(&fbdo, "Sensores/InclinacionEstado",
                              (abs(anguloGrados) < 15.0) ? "Estable" : "Inclinado");

      Serial.print("[SENSORES] Distancia: ");
      Serial.print(dActual);
      Serial.print(" cm | Angulo GY-9250: ");
      Serial.print(anguloGrados, 1);
      Serial.println(" °");

      // Actualizar meta de origen si existe en Firebase
      if (Firebase.RTDB.getFloat(&fbdo, "Control/distancia_origen")) {
        float dOrig = fbdo.floatData();
        if (dOrig > 1.0) distanciaOrigenMeta = dOrig;
      }
    }
  }
}