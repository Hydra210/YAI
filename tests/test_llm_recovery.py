import json
import unittest
import llm_recovery as lr

class RecoveryHelpers(unittest.TestCase):
    def test_parse_repairs(self):
        for raw in ('{"q":"cats",}', "{'q': 'cats'}", '{"q":"cats"} trailing'):
            self.assertEqual(lr.parse_tool_args(raw)[0], {"q": "cats"})
    def test_parse_truncated(self):
        self.assertEqual(lr.parse_tool_args('{"a":[1,2')[0], {"a": [1, 2]})
    def test_invalid_history_json(self):
        self.assertEqual(json.loads(lr.normalized_args_json("garbage")), {})
    def test_coercion(self):
        params = {"n":{"type":"integer"}, "b":{"type":"boolean"}, "fmt":{"type":"string","enum":["gif","png"]}}
        self.assertEqual(lr.coerce_args(params, {"n":"4","b":"yes","fmt":"PNG"})[0],
                         {"n":4,"b":True,"fmt":"png"})
    def test_bad_value(self):
        self.assertTrue(lr.coerce_args({"n":{"type":"integer"}},{"n":"4.5"})[1])
    def test_error_classification(self):
        class Error(Exception):
            body = {"error":{"code":"tool_use_failed"}}
        self.assertTrue(lr.is_tool_error(Error("bad")))
        self.assertFalse(lr.is_tool_error(ValueError("other")))
    def test_flatten_history(self):
        history = [{"role":"system","content":"rules"},
            {"role":"assistant","tool_calls":[{"id":"c","function":{"name":"web_search","arguments":"{}"}}]},
            {"role":"tool","tool_call_id":"c","content":"found"}]
        plain = lr.flatten_for_plain(history)
        self.assertFalse(lr.has_tool_history(plain))
        self.assertIn("found", plain[-1]["content"])

if __name__ == "__main__": unittest.main()
