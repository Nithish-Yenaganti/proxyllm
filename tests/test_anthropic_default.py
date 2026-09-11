import unittest
from providers.anthropic import translate_anthropic_request
from tests.test_providers import build_anthropic_request


class AnthropicDefaultTests(unittest.TestCase):
    def test_default_and_explicit_limits_in_both_stream_modes(self):
        for stream in (False, True):
            for supplied, expected in (({},2048),({'max_tokens':100},100),({'max_completion_tokens':4096},4096)):
                with self.subTest(stream=stream, supplied=supplied):
                    body = {'messages':[{'role':'user','content':'hello'}], 'stream':stream, **supplied}
                    translated = translate_anthropic_request(build_anthropic_request(body))
                    self.assertEqual(translated['max_tokens'], expected)
