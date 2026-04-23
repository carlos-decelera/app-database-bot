import os
import re
from datetime import date
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from anthropic import Anthropic
from supabase import create_client

load_dotenv()

# Validación temprana de variables críticas para evitar fallos opacos en runtime.
REQUIRED_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "SUPABASE_URL",
    "SUPABASE_KEY",
    "CLAUDE_MODEL",
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
]
missing_env_vars = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
if missing_env_vars:
    raise RuntimeError(
        "Faltan variables de entorno requeridas: " + ", ".join(missing_env_vars)
    )

# Inicialización de clientes
claude = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
model_claude = os.getenv("CLAUDE_MODEL")

# Configuración de Slack
app = App(
    token=os.getenv("SLACK_BOT_TOKEN"),
    signing_secret=os.getenv("SLACK_SIGNING_SECRET")
)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# --- LÓGICA DEL BOT ---

MESES_ES = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def _normalizar_sql_generada(sql_query):
    cleaned = (sql_query or "").strip()
    if not cleaned:
        return ""

    # Si el modelo devuelve bloque markdown, extrae solo el contenido SQL.
    block_match = re.search(r"```(?:sql)?\s*(.*?)\s*```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if block_match:
        cleaned = block_match.group(1).strip()

    # Acepta ';' final (estilo común) pero elimina sentencias adicionales.
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    return cleaned


def _asegurar_limit(sql_query, limit_default=20):
    cleaned = _normalizar_sql_generada(sql_query)
    if not cleaned:
        return cleaned

    # Si ya tiene LIMIT, no tocar.
    if re.search(r"\blimit\s+\d+\b", cleaned, flags=re.IGNORECASE):
        return cleaned

    return f"{cleaned} LIMIT {limit_default}"


def _limit_por_intencion(pregunta):
    texto = (pregunta or "").lower()
    if any(token in texto for token in ["todos", "todas", "lista completa", "completo"]):
        return 200
    return 50


def _quiere_total_completo(pregunta):
    texto = (pregunta or "").lower()
    return any(token in texto for token in ["todos", "todas", "lista completa", "completo", "cuantos", "cuántos", "total"])


def _sql_para_count_total(sql_query):
    cleaned = _normalizar_sql_generada(sql_query)
    # Quita LIMIT/OFFSET al final para obtener el total real.
    base = re.sub(
        r"\s+limit\s+\d+(\s+offset\s+\d+)?\s*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    base = re.sub(r"\s+offset\s+\d+\s*$", "", base, flags=re.IGNORECASE)
    return f'SELECT COUNT(*) AS total FROM ({base}) AS "__subq__"'


def _es_sql_segura_para_lectura(sql_query):
    cleaned = _normalizar_sql_generada(sql_query)
    if not cleaned:
        return False
    if ";" in cleaned:
        # Evita inyección por múltiples sentencias.
        return False
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False

    blocked_patterns = [
        r"\binsert\b",
        r"\bupdate\b",
        r"\bdelete\b",
        r"\bdrop\b",
        r"\balter\b",
        r"\btruncate\b",
        r"\bcreate\b",
        r"\bgrant\b",
        r"\brevoke\b",
    ]
    return not any(re.search(pattern, lowered) for pattern in blocked_patterns)


def _extraer_texto_mencion(event):
    text = (event.get("text") or "").strip()
    if not text:
        return ""
    # Elimina menciones tipo <@U123ABC>.
    text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
    return text


def _resolver_fecha_explicita(pregunta):
    texto = (pregunta or "").lower()

    # 1) Captura: "25 de mayo" o "25 mayo".
    match_mes = re.search(
        r"\b(\d{1,2})\s*(?:de\s+)?(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\b",
        texto,
    )
    if match_mes:
        day = int(match_mes.group(1))
        month = MESES_ES[match_mes.group(2)]
        year = date.today().year
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    # 2) Captura: "25/05", "25-05", "25.05" con año opcional.
    match_num = re.search(r"\b(\d{1,2})[\/\-.](\d{1,2})(?:[\/\-.](\d{2,4}))?\b", texto)
    if match_num:
        day = int(match_num.group(1))
        month = int(match_num.group(2))
        year_txt = match_num.group(3)
        if year_txt:
            year = int(year_txt)
            if year < 100:
                year += 2000
        else:
            year = date.today().year
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None

    # 3) Captura: "dia 25" o "día 25" sin mes -> asume mes actual.
    match_dia = re.search(r"\bd[ií]a\s+(\d{1,2})\b", texto)
    if match_dia:
        day = int(match_dia.group(1))
        today = date.today()
        try:
            return date(today.year, today.month, day).isoformat()
        except ValueError:
            return None

    return None


def _procesar_evento_pregunta(event, say, limpiar_menciones=False):
    texto = (event.get("text") or "").strip()
    if limpiar_menciones:
        texto = _extraer_texto_mencion(event)

    event_ts = event.get("ts")
    channel_id = event.get("channel")

    if not texto:
        say("Escríbeme una pregunta para poder ayudarte.")
        return

    # Indicador visual sin mensaje: reacción sobre el mensaje del usuario.
    reaction_added = False
    if channel_id and event_ts:
        try:
            app.client.reactions_add(
                channel=channel_id,
                timestamp=event_ts,
                name="mag",
            )
            reaction_added = True
        except Exception as reaction_error:
            print(f"No se pudo agregar reacción de progreso: {reaction_error}")

    try:
        respuesta = flujo_pregunta_respuesta(texto)
    finally:
        if reaction_added and channel_id and event_ts:
            try:
                app.client.reactions_remove(
                    channel=channel_id,
                    timestamp=event_ts,
                    name="mag",
                )
            except Exception as reaction_error:
                print(f"No se pudo quitar reacción de progreso: {reaction_error}")

    # Respondemos en la conversación principal (sin hilo).
    say(text=respuesta)


def flujo_pregunta_respuesta(pregunta):
    """
    Proceso: Lenguaje Natural -> SQL -> Supabase -> Respuesta Humana
    """
    
    # Prompt compacto para reducir coste, manteniendo reglas críticas.
    esquema_detallado = """
    Convierte lenguaje natural en SQL Postgres para un programa en Menorca.
    Tablas:
    - public."Person"(id, full_name, email, contact_type, expertise_tags, startup_id, arrival_date, departure_date)
    - public."Startup"(id, name, sector, stage)
    - public."Event"(id, title, description, location, start_time, speaker_id)
    - public."UserEvent"(user_id, event_id)
    Joins:
    - Person.startup_id = Startup.id
    - UserEvent.user_id = Person.id
    - UserEvent.event_id = Event.id
    - Event.speaker_id = Person.id
    Semantica:
    - "quien esta", "quienes estan", "en Menorca", "en el programa" => personas presentes por rango de fechas.
    - Presente en fecha X => arrival_date <= X AND (departure_date IS NULL OR departure_date >= X).
    - "a que hora", "horario", "eventos a las 18:00" => consultas sobre public."Event".start_time.
    Reglas de fechas:
    - "hoy" => CURRENT_DATE.
    - "manana" => CURRENT_DATE + INTERVAL '1 day'.
    - "ayer" => CURRENT_DATE - INTERVAL '1 day'.
    - "dia 24" sin mes/anio => dia 24 del mes actual:
      (date_trunc('month', CURRENT_DATE)::date + INTERVAL '23 day')::date
    - Si hay mes explicito (ej: "24 de mayo"), usa esa fecha del anio actual.
    Reglas SQL:
    - Usa SIEMPRE nombres con comillas dobles.
    - "Experience Makers" => contact_type = 'experience_maker'.
    - Para textos usa ILIKE; para expertise_tags usa operador ?.
    - start_time es timestamptz. Para filtrar por dia local, usa (start_time AT TIME ZONE 'Europe/Madrid')::date.
    - Para mostrar hora local, usa to_char(start_time AT TIME ZONE 'Europe/Madrid', 'HH24:MI') AS hora_local.
    - Si preguntan "a las HH:MM", compara to_char(start_time AT TIME ZONE 'Europe/Madrid', 'HH24:MI') = 'HH:MM'.
    - Devuelve una sola consulta SELECT (o WITH...SELECT), sin markdown ni comentarios, LIMIT 20.
    Ejemplo:
    - Pregunta: "quien esta el dia 24"
    - SQL: SELECT full_name FROM public."Person" WHERE arrival_date <= (date_trunc('month', CURRENT_DATE)::date + INTERVAL '23 day')::date AND (departure_date IS NULL OR departure_date >= (date_trunc('month', CURRENT_DATE)::date + INTERVAL '23 day')::date) LIMIT 20
    """

    try:
        limit_objetivo = _limit_por_intencion(pregunta)
        fecha_explicita_iso = _resolver_fecha_explicita(pregunta)
        contexto_fecha = (
            f"\nFecha detectada en la pregunta (usar literalmente): {fecha_explicita_iso}."
            if fecha_explicita_iso
            else ""
        )

        # PASO A: Generar el SQL
        res_sql = claude.messages.create(
            model=model_claude,
            max_tokens=180,
            system=esquema_detallado,
            messages=[{"role": "user", "content": f"Genera el SQL para: {pregunta}.{contexto_fecha}"}]
        )
        sql_query = _asegurar_limit(res_sql.content[0].text, limit_default=limit_objetivo)
        
        # Log para debug en Railway
        print(f"--- SQL GENERADO ---\n{sql_query}\n")

        if not _es_sql_segura_para_lectura(sql_query):
            print(f"SQL BLOQUEADA POR SEGURIDAD: {sql_query}")
            return (
                "Solo puedo ejecutar consultas de lectura seguras (SELECT sin "
                "múltiples sentencias). Reformula la pregunta, por favor."
            )

        # PASO B: Ejecutar en Supabase vía RPC
        db_res = supabase.rpc("exec_sql", {"query_text": sql_query}).execute()
        if getattr(db_res, "error", None):
            print(f"ERROR SUPABASE RPC: {db_res.error}")
            datos_crudos = {"error": str(db_res.error)}
        else:
            datos_crudos = getattr(db_res, "data", None)
            if datos_crudos is None:
                datos_crudos = []

        # Fallback barato: si hay fecha clara y no hay resultados,
        # pide una segunda SQL más literal con esa fecha.
        if (
            not getattr(db_res, "error", None)
            and isinstance(datos_crudos, list)
            and len(datos_crudos) == 0
            and fecha_explicita_iso
        ):
            print("Reintentando SQL por resultado vacio con fecha explicita...")
            res_sql_retry = claude.messages.create(
                model=model_claude,
                max_tokens=120,
                system=esquema_detallado,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Reformula la consulta SQL para: {pregunta}. "
                        f"Usa obligatoriamente la fecha literal DATE '{fecha_explicita_iso}'. "
                        f"Manten una sola sentencia SELECT y LIMIT {limit_objetivo}."
                    )
                }]
            )
            sql_query_retry = _asegurar_limit(res_sql_retry.content[0].text, limit_default=limit_objetivo)
            print(f"--- SQL RETRY ---\n{sql_query_retry}\n")
            if _es_sql_segura_para_lectura(sql_query_retry):
                db_res_retry = supabase.rpc("exec_sql", {"query_text": sql_query_retry}).execute()
                if getattr(db_res_retry, "error", None):
                    print(f"ERROR SUPABASE RPC RETRY: {db_res_retry.error}")
                else:
                    datos_retry = getattr(db_res_retry, "data", None)
                    if isinstance(datos_retry, list):
                        datos_crudos = datos_retry

        total_global = None
        if _quiere_total_completo(pregunta) and _es_sql_segura_para_lectura(sql_query):
            try:
                sql_count = _sql_para_count_total(sql_query)
                print(f"--- SQL COUNT ---\n{sql_count}\n")
                db_res_count = supabase.rpc("exec_sql", {"query_text": sql_count}).execute()
                if not getattr(db_res_count, "error", None):
                    count_data = getattr(db_res_count, "data", None)
                    if isinstance(count_data, list) and count_data:
                        total_global = count_data[0].get("total")
            except Exception as count_error:
                print(f"ERROR COUNT TOTAL: {count_error}")

        # PASO C: Traducir a respuesta humana
        total_filas = len(datos_crudos) if isinstance(datos_crudos, list) else None
        prompt_humano = (
            f'Pregunta: "{pregunta}"\n'
            f"Datos: {datos_crudos}\n"
            f"Filas devueltas por la consulta: {total_filas}\n"
            f"Total global estimado (sin LIMIT): {total_global}\n"
            "Si aparecen fechas/horas de eventos, expresalas en horario de Menorca (Europe/Madrid). "
            "No digas 'todos' o 'lista completa' si no puedes garantizarlo; "
            "si hay total global, di claramente 'te muestro X de Y'. "
            "Si no hay total global, di 'estos son los resultados encontrados' e indica el numero de filas cuando aplique. "
            "Responde en espanol, breve y clara. Si no hay datos, dilo. "
            "Si hay error tecnico, explicalo en una frase."
        )

        res_final = claude.messages.create(
            model=model_claude,
            max_tokens=180,
            messages=[{"role": "user", "content": prompt_humano}]
        )
        return res_final.content[0].text

    except Exception as e:
        print(f"ERROR EN EL FLUJO: {e}")
        return f"Lo siento, tuve un problema al procesar esa consulta. (Error: {str(e)})"

# --- EVENTO DE SLACK ---

@app.event("app_mention")
def handle_mentions(event, say):
    _procesar_evento_pregunta(event, say, limpiar_menciones=True)


@app.event("message")
def handle_private_messages(event, say):
    # Solo procesa DMs del usuario para evitar ruido y loops.
    if event.get("channel_type") != "im":
        return
    if event.get("bot_id") or event.get("subtype"):
        return

    _procesar_evento_pregunta(event, say, limpiar_menciones=False)

# --- RUTAS PARA RAILWAY ---

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

if __name__ == "__main__":
    flask_app.run(port=int(os.environ.get("PORT", 3000)))