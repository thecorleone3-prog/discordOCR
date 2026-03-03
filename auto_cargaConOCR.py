# ============================================================
# ⚙️ MÓDULO DE PROCESAMIENTO ASÍNCRONO: auto_cargaConOCR.py
# ============================================================
import os
import re
import json
import httpx  
import io
import pytesseract
import asyncio
import hashlib
from datetime import datetime
from PIL import Image, ImageEnhance
from dotenv import load_dotenv
from openai import AsyncOpenAI 

# Cargamos variables de entorno
load_dotenv()

# Configuración de Tesseract
if os.getenv("RAILWAY_ENVIRONMENT"):
    # En Railway usará el tesseract del sistema
    pass
else:
    TESSERACT_PATH = r'C:\Program Files\Tesseract-OCR\tesseract.exe'
    if os.path.exists(TESSERACT_PATH):
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH

# Cliente de OpenAI Global
client_ai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# --- SISTEMA DE CONTROL DE DUPLICADOS ---
enviados_recientemente = set()
lock_duplicados = asyncio.Lock()

async def registrar_y_verificar_huella(datos):
    titular = str(datos.get('titular') or "").strip().upper()
    monto = str(datos.get('monto') or "0").strip()
    banco = str(datos.get('banco') or "").strip().upper()
    usuario = str(datos.get('usuario') or "").strip().upper()
    id_unico = str(datos.get('coelsa') or datos.get('id_operacion') or "").strip().upper()
    
    minuto_actual = datetime.now().strftime("%Y-%m-%d %H:%M") 
    
    if id_unico and len(id_unico) > 5:
        semilla = f"ID-{id_unico}-USER-{usuario}"
    else:
        semilla = f"DATA-{usuario}-{titular}-{monto}-{banco}-{minuto_actual}"
    
    huella = hashlib.md5(semilla.encode()).hexdigest()
    
    async with lock_duplicados:
        if huella in enviados_recientemente:
            return True
        enviados_recientemente.add(huella)
        asyncio.create_task(limpiar_huella_despues(huella))
        return False

async def limpiar_huella_despues(huella):
    await asyncio.sleep(600) 
    async with lock_duplicados:
        enviados_recientemente.discard(huella)

# ============================================================
# 🧠 LÓGICA DE PROCESAMIENTO
# ============================================================

async def analizar_texto_con_ia(texto_ocr):
    prompt = """
    Analiza este texto de un comprobante bancario y extrae en JSON estricto:
    1. titular: Nombre completo de quien envía.
    2. monto: Valor numérico (usa '.' para decimales).
    3. coelsa: ID de 22 caracteres ALFANUMÉRICOS (mezcla letras y números). ¡IMPORTANTE!: NO confundir con el CVU (que son 22 números). Si el ID encontrado solo tiene números, NO es Coelsa.
    4. id_operacion: ID alternativo si no hay Coelsa.
    5. banco: Nombre de la billetera o banco.
    6. fecha_proceso: EXTRAE LA FECHA Y HORA REAL DEL COMPROBANTE (formato DD/MM/YYYY HH:MM:SS).

    REGLA: Si la fecha dice 'Hoy', usa la fecha actual pero mantén la hora del texto.
    Si no encuentras ninguna fecha en el texto, deja el campo como null.
    """
    try:
        response = await client_ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Eres un extractor de datos bancarios experto. Responde solo JSON."},
                {"role": "user", "content": f"Texto OCR:\n{texto_ocr}\n\n{prompt}"}
            ],
            response_format={"type": "json_object"},
            temperature=0
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"❌ Error IA: {e}")
        return None

def ejecutar_ocr_local(img_bytes):
    """Esta es la versión que no te fallaba (Sin forzar idioma spa)"""
    im = Image.open(img_bytes).convert("L")
    im = ImageEnhance.Contrast(im).enhance(2.5) 
    # Quitamos lang='spa' para evitar el error de carga de archivos
    return pytesseract.image_to_string(im)

async def procesar_comprobante_completo(url_imagen, contenido_mensaje, embeds, config):
    SHEETS_WEBHOOK_URL = config.get("SHEETS_WEBHOOK_URL")
    BACKEND_WEBHOOK = config.get("BACKEND_WEBHOOK")

    # ==========================================================
    # 🔎 Extraer información del mensaje + embeds
    # ==========================================================
    full_content = contenido_mensaje or ""

    for e in embeds or []:
        full_content += f" {getattr(e, 'description', '')} {getattr(e, 'title', '')}"
        for f in getattr(e, "fields", []):
            full_content += f" {f.name} {f.value}"

    # 🔎 Usuario
    m_user = re.search(r"Usuario:\s*(?:\*\*)?([^\*\n\s]+)", full_content, re.IGNORECASE)
    usuario_detectado = m_user.group(1).strip() if m_user else "NO-DETECTADO"

    # 🔎 LeadID (opcional)
    m_lead = re.search(r"LeadID:\s*(\d+)", full_content, re.IGNORECASE)
    lead_id_detectado = m_lead.group(1).strip() if m_lead else None

    # ==========================================================
    # 📡 Función interna para notificar backend
    # ==========================================================
    async def notificar_backend(resultado, monto=None):
        if not BACKEND_WEBHOOK or not lead_id_detectado:
            return

        payload = {
            "lead_id": lead_id_detectado,
            "resultado": resultado,
            "monto": monto
        }

        try:
            async with httpx.AsyncClient() as client:
                await client.post(BACKEND_WEBHOOK, json=payload, timeout=20)
            print(f"📡 Backend notificado → Lead {lead_id_detectado} = {resultado}")
        except Exception as e:
            print(f"❌ Error notificando backend: {e}")

    try:
        # ==========================================================
        # 📥 Descargar imagen
        # ==========================================================
        async with httpx.AsyncClient(follow_redirects=True) as client:
            img_res = await client.get(url_imagen, timeout=20)

            if img_res.status_code != 200:
                await notificar_backend("error_descarga")
                return "error_descarga"

            img_bytes = io.BytesIO(img_res.content)

            # ======================================================
            # 🔍 OCR LOCAL
            # ======================================================
            texto_raw = await asyncio.to_thread(ejecutar_ocr_local, img_bytes)

            # ======================================================
            # 🧠 Análisis IA
            # ======================================================
            datos = await analizar_texto_con_ia(texto_raw)

            if not datos:
                await notificar_backend("error_ocr")
                return "error_ocr"

            # ======================================================
            # 🛠 Preparar datos
            # ======================================================
            datos['usuario'] = usuario_detectado

            if not datos.get('fecha_proceso'):
                datos['fecha_proceso'] = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

            if datos.get('coelsa'):
                datos['coelsa'] = str(datos['coelsa']).strip().upper().replace(" ", "")

            # ======================================================
            # 🚫 Anti-duplicados
            # ======================================================
            if await registrar_y_verificar_huella(datos):
                print(f"🚫 DUPLICADO: {datos.get('usuario')} | ${datos.get('monto')}")
                await notificar_backend("duplicado")
                return "duplicado"

            # ======================================================
            # 📤 Envío a Google Sheets
            # ======================================================
            api_url = f"{SHEETS_WEBHOOK_URL}{'&' if '?' in SHEETS_WEBHOOK_URL else '?'}action=ocr"
            res = await client.post(api_url, json=datos, timeout=120.0)
            raw_res = res.text.strip()

            if res.status_code == 200:
                try:
                    rj = res.json()
                    res_obj = rj.get("resultado", {})

                    if rj.get("ok") is True:
                        if isinstance(res_obj, dict) and res_obj.get("conciliado") is True:
                            await notificar_backend("exito", datos.get("monto"))
                            return "exito"

                        elif "EXITOSO" in str(res_obj).upper():
                            await notificar_backend("exito", datos.get("monto"))
                            return "exito"

                    await notificar_backend("pendiente")
                    return "pendiente"

                except:
                    if "conciliado\":true" in raw_res.lower().replace(" ", ""):
                        await notificar_backend("exito", datos.get("monto"))
                        return "exito"

                    await notificar_backend("pendiente")
                    return "pendiente"

            else:
                await notificar_backend("error_servidor")
                return "error_servidor"

    except Exception as e:
        print(f"💀 Error crítico: {e}")
        await notificar_backend("error_critico")
        return "error_critico"