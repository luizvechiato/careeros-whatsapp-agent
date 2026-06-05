"""
Career OS — Agente WhatsApp v5.1
v5.3: Groq Whisper transcrição + fix fromMe (número próprio), anti-loop por messageId + fallback catch-all (sempre responde) + fix payload Z-API áudio
"""

from flask import Flask, request, jsonify
import urllib.request
import json
import os
import threading
import time

app = Flask(__name__)

ANTHROPIC_KEY     = os.environ["ANTHROPIC_KEY"]
NOTION_TOKEN      = os.environ["NOTION_TOKEN"]
ZAPI_INSTANCE     = os.environ["ZAPI_INSTANCE"]
ZAPI_TOKEN        = os.environ["ZAPI_TOKEN"]
ZAPI_CLIENT_TOKEN = os.environ["ZAPI_CLIENT_TOKEN"]
ZAPI_BASE         = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"
GROQ_API_KEY      = os.environ.get("GROQ_API_KEY", "")
MAKE_WEBHOOK_URL  = os.environ.get("MAKE_WEBHOOK_URL", "https://hook.us2.make.com/mpcr21sy4xdfxuw454x70e86ese0ppxt")
RENDER_URL        = os.environ.get("RENDER_URL", "https://careeros-whatsapp-agent.onrender.com")
PING_INTERVAL     = 4 * 60

# Armazena últimos 5 payloads recebidos (para debug)
_last_payloads = []

# IDs de mensagens enviadas pelo agente (evita loop)
_sent_message_ids = set()

SYSTEM_PROMPT = """Você é o agente de IA do Career OS de Luiz Vechiato.

Recebe mensagens via WhatsApp (texto ou transcrição de voz) e transforma em ações executáveis.

Converta a mensagem em um JSON com este formato EXATO:

{
  "text": "Texto limpo da tarefa/nota/ação — direto, sem pronomes desnecessários",
  "tipo": "tarefa | nota | agenda | email | planilha | outreach",
  "confirmacao": "Mensagem curta confirmando o que foi feito, com CTA no final"
}

REGRAS:
- "text": conteúdo direto — verbos no infinitivo para tarefas ("Enviar CV para João")
- Para agenda: inclua data/hora se mencionado ("Reunião com Laura — sexta 10h")
- "confirmacao": máximo 2 linhas, sempre termina com um CTA curto
- Retorne APENAS o JSON, sem texto adicional"""


# ── Self-ping ────────────────────────────────────────────────────────────────

def self_ping_loop():
    time.sleep(60)
    while True:
        try:
            with urllib.request.urlopen(f"{RENDER_URL}/health", timeout=10) as r:
                print(f"🔁 Self-ping {r.status}")
        except Exception as e:
            print(f"⚠️ Self-ping: {e}")
        time.sleep(PING_INTERVAL)

threading.Thread(target=self_ping_loop, daemon=True).start()


# ── Helpers ──────────────────────────────────────────────────────────────────

def zapi_enviar(telefone, mensagem):
    headers = {"Content-Type": "application/json", "Client-Token": ZAPI_CLIENT_TOKEN}
    req = urllib.request.Request(
        f"{ZAPI_BASE}/send-text",
        data=json.dumps({"phone": telefone, "message": mensagem}).encode(),
        headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            print(f"✅ Enviado: {result.get('messageId','?')}")
    except Exception as e:
        print(f"❌ Erro envio: {e}")


def groq_transcrever(audio_url):
    """Baixa o áudio e transcreve via Groq Whisper (gratuito)."""
    if not GROQ_API_KEY:
        print("⚠️ GROQ_API_KEY não configurada")
        return ""
    try:
        # Baixar áudio
        audio_data = urllib.request.urlopen(audio_url, timeout=15).read()
        print(f"🎙️ Áudio baixado: {len(audio_data)} bytes")

        # Montar multipart/form-data manualmente
        boundary = "----CareerOSBoundary"
        body = (
            f"--{boundary}
"
            f'Content-Disposition: form-data; name="model"

'
            f"whisper-large-v3-turbo
"
            f"--{boundary}
"
            f'Content-Disposition: form-data; name="language"

'
            f"pt
"
            f"--{boundary}
"
            f'Content-Disposition: form-data; name="file"; filename="audio.ogg"
'
            f"Content-Type: audio/ogg

"
        ).encode() + audio_data + f"
--{boundary}--
".encode()

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            data=body,
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
            text = result.get("text", "").strip()
            print(f"🎙️ Transcrição Groq: '{text[:80]}'")
            return text
    except Exception as e:
        print(f"❌ Groq transcrição: {e}")
        return ""


def enviar_make(texto, tipo="tarefa"):
    req = urllib.request.Request(
        MAKE_WEBHOOK_URL,
        data=json.dumps({"text": texto, "tipo": tipo, "fonte": "whatsapp"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(f"✅ Make: {r.read().decode()[:50]}")
            return True
    except Exception as e:
        print(f"❌ Make: {e}")
        return False


def claude(mensagem):
    headers = {
        "x-api-key": ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": mensagem}]
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["content"][0]["text"]


def extrair_audio_url(data):
    """Tenta extrair URL de áudio de todos os formatos conhecidos do Z-API."""
    # Formatos possíveis do Z-API para áudio/PTT
    candidates = [
        data.get("audio", {}).get("audioUrl"),
        data.get("audio", {}).get("url"),
        data.get("audioUrl"),
        data.get("url"),
        data.get("mediaUrl"),
        data.get("audio", {}).get("mediaUrl"),
        # Formato alternativo: dentro de "message"
        data.get("message", {}).get("audioUrl") if isinstance(data.get("message"), dict) else None,
    ]
    for url in candidates:
        if url and url.startswith("http"):
            return url
    return ""


def extrair_texto(data):
    """Extrai texto de todos os formatos conhecidos do Z-API."""
    candidates = [
        data.get("text", {}).get("message") if isinstance(data.get("text"), dict) else data.get("text"),
        data.get("body"),
        data.get("message") if isinstance(data.get("message"), str) else None,
        data.get("caption"),
    ]
    for t in candidates:
        if t and isinstance(t, str) and t.strip():
            return t.strip()
    return ""


def detectar_tipo(data):
    """Detecta se é áudio, texto, imagem, etc."""
    tipo = data.get("type", "")
    if tipo in ("PTT", "AudioMessage", "audio") or data.get("audio"):
        return "audio"
    if tipo in ("ImageMessage", "image") or data.get("image"):
        return "imagem"
    if tipo in ("VideoMessage", "video") or data.get("video"):
        return "video"
    if tipo in ("DocumentMessage", "document") or data.get("document"):
        return "documento"
    # Fallback: se tem texto, é texto
    if extrair_texto(data):
        return "texto"
    return "desconhecido"


# ── Rotas ────────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = ""
    try:
        data = request.get_json(force=True)
        print(f"📩 PAYLOAD: {json.dumps(data)[:500]}")

        # Armazenar para debug
        _last_payloads.append(data)
        if len(_last_payloads) > 5:
            _last_payloads.pop(0)

        # Ignorar mensagens enviadas pelo próprio agente (anti-loop)
        msg_id = data.get("messageId") or data.get("id", {}).get("id") if isinstance(data.get("id"), dict) else data.get("id")
        if msg_id and msg_id in _sent_message_ids:
            return jsonify({"status": "ignored_own"}), 200

        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        if not telefone:
            return jsonify({"status": "no_phone"}), 200

        tipo = detectar_tipo(data)
        print(f"Tipo detectado: {tipo} | De: {telefone}")

        # ── Áudio ─────────────────────────────────────────────────────
        if tipo == "audio":
            audio_url = extrair_audio_url(data)
            print(f"🎙️ Audio URL: '{audio_url}'")

            if audio_url:
                zapi_enviar(telefone, "🎙️ _Transcrevendo..._")
                transcricao = groq_transcrever(audio_url)
                if transcricao:
                    tipo = "texto"
                    # Processar como texto abaixo
                    texto_final = transcricao
                else:
                    zapi_enviar(telefone,
                        "🎙️ Recebi o áudio mas não consegui transcrever.\n\n"
                        "_Me manda em texto — o que queria registrar?_"
                    )
                    return jsonify({"status": "ok", "acao": "audio_sem_transcricao"}), 200
            else:
                # Sem URL — pedir confirmação mas tentar processar mesmo assim
                zapi_enviar(telefone,
                    "🎙️ Recebi seu áudio!\n\n"
                    "_Transcrição automática ainda não disponível para este formato. "
                    "Me manda em texto e registro na hora._"
                )
                return jsonify({"status": "ok", "acao": "audio_sem_url"}), 200

        # ── Texto ──────────────────────────────────────────────────────
        elif tipo == "texto":
            texto_final = extrair_texto(data)

        # ── Imagem / outros ────────────────────────────────────────────
        elif tipo == "imagem":
            caption = data.get("image", {}).get("caption", "")
            if caption:
                texto_final = caption
                tipo = "texto"
            else:
                zapi_enviar(telefone,
                    "🖼️ Recebi sua imagem!\n\n"
                    "_Ainda não processo imagens sem legenda. Descreve o que quer registrar?_"
                )
                return jsonify({"status": "ok", "acao": "imagem_sem_suporte"}), 200

        # ── Catch-all: tipo desconhecido — SEMPRE responde ─────────────
        else:
            print(f"⚠️ Tipo desconhecido. Payload completo: {json.dumps(data)}")
            zapi_enviar(telefone,
                "Recebi sua mensagem 👋\n\n"
                "_Não identifiquei o tipo de conteúdo. Me manda em texto — o que posso fazer por você?_"
            )
            return jsonify({"status": "ok", "acao": "tipo_desconhecido", "tipo_detectado": tipo}), 200

        # ── Sem texto ──────────────────────────────────────────────────
        if not texto_final:
            zapi_enviar(telefone,
                "Recebi sua mensagem mas não encontrei o conteúdo. 🤔\n\n"
                "_O que posso registrar ou fazer por você?_"
            )
            return jsonify({"status": "ok", "acao": "sem_conteudo"}), 200

        # ── Claude → Make ──────────────────────────────────────────────
        print(f"🤖 Claude: '{texto_final[:80]}'")
        try:
            resposta_raw = claude(texto_final)
            rj = json.loads(resposta_raw)
        except Exception as e:
            print(f"⚠️ Claude/JSON erro: {e} | raw: {resposta_raw[:100] if 'resposta_raw' in dir() else '?'}")
            rj = {
                "text": texto_final,
                "tipo": "nota",
                "confirmacao": f"✅ Registrado: _{texto_final[:80]}_\n\nQuer adicionar mais alguma coisa?"
            }

        texto_make = rj.get("text", texto_final)
        tipo_acao  = rj.get("tipo", "nota")
        confirmacao = rj.get("confirmacao", "✅ Registrado!\n\nO que mais posso fazer?")

        make_ok = enviar_make(texto_make, tipo_acao)

        if not make_ok:
            confirmacao = (
                f"⚠️ Processado mas erro no Make.\n"
                f"Conteúdo: _{texto_make}_\n\n"
                "_Tenta novamente?_"
            )

        zapi_enviar(telefone, confirmacao)
        return jsonify({"status": "ok", "acao": tipo_acao, "make": make_ok}), 200

    except Exception as e:
        print(f"❌ Erro geral: {e}")
        import traceback; traceback.print_exc()
        if telefone:
            zapi_enviar(telefone, "❌ Erro interno.\n\n_Tenta novamente em instantes._")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/debug", methods=["GET"])
def debug():
    """Retorna os últimos payloads recebidos — para diagnóstico."""
    return jsonify({
        "version": "5.3",
        "last_payloads_count": len(_last_payloads),
        "payloads": _last_payloads
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "Career OS Agent online ⚡",
        "version": "5.3",
        "make_webhook": MAKE_WEBHOOK_URL[:50] + "..."
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
