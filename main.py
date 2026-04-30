"""
Bot de Slack para Decelera — Claude + Supabase
Convierte preguntas en lenguaje natural a SQL y devuelve resultados en Slack.
"""

import json
import logging
import os
import re
from datetime import date

from anthropic import Anthropic
from dotenv import load_dotenv
from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from supabase import create_client

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Variables de entorno
# ---------------------------------------------------------------------------

REQUIRED_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
]
missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
if missing:
    raise RuntimeError(f"Faltan variables de entorno: {', '.join(missing)}")

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid")

# ---------------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------------

claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

app = App(token=os.getenv("SLACK_BOT_TOKEN"), signing_secret=os.getenv("SLACK_SIGNING_SECRET"))
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# ---------------------------------------------------------------------------
# Esquema de base de datos (system prompt para generación de SQL)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_SQL = f"""
Eres un experto en SQL Postgres. Conviertes preguntas en lenguaje natural en consultas SQL
para la base de datos de Decelera, un programa residencial de startups en Menorca.
La zona horaria del programa es {TIMEZONE}.

=== TABLAS (schema public, usar siempre comillas dobles) ===

"Person" — personas del programa
  id (uuid), full_name (text), email (text), bio (text),
  photo_url, linkedin_url, company_name,
  expertise_tags (jsonb, array de strings),
  startup_id (uuid → "Startup".id),
  user_id (uuid → auth),
  arrival_date (timestamptz), departure_date (timestamptz),
  contact_type (text): 'experience_maker' | 'team' | NULL (founders)
  schedule_feedback (jsonb), daily_checkin (jsonb),
  createdat, updatedat

"Startup" — startups participantes
  id (uuid), name (text), tagline, sector, stage,
  logo_url, website_url, createdat, updatedat

"Event" — eventos del programa
  id (uuid), title (text), description, location,
  start_time (timestamptz), end_time (timestamptz),
  type (text), visible_to_contact_types (json array),
  createdat, updatedat

"UserEvent" — asistencia/inscripción a eventos
  id (uuid), user_id (uuid → auth), event_id (uuid → "Event".id), createdat

"OneOnOne" — sesiones 1:1 entre experience maker y founder
  id (uuid), founder_id (uuid → "Person".id), em_id (uuid → "Person".id),
  start_time (timestamp), end_time (timestamp), location, notes,
  active_audio_url, active_audio_status, audio_transcript,
  createdat, updatedat

"OneOnOneAudioSubmission" — grabaciones de audio de 1:1
  id (uuid), one_on_one_id (→ "OneOnOne".id), user_id,
  storage_path, public_url, mime_type, file_size_bytes,
  duration_sec, status, transcript_text, transcribed_at,
  createdat, updatedat

"Notification" — notificaciones individuales
  id, user_id, event_id (nullable), message, is_read, sent_at, pushed_at

"NotificationCampaign" — campañas masivas
  id, title, message, event_id, filters_json (jsonb),
  status (draft|sent|...), scheduled_for, created_by, sent_at,
  target_count, created_notifications_count, pushed_count, error_message, created_at

"NotificationCampaignRecipient" — destinatarios de campaña
  id, campaign_id, user_id, notification_id,
  delivery_status (pending|...), error_message, created_at, updated_at

"PushSubscription" — suscripciones push de dispositivos
  id, user_id, endpoint, p256dh, auth, user_agent, createdat, updatedat

"home_daily_content" — contenido diario de la app
  id, date (date), phase_label, badge_text, title, subtitle,
  body_text, reflection_text, quote_text, quote_author, quote_cohort,
  createdat, updatedat

=== SEMÁNTICA ===

- "founders" o startups → contact_type IS NULL
- "experience makers" / "EMs" → contact_type = 'experience_maker'
- "equipo" / "team" → contact_type = 'team'
- "presente en el programa en fecha X" → arrival_date <= X AND departure_date >= X
- Para unir personas a eventos → siempre via "UserEvent": "Person".user_id = "UserEvent".user_id

=== REGLAS SQL ===

1. Devuelve SOLO la sentencia SQL, sin markdown, sin comentarios, sin punto y coma final.
2. Nunca uses SELECT *. Selecciona solo las columnas necesarias para responder.
3. Para búsquedas de texto libre (nombres, sectores, ubicaciones) usa siempre:
   unaccent(lower(columna)) ILIKE unaccent(lower('%valor%'))
4. expertise_tags es jsonb array: busca con operador ?  (ej: expertise_tags ? 'Marketing')
5. start_time y arrival_date son timestamptz. Para filtrar por día local:
   (start_time AT TIME ZONE '{TIMEZONE}')::date = 'YYYY-MM-DD'
6. Para mostrar hora local: to_char(start_time AT TIME ZONE '{TIMEZONE}', 'HH24:MI')
7. Fechas relativas: hoy = CURRENT_DATE, mañana = CURRENT_DATE + 1, ayer = CURRENT_DATE - 1
8. Pon siempre LIMIT (usa el que se te indique en el contexto del usuario).
9. Si la pregunta no es sobre datos (es un saludo, pregunta general, etc.),
   responde exactamente con: NO_SQL

=== EJEMPLOS ===

Pregunta: "¿Cuántos founders hay hoy en el programa?"
SQL: SELECT COUNT(*) AS total FROM public."Person" WHERE contact_type IS NULL AND arrival_date <= CURRENT_DATE AND departure_date >= CURRENT_DATE

Pregunta: "¿Qué eventos hay mañana?"
SQL: SELECT title, to_char(start_time AT TIME ZONE '{TIMEZONE}', 'HH24:MI') AS hora, location FROM public."Event" WHERE (start_time AT TIME ZONE '{TIMEZONE}')::date = CURRENT_DATE + 1 ORDER BY start_time LIMIT 20

Pregunta: "Hola, ¿cómo estás?"
SQL: NO_SQL
""".strip()

SYSTEM_PROMPT_RESPUESTA = """
Eres el asistente de datos de Decelera. Tu trabajo es convertir resultados de base de datos
en respuestas claras y concisas en español para el equipo en Slack.

Reglas:
- Sé breve y directo. Usa listas solo si hay más de 3 items.
- Nunca inventes datos. Solo usa lo que está en los resultados.
- Si hay 0 resultados, di que no encontraste nada.
- Usa formato Slack (negrita con *texto*, no markdown estándar).
- Máximo 10 items en listas; si hay más, menciona el total y muestra los primeros.
""".strip()

# ---------------------------------------------------------------------------
# Helpers: fechas
# ---------------------------------------------------------------------------

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


def resolver_fecha(texto: str) -> str | None:
    """Extrae una fecha ISO 8601 de texto en español si la hay."""
    t = texto.lower()
    hoy = date.today()

    # "25 de mayo" o "25 mayo"
    m = re.search(
        r"\b(\d{1,2})\s*(?:de\s+)?(enero|febrero|marzo|abril|mayo|junio|julio|agosto"
        r"|septiembre|setiembre|octubre|noviembre|diciembre)\b", t
    )
    if m:
        try:
            return date(hoy.year, MESES_ES[m.group(2)], int(m.group(1))).isoformat()
        except ValueError:
            pass

    # "25/05", "25-05", con año opcional
    m = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b", t)
    if m:
        try:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else hoy.year
            if year < 100:
                year += 2000
            return date(year, month, day).isoformat()
        except ValueError:
            pass

    # "día 25" sin mes → mes actual
    m = re.search(r"\bd[ií]a\s+(\d{1,2})\b", t)
    if m:
        try:
            return date(hoy.year, hoy.month, int(m.group(1))).isoformat()
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Helpers: SQL
# ---------------------------------------------------------------------------

def limpiar_sql(texto: str) -> str:
    """Extrae SQL limpio de la respuesta de Claude."""
    texto = texto.strip()
    m = re.search(r"```(?:sql)?\s*(.*?)\s*```", texto, re.IGNORECASE | re.DOTALL)
    if m:
        texto = m.group(1).strip()
    return texto.rstrip(";").strip()


def es_sql_segura(sql: str) -> bool:
    """Solo permite SELECT y WITH...SELECT. Bloquea escritura y múltiples sentencias."""
    if not sql:
        return False
    if ";" in sql:
        return False
    low = sql.lower().strip()
    if not (low.startswith("select") or low.startswith("with")):
        return False
    bloqueadas = r"\b(insert|update|delete|drop|alter|truncate|create|grant|revoke|execute|copy)\b"
    return not re.search(bloqueadas, low)


def asegurar_limit(sql: str, limit: int = 50) -> str:
    if not re.search(r"\blimit\s+\d+\b", sql, re.IGNORECASE):
        sql = f"{sql} LIMIT {limit}"
    return sql


def quitar_limit(sql: str) -> str:
    return re.sub(r"\s+limit\s+\d+(\s+offset\s+\d+)?\s*$", "", sql, flags=re.IGNORECASE).strip()


def sql_para_count(sql: str) -> str:
    base = quitar_limit(sql)
    return f'SELECT COUNT(*) AS total FROM ({base}) AS _subq_'


def sql_sin_unaccent(sql: str) -> str:
    """Fallback: elimina unaccent() si la extensión no está disponible."""
    sql = re.sub(r"unaccent\s*\(\s*lower\s*\((.*?)\)\s*\)", r"lower(\1)", sql, flags=re.IGNORECASE | re.DOTALL)
    sql = re.sub(r"unaccent\s*\((.*?)\)", r"\1", sql, flags=re.IGNORECASE | re.DOTALL)
    return sql


# ---------------------------------------------------------------------------
# Ejecución en Supabase
# ---------------------------------------------------------------------------

def ejecutar_sql(sql: str) -> tuple[list | None, str | None]:
    """
    Ejecuta SQL via RPC exec_sql.
    Devuelve (filas, error). La función devuelve jsonb, que puede ser
    una lista de objetos o un objeto con clave 'error'.
    """
    try:
        res = supabase.rpc("exec_sql", {"query_text": sql}).execute()
        data = res.data

        # exec_sql devuelve jsonb — puede venir como string o ya parseado
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None, f"Respuesta inesperada de exec_sql: {data[:200]}"

        # Si es un dict con clave 'error', hubo error en Postgres
        if isinstance(data, dict) and "error" in data:
            return None, data["error"]

        # Éxito: debería ser una lista
        if isinstance(data, list):
            return data, None

        # Fallback: envolver en lista si vino como dict sin error
        return [data] if data else [], None

    except Exception as e:
        return None, str(e)


def ejecutar_sql_con_fallback(sql: str) -> tuple[list | None, str | None]:
    """Intenta con unaccent; si falla por esa causa, reintenta sin ella."""
    filas, error = ejecutar_sql(sql)
    if error and "unaccent" in error.lower():
        log.warning("unaccent falló, reintentando sin ella")
        sql_fb = sql_sin_unaccent(sql)
        log.info(f"SQL FALLBACK:\n{sql_fb}")
        filas, error = ejecutar_sql(sql_fb)
    return filas, error


# ---------------------------------------------------------------------------
# Flujo principal: pregunta → SQL → resultado → respuesta
# ---------------------------------------------------------------------------

def flujo(pregunta: str) -> str:
    fecha_iso = resolver_fecha(pregunta)
    contexto_fecha = f"\nFecha detectada en la pregunta: {fecha_iso} (úsala literalmente)." if fecha_iso else ""
    limit = 50 if any(w in pregunta.lower() for w in ["todos", "todas", "completo", "lista"]) else 20

    # PASO 1 — Generar SQL
    log.info(f"Pregunta: {pregunta}")
    try:
        res_sql = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT_SQL,
            messages=[{
                "role": "user",
                "content": f"Pregunta: {pregunta}{contexto_fecha}\nLIMIT a usar: {limit}"
            }]
        )
    except Exception as e:
        log.error(f"Error llamando a Claude (SQL): {e}")
        return "No pude conectar con Claude para generar la consulta. Inténtalo de nuevo."

    sql_raw = res_sql.content[0].text.strip() if res_sql.content else ""
    log.info(f"Claude devolvió:\n{sql_raw}")

    # Pregunta no relacionada con datos
    if sql_raw.upper().startswith("NO_SQL"):
        return (
            "Hola! Soy el bot de datos de Decelera. "
            "Puedes preguntarme cosas como: _¿Cuántos founders hay hoy?_, "
            "_¿Qué eventos hay mañana?_ o _¿Quiénes son los experience makers esta semana?_"
        )

    sql = limpiar_sql(sql_raw)
    sql = asegurar_limit(sql, limit)
    log.info(f"SQL generado:\n{sql}")

    if not es_sql_segura(sql):
        log.warning(f"SQL bloqueada por seguridad: {sql}")
        return "Solo puedo ejecutar consultas de lectura. Reformula la pregunta."

    # PASO 2 — Ejecutar en Supabase
    filas, error = ejecutar_sql_con_fallback(sql)

    if error:
        log.error(f"Error Supabase: {error}")
        # Reintento con fecha explícita si la teníamos
        if fecha_iso:
            log.info("Reintentando SQL con fecha literal...")
            try:
                res_retry = claude.messages.create(
                    model=CLAUDE_MODEL,
                    max_tokens=400,
                    system=SYSTEM_PROMPT_SQL,
                    messages=[{
                        "role": "user",
                        "content": (
                            f"Pregunta: {pregunta}\n"
                            f"Usa obligatoriamente la fecha literal DATE '{fecha_iso}'.\n"
                            f"Error anterior: {error}\n"
                            f"LIMIT a usar: {limit}"
                        )
                    }]
                )
                sql_retry = limpiar_sql(res_retry.content[0].text.strip())
                sql_retry = asegurar_limit(sql_retry, limit)
                log.info(f"SQL RETRY:\n{sql_retry}")
                if es_sql_segura(sql_retry):
                    filas, error = ejecutar_sql_con_fallback(sql_retry)
                    if error:
                        return f"Error consultando la base de datos: {error}"
            except Exception as e:
                return f"Error en el reintento: {e}"
        else:
            return f"Error consultando la base de datos: {error}"

    if filas is None:
        return "No obtuve respuesta de la base de datos."

    if len(filas) == 0:
        return "No encontré resultados para esa consulta."

    # Contar total real si puede haber truncado
    total_global = None
    if len(filas) >= limit:
        sql_count = sql_para_count(sql)
        filas_count, _ = ejecutar_sql_con_fallback(sql_count)
        if filas_count and isinstance(filas_count, list) and filas_count:
            total_global = filas_count[0].get("total")

    # PASO 3 — Generar respuesta en lenguaje natural
    resumen = json.dumps(filas[:10], ensure_ascii=False, default=str)
    nota_truncado = (
        f"\n(Mostrando {len(filas)} de {total_global} resultados totales.)"
        if total_global and total_global > len(filas)
        else f"\n(Total: {len(filas)} resultados.)"
    )

    try:
        res_final = claude.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=SYSTEM_PROMPT_RESPUESTA,
            messages=[{
                "role": "user",
                "content": (
                    f"Pregunta original: {pregunta}\n"
                    f"Resultados (JSON): {resumen}"
                    f"{nota_truncado}"
                )
            }]
        )
        return res_final.content[0].text.strip() if res_final.content else "Sin respuesta."
    except Exception as e:
        log.error(f"Error generando respuesta final: {e}")
        # Fallback determinista si Claude falla en el paso final
        lineas = [f"Encontré *{len(filas)}* resultado(s):"]
        for i, fila in enumerate(filas[:10], 1):
            if isinstance(fila, dict):
                partes = [f"{k}: {v}" for k, v in list(fila.items())[:4]]
                lineas.append(f"{i}. " + " | ".join(partes))
        if total_global and total_global > 10:
            lineas.append(f"_(y {total_global - 10} más...)_")
        return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Helpers: Slack
# ---------------------------------------------------------------------------

def extraer_texto(event: dict, limpiar_menciones: bool = False) -> str:
    texto = (event.get("text") or "").strip()
    if limpiar_menciones:
        texto = re.sub(r"<@[A-Z0-9]+>", "", texto).strip()
    return texto


def dividir_mensaje(texto: str, max_chars: int = 3000) -> list[str]:
    if len(texto) <= max_chars:
        return [texto]
    partes, resto = [], texto
    while len(resto) > max_chars:
        corte = resto.rfind("\n", 0, max_chars)
        if corte < 0:
            corte = max_chars
        partes.append(resto[:corte].strip())
        resto = resto[corte:].strip()
    if resto:
        partes.append(resto)
    return partes


def procesar_evento(event: dict, say, limpiar_menciones: bool = False):
    texto = extraer_texto(event, limpiar_menciones)
    if not texto:
        say("Escríbeme una pregunta para poder ayudarte.")
        return

    channel_id = event.get("channel")
    event_ts = event.get("ts")

    # Reacción visual mientras procesa
    reaction_added = False
    if channel_id and event_ts:
        try:
            app.client.reactions_add(channel=channel_id, timestamp=event_ts, name="mag")
            reaction_added = True
        except Exception as e:
            log.warning(f"No se pudo añadir reacción: {e}")

    try:
        respuesta = flujo(texto)
    except Exception as e:
        log.error(f"Error inesperado en flujo: {e}")
        respuesta = "Ocurrió un error inesperado. Revisa los logs."
    finally:
        if reaction_added:
            try:
                app.client.reactions_remove(channel=channel_id, timestamp=event_ts, name="mag")
            except Exception:
                pass

    partes = dividir_mensaje(respuesta)
    for i, parte in enumerate(partes):
        prefijo = f"_(Parte {i+1}/{len(partes)})_\n" if len(partes) > 1 else ""
        say(text=f"{prefijo}{parte}")


# ---------------------------------------------------------------------------
# Eventos de Slack
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_mention(event, say):
    procesar_evento(event, say, limpiar_menciones=True)


@app.event("message")
def handle_dm(event, say):
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return
    procesar_evento(event, say, limpiar_menciones=False)


# ---------------------------------------------------------------------------
# Rutas Flask (Railway)
# ---------------------------------------------------------------------------

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "model": CLAUDE_MODEL}, 200


if __name__ == "__main__":
    flask_app.run(port=int(os.environ.get("PORT", 3000)))