import json
import unittest
from unittest.mock import patch

import app


class CareerOSDirectExecutionTests(unittest.TestCase):
    def test_montar_payload_padrao_matches_ecosystem_schema(self):
        payload = app.montar_payload_padrao(
            tipo="agenda",
            titulo="Reunião com Laura",
            detalhes="Alinhamento geral",
            recebido_em="09/06/2026 09:00:00",
            projeto="Casamento Laura",
            data_agenda="09/06/2026 10:00",
            participantes=["Laura"],
        )
        self.assertEqual(payload["tipo"], "agenda")
        self.assertEqual(payload["text"], "Reunião com Laura")
        self.assertEqual(payload["projeto"], "Casamento Laura")
        self.assertEqual(payload["data_agenda"], "09/06/2026 10:00")
        self.assertEqual(payload["participantes"], ["Laura"])
        self.assertEqual(payload["fonte"], "whatsapp")

    @patch("app.urllib.request.urlopen")
    def test_asana_payload_matches_make_mapping(self, mock_urlopen):
        app.ASANA_TOKEN = "token-test"
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"data":{"gid":"123"}}'
        mock_urlopen.return_value = FakeResponse()

        evento = app.montar_payload_padrao(
            "tarefa",
            "Revisar proposta",
            "Checar orçamento",
            "09/06/2026 09:00:00",
            "Evento Cliente X",
            "10/06/2026 15:00",
            ["Ana", "Laura"],
        )
        result = app.asana_criar_tarefa(evento)
        self.assertEqual(result["data"]["gid"], "123")

        request = mock_urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(body["data"]["name"], "Revisar proposta")
        self.assertIn("Projeto: Evento Cliente X", body["data"]["notes"])
        self.assertIn("Participantes: Ana, Laura", body["data"]["notes"])
        self.assertEqual(body["data"]["workspace"], app.ASANA_WORKSPACE_GID)
        self.assertEqual(body["data"]["projects"], [app.ASANA_PROJECT_GID])

    @patch("app.google_access_token", return_value="google-token-test")
    @patch("app.urllib.request.urlopen")
    def test_google_calendar_event_payload_matches_make_mapping(self, mock_urlopen, _):
        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"id":"evt123"}'
        mock_urlopen.return_value = FakeResponse()

        evento = app.montar_payload_padrao(
            "agenda",
            "Reunião com Laura",
            "Alinhamento geral",
            "09/06/2026 09:00:00",
            "Casamento Laura",
            "09/06/2026 10:00",
            ["Laura"],
        )
        result = app.google_calendar_criar_evento(evento)
        self.assertEqual(result["id"], "evt123")

        request = mock_urlopen.call_args.args[0]
        body = json.loads(request.data.decode())
        self.assertEqual(body["summary"], "Reunião com Laura")
        self.assertIn("Projeto: Casamento Laura", body["description"])
        self.assertIn("Participantes: Laura", body["description"])
        self.assertTrue(body["start"]["dateTime"].startswith("2026-06-09T10:00:00"))
        self.assertTrue(body["end"]["dateTime"].startswith("2026-06-09T11:00:00"))
        self.assertEqual(body["start"]["timeZone"], "America/Sao_Paulo")


if __name__ == "__main__":
    unittest.main()
