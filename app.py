"""
Career OS — Agente WhatsApp v5.6
Webhook Z-API → Claude → Make → Asana/Notion/Calendar
v5.4: timestamp BR | v5.5: retry audio | v5.6: ACK imediato + conversa natural + confirmação de resultado
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
  "detalhes": "informações complementares (vazio se conversa)"
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


def make_enviar(tipo, titulo, detalhes, recebido_em):
    payload = {
        "text": titulo,
        "tipo": tipo,
        "detalhes": detalhes,
        "fonte": "whatsapp",
        "recebido_em": recebido_em,
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
    recebido_em = agora_br()

    try:
        data = request.get_json(force=True)
        print(f"[{recebido_em}] Webhook: {json.dumps(data)[:300]}")

        msg_id = data.get("messageId") or data.get("id")
        if msg_id and msg_id in sent_message_ids:
            return jsonify({"status": "ignored_own"}), 200
        if data.get("fromMe") and not data.get("phone"):
            return jsonify({"status": "ignored_fromMe"}), 200

        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        if not telefone:
            return jsonify({"status": "no_phone"}), 200

        texto = ""

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

        zapi_enviar(telefone, f"⏳ Recebi às {recebido_em[11:]}. Processando...")

        resposta_raw = claude(texto)

        try:
            r = json.loads(resposta_raw)
        except:
            r = {"resposta": resposta_raw, "tipo": "conversa", "titulo": "", "detalhes": ""}

        resposta_texto = r.get("resposta", resposta_raw)
        tipo           = r.get("tipo", "conversa")
        titulo         = r.get("titulo", texto[:60])
        detalhes       = r.get("detalhes", "")

        DESTINOS = {
            "tarefa":    "Asana",
            "agenda":    "Google Calendar",
            "nota":      "Notion",
            "email":     "Outlook",
            "planilha":  "Excel",
        }

        if tipo in DESTINOS and MAKE_WEBHOOK_URL:
            destino = DESTINOS[tipo]
            try:
                make_enviar(tipo, titulo, detalhes, recebido_em)
                print(f"[{recebido_em}] Make OK: tipo={tipo}, titulo={titulo}")
                zapi_enviar(telefone,
                    f"✅ {tipo.capitalize()} registrada no {destino}!\n"
                    f"📌 {titulo}\n"
                    f"🕐 {recebido_em}"
                )
            except Exception as e:
                print(f"[{recebido_em}] Erro Make: {e}")
                zapi_enviar(telefone,
                    f"⚠️ Não consegui enviar para o {destino}.\n"
                    f"O que pedi: {titulo}\n"
                    f"Tente novamente ou acesse o {destino} diretamente."
                )
        else:
            zapi_enviar(telefone, resposta_texto)

        return jsonify({"status": "ok", "tipo": tipo, "recebido_em": recebido_em}), 200

    except Exception as e:
        print(f"[{recebido_em}] Erro webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ── Self-ping anti-sleep ───────────────────────────────────────────────────

def self_ping():
    time.sleep(30)
    while True:
        try:
            urllib.request.urlopen(f"{RENDER_URL}/health", timeout=10)
            print(f"[{agora_br()}] Self-ping OK")
        except Exception as e:
            print(f"[{agora_br()}] Self-ping erro: {e}")
        time.sleep(240)

threading.Thread(target=self_ping, daemon=True).start()


# ── Health check ───────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "5.6", "ts": agora_br()}), 200

@app.route("/", methods=["GET"])
def root():
    return jsonify({"service": "Career OS WhatsApp Agent", "version": "5.6"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
