"""Model policy, administrator scope, inspection parity, and cache safety."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch
import httpx

from api import main
from providers.anthropic import AnthropicAdapter
from providers.base import AdapterRequest, ProviderCredential, ProviderRequestError
from providers.inspection import inspect_payload
from providers.parameters import apply_sampling_policy, load_drop_policy, resolve_drop_fields, SAMPLING, FIREWORKS_MODEL
from providers.registry import MODEL_ROUTES
from tests import test_inspection


class ParameterPolicyTests(unittest.TestCase):
    def test_provider_default_and_model_override(self):
        policy = load_drop_policy('{"anthropic":["temperature","top_p","top_k"]}', MODEL_ROUTES)
        self.assertEqual(resolve_drop_fields(policy, 'anthropic', 'claude-sonnet-5'), SAMPLING)
        self.assertEqual(resolve_drop_fields(policy, 'anthropic', 'future-model'), SAMPLING)
        self.assertEqual(resolve_drop_fields(policy, 'fireworks', FIREWORKS_MODEL), frozenset())
        for raw in [
            '{"anthropic":["temperature"],"claude-sonnet-5":[]}',
            '{"claude-sonnet-5":[],"anthropic":["temperature"]}',
        ]:
            policy = load_drop_policy(raw, MODEL_ROUTES)
            self.assertEqual(resolve_drop_fields(policy, 'anthropic', 'claude-sonnet-5'), frozenset())
            self.assertEqual(resolve_drop_fields(policy, 'anthropic', 'future-model'), {'temperature'})

    def test_forward_without_mutation_or_defaults(self):
        body = {"temperature": 0, "top_p": 0.9, "top_k": 20, "messages": []}
        original = deepcopy(body)
        result, removed = apply_sampling_policy(body, "fireworks", FIREWORKS_MODEL)
        self.assertEqual(result, original)
        self.assertEqual(removed, [])
        result["messages"].append("changed")
        self.assertEqual(body, original)
        result, _ = apply_sampling_policy({}, "anthropic", "claude-sonnet-5")
        self.assertEqual(result, {})

    def test_strict_and_selected_removal(self):
        with self.assertRaises(ProviderRequestError):
            apply_sampling_policy({"temperature": 0}, "anthropic", "claude-sonnet-5")
        effective, dropped = apply_sampling_policy(
            {"temperature": 0, "tools": []}, "anthropic", "claude-sonnet-5", {"temperature"},
        )
        self.assertEqual(effective, {"tools": []})
        self.assertEqual(dropped, ["temperature"])
        with self.assertRaises(ProviderRequestError):
            apply_sampling_policy({"top_p": 0.9}, "anthropic", "claude-sonnet-5", {"temperature"})

    def test_bad_sampling_values(self):
        for field, values in {"temperature": [True, -1, 3, float('nan'), "0"],
                              "top_p": [-1, 2, float('inf')], "top_k": [-1, 101, 1.5, False]}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ProviderRequestError):
                    apply_sampling_policy({field: value}, "fireworks", FIREWORKS_MODEL)

    def test_config_resolves_aliases_and_rejects_unsafe_overrides(self):
        policy = load_drop_policy('{"claude-sonnet-5":["temperature"]}', MODEL_ROUTES)
        self.assertEqual(policy[("anthropic", "claude-sonnet-5")], {"temperature"})
        self.assertEqual(load_drop_policy('{}', MODEL_ROUTES), {})
        for raw in ['[]', '{"unknown":[]}', '{"claude-sonnet-5":["tools"]}',
                    '{"claude-sonnet-5":"temperature"}',
                    '{"claude-sonnet-5":[],"anthropic/claude-sonnet-5":["top_p"]}']:
            with self.assertRaises(ValueError):
                load_drop_policy(raw, MODEL_ROUTES)


class ParameterWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_matches_wire_and_essential_features_stay_rejected(self):
        async def connected():
            return False
        for stream in [False, True]:
            captured = []
            def transport(request):
                captured.append(json.loads(request.content))
                return httpx.Response(400, json={"error": {"message": "fixture"}})
            adapter = AnthropicAdapter(lambda: httpx.AsyncClient(transport=httpx.MockTransport(transport)))
            body = {"messages": [{"role": "user", "content": "Hi"}], "stream": stream,
                    "temperature": 0, "top_p": 0.9, "top_k": 20}
            request = AdapterRequest(body, "alias", "claude-sonnet-5",
                ProviderCredential("https://mock.test", "secret"), connected, SAMPLING)
            report = inspect_payload(adapter, request, original_body=body)
            self.assertEqual(report["status"], "prepared")
            self.assertEqual({item['field'] for item in report['dropped_parameters']}, SAMPLING)
            await adapter.send(request)
            self.assertEqual(captured, [report['payload']])
            self.assertTrue(all(field not in captured[0] for field in SAMPLING))
            body['tools'] = []
            self.assertEqual(inspect_payload(adapter, request, original_body=body)['status'], 'blocked')


class ParameterHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = test_inspection.InspectionHTTPTests.asyncSetUp

    async def test_effective_settings_drive_cache_and_wire(self):
        captured = []
        def transport(request):
            captured.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"message": "fixture"}})
        self.app.state.provider_adapters['anthropic'] = AnthropicAdapter(
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(transport)))
        body = {**self.body, "temperature": 0, "top_p": 0.9, "top_k": 20}
        body.pop('cache')
        with patch.object(main, 'PARAMETER_DROP_POLICY', {('anthropic',None): SAMPLING}):
            preview = await self.client.post('/v1/inspect', json=body)
            self.assertEqual(preview.status_code, 200)
            self.usage.assert_not_awaited()
            self.cache.assert_not_awaited()
            response = await self.client.post('/v1/chat/completions', json=body)
            self.assertEqual(response.status_code, 400)  # mock upstream, not policy rejection
            self.assertEqual(captured, [preview.json()['payload']])
            self.cache.assert_not_awaited()  # dropped temperature=0 must not enable cache
            self.cache.return_value = None
            await self.client.post('/v1/chat/completions', json={**body, 'cache': True})
            self.cache.assert_awaited_once()

    async def test_client_cannot_enable_drop_policy(self):
        response = await self.client.post('/v1/inspect', json={**self.body,
            'temperature': 0, 'drop_sampling_params': ['temperature']})
        self.assertEqual(response.status_code, 400)
