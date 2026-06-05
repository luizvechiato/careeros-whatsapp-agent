"""
Career OS — Agente WhatsApp v5
Fluxo: WhatsApp → transcrição Z-API (áudio) → Claude → Make webhook → Asana/Notion/Calendar/etc.
v5: integração Make, transcrição de voz nativa Z-API, self-ping anti-sleep
"""

from flask import Flask, request, jsonify
import urllib.request
import urllib.parse
import json
import os
import threading
import time

app = Flask(__name__)

# ── Variáveis de ambiente ────────────────────────────────────────────────────
ANTHROPIC_KEY     = os.environ["ANTHROPIC_KEY"]
NOTION_TOKEN      = os.environ["NOTION_TOKEN"]
ZAPI_INSTANCE     = os.environ["ZAPI_INSTANCE"]
ZAPI_TOKEN        = os.environ["ZAPI_TOKEN"]
ZAPI_CLIENT_TOKEN = os.environ["ZAPI_CLIENT_TOKEN"]
ZAPI_BASE         = f"https://api.z-api.io/instances/{ZAPI_INSTANCE}/token/{ZAPI_TOKEN}"
MAKE_WEBHOOK_URL  = os.environ.get("MAKE_WEBHOOK_URL", "https://hook.us2.make.com/mpcr21sy4xdfxuw454x70e86ese0ppxt")
RENDER_URL        = os.environ.get("RENDER_URL", "https://careeros-whatsapp-agent.onrender.com")
PING_INTERVAL     = 4 * 60  # 4 min — Render dorme após 15min

# ── System prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """Você é o agente de IA do Career OS de Luiz Vechiato.

Recebe mensagens via WhatsApp (texto ou transcrição de voz) e transforma em ações executáveis.

TAREFA PRINCIPAL:
Converter a mensagem em um texto limpo e direto para criar uma tarefa, nota ou ação.
Retorne SEMPRE um JSON com este formato:

{
  "text": "Texto limpo da tarefa/nota/ação — direto, sem pronomes desnecessários",
  "tipo": "tarefa | nota | agenda | email | planilha | outreach",
  "confirmacao": "Mensagem curta confirmando o que foi feito, com CTA no final"
}

REGRAS:
- "text" deve ser o conteúdo principal — direto, sem "Eu preciso", "Quero", etc.
- Use verbos no infinitivo para tarefas: "Enviar proposta para X", "Revisar CV"
- Para notas: título claro + conteúdo separado por " — "
- Para agenda: inclua data/hora se mencionado: "Reunião com X — 15/06 14h"
- "confirmacao" sempre termina com um CTA curto
- Máximo 2 linhas na confirmacao — WhatsApp não é email

EXEMPLOS:
Entrada: "lembra de mandar o currículo pro João amanhã"
Saída: {"text": "Enviar currículo para João", "tipo": "tarefa", "confirmacao": "✅ Tarefa criada: Enviar currículo para João\n\nO que mais posso registrar?"}

Entrada: "reunião com a Laura na sexta às 10"
Saída: {"text": "Reunião com Laura — sexta 10h", "tipo": "agenda", "confirmacao": "✅ Registrado: Reunião com Laura na sexta às 10h\n\nQuer que eu adicione algum contexto?"}

Entrada: "insight interessante: empresas de IA estão priorizando perfis bilíngues com experiência em go-to-market"
Saída: {"text": "Insight de mercado — Empresas de IA priorizando perfis bilíngues com experiência GTM", "tipo": "nota", "confirmacao": "✅ Nota registrada no Career OS\n\nQuer explorar esse insight ou registrar mais alguma coisa?"}"""

# ── Self-ping anti-sleep ─────────────────────────────────────────────────────

def self_ping_loop():
    time.sleep(60)
    while True:
        try:
            req = urllib.request.Request(f"{RENDER_URL}/health")
            with urllib.request.urlopen(req, timeout=10) as r:
                print(f"🔁 Self-ping OK — {r.status}")
        except Exception as e:
            print(f"⚠️  Self-ping falhou: {e}")
        time.sleep(PING_INTERVAL)

def iniciar_self_ping():
    t = threading.Thread(target=self_ping_loop, daemon=True)
    t.start()
    print(f"🔁 Self-ping iniciado ({PING_INTERVAL//60}min)")


# ── Z-API: transcrição de áudio ──────────────────────────────────────────────

def zapi_transcrever_audio(audio_url):
    """Transcreve áudio via Z-API audio-to-text."""
    headers = {
        "Content-Type": "application/json",
        "Client-Token": ZAPI_CLIENT_TOKEN
    }
    payload = {"url": audio_url}
    req = urllib.request.Request(
        f"{ZAPI_BASE}/audio-to-text",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
            print(f"🎙️ Transcrição: {result}")
            return result.get("text") or result.get("transcription") or ""
    except Exception as e:
        print(f"❌ Erro transcrição Z-API: {e}")
        return ""


# ── Claude ───────────────────────────────────────────────────────────────────

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
        headers=headers,
        method="POST"
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())["content"][0]["text"]


# ── Make webhook ─────────────────────────────────────────────────────────────

def enviar_para_make(texto, tipo="tarefa", metadata=None):
    """Envia JSON para o webhook do Make."""
    payload = {
        "text": texto,
        "tipo": tipo,
        "fonte": "whatsapp",
    }
    if metadata:
        payload.update(metadata)

    req = urllib.request.Request(
        MAKE_WEBHOOK_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resultado = r.read().decode()
            print(f"✅ Make recebeu: {resultado[:100]}")
            return True
    except Exception as e:
        print(f"❌ Erro Make webhook: {e}")
        return False


# ── Z-API: enviar mensagem ───────────────────────────────────────────────────

def zapi_enviar(telefone, mensagem):
    headers = {"Content-Type": "application/json", "Client-Token": ZAPI_CLIENT_TOKEN}
    payload = {"phone": telefone, "message": mensagem}
    req = urllib.request.Request(
        f"{ZAPI_BASE}/send-text",
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            print(f"✅ Enviado para {telefone}: {result.get('messageId','?')}")
            return result
    except Exception as e:
        print(f"❌ Erro Z-API envio: {e}")
        return None


# ── Parsing de mensagem ──────────────────────────────────────────────────────

def extrair_mensagem(data):
    """Extrai texto e tipo da mensagem Z-API."""
    # Texto direto
    texto = (
        data.get("text", {}).get("message") or
        data.get("body") or
        data.get("message") or ""
    )
    if texto:
        return texto.strip(), "texto", None

    # Áudio — tenta pegar URL para transcrição
    if data.get("audio") or data.get("type") in ("AudioMessage", "PTT"):
        audio_url = (
            data.get("audio", {}).get("audioUrl") or
            data.get("audio", {}).get("url") or
            data.get("audioUrl") or ""
        )
        return "", "audio", audio_url

    # Imagem
    if data.get("image"):
        caption = data.get("image", {}).get("caption", "")
        return f"[IMAGEM{': ' + caption if caption else ''}]", "imagem", None

    return "", "desconhecido", None


# ── Webhook principal ────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    telefone = ""
    try:
        data = request.get_json(force=True)
        print(f"📩 Recebido: {json.dumps(data)[:300]}")

        if data.get("fromMe"):
            return jsonify({"status": "ignored"}), 200

        telefone = data.get("phone") or data.get("from", "").replace("@c.us", "")
        if not telefone:
            return jsonify({"status": "no_phone"}), 200

        texto, tipo, audio_url = extrair_mensagem(data)

        # ── Áudio: transcrever ────────────────────────────────────────────
        if tipo == "audio":
            if audio_url:
                zapi_enviar(telefone, "🎙️ _Transcrevendo seu áudio..._")
                transcricao = zapi_transcrever_audio(audio_url)
                if transcricao:
                    texto = transcricao
                    tipo = "texto"
                    print(f"🎙️ Transcrito: '{texto[:80]}'")
                else:
                    zapi_enviar(telefone,
                        "🎙️ Recebi o áudio mas não consegui transcrever.\n\n"
                        "_Tenta mandar em texto — o que você queria registrar?_"
                    )
                    return jsonify({"status": "ok", "acao": "audio_sem_transcricao"}), 200
            else:
                zapi_enviar(telefone,
                    "🎙️ Recebi seu áudio!\n\n"
                    "Ainda não consegui acessar o arquivo de voz. "
                    "_Me manda em texto e registro na hora._"
                )
                return jsonify({"status": "ok", "acao": "audio_sem_url"}), 200

        # ── Imagem ────────────────────────────────────────────────────────
        if tipo == "imagem":
            zapi_enviar(telefone,
                "🖼️ Recebi sua imagem!\n\n"
                "_Ainda não processo imagens. Me manda o contexto em texto._"
            )
            return jsonify({"status": "ok", "acao": "imagem_sem_suporte"}), 200

        # ── Sem conteúdo ──────────────────────────────────────────────────
        if not texto:
            zapi_enviar(telefone,
                "Recebi sua mensagem mas não identifiquei o conteúdo. 🤔\n\n"
                "_O que posso registrar ou fazer por você?_"
            )
            return jsonify({"status": "ok", "acao": "sem_conteudo"}), 200

        # ── Claude: parsear intenção ───────────────────────────────────────
        print(f"🤖 Claude processando: '{texto[:80]}'")
        resposta_raw = claude(texto)
        print(f"🤖 Claude retornou: {resposta_raw[:200]}")

        try:
            rj = json.loads(resposta_raw)
        except Exception:
            # Claude não retornou JSON válido — usa texto direto
            rj = {
                "text": texto,
                "tipo": "nota",
                "confirmacao": f"✅ Registrado: {texto[:60]}\n\nQuer adicionar mais alguma coisa?"
            }

        texto_make = rj.get("text", texto)
        tipo_acao = rj.get("tipo", "nota")
        confirmacao = rj.get("confirmacao", f"✅ Registrado!\n\nQuer fazer mais alguma coisa?")

        # ── Enviar para Make ──────────────────────────────────────────────
        make_ok = enviar_para_make(texto_make, tipo_acao)

        if not make_ok:
            confirmacao = (
                f"⚠️ Processado pelo CareerOS, mas houve um erro ao enviar para o Make.\n\n"
                f"Conteúdo: _{texto_make}_\n\n"
                f"_Tenta novamente ou me avisa se quiser registrar de outra forma._"
            )

        zapi_enviar(telefone, confirmacao)
        return jsonify({"status": "ok", "acao": tipo_acao, "make": make_ok}), 200

    except Exception as e:
        print(f"❌ Erro geral: {e}")
        if telefone:
            zapi_enviar(telefone,
                "❌ Ocorreu um erro interno.\n\n"
                "_Tenta novamente em instantes._"
            )
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "Career OS Agent online ⚡",
        "version": "5.0",
        "make_webhook": MAKE_WEBHOOK_URL[:50] + "..."
    }), 200


# ── Startup ──────────────────────────────────────────────────────────────────

iniciar_self_ping()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
