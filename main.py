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

# Configuración de Slack
app = App(
    token=os.getenv("SLACK_BOT_TOKEN"),
    signing_secret=os.getenv("SLACK_SIGNING_SECRET")
)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# --- LÓGICA DEL BOT ---

def flujo_pregunta_respuesta(pregunta):
    # 1. EL "MAPA" DETALLADO (Contexto es Rey)
    esquema_detallado = """
    Tablas del esquema 'public':
    - Person: id, full_name, email, startup_id (FK), expertise_tags (jsonb), contact_type, arrival_date.
    - Startup: id, name, sector, stage, website_url.
    - Event: id, title, description, start_time, location, speaker_id (FK).
    - UserEvent: user_id (FK a Person), event_id (FK a Event).
    - OneOnOne: startup_id, person_id, start_time, location.

    Relaciones importantes:
    - Para saber quién asiste a un evento: Person -> UserEvent -> Event.
    - Para saber de qué startup es una persona: Person.startup_id = Startup.id.
    - Para saber quién dio una charla: Event.speaker_id = Person.id.
    """

    instrucciones_sql = f"""
    Eres un experto en PostgreSQL para Supabase. Basado en este esquema:
    {esquema_detallado}

    Reglas:
    1. Retorna SOLO el código SQL, sin explicaciones ni bloques de código markdown.
    2. Usa ILIKE para nombres (ej: name ILIKE '%termino%').
    3. Si piden expertos en algo, busca dentro del campo jsonb 'expertise_tags'.
    4. Siempre añade LIMIT 20.
    5. Usa prefijos 'public.' para todas las tablas.
    """
    
    # Llamada a Claude para generar SQL
    res_sql = claude.messages.create(
        model="claude-3-5-sonnet-20240620",
        max_tokens=300,
        system=instrucciones_sql,
        messages=[{"role": "user", "content": pregunta}]
    )
    sql = res_sql.content[0].text.strip()
    
    # Imprimir en los logs de Railway para que puedas debugear
    print(f"DEBUG: Pregunta: {pregunta} | SQL: {sql}")

    # 2. Ejecutar en Supabase
    try:
        db_res = supabase.rpc("exec_sql", {"query_text": sql}).execute()
        datos = db_res.data
        if not datos:
            datos = "No se encontraron resultados para esa consulta."
    except Exception as e:
        return f"Ups, tuve un problema con la base de datos: {e}"

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