"""
Career OS — Agente WhatsApp v5.4
Webhook Z-API → Claude → Make → Asana/Notion/Calendar
Novidade v5.4: timestamp ISO 8601 em todas as ações (recebido_em)
"""

from flask import Flask, request, jsonify
import urllib.request
import urllib.error
import urllib.parse
import json
import os
import threading
import time
from datetime import datetime, timezone

app = Flask(__name__)

# ── Variáveis de ambiente ──────────────────────────────────────────────────
ANTHROPIC_KEY      = os.environ.get("ANTHROPIC_KEY", "")
NOTION_TOKEN       = os.environ.get("NOTION_TOKEN", "")
ZAPI_INSTANCE      = os.environ.get("ZAPI_INSTANCE", "")
ZAPI_TOKEN         = os.environ.get("ZAPI_TOKEN", "")
ZAPI_CLIENT_TOKEN  = os.environ.get("ZAPI_CLIENT_TOKEN", "")
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "")
MAKE_WEBHOOK_URL   = os.environ.get("MAKE_WEBHOOK_URL", "")
RENDER_URL         = os.environ.get("RENDER_URL", "https://careeros-whatsapp-agent.onrender.com")

ZAPI_BASE = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"

# Anti-loop: IDs de mensagens que o próprio bot enviou
sent_message_ids = set()

# ── System Prompt ──────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Você é o agente de IA do Career OS — sistema de recolocação executiva de Luiz Vechiato.

Recebe mensagens de voz ou texto via WhatsApp e classifica a intenção para execução automática.

INTENÇÕES POSSÍVEIS:
- tarefa → criar no Asana via Make
- agenda → criar no Google Calendar via Make
- nota → registrar no Notion via Make
- email → rascunho no Outlook via Make
- planilha → atualizar Excel via Make
- pergunta → responder diretamente no WhatsApp

INSTRUÇÕES:
1. Identifique a intenção principal da mensagem
2. Extraia título e detalhes relevantes
3. Responda confirmando o que será feito (máx 2 linhas)
4. Use português brasileiro, tom direto

RESPONDA SEMPRE COM JSON VÁLIDO:
{
  "resposta": "texto curto para o WhatsApp",
  "tipo": "tarefa | agenda | nota | email | planilha | pergunta",
  "titulo": "título da ação extraído da mensagem",
  "detalhes": "informações complementares"
}"""

# ── Helpers ────────────────────────────────────────────────────────────────

def agora_br():
    """Retorna timestamp em horário de Brasília (UTC-3), formato DD/MM/YYYY HH:MM:SS."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%d/%m/%Y %H:%M:%S")


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
    """Baixa áudio da URL e transcreve com Groq Whisper."""
    # Baixar áudio
    with urllib.request.urlopen(audio_url, timeout=30) as r:
        audio_bytes = r.read()

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


def make_enviar(tipo, titulo, detalhes, recebido_em):
    """Envia payload ao Make webhook com timestamp."""
    payload = {
        "text": titulo,
        "tipo": tipo,
        "detalhes": detalhes,
        "fonte": "whatsapp",
        "recebido_em": recebido_em,            # DD/MM/YYYY HH:MM:SS (Brasília)
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        MAKE_WEBHOOK_URL,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


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

        # Processar com Claude
        resposta_raw = claude(texto)

        try:
            r = json.loads(resposta_raw)
        except:
            r = {"resposta": resposta_raw, "tipo": "pergunta", "titulo": texto[:60], "detalhes": ""}

        resposta_texto = r.get("resposta", resposta_raw)
        tipo           = r.get("tipo", "pergunta")
        titulo         = r.get("titulo", texto[:60])
        detalhes       = r.get("detalhes", "")

        # Enviar ao Make se for ação (não pergunta simples)
        if tipo != "pergunta" and MAKE_WEBHOOK_URL:
            try:
                make_enviar(tipo, titulo, detalhes, recebido_em)
                print(f"[{recebido_em}] Make acionado: tipo={tipo}, titulo={titulo}")
            except Exception as e:
                print(f"[{recebido_em}] Erro Make: {e}")
                resposta_texto += "\n⚠️ Ação registrada localmente (Make indisponível)."

        # Responder no WhatsApp
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
    return jsonify({"status": "ok", "version": "5.4", "ts": agora_br()}), 200

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "Career OS WhatsApp Agent", "version": "5.4"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
