"""
Career OS — Agente WhatsApp v5.7
Webhook Z-API → Claude → execução direta Asana/Google Calendar
v5.7: execução direta Asana/Google Calendar; Make apenas fallback opcional
"""

from flask import Flask, request, jsonify
import urllib.request
import urllib.error
import urllib.parse
import json
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)

# ── Variáveis de ambiente ──────────────────────────────────────────────────
ANTHROPIC_KEY      = os.environ.get("ANTHROPIC_KEY", "")
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
ZAPI_INSTANCE      = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN         = os.environ.get("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN  = os.environ.get("ZAPI_CLIENT_TOKEN", "")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
MAKE_WEBHOOK_URL   = os.environ.get("MAKE_WEBHOOK_URL", "")
USE_MAKE_FALLBACK = os.environ.get("USE_MAKE_FALLBACK", "false").lower() == "true"

ASANA_TOKEN = os.environ.get("ASANA_TOKEN", "")
ASANA_WORKSPACE_GID = os.environ.get("ASANA_WORKSPACE_GID", "1214916782248921")
ASANA_PROJECT_GID = os.environ.get("ASANA_PROJECT_GID", "1215210669565722")

GOOGLE_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "lhvechiato@gmail.com")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

RENDER_URL         = os.environ.get("RENDER_URL", "https://careeros-whatsapp-agent.onrender.com")

ZAPI_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"

# Anti-loop: IDs de mensagens que o próprio bot enviou
sent_message_ids = set()

# ── System Prompt ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Você é o assistente de IA do Career OS, integrado ao WhatsApp de Luiz Vechiato.

Você é inteligente, direto e conversacional — funciona como uma extensão natural do Claude no terminal, mas via WhatsApp. Responde tanto a comandos de ação quanto a conversas livres, perguntas e bate-papo.

INTENÇÕES POSSÍVEIS:
- tarefa → criar no Asana (ex: "lembra de ligar para o João", "tarefa: revisar proposta")
- agenda → criar no Google Calendar (ex: "reunião amanhã às 15h com Pedro")
- nota → registrar no Notion (ex: "anota aí: insight sobre posicionamento")
- email → rascunho no Outlook (ex: "escreve um email para a Ana sobre a vaga")
- planilha → atualizar Excel (ex: "adiciona na planilha: R$500 de consultoria")
- conversa → responder diretamente, sem ação externa (qualquer outra coisa)

INSTRUÇÕES GERAIS:
1. Para ações (tarefa/agenda/nota/email/planilha): extraia título e detalhes. A resposta deve confirmar brevemente o que foi identificado.
2. Para conversa, perguntas, bate-papo ou saudações: responda de forma natural, inteligente e útil — sem forçar classificação de ação. Seja o assistente que Luiz precisa.
3. Se a mensagem for ambígua, pergunte para confirmar antes de executar.
4. Use português brasileiro, tom direto e humano.
5. Respostas de WhatsApp devem ser curtas (máx 3 linhas), exceto quando Luiz pedir explicação.

RESPONDA SEMPRE COM JSON VÁLIDO:
{
  "resposta": "texto para enviar no WhatsApp",
  "tipo": "tarefa | agenda | nota | email | planilha | conversa",
  "titulo": "título extraído (vazio se conversa)",
  "detalhes": "informações complementares (vazio se conversa)",
  "projeto": "projeto relacionado, se houver",
  "data_agenda": "data/hora em DD/MM/YYYY HH:mm, obrigatório para agenda quando possível",
  "participantes": ["nomes dos participantes, sem inventar e-mails"]
}

REGRAS DE EXTRAÇÃO:
- Para agenda, tente preencher data_agenda no formato DD/MM/YYYY HH:mm.
- Se faltar data ou horário para agenda, faça pergunta de confirmação em vez de executar.
- Para tarefa, use titulo como nome da tarefa e detalhes como descrição.
- Não invente e-mails de participantes; use apenas nomes quando o usuário falar nomes.
"""

# ── Helpers ────────────────────────────────────────────────────────────────

def agora_br():
    """Retorna timestamp em horário de Brasília, formato DD/MM/YYYY HH:MM:SS."""
    return datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y %H:%M:%S")


def claude(mensagem):
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": mensagem}]
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read())
        return resp["content"][0]["text"]


def groq_transcrever(audio_url):
    """Baixa áudio da URL e transcreve com Groq Whisper.
    Tenta até 3 vezes com delay progressivo — a URL do Z-API pode demorar
    alguns segundos para ficar disponível após o webhook chegar.
    """
    audio_bytes = None
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(audio_url, timeout=30) as r:
                audio_bytes = r.read()
            if audio_bytes:
                break
        except Exception as e:
            print(f"Download áudio tentativa {tentativa+1}/3: {e}")
            time.sleep(3 * (tentativa + 1))  # 3s, 6s, 9s
    if not audio_bytes:
        raise Exception(f"Áudio não disponível após 3 tentativas: {audio_url}")

    # Montar multipart/form-data manualmente
    boundary = "----CareerOSBoundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="audio.ogg"\r\n'
        f"Content-Type: audio/ogg\r\n\r\n"
    ).encode() + audio_bytes + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model"\r\n\r\n'
        f"whisper-large-v3-turbo\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        data=body,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        result = json.loads(r.read())
        return result.get("text", "")


def montar_payload_padrao(tipo, titulo, detalhes, recebido_em, projeto="", data_agenda="", participantes=None):
    """Monta o payload padrão usado pelo ecossistema do Luiz."""
    return {
        "tipo": tipo,
        "text": titulo,
        "detalhes": detalhes,
        "projeto": projeto or "",
        "data_agenda": data_agenda or "",
        "participantes": participantes or [],
        "fonte": "whatsapp",
        "recebido_em": recebido_em,
    }


def make_enviar(tipo, titulo, detalhes, recebido_em, projeto="", data_agenda="", participantes=None):
    """Fallback opcional: envia payload ao Make webhook no padrão oficial."""
    payload = [montar_payload_padrao(tipo, titulo, detalhes, recebido_em, projeto, data_agenda, participantes)]
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        MAKE_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def asana_criar_tarefa(evento):
    """Cria tarefa diretamente no Asana, substituindo o módulo asana:CreateTask do Make."""
    if not ASANA_TOKEN:
        raise RuntimeError("ASANA_TOKEN não configurado")

    notes = (
        f"{evento.get('detalhes', '')}\n"
        f"Projeto: {evento.get('projeto', '')}\n"
        f"Data: {evento.get('data_agenda', '')}\n"
        f"Participantes: {', '.join(evento.get('participantes') or [])}"
    ).strip()

    payload = {
        "data": {
            "name": evento.get("text") or "Tarefa sem título",
            "notes": notes,
            "workspace": ASANA_WORKSPACE_GID,
            "projects": [ASANA_PROJECT_GID],
        }
    }
    req = urllib.request.Request(
        "https://app.asana.com/api/1.0/tasks",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ASANA_TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def google_access_token():
    """Obtém access token Google via OAuth refresh token."""
    missing = [name for name, value in {
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
        "GOOGLE_REFRESH_TOKEN": GOOGLE_REFRESH_TOKEN,
    }.items() if not value]
    if missing:
        raise RuntimeError("Credenciais Google ausentes: " + ", ".join(missing))

    form = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": GOOGLE_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        token_data = json.loads(r.read())
    if "access_token" not in token_data:
        raise RuntimeError("Google OAuth não retornou access_token")
    return token_data["access_token"]


def google_calendar_criar_evento(evento):
    """Cria evento diretamente no Google Calendar, substituindo o módulo do Make."""
    data_agenda = evento.get("data_agenda") or ""
    if not data_agenda:
        raise RuntimeError("data_agenda é obrigatória para criar evento")

    tz = ZoneInfo("America/Sao_Paulo")
    try:
        start_dt = datetime.strptime(data_agenda, "%d/%m/%Y %H:%M").replace(tzinfo=tz)
    except ValueError as exc:
        raise RuntimeError("data_agenda deve estar no formato DD/MM/YYYY HH:mm") from exc
    end_dt = start_dt + timedelta(hours=1)

    body = {
        "summary": evento.get("text") or "Evento sem título",
        "description": (
            f"Projeto: {evento.get('projeto', '')}\n"
            f"Participantes: {', '.join(evento.get('participantes') or [])}\n"
            f"Detalhes: {evento.get('detalhes', '')}"
        ),
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Sao_Paulo"},
        "visibility": "default",
        "transparency": "opaque",
        "guestsCanModify": False,
        "guestsCanInviteOthers": True,
        "guestsCanSeeOtherGuests": True,
    }

    access_token = google_access_token()
    calendar_id = urllib.parse.quote(GOOGLE_CALENDAR_ID, safe="")
    req = urllib.request.Request(
        f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events?sendUpdates=all",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def executar_acao(tipo, evento):
    """Executa ação diretamente no CareerOS; Make fica apenas como fallback opcional."""
    if tipo == "tarefa":
        return "Asana", asana_criar_tarefa(evento)
    if tipo == "agenda":
        return "Google Calendar", google_calendar_criar_evento(evento)

    if USE_MAKE_FALLBACK and MAKE_WEBHOOK_URL:
        make_enviar(
            tipo,
            evento.get("text", ""),
            evento.get("detalhes", ""),
            evento.get("recebido_em", ""),
            evento.get("projeto", ""),
            evento.get("data_agenda", ""),
            evento.get("participantes", []),
        )
        return "Make", {"status": "fallback_sent"}

    raise RuntimeError(f"Tipo '{tipo}' ainda não tem executor direto configurado")


def zapi_enviar(telefone, mensagem):
    headers = {
        "Content-Type": "application/json",
        "client-token": ZAPI_CLIENT_TOKEN,
    }
    payload = {"phone": telefone, "message": mensagem}
    req = urllib.request.Request(
        f"{ZAPI_BASE}/send-text",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            msg_id = result.get("zaapId") or result.get("messageId")
            if msg_id:
                sent_message_ids.add(msg_id)
            return result
    except Exception as e:
        print(f"Erro Z-API enviar: {e}")
        return None


# ── Webhook principal ──────────────────────────────────────────────────────

@app.route("/webhook", methods=["GET"])
def webhook_verify():
    """Meta Cloud API webhook verification (hub challenge)."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    verify_token = os.environ.get("META_VERIFY_TOKEN", "careeros2026")
    if mode == "subscribe" and token == verify_token:
        print(f"[META] Webhook verificado com sucesso")
        return challenge, 200
    print(f"[META] Falha na verificação do webhook: mode={mode} token={token}")
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    recebido_em = agora_br()   # captura imediato ao chegar no servidor

    try:
        data = request.get_json(force=True)
        print(f"[{recebido_em}] Webhook: {json.dumps(data)[:300]}")

        # Anti-loop: ignorar mensagens enviadas pelo próprio bot
        msg_id = data.get("messageId") or data.get("id")
        if msg_id and msg_id in sent_message_ids:
            return jsonify({"status": "ignored_own"}), 200
        if data.get("fromMe") and not data.get("phone"):
            return jsonify({"status": "ignored_fromMe"}), 200

        # Extrair telefone
        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        if not telefone:
            return jsonify({"status": "no_phone"}), 200

        # Extrair texto (pode ser mensagem de texto ou áudio transcrito)
        texto = ""

        # Verificar se é áudio
        audio_url = (
            data.get("audio", {}).get("audioUrl") or
            data.get("audio", {}).get("url") or
            (data.get("type") == "audio" and data.get("body"))
        )

        if audio_url and isinstance(audio_url, str) and audio_url.startswith("http"):
            print(f"[{recebido_em}] Transcrevendo áudio: {audio_url[:80]}")
            try:
                texto = groq_transcrever(audio_url)
                print(f"[{recebido_em}] Transcrição: {texto[:100]}")
            except Exception as e:
                print(f"[{recebido_em}] Erro Groq: {e}")
                zapi_enviar(telefone, "⚠️ Não consegui transcrever o áudio. Tente enviar como texto.")
                return jsonify({"status": "groq_error"}), 200
        else:
            texto = (
                data.get("text", {}).get("message") or
                data.get("body") or
                data.get("message") or ""
            )

        if not texto:
            return jsonify({"status": "no_message"}), 200

        print(f"[{recebido_em}] De {telefone}: {texto[:100]}")

        # ACK imediato — diz que recebeu enquanto Claude processa
        zapi_enviar(telefone, f"⏳ Recebi às {recebido_em[11:]}. Processando...")

        # Processar com Claude
        resposta_raw = claude(texto)

        try:
            r = json.loads(resposta_raw)
        except:
            r = {"resposta": resposta_raw, "tipo": "conversa", "titulo": "", "detalhes": ""}

        resposta_texto = r.get("resposta", resposta_raw)
        tipo           = r.get("tipo", "conversa")
        titulo         = r.get("titulo", texto[:60])
        detalhes       = r.get("detalhes", "")
        projeto        = r.get("projeto", "")
        data_agenda    = r.get("data_agenda", "")
        participantes  = r.get("participantes") or []
        if isinstance(participantes, str):
            participantes = [p.strip() for p in participantes.split(",") if p.strip()]

        evento = montar_payload_padrao(
            tipo=tipo,
            titulo=titulo,
            detalhes=detalhes,
            recebido_em=recebido_em,
            projeto=projeto,
            data_agenda=data_agenda,
            participantes=participantes,
        )

        if tipo in {"tarefa", "agenda", "nota", "email", "planilha"}:
            try:
                destino, resultado = executar_acao(tipo, evento)
                print(f"[{recebido_em}] {destino} OK: tipo={tipo}, titulo={titulo}")
                zapi_enviar(telefone,
                    f"✅ {tipo.capitalize()} registrada no {destino}!\n"
                    f"📌 {titulo}\n"
                    f"🕐 {recebido_em}"
                )
            except Exception as e:
                print(f"[{recebido_em}] Erro executor direto: {e}")
                zapi_enviar(telefone,
                    f"⚠️ Não consegui executar {tipo}.\n"
                    f"O que pedi: {titulo}\n"
                    f"Erro: {str(e)[:120]}"
                )
        else:
            # Conversa ou pergunta — responde o que o Claude gerou
            zapi_enviar(telefone, resposta_texto)

        return jsonify({"status": "ok", "tipo": tipo, "recebido_em": recebido_em}), 200

    except Exception as e:
        print(f"[{recebido_em}] Erro webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Self-ping anti-sleep ───────────────────────────────────────────────────

def self_ping():
    """Pinga o próprio endpoint a cada 4 minutos para evitar cold start."""
    time.sleep(30)  # aguarda app subir
    while True:
        try:
            urllib.request.urlopen(f"{RENDER_URL}/health", timeout=10)
            print(f"[{agora_br()}] Self-ping OK")
        except Exception as e:
            print(f"[{agora_br()}] Self-ping erro: {e}")
        time.sleep(240)  # 4 minutos

threading.Thread(target=self_ping, daemon=True).start()


# ── Health check ───────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "5.7", "ts": agora_br()}), 200

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "Career OS WhatsApp Agent", "version": "5.7"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
