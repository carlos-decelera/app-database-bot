import os
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from flask import Flask, request
from anthropic import Anthropic
from supabase import create_client

load_dotenv()

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

def flujo_pregunta_respuesta(pregunta):
    """
    Proceso: Lenguaje Natural -> SQL -> Supabase -> Respuesta Humana
    """
    
    # 1. ESQUEMA DETALLADO Y REGLAS DE NEGOCIO (MENORCA CONTEXT)
    esquema_detallado = """
    CONTEXTO: Base de datos de un programa de emprendimiento en MENORCA.
    
    TABLAS:
    - public.Person: id, full_name, email, contact_type, expertise_tags (jsonb), startup_id, arrival_date (llegada a menorca), departure_date (salida de menorca).
      * Valores contact_type: 'experience_maker', 'team', 'vc', 'founder', 'staff'.
    - public.Startup: id, name, sector, stage.
    - public.Event: id, title, description, location, start_time, speaker_id.
    - public.UserEvent: user_id, event_id (Relaciona personas con asistencia a eventos).

    REGLAS DE SQL:
    - IMPORTANTE: Usa SIEMPRE comillas dobles para los nombres de las tablas.
        Ejemplo: public."Person", public."Startup", public."Event".
    - Si no usas comillas dobles, la consulta fallará.
    - "Experience Makers" -> contact_type = 'experience_maker'.
    - Ubicación -> Si mencionan lugares de Menorca, buscar en public.Event.location.
    - "Hoy" -> Usar CURRENT_DATE.
    - Para saber si alguien esté en el programa o en menorca comparar hoy con arrival_date y departure_date.
    - Para filtrar por expertise_tags (JSONB), usar: expertise_tags ? 'Valor'.
    - Usar siempre ILIKE para textos y LIMIT 20.
    - Responder SOLO con el código SQL puro.
    """

    try:
        # PASO A: Generar el SQL
        res_sql = claude.messages.create(
            model=model_claude,
            max_tokens=300,
            system=esquema_detallado,
            messages=[{"role": "user", "content": f"Genera el SQL para: {pregunta}"}]
        )
        sql_query = res_sql.content[0].text.strip()
        
        # Log para debug en Railway
        print(f"--- SQL GENERADO ---\n{sql_query}\n")

        # PASO B: Ejecutar en Supabase vía RPC
        db_res = supabase.rpc("exec_sql", {"query_text": sql_query}).execute()
        datos_crudos = db_res.data

        # PASO C: Traducir a respuesta humana
        prompt_humano = f"""
        El usuario preguntó: "{pregunta}"
        Los datos obtenidos de la base de datos son: {datos_crudos}
        
        Instrucciones:
        - Si hay datos, redacta una respuesta amable y concisa.
        - Si no hay datos, indica que no encontraste información sobre eso.
        - Si los datos contienen un error, explica brevemente que hubo un problema técnico.
        """

        res_final = claude.messages.create(
            model=model_claude,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt_humano}]
        )
        return res_final.content[0].text

    except Exception as e:
        print(f"ERROR EN EL FLUJO: {e}")
        return f"Lo siento, tuve un problema al procesar esa consulta. (Error: {str(e)})"

    # 3. Respuesta humana
    res_final = claude.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=500,
        system="Eres un asistente de eventos y startups amigable. Traduce los datos de la base de datos a una respuesta natural.",
        messages=[{"role": "user", "content": f"Usuario: {pregunta}\nDatos: {datos}"}]
    )
    return res_final.content[0].text

# --- EVENTO DE SLACK ---

@app.event("app_mention")
def handle_mentions(event, say):
    # Obtenemos la pregunta (quitando la mención al bot)
    texto = event['text'].split('> ')[1] if '> ' in event['text'] else event['text']
    thread_ts = event.get("ts") # Esto permite responder en el hilo
    
    # Indicamos que estamos trabajando (opcional)
    say("Buscando en la base de datos... 🔍", thread_ts=thread_ts)
    
    respuesta = flujo_pregunta_respuesta(texto)
    
    # Respondemos en el hilo
    say(text=respuesta, thread_ts=thread_ts)

# --- RUTAS PARA RAILWAY ---

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

if __name__ == "__main__":
    flask_app.run(port=int(os.environ.get("PORT", 3000)))