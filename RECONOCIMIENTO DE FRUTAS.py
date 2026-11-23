"""
Detector de Madurez de Frutas/Tomates (Sincronizado con ESP32) - Versión Multi-Ciclo Definitiva
"""
import json
import os
import threading
import time 
from collections import Counter, deque
from datetime import datetime

os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"
import cv2
import numpy as np
import requests

# ---------------- CONFIGURACIÓN ----------------
FIREBASE_URL = "https://estacionamientouanl-default-rtdb.firebaseio.com"
FIREBASE_AUTH = ""
CAMARA = 0

TAMANO_RECUADRO = 0.16     # Recuadro pequeño (16%), ajustable con '9' y '0'
MARGEN_FONDO = 14          # Rigor del fondo 'F', ajustable con '+' y '-'

ARCHIVO_CAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibracion_lab.json")
# -----------------------------------------------

CLASES = ["VERDE", "INTERMEDIO", "MADURA"]
COLORES = {
    "VERDE": (0, 200, 0),
    "INTERMEDIO": (0, 200, 255),
    "MADURA": (0, 0, 255),
    "SIN_FRUTA": (150, 150, 150),
}


def cargar_cal():
    cal = {c: [] for c in CLASES}
    try:
        with open(ARCHIVO_CAL, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if k in cal:
                    cal[k] = v
    except Exception:
        pass
    return cal


def guardar_cal(cal):
    with open(ARCHIVO_CAL, "w", encoding="utf-8") as f:
        json.dump(cal, f)


def roi_rect(shape, escala):
    h, w = shape[:2]
    lado = int(min(w, h) * escala)
    x = (w - lado) // 2
    y = (h - lado) // 2
    return x, y, lado, lado


def capturar_rango_fondo(cap, escala):
    muestras_lab = []
    for _ in range(25):
        ok, f = cap.read()
        if ok and f is not None:
            x, y, rw, rh = roi_rect(f.shape, escala)
            roi = cv2.GaussianBlur(f[y:y + rh, x:x + rw], (5, 5), 0)
            lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB).astype(np.float32)
            muestras_lab.append(lab.reshape(-1, 3))
        time.sleep(0.02)

    todos_lab = np.vstack(muestras_lab)
    return {
        "a_min": float(np.percentile(todos_lab[:, 1], 1)),
        "a_max": float(np.percentile(todos_lab[:, 1], 99)),
        "b_min": float(np.percentile(todos_lab[:, 2], 1)),
        "b_max": float(np.percentile(todos_lab[:, 2], 99)),
        "a_med": float(np.median(todos_lab[:, 1])),
        "b_med": float(np.median(todos_lab[:, 2])),
    }


def medir_recuadro(img, rango_fondo, margen, escala):
    x, y, rw, rh = roi_rect(img.shape, escala)
    roi = cv2.GaussianBlur(img[y:y + rh, x:x + rw], (5, 5), 0)

    if rango_fondo is None:
        return None, roi, np.zeros((rh, rw), np.uint8), "1. PRESIONA 'F' SIN FRUTA"

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV).astype(np.float32)
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB).astype(np.float32)

    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    L, A, B = lab[..., 0], lab[..., 1], lab[..., 2]

    dentro_a = (A >= (rango_fondo["a_min"] - margen)) & (A <= (rango_fondo["a_max"] + margen))
    dentro_b = (B >= (rango_fondo["b_min"] - margen)) & (B <= (rango_fondo["b_max"] + margen))
    es_color_fondo = dentro_a & dentro_b

    dist_color_fondo = np.sqrt((A - rango_fondo["a_med"]) ** 2 + (B - rango_fondo["b_med"]) ** 2)
    es_fruta = (~es_color_fondo) & (dist_color_fondo > (margen + 2.0))

    tonos_validos = ((H <= 105) | (H >= 145)) & (V >= 28) & (L < 244)
    mask = (es_fruta & tonos_validos).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11)))

    pixeles_activos = np.count_nonzero(mask)
    fraccion = pixeles_activos / float(rw * rh)

    if fraccion < 0.10:
        return None, roi, mask, f"Buscando fruta ({fraccion:.0%})"

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, roi, mask, "Sin contorno"

    c_mayor = max(cnts, key=cv2.contourArea)
    hull = cv2.convexHull(c_mayor)

    mask_limpia = np.zeros_like(mask)
    cv2.drawContours(mask_limpia, [hull], -1, 255, -1)

    L_med, a_med, b_med = np.median(lab[mask_limpia > 0], axis=0)
    vec = np.array([a_med - 128.0, b_med - 128.0, 0.15 * L_med * 100.0 / 255.0], np.float32)

    return (vec, hull, (x, y), fraccion), roi, mask_limpia, f"FRUTA OK ({fraccion:.0%})"


def clasificar(vec, cal):
    tiene_cal = all(len(cal[c]) > 0 for c in CLASES)
    if not tiene_cal:
        val_a = vec[0]
        if val_a < -1.5:
            return "VERDE", 0.95
        elif val_a < 11.0:
            return "INTERMEDIO", 0.90
        else:
            return "MADURA", 0.95

    dist = sorted((float(np.linalg.norm(vec - np.array(m))), c) for c in CLASES for m in cal[c])
    pesos = {c: 0.0 for c in CLASES}
    for d, c in dist[:5]:
        pesos[c] += 1.0 / (d + 1e-3)
    estado = max(pesos, key=pesos.get)
    return estado, pesos[estado] / sum(pesos.values())


def _url(ruta=""):
    return f"{FIREBASE_URL}/{ruta}.json?auth={FIREBASE_AUTH}" if FIREBASE_AUTH else f"{FIREBASE_URL}/{ruta}.json"


def fb_get(ruta=""):
    return requests.get(_url(ruta), timeout=4).json()


def fb_put(ruta, valor):
    requests.put(_url(ruta), json=valor, timeout=4)


def fb_patch(ruta, diccionario):
    requests.patch(_url(ruta), json=diccionario, timeout=4)


def main():
    cap = cv2.VideoCapture(CAMARA, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise SystemExit("No se pudo abrir ninguna cámara.")

    for _ in range(10):
        cap.read()

    cal = cargar_cal()
    rango_fondo = None
    margen_actual = MARGEN_FONDO
    escala_actual = TAMANO_RECUADRO
    historial = deque(maxlen=5)

    estado_fb = {"leido": -1, "estado_sistema": "ESPERANDO"}
    enviando_orden = False
    t_inicio_analisis = 0.0
    frames_detectados = 0
    votos = []
    ultimo_estado_conocido = "INTERMEDIO"

    # --- VARIABLES AGREGADAS SOLO PARA LA PÁGINA WEB ---
    pedir_fondo_web = False
    estacion_idx = 0
    ciclo_actual = ["--", "--", "--", "--"]
    try:
        fb_patch("WebControl", {"fondo_ok": False, "cmd_fondo": False, "cmd_iniciar": False})
        fb_put("CicloActual", {"e1": "--", "e2": "--", "e3": "--", "e4": "--"})
    except Exception:
        pass
    # ---------------------------------------------------

    def iniciar_sistema_buscando():
        nonlocal enviando_orden, frames_detectados, estacion_idx, ciclo_actual
        enviando_orden = False
        frames_detectados = 0
        votos.clear()
        estacion_idx = 0
        ciclo_actual = ["--", "--", "--", "--"]
        print("\n[B] INICIO: Estableciendo estado BUSCANDO en Firebase...")
        fb_put("Control/leido", 0)
        fb_put("Control/estado_sistema", "BUSCANDO")
        try:
            fb_put("CicloActual", {"e1": "--", "e2": "--", "e3": "--", "e4": "--"})
        except Exception:
            pass
        estado_fb["leido"] = 0
        estado_fb["estado_sistema"] = "BUSCANDO"

    def sondear():
        nonlocal enviando_orden, frames_detectados, pedir_fondo_web, escala_actual, margen_actual
        while True:
            try:
                ctrl = fb_get("Control")
                if ctrl:
                    nuevo_est = ctrl.get("estado_sistema", "")
                    nuevo_leido = ctrl.get("leido", -1)

                    # Si el ESP32 avisa que ya llegó a la siguiente estación (BUSCANDO)
                    if nuevo_est == "BUSCANDO":
                        if enviando_orden:
                            # Forzamos leido=0 en Firebase para limpiar el candado del motor anterior
                            try:
                                fb_put("Control/leido", 0)
                            except Exception:
                                pass
                            enviando_orden = False
                            frames_detectados = 0
                            votos.clear()
                            print("\n--> [NUEVA ESTACIÓN] Motor detenido. Listo para el siguiente tomate.")

                    estado_fb["estado_sistema"] = nuevo_est
                    estado_fb["leido"] = nuevo_leido

                # --- LECTURA DE BOTONES Y SLIDERS DE LA PÁGINA WEB ---
                web = fb_get("WebControl")
                if isinstance(web, dict):
                    if "tamano_recuadro" in web:
                        escala_actual = max(0.08, min(0.45, float(web["tamano_recuadro"])))
                    if "margen_fondo" in web:
                        margen_actual = max(2, min(45, int(web["margen_fondo"])))
                    if web.get("cmd_fondo"):
                        pedir_fondo_web = True
                        fb_patch("WebControl", {"cmd_fondo": False})
                    if web.get("cmd_iniciar"):
                        fb_patch("WebControl", {"cmd_iniciar": False})
                        threading.Thread(target=iniciar_sistema_buscando, daemon=True).start()
                # -----------------------------------------------------
            except Exception:
                pass
            time.sleep(0.15)

    threading.Thread(target=sondear, daemon=True).start()

    def disparar_motor(resultado_final):
        nonlocal enviando_orden, estacion_idx, ciclo_actual
        try:
            print(f"\n[ANALISIS] Fruta clasificada como: {resultado_final}")
            print("[MOTOR] Enviando leido = 1 a Firebase para avanzar 60 mm...")
            fb_put("Control/resultado", resultado_final)
            time.sleep(0.05)
            fb_put("Control/leido", 1)
            estado_fb["leido"] = 1

            # --- SUBIR ESTACIÓN ACTUAL E HISTORIAL DE 5 CICLOS A LA WEB ---
            if estacion_idx < 4:
                ciclo_actual[estacion_idx] = resultado_final
            estacion_idx += 1

            fb_put("CicloActual", {
                "e1": ciclo_actual[0],
                "e2": ciclo_actual[1],
                "e3": ciclo_actual[2],
                "e4": ciclo_actual[3],
            })

            if estacion_idx >= 4:
                hist = fb_get("HistorialCiclos")
                lista = hist if isinstance(hist, list) else (list(hist.values()) if isinstance(hist, dict) else [])
                lista.append({
                    "hora": datetime.now().strftime("%H:%M:%S"),
                    "e1": ciclo_actual[0],
                    "e2": ciclo_actual[1],
                    "e3": ciclo_actual[2],
                    "e4": ciclo_actual[3],
                })
                fb_put("HistorialCiclos", lista[-5:])
                estacion_idx = 0
                ciclo_actual = ["--", "--", "--", "--"]
            # --------------------------------------------------------------
        except Exception as e:
            print(f"[ERROR] Firebase: {e}")
            enviando_orden = False

    print("=== IA DETECTOR DE FRUTAS (MULTICICLO BLINDADO + WEB) ===")
    print("1. Sin fruta en el cuadrito, presiona 'F' (o el botón en la Web) para capturar el fondo.")
    print("2. Presiona 'B' (o el botón en la Web) para arrancar oficialmente en modo BUSCANDO.")

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break

            # Si presionaron "Capturar Fondo (F)" desde la página web
            if pedir_fondo_web:
                pedir_fondo_web = False
                print("[WEB] Capturando colores del fondo...")
                rango_fondo = capturar_rango_fondo(cap, escala_actual)
                try:
                    fb_patch("WebControl", {"fondo_ok": True})
                except Exception:
                    pass
                print("[OK] Fondo bloqueado correctamente desde la Web.")

            med, roi_vis, mask_vis, msg_debug = medir_recuadro(frame, rango_fondo, margen_actual, escala_actual)
            if med:
                historial.append(med[0])
                vec = np.median(np.array(historial), axis=0)
                estado, conf = clasificar(vec, cal)
                ultimo_estado_conocido = estado
            else:
                historial.clear()
                vec, estado, conf = None, "SIN_FRUTA", 0.0

            sys_estado = estado_fb["estado_sistema"]
            leido = estado_fb["leido"]
            ahora = time.time()

            # ================= LÓGICA MULTICICLO DE DETENCIÓN =================
            if not enviando_orden and rango_fondo is not None:

                # PASO A: Buscando en la estación actual
                if sys_estado == "BUSCANDO" and leido == 0:
                    if med is not None:
                        frames_detectados += 1
                        if frames_detectados >= 2:
                            print(f"\n--> [DETECTADO] Fruta estable ({estado}). Congelando servo en ANALIZANDO...")
                            fb_put("Control/estado_sistema", "ANALIZANDO")
                            estado_fb["estado_sistema"] = "ANALIZANDO"
                            t_inicio_analisis = ahora
                            votos.clear()
                            frames_detectados = 0
                    else:
                        frames_detectados = 0

                # PASO B: Tomando votos de la fruta actual
                elif sys_estado == "ANALIZANDO" and leido == 0:
                    if med is not None and estado != "SIN_FRUTA":
                        votos.append(estado)

                    if len(votos) >= 4 or (ahora - t_inicio_analisis) >= 1.5:
                        if votos:
                            final = Counter(votos).most_common(1)[0][0]
                        else:
                            final = estado if estado != "SIN_FRUTA" else ultimo_estado_conocido

                        enviando_orden = True
                        votos.clear()
                        threading.Thread(target=disparar_motor, args=(final,), daemon=True).start()
            # =================================================================

            # DIBUJO EN PANTALLA
            x, y, rw, rh = roi_rect(frame.shape, escala_actual)
            color = COLORES.get(estado, (255, 255, 255))
            cv2.rectangle(frame, (x, y), (x + rw, y + rh), color, 2)

            if med:
                cv2.drawContours(frame, [med[1] + np.array(med[2])], -1, color, 2)
                cv2.putText(frame, f"a:{med[0][0]:.1f} | Lleno:{med[3]:.0%}",
                            (x - 20, y + rh + 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (0, 255, 255), 2)
            else:
                cv2.putText(frame, msg_debug, (max(10, x - 50), y + rh + 22),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.52,
                            (0, 0, 255) if rango_fondo is None else (0, 200, 0), 2)

            txt = estado if estado == "SIN_FRUTA" else f"{estado} ({conf:.0%})"
            cv2.putText(frame, txt, (x, max(y - 10, 25)), cv2.FONT_HERSHEY_SIMPLEX, 0.75, color, 2)

            f_txt = "FONDO OK" if rango_fondo is not None else "FALTA 'F'"
            cv2.putText(frame, f"{f_txt} | ESP32:{sys_estado} | leido:{leido} | Cuadro:{int(escala_actual*100)}% | Rigor:{margen_actual}",
                        (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.46,
                        (0, 255, 0) if rango_fondo is not None else (0, 0, 255), 2)
            cv2.putText(frame, "[F] Fondo   [B] Iniciar BUSCANDO   [9/0] Tamano Cuadro   [+/-] Rigor",
                        (10, frame.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (255, 255, 255), 1)

            cv2.imshow("Detector de Fruta", frame)
            vista_mask = cv2.bitwise_and(roi_vis, roi_vis, mask=mask_vis)
            cv2.imshow("Filtro IA", cv2.resize(vista_mask, (220, 220)))

            key = cv2.waitKey(1) & 0xFF
            if key in (ord("b"), ord("B")):
                threading.Thread(target=iniciar_sistema_buscando, daemon=True).start()
            elif key in (ord("f"), ord("F")):
                print("[...] Capturando colores del fondo...")
                rango_fondo = capturar_rango_fondo(cap, escala_actual)
                try:
                    fb_patch("WebControl", {"fondo_ok": True})
                except Exception:
                    pass
                print("[OK] Fondo bloqueado correctamente.")
            elif key == ord("9"):
                escala_actual = max(0.08, escala_actual - 0.02)
            elif key == ord("0"):
                escala_actual = min(0.45, escala_actual + 0.02)
            elif key in (ord("+"), ord("=")):
                margen_actual = min(45, margen_actual + 2)
            elif key in (ord("-"), ord("_")):
                margen_actual = max(2, margen_actual - 2)
            elif key in (ord("1"), ord("2"), ord("3")) and vec is not None:
                clase = CLASES[key - ord("1")]
                cal[clase].append([float(v) for v in vec])
                cal[clase] = cal[clase][-10:]
                guardar_cal(cal)
                print(f"Muestra guardada para {clase}")
            elif key in (ord("r"), ord("R")):
                cal = {c: [] for c in CLASES}
                guardar_cal(cal)
                print("Calibración reiniciada.")
            elif key in (ord("q"), ord("Q"), 27):
                break
    finally:
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()